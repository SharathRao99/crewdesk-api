"""Fetch a candidate's public profile and repos, and derive scoring features."""

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from sourcer.github import GitHubClient
from sourcer.portfolio import LINKEDIN_RE
from sourcer.role import Role

logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    login: str
    name: Optional[str]
    url: str
    location: Optional[str]
    bio: Optional[str]
    email: Optional[str]
    blog: Optional[str]
    twitter: Optional[str]
    hireable: Optional[bool]
    followers: int
    public_repos: int
    est_years: Optional[float]
    last_active: Optional[str]
    language_repos: int
    keyword_repos: int
    matching_repos: List[str] = field(default_factory=list)
    company: Optional[str] = None
    linkedin_url: Optional[str] = None
    language_share: float = 0.0


def _parse(ts: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None


def location_ok(location: Optional[str], role: Role) -> bool:
    """True if the free-text location matches a target and none of the excluded places
    (city names are shared across countries, e.g. "Hyderabad, Pakistan")."""
    if not location:
        return False
    lowered = location.lower()
    if any(place.lower() in lowered for place in role.exclude_locations):
        return False
    return any(loc.lower() in lowered for loc in role.locations)


def _repo_matches_keyword(repo: Dict[str, Any], keywords: List[str]) -> bool:
    haystack = " ".join(
        [repo.get("name") or "", repo.get("description") or "", " ".join(repo.get("topics") or [])]
    ).lower()
    return any(keyword in haystack for keyword in keywords)




def find_linkedin(client: GitHubClient, login: str, profile: Dict[str, Any]) -> Optional[str]:
    """LinkedIn URL the person published themselves: GitHub social accounts, then bio/blog."""
    for account in client.get(f"/users/{login}/social_accounts") or []:
        if account.get("provider") == "linkedin" and account.get("url"):
            return account["url"]
    for text in (profile.get("blog"), profile.get("bio")):
        match = LINKEDIN_RE.search(text or "")
        if match:
            return match.group(0)
    return None


def _manifest_hits(client: GitHubClient, login: str, repos: List[Dict[str, Any]], role: Role, limit: int = 3) -> List[str]:
    """Repos whose dependency manifest (e.g. composer.json) contains a role hint."""
    hits = []
    for repo in repos[:limit]:
        try:
            data = client.get(f"/repos/{login}/{repo['name']}/contents/{role.manifest_file}")
        except httpx.HTTPStatusError:  # e.g. 403 on a blocked repo: skip it, keep the candidate
            continue
        if not data or not data.get("content"):
            continue
        text = base64.b64decode(data["content"]).decode("utf-8", errors="ignore").lower()
        if any(hint in text for hint in role.manifest_contains):
            hits.append(repo["name"])
    return hits


def enrich(client: GitHubClient, role: Role, login: str) -> Optional[Candidate]:
    """Return a Candidate, or None if the profile is missing or outside the role's locations."""
    profile = client.get(f"/users/{login}")
    if not profile or profile.get("type") != "User":
        return None
    if not location_ok(profile.get("location"), role):
        return None

    repos = client.get(
        f"/users/{login}/repos", {"per_page": 100, "sort": "pushed", "type": "owner"}
    ) or []
    own_repos = [r for r in repos if not r.get("fork")]

    languages = {lang.lower() for lang in role.languages}
    language_repos = [r for r in own_repos if (r.get("language") or "").lower() in languages]
    keyword_repos = [r for r in own_repos if _repo_matches_keyword(r, role.keywords)]

    if role.manifest_file and len(keyword_repos) < role.min_keyword_repos:
        names = {r["name"] for r in keyword_repos}
        unchecked = [r for r in language_repos if r["name"] not in names]
        keyword_repos += [r for r in unchecked if r["name"] in _manifest_hits(client, login, unchecked, role)]

    # Experience hint: age of the first public repo in a role language, falling
    # back to account age if there is none.
    created_dates = [d for d in (_parse(r.get("created_at")) for r in language_repos) if d]
    start = min(created_dates) if created_dates else _parse(profile.get("created_at"))
    now = datetime.now(timezone.utc)
    est_years = round((now - start).days / 365.25, 1) if start else None

    pushed = [d for d in (_parse(r.get("pushed_at")) for r in own_repos) if d]
    last_active = max(pushed).date().isoformat() if pushed else None

    return Candidate(
        login=login,
        name=profile.get("name"),
        url=profile["html_url"],
        location=profile.get("location"),
        bio=profile.get("bio"),
        email=profile.get("email"),
        blog=profile.get("blog") or None,
        twitter=profile.get("twitter_username"),
        hireable=profile.get("hireable"),
        followers=profile.get("followers", 0),
        public_repos=profile.get("public_repos", 0),
        est_years=est_years,
        last_active=last_active,
        language_repos=len(language_repos),
        keyword_repos=len(keyword_repos),
        matching_repos=[r["name"] for r in keyword_repos[:5]],
        company=profile.get("company"),
        linkedin_url=find_linkedin(client, login, profile),
        language_share=round(len(language_repos) / len(own_repos), 2) if own_repos else 0.0,
    )


def candidate_from_row(row: Dict[str, Any]) -> Candidate:
    """Rebuild a Candidate from a stored MongoDB document (extra keys are ignored)."""
    from dataclasses import fields

    values = {f.name: row[f.name] for f in fields(Candidate) if row.get(f.name) is not None}
    values.setdefault("followers", 0)
    values.setdefault("public_repos", 0)
    values.setdefault("language_repos", 0)
    values.setdefault("keyword_repos", 0)
    for required in ("name", "location", "bio", "email", "blog", "twitter", "hireable", "est_years", "last_active"):
        values.setdefault(required, None)
    values.setdefault("url", f"https://github.com/{row['login']}")
    return Candidate(**values)
