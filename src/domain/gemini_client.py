"""
Exam Service — Gemini API Client
Standalone copy of the monolith's gemini_client, adapted to read
api_key from exam-service Settings instead of pipeline CFG.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

log = logging.getLogger("gemini_client")

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


def get_api_key() -> str:
    from src.core.config import get_settings
    return get_settings().gemini_api_key


def get_model(variant: str = "flash") -> str:
    from src.core.config import get_settings
    return get_settings().gemini_model


def call_gemini(
    prompt: str,
    *,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    temperature: float = 0.1,
    max_tokens: int = 4096,
    json_mode: bool = True,
    timeout: int = 60,
    max_retries: int = 3,
) -> str:
    import requests

    key = api_key or get_api_key()
    mdl = model or get_model("flash")
    url = f"{_BASE_URL}/{mdl}:generateContent"

    gen_config: dict = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
    }
    if json_mode:
        gen_config["responseMimeType"] = "application/json"

    last_exc: Optional[Exception] = None

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
    """Extract JSON from Gemini response (handles markdown code fences)."""
    text = raw.strip()
    # Strip ```json ... ``` fences
    match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if match:
        text = match.group(1)
    return json.loads(text)
