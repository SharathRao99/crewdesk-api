"""Shared API dependencies: the service key and the tenant/user context."""

import hmac
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Depends, Header, HTTPException
from pymongo.database import Database

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
        name = registry.get_tenant(x_tenant)["name"]
    except TenantError:
        raise HTTPException(403, "Tenant unavailable") from None
    return Context(tenant=x_tenant, tenant_name=name, user=user, db=db)


def audit(ctx: Context, action: str, detail: Optional[Dict[str, Any]] = None) -> None:
    """Record who did something that moves personal data (exports, drafts)."""
    ctx.db["audit_log"].insert_one(
        {"action": action, "by": str(ctx.user["_id"]), "email": ctx.user["email"], "detail": detail or {}, "at": datetime.now(timezone.utc)}
    )
