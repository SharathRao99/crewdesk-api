"""Approving and rejecting employees' requests: for HR (all requests) and for reporting managers (their direct reports')."""

from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query

from sourcer.api.deps import Context, audit, get_context
from sourcer.people import leave, policy as company_policy, requests as people_requests, store
from sourcer.people.models_requests import (
    ApproveIn, RejectIn, RequestBalanceOut, RequestLeaveOut, RequestList, RequestNoticeOut, RequestOut, RequestSummary,
)
from sourcer.people.routes import _guard, _iso

router = APIRouter(prefix="/people/requests", tags=["people-requests"])

Decider = Dict[str, Any]


def require_decider(ctx: Context = Depends(get_context)) -> Tuple[Context, Decider]:
    """HR administrators, or an active reporting manager while the company allows manager approval. Nobody else."""
    if ctx.user.get("role") == "admin":
        return ctx, {"kind": "hr", "user_id": ctx.user["_id"], "employee_id": None, "name": ctx.user["email"]}
    employee_id = ctx.user.get("employee_id")
    if ctx.user.get("role") == "employee" and employee_id and company_policy.get_policy(ctx.db).get("manager_approval", True):
        try:
            emp = store.get(ctx.db, employee_id)
        except store.NotFound:
            emp = None
        if emp and emp.get("status") == "active" and people_requests.is_manager(ctx.db, emp["_id"]):
            return ctx, {"kind": "manager", "user_id": ctx.user["_id"], "employee_id": emp["_id"], "name": emp["name"]}
    raise HTTPException(403, "You cannot decide requests")


def request_out(ctx: Context, doc: Dict[str, Any], names: Dict[str, str], decider: Optional[Decider] = None, policy: Optional[Dict[str, Any]] = None, with_balance: bool = False) -> RequestOut:
    by = doc.get("decided_by") or {}
    lv = doc.get("leave")
    nt = doc.get("notice")
    balance = None
    if with_balance and lv and doc["status"] == "pending":
        try:
            p = leave.preview(ctx.db, {"employee_id": str(doc["employee_id"]), "type_id": str(lv["type_id"]), "start_date": lv["start_date"], "end_date": lv["end_date"], "day_part": lv["day_part"]})
            balance = RequestBalanceOut(years=p["years"], over_balance=p["over_balance"], conflict=p["conflict"], unlimited=p["unlimited"])
        except Exception:  # the employee left or the leave type was retired since: nothing useful to show
            balance = None
    return RequestOut(
        id=str(doc["_id"]), kind=doc["kind"], status=doc["status"], employee_id=str(doc["employee_id"]), employee_name=names.get(str(doc["employee_id"])),
        reason=doc.get("reason"), created_at=_iso(doc.get("created_at")),
        leave=RequestLeaveOut(type_id=str(lv["type_id"]), type_name=lv["type_name"], start_date=lv["start_date"], end_date=lv["end_date"], day_part=lv["day_part"], days=lv["days"]) if lv else None,
        notice=RequestNoticeOut(from_date=nt["from_date"], to_date=nt["to_date"]) if nt else None, balance=balance,
        decided_by_name=by.get("name"), decided_by_kind=by.get("kind"), decided_at=_iso(doc.get("decided_at")) or None, decision_note=doc.get("decision_note"),
        can_decide=bool(decider and doc["status"] == "pending" and people_requests.can_decide(ctx.db, doc, decider, policy or company_policy.get_policy(ctx.db))),
    )


@router.get("/summary", response_model=RequestSummary)
def summary(who: Tuple[Context, Decider] = Depends(require_decider)) -> RequestSummary:
    ctx, decider = who
    return RequestSummary(pending=people_requests.pending_count(ctx.db, decider), can_approve=True)


@router.get("", response_model=RequestList)
def list_requests(
    status: str = Query(default="pending", max_length=10), page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=25, ge=1, le=people_requests.MAX_PAGE_SIZE), who: Tuple[Context, Decider] = Depends(require_decider),
) -> RequestList:
    ctx, decider = who
    docs, total = _guard(lambda: people_requests.list_for_decider(ctx.db, decider, status, page, page_size))
    names = store.names_of(ctx.db, [d["employee_id"] for d in docs])
    policy = company_policy.get_policy(ctx.db)
    return RequestList(items=[request_out(ctx, d, names, decider, policy, with_balance=True) for d in docs], total=total, page=page, page_size=page_size)


@router.get("/{request_id}", response_model=RequestOut)
def get_request(request_id: str, who: Tuple[Context, Decider] = Depends(require_decider)) -> RequestOut:
    ctx, decider = who
    doc = _guard(lambda: people_requests.get(ctx.db, request_id))
    policy = company_policy.get_policy(ctx.db)
    if not people_requests.can_decide(ctx.db, doc, decider, policy):
        raise HTTPException(404, "Request not found")  # not theirs to see
    return request_out(ctx, doc, store.names_of(ctx.db, [doc["employee_id"]]), decider, policy, with_balance=True)


@router.post("/{request_id}/approve", response_model=RequestOut)
def approve(request_id: str, body: ApproveIn, who: Tuple[Context, Decider] = Depends(require_decider)) -> RequestOut:
    ctx, decider = who
    doc = _guard(lambda: people_requests.approve(ctx.db, request_id, decider, body.allow_over_balance, body.note))
    audit(ctx, "request_approved", {"request": request_id, "kind": doc["kind"], "by": decider["kind"]})
    return request_out(ctx, doc, store.names_of(ctx.db, [doc["employee_id"]]), decider)


@router.post("/{request_id}/reject", response_model=RequestOut)
def reject(request_id: str, body: RejectIn, who: Tuple[Context, Decider] = Depends(require_decider)) -> RequestOut:
    ctx, decider = who
    doc = _guard(lambda: people_requests.reject(ctx.db, request_id, decider, body.note))
    audit(ctx, "request_rejected", {"request": request_id, "kind": doc["kind"], "by": decider["kind"]})
    return request_out(ctx, doc, store.names_of(ctx.db, [doc["employee_id"]]), decider)
