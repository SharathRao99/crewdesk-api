"""The sourcing pipeline as the worker runs it.

discover people -> evaluate and save them in batches -> add portfolio contact links -> finish.

Every batch is saved together with a cursor into the (saved) list of discovered people, so a crash,
a pause or a restart carries on from the last batch instead of starting again. Saving a person is an
upsert, so doing a few of them twice is harmless.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from pymongo.database import Database

from sourcer import jobs, roles_store
from sourcer.discover import discover
from sourcer.enrich import enrich, is_preferred
from sourcer.github import GitHubClient, Interrupted
from sourcer.portfolio import crawl_portfolio, find_phones
from sourcer.role import Role
from sourcer.scoring import meets_primary_skill, score
from sourcer.store import prune_stale, save_candidates, update_fields

logger = logging.getLogger(__name__)

CHECKPOINT_EVERY = 25  # people per saved batch
MAX_CONSECUTIVE_ERRORS = 10  # this many failures in a row means something systemic (network, token): stop
CONTACT_CHUNK = 24
LOGGED_ERRORS = 20  # only the first few skipped profiles are written to the job's log
CONTROL_POLL_SECONDS = 1.0  # how often a running job looks for a pause or cancel request


def evaluate(client: GitHubClient, role: Role, login: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """('kept', row) | ('dropped', None) outside the role's places or too few repos | ('not_primary', None)."""
    candidate = enrich(client, role, login)
    if not candidate or candidate.public_repos < role.min_public_repos:
        return "dropped", None
    if not meets_primary_skill(candidate, role):
        return "not_primary", None
    breakdown = score(candidate, role)
    return "kept", {**asdict(candidate), "score": breakdown["total"], "score_breakdown": breakdown,
                    "preferred": is_preferred(candidate.location, role.preferred_locations)}


class _Control:
    """Why the run should stop, if it should. Reads the job's `control` at most once a second."""

    def __init__(self, db: Database, job_id: Any, stopping: Callable[[], bool]):
        self.db, self.job_id, self.stopping = db, job_id, stopping
        self._checked, self._value = 0.0, None

    def reason(self) -> Optional[str]:
        if self.stopping():
            return "shutdown"
        now = time.monotonic()
        if now - self._checked >= CONTROL_POLL_SECONDS:
            self._value, self._checked = jobs.control_of(self.db, self.job_id), now
        return self._value


class _Heartbeat:
    """Tells the database every few seconds that the job is alive, even while it waits on GitHub."""

    def __init__(self, db: Database, job_id: Any):
        self.db, self.job_id, self._stop = db, job_id, threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(jobs.HEARTBEAT_EVERY):
            try:
                jobs.beat(self.db, self.job_id)
            except Exception:  # a hiccup must not kill the job; the next beat will do
                logger.warning("heartbeat failed", exc_info=True)

    def __enter__(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def run_job(
    db: Database,
    job: Dict[str, Any],
    token: Optional[str],
    *,
    client_factory: Optional[Callable[..., Any]] = None,
    crawl: Callable[[Optional[str]], Dict[str, Any]] = crawl_portfolio,
    stopping: Callable[[], bool] = lambda: False,
) -> str:
    """Run (or resume) one claimed job to the end, a pause, a cancel or a failure. Returns the final status.

    A crash that this function cannot catch (the process is killed) leaves the job `running`; its stale
    heartbeat is what lets `jobs.requeue_orphans` put it back in the queue.
    """
    job_id, params = job["_id"], job["params"]
    ctl = _Control(db, job_id, stopping)
    client: Optional[Any] = None
    try:
        try:
            role, _ = roles_store.get_role(db, job["role"])
        except KeyError:
            raise RuntimeError("This role no longer exists") from None

        def on_wait(reason: str, seconds: float) -> None:
            jobs.update(db, job_id, note=f"Waiting on GitHub ({reason}) for about {max(1, round(seconds / 60))} min. This is normal.")

        factory = client_factory or (lambda *a, **k: GitHubClient(*a, **k))
        client = factory(token, on_wait=on_wait, interrupt=lambda: ctl.reason() is not None)

        with _Heartbeat(db, job_id):
            logins = _discover(db, job, role, client, params)
            _process(db, job, role, client, logins, ctl)
            _add_contacts(db, job, role, logins, ctl, crawl)
            return _finish(db, job, role, params)
    except Interrupted:
        return _stopped(db, job_id, ctl.reason())
    except Exception as e:
        logger.exception("job %s failed", job_id)
        jobs.event(db, job_id, "error", f"Stopped: {e}")
        jobs.finish(db, job_id, "failed", str(e)[:300])
        return "failed"
    finally:
        if client is not None:
            client.close()


def _stopped(db: Database, job_id: Any, reason: Optional[str]) -> str:
    if reason == "cancel":
        jobs.finish(db, job_id, "cancelled")
        jobs.event(db, job_id, "info", "Cancelled. People already found are kept.")
        return "cancelled"
    if reason == "pause":
        jobs.finish(db, job_id, "paused")
        jobs.event(db, job_id, "info", "Paused. Resume to carry on from here.")
        return "paused"
    db["jobs"].update_one({"_id": job_id}, {"$set": {"status": "queued", "control": None, "note": None}})  # worker shutting down
    jobs.event(db, job_id, "info", "The worker is restarting. This run will continue from the last saved point.")
    return "queued"


def _discover(db: Database, job: Dict[str, Any], role: Role, client: Any, params: Dict[str, Any]) -> List[str]:
    if job.get("logins") is not None:
        return job["logins"]  # resuming: the list was saved by an earlier attempt
    jobs.update(db, job["_id"], phase="discovering")
    found = sorted(discover(client, role, pages=params["pages"]))
    if params["max_candidates"]:
        found = found[: params["max_candidates"]]
    jobs.update(db, job["_id"], logins=found, total=len(found), cursor=0, phase="processing", note=None)
    jobs.event(db, job["_id"], "info", f"Found {len(found)} possible people to check")
    job["logins"] = found
    return found


def _process(db: Database, job: Dict[str, Any], role: Role, client: Any, logins: List[str], ctl: _Control) -> None:
    job_id, collection = job["_id"], db["candidates"]
    kept, not_primary, dropped, errors = job["kept"], job["not_primary"], job["dropped"], job["errors"]
    done_upto, pending, consecutive = job["cursor"], [], 0

    def checkpoint(cursor: int) -> None:
        save_candidates(collection, role.name, pending)
        pending.clear()
        jobs.update(db, job_id, cursor=cursor, kept=kept, not_primary=not_primary, dropped=dropped, errors=errors, note=None)

    try:
        for index in range(job["cursor"], len(logins)):
            if ctl.reason():
                raise Interrupted()
            login = logins[index]
            try:
                outcome, row = evaluate(client, role, login)
            except Interrupted:
                raise
            except Exception as e:  # one bad profile must not stop a long run
                errors += 1
                consecutive += 1
                if errors <= LOGGED_ERRORS:
                    jobs.event(db, job_id, "warn", f"Skipped {login}: {e}")
                if consecutive >= MAX_CONSECUTIVE_ERRORS:
                    raise RuntimeError(f"{MAX_CONSECUTIVE_ERRORS} people in a row failed, so the run stopped. Progress is kept.") from e
            else:
                consecutive = 0
                if outcome == "kept":
                    kept += 1
                    pending.append(row)
                elif outcome == "not_primary":
                    not_primary += 1
                else:
                    dropped += 1
            done_upto = index + 1
            if done_upto % CHECKPOINT_EVERY == 0:
                checkpoint(done_upto)
    except BaseException as stop:
        if isinstance(stop, (Interrupted, Exception)):  # save what was finished, so nothing is redone
            checkpoint(done_upto)
        raise
    checkpoint(len(logins))
    jobs.event(db, job_id, "info", f"Checked everyone: {kept} kept, {not_primary} not mainly in the role's skill, {dropped} outside the role's places")


def _add_contacts(db: Database, job: Dict[str, Any], role: Role, logins: List[str], ctl: _Control, crawl: Callable[[Optional[str]], Dict[str, Any]]) -> None:
    """Read each person's own portfolio for LinkedIn, resume, email and phone links.

    Only people this run found: touching anyone else would refresh their `updated_at`, and a person
    the run did not find must still look stale to the optional clean-up.
    """
    job_id, collection = job["_id"], db["candidates"]
    todo = list(collection.find({"role": role.name, "login": {"$in": logins}, "contacts_checked_at": {"$exists": False}}, {"login": 1, "blog": 1, "bio": 1, "linkedin_url": 1}))
    total = job["contacts_total"] or len(todo)
    jobs.update(db, job_id, phase="contacts", contacts_total=total, contacts_done=total - len(todo))
    for start in range(0, len(todo), CONTACT_CHUNK):
        if ctl.reason():
            raise Interrupted()
        chunk = todo[start : start + CONTACT_CHUNK]
        with ThreadPoolExecutor(max_workers=8) as pool:
            sites = list(pool.map(lambda row: crawl(row.get("blog")), chunk))
        for row, site in zip(chunk, sites):
            update_fields(collection, role.name, row["login"], {
                "linkedin_url": row.get("linkedin_url") or site["linkedin"],
                "portfolio_url": site["portfolio_url"],
                "portfolio_emails": site["emails"],
                "resume_links": site["resumes"],
                "portfolio_phones": sorted(set(site["phones"]) | find_phones(row.get("bio") or ""))[:2],
                "contacts_checked_at": datetime.now(timezone.utc),
            })
        jobs.update(db, job_id, contacts_done=total - len(todo) + start + len(chunk))


def _finish(db: Database, job: Dict[str, Any], role: Role, params: Dict[str, Any]) -> str:
    fresh = db["jobs"].find_one({"_id": job["_id"]}, {"errors": 1, "started_at": 1})
    removed = 0
    # Only a complete, clean, uncapped run can say who no longer qualifies.
    if params["remove_stale"] and not params["max_candidates"] and fresh["errors"] == 0:
        removed = prune_stale(db["candidates"], role.name, fresh["started_at"])
        jobs.event(db, job["_id"], "info", f"Removed {removed} people who no longer match (only those you had not contacted)")
    elif params["remove_stale"]:
        jobs.event(db, job["_id"], "warn", "Did not remove anyone: the run had errors or a cap, so it cannot tell who no longer matches")
    jobs.update(db, job["_id"], removed=removed, phase="finished")
    jobs.finish(db, job["_id"], "done")
    jobs.event(db, job["_id"], "info", "Finished")
    return "done"
