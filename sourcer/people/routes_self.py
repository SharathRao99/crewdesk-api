"""What an employee can see about themselves. Everything here is keyed on the signed-in login's own employee record,
never on an id sent by the browser, so nobody can ask for someone else's data.
"""

from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from sourcer.api.deps import Context, audit, get_context
from sourcer.people import export as people_export, leave, policy as company_policy, requests as people_requests, store, wfh
from sourcer.people.models import EmployeeOut
from sourcer.people.models_leave import BalancesOut, HolidayOut, LeaveList, LeavePreviewOut, PolicyOut
from sourcer.people.models_requests import LeaveRequestIn, LeaveRequestPreviewIn, NoticeRequestIn, RequestOut, RoutingOut
from sourcer.people.models_wfh import WfhMine, WfhPreviewOut, WfhRequestIn, WfhSelfPreviewIn
from sourcer.people.routes import _guard, _iso, _one
from sourcer.people.routes_requests import request_out
from sourcer.people.routes_wfh import _this_month, wfh_out
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


@router.get("/leave/export")
def export_my_leave(format: Literal["csv", "xlsx"] = "xlsx", me: Tuple[Context, Dict[str, Any]] = Depends(require_self)) -> Response:
    """The employee's own leave records as a spreadsheet. The employee comes from the login, never from the request."""
    ctx, emp = me
    records = leave.export_records(ctx.db, employee_id=emp["_id"])
    audit(ctx, "leave_exported", {"format": format, "rows": len(records), "own": True})
    data, media = people_export.render(format, "Leave", people_export.LEAVE_HEADERS, people_export.leave_rows(ctx.db, records))
    return Response(content=data, media_type=media, headers={"Content-Disposition": f'attachment; filename="my-leave.{format}"', "Cache-Control": "no-store"})


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


# ---------- work from home ----------

@router.get("/wfh", response_model=WfhMine)
def my_wfh(month: str = Query(default="", max_length=7), me: Tuple[Context, Dict[str, Any]] = Depends(require_self)) -> WfhMine:
    """The employee's own month: days used against the company's limit, and each record."""
    ctx, emp = me
    month = month or _this_month()
    policy = company_policy.get_policy(ctx.db)
    docs, _ = _guard(lambda: wfh.list_records(ctx.db, None, emp["_id"], month, "", 1, wfh.MAX_PAGE_SIZE))
    return WfhMine(month=month, label=wfh.month_label(month), used=wfh.month_used(ctx.db, emp["_id"], month), limit=int(policy.get("wfh_monthly_limit", 0)),
                   enabled=bool(policy.get("wfh_enabled", True)), items=[wfh_out(d, {str(emp["_id"]): emp["name"]}) for d in docs])


@router.post("/wfh-requests/preview", response_model=WfhPreviewOut)
def preview_my_wfh_request(body: WfhSelfPreviewIn, me: Tuple[Context, Dict[str, Any]] = Depends(require_self)) -> WfhPreviewOut:
    ctx, emp = me
    return WfhPreviewOut(**_guard(lambda: wfh.preview(ctx.db, {**body.model_dump(), "employee_id": str(emp["_id"])})))


@router.post("/wfh-requests", response_model=RequestOut, status_code=201)
def request_wfh(body: WfhRequestIn, me: Tuple[Context, Dict[str, Any]] = Depends(require_self)) -> RequestOut:
    ctx, emp = me
    doc = _guard(lambda: people_requests.create_wfh_request(ctx.db, emp, body.model_dump()))
    audit(ctx, "request_created", {"request": str(doc["_id"]), "kind": "wfh"})
    return request_out(ctx, doc, {str(emp["_id"]): emp["name"]})
