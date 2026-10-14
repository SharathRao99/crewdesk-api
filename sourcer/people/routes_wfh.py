"""Tracking work from home: HR sees everyone, a reporting manager sees their team. HR can also record it directly."""

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query

from sourcer.api.deps import Context, audit, require_admin
from sourcer.people import policy as company_policy, requests as people_requests, store, wfh
from sourcer.people.models_wfh import WfhCancelIn, WfhList, WfhOut, WfhPreviewIn, WfhPreviewOut, WfhRecordIn, WfhSummary
from sourcer.people.routes import _guard, _iso
from sourcer.people.routes_requests import Decider, require_decider

router = APIRouter(prefix="/people/wfh", tags=["people-wfh"])


def wfh_out(doc: Dict[str, Any], names: Dict[str, str]) -> WfhOut:
    return WfhOut(
        id=str(doc["_id"]), employee_id=str(doc["employee_id"]), employee_name=names.get(str(doc["employee_id"])), start_date=doc["start_date"],
        end_date=doc["end_date"], day_part=doc["day_part"], days=doc["days"], reason=doc.get("reason"), note=doc.get("note"),
        approved_by_name=doc.get("approved_by_name"), over_limit=doc.get("over_limit", False), status=doc["status"], created_at=_iso(doc.get("created_at")),
    )


def _scope(ctx: Context, decider: Decider) -> Optional[List[ObjectId]]:
    """Whose work from home this person may look at: everyone's for HR, their direct reports' for a manager."""
    return None if decider["kind"] == "hr" else people_requests.reports_of(ctx.db, decider["employee_id"])


def _this_month() -> str:
    return date.today().strftime("%Y-%m")


@router.get("/summary", response_model=WfhSummary)
def month_summary(month: str = Query(default="", max_length=7), who: Tuple[Context, Decider] = Depends(require_decider)) -> WfhSummary:
    ctx, decider = who
    return WfhSummary(**_guard(lambda: wfh.month_summary(ctx.db, month or _this_month(), _scope(ctx, decider))))


@router.get("", response_model=WfhList)
def list_wfh(
    employee_id: str = Query(default="", max_length=24), month: str = Query(default="", max_length=7), status: str = Query(default="", max_length=10),
    page: int = Query(default=1, ge=1, le=10_000), page_size: int = Query(default=25, ge=1, le=wfh.MAX_PAGE_SIZE), who: Tuple[Context, Decider] = Depends(require_decider),
) -> WfhList:
    ctx, decider = who
    docs, total = _guard(lambda: wfh.list_records(ctx.db, _scope(ctx, decider), employee_id, month, status, page, page_size))
    names = store.names_of(ctx.db, [d["employee_id"] for d in docs])
    return WfhList(items=[wfh_out(d, names) for d in docs], total=total, page=page, page_size=page_size)


@router.post("/preview", response_model=WfhPreviewOut)
def preview(body: WfhPreviewIn, ctx: Context = Depends(require_admin)) -> WfhPreviewOut:
    return WfhPreviewOut(**_guard(lambda: wfh.preview(ctx.db, body.model_dump())))


@router.post("", response_model=WfhOut, status_code=201)
def record(body: WfhRecordIn, ctx: Context = Depends(require_admin)) -> WfhOut:
    data = body.model_dump()
    data["approved_by_label"] = "HR"
    doc = _guard(lambda: wfh.create(ctx.db, data, str(ctx.user["_id"])))
    audit(ctx, "wfh_recorded", {"wfh": str(doc["_id"]), "employee": str(doc["employee_id"]), "days": doc["days"]})
    return wfh_out(doc, store.names_of(ctx.db, [doc["employee_id"]]))


@router.post("/{record_id}/cancel", response_model=WfhOut)
def cancel(record_id: str, body: WfhCancelIn, ctx: Context = Depends(require_admin)) -> WfhOut:
    doc = _guard(lambda: wfh.cancel(ctx.db, record_id, body.reason, str(ctx.user["_id"])))
    audit(ctx, "wfh_cancelled", {"wfh": record_id, "employee": str(doc["employee_id"])})
    return wfh_out(doc, store.names_of(ctx.db, [doc["employee_id"]]))
