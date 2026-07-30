"""Minimal 5-field cron parser and next-occurrence solver (stdlib only).

    ┌─ minute (0-59)
    │ ┌─ hour (0-23)
    │ │ ┌─ day of month (1-31)
    │ │ │ ┌─ month (1-12)
    │ │ │ │ ┌─ day of week (0-7, both 0 and 7 = Sunday)
    *  *  *  *  *

Each field accepts ``*``, ``n``, ``a-b``, ``a,b,c`` and a ``/step`` suffix on
any of those — enough for everything the UI presets generate and for what a
model writes from the examples in the tool description::

    */20 * * * *    every 20 minutes
    0 9 * * 1       Mondays at 09:00
    30 7 * * 1-5    weekdays at 07:30

Two deliberate deviations from POSIX cron, both to avoid code for cases nobody
writes here:

* day-of-month and day-of-week are ANDed, not ORed (the OR is a historical
  wart: in POSIX ``0 0 1 * 1`` means "the 1st OR any Monday");
* no names (``mon``, ``jan``) and no ``@daily`` macros — numbers only.

Times are naive local ``datetime``, matching the rest of the codebase
(``storage.sessions.now_iso``). This module holds no state and does no I/O.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

# (low, high) per field, in cron order. dow tops out at 7 so that both 0 and 7
# parse as Sunday; 7 is normalized away in parse().
_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")

# How far next_after() looks before giving up. Just over a year, so a yearly
# expression is found and an impossible one (e.g. "0 0 30 2 *") terminates.
_HORIZON_DAYS = 400


def _field(spec: str, lo: int, hi: int, name: str) -> set[int]:
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or int(raw_step) < 1:
                raise ValueError(f"{name}: invalid step in {spec!r}")
            step = int(raw_step)
        part = part.strip()
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            start, end = _int(a, name, spec), _int(b, name, spec)
        else:
            start = end = _int(part, name, spec)
            if step > 1:
                # "5/15" is the common shorthand for "from 5, every 15".
                end = hi
        if not (lo <= start <= end <= hi):
            raise ValueError(f"{name}: {part!r} out of range {lo}-{hi}")
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError(f"{name}: empty field")
    return values


def _int(raw: str, name: str, spec: str) -> int:
    raw = raw.strip()
    if not raw.isdigit():
        raise ValueError(f"{name}: {spec!r} is not a number")
    return int(raw)


def parse(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """(minutes, hours, days, months, weekdays) as sets. Raises ValueError with
    a message meant to be shown to a human OR to the model, which then retries."""
    parts = (expr or "").split()
    if len(parts) != 5:
        raise ValueError(
            f"a cron expression has 5 fields (minute hour day month weekday), "
            f"got {len(parts)}: {expr!r}")
    fields = [_field(p, lo, hi, name)
              for p, (lo, hi), name in zip(parts, _RANGES, _NAMES)]
    if 7 in fields[4]:                      # both 0 and 7 mean Sunday
        fields[4] = (fields[4] - {7}) | {0}
    return tuple(fields)  # type: ignore[return-value]


def is_valid(expr: str) -> bool:
    try:
        parse(expr)
        return True
    except ValueError:
        return False


def next_after(expr: str, after: datetime | None = None) -> datetime | None:
    """First occurrence strictly after ``after`` (default: now), or None when
    the expression has none within ~13 months (e.g. February 30th).

    Iterates by DAY and only then over the matching hours and minutes, so the
    cost is bounded by the number of skipped days, not by minutes elapsed."""
    minutes, hours, days, months, weekdays = parse(expr)
    after = (after or datetime.now()).replace(second=0, microsecond=0)
    hh_mm = [(h, m) for h in sorted(hours) for m in sorted(minutes)]
    for offset in range(_HORIZON_DAYS):
        day = (after + timedelta(days=offset)).date()
        if day.month not in months or day.day not in days:
            continue
        # date.weekday() is Monday=0; cron counts Sunday=0.
        if (day.weekday() + 1) % 7 not in weekdays:
            continue
        for hour, minute in hh_mm:
            candidate = datetime.combine(day, time(hour, minute))
            if candidate > after:
                return candidate
    return None


def upcoming(expr: str, count: int = 3, after: datetime | None = None) -> list[datetime]:
    """The next ``count`` occurrences. Powers the form's preview, so the user
    can see what an expression actually means before saving it."""
    out: list[datetime] = []
    cursor = after or datetime.now()
    for _ in range(max(0, count)):
        nxt = next_after(expr, cursor)
        if nxt is None:
            break
        out.append(nxt)
        cursor = nxt
    return out
