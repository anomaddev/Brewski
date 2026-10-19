"""Ask Grok for a second reading of a calibration window.

The heater loop does not wait on this. A missing key, a network error, or a
number far from the local fit leaves the mathematical model in charge.
The key is read from the environment and is never logged.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

API_URL = "https://api.x.ai/v1/chat/completions"
GROK_MODEL = "grok-4"
TIMEOUT_SECONDS = 30.0

_PROMPTS = {
    "cold_start": (
        "These samples are seconds and °F while a cold heater is warming. "
        "Reply with JSON only: {\"rate\": number} where rate is the average "
        "°F per second, positive when the pot is rising."
    ),
    "linear": (
        "These samples are seconds and °F during straight-line heating. "
        "Reply with JSON only: {\"rate\": number} where rate is °F per second, "
        "positive when the pot is rising."
    ),
    "coast": (
        "These samples are seconds and °F after the heater turned off, while "
        "stored heat is still arriving. Reply with JSON only: {\"rate\": number} "
        "where rate is how many °F the pot climbed from the first sample to the crest."
    ),
    "momentum": (
        "These samples are seconds and °F with the heater off after the coast. "
        "Reply with JSON only: {\"rate\": number} where rate is the cooling rate "
        "in °F per minute, positive when the pot is falling."
    ),
    "full": (
        "These samples are seconds and °F for a whole calibration, with the "
        "local model. Reply with JSON only: {\"linear_rate_f_per_sec\": number, "
        "\"coast_rise_f\": number, \"cooldown_f_per_min\": number}. Rates are "
        "positive. cooldown_f_per_min is how fast the pot falls with the heater off."
    ),
}


def api_key() -> str:
    return os.environ.get("GROK_API_KEY", "").strip()


def inline_analysis() -> bool:
    """Tests run the request on the control thread so the result is immediate."""

    return os.environ.get("BREWSKI_ANALYSIS_INLINE", "").strip() == "1"


def analyze_rate(samples: list, kind: str) -> float | None:
    """One slope or rise from Grok. None when the key is missing or the call fails."""

    parsed = _ask(samples, kind)
    if not isinstance(parsed, dict):
        return None
    return _number(parsed.get("rate"))


def analyze_model(samples: list, local: dict) -> dict | None:
    """A revised model for the next run. None when the call does not answer."""

    parsed = _ask({"samples": samples, "local": local}, "full")
    if not isinstance(parsed, dict):
        return None
    revised = {
        "linear_rate_f_per_sec": _number(parsed.get("linear_rate_f_per_sec")),
        "coast_rise_f": _number(parsed.get("coast_rise_f")),
        "cooldown_f_per_min": _number(parsed.get("cooldown_f_per_min")),
    }
    if all(value is None for value in revised.values()):
        return None
    return revised


def _ask(payload: object, kind: str) -> dict | None:
    key = api_key()
    if not key:
        return None
    prompt = _PROMPTS.get(kind, _PROMPTS["linear"])
    body = json.dumps(
        {
            "model": GROK_MODEL,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps(_compact(payload))},
            ],
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        logger.warning("Grok analysis failed for %s", kind)
        return None
    try:
        document = json.loads(raw)
        content = document["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        logger.warning("Grok analysis for %s did not return a message", kind)
        return None
    return _json_object(content)


def _compact(payload: object) -> object:
    if isinstance(payload, dict):
        compact = dict(payload)
        samples = compact.get("samples")
        if isinstance(samples, list):
            compact["samples"] = samples[:120]
        return compact
    if isinstance(payload, list):
        return payload[:120]
    return payload


def _json_object(content: str) -> dict | None:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number
