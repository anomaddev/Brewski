"""Sentences and labels for the control log stored on a run.

A calibration and a later still run both write this log. The numbers stay in
the row's detail. The sentence is what the run page shows, and the same rows
export as a spreadsheet.
"""

from __future__ import annotations

import csv
import io
import math

# Names that match the lines drawn on the run chart.
PHASE_LABELS = {
    "settle": "Settle",
    "cold_start": "Cold Start",
    "linear": "Heating",
    "coast": "Coasting",
    "momentum": "Momentum",
    "approach": "Stage II",
    "approach_coast": "Stage II coast",
    "pulses": "Pulses",
    "hold": "Stage III",
    "heating": "Initial Heating",
    "run": "Run",
    "cutoff": "Run",
    "sustain": "Run",
    "model": "Run",
    "cooldown": "Cooldown",
    "pause": "Paused",
    "stop": "Stopped",
}

_DECISION_KEYS = (
    "slope_f_per_sec",
    "sheath_slope_f_per_sec",
    "live_slope_f_per_sec",
    "predicted_crest_f",
    "remaining_coast_f",
    "gain_f",
    "on_seconds",
    "off_seconds",
    "goal_f",
    "live_rise_f",
    "live_rate_f_per_min",
    "learned_coast_f",
    "learned_rate_f_per_min",
    "linear_rate_f_per_sec",
    "shutoff_coast_f",
    "soak",
    "cutoff_f",
    "reheat_f",
    "pulse_seconds",
    "hold_ready",
    "seconds_to_target",
    "seconds_to_cooldown",
)


def clean_detail(detail: dict | None) -> dict:
    """Drop empty values so a stored row is only the numbers that existed."""

    if not isinstance(detail, dict):
        return {}
    cleaned: dict = {}
    for key, value in detail.items():
        packed = _clean(value)
        if packed is not None:
            cleaned[key] = packed
    return cleaned


def model_numbers(model: dict | None) -> dict | None:
    """The heating-model fields worth keeping on a log line."""

    if not isinstance(model, dict):
        return None
    kept = {}
    for key in (
        "cold_start_seconds",
        "cold_start_quad",
        "linear_rate_f_per_sec",
        "coast_rise_f",
        "coast_seconds",
        "coast_quad",
        "cooldown_f_per_min",
        "calibration_count",
    ):
        value = _clean(model.get(key))
        if value is not None:
            kept[key] = value
    return kept or None


def decision_fields(decision: dict) -> dict:
    """Copy the calculation fields that are present on a decision."""

    kept = {}
    for key in _DECISION_KEYS:
        if key not in decision or decision[key] is None:
            continue
        value = _clean(decision[key])
        if value is not None:
            kept[key] = value
    return kept


def start_message(
    kind: str,
    target_f: float,
    model: dict | None,
    learned_coast_f: float | None,
) -> str:
    """What this run began with. The first sample then records the first decision."""

    aim = f"{float(target_f):.0f}"
    numbers = model or {}
    rate = _rate(numbers.get("linear_rate_f_per_sec"))
    coast = _deg(numbers.get("coast_rise_f"))
    seconds = _deg(numbers.get("coast_seconds"), 0)
    if kind == "test":
        return "Initial Heating test started."
    if kind == "calibration":
        bits = []
        if rate:
            bits.append(f"linear {rate}°F/s")
        if coast:
            bits.append(f"coast {coast}°F")
        if bits:
            return f"Calibration started at {aim}°F. Prior {', '.join(bits)}."
        return f"Calibration started at {aim}°F. No prior heating model."
    if rate and coast:
        over = "" if not seconds else f" over {seconds}s"
        return f"Run started at {aim}°F. Model linear {rate}°F/s, coast {coast}°F{over}."
    learned = _deg(learned_coast_f)
    if learned:
        return f"Run started at {aim}°F. Learned coast {learned}°F."
    return f"Run started at {aim}°F."


def suite_result_message(result: dict | None) -> str:
    """One line when an Initial Heating test holds its result."""

    measured = result or {}
    slope = measured.get("slope_f_per_sec")
    minutes = measured.get("slope_f_per_min")
    seconds = measured.get("seconds")
    if measured.get("found") and slope is not None and minutes is not None and seconds is not None:
        return (
            f"Initial Heating slope {float(slope):.5f}°F/s "
            f"({float(minutes):.1f}°F/min) over {float(seconds):.0f}s."
        )
    if slope is not None and seconds is not None:
        why = "stopped early" if measured.get("stopped") else "did not go straight"
        return (
            f"Initial Heating {why}. Slope so far {float(slope):.5f}°F/s "
            f"over {float(seconds):.0f}s."
        )
    if measured.get("stopped"):
        return "Initial Heating stopped before the heaters came on."
    return "Initial Heating ended before the rise went straight."


def model_message(model: dict | None) -> str:
    """One line when a calibration writes the heating model."""

    numbers = model or {}
    parts = []
    rate = _rate(numbers.get("linear_rate_f_per_sec"))
    coast = _deg(numbers.get("coast_rise_f"))
    seconds = _deg(numbers.get("coast_seconds"), 0)
    cool = _deg(numbers.get("cooldown_f_per_min"))
    if rate:
        parts.append(f"linear {rate}°F/s")
    if coast:
        over = "" if not seconds else f" over {seconds}s"
        parts.append(f"coast {coast}°F{over}")
    if cool:
        parts.append(f"cooling {cool}°F/min")
    if not parts:
        return "Model saved."
    return "Model saved. " + ", ".join(parts) + "."


def analysis_message(kind: str, local: float | None, remote: float | None, accepted: bool) -> str:
    """One line for a Grok comparison, including a comparison that was not used."""

    if kind == "full":
        if accepted:
            return "Full-run analysis was accepted into the model."
        return "Full-run analysis was not applied."
    name = kind.replace("_", " ")
    if remote is None:
        if local is None:
            return f"Analysis {name}: Grok was not used."
        return f"Analysis {name}: Grok was not used. Kept local {_measure(local)}."
    decision = "Accepted" if accepted else "Kept the local fit"
    local_text = "—" if local is None else _measure(local)
    return f"Analysis {name}: local {local_text}, Grok {_measure(remote)}. {decision}."


def phase_message(phase: str, detail: dict) -> str:
    """A calibration stage changed. The chart uses the same name."""

    label = PHASE_LABELS.get(phase, phase)
    temp = _deg(detail.get("temp_f"))
    where = "" if temp is None else f" at {temp}°F"
    learned = _learned_phrase(detail.get("model"))
    return f"Entered {label}{where}. {heat_phrase(detail)}.{learned}"


def heat_phrase(detail: dict) -> str:
    """Which heaters are on, including a minimum on or off time that overrode the math."""

    states = detail.get("heaters_on")
    if not isinstance(states, list):
        text = "Heaters off"
    elif states and any(states) and not all(states):
        parts = [
            f"heater {index + 1} {'on' if on else 'off'}" for index, on in enumerate(states)
        ]
        text = ", ".join(parts)
        text = text[0].upper() + text[1:]
    elif any(states):
        text = "Heaters on"
    else:
        text = "Heaters off"
    dwell = detail.get("dwell")
    if dwell == "min_on":
        return f"{text}, held by the minimum on time"
    if dwell == "min_off":
        return f"{text}, held by the minimum off time"
    if dwell == "both":
        return f"{text}, held by the minimum on and off times"
    return text


def format_control(detail: dict) -> str:
    """The calculation sentence for one sample or one heater change."""

    path = str(detail.get("path") or detail.get("phase") or "run")
    if detail.get("temp_f") is None and path not in {"pause", "stop"}:
        title = PHASE_LABELS.get(path, "Run")
        return f"{title}. No control-probe reading. {heat_phrase(detail)}."
    if path == "settle":
        return f"Settle. {_temp(detail)} Heaters off while the probes stabilize."
    if path == "cold_start":
        return _join(
            "Cold Start.",
            _temp(detail),
            heat_phrase(detail) + ".",
            _slope_sentence(detail),
        ) + _eta(detail)
    if path == "linear":
        return _join("Heating.", _temp(detail), heat_phrase(detail) + ".", _slope_sentence(detail)) + _eta(detail)
    if path == "coast":
        rise = _deg(detail.get("live_rise_f"))
        climbed = "" if rise is None else f"Rise since shutoff {rise}°F."
        return _join("Coasting.", _temp(detail), heat_phrase(detail) + ".", climbed, _slope_sentence(detail)) + _eta(detail)
    if path == "momentum":
        rate = _deg(detail.get("live_rate_f_per_min"))
        cooling = "" if rate is None else f"Cooling {rate}°F/min."
        return _join("Momentum.", _temp(detail), heat_phrase(detail) + ".", cooling, _slope_sentence(detail)) + _eta(detail)
    if path == "approach":
        return _approach(detail, coasting=False)
    if path == "approach_coast":
        return _approach(detail, coasting=True)
    if path in {"pulses", "hold", "model"}:
        return _crest_sentence(detail)
    if path == "sustain":
        return _sustain(detail)
    if path == "cutoff":
        return _cutoff(detail)
    if path == "cooldown":
        return _cooldown(detail)
    if path == "pause":
        return f"Paused. {_temp(detail)} Heaters off."
    if path == "stop":
        return f"Stopped. {_temp(detail)} Heaters off."
    return _generic(detail)


def _approach(detail: dict, *, coasting: bool) -> str:
    temp = _deg(detail.get("temp_f"))
    rise = _deg(detail.get("remaining_coast_f"))
    crest = _deg(detail.get("predicted_crest_f"))
    goal = _deg(detail.get("goal_f"), 0)
    if coasting:
        climbed = _deg(detail.get("live_rise_f"))
        climbed_text = "" if climbed is None else f"Rise since shutoff {climbed}°F."
        crest_text = ""
        if crest and goal:
            crest_text = f"Crest {crest}°F has reached the {goal}°F step."
        return _join(
            "Stage II coast.",
            "" if temp is None else f"{temp}°F.",
            crest_text,
            heat_phrase(detail) + ".",
            climbed_text,
        ) + _eta(detail)
    if temp and rise and crest and goal:
        body = (
            f"Stage II. {temp}°F + {rise}°F coast = crest {crest}°F, under the {goal}°F step. "
            f"{heat_phrase(detail)}."
        )
    else:
        body = _generic(detail, title="Stage II")
        return body
    return _join(body, _slope_sentence(detail)) + _eta(detail)


def _crest_sentence(detail: dict) -> str:
    path = str(detail.get("path") or "run")
    title = {"pulses": "Pulses", "hold": "Stage III", "model": "Run"}.get(path, "Run")
    temp = _deg(detail.get("temp_f"))
    crest = _deg(detail.get("predicted_crest_f"))
    target = _deg(detail.get("target_f"), 0)
    parts = [f"{title}."]
    if temp:
        parts.append(f"{temp}°F.")
    if crest and target:
        parts.append(f"Crest {crest}°F versus {target}°F.")
    elif crest:
        parts.append(f"Crest {crest}°F.")
    parts.append(heat_phrase(detail) + ".")
    slope = _slope_sentence(detail)
    if slope:
        parts.append(slope)
    on = detail.get("on_seconds")
    if isinstance(on, (int, float)) and not isinstance(on, bool) and on > 0:
        parts.append(f"On for {float(on):.1f}s.")
    gain = detail.get("gain_f")
    if (
        isinstance(gain, (int, float))
        and not isinstance(gain, bool)
        and gain >= 0.05
        and (isinstance(on, bool) or not isinstance(on, (int, float)) or on <= 0)
    ):
        parts.append(f"A minimum pulse would climb {_deg(gain)}°F before the coast.")
    return " ".join(part for part in parts if part) + _eta(detail)


def _cutoff(detail: dict) -> str:
    return _coast_body(detail) + _eta(detail)


def _sustain(detail: dict) -> str:
    reheat = _deg(detail.get("reheat_f"))
    pulse = _deg(detail.get("pulse_seconds"), 1)
    extra = ""
    if reheat and pulse:
        extra = f" Reheat at {reheat}°F for {pulse}s."
    if detail.get("hold_ready") is False and not _any_on(detail):
        extra += " Waiting for the coast to reach the target before a pulse."
    return _coast_body(detail) + extra + _eta(detail)


def _coast_body(detail: dict) -> str:
    temp = _deg(detail.get("temp_f"))
    coast = _deg(detail.get("shutoff_coast_f"))
    cutoff = _deg(detail.get("cutoff_f"), 0)
    slope = _rate(detail.get("slope_f_per_sec"))
    soak = detail.get("soak")
    extras = []
    if slope:
        extras.append(f"slope {slope}°F/s")
    if isinstance(soak, (int, float)) and not isinstance(soak, bool):
        extras.append(f"soak {float(soak):.2f}")
    coast_text = "Shutoff coast unknown."
    if coast:
        coast_text = f"Shutoff coast {coast}°F"
        if extras:
            coast_text += f" ({', '.join(extras)})"
        coast_text += "."
    cutoff_text = "" if cutoff is None else f"Cutoff {cutoff}°F."
    return _join(
        "Run.",
        "" if temp is None else f"{temp}°F.",
        coast_text,
        cutoff_text,
        heat_phrase(detail) + ".",
    )


def _cooldown(detail: dict) -> str:
    rate = _deg(detail.get("learned_rate_f_per_min"))
    learned = "" if rate is None else f" Learned cooling {rate}°F/min."
    slope = _slope_sentence(detail)
    return _join("Cooldown.", _temp(detail), heat_phrase(detail) + ".", slope) + learned + _eta(detail)


def _generic(detail: dict, title: str | None = None) -> str:
    path = str(detail.get("path") or detail.get("phase") or "run")
    heading = title or PHASE_LABELS.get(path, "Run")
    return _join(f"{heading}.", _temp(detail), heat_phrase(detail) + ".", _slope_sentence(detail), _crest_only(detail)) + _eta(detail)


def _temp(detail: dict) -> str:
    temp = _deg(detail.get("temp_f"))
    if temp is None:
        return ""
    return f"{temp}°F."


def _crest_only(detail: dict) -> str:
    crest = _deg(detail.get("predicted_crest_f"))
    if crest is None:
        return ""
    return f"Crest {crest}°F."


def _slope_sentence(detail: dict) -> str:
    """Measured probe slope, plus the slope the heater decision used when they differ."""

    live = _rate(detail.get("live_slope_f_per_sec"))
    decision = _rate(detail.get("slope_f_per_sec"))
    sheath = _rate(detail.get("sheath_slope_f_per_sec"))
    used = decision or sheath
    if live and used and live != used:
        name = "decision" if decision else "sheath"
        return f"Probe slope {live}°F/s, {name} slope {used}°F/s."
    if live:
        return f"Slope {live}°F/s."
    if used:
        return f"Slope {used}°F/s."
    return ""


def _learned_phrase(model: object) -> str:
    """Numbers already fitted when a calibration stage changes."""

    if not isinstance(model, dict):
        return ""
    bits = []
    if model.get("cold_start_seconds") is not None and model.get("cold_start_quad") is not None:
        bits.append(
            f"cold start {_deg(model.get('cold_start_seconds'), 1)}s, "
            f"quad {_rate(model.get('cold_start_quad'))}°F/s²"
        )
    if model.get("linear_rate_f_per_sec") is not None:
        bits.append(f"linear {_rate(model.get('linear_rate_f_per_sec'))}°F/s")
    if model.get("coast_rise_f") is not None:
        bits.append(f"coast {_deg(model.get('coast_rise_f'))}°F")
    if model.get("cooldown_f_per_min") is not None:
        bits.append(f"cooling {_deg(model.get('cooldown_f_per_min'))}°F/min")
    if not bits:
        return ""
    return " " + ", ".join(bits) + "."


def _eta(detail: dict) -> str:
    path = str(detail.get("path") or "")
    if path in {"pause", "stop"}:
        return ""
    target = detail.get("seconds_to_target")
    cool = detail.get("seconds_to_cooldown")
    if isinstance(target, (int, float)) and not isinstance(target, bool):
        return f" ETA {float(target):.0f}s to target."
    if isinstance(cool, (int, float)) and not isinstance(cool, bool):
        return f" {float(cool):.0f}s until 90°F."
    return ""


def _any_on(detail: dict) -> bool:
    states = detail.get("heaters_on")
    return isinstance(states, list) and any(states)


def _join(*parts: str) -> str:
    return " ".join(part.strip() for part in parts if part and part.strip())


def _deg(value: object, digits: int = 1) -> str | None:
    number = _number(value)
    if number is None:
        return None
    return f"{number:.{digits}f}"


_MODEL_EXPORT_KEYS = (
    "cold_start_seconds",
    "cold_start_quad",
    "linear_rate_f_per_sec",
    "coast_rise_f",
    "coast_seconds",
    "coast_quad",
    "cooldown_f_per_min",
    "calibration_count",
)

_CSV_FIELDS = (
    "elapsed_seconds",
    "recorded_at",
    "event",
    "phase",
    "path",
    "temp_f",
    "target_f",
    "heat",
    "heater_1_on",
    "heater_2_on",
    *_DECISION_KEYS,
    "dwell",
    *(f"model_{key}" for key in _MODEL_EXPORT_KEYS),
    "message",
)


def control_log_csv(logs: list[dict]) -> str:
    """The whole control log as UTF-8 CSV, with a mark Excel understands."""

    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=_CSV_FIELDS,
        extrasaction="ignore",
        lineterminator="\r\n",
    )
    writer.writeheader()
    for entry in logs:
        writer.writerow(_csv_row(entry))
    return "\ufeff" + buffer.getvalue()


def _csv_row(entry: dict) -> dict:
    detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
    row = {
        "elapsed_seconds": entry.get("elapsed_seconds", ""),
        "recorded_at": entry.get("recorded_at", ""),
        "event": entry.get("event", ""),
        "phase": detail.get("phase", ""),
        "path": detail.get("path", ""),
        "message": entry.get("message", ""),
    }
    for key in ("temp_f", "target_f", "heat", "dwell", *_DECISION_KEYS):
        if key in detail:
            row[key] = _csv_value(detail.get(key))
    states = detail.get("heaters_on")
    if isinstance(states, list):
        for index in range(2):
            if index < len(states):
                row[f"heater_{index + 1}_on"] = _csv_value(bool(states[index]))
    model = detail.get("model")
    if isinstance(model, dict):
        for key in _MODEL_EXPORT_KEYS:
            if key in model:
                row[f"model_{key}"] = _csv_value(model.get(key))
    return row


def _csv_value(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None:
        return ""
    return str(value)


def _rate(value: object) -> str | None:
    return _deg(value, 3)


def _measure(value: float) -> str:
    number = float(value)
    digits = 3 if abs(number) < 2 else 2
    return f"{number:.{digits}f}"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _clean(value: object) -> object | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return round(value, 5)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, dict):
        return clean_detail(value)
    return None
