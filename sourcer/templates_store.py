"""Saved email templates per tenant. Editing keeps the previous versions."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.database import Database

from sourcer.outreach import unknown_placeholders

MAX_SUBJECT = 200
MAX_BODY = 10_000


class TemplateError(ValueError):
    """The template text is not acceptable."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate(name: str, subject: str, body: str) -> None:
    if not name.strip():
        raise TemplateError("Give the template a name")
    if not subject.strip() or not body.strip():
        raise TemplateError("Subject and body cannot be empty")
    if len(subject) > MAX_SUBJECT or len(body) > MAX_BODY:
        raise TemplateError(f"Subject is limited to {MAX_SUBJECT} characters and body to {MAX_BODY}")
    unknown = unknown_placeholders(subject, body)
    if unknown:
        raise TemplateError("Unknown placeholder(s): " + ", ".join("{" + u + "}" for u in unknown))


def _oid(template_id: str) -> Optional[ObjectId]:
    try:
        return ObjectId(template_id)
    except (InvalidId, TypeError):
        return None


def create(db: Database, name: str, subject: str, body: str, role: Optional[str], by: str) -> Dict[str, Any]:
    validate(name, subject, body)
    doc = {
        "name": name.strip(), "subject": subject, "body": body, "role": role or None,
        "version": 1, "versions": [], "archived": False, "created_by": by,
        "created_at": _now(), "updated_at": _now(),
    }
    db["email_templates"].insert_one(doc)
    return doc


def get(db: Database, template_id: str) -> Optional[Dict[str, Any]]:
    oid = _oid(template_id)
    return db["email_templates"].find_one({"_id": oid, "archived": False}) if oid else None


def list_templates(db: Database, role: Optional[str] = None) -> List[Dict[str, Any]]:
    """Templates for a role plus the ones not tied to any role."""
    query: Dict[str, Any] = {"archived": False}
    if role:
        query["role"] = {"$in": [role, None]}
    return list(db["email_templates"].find(query).sort("name", 1))


def update(db: Database, template_id: str, name: str, subject: str, body: str, role: Optional[str], by: str) -> Optional[Dict[str, Any]]:
    validate(name, subject, body)
    current = get(db, template_id)
    if not current:
        return None
    snapshot = {"version": current["version"], "subject": current["subject"], "body": current["body"],
                "at": current["updated_at"], "by": current.get("updated_by") or current["created_by"]}
    db["email_templates"].update_one(
        {"_id": current["_id"]},
        {"$set": {"name": name.strip(), "subject": subject, "body": body, "role": role or None,
                  "version": current["version"] + 1, "updated_at": _now(), "updated_by": by},
         "$push": {"versions": snapshot}},
    )
    return get(db, template_id)


def archive(db: Database, template_id: str) -> bool:
    oid = _oid(template_id)
    if not oid:
        return False
    result = db["email_templates"].update_one({"_id": oid, "archived": False}, {"$set": {"archived": True, "updated_at": _now()}})
    if result.matched_count:
        # A role must not keep pointing at a template that is gone.
        db["roles"].update_many({"default_template_id": template_id}, {"$set": {"default_template_id": None}})
    return bool(result.matched_count)
