"""The only place that opens MongoDB connections.

Layout: one `platform` database (tenant registry and users) plus one database per
tenant. Everything else gets its database through `tenant_db(slug)`, so no query can
cross from one company's data into another's.
"""

import os
import re
from functools import lru_cache
from typing import Optional

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.database import Database

load_dotenv()

# Lowercase letters, digits and inner hyphens, 2-30 chars. No underscores, so a slug maps to
# exactly one database name.
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])$")


class TenantError(Exception):
    """Unknown, inactive or invalid tenant."""


@lru_cache(maxsize=1)
def client() -> MongoClient:
    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise RuntimeError("MONGODB_URI is not set (see .env.example)")
    return MongoClient(uri)


def reset_client() -> None:
    """Drop the cached connection (used by tests after changing the environment)."""
    if client.cache_info().currsize:
        client().close()
    client.cache_clear()


def platform_db() -> Database:
    return client()[os.getenv("PLATFORM_DB", "platform")]


def validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise TenantError(f"Invalid tenant slug {slug!r}: use 2-30 lowercase letters, digits or hyphens")
    return slug


def tenant_db_name(slug: str) -> str:
    return os.getenv("TENANT_DB_PREFIX", "tenant_") + validate_slug(slug).replace("-", "_")


def tenant_db(slug: str) -> Database:
    """Database for an existing, active tenant.

    Looks the tenant up first: MongoDB creates databases on first write, so without this
    check a typo in a slug would silently create and fill a stray database.
    """
    validate_slug(slug)
    tenant: Optional[dict] = platform_db()["tenants"].find_one({"slug": slug})
    if not tenant:
        raise TenantError(f"Unknown tenant {slug!r}")
    if tenant.get("status") != "active":
        raise TenantError(f"Tenant {slug!r} is not active")
    return client()[tenant_db_name(slug)]
