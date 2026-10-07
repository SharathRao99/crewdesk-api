"""JSON API for the web app.

Browsers never call this directly. The Next.js server does, sending a shared service key plus
the tenant and user from the logged-in session. Tenant data is only reachable through the
`Context` dependency (deps.py), which checks that the user really belongs to the tenant.
"""

from typing import Dict

from fastapi import FastAPI

from sourcer.api.routes import auth, candidates, export, roles, templates

app = FastAPI(title="Talent Sourcer API", version="0.2.0")


@app.get("/health", tags=["health"])
def health() -> Dict[str, str]:
    return {"status": "ok"}


for module in (auth, candidates, roles, templates, export):
    app.include_router(module.router)
