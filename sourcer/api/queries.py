"""Query building and mapping shared by the candidate list, the detail view and the export."""

import re
from typing import Any, Dict, List, Optional

from pymongo.database import Database

from sourcer import roles_store, screening, templates_store
from sourcer.api.models import CandidateDetail, CandidateListItem, Explanation, ScreeningSummary
from sourcer.enrich import candidate_from_row
from sourcer.outreach import DEFAULT_OUTREACH, best_email
from sourcer.scoring import explain

SORTS = {"score": "score", "name": "name", "est_years": "est_years", "last_active": "last_active", "status": "status", "expected_ctc": "screening.expected_ctc"}


def _contains(text: str) -> Dict[str, Any]:
    return {"$regex": re.escape(text), "$options": "i"}  # escaped: user input is never a pattern


def build_query(
    role: Optional[str] = None,
    status: Optional[str] = None,
    min_score: float = 0,
    q: Optional[str] = None,
    location: Optional[str] = None,
    has_email: bool = False,
    has_phone: bool = False,
    has_linkedin: bool = False,
    has_portfolio: bool = False,
    answered: Optional[bool] = None,
    budget: Optional[str] = None,
    role_budget: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    clauses: List[Dict[str, Any]] = [{"score": {"$gte": min_score}}]
    if role:
        clauses.append({"role": role})
    if status:
        clauses.append({"status": status})
    if location:
        clauses.append({"location": _contains(location.strip())})
    if q and q.strip():
        text = _contains(q.strip()[:100])
        clauses.append({"$or": [{f: text} for f in ("login", "name", "location", "bio", "company")]})
    if has_email:
        clauses.append({"$or": [{"email": {"$nin": [None, ""]}}, {"portfolio_emails.0": {"$exists": True}}]})
    if has_phone:
        clauses.append({"portfolio_phones.0": {"$exists": True}})
    if has_linkedin:
        clauses.append({"linkedin_url": {"$nin": [None, ""]}})
    if has_portfolio:
        clauses.append({"$or": [{"portfolio_url": {"$nin": [None, ""]}}, {"blog": {"$nin": [None, ""]}}]})
    if answered is True:
        clauses.append({"screening.answered_at": {"$exists": True}})
    elif answered is False:
        clauses.append({"screening.answered_at": {"$exists": False}})
    if budget:
        condition = screening.budget_query(role_budget, budget)
        clauses.append(condition if condition else {"_id": {"$in": []}})  # no budget set: nothing can match a verdict
    return {"$and": clauses}


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def list_item(row: Dict[str, Any], budget: Optional[Dict[str, Any]] = None) -> CandidateListItem:
    summary = row.get("screening")
    return CandidateListItem(
        screening=ScreeningSummary(
            current_ctc=summary["current_ctc"], expected_ctc=summary["expected_ctc"], notice_days=summary["notice_days"],
            answered_at=_iso(summary["answered_at"]) or "",
        ) if summary else None,
        budget_verdict=screening.verdict(summary["expected_ctc"], budget) if summary else None,
        role=row["role"], login=row["login"], name=row.get("name"), company=row.get("company"),
        location=row.get("location"), score=row.get("score", 0), est_years=row.get("est_years"),
        last_active=_iso(row.get("last_active")), status=row.get("status", "new"), email=best_email(row),
        phone=(row.get("portfolio_phones") or [None])[0], linkedin_url=row.get("linkedin_url"),
        portfolio_url=row.get("portfolio_url") or row.get("blog") or None,
        resume_url=(row.get("resume_links") or [None])[0], github_url=row.get("url") or f"https://github.com/{row['login']}",
        repos=(row.get("matching_repos") or [])[:5], hireable=bool(row.get("hireable")), preferred=bool(row.get("preferred")),
    )


def role_budget(db: Database, role_name: Optional[str]) -> Optional[Dict[str, Any]]:
    if not role_name:
        return None
    doc = db["roles"].find_one({"_id": role_name}, {"budget": 1})
    return (doc or {}).get("budget")


def detail(db: Database, row: Dict[str, Any]) -> CandidateDetail:
    why = None
    try:
        role, _ = roles_store.get_role(db, row["role"])
        why = Explanation(**explain(candidate_from_row(row), role))
    except KeyError:
        pass  # the role was removed; show the candidate without the breakdown
    emails = list(dict.fromkeys(e for e in [row.get("email"), *(row.get("portfolio_emails") or [])] if e))
    return CandidateDetail(
        **list_item(row, role_budget(db, row['role'])).model_dump(), bio=row.get("bio"), followers=row.get("followers", 0),
        public_repos=row.get("public_repos", 0), emails=emails, phones=row.get("portfolio_phones") or [],
        resume_links=row.get("resume_links") or [], notes=row.get("notes") or "",
        first_seen=_iso(row.get("first_seen")), updated_at=_iso(row.get("updated_at")), why=why,
    )


def resolve_outreach(db: Database, role_name: str, template_id: Optional[str]) -> Dict[str, str]:
    """The {role_title, subject, body, name} to use: the chosen template, else the role's default, else the YAML text."""
    role_title, default_id, yaml_outreach = role_name, None, {}
    try:
        role, doc = roles_store.get_role(db, role_name)
        yaml_outreach, default_id = role.outreach, doc.get("default_template_id")
        role_title = yaml_outreach.get("role_title") or role.name
    except KeyError:
        pass
    template = templates_store.get(db, template_id) if template_id else None
    if not template and default_id:
        template = templates_store.get(db, default_id)
    if template:
        return {"role_title": role_title, "subject": template["subject"], "body": template["body"], "name": template["name"]}
    base = {**DEFAULT_OUTREACH, **yaml_outreach}
    return {"role_title": role_title, "subject": base["subject"], "body": base["body"], "name": "Role default"}
