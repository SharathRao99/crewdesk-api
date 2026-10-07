"""The candidate screening form: signed links, answers, budget verdicts and data removal.

A link looks like `<company>.<link id>.<signature>`. The signature is an HMAC of the first two
parts with a key derived from SECRETS_KEY, so nothing secret is stored: the same link can be
shown again, any change to it makes it invalid, and a link can be cancelled or expire on the
server. A link works once.
"""

import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from bson import ObjectId
from bson.errors import InvalidId
from pymongo.database import Database

from sourcer.tenancy.db import TenantError, SLUG_RE, tenant_db

LINK_DAYS = int(os.getenv("SCREENING_LINK_DAYS", "14"))
CONSENT_VERSION = "2026-10"
NOTICE_CHOICES = (0, 15, 30, 45, 60, 90)  # days offered on the form; any 0-365 is accepted
MAX_CTC = 500.0  # lakh per annum


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)  # MongoDB returns naive UTC


def web_base_url() -> str:
    return os.getenv("PUBLIC_WEB_URL", "http://localhost:3100").rstrip("/")


# ---------- tokens ----------

def _key() -> bytes:
    secret = os.getenv("SECRETS_KEY")
    if not secret:
        raise RuntimeError("SECRETS_KEY is not set")
    return hashlib.sha256(b"screening-link|" + secret.encode()).digest()


def _sign(message: str) -> str:
    return base64.urlsafe_b64encode(hmac.new(_key(), message.encode(), hashlib.sha256).digest()).decode().rstrip("=")


def make_token(tenant: str, link_id: Any) -> str:
    body = f"{tenant}.{link_id}"
    return f"{body}.{_sign(body)}"


def parse_token(token: str) -> Optional[Tuple[str, ObjectId]]:
    """(company, link id) if the token is well formed and correctly signed, else None."""
    parts = (token or "").split(".")
    if len(parts) != 3 or len(token) > 200:
        return None
    slug, link_id, signature = parts
    if not SLUG_RE.match(slug) or not hmac.compare_digest(signature, _sign(f"{slug}.{link_id}")):
        return None
    try:
        return slug, ObjectId(link_id)
    except InvalidId:
        return None


def url_for(tenant: str, link: Dict[str, Any]) -> str:
    return f"{web_base_url()}/s/{make_token(tenant, link['_id'])}"


# ---------- links ----------

def link_state(link: Dict[str, Any], now: Optional[datetime] = None) -> str:
    """open, answered, expired or revoked."""
    now = now or _now()
    if link.get("revoked"):
        return "revoked"
    if link.get("used_at"):
        return "answered"
    if _aware(link["expires_at"]) <= now:
        return "expired"
    return "open"


def _active_link(db: Database, candidate_id: str, now: datetime) -> Optional[Dict[str, Any]]:
    return db["screening_links"].find_one(
        {"candidate_id": candidate_id, "revoked": False, "used_at": None, "expires_at": {"$gt": now}}
    )


def get_or_create_link(db: Database, candidate: Dict[str, Any], by: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The candidate's open link, creating one if there is none (so drafts reuse the same URL)."""
    now = now or _now()
    existing = _active_link(db, candidate["_id"], now)
    if existing:
        return existing
    link = {
        "candidate_id": candidate["_id"], "role": candidate["role"], "login": candidate["login"],
        "created_by": by, "created_at": now, "expires_at": now + timedelta(days=LINK_DAYS),
        "used_at": None, "opened_at": None, "revoked": False,
    }
    db["screening_links"].insert_one(link)
    return link


def regenerate_link(db: Database, candidate: Dict[str, Any], by: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Cancel any open link and issue a new one."""
    db["screening_links"].update_many({"candidate_id": candidate["_id"], "used_at": None, "revoked": False}, {"$set": {"revoked": True}})
    return get_or_create_link(db, candidate, by, now)


def latest_link(db: Database, candidate_id: str) -> Optional[Dict[str, Any]]:
    return db["screening_links"].find_one({"candidate_id": candidate_id, "revoked": False}, sort=[("created_at", -1)])


# ---------- public side ----------

def resolve(token: str) -> Optional[Tuple[Database, str, Dict[str, Any], Dict[str, Any]]]:
    """(database, company slug, link, candidate) for a valid token, else None. Never says why it failed."""
    parsed = parse_token(token)
    if not parsed:
        return None
    slug, link_id = parsed
    try:
        db = tenant_db(slug)
    except TenantError:
        return None
    link = db["screening_links"].find_one({"_id": link_id})
    if not link or link.get("revoked"):
        return None
    candidate = db["candidates"].find_one({"_id": link["candidate_id"]})
    return (db, slug, link, candidate) if candidate else None


class SubmitError(Exception):
    def __init__(self, state: str):
        super().__init__(state)
        self.state = state  # answered | expired


def submit(db: Database, link: Dict[str, Any], candidate: Dict[str, Any], answers: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Record the answers. The link is marked used first, in one atomic step, so two submissions cannot both win."""
    now = now or _now()
    claimed = db["screening_links"].find_one_and_update(
        {"_id": link["_id"], "used_at": None, "revoked": False, "expires_at": {"$gt": now}}, {"$set": {"used_at": now}}
    )
    if not claimed:
        raise SubmitError(link_state(db["screening_links"].find_one({"_id": link["_id"]}) or link, now))

    response = {
        "link_id": link["_id"], "candidate_id": candidate["_id"], "role": candidate["role"], "login": candidate["login"],
        "current_ctc": answers["current_ctc"], "expected_ctc": answers["expected_ctc"], "notice_days": answers["notice_days"],
        "comment": (answers.get("comment") or "").strip() or None, "consent": True, "consent_version": CONSENT_VERSION,
        "submitted_at": now,
    }
    db["screening_responses"].insert_one(response)

    update: Dict[str, Any] = {"screening": {k: response[k] for k in ("current_ctc", "expected_ctc", "notice_days")} | {"answered_at": now}, "updated_at": now}
    old_status = candidate.get("status", "new")
    if old_status in ("new", "contacted"):
        update["status"] = "replied"
        db["candidate_events"].insert_one(
            {"type": "status", "from_status": old_status, "to_status": "replied", "note": "Answered the screening form",
             "candidate_id": candidate["_id"], "role": candidate["role"], "login": candidate["login"], "by": "candidate (form)", "at": now}
        )
    db["candidates"].update_one({"_id": candidate["_id"]}, {"$set": update})
    return response


# ---------- budget ----------

def validate_budget(minimum: Optional[float], maximum: float, stretch_max: Optional[float]) -> None:
    if not 0 < maximum <= MAX_CTC:
        raise ValueError(f"The maximum must be between 0 and {int(MAX_CTC)} lakh")
    if minimum is not None and not 0 <= minimum <= maximum:
        raise ValueError("The minimum cannot be above the maximum")
    if stretch_max is not None and not maximum <= stretch_max <= MAX_CTC:
        raise ValueError("The stretch limit must be at or above the maximum")


def verdict(expected_ctc: Optional[float], budget: Optional[Dict[str, Any]]) -> Optional[str]:
    """within / stretch / over, judged on what the candidate expects. None if either side is unknown."""
    if expected_ctc is None or not budget or not budget.get("max"):
        return None
    if expected_ctc <= budget["max"]:
        return "within"
    if budget.get("stretch_max") and expected_ctc <= budget["stretch_max"]:
        return "stretch"
    return "over"


def budget_query(role_budget: Optional[Dict[str, Any]], wanted: str) -> Optional[Dict[str, Any]]:
    """A MongoDB condition on the stored summary matching candidates with the wanted verdict."""
    if not role_budget or not role_budget.get("max"):
        return None
    top, stretch = role_budget["max"], role_budget.get("stretch_max")
    field = "screening.expected_ctc"
    if wanted == "within":
        return {field: {"$lte": top}}
    if wanted == "stretch":
        return {field: {"$gt": top, "$lte": stretch}} if stretch else {"_id": {"$in": []}}
    if wanted == "over":
        return {field: {"$gt": stretch or top}}
    return None


# ---------- removing data ----------

def delete_candidate(db: Database, role: str, login: str) -> Dict[str, int]:
    """Remove a candidate and everything held about them."""
    cid = f"{role}:{login}"
    return {
        "candidates": db["candidates"].delete_one({"_id": cid}).deleted_count,
        "events": db["candidate_events"].delete_many({"candidate_id": cid}).deleted_count,
        "links": db["screening_links"].delete_many({"candidate_id": cid}).deleted_count,
        "responses": db["screening_responses"].delete_many({"candidate_id": cid}).deleted_count,
    }


def purge_old(db: Database, days: int, now: Optional[datetime] = None) -> Dict[str, int]:
    """Delete form answers and links older than `days`, and clear the answer summary on those candidates."""
    cutoff = (now or _now()) - timedelta(days=days)
    old = list(db["screening_responses"].find({"submitted_at": {"$lt": cutoff}}, {"candidate_id": 1}))
    ids = [r["candidate_id"] for r in old]
    responses = db["screening_responses"].delete_many({"submitted_at": {"$lt": cutoff}}).deleted_count
    if ids:
        db["candidates"].update_many({"_id": {"$in": ids}}, {"$unset": {"screening": ""}})
    links = db["screening_links"].delete_many({"created_at": {"$lt": cutoff}}).deleted_count
    return {"responses": responses, "links": links}
