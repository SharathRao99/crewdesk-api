"""Tenants and users (the `platform` database)."""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from sourcer.tenancy import crypto
from sourcer.tenancy.db import TenantError, client, platform_db, tenant_db_name, validate_slug

ROLES = ("admin", "recruiter")
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)

_hasher = PasswordHasher()
# Verified against when the email is unknown, so a missing user takes as long as a wrong password.
_DUMMY_HASH = _hasher.hash("not-a-real-password")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_email(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValueError(f"Invalid email {email!r}")
    return email


def ensure_platform_indexes() -> None:
    db = platform_db()
    db["tenants"].create_index("slug", unique=True)
    db["users"].create_index("email", unique=True)
    db["users"].create_index("tenant")


def ensure_tenant_indexes(slug: str) -> None:
    db = client()[tenant_db_name(slug)]
    db["candidates"].create_index([("role", ASCENDING), ("score", DESCENDING)])
    db["candidates"].create_index("status")


# ---------- tenants ----------

def create_tenant(slug: str, name: str) -> Dict[str, Any]:
    validate_slug(slug)
    ensure_platform_indexes()
    doc = {"slug": slug, "name": name.strip() or slug, "status": "active", "github_token_enc": None, "created_at": _now()}
    try:
        platform_db()["tenants"].insert_one(doc)
    except DuplicateKeyError:
        raise TenantError(f"Tenant {slug!r} already exists") from None
    ensure_tenant_indexes(slug)
    return doc


def list_tenants() -> List[Dict[str, Any]]:
    return list(platform_db()["tenants"].find({}, {"_id": 0, "github_token_enc": 0}).sort("slug", 1))


def get_tenant(slug: str) -> Dict[str, Any]:
    tenant = platform_db()["tenants"].find_one({"slug": validate_slug(slug)})
    if not tenant:
        raise TenantError(f"Unknown tenant {slug!r}")
    return tenant


def set_github_token(slug: str, token: str) -> None:
    get_tenant(slug)
    platform_db()["tenants"].update_one({"slug": slug}, {"$set": {"github_token_enc": crypto.encrypt(token.strip())}})


def get_github_token(slug: str) -> Optional[str]:
    enc = get_tenant(slug).get("github_token_enc")
    return crypto.decrypt(enc) if enc else None


def delete_tenant(slug: str) -> None:
    """Drop the tenant's database, its users and its registry entry."""
    get_tenant(slug)
    client().drop_database(tenant_db_name(slug))
    platform_db()["users"].delete_many({"tenant": slug})
    platform_db()["tenants"].delete_one({"slug": slug})


# ---------- users ----------

def new_password() -> str:
    return secrets.token_urlsafe(12)


def create_user(email: str, tenant: str, role: str = "recruiter", password: Optional[str] = None) -> Tuple[Dict[str, Any], str]:
    """Create a user and return (user, one-time password). They must change it at first login."""
    if role not in ROLES:
        raise ValueError(f"Role must be one of {', '.join(ROLES)}")
    get_tenant(tenant)
    ensure_platform_indexes()
    password = password or new_password()
    doc = {
        "email": _norm_email(email),
        "tenant": tenant,
        "role": role,
        "password_hash": _hasher.hash(password),
        "must_change_password": True,
        "disabled": False,
        "failed_logins": 0,
        "locked_until": None,
        "created_at": _now(),
        "last_login": None,
    }
    try:
        platform_db()["users"].insert_one(doc)
    except DuplicateKeyError:
        raise ValueError(f"User {doc['email']} already exists") from None
    return doc, password


def list_users(tenant: Optional[str] = None) -> List[Dict[str, Any]]:
    query = {"tenant": tenant} if tenant else {}
    return list(platform_db()["users"].find(query, {"password_hash": 0}).sort("email", 1))


def get_user_by_id(user_id: Any) -> Optional[Dict[str, Any]]:
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        return platform_db()["users"].find_one({"_id": ObjectId(str(user_id))}, {"password_hash": 0})
    except InvalidId:
        return None


def reset_password(email: str) -> str:
    password = new_password()
    result = platform_db()["users"].update_one(
        {"email": _norm_email(email)},
        {"$set": {"password_hash": _hasher.hash(password), "must_change_password": True, "failed_logins": 0, "locked_until": None}},
    )
    if not result.matched_count:
        raise ValueError(f"No user {email!r}")
    return password


def set_disabled(email: str, disabled: bool) -> None:
    result = platform_db()["users"].update_one({"email": _norm_email(email)}, {"$set": {"disabled": disabled}})
    if not result.matched_count:
        raise ValueError(f"No user {email!r}")


def verify_login(email: str, password: str) -> Optional[Dict[str, Any]]:
    """Return the user (without hash) if the credentials are right, else None.

    The reason for a failure is never revealed. After MAX_FAILED_LOGINS wrong passwords
    the account is locked for LOCKOUT, whatever the next password is.
    """
    try:
        email = _norm_email(email)
    except ValueError:
        email = ""
    users = platform_db()["users"]
    user = users.find_one({"email": email}) if email else None

    if not user:
        try:
            _hasher.verify(_DUMMY_HASH, password or "")
        except (VerifyMismatchError, InvalidHashError):
            pass
        return None

    locked = user.get("locked_until")
    if locked and locked.replace(tzinfo=timezone.utc) > _now():
        return None

    try:
        _hasher.verify(user["password_hash"], password or "")
    except (VerifyMismatchError, InvalidHashError):
        failures = user.get("failed_logins", 0) + 1
        update: Dict[str, Any] = {"failed_logins": failures}
        if failures >= MAX_FAILED_LOGINS:
            update.update(locked_until=_now() + LOCKOUT, failed_logins=0)
        users.update_one({"_id": user["_id"]}, {"$set": update})
        return None

    tenant = platform_db()["tenants"].find_one({"slug": user["tenant"]})
    if user.get("disabled") or not tenant or tenant.get("status") != "active":
        return None

    users.update_one({"_id": user["_id"]}, {"$set": {"failed_logins": 0, "locked_until": None, "last_login": _now()}})
    user.pop("password_hash", None)
    user["tenant_name"] = tenant["name"]
    return user


def change_password(email: str, old: str, new: str) -> bool:
    if len(new) < 10:
        raise ValueError("New password must be at least 10 characters")
    if not verify_login(email, old):
        return False
    platform_db()["users"].update_one(
        {"email": _norm_email(email)}, {"$set": {"password_hash": _hasher.hash(new), "must_change_password": False}}
    )
    return True
