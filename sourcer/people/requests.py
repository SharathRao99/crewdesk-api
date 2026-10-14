"""Requests from employees: leave, and a change to their last working day. A reporting manager or HR decides.

A request is a question until someone decides it. Approving a leave request records the leave (through the same engine
HR uses, so every rule still applies: working days, clashes, balance); approving a notice request moves the last working
day (and keeps it in the notice history). Who may decide is set per company: HR always, and the reporting manager unless
the company has switched manager approval off.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database

from sourcer.people import leave, lifecycle, policy as company_policy, store
from sourcer.people.dates import iso, parse_date
from sourcer.people.errors import NotFound, PeopleError

KINDS = ("leave", "notice")
STATUSES = ("pending", "approved", "rejected", "cancelled")
MAX_PENDING_PER_EMPLOYEE = 20
BACKDATE_DAYS = 30  # an employee may ask for leave they already took, up to this long ago
MAX_PAGE_SIZE = 100


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_indexes(db: Database) -> None:
    col = db["requests"]
    col.create_index([("employee_id", ASCENDING), ("status", ASCENDING)])
    col.create_index([("status", ASCENDING), ("created_at", DESCENDING)])


def _oid(value: Any) -> ObjectId:
    try:
        return ObjectId(str(value))
    except Exception:
        raise NotFound("Request not found") from None


def _text(value: Any, label: str, limit: int, required: bool = False, minimum: int = 0) -> Optional[str]:
    text = (value or "").strip() if isinstance(value, str) or value is None else ""
    if required and len(text) < max(minimum, 1):
        raise PeopleError(f"{label} is required" + (" (a few words)" if minimum > 1 else ""))
    if len(text) > limit:
        raise PeopleError(f"{label} must be {limit} characters or fewer")
    if any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise PeopleError(f"{label} has characters that are not allowed")
    return text or None


# ---------- who can decide ----------

def reports_of(db: Database, manager_id: ObjectId) -> List[ObjectId]:
    return [d["_id"] for d in db["employees"].find({"manager_id": manager_id, "status": "active"}, {"_id": 1})]


def has_reports(db: Database, employee_id: ObjectId) -> bool:
    return db["employees"].count_documents({"manager_id": employee_id, "status": "active"}, limit=1) > 0


def is_manager(db: Database, emp: Dict[str, Any]) -> bool:
    """Is this person a manager who can approve their team's requests?

    HR sets it on the person (`manager_role`). Until HR has chosen either way it follows the reporting line: someone
    who has people reporting to them is a manager. Saying "off" is deliberate and stays off.
    """
    chosen = emp.get("manager_role")
    return bool(chosen) if chosen is not None else has_reports(db, emp["_id"])


def can_decide(db: Database, request: Dict[str, Any], decider: Dict[str, Any], policy: Dict[str, Any]) -> bool:
    """HR always; the requester's *current* reporting manager unless the company turned manager approval off."""
    if decider["kind"] == "hr":
        return True
    if not policy.get("manager_approval", True):
        return False
    emp = db["employees"].find_one({"_id": request["employee_id"]}, {"manager_id": 1})
    return bool(emp and emp.get("manager_id") and emp["manager_id"] == decider["employee_id"] and request["employee_id"] != decider["employee_id"])


def routing(db: Database, emp: Dict[str, Any], policy: Dict[str, Any]) -> Dict[str, Any]:
    """Who a request from this employee goes to: the manager (when allowed and active) and always HR."""
    manager = None
    if policy.get("manager_approval", True) and emp.get("manager_id"):
        m = db["employees"].find_one({"_id": emp["manager_id"], "status": "active"}, {"name": 1, "manager_role": 1})
        if m and (m.get("manager_role") is None or m["manager_role"]):  # (a person HR has set to "not a manager" cannot approve)
            manager = m["name"]
    return {"manager_name": manager, "hr": True}


# ---------- creating ----------

def _pending_count(db: Database, employee_id: ObjectId) -> int:
    return db["requests"].count_documents({"employee_id": employee_id, "status": "pending"})


def _base(emp: Dict[str, Any], kind: str, reason: Optional[str]) -> Dict[str, Any]:
    return {"kind": kind, "employee_id": emp["_id"], "status": "pending", "reason": reason, "created_at": _now(), "decided_at": None,
            "decided_by": None, "decision_note": None}


def create_leave_request(db: Database, emp: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    ensure_indexes(db)
    if _pending_count(db, emp["_id"]) >= MAX_PENDING_PER_EMPLOYEE:
        raise PeopleError("You have too many requests waiting. Wait for some to be decided first")
    body = {**data, "employee_id": str(emp["_id"])}
    policy, emp, ltype, start, end, counted = leave._resolve(db, body)
    if start < date.today() - timedelta(days=BACKDATE_DAYS):
        raise PeopleError(f"Leave more than {BACKDATE_DAYS} days ago has to be recorded by HR")
    clash = leave._clash(db, emp["_id"], counted["units"], start, end)
    if clash:
        raise PeopleError(clash)
    for other in db["requests"].find({"employee_id": emp["_id"], "kind": "leave", "status": "pending"}, {"leave.units": 1}):
        if any(counted["units"].get(d, 0) + u > 1.0 + leave.EPS for d, u in other["leave"]["units"].items()):
            raise PeopleError("You already have a request waiting for some of these dates")
    doc = _base(emp, "leave", _text(data.get("reason"), "Reason", 300))
    doc["leave"] = {"type_id": ltype["_id"], "type_code": ltype["code"], "type_name": ltype["name"], "start_date": iso(start), "end_date": iso(end),
                    "day_part": body.get("day_part") or "full", "days": counted["days"], "days_by_year": counted["by_year"], "units": counted["units"]}
    db["requests"].insert_one(doc)
    return doc


def create_notice_request(db: Database, emp: Dict[str, Any], last_working_day: Any, reason: Any) -> Dict[str, Any]:
    ensure_indexes(db)
    notice = emp.get("notice")
    if not notice:
        raise PeopleError("You have not given notice, so there is no last working day to change")
    new = parse_date(last_working_day, "New last working day")
    if new < date.fromisoformat(notice["given_on"]):
        raise PeopleError("The last working day cannot be before the day notice was given")
    if iso(new) == notice["last_working_day"]:
        raise PeopleError("That is already your last working day")
    if db["requests"].count_documents({"employee_id": emp["_id"], "kind": "notice", "status": "pending"}):
        raise PeopleError("You already have a request to change your last working day waiting")
    doc = _base(emp, "notice", _text(reason, "Reason", 300, required=True, minimum=3))
    doc["notice"] = {"from_date": notice["last_working_day"], "to_date": iso(new)}
    db["requests"].insert_one(doc)
    return doc


# ---------- reading ----------

def get(db: Database, request_id: Any) -> Dict[str, Any]:
    doc = db["requests"].find_one({"_id": _oid(request_id)})
    if not doc:
        raise NotFound("Request not found")
    return doc


def list_for_employee(db: Database, employee_id: ObjectId, status: str = "", limit: int = 100) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {"employee_id": employee_id}
    if status:
        _check_status(status)
        query["status"] = status
    return list(db["requests"].find(query, {"leave.units": 0}).sort("created_at", -1).limit(limit))


def _check_status(status: str) -> None:
    if status not in STATUSES:
        raise PeopleError(f"Status must be one of {', '.join(STATUSES)}")


def list_for_decider(db: Database, decider: Dict[str, Any], status: str = "pending", page: int = 1, page_size: int = 25) -> Tuple[List[Dict[str, Any]], int]:
    """What this person may decide: everything for HR, their direct reports' requests for a manager. status 'decided' = not pending."""
    query: Dict[str, Any] = {}
    if status == "decided":
        query["status"] = {"$in": ["approved", "rejected", "cancelled"]}
    elif status:
        _check_status(status)
        query["status"] = status
    if decider["kind"] == "manager":
        ids = reports_of(db, decider["employee_id"])
        query["employee_id"] = {"$in": ids}
    page, page_size = max(1, page), max(1, min(MAX_PAGE_SIZE, page_size))
    order = [("created_at", -1 if status in ("decided", "approved", "rejected", "cancelled") else 1), ("_id", 1)]  # oldest waiting first
    cursor = db["requests"].find(query, {"leave.units": 0}).sort(order).skip((page - 1) * page_size).limit(page_size)
    return list(cursor), db["requests"].count_documents(query)


def pending_count(db: Database, decider: Dict[str, Any]) -> int:
    query: Dict[str, Any] = {"status": "pending"}
    if decider["kind"] == "manager":
        query["employee_id"] = {"$in": reports_of(db, decider["employee_id"])}
    return db["requests"].count_documents(query)


# ---------- the employee takes it back ----------

def cancel(db: Database, request_id: Any, emp: Dict[str, Any]) -> Dict[str, Any]:
    req = get(db, request_id)
    if req["employee_id"] != emp["_id"]:
        raise NotFound("Request not found")  # not theirs: it does not exist as far as they can tell
    result = db["requests"].update_one({"_id": req["_id"], "status": "pending"}, {"$set": {"status": "cancelled", "decided_at": _now(), "decided_by": {"kind": "employee", "name": emp["name"]}}})
    if not result.matched_count:
        raise PeopleError("This request has already been decided")
    return get(db, request_id)


def cancel_pending_for(db: Database, employee_id: ObjectId, why: str) -> int:
    """When someone leaves, whatever they had waiting is withdrawn."""
    result = db["requests"].update_many({"employee_id": employee_id, "status": "pending"},
                                        {"$set": {"status": "cancelled", "decided_at": _now(), "decided_by": {"kind": "system", "name": why}}})
    return result.modified_count


# ---------- deciding ----------

def _claim(db: Database, request_id: Any) -> Dict[str, Any]:
    """Take a pending request so only one decider can act on it, even if two click at once."""
    req = db["requests"].find_one_and_update({"_id": _oid(request_id), "status": "pending"}, {"$set": {"status": "deciding"}})
    if not req:
        existing = db["requests"].find_one({"_id": _oid(request_id)}, {"status": 1})
        if not existing:
            raise NotFound("Request not found")
        raise PeopleError("This request has already been decided")
    return req


def _release(db: Database, request_id: ObjectId) -> None:
    db["requests"].update_one({"_id": request_id, "status": "deciding"}, {"$set": {"status": "pending"}})


def _finish(db: Database, req: Dict[str, Any], status: str, decider: Dict[str, Any], note: Optional[str], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    by = {"kind": decider["kind"], "name": decider["name"], "user_id": str(decider["user_id"])}
    if decider["kind"] == "manager":
        by["employee_id"] = str(decider["employee_id"])
    db["requests"].update_one({"_id": req["_id"]}, {"$set": {"status": status, "decided_at": _now(), "decided_by": by, "decision_note": note, **(extra or {})}})
    return get(db, req["_id"])


def approve(db: Database, request_id: Any, decider: Dict[str, Any], allow_over_balance: bool = False, note: Any = None) -> Dict[str, Any]:
    policy = company_policy.get_policy(db)
    existing = get(db, request_id)
    if not can_decide(db, existing, decider, policy):
        raise NotFound("Request not found")  # not theirs to see
    note_text = _text(note, "Note", 300)
    req = _claim(db, request_id)
    try:
        extra: Dict[str, Any] = {}
        if req["kind"] == "leave":
            lv = req["leave"]
            emp = store.get(db, req["employee_id"])
            preview = leave.preview(db, {"employee_id": str(emp["_id"]), "type_id": str(lv["type_id"]), "start_date": lv["start_date"], "end_date": lv["end_date"], "day_part": lv["day_part"]})
            if preview["over_balance"] and decider["kind"] != "hr":
                raise PeopleError("This is more than the employee's balance. Only HR can approve it")
            record = leave.create(db, {
                "employee_id": str(emp["_id"]), "type_id": str(lv["type_id"]), "start_date": lv["start_date"], "end_date": lv["end_date"],
                "day_part": lv["day_part"], "reason": req.get("reason"), "note": None,
                "approved_by_id": str(decider["employee_id"]) if decider["kind"] == "manager" else None,
                "approved_by_label": "HR" if decider["kind"] == "hr" else None,
                "allow_over_balance": bool(allow_over_balance) and decider["kind"] == "hr", "request_id": req["_id"],
            }, str(decider["user_id"]))
            extra["leave_record_id"] = record["_id"]
        else:
            lifecycle.adjust_notice(db, req["employee_id"], req["notice"]["to_date"], req.get("reason"), "employee", str(decider["user_id"]))
        return _finish(db, req, "approved", decider, note_text, extra)
    except Exception:
        _release(db, req["_id"])  # nothing was decided: it stays waiting, and the reason reaches the decider
        raise


def reject(db: Database, request_id: Any, decider: Dict[str, Any], note: Any) -> Dict[str, Any]:
    policy = company_policy.get_policy(db)
    existing = get(db, request_id)
    if not can_decide(db, existing, decider, policy):
        raise NotFound("Request not found")
    note_text = _text(note, "The reason", 300, required=True, minimum=3)  # the employee is owed an explanation
    req = _claim(db, request_id)
    return _finish(db, req, "rejected", decider, note_text)
