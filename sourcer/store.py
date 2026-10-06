"""MongoDB persistence. Re-running a role refreshes data but keeps outreach state."""

import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne
from pymongo.collection import Collection

STATUSES = ("new", "contacted", "replied", "interviewing", "rejected", "hired")

load_dotenv()


def get_collection() -> Collection:
    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise RuntimeError("MONGODB_URI is not set (see .env.example), or use --dry-run")
    db = MongoClient(uri)[os.getenv("MONGODB_DB", "talent_sourcer")]
    return db["candidates"]


def save_candidates(collection: Collection, role_name: str, rows: Iterable[Dict[str, Any]]) -> int:
    """Upsert rows. `status`/`notes` are only set on first insert."""
    now = datetime.now(timezone.utc)
    ops = [
        UpdateOne(
            {"_id": f"{role_name}:{row['login']}"},
            {
                # None values are skipped so a re-run can't wipe links found by `contacts`.
                "$set": {**{k: v for k, v in row.items() if v is not None}, "role": role_name, "updated_at": now},
                "$setOnInsert": {"status": "new", "notes": "", "first_seen": now},
            },
            upsert=True,
        )
        for row in rows
    ]
    if not ops:
        return 0
    collection.bulk_write(ops)
    return len(ops)


def find_candidates(
    collection: Collection,
    role_name: str,
    status: Optional[str] = None,
    min_score: float = 0,
    limit: int = 0,
) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {"role": role_name, "score": {"$gte": min_score}}
    if status:
        query["status"] = status
    cursor = collection.find(query).sort("score", -1)
    return list(cursor.limit(limit) if limit else cursor)


def set_status(collection: Collection, role_name: str, login: str, status: str, note: str = "") -> bool:
    update: Dict[str, Any] = {"$set": {"status": status, "updated_at": datetime.now(timezone.utc)}}
    if note:
        update["$set"]["notes"] = note
    result = collection.update_one({"_id": f"{role_name}:{login}"}, update)
    return result.matched_count == 1


def update_fields(collection: Collection, role_name: str, login: str, fields: Dict[str, Any]) -> None:
    fields = {**fields, "updated_at": datetime.now(timezone.utc)}
    collection.update_one({"_id": f"{role_name}:{login}"}, {"$set": fields})


def prune_stale(collection: Collection, role_name: str, before: datetime) -> int:
    """Delete untouched, not-yet-contacted candidates that a full run no longer returned."""
    result = collection.delete_many({"role": role_name, "status": "new", "updated_at": {"$lt": before}})
    return result.deleted_count
