from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException

from sourcer import jobs, roles_store
from sourcer.api.deps import Context, audit, get_context
from sourcer.api.models import JobCreate, JobDetail, JobEventOut, JobOut
from sourcer.tenancy import registry

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _iso(value: Any) -> Any:
    return value.isoformat() if value else None


def _out(job: Dict[str, Any]) -> JobOut:
    params = job["params"]
    return JobOut(
        id=str(job["_id"]), role=job["role"], status=job["status"], control=job.get("control"), phase=job["phase"],
        total=job["total"], cursor=job["cursor"], kept=job["kept"], not_primary=job["not_primary"], dropped=job["dropped"],
        errors=job["errors"], contacts_total=job["contacts_total"], contacts_done=job["contacts_done"], removed=job["removed"],
        note=job.get("note"), error=job.get("error"), pages=params["pages"], max_candidates=params["max_candidates"],
        remove_stale=params["remove_stale"], created_by=job["created_by"], created_at=_iso(job["created_at"]),
        started_at=_iso(job.get("started_at")), finished_at=_iso(job.get("finished_at")),
    )


def _job(ctx: Context, job_id: str) -> Dict[str, Any]:
    job = jobs.get(ctx.db, job_id)
    if not job:
        raise HTTPException(404, "Run not found")
    return job


@router.get("", response_model=List[JobOut])
def list_jobs(ctx: Context = Depends(get_context), active: bool = False, limit: int = 30) -> List[JobOut]:
    return [_out(j) for j in jobs.list_jobs(ctx.db, active_only=active, limit=max(1, min(limit, 100)))]


@router.post("", response_model=JobOut, status_code=201)
def create_job(body: JobCreate, ctx: Context = Depends(get_context)) -> JobOut:
    """Queue a sourcing run. A background worker picks it up; it can take a few hours."""
    if not ctx.db["roles"].find_one({"_id": body.role}):
        raise HTTPException(404, "Role not found")
    if not registry.get_github_token(ctx.tenant):
        raise HTTPException(409, "Your company has no GitHub token yet. An administrator adds one with: sourcer-admin tenant set-github-token")
    try:
        job = jobs.create(ctx.db, body.role, body.model_dump(exclude={"role"}), ctx.user["email"])
    except jobs.JobError as e:
        raise HTTPException(409, str(e)) from None
    audit(ctx, "job_created", {"role": body.role, **body.model_dump(exclude={"role"})})
    return _out(job)


@router.get("/{job_id}", response_model=JobDetail)
def get_job(job_id: str, ctx: Context = Depends(get_context)) -> JobDetail:
    job = _job(ctx, job_id)
    log = [JobEventOut(level=e["level"], message=e["message"], at=e["at"].isoformat()) for e in jobs.events(ctx.db, job["_id"])]
    return JobDetail(**_out(job).model_dump(), events=log)


def _act(ctx: Context, job_id: str, action: Any) -> JobOut:
    job = _job(ctx, job_id)
    try:
        action(ctx.db, job)
    except jobs.JobError as e:
        raise HTTPException(409, str(e)) from None
    return _out(_job(ctx, job_id))


@router.post("/{job_id}/pause", response_model=JobOut)
def pause_job(job_id: str, ctx: Context = Depends(get_context)) -> JobOut:
    return _act(ctx, job_id, jobs.pause)


@router.post("/{job_id}/resume", response_model=JobOut)
def resume_job(job_id: str, ctx: Context = Depends(get_context)) -> JobOut:
    return _act(ctx, job_id, jobs.resume)


@router.post("/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: str, ctx: Context = Depends(get_context)) -> JobOut:
    return _act(ctx, job_id, jobs.cancel)
