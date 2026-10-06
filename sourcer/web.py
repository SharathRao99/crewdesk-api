"""Small FastAPI app to browse candidates and track outreach."""

import csv
import io
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from sourcer.export import COLUMNS, flatten, has_useful_link
from sourcer.store import STATUSES, find_candidates, get_collection, set_status

app = FastAPI(title="Talent Sourcer")
STATIC = Path(__file__).parent / "static"


class StatusUpdate(BaseModel):
    status: str
    note: Optional[str] = None


def _clean(row: Dict[str, Any]) -> Dict[str, Any]:
    row.pop("_id", None)
    for key in ("updated_at", "first_seen"):
        if key in row:
            row[key] = row[key].isoformat()
    return row


def _query(
    role: str, status: Optional[str], min_score: float, q: Optional[str], reachable: bool = False
) -> List[Dict[str, Any]]:
    rows = [_clean(r) for r in find_candidates(get_collection(), role, status, min_score)]
    if reachable:
        rows = [r for r in rows if has_useful_link(r)]
    if q:
        needle = q.lower()
        rows = [
            r for r in rows
            if needle in " ".join(str(r.get(k) or "") for k in ("login", "name", "location", "bio")).lower()
        ]
    return rows


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta() -> Dict[str, Any]:
    return {"roles": sorted(get_collection().distinct("role")), "statuses": list(STATUSES)}


@app.get("/api/candidates")
def candidates(
    role: str, status: Optional[str] = None, min_score: float = 0, q: Optional[str] = None, reachable: bool = False
) -> List[Dict[str, Any]]:
    return _query(role, status, min_score, q, reachable)


@app.patch("/api/candidates/{login}")
def update(login: str, role: str, body: StatusUpdate) -> Dict[str, str]:
    if body.status not in STATUSES:
        raise HTTPException(400, f"status must be one of {', '.join(STATUSES)}")
    if not set_status(get_collection(), role, login, body.status, body.note or ""):
        raise HTTPException(404, "candidate not found")
    return {"login": login, "status": body.status}


@app.get("/api/export.csv")
def export(
    role: str, status: Optional[str] = None, min_score: float = 0, q: Optional[str] = None, reachable: bool = False
) -> StreamingResponse:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in _query(role, status, min_score, q, reachable):
        if has_useful_link(row):
            writer.writerow(flatten(row))
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{role}.csv"'},
    )
