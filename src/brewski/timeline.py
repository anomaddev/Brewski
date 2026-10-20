"""Display clock for the controller timeline.

Wall time is kept while Brewski is running. A hole longer than 30 minutes
means the process was stopped, and that hole is drawn as a short break so
the chart does not stretch across an empty outage. ``web/src/timeline.js``
uses the same thresholds.
"""

from __future__ import annotations

from datetime import datetime, timezone

# A missing stretch shorter than this is still one continuous line.
GAP_AFTER_MS = 30_000
# Longer than this, the hole is a power-off and collapses on the axis.
CONDENSE_AFTER_MS = 30 * 60 * 1000
# How wide that collapsed hole is. Two minutes stays visible on the default
# 20 minute window without swallowing the readings around it.
CONDENSED_MS = 2 * 60 * 1000
# How often old timeline rows are thinned. Recent readings stay at full rate.
TIMELINE_COMPACT_SECONDS = 3600


def shown_ms(duration_ms: int) -> int:
    """Axis width of one hole. Short holes keep their real length."""

    if duration_ms > CONDENSE_AFTER_MS:
        return CONDENSED_MS
    return duration_ms


def to_display(moment: float, gaps: list[tuple[int, int]]) -> float:
    """Map a real timestamp onto the axis, subtracting collapsed outages.

    ``gaps`` are ``(start, end)`` in milliseconds, sorted, and not overlapping.
    Inside a hole the axis moves only across that hole's shown width.
    """

    shrink = 0.0
    for start, end in gaps:
        if moment <= start:
            break
        duration = end - start
        if duration <= 0:
            continue
        shown = float(shown_ms(duration))
        if moment >= end:
            shrink += duration - shown
            continue
        into = moment - start
        shrink += into - (into / duration) * shown
        break
    return moment - shrink


def to_real(display: float, gaps: list[tuple[int, int]], lo: int, hi: int) -> int:
    """Inverse of :func:`to_display`, clamped to ``lo`` and ``hi``."""

    if hi < lo:
        lo, hi = hi, lo
    if to_display(lo, gaps) >= display:
        return int(lo)
    if to_display(hi, gaps) <= display:
        return int(hi)
    low = float(lo)
    high = float(hi)
    for _ in range(64):
        mid = (low + high) / 2
        if to_display(mid, gaps) < display:
            low = mid
        else:
            high = mid
    return int(round(high))


def window_start(
    end: int,
    span_ms: int,
    gaps: list[tuple[int, int]],
    earliest: int | None,
) -> int:
    """Real timestamp where a window of ``span_ms`` display-time begins."""

    lo = end - span_ms if earliest is None else min(earliest, end)
    return to_real(to_display(end, gaps) - span_ms, gaps, lo, end)


def iso_to_ms(value: str) -> int:
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)
