from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from sourcer.api.deps import Context, audit, require_admin
from sourcer.people import export as people_export, leave, lifecycle, policy as company_policy, store
from sourcer.people.models import EmployeeOut
from sourcer.people.models_leave import (
    AdjustmentIn, AdjustmentOut, BalancesOut, HolidayIn, HolidayOut, HolidayPatch, LeaveCancelIn, LeaveIn, LeaveList, LeaveOut,
    LeavePatch, LeavePreviewIn, LeavePreviewOut, LeaveTypeIn, LeaveTypeOut, LeaveTypePatch, NoticeAdjustIn, NoticeGiveIn,
    PolicyOut, PolicyPatch, ProbationConfirmIn, ProbationExtendIn,
)
from sourcer.people.routes import _guard, _iso, _one

# Company rules, leave and the employee life-cycle (probation, notice). Same access as employees: the `people` module
# (set in app.py) and an administrator. Everything reads and writes the signed-in user's own company only.
router = APIRouter(prefix="/people", tags=["people"])


def _by(ctx: Context) -> str:
    return str(ctx.user["_id"])


# ---------- company policy ----------

def _policy_out(doc: Dict[str, Any]) -> PolicyOut:
    return PolicyOut(**{**doc, "updated_at": _iso(doc.get("updated_at")) or None})


@router.get("/policy", response_model=PolicyOut)
def get_policy(ctx: Context = Depends(require_admin)) -> PolicyOut:
    return _policy_out(company_policy.get_policy(ctx.db))


@router.patch("/policy", response_model=PolicyOut)
def update_policy(body: PolicyPatch, ctx: Context = Depends(require_admin)) -> PolicyOut:
    sent = body.model_dump(exclude_unset=True)
    for key, value in sent.items():
        if value is None:
            raise HTTPException(422, f"{key} cannot be empty")
    doc = _guard(lambda: company_policy.update_policy(ctx.db, sent, _by(ctx)))
    if sent:
        audit(ctx, "policy_updated", {"fields": sorted(sent)})
    return _policy_out(doc)


# ---------- leave types ----------

def _type_out(t: Dict[str, Any]) -> LeaveTypeOut:
    return LeaveTypeOut(
        id=str(t["_id"]), code=t["code"], name=t["name"], days_per_year=t.get("days_per_year"), paid=t.get("paid", True),
        carry_forward_max=float(t.get("carry_forward_max", 0)), counts_weekends=t.get("counts_weekends", False), active=t.get("active", True),
    )


@router.get("/leave-types", response_model=List[LeaveTypeOut])
def list_leave_types(include_inactive: bool = False, ctx: Context = Depends(require_admin)) -> List[LeaveTypeOut]:
    return [_type_out(t) for t in company_policy.list_types(ctx.db, include_inactive)]


@router.post("/leave-types", response_model=LeaveTypeOut, status_code=201)
def create_leave_type(body: LeaveTypeIn, ctx: Context = Depends(require_admin)) -> LeaveTypeOut:
    doc = _guard(lambda: company_policy.create_type(ctx.db, body.model_dump(), _by(ctx)))
    audit(ctx, "leave_type_created", {"type": str(doc["_id"]), "code": doc["code"]})
    return _type_out(doc)


@router.patch("/leave-types/{type_id}", response_model=LeaveTypeOut)
def update_leave_type(type_id: str, body: LeaveTypePatch, ctx: Context = Depends(require_admin)) -> LeaveTypeOut:
    sent = body.model_dump(exclude_unset=True)
    for key in ("code", "name", "paid", "counts_weekends", "active", "carry_forward_max"):
        if key in sent and sent[key] is None:
            raise HTTPException(422, f"{key} cannot be empty")
    doc = _guard(lambda: company_policy.update_type(ctx.db, type_id, sent, _by(ctx)))
    if sent:
        audit(ctx, "leave_type_updated", {"type": type_id, "fields": sorted(sent)})
    return _type_out(doc)


# ---------- holidays ----------

def _holiday_out(h: Dict[str, Any]) -> HolidayOut:
    return HolidayOut(id=str(h["_id"]), date=h["date"], name=h["name"])


@router.get("/holidays", response_model=List[HolidayOut])
def list_holidays(year: Optional[int] = Query(default=None, ge=1970, le=2200), ctx: Context = Depends(require_admin)) -> List[HolidayOut]:
    return [_holiday_out(h) for h in company_policy.list_holidays(ctx.db, year)]


@router.post("/holidays", response_model=HolidayOut, status_code=201)
def create_holiday(body: HolidayIn, ctx: Context = Depends(require_admin)) -> HolidayOut:
    doc = _guard(lambda: company_policy.create_holiday(ctx.db, body.model_dump(), _by(ctx)))
    audit(ctx, "holiday_created", {"date": doc["date"]})
    return _holiday_out(doc)


@router.patch("/holidays/{holiday_id}", response_model=HolidayOut)
def update_holiday(holiday_id: str, body: HolidayPatch, ctx: Context = Depends(require_admin)) -> HolidayOut:
    sent = body.model_dump(exclude_unset=True)
    if any(v is None for v in sent.values()):
        raise HTTPException(422, "date and name cannot be empty")
    doc = _guard(lambda: company_policy.update_holiday(ctx.db, holiday_id, sent))
    audit(ctx, "holiday_updated", {"holiday": holiday_id})
    return _holiday_out(doc)


@router.delete("/holidays/{holiday_id}", status_code=204)
def delete_holiday(holiday_id: str, ctx: Context = Depends(require_admin)) -> Response:
    _guard(lambda: company_policy.delete_holiday(ctx.db, holiday_id))
    audit(ctx, "holiday_deleted", {"holiday": holiday_id})
    return Response(status_code=204)


# ---------- leave records ----------

def _leave_out(r: Dict[str, Any], names: Dict[str, str]) -> LeaveOut:
    return LeaveOut(
        id=str(r["_id"]), employee_id=str(r["employee_id"]), employee_name=names.get(str(r["employee_id"])),
        type_id=str(r["type_id"]), type_code=r["type_code"], type_name=r["type_name"], start_date=r["start_date"], end_date=r["end_date"],
        day_part=r["day_part"], days=r["days"], reason=r.get("reason"), note=r.get("note"), approved_by_id=str(r["approved_by_id"]) if r.get("approved_by_id") else None,
        approved_by_name=r.get("approved_by_name"), over_balance=r.get("over_balance", False), status=r["status"], created_at=_iso(r.get("created_at")),
    )


def _one_leave(ctx: Context, r: Dict[str, Any]) -> LeaveOut:
    return _leave_out(r, store.names_of(ctx.db, [r["employee_id"]]))


@router.post("/leave/preview", response_model=LeavePreviewOut)
def preview_leave(body: LeavePreviewIn, ctx: Context = Depends(require_admin)) -> LeavePreviewOut:
    return LeavePreviewOut(**_guard(lambda: leave.preview(ctx.db, body.model_dump())))


@router.get("/leave", response_model=LeaveList)
def list_leave(
    employee_id: str = Query(default="", max_length=24), type_id: str = Query(default="", max_length=24),
    status: str = Query(default="", max_length=10), date_from: str = Query(default="", max_length=10), date_to: str = Query(default="", max_length=10),
    page: int = Query(default=1, ge=1, le=10_000), page_size: int = Query(default=25, ge=1, le=leave.MAX_PAGE_SIZE),
    ctx: Context = Depends(require_admin),
) -> LeaveList:
    docs, total = _guard(lambda: leave.list_records(ctx.db, employee_id, type_id, status, date_from, date_to, page, page_size))
    names = store.names_of(ctx.db, [d["employee_id"] for d in docs])
    return LeaveList(items=[_leave_out(d, names) for d in docs], total=total, page=page, page_size=page_size)


def _download(fmt: str, base: str, sheet: str, headers: List[str], rows: List[List[Any]]) -> Response:
    data, media = people_export.render(fmt, sheet, headers, rows)
    return Response(content=data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{base}.{fmt}"', "Cache-Control": "no-store"})


@router.get("/leave/export")
def export_leave(
    format: Literal["csv", "xlsx"] = "xlsx",
    employee_id: str = Query(default="", max_length=24), type_id: str = Query(default="", max_length=24),
    status: str = Query(default="", max_length=10), date_from: str = Query(default="", max_length=10), date_to: str = Query(default="", max_length=10),
    ctx: Context = Depends(require_admin),
) -> Response:
    """Leave records as a spreadsheet: everyone's, or one employee's, with the same filters as the list."""
    records = _guard(lambda: leave.export_records(ctx.db, employee_id, type_id, status, date_from, date_to))
    audit(ctx, "leave_exported", {"format": format, "rows": len(records), "employee": employee_id or None})
    return _download(format, "leave-records", "Leave", people_export.LEAVE_HEADERS, people_export.leave_rows(ctx.db, records))


@router.get("/leave/balances-export")
def export_balances(format: Literal["csv", "xlsx"] = "xlsx", year: Optional[int] = Query(default=None, ge=1990, le=2200), ctx: Context = Depends(require_admin)) -> Response:
    """One row per employee: days used and remaining for each leave type, for one leave year."""
    headers, rows = _guard(lambda: people_export.balance_table(ctx.db, year))
    audit(ctx, "balances_exported", {"format": format, "rows": len(rows), "year": year})
    return _download(format, "leave-balances", "Balances", headers, rows)


@router.post("/leave", response_model=LeaveOut, status_code=201)
def record_leave(body: LeaveIn, ctx: Context = Depends(require_admin)) -> LeaveOut:
    doc = _guard(lambda: leave.create(ctx.db, body.model_dump(), _by(ctx)))
    audit(ctx, "leave_recorded", {"leave": str(doc["_id"]), "employee": str(doc["employee_id"]), "type": doc["type_code"], "days": doc["days"]})
    return _one_leave(ctx, doc)


@router.get("/leave/{leave_id}", response_model=LeaveOut)
def get_leave(leave_id: str, ctx: Context = Depends(require_admin)) -> LeaveOut:
    return _one_leave(ctx, _guard(lambda: leave.get(ctx.db, leave_id)))


@router.patch("/leave/{leave_id}", response_model=LeaveOut)
def update_leave(leave_id: str, body: LeavePatch, ctx: Context = Depends(require_admin)) -> LeaveOut:
    doc, changed = _guard(lambda: leave.update(ctx.db, leave_id, body.model_dump(exclude_unset=True), _by(ctx)))
    if changed:
        audit(ctx, "leave_updated", {"leave": leave_id, "fields": changed})
    return _one_leave(ctx, doc)


@router.post("/leave/{leave_id}/cancel", response_model=LeaveOut)
def cancel_leave(leave_id: str, body: LeaveCancelIn, ctx: Context = Depends(require_admin)) -> LeaveOut:
    doc = _guard(lambda: leave.cancel(ctx.db, leave_id, body.reason, _by(ctx)))
    audit(ctx, "leave_cancelled", {"leave": leave_id, "employee": str(doc["employee_id"])})
    return _one_leave(ctx, doc)


# ---------- balances and adjustments ----------

@router.get("/employees/{employee_id}/balances", response_model=BalancesOut)
def employee_balances(employee_id: str, year: Optional[int] = Query(default=None, ge=1970, le=2200), ctx: Context = Depends(require_admin)) -> BalancesOut:
    emp = _guard(lambda: store.get(ctx.db, employee_id))
    return BalancesOut(**leave.balances(ctx.db, emp, year))


@router.get("/employees/{employee_id}/leave-adjustments", response_model=List[AdjustmentOut])
def list_adjustments(employee_id: str, ctx: Context = Depends(require_admin)) -> List[AdjustmentOut]:
    docs = _guard(lambda: leave.list_adjustments(ctx.db, employee_id))
    return [AdjustmentOut(id=str(a["_id"]), type_id=str(a["type_id"]), type_name=a["type_name"], year=a["year"], days=a["days"], reason=a["reason"], created_at=_iso(a["created_at"])) for a in docs]


@router.post("/employees/{employee_id}/leave-adjustments", response_model=AdjustmentOut, status_code=201)
def add_adjustment(employee_id: str, body: AdjustmentIn, ctx: Context = Depends(require_admin)) -> AdjustmentOut:
    a = _guard(lambda: leave.create_adjustment(ctx.db, employee_id, body.model_dump(), _by(ctx)))
    audit(ctx, "leave_adjusted", {"employee": employee_id, "type": str(a["type_id"]), "days": a["days"]})
    return AdjustmentOut(id=str(a["_id"]), type_id=str(a["type_id"]), type_name=a["type_name"], year=a["year"], days=a["days"], reason=a["reason"], created_at=_iso(a["created_at"]))


# ---------- probation and notice ----------

@router.post("/employees/{employee_id}/probation/extend", response_model=EmployeeOut)
def extend_probation(employee_id: str, body: ProbationExtendIn, ctx: Context = Depends(require_admin)) -> EmployeeOut:
    doc = _guard(lambda: lifecycle.extend_probation(ctx.db, employee_id, body.new_end_date, body.reason, _by(ctx)))
    audit(ctx, "probation_extended", {"employee": employee_id})
    return _one(ctx, doc)


@router.post("/employees/{employee_id}/probation/confirm", response_model=EmployeeOut)
def confirm_probation(employee_id: str, body: ProbationConfirmIn, ctx: Context = Depends(require_admin)) -> EmployeeOut:
    doc = _guard(lambda: lifecycle.confirm(ctx.db, employee_id, body.confirmed_on, _by(ctx)))
    audit(ctx, "employee_confirmed", {"employee": employee_id})
    return _one(ctx, doc)


@router.post("/employees/{employee_id}/notice", response_model=EmployeeOut, status_code=201)
def give_notice(employee_id: str, body: NoticeGiveIn, ctx: Context = Depends(require_admin)) -> EmployeeOut:
    doc = _guard(lambda: lifecycle.give_notice(ctx.db, employee_id, body.given_on, body.note, _by(ctx)))
    audit(ctx, "notice_recorded", {"employee": employee_id})
    return _one(ctx, doc)


@router.patch("/employees/{employee_id}/notice", response_model=EmployeeOut)
def adjust_notice(employee_id: str, body: NoticeAdjustIn, ctx: Context = Depends(require_admin)) -> EmployeeOut:
    doc = _guard(lambda: lifecycle.adjust_notice(ctx.db, employee_id, body.last_working_day, body.reason, body.requested_by, _by(ctx)))
    audit(ctx, "notice_adjusted", {"employee": employee_id, "requested_by": body.requested_by})
    return _one(ctx, doc)


@router.delete("/employees/{employee_id}/notice", response_model=EmployeeOut)
def withdraw_notice(employee_id: str, ctx: Context = Depends(require_admin)) -> EmployeeOut:
    doc = _guard(lambda: lifecycle.withdraw_notice(ctx.db, employee_id, _by(ctx)))
    audit(ctx, "notice_withdrawn", {"employee": employee_id})
    return _one(ctx, doc)
