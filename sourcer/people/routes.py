from datetime import timezone
from typing import Any, Callable, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from sourcer.api.deps import Context, audit, require_admin
from sourcer import join
from sourcer.people import lifecycle, policy as company_policy, store
from sourcer.tenancy import registry
from sourcer.people.models import EmployeeIn, EmployeeList, EmployeeOption, EmployeeOut, EmployeePatch, LoginLinkOut, LoginOut

# Everything here needs the `people` module (set in app.py) and, for now, an administrator: employees get their own
# self-service routes later. HR data is never readable by a recruiter or by another employee.
router = APIRouter(prefix="/people", tags=["people"])


def _iso(value: Any) -> str:
    """UTC with a Z, to the second. MongoDB hands datetimes back without a zone, so format both the same way."""
    if not value:
        return ""
    return value.replace(tzinfo=timezone.utc).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if value.tzinfo is None else value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _out(doc: Dict[str, Any], names: Dict[str, str], policy: Dict[str, Any], login: bool = False) -> EmployeeOut:
    manager = str(doc["manager_id"]) if doc.get("manager_id") else None
    return EmployeeOut(
        id=str(doc["_id"]), employee_no=doc["employee_no"], name=doc["name"], work_email=doc["work_email"],
        phone=doc.get("phone"), designation=doc.get("designation"), department=doc.get("department"),
        joining_date=doc["joining_date"], manager_id=manager, manager_name=names.get(manager) if manager else None,
        status=doc.get("status", "active"), has_login=bool(doc.get("user_id")),
        login=_login(doc) if login else None,
        probation=lifecycle.probation_info(doc, policy), notice=lifecycle.notice_info(doc),
        created_at=_iso(doc.get("created_at")), updated_at=_iso(doc.get("updated_at")),
    )


def _login(doc: Dict[str, Any]) -> LoginOut:
    info = registry.login_summary(doc.get("user_id"))
    return LoginOut(state=info["state"], last_login=_iso(info["last_login"]) or None)


def _one(ctx: Context, doc: Dict[str, Any]) -> EmployeeOut:
    return _out(doc, store.names_of(ctx.db, [doc.get("manager_id")]), company_policy.get_policy(ctx.db), login=True)


def _guard(call: Callable[[], Any]) -> Any:
    """Turn the store's errors into HTTP answers: a bad value is 422, a missing employee 404."""
    try:
        return call()
    except store.NotFound as e:
        raise HTTPException(404, str(e)) from None
    except store.EmployeeError as e:
        raise HTTPException(422, str(e)) from None


@router.get("/employees", response_model=EmployeeList)
def list_employees(
    q: str = Query(default="", max_length=100),
    status: str = Query(default="", max_length=10),
    sort: str = Query(default="name", max_length=20),
    order: str = Query(default="asc", max_length=4),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=25, ge=1, le=store.MAX_PAGE_SIZE),
    ctx: Context = Depends(require_admin),
) -> EmployeeList:
    docs, total = _guard(lambda: store.list_employees(ctx.db, q, status, sort, order, page, page_size))
    names = store.names_of(ctx.db, [d.get("manager_id") for d in docs])
    policy = company_policy.get_policy(ctx.db)
    return EmployeeList(items=[_out(d, names, policy) for d in docs], total=total, page=page, page_size=page_size)


@router.get("/employees/options", response_model=List[EmployeeOption])
def employee_options(ctx: Context = Depends(require_admin)) -> List[EmployeeOption]:
    return [EmployeeOption(id=str(d["_id"]), name=d["name"], employee_no=d["employee_no"]) for d in store.options(ctx.db)]


@router.post("/employees", response_model=EmployeeOut, status_code=201)
def create_employee(body: EmployeeIn, ctx: Context = Depends(require_admin)) -> EmployeeOut:
    doc = _guard(lambda: store.create(ctx.db, body.model_dump(), str(ctx.user["_id"])))
    audit(ctx, "employee_created", {"employee": str(doc["_id"])})
    return _one(ctx, doc)


@router.get("/employees/{employee_id}", response_model=EmployeeOut)
def get_employee(employee_id: str, ctx: Context = Depends(require_admin)) -> EmployeeOut:
    return _one(ctx, _guard(lambda: store.get(ctx.db, employee_id)))


@router.patch("/employees/{employee_id}", response_model=EmployeeOut)
def update_employee(employee_id: str, body: EmployeePatch, ctx: Context = Depends(require_admin)) -> EmployeeOut:
    # Only the fields the caller actually sent. An explicit null clears an optional field.
    sent = body.model_dump(exclude_unset=True)
    for required in ("name", "work_email", "joining_date", "status"):
        if required in sent and sent[required] is None:
            raise HTTPException(422, f"{required} cannot be empty")
    doc, changed = _guard(lambda: store.update(ctx.db, employee_id, sent, str(ctx.user["_id"])))
    if changed:
        audit(ctx, "employee_updated", {"employee": str(doc["_id"]), "fields": changed})  # field names only, never the values
    return _one(ctx, doc)


# ---------- logins (HR creates a one-time link and hands it over by hand) ----------

@router.post("/employees/{employee_id}/login-link", response_model=LoginLinkOut, status_code=201)
def create_login_link(employee_id: str, ctx: Context = Depends(require_admin)) -> LoginLinkOut:
    """Creates the employee's login if they have none, and a fresh single-use link. Older links and sessions stop working."""
    emp = _guard(lambda: store.get(ctx.db, employee_id))
    if emp.get("status") != "active":
        raise HTTPException(422, "Only active employees can have a login")
    user_id = emp.get("user_id")
    if not user_id or not registry.get_user_by_id(user_id):
        try:
            user = registry.create_employee_user(emp["work_email"], ctx.tenant, str(emp["_id"]))
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        user_id = user["_id"]
        ctx.db["employees"].update_one({"_id": emp["_id"]}, {"$set": {"user_id": user_id}})
    else:
        registry.set_user_disabled(user_id, False)  # a new link turns a disabled login back on
    token, expires = join.create_link(user_id, ctx.tenant, _by_user(ctx))
    audit(ctx, "login_link_created", {"employee": employee_id})
    return LoginLinkOut(token=token, expires_at=_iso(expires), email=emp["work_email"], name=emp["name"])


@router.post("/employees/{employee_id}/login/disable", response_model=EmployeeOut)
def disable_login(employee_id: str, ctx: Context = Depends(require_admin)) -> EmployeeOut:
    emp = _guard(lambda: store.get(ctx.db, employee_id))
    if not emp.get("user_id"):
        raise HTTPException(422, "This employee has no login")
    registry.set_user_disabled(emp["user_id"], True)
    audit(ctx, "login_disabled", {"employee": employee_id})
    return _one(ctx, _guard(lambda: store.get(ctx.db, employee_id)))


def _by_user(ctx: Context) -> str:
    return str(ctx.user["_id"])
