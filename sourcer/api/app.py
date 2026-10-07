"""JSON API for the web app.

Browsers never call this directly. The Next.js server does, sending a shared service key plus
the tenant and user from the logged-in session. Tenant data is only reachable through the
`Context` dependency, which checks that the user really belongs to the tenant.
"""

import hmac
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from pymongo.database import Database

from sourcer.tenancy import registry
from sourcer.tenancy.db import TenantError, tenant_db

app = FastAPI(title="Talent Sourcer API", version="0.1.0")


def require_service_key(x_service_key: Optional[str] = Header(default=None)) -> None:
    expected = os.getenv("API_SERVICE_KEY")
    if not expected:
        raise HTTPException(503, "API_SERVICE_KEY is not configured")
    if not x_service_key or not hmac.compare_digest(x_service_key, expected):
        raise HTTPException(401, "Invalid service key")


@dataclass
class Context:
    tenant: str
    user: Dict[str, Any]
    db: Database


def get_context(
    _: None = Depends(require_service_key),
    x_tenant: Optional[str] = Header(default=None),
    x_user_id: Optional[str] = Header(default=None),
) -> Context:
    if not x_tenant or not x_user_id:
        raise HTTPException(400, "X-Tenant and X-User-Id are required")
    user = registry.get_user_by_id(x_user_id)
    # A session for tenant A must never be usable against tenant B, even with a valid key.
    if not user or user.get("disabled") or user["tenant"] != x_tenant:
        raise HTTPException(403, "User does not belong to this tenant")
    try:
        db = tenant_db(x_tenant)
    except TenantError:
        raise HTTPException(403, "Tenant unavailable") from None
    return Context(tenant=x_tenant, user=user, db=db)


class LoginBody(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    tenant: str
    tenant_name: str
    role: str
    must_change_password: bool


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/auth/login", response_model=UserOut, dependencies=[Depends(require_service_key)])
def login(body: LoginBody) -> UserOut:
    user = registry.verify_login(body.email, body.password)
    if not user:
        raise HTTPException(401, "Invalid email or password")
    return UserOut(
        id=str(user["_id"]),
        email=user["email"],
        tenant=user["tenant"],
        tenant_name=user["tenant_name"],
        role=user["role"],
        must_change_password=user.get("must_change_password", False),
    )


@app.get("/me")
def me(ctx: Context = Depends(get_context)) -> Dict[str, Any]:
    return {"id": str(ctx.user["_id"]), "email": ctx.user["email"], "tenant": ctx.tenant, "role": ctx.user["role"]}


@app.get("/candidates")
def candidates(
    ctx: Context = Depends(get_context),
    role: Optional[str] = None,
    status: Optional[str] = None,
    min_score: float = 0,
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
) -> Dict[str, Any]:
    query: Dict[str, Any] = {"score": {"$gte": min_score}}
    if role:
        query["role"] = role
    if status:
        query["status"] = status
    col = ctx.db["candidates"]
    rows: List[Dict[str, Any]] = list(col.find(query, {"_id": 0}).sort("score", -1).skip(skip).limit(limit))
    return {"total": col.count_documents(query), "items": jsonable_encoder(rows)}
