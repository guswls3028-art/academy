from __future__ import annotations

import datetime


def session_window(session) -> tuple[datetime.datetime, datetime.datetime]:
    start = datetime.datetime.combine(session.date, session.start_time)
    end = start + datetime.timedelta(minutes=int(session.duration_minutes))
    return start, end


def is_supported_time_range_values(
    *,
    session_date: datetime.date,
    start_time: datetime.time,
    duration_minutes: int,
) -> bool:
    """Allow same-day ranges and the exact next-day midnight boundary only."""

    start = datetime.datetime.combine(session_date, start_time)
    end = start + datetime.timedelta(minutes=int(duration_minutes))
    return end.date() == session_date or (
        end.date() == session_date + datetime.timedelta(days=1)
        and end.time() == datetime.time.min
    )


def is_supported_time_range_session(session) -> bool:
    return is_supported_time_range_values(
        session_date=session.date,
        start_time=session.start_time,
        duration_minutes=session.duration_minutes,
    )


def booking_window(
    *,
    session,
    start_time: datetime.time,
    end_time: datetime.time,
) -> tuple[datetime.datetime, datetime.datetime]:
    start = datetime.datetime.combine(session.date, start_time)
    end = datetime.datetime.combine(session.date, end_time)
    if end_time == datetime.time.min and start_time > datetime.time.min:
        end += datetime.timedelta(days=1)
    return start, end


def ranges_overlap(
    first_start: datetime.datetime,
    first_end: datetime.datetime,
    second_start: datetime.datetime,
    second_end: datetime.datetime,
) -> bool:
    return first_start < second_end and first_end > second_start
