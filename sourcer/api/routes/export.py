from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from sourcer import roles_store
from sourcer.api import queries
from sourcer.api.deps import Context, audit, get_context
from sourcer.enrich import location_ok
from sourcer.export import csv_bytes, has_contact, has_useful_link, prepare, xlsx_bytes
from sourcer.store import STATUSES

router = APIRouter(tags=["export"])

MAX_ROWS = 5000
KEEP = {"contact": has_contact, "links": has_useful_link, "none": lambda row: True}


@router.get("/export")
def export(
    ctx: Context = Depends(get_context),
    role: str = Query(...),
    format: Literal["csv", "xlsx"] = "xlsx",
    require: Literal["contact", "links", "none"] = "contact",
    status: Optional[str] = None,
    min_score: float = Query(0, ge=0, le=100),
    q: Optional[str] = None,
    location: Optional[str] = None,
    template_id: Optional[str] = None,
    sender: str = "",
) -> Response:
    if status and status not in STATUSES:
        raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
    query = queries.build_query(role, status, min_score, q, location)
    rows = list(ctx.db["candidates"].find(query).sort("score", -1).limit(MAX_ROWS))
    try:  # drop people outside the role's target places, as the standalone report does
        role_obj, _ = roles_store.get_role(ctx.db, role)
        rows = [r for r in rows if location_ok(r.get("location"), role_obj)]
    except KeyError:
        pass
    outreach = queries.resolve_outreach(ctx.db, role, template_id)
    prepared = prepare(rows, outreach, sender.strip(), KEEP[require], ctx.tenant_name)

    audit(ctx, "export", {"role": role, "format": format, "rows": len(prepared), "require": require})
    if format == "csv":
        data, media = csv_bytes(prepared), "text/csv; charset=utf-8"
    else:
        data, media = xlsx_bytes(prepared), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return Response(content=data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{role}.{format}"'})
