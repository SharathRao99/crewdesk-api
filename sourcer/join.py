"""One-time login links for employees.

HR creates a link and hands it over by hand (there is no email). The link carries a random token; only its hash is stored,
so a copy of the database cannot be used to sign in. A link works once, expires, and a newer link replaces any older one.
Setting the password and using up the link happen in one atomic step.
"""

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from bson import ObjectId

from sourcer.tenancy import registry
from sourcer.tenancy.db import platform_db, tenant_db

LINK_DAYS = 3
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")  # token_urlsafe(32)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _ensure_indexes() -> None:
    col = platform_db()["join_links"]
    col.create_index("token_hash", unique=True)
    col.create_index("expires_at", expireAfterSeconds=0)  # expired links delete themselves
    col.create_index("user_id")


def create_link(user_id: Any, tenant: str, by: str) -> Tuple[str, datetime]:
    """A fresh link for this login. Older unused links stop working and so does any session already signed in."""
    _ensure_indexes()
    col = platform_db()["join_links"]
    col.update_many({"user_id": user_id, "used_at": None, "revoked": False}, {"$set": {"revoked": True}})
    registry.bump_session(user_id)
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(days=LINK_DAYS)
    col.insert_one({"token_hash": _hash(token), "user_id": user_id, "tenant": tenant, "created_by": by, "created_at": _now(),
                    "expires_at": expires, "used_at": None, "revoked": False})
    return token, expires


def _open_filter(token: str) -> Dict[str, Any]:
    return {"token_hash": _hash(token), "used_at": None, "revoked": False, "expires_at": {"$gt": _now()}}


def peek(token: str) -> Optional[Dict[str, str]]:
    """Who this link is for ({name, company}) while it is open, else None. Every failure looks the same."""
    if not TOKEN_RE.match(token or ""):
        return None
    link = platform_db()["join_links"].find_one(_open_filter(token))
    if not link:
        return None
    user = platform_db()["users"].find_one({"_id": link["user_id"]})
    tenant = platform_db()["tenants"].find_one({"slug": link["tenant"]})
    if not user or user.get("disabled") or not tenant or tenant.get("status") != "active":
        return None
    try:
        emp = tenant_db(link["tenant"])["employees"].find_one({"_id": ObjectId(user["employee_id"])}, {"name": 1})
    except Exception:  # an employee record that cannot be read just means no name is shown
        emp = None
    return {"name": (emp or {}).get("name", ""), "company": tenant["name"]}


def consume(token: str, password: str) -> Optional[Dict[str, Any]]:
    """Choose the password and use the link up. Returns the signed-in user, or None if the link is no good.

    A weak password raises ValueError *before* the link is touched, so a typo does not burn it.
    """
    if not TOKEN_RE.match(token or ""):
        return None
    if len(password or "") < 10:
        raise ValueError("Use at least 10 characters")
    if len(password) > 256:
        raise ValueError("That password is too long")
    link = platform_db()["join_links"].find_one_and_update(_open_filter(token), {"$set": {"used_at": _now()}})  # atomic: one winner
    if not link:
        return None
    user = platform_db()["users"].find_one({"_id": link["user_id"]}, {"password_hash": 0})
    tenant = platform_db()["tenants"].find_one({"slug": link["tenant"]})
    if not user or user.get("disabled") or not tenant or tenant.get("status") != "active":
        return None
    registry.set_password(user["_id"], password)
    platform_db()["users"].update_one({"_id": user["_id"]}, {"$set": {"last_login": _now()}})
    fresh = registry.get_user_by_id(user["_id"])
    fresh["tenant_name"] = tenant["name"]
    return fresh
