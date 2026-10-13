"""Leave records and balances.

HR records who took which leave; the system works out the days (skipping the company's weekend and holidays),
refuses clashes, and keeps each person's balance per leave type and leave year. All rules come from the
company's own policy (policy.py), so two companies can differ.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database

from sourcer.people import policy as pol
from sourcer.people import store
from sourcer.people.dates import add_months, days_between, iso, parse_date
from sourcer.people.errors import NotFound, PeopleError

PARTS = ("full", "first_half", "second_half")
STATUSES = ("recorded", "cancelled")
MAX_SPAN_DAYS = 366
MAX_PAGE_SIZE = 100
EPS = 1e-9


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_indexes(db: Database) -> None:
    db["leave_records"].create_index([("employee_id", ASCENDING), ("start_date", DESCENDING)])
    db["leave_records"].create_index([("start_date", DESCENDING)])
    db["leave_adjustments"].create_index("employee_id")


# ---------- leave years ----------

def leave_year(d: date, start_month: int) -> int:
    """The leave year a date falls in, named by the calendar year it starts in (1 = calendar year)."""
    return d.year if d.month >= start_month else d.year - 1


def year_bounds(year: int, start_month: int) -> Tuple[date, date]:
    start = date(year, start_month, 1)
    return start, add_months(start, 12) - timedelta(days=1)


def entitlement(emp: Dict[str, Any], ltype: Dict[str, Any], year: int, policy: Dict[str, Any]) -> Optional[float]:
    """Days this person gets for the type in the leave year; None when the type has no limit (unpaid leave)."""
    base = ltype.get("days_per_year")
    if base is None:
        return None
    sm = policy["year_start_month"]
    start, end = year_bounds(year, sm)
    joined = date.fromisoformat(emp["joining_date"])
    if joined > end:
        return 0.0
    if joined <= start or not policy["prorate_new_joiners"]:
        return float(base)
    months_in = (joined.year - start.year) * 12 + joined.month - start.month  # whole months before the joining month
    return round(base * (12 - months_in) / 12 * 2) / 2  # rounded to the nearest half day


# ---------- counting days ----------

def compute(db: Database, policy: Dict[str, Any], ltype: Dict[str, Any], start: date, end: date, part: str) -> Dict[str, Any]:
    if part not in PARTS:
        raise PeopleError("Day part must be full, first_half or second_half")
    if end < start:
        raise PeopleError("The end date cannot be before the start date")
    if (end - start).days >= MAX_SPAN_DAYS:
        raise PeopleError("A single leave record can cover up to a year")
    if part != "full" and start != end:
        raise PeopleError("A half day is for a single date")
    holidays = pol.holiday_dates(db, iso(start), iso(end))
    weekend = set(policy["weekend_days"])
    weight = 1.0 if part == "full" else 0.5
    units: Dict[str, float] = {}
    skipped_weekend, skipped_holidays = 0, []
    for d in days_between(start, end):
        if not ltype.get("counts_weekends"):
            if d.weekday() in weekend:
                skipped_weekend += 1
                continue
            if iso(d) in holidays:
                skipped_holidays.append({"date": iso(d), "name": holidays[iso(d)]})
                continue
        units[iso(d)] = weight
    if not units:
        raise PeopleError("Those dates have no working days (weekend or holiday), so there is nothing to record")
    by_year: Dict[str, float] = {}
    for ds, u in units.items():
        y = str(leave_year(date.fromisoformat(ds), policy["year_start_month"]))
        by_year[y] = by_year.get(y, 0.0) + u
    return {"units": units, "days": sum(units.values()), "by_year": by_year, "skipped_weekend": skipped_weekend, "skipped_holidays": skipped_holidays}


def _clash(db: Database, employee_id: ObjectId, units: Dict[str, float], start: date, end: date, ignore: Optional[ObjectId] = None) -> Optional[str]:
    """A message when this leave would put a person on more than one day of leave on any date."""
    query: Dict[str, Any] = {"employee_id": employee_id, "status": "recorded", "start_date": {"$lte": iso(end)}, "end_date": {"$gte": iso(start)}}
    if ignore:
        query["_id"] = {"$ne": ignore}
    taken: Dict[str, float] = {}
    for rec in db["leave_records"].find(query, {"units": 1}):
        for ds, u in rec["units"].items():
            taken[ds] = taken.get(ds, 0.0) + u
    for ds in sorted(units):
        if taken.get(ds, 0.0) + units[ds] > 1.0 + EPS:
            return f"{ds} already has leave recorded for this person"
    return None


# ---------- balances ----------

def _used_by_year(db: Database, employee_id: ObjectId) -> Dict[str, Dict[int, float]]:
    used: Dict[str, Dict[int, float]] = {}
    for rec in db["leave_records"].find({"employee_id": employee_id, "status": "recorded"}, {"type_id": 1, "days_by_year": 1}):
        for y, days in rec["days_by_year"].items():
            bucket = used.setdefault(str(rec["type_id"]), {})
            bucket[int(y)] = bucket.get(int(y), 0.0) + days
    return used


def _adjusted_by_year(db: Database, employee_id: ObjectId) -> Dict[str, Dict[int, float]]:
    adj: Dict[str, Dict[int, float]] = {}
    for a in db["leave_adjustments"].find({"employee_id": employee_id}):
        bucket = adj.setdefault(str(a["type_id"]), {})
        bucket[a["year"]] = bucket.get(a["year"], 0.0) + a["days"]
    return adj


def balances(db: Database, emp: Dict[str, Any], year: Optional[int] = None, policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Each leave type's entitlement, carry-forward, adjustments, days used and what remains, for one leave year."""
    policy = policy or pol.get_policy(db)
    sm = policy["year_start_month"]
    today = date.today()
    year = leave_year(today, sm) if year is None else year
    first = leave_year(date.fromisoformat(emp["joining_date"]), sm)
    year = max(year, first)  # nothing exists before they joined
    used = _used_by_year(db, emp["_id"])
    adjusted = _adjusted_by_year(db, emp["_id"])
    rows: List[Dict[str, Any]] = []
    for t in pol.list_types(db, include_inactive=True):
        tid = str(t["_id"])
        has_history = any(tid in m for m in (used, adjusted))
        if not t.get("active", True) and not has_history:
            continue
        carry_in, row = 0.0, None
        for y in range(first, year + 1):
            ent = entitlement(emp, t, y, policy)
            u = used.get(tid, {}).get(y, 0.0)
            a = adjusted.get(tid, {}).get(y, 0.0)
            if ent is None:
                row = {"entitlement": None, "carried_in": 0.0, "adjusted": a, "used": u, "remaining": None}
            else:
                total = ent + carry_in + a
                remaining = total - u
                row = {"entitlement": ent, "carried_in": carry_in, "adjusted": a, "used": u, "remaining": remaining}
                carry_in = min(max(remaining, 0.0), float(t.get("carry_forward_max", 0.0)))
        rows.append({"type_id": tid, "code": t["code"], "name": t["name"], "paid": t.get("paid", True), "active": t.get("active", True), **(row or {})})
    start, end = year_bounds(year, sm)
    return {"year": year, "label": year_label(year, sm), "starts": iso(start), "ends": iso(end), "types": rows}


def year_label(year: int, start_month: int) -> str:
    return str(year) if start_month == 1 else f"{year}-{str(year + 1)[2:]}"


# ---------- records ----------

def _approver(db: Database, approved_by_id: Any) -> Tuple[Optional[ObjectId], Optional[str]]:
    if not approved_by_id:
        return None, None
    approver = store.get(db, approved_by_id)  # NotFound -> "Employee not found"
    return approver["_id"], approver["name"]


def _text(value: Any, label: str, limit: int) -> Optional[str]:
    value = (value or "").strip() if isinstance(value, str) or value is None else str(value).strip()
    if len(value) > limit:
        raise PeopleError(f"{label} must be {limit} characters or fewer")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise PeopleError(f"{label} has characters that are not allowed")
    return value or None


def _resolve(db: Database, data: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], date, date, Dict[str, Any]]:
    """Look up and check everything a leave record depends on, and count its days."""
    policy = pol.get_policy(db)
    emp = store.get(db, data.get("employee_id"))
    if emp.get("status") != "active":
        raise PeopleError("Leave can only be recorded for active employees")
    ltype = pol.get_type(db, data.get("type_id"))
    if not ltype.get("active", True):
        raise PeopleError("That leave type is no longer in use")
    start = parse_date(data.get("start_date"), "Start date")
    end = parse_date(data.get("end_date") or data.get("start_date"), "End date")
    if start < date.fromisoformat(emp["joining_date"]):
        raise PeopleError("Leave cannot start before the employee joined")
    counted = compute(db, policy, ltype, start, end, data.get("day_part") or "full")
    return policy, emp, ltype, start, end, counted


def _remaining_for(db: Database, emp: Dict[str, Any], ltype: Dict[str, Any], year: int, policy: Dict[str, Any]) -> Optional[float]:
    for row in balances(db, emp, year, policy)["types"]:
        if row["type_id"] == str(ltype["_id"]):
            return row["remaining"]
    return None


def preview(db: Database, data: Dict[str, Any]) -> Dict[str, Any]:
    """What recording this leave would do, without saving: days, what was skipped, the balance before and after, any clash."""
    policy, emp, ltype, start, end, counted = _resolve(db, data)
    clash = _clash(db, emp["_id"], counted["units"], start, end)
    years = []
    for y, need in sorted(counted["by_year"].items()):
        remaining = _remaining_for(db, emp, ltype, int(y), policy)
        years.append({"label": year_label(int(y), policy["year_start_month"]), "days": need, "remaining_before": remaining,
                      "remaining_after": None if remaining is None else remaining - need})
    over = any(r["remaining_after"] is not None and r["remaining_after"] < -EPS for r in years)
    return {"days": counted["days"], "skipped_weekend": counted["skipped_weekend"], "skipped_holidays": counted["skipped_holidays"],
            "years": years, "over_balance": over, "conflict": clash, "unlimited": ltype.get("days_per_year") is None}


def create(db: Database, data: Dict[str, Any], by: str) -> Dict[str, Any]:
    ensure_indexes(db)
    policy, emp, ltype, start, end, counted = _resolve(db, data)
    clash = _clash(db, emp["_id"], counted["units"], start, end)
    if clash:
        raise PeopleError(clash)
    over = False
    for y, need in counted["by_year"].items():
        remaining = _remaining_for(db, emp, ltype, int(y), policy)
        if remaining is not None and need > remaining + EPS:
            over = True
            if not data.get("allow_over_balance"):
                raise PeopleError(
                    f"{ltype['name']}: {need:g} day(s) requested but only {max(remaining, 0):g} left in {year_label(int(y), policy['year_start_month'])}. "
                    "Tick 'record anyway' to go over the balance"
                )
    approver_id, approver_name = _approver(db, data.get("approved_by_id"))
    if approver_id is None and data.get("approved_by_label"):
        approver_name = _text(data["approved_by_label"], "Approved by", 60)  # an approver who is not in the employee list, such as "HR"
    doc = {
        "employee_id": emp["_id"], "type_id": ltype["_id"], "type_code": ltype["code"], "type_name": ltype["name"],
        "start_date": iso(start), "end_date": iso(end), "day_part": data.get("day_part") or "full",
        "days": counted["days"], "days_by_year": counted["by_year"], "units": counted["units"],
        "reason": _text(data.get("reason"), "Reason", 300), "note": _text(data.get("note"), "Note", 500),
        "approved_by_id": approver_id, "approved_by_name": approver_name, "over_balance": over,
        "status": "recorded", "created_at": _now(), "created_by": by,
    }
    if data.get("request_id"):
        doc["request_id"] = data["request_id"]
    db["leave_records"].insert_one(doc)
    return doc


def get(db: Database, record_id: Any) -> Dict[str, Any]:
    try:
        oid = ObjectId(str(record_id))
    except Exception:
        raise NotFound("Leave record not found") from None
    doc = db["leave_records"].find_one({"_id": oid})
    if not doc:
        raise NotFound("Leave record not found")
    return doc


def update(db: Database, record_id: Any, data: Dict[str, Any], by: str) -> Tuple[Dict[str, Any], List[str]]:
    """Only the details that do not change the days: reason, note, who approved. For other changes, cancel and record again."""
    current = get(db, record_id)
    fields: Dict[str, Any] = {}
    if "reason" in data:
        fields["reason"] = _text(data["reason"], "Reason", 300)
    if "note" in data:
        fields["note"] = _text(data["note"], "Note", 500)
    if "approved_by_id" in data:
        fields["approved_by_id"], fields["approved_by_name"] = _approver(db, data["approved_by_id"])
    changed = [k for k, v in fields.items() if current.get(k) != v and k != "approved_by_name"]
    if changed:
        db["leave_records"].update_one({"_id": current["_id"]}, {"$set": {**fields, "updated_at": _now(), "updated_by": by}})
    return get(db, record_id), changed


def cancel(db: Database, record_id: Any, reason: Any, by: str) -> Dict[str, Any]:
    current = get(db, record_id)
    if current["status"] != "recorded":
        raise PeopleError("This leave is already cancelled")
    db["leave_records"].update_one(
        {"_id": current["_id"], "status": "recorded"},
        {"$set": {"status": "cancelled", "cancelled_at": _now(), "cancelled_by": by, "cancel_reason": _text(reason, "Reason", 300)}},
    )
    return get(db, record_id)


def list_records(
    db: Database, employee_id: Any = None, type_id: Any = None, status: str = "", date_from: str = "", date_to: str = "",
    page: int = 1, page_size: int = 25,
) -> Tuple[List[Dict[str, Any]], int]:
    query: Dict[str, Any] = {}
    if employee_id:
        query["employee_id"] = store.oid(employee_id)
    if type_id:
        query["type_id"] = pol._oid(type_id)
    if status:
        if status not in STATUSES:
            raise PeopleError(f"Status must be one of {', '.join(STATUSES)}")
        query["status"] = status
    if date_from:  # records that overlap the window
        query["end_date"] = {"$gte": iso(parse_date(date_from, "From date", 5))}
    if date_to:
        query["start_date"] = {"$lte": iso(parse_date(date_to, "To date", 5))}
    page, page_size = max(1, page), max(1, min(MAX_PAGE_SIZE, page_size))
    cursor = db["leave_records"].find(query, {"units": 0}).sort([("start_date", -1), ("_id", 1)]).skip((page - 1) * page_size).limit(page_size)
    return list(cursor), db["leave_records"].count_documents(query)


# ---------- adjustments (HR corrections to a balance) ----------

def create_adjustment(db: Database, employee_id: Any, data: Dict[str, Any], by: str) -> Dict[str, Any]:
    ensure_indexes(db)
    emp = store.get(db, employee_id)
    ltype = pol.get_type(db, data.get("type_id"))
    policy = pol.get_policy(db)
    days = data.get("days")
    if isinstance(days, bool) or not isinstance(days, (int, float)) or days == 0 or abs(days) > 100 or (days * 2) % 1:
        raise PeopleError("Days must be a number other than zero, in steps of half a day, up to 100")
    year = data.get("year")
    first = leave_year(date.fromisoformat(emp["joining_date"]), policy["year_start_month"])
    if isinstance(year, bool) or not isinstance(year, int) or not first <= year <= leave_year(date.today(), policy["year_start_month"]) + 1:
        raise PeopleError("Choose a leave year from when they joined up to next year")
    if ltype.get("days_per_year") is None:
        raise PeopleError("This leave type has no limit, so there is no balance to adjust")
    reason = _text(data.get("reason"), "Reason", 200)
    if not reason:
        raise PeopleError("Say why the balance is being changed")
    doc = {"employee_id": emp["_id"], "type_id": ltype["_id"], "type_name": ltype["name"], "year": year, "days": float(days),
           "reason": reason, "created_at": _now(), "created_by": by}
    db["leave_adjustments"].insert_one(doc)
    return doc


def list_adjustments(db: Database, employee_id: Any) -> List[Dict[str, Any]]:
    return list(db["leave_adjustments"].find({"employee_id": store.oid(employee_id)}).sort("created_at", -1).limit(200))
