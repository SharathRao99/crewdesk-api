"""Erasing a former employee's personal details (and the retention rule that does it after a set time).

What stays: that the person worked here (an anonymous record), their joining date, and the leave totals, so history
and balances still add up. What goes: name, email, phone, role, free-text reasons and notes, and the login.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from pymongo.database import Database

from sourcer.people import store
from sourcer.people.errors import PeopleError
from sourcer.tenancy import registry

REMOVED = "(removed)"


def anonymise(db: Database, employee_id: Any, by: str) -> Dict[str, Any]:
    emp = store.get(db, employee_id)
    if emp.get("status") == "active":
        raise PeopleError("Make the employee inactive first")
    if emp.get("anonymised_at"):
        raise PeopleError("This employee has already been anonymised")
    now = datetime.now(timezone.utc)
    update: Dict[str, Any] = {
        "name": "Former employee", "name_lc": "former employee", "work_email": f"anonymised-{emp['_id']}@invalid.local",
        "phone": None, "designation": None, "department": None, "user_id": None, "anonymised_at": now, "updated_at": now, "updated_by": by,
    }
    if emp.get("notice"):
        update["notice.note"] = None
        update["notice.adjustments"] = [{**a, "reason": REMOVED} for a in emp["notice"].get("adjustments", [])]
    if emp.get("probation_extensions"):
        update["probation_extensions"] = [{**x, "reason": REMOVED} for x in emp["probation_extensions"]]
    db["employees"].update_one({"_id": emp["_id"]}, {"$set": update})
    if emp.get("user_id"):
        registry.delete_user(emp["user_id"])  # the login, and any link still waiting to be used
    records = db["leave_records"].update_many({"employee_id": emp["_id"]}, {"$set": {"reason": None, "note": None, "cancel_reason": None, "approved_by_name": None}})
    db["leave_records"].update_many({"approved_by_id": emp["_id"]}, {"$set": {"approved_by_name": "Former employee"}})  # where they approved others' leave
    db["requests"].update_many({"employee_id": emp["_id"]}, {"$set": {"reason": None, "decision_note": None}})
    db["requests"].update_many({"decided_by.employee_id": str(emp["_id"])}, {"$set": {"decided_by.name": "Former employee"}})  # where they decided others' requests
    adjustments = db["leave_adjustments"].update_many({"employee_id": emp["_id"]}, {"$set": {"reason": REMOVED}})
    return {"leave_records": records.matched_count, "adjustments": adjustments.matched_count}


def due_for_retention(db: Database, days: int) -> List[Dict[str, Any]]:
    """Inactive employees who left more than `days` days ago and have not been anonymised yet."""
    if days < 30:
        raise PeopleError("Retention must be at least 30 days")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return list(db["employees"].find({"status": "inactive", "anonymised_at": None, "inactive_at": {"$lt": cutoff}}, {"_id": 1}))


def run_retention(db: Database, days: int, by: str = "retention") -> int:
    count = 0
    for emp in due_for_retention(db, days):
        anonymise(db, emp["_id"], by)
        count += 1
    return count
