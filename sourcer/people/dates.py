"""Date helpers shared by the People modules. Dates travel as plain YYYY-MM-DD text."""

import calendar
import re
from datetime import date, timedelta
from typing import Any, Dict, Iterator

from sourcer.people.errors import PeopleError

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MIN_YEAR = 1970


def parse_date(value: Any, label: str, years_ahead: int = 2) -> date:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise PeopleError(f"{label} must be a date like 2026-04-01")
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise PeopleError(f"{label} is not a real date") from None
    today = date.today()
    if d.year < MIN_YEAR or d > today.replace(year=today.year + years_ahead):
        raise PeopleError(f"{label} is out of range")
    return d


def add_months(d: date, months: int) -> date:
    """d plus whole months; the day is clamped to the end of a shorter month (31 Jan + 1 month = 28 Feb)."""
    index = d.month - 1 + months
    year, month = d.year + index // 12, index % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def add_period(d: date, period: Dict[str, Any]) -> date:
    """d plus {"amount": 2, "unit": "months"} or {"amount": 7, "unit": "days"}."""
    amount = int(period["amount"])
    return add_months(d, amount) if period["unit"] == "months" else d + timedelta(days=amount)


def days_between(start: date, end: date) -> Iterator[date]:
    """Every date from start to end, both included."""
    for offset in range((end - start).days + 1):
        yield start + timedelta(days=offset)


def iso(d: date) -> str:
    return d.isoformat()
