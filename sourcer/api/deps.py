"""Shared API dependencies: the service key and the tenant/user context."""

import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Depends, Header, HTTPException, Request
from pymongo.database import Database

from sourcer import modules
from sourcer.tenancy import registry
from sourcer.tenancy.db import TenantError, tenant_db


def require_service_key(x_service_key: Optional[str] = Header(default=None)) -> None:
    expected = os.getenv("API_SERVICE_KEY")
    if not expected:
        raise HTTPException(503, "API_SERVICE_KEY is not configured")
    if not x_service_key or not hmac.compare_digest(x_service_key, expected):
        raise HTTPException(401, "Invalid service key")


@dataclass
class Context:
    tenant: str
    tenant_name: str
    user: Dict[str, Any]
    db: Database


def _session_ended(detail: str) -> HTTPException:
    """401 plus a marker: the web server ends the signed-in session. A plain 403 means "not allowed" and does not."""
    return HTTPException(401, detail, headers={"X-Session": "ended"})


# The only routes a user who still has a one-time password may call: to see who they are and to change it.
PASSWORD_CHANGE_PATHS = ("/me", "/me/password")


def get_context(
    request: Request,
    _: None = Depends(require_service_key),
    x_tenant: Optional[str] = Header(default=None),
    x_user_id: Optional[str] = Header(default=None),
    x_session_version: Optional[str] = Header(default=None),
) -> Context:
    if not x_tenant or not x_user_id:
        raise HTTPException(400, "X-Tenant and X-User-Id are required")
    user = registry.get_user_by_id(x_user_id)
    # A session for tenant A must never be usable against tenant B, even with a valid key.
    if not user or user.get("disabled") or user["tenant"] != x_tenant:
        raise _session_ended("User does not belong to this tenant")
    # The web server sends the version stored in the session cookie. A password reset, a new login link or
    # disabling the user bumps it, which ends every older session.
    if x_session_version is not None and x_session_version != str(user.get("session_version", 0)):
        raise _session_ended("Session expired")
    if user.get("must_change_password") and request.url.path not in PASSWORD_CHANGE_PATHS:
        raise HTTPException(403, "Change your one-time password first")
    try:
        db = tenant_db(x_tenant)
        name = registry.get_tenant(x_tenant)["name"]
    except TenantError:
        raise _session_ended("Tenant unavailable") from None
    return Context(tenant=x_tenant, tenant_name=name, user=user, db=db)


def audit(ctx: Context, action: str, detail: Optional[Dict[str, Any]] = None) -> None:
    """Record who did something that moves personal data (exports, drafts)."""
    ctx.db["audit_log"].insert_one(
        {"action": action, "by": str(ctx.user["_id"]), "email": ctx.user["email"], "detail": detail or {}, "at": datetime.now(timezone.utc)}
    )


def require_admin(ctx: Context = Depends(get_context)) -> Context:
    if ctx.user.get("role") != "admin":
        raise HTTPException(403, "Only an administrator can do this")
    return ctx


def require_module(name: str):
    """Dependency factory: the user must be allowed to open module `name`. Applied to whole routers in app.py."""

    def check(ctx: Context = Depends(get_context)) -> Context:
        if name not in modules.modules_for(ctx.user):
            raise HTTPException(403, "You do not have access to this part of the app")
        return ctx

    return check
