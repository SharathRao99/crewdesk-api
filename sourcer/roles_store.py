"""Roles stored per tenant (the YAML profile, as a document)."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import yaml
from pymongo.database import Database

from sourcer.role import Role, role_from_dict


def _now() -> datetime:
    return datetime.now(timezone.utc)


def import_role(db: Database, data: Dict[str, Any]) -> Role:
    """Validate and upsert a role. Keeps its default template when re-imported."""
    role = role_from_dict(data)
    db["roles"].update_one(
        {"_id": role.name},
        {"$set": {"name": role.name, "data": data, "updated_at": _now()},
         "$setOnInsert": {"default_template_id": None, "created_at": _now()}},
        upsert=True,
    )
    return role


def import_role_file(db: Database, path: str) -> Role:
    with open(path) as f:
        return import_role(db, yaml.safe_load(f))


def get_role(db: Database, name: str) -> Tuple[Role, Dict[str, Any]]:
    doc = db["roles"].find_one({"_id": name})
    if not doc:
        raise KeyError(name)
    return role_from_dict(doc["data"]), doc


def list_roles(db: Database) -> List[Dict[str, Any]]:
    return list(db["roles"].find({}).sort("name", 1))


def set_default_template(db: Database, name: str, template_id: str | None) -> bool:
    return db["roles"].update_one({"_id": name}, {"$set": {"default_template_id": template_id}}).matched_count == 1
