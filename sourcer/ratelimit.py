"""Small fixed-window rate limiter backed by MongoDB (shared by every API process)."""

import hashlib
from datetime import datetime, timedelta, timezone

from sourcer.tenancy.db import platform_db


def allow(key: str, limit: int, window_seconds: int) -> bool:
    """Count one hit for `key`; False once more than `limit` hits fall in the current window.

    The key is hashed, so an IP address or token never sits in the database in the clear.
    """
    now = datetime.now(timezone.utc)
    bucket = int(now.timestamp()) // window_seconds
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    col = platform_db()["rate_limits"]
    col.create_index("expires_at", expireAfterSeconds=0)  # old windows delete themselves
    doc = col.find_one_and_update(
        {"_id": f"{digest}:{bucket}"},
        {"$inc": {"n": 1}, "$setOnInsert": {"expires_at": now + timedelta(seconds=window_seconds * 2)}},
        upsert=True,
        return_document=True,
    )
    return doc["n"] <= limit


def over(key: str, limit: int, window_seconds: int) -> bool:
    """True once `key` has used up its `limit` hits in the current window. Counts nothing itself (see `allow`)."""
    bucket = int(datetime.now(timezone.utc).timestamp()) // window_seconds
    digest = hashlib.sha256(key.encode()).hexdigest()[:32]
    doc = platform_db()["rate_limits"].find_one({"_id": f"{digest}:{bucket}"})
    return bool(doc) and doc["n"] >= limit
