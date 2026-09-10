"""Turning events into free and busy periods.

Pure interval arithmetic over times the application already holds. No network,
no model, no settings and no I/O: given a window and a list of events, it says
which parts of the window are occupied and which are not.

Why this exists rather than a call to Google's free/busy endpoint: Mai already
reads the events, the scope it holds already permits that read, and asking for
a second endpoint would mean a second permission and a second thing to get
wrong. The events are enough.

**Availability is computed here, not by the model.** A model asked "is the user
free?" over a list of events will usually be right and will occasionally be
confidently wrong, and the failure is invisible -- a plausible sentence about a
gap that is not there. So the gaps are computed, and the model is given the
answer to phrase rather than the data to reason over.

Boundary rules, stated once
---------------------------

An event occupies the half-open interval ``[start, end)``. That single choice
settles the cases the brief asks about:

    adjacent       10:00-11:00 and 11:00-12:00 leave no gap between them;
                   they merge into one busy period, because a zero-length
                   gap is not a gap anyone can use.
    overlapping    10:00-11:30 and 11:00-12:00 merge into 10:00-12:00.
    touching the
    window edge    an event ending exactly when the window opens does not
                   occupy it.

Everything else:

    all-day        occupies the whole window. A day marked "out of office" is
                   not availability, and treating it as free is the error that
                   books a meeting into someone's holiday.
    missing end    treated as ending at the window's end, not as a point.
                   Assuming an unbounded event is instantaneous invents
                   availability; assuming it is long only over-reports busy.
    cancelled      never arrives here -- `parse_events` drops it, because a
                   cancelled event is not on the calendar.
    recurring      never arrives here as a rule. Google is asked with
                   `singleEvents=true`, so a weekly stand-up arrives as the
                   individual instances that fall inside the window.
    outside the
    window        clipped to it. Google may return an event that straddles
                   the boundary, and only the part inside is relevant.
"""

from datetime import datetime, timedelta
from typing import List, NamedTuple, Optional, Sequence, Tuple

from app.core.logging import get_logger

logger = get_logger(__name__)

#: The shortest gap worth reporting as free time.
#:
#: A four-minute window between two meetings is not availability; reporting it
#: makes the answer longer and less true. Fifteen minutes is the smallest
#: period most people would call free.
MIN_GAP_MINUTES = 15

#: Most periods either list may carry, so one pathological day cannot produce
#: an unbounded block of prompt text.
MAX_PERIODS = 40


class Period(NamedTuple):
    """A half-open interval, timezone-aware."""

    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return max(0, int((self.end - self.start).total_seconds() // 60))


class Availability(NamedTuple):
    """What the window looks like once the events are laid over it."""

    window: Period
    busy: Tuple[Period, ...]
    free: Tuple[Period, ...]
    #: True when an all-day event covers the window.
    all_day_blocked: bool = False

    @property
    def fully_free(self) -> bool:
        return not self.busy

    @property
    def fully_busy(self) -> bool:
        return not self.free


def compute(
    window_start: datetime,
    window_end: datetime,
    intervals: Sequence[Tuple[Optional[datetime], Optional[datetime], bool]],
    min_gap_minutes: int = MIN_GAP_MINUTES,
) -> Availability:
    """Lay events over a window and return the busy and free periods.

    `intervals` is `(start, end, all_day)` triples -- deliberately not
    `CalendarEvent`, so that titles and locations cannot reach this module
    even by accident. Availability needs times and nothing else.
    """
    window = Period(window_start, window_end)
    if window_end <= window_start:
        return Availability(window=window, busy=(), free=(), all_day_blocked=False)

    all_day_blocked = False
    clipped: List[Period] = []

    for start, end, all_day in intervals:
        if all_day:
            # An all-day event covers the window rather than a slice of it.
            all_day_blocked = True
            clipped.append(window)
            continue
        if start is None:
            # No start means it cannot be placed. `parse_events` already
            # drops these; guarded here so this function is safe alone.
            continue
        if end is None or end <= start:
            # An event with no usable end runs to the end of the window.
            # Over-reporting busy is the safe direction: the alternative
            # invents free time next to an event of unknown length.
            end = window_end

        begin = max(start, window_start)
        finish = min(end, window_end)
        if finish <= begin:
            # Entirely outside, or touching an edge. Half-open intervals mean
            # an event ending exactly at the window start does not occupy it.
            continue
        clipped.append(Period(begin, finish))

    busy = _merge(clipped)
    free = _gaps(window, busy, min_gap_minutes)

    return Availability(
        window=window,
        busy=tuple(busy[:MAX_PERIODS]),
        free=tuple(free[:MAX_PERIODS]),
        all_day_blocked=all_day_blocked,
    )


def _merge(periods: List[Period]) -> List[Period]:
    """Sort and coalesce. Overlapping *and* adjacent periods become one.

    Adjacency merges because `[10:00, 11:00)` and `[11:00, 12:00)` leave a
    gap of exactly zero minutes, and reporting "you are free from 11:00 to
    11:00" is worse than saying nothing.
    """
    if not periods:
        return []

    ordered = sorted(periods, key=lambda period: (period.start, period.end))
    merged = [ordered[0]]
    for period in ordered[1:]:
        last = merged[-1]
        if period.start <= last.end:
            if period.end > last.end:
                merged[-1] = Period(last.start, period.end)
        else:
            merged.append(period)
    return merged


def _gaps(window: Period, busy: List[Period], min_gap_minutes: int) -> List[Period]:
    """Whatever the busy periods leave, bounded below by `min_gap_minutes`."""
    minimum = timedelta(minutes=max(0, min_gap_minutes))
    gaps: List[Period] = []
    cursor = window.start

    for period in busy:
        if period.start - cursor >= minimum:
            gaps.append(Period(cursor, period.start))
        cursor = max(cursor, period.end)

    if window.end - cursor >= minimum:
        gaps.append(Period(cursor, window.end))

    return gaps


__all__ = [
    "MAX_PERIODS",
    "MIN_GAP_MINUTES",
    "Availability",
    "Period",
    "compute",
]
