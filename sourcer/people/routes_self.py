"""What an employee can see about themselves. Everything here is keyed on the signed-in login's own employee record,
never on an id sent by the browser, so nobody can ask for someone else's data.
"""

from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query

from sourcer.api.deps import Context, audit, get_context
from sourcer.people import leave, policy as company_policy, requests as people_requests, store
from sourcer.people.models import EmployeeOut
from sourcer.people.models_leave import BalancesOut, HolidayOut, LeaveList, LeavePreviewOut, PolicyOut
from sourcer.people.models_requests import LeaveRequestIn, LeaveRequestPreviewIn, NoticeRequestIn, RequestOut, RoutingOut
from sourcer.people.routes import _guard, _iso, _one
from sourcer.people.routes_requests import request_out
from sourcer.people.routes_leave import _holiday_out, _leave_out, _policy_out

router = APIRouter(prefix="/people/me", tags=["people-self"])


def require_self(ctx: Context = Depends(get_context)) -> Tuple[Context, Dict[str, Any]]:
    """The signed-in employee and their own record. Anyone not linked to an employee record is refused."""
    employee_id = ctx.user.get("employee_id")
    if ctx.user.get("role") != "employee" or not employee_id:
        raise HTTPException(403, "This page is for employees")
    try:
        emp = store.get(ctx.db, employee_id)
    except store.NotFound:
        raise HTTPException(403, "Your employee record could not be found") from None
    if emp.get("status") != "active":
        raise HTTPException(403, "Your employment record is no longer active")
    return ctx, emp


@router.get("", response_model=EmployeeOut)
def my_profile(me: Tuple[Context, Dict[str, Any]] = Depends(require_self)) -> EmployeeOut:
    ctx, emp = me
    return _one(ctx, emp)


@router.get("/balances", response_model=BalancesOut)
def my_balances(year: Optional[int] = Query(default=None, ge=1970, le=2200), me: Tuple[Context, Dict[str, Any]] = Depends(require_self)) -> BalancesOut:
    ctx, emp = me
    return BalancesOut(**leave.balances(ctx.db, emp, year))


@router.get("/leave", response_model=LeaveList)
def my_leave(
    status: str = Query(default="", max_length=10), page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=25, ge=1, le=leave.MAX_PAGE_SIZE), me: Tuple[Context, Dict[str, Any]] = Depends(require_self),
) -> LeaveList:
    ctx, emp = me
    try:
        docs, total = leave.list_records(ctx.db, employee_id=emp["_id"], status=status, page=page, page_size=page_size)
    except leave.PeopleError as e:
        raise HTTPException(422, str(e)) from None
    names = store.names_of(ctx.db, [emp["_id"]])
    return LeaveList(items=[_leave_out(d, names) for d in docs], total=total, page=page, page_size=page_size)


@router.get("/holidays", response_model=List[HolidayOut])
def my_holidays(year: Optional[int] = Query(default=None, ge=1970, le=2200), me: Tuple[Context, Dict[str, Any]] = Depends(require_self)) -> List[HolidayOut]:
    ctx, _ = me
    return [_holiday_out(h) for h in company_policy.list_holidays(ctx.db, year)]


@router.get("/policy", response_model=PolicyOut)
def my_company_rules(me: Tuple[Context, Dict[str, Any]] = Depends(require_self)) -> PolicyOut:
    ctx, _ = me
    return _policy_out(company_policy.get_policy(ctx.db))


# ---------- requests: asking for leave, or for a change to the last working day ----------

@router.get("/request-routing", response_model=RoutingOut)
def my_request_routing(me: Tuple[Context, Dict[str, Any]] = Depends(require_self)) -> RoutingOut:
    """Who will see a request from this employee, so the page can say so: their manager (when allowed) and HR."""
    ctx, emp = me
    return RoutingOut(**people_requests.routing(ctx.db, emp, company_policy.get_policy(ctx.db)))


@router.post("/leave-requests/preview", response_model=LeavePreviewOut)
def preview_my_leave_request(body: LeaveRequestPreviewIn, me: Tuple[Context, Dict[str, Any]] = Depends(require_self)) -> LeavePreviewOut:
    ctx, emp = me
    return LeavePreviewOut(**_guard(lambda: leave.preview(ctx.db, {**body.model_dump(), "employee_id": str(emp["_id"])})))


@router.post("/leave-requests", response_model=RequestOut, status_code=201)
def request_leave(body: LeaveRequestIn, me: Tuple[Context, Dict[str, Any]] = Depends(require_self)) -> RequestOut:
    ctx, emp = me
    doc = _guard(lambda: people_requests.create_leave_request(ctx.db, emp, body.model_dump()))
    audit(ctx, "request_created", {"request": str(doc["_id"]), "kind": "leave"})
    return request_out(ctx, doc, {str(emp["_id"]): emp["name"]})


@router.post("/notice-requests", response_model=RequestOut, status_code=201)
def request_notice_change(body: NoticeRequestIn, me: Tuple[Context, Dict[str, Any]] = Depends(require_self)) -> RequestOut:
    ctx, emp = me
    doc = _guard(lambda: people_requests.create_notice_request(ctx.db, emp, body.last_working_day, body.reason))
    audit(ctx, "request_created", {"request": str(doc["_id"]), "kind": "notice"})
    return request_out(ctx, doc, {str(emp["_id"]): emp["name"]})


@router.get("/requests", response_model=List[RequestOut])
def my_requests(status: str = Query(default="", max_length=10), me: Tuple[Context, Dict[str, Any]] = Depends(require_self)) -> List[RequestOut]:
    ctx, emp = me
    docs = _guard(lambda: people_requests.list_for_employee(ctx.db, emp["_id"], status))
    names = {str(emp["_id"]): emp["name"]}
    return [request_out(ctx, d, names) for d in docs]


@router.post("/requests/{request_id}/cancel", response_model=RequestOut)
def cancel_my_request(request_id: str, me: Tuple[Context, Dict[str, Any]] = Depends(require_self)) -> RequestOut:
    ctx, emp = me
    doc = _guard(lambda: people_requests.cancel(ctx.db, request_id, emp))
    audit(ctx, "request_cancelled", {"request": request_id})
    return request_out(ctx, doc, {str(emp["_id"]): emp["name"]})
