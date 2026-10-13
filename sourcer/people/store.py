"""Employees (the `employees` collection in a company's database).

Kept to what HR needs to run leave: who, how to reach them, when they joined, who they report to.
No government IDs, bank details or birth dates are collected on purpose (less data to protect or leak).
"""

import re
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ASCENDING, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from sourcer.people import policy as company_policy
from sourcer.people.dates import add_months
from sourcer.people.errors import NotFound, PeopleError
from sourcer.tenancy import registry
from sourcer.tenancy.registry import normalize_email

STATUSES = ("active", "inactive")
SORTS = {"name": "name_lc", "joining_date": "joining_date", "employee_no": "employee_no"}
MIN_YEAR = 1970
PHONE_RE = re.compile(r"^[0-9+()\-. ]{5,20}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_PAGE_SIZE = 100


EmployeeError = PeopleError  # the name the rest of this file (and its callers) already use


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_indexes(db: Database) -> None:
    col = db["employees"]
    col.create_index("work_email", unique=True)
    col.create_index("employee_no", unique=True)
    col.create_index([("status", ASCENDING), ("name_lc", ASCENDING)])
    col.create_index("manager_id")
    from sourcer.people import leave  # here, not at the top: leave imports this module

    company_policy.ensure_indexes(db)
    leave.ensure_indexes(db)


def oid(value: Any) -> ObjectId:
    try:
        return ObjectId(str(value))
    except (InvalidId, TypeError):
        raise NotFound("Employee not found") from None


# ---------- validation ----------

def _text(value: Any, label: str, max_len: int, required: bool = False) -> Optional[str]:
    value = (value or "").strip() if isinstance(value, str) or value is None else str(value).strip()
    if not value:
        if required:
            raise EmployeeError(f"{label} is required")
        return None
    if len(value) > max_len:
        raise EmployeeError(f"{label} must be {max_len} characters or fewer")
    if any(ord(c) < 32 for c in value):
        raise EmployeeError(f"{label} has characters that are not allowed")
    return value


def _date(value: Any, label: str) -> str:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise EmployeeError(f"{label} must be a date like 2026-04-01")
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise EmployeeError(f"{label} is not a real date") from None
    if d.year < MIN_YEAR or d > date.today().replace(year=date.today().year + 1):
        raise EmployeeError(f"{label} is out of range")
    return value


def clean(data: Dict[str, Any], partial: bool = False) -> Dict[str, Any]:
    """Validate the fields given. With partial=True only the fields present are checked (for edits)."""
    out: Dict[str, Any] = {}
    present = lambda k: k in data  # noqa: E731
    if not partial or present("name"):
        out["name"] = _text(data.get("name"), "Name", 100, required=True)
    if not partial or present("work_email"):
        try:
            email = normalize_email(data.get("work_email") or "")
        except ValueError:
            raise EmployeeError("Enter a valid work email") from None
        out["work_email"] = email
    if not partial or present("joining_date"):
        out["joining_date"] = _date(data.get("joining_date"), "Joining date")
    for key, label, size in (("designation", "Designation", 80), ("department", "Department", 80)):
        if present(key):
            out[key] = _text(data.get(key), label, size)
    if present("phone"):
        phone = _text(data.get("phone"), "Phone", 20)
        if phone and not PHONE_RE.match(phone):
            raise EmployeeError("Phone can have digits, spaces and + ( ) - . only, 5 to 20 characters")
        out["phone"] = phone
    if present("employee_no"):
        no = _text(data.get("employee_no"), "Employee number", 20)
        if no and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._\-/]*$", no):
            raise EmployeeError("Employee number can use letters, digits and . _ - / only")
        out["employee_no"] = no.upper() if no else None
    if present("status"):
        if data["status"] not in STATUSES:
            raise EmployeeError(f"Status must be one of {', '.join(STATUSES)}")
        out["status"] = data["status"]
    if present("manager_id"):
        out["manager_id"] = data["manager_id"] or None
    return out


def _check_manager(db: Database, employee_id: Optional[ObjectId], manager_id: Optional[str]) -> Optional[ObjectId]:
    if not manager_id:
        return None
    try:
        manager = ObjectId(str(manager_id))
    except InvalidId:
        raise EmployeeError("That manager does not exist") from None
    doc = db["employees"].find_one({"_id": manager}, {"manager_id": 1, "status": 1})
    if not doc:
        raise EmployeeError("That manager does not exist")
    if doc.get("status") != "active":
        raise EmployeeError("A manager must be an active employee")
    if employee_id is not None:
        # Walk up from the chosen manager: reaching this employee again would make a loop (A reports to B reports to A).
        seen, current = set(), doc
        while current:
            cid = current["_id"]
            if cid == employee_id:
                raise EmployeeError("That would make a reporting loop")
            if cid in seen:
                break
            seen.add(cid)
            parent = current.get("manager_id")
            current = db["employees"].find_one({"_id": parent}, {"manager_id": 1}) if parent else None
    return manager


def _next_number(db: Database) -> str:
    """EMP-0001, EMP-0002 ...: a counter per company, never reused."""
    while True:
        counter = db["counters"].find_one_and_update(
            {"_id": "employee_no"}, {"$inc": {"n": 1}}, upsert=True, return_document=ReturnDocument.AFTER
        )
        number = f"EMP-{counter['n']:04d}"
        if not db["employees"].find_one({"employee_no": number}, {"_id": 1}):
            return number  # skip a number HR typed in by hand


# ---------- operations ----------

def create(db: Database, data: Dict[str, Any], by: str) -> Dict[str, Any]:
    ensure_indexes(db)
    fields = clean(data)
    fields["manager_id"] = _check_manager(db, None, data.get("manager_id"))
    from sourcer.people import lifecycle  # here, not at the top: lifecycle imports this module

    probation = lifecycle.initial_fields(company_policy.get_policy(db), date.fromisoformat(fields["joining_date"]))
    now = _now()
    doc = {
        "employee_no": fields.pop("employee_no", None) or _next_number(db),
        "designation": None, "department": None, "phone": None,
        **fields, **probation, "status": "active", "user_id": None, "inactive_at": None, "anonymised_at": None,
        "name_lc": fields["name"].lower(), "created_at": now, "updated_at": now, "created_by": by,
    }
    try:
        db["employees"].insert_one(doc)
    except DuplicateKeyError as e:
        raise EmployeeError(_duplicate_message(e)) from None
    return doc


def _duplicate_message(error: DuplicateKeyError) -> str:
    text = str(error)
    return "An employee with that employee number already exists" if "employee_no" in text else "An employee with that work email already exists"


def get(db: Database, employee_id: Any) -> Dict[str, Any]:
    doc = db["employees"].find_one({"_id": oid(employee_id)})
    if not doc:
        raise NotFound("Employee not found")
    return doc


def update(db: Database, employee_id: Any, data: Dict[str, Any], by: str) -> Tuple[Dict[str, Any], List[str]]:
    """Apply the fields given. Returns the new document and the names of the fields that changed."""
    current = get(db, employee_id)
    fields = clean(data, partial=True)
    if "employee_no" in fields and not fields["employee_no"]:
        raise EmployeeError("Employee number cannot be empty")
    if "manager_id" in fields:
        fields["manager_id"] = _check_manager(db, current["_id"], fields["manager_id"])
    if fields.get("status") == "inactive":
        reports = db["employees"].count_documents({"manager_id": current["_id"], "status": "active"})
        if reports:
            raise EmployeeError(f"{reports} active employee(s) still report to this person. Give them a new manager first")
    changed = [k for k, v in fields.items() if current.get(k) != v]
    user_id = current.get("user_id")
    if "status" in changed:
        # Leaving ends their login at once; coming back does not turn it on again (HR makes a new link).
        fields["inactive_at"] = _now() if fields["status"] == "inactive" else None
    if "name" in fields:
        fields["name_lc"] = fields["name"].lower()
    if "joining_date" in changed and "probation_end_date" in current and not current.get("probation_extensions") and not current.get("confirmed_on"):
        # A corrected joining date moves the default probation end with it, unless HR has already extended or confirmed.
        fields["probation_end_date"] = add_months(date.fromisoformat(fields["joining_date"]), company_policy.get_policy(db)["probation_months"]).isoformat()
    if not changed:
        return current, []
    fields.update(updated_at=_now(), updated_by=by)
    if user_id and "work_email" in changed:
        try:
            registry.change_user_email(user_id, fields["work_email"])  # the login follows the work email
        except ValueError as e:
            raise EmployeeError(str(e)) from None
    try:
        doc = db["employees"].find_one_and_update({"_id": current["_id"]}, {"$set": fields}, return_document=ReturnDocument.AFTER)
    except DuplicateKeyError as e:
        if user_id and "work_email" in changed:
            registry.change_user_email(user_id, current["work_email"])  # undo: the employee record refused it
        raise EmployeeError(_duplicate_message(e)) from None
    if user_id and fields.get("status") == "inactive":
        registry.set_user_disabled(user_id, True)
    return doc, changed


def list_employees(
    db: Database, q: str = "", status: str = "", sort: str = "name", order: str = "asc", page: int = 1, page_size: int = 25,
) -> Tuple[List[Dict[str, Any]], int]:
    query: Dict[str, Any] = {}
    if status:
        if status not in STATUSES:
            raise EmployeeError(f"Status must be one of {', '.join(STATUSES)}")
        query["status"] = status
    q = (q or "").strip()[:100]
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        query["$or"] = [{"name": rx}, {"work_email": rx}, {"employee_no": rx}, {"designation": rx}, {"department": rx}]
    if sort not in SORTS:
        raise EmployeeError(f"Sort must be one of {', '.join(SORTS)}")
    if order not in ("asc", "desc"):
        raise EmployeeError("Order must be asc or desc")
    page = max(1, page)
    page_size = max(1, min(MAX_PAGE_SIZE, page_size))
    direction = 1 if order == "asc" else -1
    cursor = (
        db["employees"].find(query).sort([(SORTS[sort], direction), ("_id", 1)]).skip((page - 1) * page_size).limit(page_size)
    )
    return list(cursor), db["employees"].count_documents(query)


def names_of(db: Database, ids: List[Any]) -> Dict[str, str]:
    """{id: name} for the given ids, in one query (for showing manager names)."""
    wanted = [i for i in {str(x) for x in ids if x}]
    docs = db["employees"].find({"_id": {"$in": [oid(i) for i in wanted]}}, {"name": 1}) if wanted else []
    return {str(d["_id"]): d["name"] for d in docs}


def options(db: Database) -> List[Dict[str, Any]]:
    """Active employees as (id, name, employee_no): the choices for a manager picker."""
    return list(db["employees"].find({"status": "active"}, {"name": 1, "employee_no": 1}).sort("name_lc", 1).limit(1000))
