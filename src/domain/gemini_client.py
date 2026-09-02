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
) -> str:
    """
    Calls Azure OpenAI / DeepSeek (Primary) to generate report narrative/JSON.
    Falls back to Google Gemini if Azure fails or is unconfigured.
    """
    settings = get_settings()

    # 1. Primary: Azure OpenAI / DeepSeek
    azure_key = settings.azure_deepseek_api_key.strip()
    azure_url = settings.azure_deepseek_endpoint.strip()
    if azure_key and azure_url:
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
            resp = requests.post(azure_url, json=payload, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                text = data["choices"][0]["message"]["content"].strip()
                if text:
                    log.info("Successfully generated AI report via Azure OpenAI / DeepSeek (%d chars)", len(text))
                    return text
            else:
                log.warning("Azure DeepSeek returned HTTP %d: %s", resp.status_code, resp.text[:300])
        except Exception as exc:
            log.warning("Azure DeepSeek call failed: %s — trying Gemini fallback...", exc)

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
        try:
            resp = requests.post(
                url,
                params={"key": key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": gen_config,
                },
                timeout=timeout,
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2 ** attempt
                log.warning("Gemini %s (attempt %d/%d), retrying in %ds",
                            resp.status_code, attempt + 1, max_retries, wait)
                time.sleep(wait)
                last_exc = requests.HTTPError(response=resp)
                continue

            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]

        except requests.ConnectionError as exc:
            wait = 2 ** attempt
            log.warning("Gemini connection error (attempt %d/%d), retrying in %ds",
                        attempt + 1, max_retries, wait)
            time.sleep(wait)
            last_exc = exc
            continue

        except Exception as exc:
            log.error("Gemini unexpected error: %s", exc)
            raise

    raise RuntimeError(f"Gemini API failed after {max_retries} attempts: {last_exc}")


def parse_json_response(raw: str) -> dict:
    """Extract JSON from AI response (handles markdown code fences, comments, and LaTeX backslashes)."""
    if not raw or not raw.strip():
        return {}

    text = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    cleaned = re.sub(r"//[^\n]*", "", cleaned)

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

