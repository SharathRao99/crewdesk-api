"""Roles stored per tenant (the YAML profile, as a document)."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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
    apply_preferred(db, role.name)
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


def set_budget(db: Database, name: str, minimum: float | None, maximum: float, stretch_max: float | None) -> bool:
    """Set what the company can pay for a role, in lakh per annum. Verdicts on answers follow it."""
    from sourcer.screening import validate_budget

    validate_budget(minimum, maximum, stretch_max)
    budget = {"min": minimum, "max": maximum, "stretch_max": stretch_max}
    return db["roles"].update_one({"_id": name}, {"$set": {"budget": budget, "updated_at": _now()}}).matched_count == 1


def clear_budget(db: Database, name: str) -> bool:
    return db["roles"].update_one({"_id": name}, {"$unset": {"budget": ""}}).matched_count == 1


# ---------- creating and editing roles (the role builder) ----------

import re

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$")  # used in URLs and in candidate ids, so it stays simple
LIMITS = {"languages": (1, 5), "keywords": (0, 15), "locations": (1, 30), "exclude_locations": (0, 30), "preferred_locations": (0, 10), "manifest_contains": (0, 5)}


class RoleExists(Exception):
    pass


def _clean_list(name: str, values: List[str]) -> List[str]:
    seen, out = set(), []
    for raw in values:
        value = " ".join((raw or "").split())
        if not value or value.lower() in seen:
            continue
        if len(value) > 60:
            raise ValueError(f"'{value[:20]}...' is too long (60 characters at most)")
        seen.add(value.lower())
        out.append(value)
    low, high = LIMITS[name]
    if not low <= len(out) <= high:
        label = name.replace("_", " ")
        raise ValueError(f"Give between {low} and {high} {label}" if low else f"At most {high} {label}")
    return out


def definition_to_data(defn: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Validate the builder's fields and turn them into the stored profile.

    Anything the builder does not edit (score weights, other email defaults) is kept from `existing`.
    """
    name = (defn.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("The name must be 3-50 characters: lowercase letters, digits and hyphens, for example laravel-dev-pune")
    low, high = defn["min_years"], defn["max_years"]
    if not 0 <= low <= high <= 50:
        raise ValueError("Experience must run from a lower number up to a higher one, within 0 to 50 years")
    if not 0 <= defn["min_language_share"] <= 1:
        raise ValueError("The share of repos in the main language must be between 0 and 100%")
    if not (0 <= defn["min_public_repos"] <= 100 and 0 <= defn["min_keyword_repos"] <= 20 and 1 <= defn["active_within_months"] <= 60):
        raise ValueError("A number is outside its allowed range")

    data = dict(existing or {})
    data.update(
        name=name,
        languages=_clean_list("languages", defn["languages"]),
        keywords=[k.lower() for k in _clean_list("keywords", defn["keywords"])],
        locations=_clean_list("locations", defn["locations"]),
        exclude_locations=_clean_list("exclude_locations", defn.get("exclude_locations") or []),
        preferred_locations=_clean_list("preferred_locations", defn.get("preferred_locations") or []),
        experience={"min_years": low, "max_years": high},
        signals={"min_public_repos": defn["min_public_repos"], "active_within_months": defn["active_within_months"]},
    )
    primary: Dict[str, Any] = {"min_language_share": defn["min_language_share"], "min_keyword_repos": defn["min_keyword_repos"]}
    manifest_file = (defn.get("manifest_file") or "").strip()
    contains = _clean_list("manifest_contains", defn.get("manifest_contains") or [])
    if manifest_file and contains:
        primary["manifest"] = {"file": manifest_file[:60], "contains": contains}
    data["primary_skill"] = primary
    title = (defn.get("role_title") or "").strip()
    if title:
        data["outreach"] = {**(data.get("outreach") or {}), "role_title": title[:80]}
    elif data.get("outreach"):
        data["outreach"] = {k: v for k, v in data["outreach"].items() if k != "role_title"} or None
        if data["outreach"] is None:
            data.pop("outreach")
    role_from_dict(data)  # the same check the worker will make
    return data


def data_to_definition(data: Dict[str, Any]) -> Dict[str, Any]:
    role = role_from_dict(data)
    return {
        "name": role.name, "languages": role.languages, "keywords": role.keywords, "locations": role.locations,
        "exclude_locations": role.exclude_locations, "preferred_locations": role.preferred_locations, "min_years": role.min_years, "max_years": role.max_years,
        "min_public_repos": role.min_public_repos, "active_within_months": role.active_within_months,
        "min_language_share": role.min_language_share, "min_keyword_repos": role.min_keyword_repos,
        "manifest_file": role.manifest_file, "manifest_contains": role.manifest_contains,
        "role_title": role.outreach.get("role_title"),
    }


def create_role(db: Database, defn: Dict[str, Any]) -> Dict[str, Any]:
    data = definition_to_data(defn)
    if db["roles"].find_one({"_id": data["name"]}):
        raise RoleExists(data["name"])
    db["roles"].insert_one({"_id": data["name"], "name": data["name"], "data": data, "default_template_id": None,
                            "created_at": _now(), "updated_at": _now()})
    return db["roles"].find_one({"_id": data["name"]})


def update_role(db: Database, name: str, defn: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    doc = db["roles"].find_one({"_id": name})
    if not doc:
        return None
    data = definition_to_data({**defn, "name": name}, existing=doc["data"])
    db["roles"].update_one({"_id": name}, {"$set": {"data": data, "updated_at": _now()}})
    apply_preferred(db, name)  # people already found are re-flagged, so the list order follows the new setting
    return db["roles"].find_one({"_id": name})


def apply_preferred(db: Database, name: str) -> int:
    """Set `preferred` on every candidate of a role from the role's preferred places. Returns how many are preferred.

    The list sorts on this stored flag, so changing the setting has to re-flag the people already found.
    """
    doc = db["roles"].find_one({"_id": name})
    if not doc:
        return 0
    terms = [t for t in role_from_dict(doc["data"]).preferred_locations if t.strip()]
    if terms:
        expr = {"$regexMatch": {"input": {"$ifNull": ["$location", ""]}, "regex": "|".join(re.escape(t) for t in terms), "options": "i"}}
    else:
        expr = False
    db["candidates"].update_many({"role": name}, [{"$set": {"preferred": expr}}])
    return db["candidates"].count_documents({"role": name, "preferred": True})
