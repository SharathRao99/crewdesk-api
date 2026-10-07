from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from sourcer import roles_store, templates_store
from sourcer.api import queries
from sourcer.api.deps import Context, get_context
from sourcer.api.models import (CandidateDetail, CandidateList, CandidatePatch, DraftIn, DraftOut, EventOut,
                                Facets, StatusCount)
from sourcer.outreach import best_email, candidate_values, mail_links, render
from sourcer.store import STATUSES

router = APIRouter(prefix="/candidates", tags=["candidates"])


def _row(ctx: Context, role: str, login: str) -> Dict[str, Any]:
    row = ctx.db["candidates"].find_one({"_id": f"{role}:{login}"})
    if not row:
        raise HTTPException(404, "Candidate not found")
    return row


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
    sort: Literal["score", "name", "est_years", "last_active", "status"] = "score",
    order: Literal["asc", "desc"] = "desc",
    limit: int = Query(25, ge=1, le=100),
    skip: int = Query(0, ge=0),
) -> CandidateList:
    if status and status not in STATUSES:
        raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
    query = queries.build_query(role, status, min_score, q, location, has_email, has_phone, has_linkedin, has_portfolio)
    col = ctx.db["candidates"]
    direction = 1 if order == "asc" else -1
    rows = col.find(query).sort([(queries.SORTS[sort], direction), ("_id", 1)]).skip(skip).limit(limit)
    return CandidateList(total=col.count_documents(query), items=[queries.list_item(r) for r in rows])


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


@router.get("/{role}/{login}/events", response_model=List[EventOut])
def candidate_events(role: str, login: str, ctx: Context = Depends(get_context)) -> List[EventOut]:
    _row(ctx, role, login)  # 404 for unknown candidates
    cursor = ctx.db["candidate_events"].find({"candidate_id": f"{role}:{login}"}).sort("at", -1).limit(100)
    return [EventOut(type=e["type"], from_status=e.get("from_status"), to_status=e.get("to_status"),
                     note=e.get("note"), by=e["by"], at=e["at"].isoformat()) for e in cursor]


@router.post("/{role}/{login}/draft", response_model=DraftOut)
def draft(role: str, login: str, body: DraftIn, ctx: Context = Depends(get_context)) -> DraftOut:
    row = _row(ctx, role, login)
    if body.template_id and not templates_store.get(ctx.db, body.template_id):
        raise HTTPException(404, "Template not found")
    outreach = queries.resolve_outreach(ctx.db, role, body.template_id)
    values = candidate_values(row, ctx.tenant_name, outreach["role_title"], body.sender.strip())
    subject, text = render(outreach["subject"], outreach["body"], values)
    to = best_email(row)
    return DraftOut(to=to, subject=subject, body=text, template_name=outreach["name"], **(mail_links(to, subject, text) if to else {}))
