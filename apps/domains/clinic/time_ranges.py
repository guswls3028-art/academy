from __future__ import annotations

import datetime


def session_window(session) -> tuple[datetime.datetime, datetime.datetime]:
    start = datetime.datetime.combine(session.date, session.start_time)
    end = start + datetime.timedelta(minutes=int(session.duration_minutes))
    return start, end


def is_supported_time_range_session(session) -> bool:
    """Allow same-day ranges and the exact next-day midnight boundary only."""

    _start, end = session_window(session)
    return end.date() == session.date or (
        end.date() == session.date + datetime.timedelta(days=1)
        and end.time() == datetime.time.min
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

