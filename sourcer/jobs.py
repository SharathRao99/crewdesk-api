"""Sourcing jobs: a queued request to find candidates for a role, run by the worker.

Status flow: queued -> running -> done | failed | paused | cancelled.
A person controls a job with `control` (pause / cancel). The worker notices it between candidates
and during waits, then moves the job to paused / cancelled. A job keeps its place (`cursor`) when
paused, failed or interrupted, so resuming never starts over.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.database import Database
from pymongo import ReturnDocument

ACTIVE = ("queued", "running", "paused")  # a role can have only one of these at a time
HEARTBEAT_EVERY = 15  # seconds
STALE_AFTER = timedelta(minutes=2)  # a running job with no heartbeat for this long lost its worker
MAX_EVENTS = 500
DEFAULTS = {"pages": 2, "max_candidates": 0, "remove_stale": False}


class JobError(Exception):
    """The requested change is not allowed in the job's current state."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def oid(job_id: str) -> Optional[ObjectId]:
    try:
        return ObjectId(job_id)
    except (InvalidId, TypeError):
        return None


def create(db: Database, role: str, params: Dict[str, Any], by: str) -> Dict[str, Any]:
    if db["jobs"].find_one({"role": role, "status": {"$in": list(ACTIVE)}}):
        raise JobError("A sourcing run for this role is already queued, running or paused")
    job = {
        "role": role, "status": "queued", "control": None, "phase": "waiting", "params": {**DEFAULTS, **params},
        "logins": None, "total": 0, "cursor": 0, "kept": 0, "not_primary": 0, "dropped": 0, "errors": 0,
        "contacts_total": 0, "contacts_done": 0, "removed": 0, "note": None, "error": None,
        "created_by": by, "created_at": _now(), "started_at": None, "finished_at": None, "heartbeat_at": None, "worker": None,
    }
    db["jobs"].insert_one(job)
    event(db, job["_id"], "info", f"Queued by {by}")
    return job


def get(db: Database, job_id: str) -> Optional[Dict[str, Any]]:
    key = oid(job_id)
    return db["jobs"].find_one({"_id": key}, {"logins": 0}) if key else None


def list_jobs(db: Database, active_only: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
    query = {"status": {"$in": list(ACTIVE)}} if active_only else {}
    return list(db["jobs"].find(query, {"logins": 0}).sort("created_at", -1).limit(limit))


def event(db: Database, job_id: Any, level: str, message: str) -> None:
    db["job_events"].insert_one({"job_id": job_id, "level": level, "message": message[:300], "at": _now()})
    # Keep the log short: a long run with many skipped profiles must not grow without limit.
    count = db["job_events"].count_documents({"job_id": job_id})
    if count > MAX_EVENTS:
        old = db["job_events"].find({"job_id": job_id}, {"_id": 1}).sort("at", 1).limit(count - MAX_EVENTS)
        db["job_events"].delete_many({"_id": {"$in": [d["_id"] for d in old]}})


def events(db: Database, job_id: Any, limit: int = 100) -> List[Dict[str, Any]]:
    return list(db["job_events"].find({"job_id": job_id}).sort("at", -1).limit(limit))


# ---------- people changing a job ----------

def pause(db: Database, job: Dict[str, Any]) -> str:
    if job["status"] == "queued":
        _set_status(db, job, "paused")
    elif job["status"] == "running":
        db["jobs"].update_one({"_id": job["_id"]}, {"$set": {"control": "pause"}})
    elif job["status"] != "paused":
        raise JobError(f"A {job['status']} run cannot be paused")
    event(db, job["_id"], "info", "Pause requested")
    return "paused" if job["status"] != "running" else "pausing"


def cancel(db: Database, job: Dict[str, Any]) -> str:
    if job["status"] in ("queued", "paused", "failed"):
        _set_status(db, job, "cancelled", finished=True)
    elif job["status"] == "running":
        db["jobs"].update_one({"_id": job["_id"]}, {"$set": {"control": "cancel"}})
    else:
        raise JobError(f"A {job['status']} run cannot be cancelled")
    event(db, job["_id"], "info", "Cancel requested")
    return "cancelling" if job["status"] == "running" else "cancelled"


def resume(db: Database, job: Dict[str, Any]) -> None:
    """Put a paused or failed run back in the queue. It carries on from where it stopped."""
    if job["status"] not in ("paused", "failed"):
        raise JobError(f"A {job['status']} run cannot be resumed")
    if db["jobs"].find_one({"role": job["role"], "status": {"$in": list(ACTIVE)}, "_id": {"$ne": job["_id"]}}):
        raise JobError("Another run for this role is already active")
    db["jobs"].update_one({"_id": job["_id"]}, {"$set": {"status": "queued", "control": None, "error": None, "note": None}})
    event(db, job["_id"], "info", "Resumed: back in the queue")


def _set_status(db: Database, job: Dict[str, Any], status: str, finished: bool = False) -> None:
    update: Dict[str, Any] = {"status": status, "control": None, "note": None}
    if finished:
        update["finished_at"] = _now()
    db["jobs"].update_one({"_id": job["_id"]}, {"$set": update})


# ---------- the worker's side ----------

def claim_next(db: Database, worker: str) -> Optional[Dict[str, Any]]:
    """Take the oldest queued job, unless this company already has a live one running.

    Each company has its own GitHub token and so its own quota; running two jobs on one token would
    just make both slow. The claim itself is one atomic update, so two workers cannot take the same job.
    """
    fresh = _now() - STALE_AFTER
    if db["jobs"].find_one({"status": "running", "heartbeat_at": {"$gt": fresh}}):
        return None
    now = _now()
    claimed = db["jobs"].find_one_and_update(
        {"status": "queued"}, {"$set": {"status": "running", "heartbeat_at": now, "worker": worker, "note": None}},
        sort=[("created_at", 1)], return_document=ReturnDocument.AFTER,
    )
    if claimed and not claimed.get("started_at"):
        db["jobs"].update_one({"_id": claimed["_id"]}, {"$set": {"started_at": now}})
        claimed["started_at"] = now
    return claimed


def requeue_orphans(db: Database) -> int:
    """A running job whose worker stopped heartbeating goes back in the queue, keeping its place."""
    stale = _now() - STALE_AFTER
    orphans = list(db["jobs"].find({"status": "running", "$or": [{"heartbeat_at": None}, {"heartbeat_at": {"$lt": stale}}]}, {"_id": 1}))
    for job in orphans:
        db["jobs"].update_one({"_id": job["_id"], "status": "running"}, {"$set": {"status": "queued", "note": None}})
        event(db, job["_id"], "warn", "The worker stopped unexpectedly. Continuing from the last saved point.")
    return len(orphans)


def beat(db: Database, job_id: Any) -> None:
    db["jobs"].update_one({"_id": job_id}, {"$set": {"heartbeat_at": _now()}})


def control_of(db: Database, job_id: Any) -> Optional[str]:
    doc = db["jobs"].find_one({"_id": job_id}, {"control": 1})
    return (doc or {}).get("control")


def update(db: Database, job_id: Any, **fields: Any) -> None:
    db["jobs"].update_one({"_id": job_id}, {"$set": {**fields, "heartbeat_at": _now()}})


def finish(db: Database, job_id: Any, status: str, error: Optional[str] = None) -> None:
    db["jobs"].update_one(
        {"_id": job_id},
        {"$set": {"status": status, "control": None, "note": None, "error": error, "finished_at": _now()
                  if status in ("done", "failed", "cancelled") else None}},
    )
