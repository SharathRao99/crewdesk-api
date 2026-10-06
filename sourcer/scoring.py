"""Rule-based fit score (0-100). Weights come from the role profile."""

from datetime import date
from typing import Any, Dict, List, Optional

from sourcer.enrich import Candidate
from sourcer.role import Role


def score(candidate: Candidate, role: Role, today: Optional[date] = None) -> Dict[str, float]:
    """Return per-rule points and a `total`. Each rule scores 0..its weight."""
    today = today or date.today()
    w = role.weights

    # Keyword repos: full marks at 3+ matching repos.
    keywords = w["keywords"] * min(candidate.keyword_repos / 3, 1)

    # Language repos: full marks at 5+ repos in a role language.
    language = w["language"] * min(candidate.language_repos / 5, 1)

    # Experience: full inside [min, max], fading linearly to 0 over 2 years outside.
    experience = 0.0
    if candidate.est_years is not None:
        years = candidate.est_years
        if years < role.min_years:
            gap = role.min_years - years
        elif years > role.max_years:
            gap = years - role.max_years
        else:
            gap = 0
        experience = w["experience"] * max(1 - gap / 2, 0)

    # Activity: full if active inside the window, fading to 0 at twice the window.
    activity = 0.0
    if candidate.last_active:
        months_idle = (today - date.fromisoformat(candidate.last_active)).days / 30
        window = role.active_within_months
        if months_idle <= window:
            activity = w["activity"]
        elif months_idle < window * 2:
            activity = w["activity"] * (1 - (months_idle - window) / window)

    # Contact: a public email is worth most; hireable/blog/twitter add a little.
    contact = 0.0
    if candidate.email:
        contact += w["contact"] * 0.6
    if candidate.hireable:
        contact += w["contact"] * 0.2
    if candidate.blog or candidate.twitter:
        contact += w["contact"] * 0.2

    parts = {
        "keywords": keywords,
        "language": language,
        "experience": experience,
        "activity": activity,
        "contact": contact,
    }
    parts = {k: round(v, 1) for k, v in parts.items()}
    parts["total"] = round(sum(parts.values()), 1)
    return parts


def meets_primary_skill(candidate: Candidate, role: Role) -> bool:
    """True when the role's language/framework is demonstrably a main skill."""
    return (
        candidate.language_share >= role.min_language_share
        and candidate.keyword_repos >= role.min_keyword_repos
    )


def explain(candidate: Candidate, role: Role, today: Optional[date] = None) -> Dict[str, Any]:
    """Human-readable breakdown: pass/fail gates plus points earned per scoring rule."""
    today = today or date.today()
    c, w = candidate, role.weights
    parts = score(c, role, today)
    langs = "/".join(role.languages)
    framework = ", ".join(k.title() for k in role.keywords[:3]) or "the framework"

    def level(points: float, maximum: float) -> str:
        return "full" if points >= maximum * 0.99 else ("partial" if points > 0 else "none")

    gates: List[Dict[str, Any]] = [
        {
            "name": f"Based in a target location",
            "ok": True,
            "detail": c.location or "unknown",
        },
        {
            "name": f"{langs} is a main language",
            "ok": c.language_share >= role.min_language_share,
            "detail": f"{round(c.language_share * 100)}% of their own repos (needs {round(role.min_language_share * 100)}%)",
        },
        {
            "name": f"Uses {framework}",
            "ok": c.keyword_repos >= role.min_keyword_repos,
            "detail": f"{c.keyword_repos} repo(s) show it (needs {role.min_keyword_repos})",
        },
    ]

    years = f"~{c.est_years} years of public {langs} work" if c.est_years is not None else "unknown"
    idle = f"last pushed {c.last_active}" if c.last_active else "no recent pushes"
    contact_bits = [b for b, ok in (("public email", c.email), ("open to work", c.hireable), ("website", c.blog or c.twitter)) if ok]

    rules = [
        {"name": "Framework evidence", "points": parts["keywords"], "max": w["keywords"],
         "detail": f"{c.keyword_repos} repo(s) show {framework} (full marks at 3)" + (f": {', '.join(c.matching_repos[:3])}" if c.matching_repos else "")},
        {"name": f"{langs} depth", "points": parts["language"], "max": w["language"],
         "detail": f"{c.language_repos} {langs} repo(s) (full marks at 5)"},
        {"name": "Experience fit", "points": parts["experience"], "max": w["experience"],
         "detail": f"{years}; target {role.min_years}-{role.max_years} (an estimate from the first public repo)"},
        {"name": "Recent activity", "points": parts["activity"], "max": w["activity"],
         "detail": f"{idle}; window {role.active_within_months} months"},
        {"name": "Reachability", "points": parts["contact"], "max": w["contact"],
         "detail": ", ".join(contact_bits) or "no public contact details on GitHub"},
    ]
    for rule in rules:
        rule["level"] = level(rule["points"], rule["max"])
    return {"total": parts["total"], "gates": gates, "rules": rules}
