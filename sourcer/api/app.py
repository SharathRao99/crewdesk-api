"""JSON API for the web app.

Browsers never call this directly. The Next.js server does, sending a shared service key plus
the tenant and user from the logged-in session. Tenant data is only reachable through the
`Context` dependency (deps.py), which checks that the user really belongs to the tenant.
"""

from typing import Dict

from fastapi import Depends, FastAPI

from sourcer.api.deps import require_module
from sourcer.people import routes as people_routes
from sourcer.api.routes import auth, candidates, export, jobs, public, roles, templates

app = FastAPI(title="Talent Sourcer API", version="0.2.0")


@app.get("/health", tags=["health"])
def health() -> Dict[str, str]:
    return {"status": "ok"}


# Open to every signed-in user (or to nobody signed in): sign-in, who-am-I, the public candidate form.
for module in (auth, public):
    app.include_router(module.router)

# Hiring: a router added here cannot forget the module check.
for module in (candidates, roles, jobs, templates, export):
    app.include_router(module.router, dependencies=[Depends(require_module("hiring"))])

# People: same rule, its own module.
app.include_router(people_routes.router, dependencies=[Depends(require_module("people"))])
