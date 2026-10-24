"""
Exam Service — AI Client (Azure OpenAI / DeepSeek Primary, Gemini Fallback)
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from src.core.config import get_settings

log = logging.getLogger("gemini_client")

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

SERVICE = "exam"
NARRATIVE_PURPOSE = "report_narrative"
_OFF_VALUES = {"false", "0", "no", "off", "disabled"}

# EX-25: the WHOLE chain (Azure, then Gemini with its retries and back-off sleeps) must end before the
# nginx proxy_read_timeout (120 s) turns the request into a 504 while the thread keeps paying in the background.
OVERALL_DEADLINE_SECONDS = 100.0
_MIN_USEFUL_SECONDS = 3.0


class NarrativeDisabledError(RuntimeError):
    """REPORT_NARRATIVE_ENABLED is off: no paid call was made."""


def narrative_enabled() -> bool:
    """The ONE place where REPORT_NARRATIVE_ENABLED is read (default: on).

    Only an explicit "false" / "0" / "no" / "off" switches paid narrative calls off.
    """
    value = str(get_settings().report_narrative_enabled or "").strip().lower()
    return value not in _OFF_VALUES


def _log_paid_call(purpose: str, provider: str, model: str, started: float, ok: bool) -> None:
    """One line per paid request.  Never the prompt, the response or any student data."""
    log.info(
        "paid_llm_call service=%s purpose=%s provider=%s model=%s duration_ms=%d ok=%s",
        SERVICE, purpose, provider, model, int((time.monotonic() - started) * 1000), "true" if ok else "false",
    )


def call_gemini(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    json_mode: bool = True,
    timeout: int = 120,
    max_retries: int = 3,
    purpose: str = "other",
    deadline_seconds: float = OVERALL_DEADLINE_SECONDS,
) -> str:
    """
    Calls Azure OpenAI / DeepSeek (Primary) to generate report narrative/JSON.
    Falls back to Google Gemini if Azure fails or is unconfigured.

    ``deadline_seconds`` bounds the whole chain (every request timeout is clipped to what is left, a retry is
    not started when too little is left).  ``purpose="report_narrative"`` calls honour REPORT_NARRATIVE_ENABLED.
    """
    settings = get_settings()
    if purpose == NARRATIVE_PURPOSE and not narrative_enabled():
        raise NarrativeDisabledError("REPORT_NARRATIVE_ENABLED is off")

    deadline = time.monotonic() + max(float(deadline_seconds), 1.0)

    def remaining() -> float:
        return deadline - time.monotonic()

    # 1. Primary: Azure OpenAI / DeepSeek
    azure_key = settings.azure_deepseek_api_key.strip()
    azure_url = settings.azure_deepseek_endpoint.strip()
    if azure_key and azure_url:
        started = time.monotonic()
        ok = False
        try:
            import requests

            payload = {
                "model": settings.azure_deepseek_model,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}

            headers = {
                "Content-Type": "application/json",
                "api-key": azure_key,
                "Authorization": f"Bearer {azure_key}",
            }
            resp = requests.post(azure_url, json=payload, headers=headers, timeout=min(timeout, remaining()))
            if resp.status_code == 200:
                data = resp.json()
                text = data["choices"][0]["message"]["content"].strip()
                if text:
                    ok = True
                    log.info("Successfully generated AI report via Azure OpenAI / DeepSeek (%d chars)", len(text))
                    return text
            else:
                log.warning("Azure DeepSeek returned HTTP %d: %s", resp.status_code, resp.text[:300])
        except Exception as exc:
            log.warning("Azure DeepSeek call failed: %s — trying Gemini fallback...", exc)
        finally:
            _log_paid_call(purpose, "azure", settings.azure_deepseek_model, started, ok)

    # 2. Fallback: Google Gemini AI
    key = api_key or settings.gemini_api_key
    if not key:
        log.warning("Neither Azure DeepSeek nor Gemini API keys available")
        raise RuntimeError("No AI API keys configured (both Azure DeepSeek and Gemini missing)")

    mdl = model or settings.gemini_model or "gemini-2.5-flash"
    url = f"{_BASE_URL}/{mdl}:generateContent"

    gen_config: dict = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
    }
    if json_mode:
        gen_config["responseMimeType"] = "application/json"

    last_exc: Optional[Exception] = None

    import requests
    for attempt in range(max_retries):
        if remaining() < _MIN_USEFUL_SECONDS:
            log.warning("AI call chain ran out of its %.0f s deadline before attempt %d", deadline_seconds, attempt + 1)
            break
        started = time.monotonic()
        ok = False
        try:
            resp = requests.post(
                url,
                params={"key": key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": gen_config,
                },
                timeout=min(timeout, remaining()),
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = min(2 ** attempt, max(remaining() - _MIN_USEFUL_SECONDS, 0))
                log.warning("Gemini %s (attempt %d/%d), retrying in %ds",
                            resp.status_code, attempt + 1, max_retries, wait)
                last_exc = requests.HTTPError(response=resp)
                _log_paid_call(purpose, "gemini", mdl, started, False)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            ok = True
            _log_paid_call(purpose, "gemini", mdl, started, True)
            return text

        except requests.ConnectionError as exc:
            wait = min(2 ** attempt, max(remaining() - _MIN_USEFUL_SECONDS, 0))
            log.warning("Gemini connection error (attempt %d/%d), retrying in %ds",
                        attempt + 1, max_retries, wait)
            last_exc = exc
            _log_paid_call(purpose, "gemini", mdl, started, False)
            time.sleep(wait)
            continue

        except Exception as exc:
            if not ok:
                _log_paid_call(purpose, "gemini", mdl, started, False)
            log.error("Gemini unexpected error: %s", exc)
            raise

    raise RuntimeError(f"Gemini API failed after {max_retries} attempts (deadline {deadline_seconds:.0f} s): {last_exc}")


def _strip_line_comments(text: str) -> str:
    """Remove ``// ...`` comments that stand OUTSIDE JSON string literals (EX-26)."""
    out = []
    in_string = False
    escaped = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            out.append(ch)
        elif ch == "/" and text[i + 1:i + 2] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def parse_json_response(raw: str) -> dict:
    """Extract JSON from AI response (handles markdown code fences, comments, and LaTeX backslashes).

    The text is parsed as is FIRST; ``//`` comments are removed (outside string literals only) just when that fails,
    so a link or "и/или//" inside a narrative value never turns valid JSON into the deterministic fallback (EX-26).
    """
    if not raw or not raw.strip():
        return {}

    text = raw.strip()
    fenced = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    fenced = re.sub(r"\s*```$", "", fenced.strip())

    for cleaned in (fenced, _strip_line_comments(fenced)):
        # 1. Direct parse
        try:
            return json.loads(cleaned)
        except Exception:
            pass

        # 2. Try regex match for outermost JSON object
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            obj_str = match.group(0)
            try:
                return json.loads(obj_str)
            except Exception:
                # 3. Handle unescaped backslashes often present in LaTeX (e.g. \frac, \alpha)
                try:
                    fixed_slashes = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', obj_str)
                    return json.loads(fixed_slashes)
                except Exception:
                    pass

    return {}
