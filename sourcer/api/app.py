"""JSON API for the web app.

Browsers never call this directly. The Next.js server does, sending a shared service key plus
the tenant and user from the logged-in session. Tenant data is only reachable through the
`Context` dependency (deps.py), which checks that the user really belongs to the tenant.
"""

import os
from contextlib import asynccontextmanager
from typing import Dict

from fastapi import Depends, FastAPI

from sourcer.api.deps import require_module
from sourcer.people import routes as people_routes
from sourcer.people import routes_leave as people_leave_routes
from sourcer.people import routes_requests as people_request_routes
from sourcer.people import routes_self as people_self_routes
from sourcer.people import routes_wfh as people_wfh_routes
from sourcer.api.routes import auth, candidates, export, jobs, join, public, roles, templates

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """RUN_WORKER_IN_API=1 runs the sourcing worker inside this process, for a small host with one process. Otherwise run `sourcer worker`."""
    stop = None
    if os.environ.get("RUN_WORKER_IN_API") == "1":
        from sourcer import worker

        stop = worker.start_in_background()
    yield
    if stop:
        stop()


app = FastAPI(title="Talent Sourcer API", version="0.2.0", lifespan=lifespan)


@app.get("/health", tags=["health"])
def health() -> Dict[str, str]:
    return {"status": "ok"}


# Open to every signed-in user (or to nobody signed in): sign-in, who-am-I, the public candidate form.
for module in (auth, public, join):
    app.include_router(module.router)

# Hiring: a router added here cannot forget the module check.
for module in (candidates, roles, jobs, templates, export):
    app.include_router(module.router, dependencies=[Depends(require_module("hiring"))])

# People: same rule, its own module.
for module in (people_routes, people_leave_routes, people_self_routes, people_request_routes, people_wfh_routes):
    app.include_router(module.router, dependencies=[Depends(require_module("people"))])
