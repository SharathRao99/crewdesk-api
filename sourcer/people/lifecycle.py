"""Probation and notice period, from the company's own policy (each person's dates are stored on their record)."""

from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

from pymongo.database import Database

from sourcer.people import policy as pol
from sourcer.people import store
from sourcer.people.dates import add_months, add_period, iso, parse_date
from sourcer.people.errors import PeopleError

MAX_PROBATION_MONTHS = 24
REQUESTERS = ("employee", "company")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _reason(value: Any, label: str = "Reason") -> str:
    text = (value or "").strip() if isinstance(value, str) or value is None else ""
    if len(text) < 3:
        raise PeopleError(f"{label} is required (a few words)")
    if len(text) > 300:
        raise PeopleError(f"{label} must be 300 characters or fewer")
    if any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise PeopleError(f"{label} has characters that are not allowed")
    return text


# ---------- probation ----------

def initial_fields(policy: Dict[str, Any], joining: date) -> Dict[str, Any]:
    """What a new employee's record starts with: probation ends after the company's default months (0 = no probation)."""
    months = policy["probation_months"]
    return {"probation_end_date": iso(add_months(joining, months)), "confirmed_on": iso(joining) if months == 0 else None, "probation_extensions": []}


def probation_info(emp: Dict[str, Any], policy: Dict[str, Any], today: Optional[date] = None) -> Dict[str, Any]:
    """{status, end_date, confirmed_on, extensions}. status: on_probation, review_due (ended but not yet confirmed), confirmed."""
    today = today or date.today()
    joining = date.fromisoformat(emp["joining_date"])
    legacy = "probation_end_date" not in emp  # records from before probation was tracked
    end = emp.get("probation_end_date") or iso(add_months(joining, policy["probation_months"]))
    confirmed = emp.get("confirmed_on")
    if confirmed:
        status = "confirmed"
    elif legacy and date.fromisoformat(end) < today:
        status = "confirmed"  # long-serving people from before this was tracked are taken as confirmed
    elif date.fromisoformat(end) < today:
        status = "review_due"
    else:
        status = "on_probation"
    extensions = [{"from_date": e["from_date"], "to_date": e["to_date"], "reason": e["reason"], "at": e["at"]} for e in emp.get("probation_extensions", [])]
    return {"status": status, "end_date": end, "confirmed_on": confirmed, "extensions": extensions}


def _on_probation_on(emp: Dict[str, Any], policy: Dict[str, Any], day: date) -> bool:
    """Was this person still on probation on `day`? Not confirmed by then counts as on probation, even past the end date."""
    confirmed = emp.get("confirmed_on")
    if confirmed and date.fromisoformat(confirmed) <= day:
        return False
    if "probation_end_date" not in emp and add_months(date.fromisoformat(emp["joining_date"]), policy["probation_months"]) < day:
        return False  # from before probation was tracked: long-serving, taken as confirmed
    return True


def extend_probation(db: Database, employee_id: Any, new_end: Any, reason: Any, by: str) -> Dict[str, Any]:
    emp = store.get(db, employee_id)
    policy = pol.get_policy(db)
    info = probation_info(emp, policy)
    if info["status"] == "confirmed":
        raise PeopleError("This employee is already confirmed")
    end = parse_date(new_end, "New probation end date")
    current = date.fromisoformat(info["end_date"])
    if end <= current:
        raise PeopleError("The new end date must be after the current one")
    if end > add_months(date.fromisoformat(emp["joining_date"]), MAX_PROBATION_MONTHS):
        raise PeopleError(f"Probation cannot run past {MAX_PROBATION_MONTHS} months from joining")
    entry = {"from_date": iso(current), "to_date": iso(end), "reason": _reason(reason), "at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"), "by": by}
    db["employees"].update_one(
        {"_id": emp["_id"]},
        {"$set": {"probation_end_date": iso(end), "updated_at": _now(), "updated_by": by}, "$push": {"probation_extensions": entry}},
    )
    return store.get(db, employee_id)


def confirm(db: Database, employee_id: Any, on_date: Any, by: str) -> Dict[str, Any]:
    emp = store.get(db, employee_id)
    info = probation_info(emp, pol.get_policy(db))
    if info["status"] == "confirmed":
        raise PeopleError("This employee is already confirmed")
    day = parse_date(on_date or iso(date.today()), "Confirmation date")
    if day < date.fromisoformat(emp["joining_date"]):
        raise PeopleError("Confirmation cannot be before the joining date")
    db["employees"].update_one({"_id": emp["_id"]}, {"$set": {"confirmed_on": iso(day), "updated_at": _now(), "updated_by": by}})
    return store.get(db, employee_id)


# ---------- notice period ----------

def notice_info(emp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    n = emp.get("notice")
    if not n:
        return None
    return {"given_on": n["given_on"], "last_working_day": n["last_working_day"], "basis": n["basis"], "note": n.get("note"),
            "adjustments": [{"from_date": a["from_date"], "to_date": a["to_date"], "reason": a["reason"], "requested_by": a["requested_by"], "at": a["at"]} for a in n.get("adjustments", [])]}


def give_notice(db: Database, employee_id: Any, given_on: Any, note: Any, by: str) -> Dict[str, Any]:
    emp = store.get(db, employee_id)
    if emp.get("status") != "active":
        raise PeopleError("Notice can only be recorded for active employees")
    if emp.get("notice"):
        raise PeopleError("Notice has already been recorded. Change its end date or withdraw it instead")
    policy = pol.get_policy(db)
    day = parse_date(given_on, "Date notice was given")
    if day < date.fromisoformat(emp["joining_date"]):
        raise PeopleError("Notice cannot be given before the joining date")
    on_probation = _on_probation_on(emp, policy, day)
    period = policy["probation_notice_period"] if on_probation else policy["notice_period"]
    text = (note or "").strip() if isinstance(note, str) or note is None else ""
    if len(text) > 300:
        raise PeopleError("Note must be 300 characters or fewer")
    notice = {"given_on": iso(day), "last_working_day": iso(add_period(day, period)), "basis": "probation" if on_probation else "notice_period",
              "note": text or None, "adjustments": [], "recorded_at": _now(), "recorded_by": by}
    db["employees"].update_one({"_id": emp["_id"]}, {"$set": {"notice": notice, "updated_at": _now(), "updated_by": by}})
    return store.get(db, employee_id)


def adjust_notice(db: Database, employee_id: Any, last_working_day: Any, reason: Any, requested_by: Any, by: str) -> Dict[str, Any]:
    """Move the last working day (for example, extended because the employee asked). Every change is kept with its reason."""
    emp = store.get(db, employee_id)
    notice = emp.get("notice")
    if not notice:
        raise PeopleError("No notice has been recorded for this employee")
    if requested_by not in REQUESTERS:
        raise PeopleError("Say who asked for the change: the employee or the company")
    new = parse_date(last_working_day, "Last working day")
    if new < date.fromisoformat(notice["given_on"]):
        raise PeopleError("The last working day cannot be before the day notice was given")
    if iso(new) == notice["last_working_day"]:
        raise PeopleError("That is already the last working day")
    entry = {"from_date": notice["last_working_day"], "to_date": iso(new), "reason": _reason(reason), "requested_by": requested_by,
             "at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"), "by": by}
    db["employees"].update_one(
        {"_id": emp["_id"]},
        {"$set": {"notice.last_working_day": iso(new), "updated_at": _now(), "updated_by": by}, "$push": {"notice.adjustments": entry}},
    )
    return store.get(db, employee_id)


def withdraw_notice(db: Database, employee_id: Any, by: str) -> Dict[str, Any]:
    emp = store.get(db, employee_id)
    if not emp.get("notice"):
        raise PeopleError("No notice has been recorded for this employee")
    db["employees"].update_one({"_id": emp["_id"]}, {"$unset": {"notice": ""}, "$set": {"updated_at": _now(), "updated_by": by}})
    return store.get(db, employee_id)
