"""What an employee can see about themselves. Everything here is keyed on the signed-in login's own employee record,
never on an id sent by the browser, so nobody can ask for someone else's data.
"""

from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query

from sourcer.api.deps import Context, get_context
from sourcer.people import leave, policy as company_policy, store
from sourcer.people.models import EmployeeOut
from sourcer.people.models_leave import BalancesOut, HolidayOut, LeaveList, PolicyOut
from sourcer.people.routes import _iso, _one
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
