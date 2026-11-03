from __future__ import annotations

from datetime import datetime

from croniter import croniter


SCHEDULE_ALIASES: dict[str, str] = {
    "minute": "* * * * *",
    "minutely": "* * * * *",
    "hour": "0 * * * *",
    "hourly": "0 * * * *",
    "day": "0 0 * * *",
    "daily": "0 0 * * *",
    "week": "0 0 * * 0",
    "weekly": "0 0 * * 0",
    "month": "0 0 1 * *",
    "monthly": "0 0 1 * *",
    "year": "0 0 1 1 *",
    "yearly": "0 0 1 1 *",
}


def normalize_schedule(schedule: str) -> str:
    normalized = (schedule or "").strip()
    if not normalized:
        raise ValueError("Schedule cannot be empty.")

    return SCHEDULE_ALIASES.get(normalized.lower(), normalized)


def validate_schedule(schedule: str) -> str:
    normalized = normalize_schedule(schedule)
    if not croniter.is_valid(normalized):
        raise ValueError(
            "Invalid schedule. Use an alias like daily/weekly/monthly or a cron expression such as '0 */6 * * *'.",
        )
    return normalized


def next_run_for_schedule(schedule: str, after: datetime) -> datetime:
    normalized = validate_schedule(schedule)
    return croniter(normalized, after).get_next(datetime)


def current_slot_for_schedule(schedule: str, now: datetime) -> datetime:
    """Return the concrete cron slot that is due at ``now``.

    The slot is the most recent cron fire at or before ``now``. Computing it
    from the first fire strictly after ``now`` makes the boundary exact: at a
    fire instant the current slot is that very fire, not the previous one.
    Callers own deciding whether that slot is actually due (e.g. it must be
    newer than the schedule's last-confirmed anchor).
    """
    normalized = validate_schedule(schedule)
    next_fire = croniter(normalized, now).get_next(datetime)
    return croniter(normalized, next_fire).get_prev(datetime)
