"""A company's own rules: leave year, working week, probation, notice, leave types and holidays.

Every company has its own copy (in its own database), editable by HR. New companies start from the defaults
below, which HR can change; nothing here is shared between companies.
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from sourcer.people.dates import parse_date
from sourcer.people.errors import NotFound, PeopleError

DEFAULT_POLICY: Dict[str, Any] = {
    "year_start_month": 1,                 # the leave year starts on the 1st of this month (1 = calendar year)
    "weekend_days": [5, 6],                # Monday = 0 ... Sunday = 6: Saturday and Sunday are off
    "probation_months": 6,                 # new joiners; HR can extend one person's probation
    "notice_period": {"amount": 2, "unit": "months"},
    "probation_notice_period": {"amount": 7, "unit": "days"},
    "prorate_new_joiners": False,          # false: a joiner gets the full yearly days; true: by months left in the year
    "manager_approval": True,              # true: a reporting manager or HR can approve a request; false: HR only
    "wfh_enabled": True,                   # work from home: requests and records (false: switched off for this company)
    "wfh_monthly_limit": 0,                # most WFH days a person can have in a month without HR's say-so; 0 = no limit
}

# Starting leave types for a new company. Editable, and no others (no casual leave) unless HR adds them.
DEFAULT_TYPES = (
    {"code": "PL", "name": "Planned leave", "days_per_year": 18.0, "paid": True},
    {"code": "SL", "name": "Sick leave", "days_per_year": 12.0, "paid": True},
    {"code": "UL", "name": "Unpaid leave", "days_per_year": None, "paid": False},
)
CODE_RE = re.compile(r"^[A-Z0-9]{2,8}$")
UNITS = ("months", "days")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _half_steps(value: Any, label: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PeopleError(f"{label} must be a number")
    if not (low <= value <= high):
        raise PeopleError(f"{label} must be between {low:g} and {high:g}")
    if (value * 2) % 1:
        raise PeopleError(f"{label} must be in steps of half a day")
    return float(value)


def ensure_indexes(db: Database) -> None:
    db["leave_types"].create_index("code", unique=True)
    db["holidays"].create_index("date", unique=True)


# ---------- company policy ----------

def get_policy(db: Database) -> Dict[str, Any]:
    """The company's rules, with the defaults filling anything HR has not changed. Seeds the starting leave types once."""
    doc = db["company_policy"].find_one({"_id": "policy"}) or {}
    if not doc.get("types_seeded"):
        ensure_indexes(db)
        if db["leave_types"].count_documents({}) == 0:
            for i, t in enumerate(DEFAULT_TYPES):
                db["leave_types"].insert_one({**t, "carry_forward_max": 0.0, "counts_weekends": False, "active": True, "order": i, "created_at": _now()})
        db["company_policy"].update_one({"_id": "policy"}, {"$set": {"types_seeded": True}}, upsert=True)
    merged = {**DEFAULT_POLICY, **{k: v for k, v in doc.items() if k in DEFAULT_POLICY}}
    merged["updated_at"] = doc.get("updated_at")
    return merged


def _period(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"amount", "unit"}:
        raise PeopleError(f"{label} needs an amount and a unit")
    if value["unit"] not in UNITS:
        raise PeopleError(f"{label} unit must be months or days")
    amount = value["amount"]
    if isinstance(amount, bool) or not isinstance(amount, int) or not (0 <= amount <= (24 if value["unit"] == "months" else 365)):
        raise PeopleError(f"{label} amount is out of range")
    return {"amount": amount, "unit": value["unit"]}


def update_policy(db: Database, data: Dict[str, Any], by: str) -> Dict[str, Any]:
    get_policy(db)
    out: Dict[str, Any] = {}
    if "year_start_month" in data:
        m = data["year_start_month"]
        if isinstance(m, bool) or not isinstance(m, int) or not 1 <= m <= 12:
            raise PeopleError("The leave year must start in a month from 1 to 12")
        out["year_start_month"] = m
    if "weekend_days" in data:
        days = data["weekend_days"]
        if not isinstance(days, list) or any(isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6 for d in days) or len(set(days)) != len(days):
            raise PeopleError("Weekend days must be different days of the week")
        if len(days) > 3:
            raise PeopleError("A company needs at least four working days a week")
        out["weekend_days"] = sorted(days)
    if "probation_months" in data:
        p = data["probation_months"]
        if isinstance(p, bool) or not isinstance(p, int) or not 0 <= p <= 24:
            raise PeopleError("Probation must be between 0 and 24 months")
        out["probation_months"] = p
    if "notice_period" in data:
        out["notice_period"] = _period(data["notice_period"], "Notice period")
    if "probation_notice_period" in data:
        out["probation_notice_period"] = _period(data["probation_notice_period"], "Notice period during probation")
    if "prorate_new_joiners" in data:
        if not isinstance(data["prorate_new_joiners"], bool):
            raise PeopleError("Prorating must be on or off")
        out["prorate_new_joiners"] = data["prorate_new_joiners"]
    if "manager_approval" in data:
        if not isinstance(data["manager_approval"], bool):
            raise PeopleError("Manager approval must be on or off")
        out["manager_approval"] = data["manager_approval"]
    if "wfh_enabled" in data:
        if not isinstance(data["wfh_enabled"], bool):
            raise PeopleError("Work from home must be on or off")
        out["wfh_enabled"] = data["wfh_enabled"]
    if "wfh_monthly_limit" in data:
        n = data["wfh_monthly_limit"]
        if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 31:
            raise PeopleError("The monthly work from home limit must be between 0 and 31 days (0 means no limit)")
        out["wfh_monthly_limit"] = n
    if out:
        db["company_policy"].update_one({"_id": "policy"}, {"$set": {**out, "updated_at": _now(), "updated_by": by}}, upsert=True)
    return get_policy(db)


# ---------- leave types ----------

def _oid(value: Any) -> ObjectId:
    try:
        return ObjectId(str(value))
    except Exception:
        raise NotFound("Leave type not found") from None


def _type_fields(data: Dict[str, Any], partial: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not partial or "code" in data:
        code = (data.get("code") or "").strip().upper()
        if not CODE_RE.match(code):
            raise PeopleError("The short code must be 2 to 8 letters or digits, like PL")
        out["code"] = code
    if not partial or "name" in data:
        name = (data.get("name") or "").strip()
        if not name or len(name) > 60:
            raise PeopleError("Give the leave type a name (up to 60 characters)")
        out["name"] = name
    if not partial or "days_per_year" in data:
        d = data.get("days_per_year")
        out["days_per_year"] = None if d is None else _half_steps(d, "Days per year", 0, 366)
    if "carry_forward_max" in data:
        out["carry_forward_max"] = _half_steps(data["carry_forward_max"], "Carry forward", 0, 366)
    for flag in ("paid", "counts_weekends", "active"):
        if flag in data:
            if not isinstance(data[flag], bool):
                raise PeopleError(f"{flag} must be true or false")
            out[flag] = data[flag]
    return out


def list_types(db: Database, include_inactive: bool = False) -> List[Dict[str, Any]]:
    get_policy(db)  # seeds the starting types on first use
    query = {} if include_inactive else {"active": True}
    return list(db["leave_types"].find(query).sort([("order", 1), ("name", 1)]))


def get_type(db: Database, type_id: Any) -> Dict[str, Any]:
    doc = db["leave_types"].find_one({"_id": _oid(type_id)})
    if not doc:
        raise NotFound("Leave type not found")
    return doc


def create_type(db: Database, data: Dict[str, Any], by: str) -> Dict[str, Any]:
    get_policy(db)
    fields = _type_fields(data, partial=False)
    last = db["leave_types"].find_one(sort=[("order", -1)])
    doc = {"paid": True, "carry_forward_max": 0.0, "counts_weekends": False, "active": True, **fields,
           "order": (last["order"] + 1) if last else 0, "created_at": _now(), "created_by": by}
    try:
        db["leave_types"].insert_one(doc)
    except DuplicateKeyError:
        raise PeopleError("A leave type with that short code already exists") from None
    return doc


def update_type(db: Database, type_id: Any, data: Dict[str, Any], by: str) -> Dict[str, Any]:
    current = get_type(db, type_id)
    fields = _type_fields(data, partial=True)
    if "code" in fields and fields["code"] != current["code"]:
        raise PeopleError("The short code cannot be changed once the leave type exists")
    fields.pop("code", None)
    if fields:
        db["leave_types"].update_one({"_id": current["_id"]}, {"$set": {**fields, "updated_at": _now(), "updated_by": by}})
    return get_type(db, type_id)


# ---------- holidays ----------

def _holiday_fields(data: Dict[str, Any], partial: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not partial or "date" in data:
        out["date"] = parse_date(data.get("date"), "Date", years_ahead=5).isoformat()
    if not partial or "name" in data:
        name = (data.get("name") or "").strip()
        if not name or len(name) > 80:
            raise PeopleError("Give the holiday a name (up to 80 characters)")
        out["name"] = name
    return out


def list_holidays(db: Database, year: Optional[int] = None) -> List[Dict[str, Any]]:
    get_policy(db)
    query: Dict[str, Any] = {}
    if year is not None:
        query["date"] = {"$gte": f"{year:04d}-01-01", "$lte": f"{year:04d}-12-31"}
    return list(db["holidays"].find(query).sort("date", 1))


def holiday_dates(db: Database, start: str, end: str) -> Dict[str, str]:
    """{date: name} for the holidays between two ISO dates (both included)."""
    return {h["date"]: h["name"] for h in db["holidays"].find({"date": {"$gte": start, "$lte": end}})}


def create_holiday(db: Database, data: Dict[str, Any], by: str) -> Dict[str, Any]:
    get_policy(db)
    doc = {**_holiday_fields(data, partial=False), "created_at": _now(), "created_by": by}
    try:
        db["holidays"].insert_one(doc)
    except DuplicateKeyError:
        raise PeopleError("There is already a holiday on that date") from None
    return doc


def update_holiday(db: Database, holiday_id: Any, data: Dict[str, Any]) -> Dict[str, Any]:
    fields = _holiday_fields(data, partial=True)
    try:
        doc = db["holidays"].find_one_and_update({"_id": _holiday_oid(holiday_id)}, {"$set": fields}, return_document=True)
    except DuplicateKeyError:
        raise PeopleError("There is already a holiday on that date") from None
    if not doc:
        raise NotFound("Holiday not found")
    return doc


def delete_holiday(db: Database, holiday_id: Any) -> None:
    if not db["holidays"].delete_one({"_id": _holiday_oid(holiday_id)}).deleted_count:
        raise NotFound("Holiday not found")


def _holiday_oid(value: Any) -> ObjectId:
    try:
        return ObjectId(str(value))
    except Exception:
        raise NotFound("Holiday not found") from None
