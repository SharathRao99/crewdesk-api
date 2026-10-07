from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from sourcer import screening, templates_store
from sourcer.api import queries
from sourcer.api.deps import Context, audit, get_context, require_admin
from sourcer.api.models import (BudgetOut, CandidateDetail, CandidateList, CandidatePatch, DeletedOut, DraftIn, DraftOut,
                                EventOut, Facets, ScreeningAnswers, ScreeningOut, StatusCount)
from sourcer.outreach import best_email, candidate_values, mail_links, render, uses_form_link
from sourcer.store import STATUSES

router = APIRouter(prefix="/candidates", tags=["candidates"])


def _row(ctx: Context, role: str, login: str) -> Dict[str, Any]:
    row = ctx.db["candidates"].find_one({"_id": f"{role}:{login}"})
    if not row:
        raise HTTPException(404, "Candidate not found")
    return row


def _budgets(ctx: Context) -> Dict[str, Optional[Dict[str, Any]]]:
    return {d["_id"]: d.get("budget") for d in ctx.db["roles"].find({}, {"budget": 1})}


@router.get("", response_model=CandidateList)
def list_candidates(
    ctx: Context = Depends(get_context),
    role: Optional[str] = None,
    status: Optional[str] = None,
    min_score: float = Query(0, ge=0, le=100),
    q: Optional[str] = None,
    location: Optional[str] = None,
    has_email: bool = False,
    has_phone: bool = False,
    has_linkedin: bool = False,
    has_portfolio: bool = False,
    answered: Optional[bool] = None,
    budget: Optional[Literal["within", "stretch", "over"]] = None,
    sort: Literal["score", "name", "est_years", "last_active", "status", "expected_ctc"] = "score",
    order: Literal["asc", "desc"] = "desc",
    limit: int = Query(25, ge=1, le=100),
    skip: int = Query(0, ge=0),
) -> CandidateList:
    if status and status not in STATUSES:
        raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
    if budget and not role:
        raise HTTPException(422, "Choose a role to filter by budget")
    budgets = _budgets(ctx)
    query = queries.build_query(role, status, min_score, q, location, has_email, has_phone, has_linkedin, has_portfolio,
                                answered, budget, budgets.get(role) if role else None)
    col = ctx.db["candidates"]
    direction = 1 if order == "asc" else -1
    rows = col.find(query).sort([(queries.SORTS[sort], direction), ("_id", 1)]).skip(skip).limit(limit)
    return CandidateList(total=col.count_documents(query), items=[queries.list_item(r, budgets.get(r["role"])) for r in rows])


@router.get("/facets", response_model=Facets)
def facets(ctx: Context = Depends(get_context), role: Optional[str] = None) -> Facets:
    match = {"role": role} if role else {}
    counts = {r["_id"]: r["n"] for r in ctx.db["candidates"].aggregate(
        [{"$match": match}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}])}
    return Facets(total=sum(counts.values()), by_status=[StatusCount(status=s, count=counts.get(s, 0)) for s in STATUSES])


@router.get("/{role}/{login}", response_model=CandidateDetail)
def get_candidate(role: str, login: str, ctx: Context = Depends(get_context)) -> CandidateDetail:
    return queries.detail(ctx.db, _row(ctx, role, login))


@router.patch("/{role}/{login}", response_model=CandidateDetail)
def patch_candidate(role: str, login: str, body: CandidatePatch, ctx: Context = Depends(get_context)) -> CandidateDetail:
    row = _row(ctx, role, login)
    updates: Dict[str, Any] = {}
    events: List[Dict[str, Any]] = []

    if body.status is not None and body.status != row.get("status", "new"):
        if body.status not in STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
        updates["status"] = body.status
        events.append({"type": "status", "from_status": row.get("status", "new"), "to_status": body.status})
    if body.notes is not None and body.notes != (row.get("notes") or ""):
        updates["notes"] = body.notes
        events.append({"type": "note", "note": body.notes})

    if updates:
        now = datetime.now(timezone.utc)
        ctx.db["candidates"].update_one({"_id": row["_id"]}, {"$set": {**updates, "updated_at": now}})
        ctx.db["candidate_events"].insert_many(
            [{**e, "candidate_id": row["_id"], "role": role, "login": login, "by": ctx.user["email"], "at": now} for e in events]
        )
    return queries.detail(ctx.db, _row(ctx, role, login))


@router.delete("/{role}/{login}", response_model=DeletedOut)
def delete_candidate(role: str, login: str, ctx: Context = Depends(require_admin)) -> DeletedOut:
    """Remove a candidate and everything held about them (notes, history, form links and answers)."""
    _row(ctx, role, login)
    counts = screening.delete_candidate(ctx.db, role, login)
    audit(ctx, "delete_candidate", {"role": role, "login": login, **counts})
    return DeletedOut(**counts)


@router.get("/{role}/{login}/events", response_model=List[EventOut])
def candidate_events(role: str, login: str, ctx: Context = Depends(get_context)) -> List[EventOut]:
    _row(ctx, role, login)  # 404 for unknown candidates
    cursor = ctx.db["candidate_events"].find({"candidate_id": f"{role}:{login}"}).sort("at", -1).limit(100)
    return [EventOut(type=e["type"], from_status=e.get("from_status"), to_status=e.get("to_status"),
                     note=e.get("note"), by=e["by"], at=e["at"].isoformat()) for e in cursor]


def _screening_out(ctx: Context, row: Dict[str, Any]) -> ScreeningOut:
    budget = queries.role_budget(ctx.db, row["role"])
    budget_out = BudgetOut(**budget) if budget else None
    link = screening.latest_link(ctx.db, row["_id"])
    state = screening.link_state(link) if link else "none"

    if state == "answered":
        resp = ctx.db["screening_responses"].find_one({"candidate_id": row["_id"]}, sort=[("submitted_at", -1)])
        if resp:
            return ScreeningOut(
                state="answered", budget=budget_out, verdict=screening.verdict(resp["expected_ctc"], budget),
                answers=ScreeningAnswers(current_ctc=resp["current_ctc"], expected_ctc=resp["expected_ctc"], notice_days=resp["notice_days"],
                                         comment=resp.get("comment"), submitted_at=resp["submitted_at"].isoformat()),
            )
        return ScreeningOut(state="none", budget=budget_out)  # the answers were removed (retention or deletion)
    if state == "open":
        return ScreeningOut(state="pending", link_url=screening.url_for(ctx.tenant, link), expires_at=link["expires_at"].isoformat(), budget=budget_out)
    return ScreeningOut(state="expired" if state == "expired" else "none", budget=budget_out)


@router.get("/{role}/{login}/screening", response_model=ScreeningOut)
def get_screening(role: str, login: str, ctx: Context = Depends(get_context)) -> ScreeningOut:
    return _screening_out(ctx, _row(ctx, role, login))


@router.post("/{role}/{login}/screening-link", response_model=ScreeningOut)
def new_screening_link(role: str, login: str, ctx: Context = Depends(get_context)) -> ScreeningOut:
    """Cancel any open link for this candidate and issue a fresh one (14 days, single use)."""
    row = _row(ctx, role, login)
    screening.regenerate_link(ctx.db, row, ctx.user["email"])
    audit(ctx, "screening_link", {"role": role, "login": login})
    return _screening_out(ctx, row)


@router.post("/{role}/{login}/draft", response_model=DraftOut)
def draft(role: str, login: str, body: DraftIn, ctx: Context = Depends(get_context)) -> DraftOut:
    row = _row(ctx, role, login)
    if body.template_id and not templates_store.get(ctx.db, body.template_id):
        raise HTTPException(404, "Template not found")
    outreach = queries.resolve_outreach(ctx.db, role, body.template_id)
    form_link = ""
    if uses_form_link(outreach["subject"], outreach["body"]):  # reuse the open link, so the URL stays the same
        form_link = screening.url_for(ctx.tenant, screening.get_or_create_link(ctx.db, row, ctx.user["email"]))
    values = candidate_values(row, ctx.tenant_name, outreach["role_title"], body.sender.strip(), form_link)
    subject, text = render(outreach["subject"], outreach["body"], values)
    to = best_email(row)
    return DraftOut(to=to, subject=subject, body=text, template_name=outreach["name"], **(mail_links(to, subject, text) if to else {}))
