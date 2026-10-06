"""Generate a self-contained HTML report for HR: no server, no database."""

import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

from sourcer.enrich import candidate_from_row
from sourcer.export import has_useful_link
from sourcer.outreach import DEFAULT_OUTREACH
from sourcer.role import Role
from sourcer.scoring import explain

TEMPLATE = Path(__file__).parent / "static" / "report_template.html"


def _row(r: Dict[str, Any], role: Role) -> Dict[str, Any]:
    emails = list(dict.fromkeys([e for e in [r.get("email"), *(r.get("portfolio_emails") or [])] if e]))
    return {
        "login": r["login"],
        "name": r.get("name") or r["login"],
        "company": r.get("company"),
        "github": r["url"],
        "score": r["score"],
        "location": r.get("location"),
        "years": r.get("est_years"),
        "last_active": r.get("last_active"),
        "emails": emails,
        "phones": r.get("portfolio_phones") or [],
        "linkedin": r.get("linkedin_url"),
        "portfolio": r.get("portfolio_url") or r.get("blog"),
        "resumes": r.get("resume_links") or [],
        "repos": r.get("matching_repos") or [],
        "bio": r.get("bio"),
        "followers": r.get("followers", 0),
        "public_repos": r.get("public_repos", 0),
        "hireable": bool(r.get("hireable")),
        "why": explain(candidate_from_row(r), role),
    }


def build_report(role: Role, candidates: List[Dict[str, Any]], path: str) -> int:
    """Write the report to `path`; returns the number of candidates included."""
    rows = [_row(r, role) for r in candidates if has_useful_link(r)]
    payload = {
        "role": role.name,
        "generated": date.today().isoformat(),
        "role_summary": {
            "languages": role.languages,
            "keywords": role.keywords,
            "experience": [role.min_years, role.max_years],
            "locations": role.locations,
        },
        "outreach": {**DEFAULT_OUTREACH, **role.outreach},
        "candidates": rows,
    }
    # "</" would end the <script> block early; escape it.
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text().replace("__DATA__", data)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(html)
    return len(rows)
