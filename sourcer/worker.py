"""The background worker: picks up queued sourcing jobs and runs them. `sourcer worker` starts it.

It can be stopped and restarted at any time. A job it was running goes back in the queue and continues
from its last saved point.
"""

import logging
import os
import signal
import socket
import threading
import time
from typing import Any, Callable, Optional

from sourcer import jobs, pipeline
from sourcer.portfolio import crawl_portfolio
from sourcer.tenancy import registry
from sourcer.tenancy.db import tenant_db

logger = logging.getLogger(__name__)


def run_once(
    worker_id: str,
    *,
    client_factory: Optional[Callable[..., Any]] = None,
    crawl: Callable[..., Any] = crawl_portfolio,
    stopping: Callable[[], bool] = lambda: False,
) -> bool:
    """Run at most one job, for the first company that has one waiting. True if a job was run."""
    for tenant in registry.list_tenants():
        if tenant.get("status") != "active":
            continue
        slug = tenant["slug"]
        db = tenant_db(slug)
        jobs.requeue_orphans(db)
        job = jobs.claim_next(db, worker_id)
        if not job:
            continue
        token = registry.get_github_token(slug)
        if not token and client_factory is None:
            jobs.event(db, job["_id"], "error", "This company has no GitHub token. Add one with: sourcer-admin tenant set-github-token")
            jobs.finish(db, job["_id"], "failed", "No GitHub token is set for this company")
            return True
        logger.info("running job %s for %s", job["_id"], slug)
        pipeline.run_job(db, job, token, client_factory=client_factory, crawl=crawl, stopping=stopping)
        return True
    return False


def loop(worker_id: str, stop: dict, poll_seconds: float = 5.0) -> None:
    """Run jobs until stop["now"] is set."""
    while not stop["now"]:
        try:
            ran = run_once(worker_id, stopping=lambda: stop["now"])
        except Exception:
            logger.exception("worker loop error")
            ran = False
        if not ran:
            time.sleep(poll_seconds)


def start_in_background(poll_seconds: float = 5.0) -> Callable[[], None]:
    """Run the worker on a thread inside this process (for a small host that has one process only). Returns a function that stops it."""
    stop = {"now": False}
    worker_id = f"{socket.gethostname()}:{os.getpid()}:thread"
    thread = threading.Thread(target=loop, args=(worker_id, stop, poll_seconds), name="sourcing-worker", daemon=True)
    thread.start()
    logger.info("worker %s started inside the API", worker_id)

    def stopper() -> None:
        stop["now"] = True
        thread.join(timeout=30)

    return stopper


def main(poll_seconds: float = 5.0) -> None:
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    stop = {"now": False}

    def handle(signum: int, _frame: Any) -> None:
        logger.info("signal %s received: finishing the current step and stopping", signum)
        stop["now"] = True

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    logger.info("worker %s started", worker_id)
    loop(worker_id, stop, poll_seconds)
    logger.info("worker stopped")
