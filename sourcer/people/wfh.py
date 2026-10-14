"""Work from home: requests (see requests.py) and the records they become, tracked by month.

WFH is not leave: it never touches a leave balance. It is counted in working days (the company's weekly off days and
holidays are skipped, half days allowed), a day cannot be both leave and WFH, and a company may set a monthly limit
that only HR can go past. HR can also record WFH directly.
"""

import calendar
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database

from sourcer.people import leave, policy as company_policy, store
from sourcer.people.dates import iso, parse_date
from sourcer.people.errors import NotFound, PeopleError

STATUSES = ("recorded", "cancelled")
MAX_PAGE_SIZE = 100
EPS = 1e-9


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_indexes(db: Database) -> None:
    db["wfh_records"].create_index([("employee_id", ASCENDING), ("start_date", DESCENDING)])
    db["wfh_records"].create_index([("start_date", DESCENDING)])


def require_enabled(policy: Dict[str, Any]) -> None:
    if not policy.get("wfh_enabled", True):
        raise PeopleError("Work from home is not switched on for your company")


def _text(value: Any, label: str, limit: int) -> Optional[str]:
    return leave._text(value, label, limit)


def month_label(key: str) -> str:
    y, m = int(key[:4]), int(key[5:7])
    return f"{calendar.month_name[m]} {y}"


def parse_month(value: str) -> Tuple[str, str]:
    """('2026-10-01', '2026-10-31') for '2026-10'."""
    try:
        y, m = int(value[:4]), int(value[5:7])
        if len(value) != 7 or value[4] != "-" or not (1970 <= y <= 2200 and 1 <= m <= 12):
            raise ValueError
    except (ValueError, TypeError):
        raise PeopleError("Month must look like 2026-10") from None
    return f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


# ---------- counting ----------

def compute(db: Database, policy: Dict[str, Any], start: date, end: date, part: str) -> Dict[str, Any]:
    """Working days in the range (the company's off days and holidays skipped), as leave counts them, but never calendar days."""
    return leave.compute(db, policy, {"counts_weekends": False}, start, end, part)


def month_used(db: Database, employee_id: ObjectId, month: str) -> float:
    """WFH days already recorded for this person in a month ('2026-10')."""
    first, last = parse_month(month)
    total = 0.0
    for rec in db["wfh_records"].find({"employee_id": employee_id, "status": "recorded", "start_date": {"$lte": last}, "end_date": {"$gte": first}}, {"units": 1}):
        total += sum(u for ds, u in rec["units"].items() if ds[:7] == month)
    return total


def _months(units: Dict[str, float]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for ds, u in units.items():
        out[ds[:7]] = out.get(ds[:7], 0.0) + u
    return out


def _effects(db: Database, employee_id: ObjectId, units: Dict[str, float], limit: int) -> List[Dict[str, Any]]:
    rows = []
    for month, days in sorted(_months(units).items()):
        before = month_used(db, employee_id, month)
        rows.append({"month": month, "label": month_label(month), "days": days, "used_before": before, "limit": limit,
                     "over": bool(limit) and before + days > limit + EPS})
    return rows


def _resolve(db: Database, data: Dict[str, Any], backdate_days: Optional[int] = None) -> Tuple[Dict[str, Any], Dict[str, Any], date, date, Dict[str, Any]]:
    policy = company_policy.get_policy(db)
    require_enabled(policy)
    emp = store.get(db, data.get("employee_id"))
    if emp.get("status") != "active":
        raise PeopleError("Work from home can only be recorded for active employees")
    start = parse_date(data.get("start_date"), "Start date")
    end = parse_date(data.get("end_date") or data.get("start_date"), "End date")
    if start < date.fromisoformat(emp["joining_date"]):
        raise PeopleError("Work from home cannot start before the employee joined")
    counted = compute(db, policy, start, end, data.get("day_part") or "full")
    return policy, emp, start, end, counted


def clash(db: Database, employee_id: ObjectId, units: Dict[str, float], start: date, end: date) -> Optional[str]:
    """A message when these days are already leave or WFH for this person (two halves of one day are fine)."""
    query = {"employee_id": employee_id, "status": "recorded", "start_date": {"$lte": iso(end)}, "end_date": {"$gte": iso(start)}}
    for kind, label in (("wfh_records", "work from home"), ("leave_records", "leave")):
        taken: Dict[str, float] = {}
        for rec in db[kind].find(query, {"units": 1}):
            for ds, u in rec["units"].items():
                taken[ds] = taken.get(ds, 0.0) + u
        for ds in sorted(units):
            if taken.get(ds, 0.0) + units[ds] > 1.0 + EPS:
                return f"{ds} already has {label} recorded for this person"
    # leave and WFH together on one day
    both: Dict[str, float] = {}
    for kind in ("wfh_records", "leave_records"):
        for rec in db[kind].find(query, {"units": 1}):
            for ds, u in rec["units"].items():
                both[ds] = both.get(ds, 0.0) + u
    for ds in sorted(units):
        if both.get(ds, 0.0) + units[ds] > 1.0 + EPS:
            return f"{ds} is already taken by leave or work from home for this person"
    return None


def preview(db: Database, data: Dict[str, Any]) -> Dict[str, Any]:
    policy, emp, start, end, counted = _resolve(db, data)
    effects = _effects(db, emp["_id"], counted["units"], int(policy.get("wfh_monthly_limit", 0)))
    return {"days": counted["days"], "skipped_weekend": counted["skipped_weekend"], "skipped_holidays": counted["skipped_holidays"],
            "months": effects, "over_limit": any(e["over"] for e in effects), "conflict": clash(db, emp["_id"], counted["units"], start, end), "enabled": True}


# ---------- records ----------

def _approver(db: Database, approved_by_id: Any, label: Optional[str]) -> Tuple[Optional[ObjectId], Optional[str]]:
    if approved_by_id:
        a = store.get(db, approved_by_id)
        return a["_id"], a["name"]
    return None, label


def create(db: Database, data: Dict[str, Any], by: str) -> Dict[str, Any]:
    ensure_indexes(db)
    policy, emp, start, end, counted = _resolve(db, data)
    problem = clash(db, emp["_id"], counted["units"], start, end)
    if problem:
        raise PeopleError(problem)
    limit = int(policy.get("wfh_monthly_limit", 0))
    effects = _effects(db, emp["_id"], counted["units"], limit)
    over = any(e["over"] for e in effects)
    if over and not data.get("allow_over_limit"):
        bad = next(e for e in effects if e["over"])
        raise PeopleError(
            f"That would take {emp['name']} to {bad['used_before'] + bad['days']:g} work from home days in {bad['label']}, "
            f"over the limit of {limit}. Tick 'record anyway' to go over it"
        )
    approver_id, approver_name = _approver(db, data.get("approved_by_id"), data.get("approved_by_label"))
    doc = {
        "employee_id": emp["_id"], "start_date": iso(start), "end_date": iso(end), "day_part": data.get("day_part") or "full",
        "days": counted["days"], "units": counted["units"], "reason": _text(data.get("reason"), "Reason", 300), "note": _text(data.get("note"), "Note", 500),
        "approved_by_id": approver_id, "approved_by_name": approver_name, "over_limit": over, "status": "recorded",
        "created_at": _now(), "created_by": by,
    }
    if data.get("request_id"):
        doc["request_id"] = data["request_id"]
    db["wfh_records"].insert_one(doc)
    return doc


def get(db: Database, record_id: Any) -> Dict[str, Any]:
    try:
        oid = ObjectId(str(record_id))
    except Exception:
        raise NotFound("Work from home record not found") from None
    doc = db["wfh_records"].find_one({"_id": oid})
    if not doc:
        raise NotFound("Work from home record not found")
    return doc


def cancel(db: Database, record_id: Any, reason: Any, by: str) -> Dict[str, Any]:
    current = get(db, record_id)
    if current["status"] != "recorded":
        raise PeopleError("This is already cancelled")
    db["wfh_records"].update_one({"_id": current["_id"], "status": "recorded"},
                                 {"$set": {"status": "cancelled", "cancelled_at": _now(), "cancelled_by": by, "cancel_reason": _text(reason, "Reason", 300)}})
    return get(db, record_id)


def list_records(
    db: Database, employee_ids: Optional[List[ObjectId]] = None, employee_id: Any = None, month: str = "", status: str = "",
    page: int = 1, page_size: int = 25,
) -> Tuple[List[Dict[str, Any]], int]:
    query: Dict[str, Any] = {}
    if employee_id:
        eid = store.oid(employee_id)
        if employee_ids is not None and eid not in employee_ids:
            return [], 0  # not someone this person may look at
        query["employee_id"] = eid
    elif employee_ids is not None:
        query["employee_id"] = {"$in": employee_ids}
    if status:
        if status not in STATUSES:
            raise PeopleError(f"Status must be one of {', '.join(STATUSES)}")
        query["status"] = status
    if month:
        first, last = parse_month(month)
        query["start_date"] = {"$lte": last}
        query["end_date"] = {"$gte": first}
    page, page_size = max(1, page), max(1, min(MAX_PAGE_SIZE, page_size))
    cursor = db["wfh_records"].find(query, {"units": 0}).sort([("start_date", -1), ("_id", 1)]).skip((page - 1) * page_size).limit(page_size)
    return list(cursor), db["wfh_records"].count_documents(query)


def month_summary(db: Database, month: str, employee_ids: Optional[List[ObjectId]] = None) -> Dict[str, Any]:
    """Who worked from home in a month: a count per person, and who is out on each day."""
    first, last = parse_month(month)
    query: Dict[str, Any] = {"status": "recorded", "start_date": {"$lte": last}, "end_date": {"$gte": first}}
    if employee_ids is not None:
        query["employee_id"] = {"$in": employee_ids}
    per_person: Dict[ObjectId, float] = {}
    per_day: Dict[str, List[Tuple[ObjectId, str]]] = {}
    for rec in db["wfh_records"].find(query, {"employee_id": 1, "units": 1, "day_part": 1}):
        for ds, u in rec["units"].items():
            if ds[:7] != month:
                continue
            per_person[rec["employee_id"]] = per_person.get(rec["employee_id"], 0.0) + u
            per_day.setdefault(ds, []).append((rec["employee_id"], rec.get("day_part", "full")))
    names = store.names_of(db, list(per_person))
    policy = company_policy.get_policy(db)
    people = sorted(({"employee_id": str(e), "name": names.get(str(e), "Former employee"), "days": d} for e, d in per_person.items()), key=lambda r: (-r["days"], r["name"]))
    days = [{"date": ds, "people": sorted(({"employee_id": str(e), "name": names.get(str(e), "Former employee"), "part": part} for e, part in who), key=lambda r: r["name"])}
            for ds, who in sorted(per_day.items())]
    return {"month": month, "label": month_label(month), "limit": int(policy.get("wfh_monthly_limit", 0)), "total_days": sum(per_person.values()), "people": people, "days": days}
