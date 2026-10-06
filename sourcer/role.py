"""Role profile loaded from a YAML file."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

DEFAULT_WEIGHTS: Dict[str, float] = {
    "keywords": 40,
    "language": 10,
    "experience": 25,
    "activity": 15,
    "contact": 10,
}


@dataclass
class Role:
    name: str
    languages: List[str]
    keywords: List[str]
    locations: List[str]
    exclude_locations: List[str] = field(default_factory=list)
    min_years: float = 0
    max_years: float = 100
    min_public_repos: int = 0
    active_within_months: int = 12
    weights: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    # Primary-skill gate: the language must be a real share of the person's own
    # repos, and the framework must show up in at least this many repos.
    min_language_share: float = 0
    min_keyword_repos: int = 0
    # Optional dependency check used when repo names/descriptions don't mention the framework.
    manifest_file: Optional[str] = None
    manifest_contains: List[str] = field(default_factory=list)
    outreach: Dict[str, str] = field(default_factory=dict)


def load_role(path: str) -> Role:
    data = yaml.safe_load(Path(path).read_text())
    for key in ("name", "languages", "locations"):
        if not data.get(key):
            raise ValueError(f"Role file {path} is missing required field '{key}'")

    experience = data.get("experience") or {}
    signals = data.get("signals") or {}
    primary = data.get("primary_skill") or {}
    manifest = primary.get("manifest") or {}
    return Role(
        name=data["name"],
        languages=list(data["languages"]),
        keywords=[k.lower() for k in data.get("keywords") or []],
        locations=list(data["locations"]),
        exclude_locations=list(data.get("exclude_locations") or []),
        min_years=experience.get("min_years", 0),
        max_years=experience.get("max_years", 100),
        min_public_repos=signals.get("min_public_repos", 0),
        active_within_months=signals.get("active_within_months", 12),
        weights={**DEFAULT_WEIGHTS, **(data.get("weights") or {})},
        min_language_share=primary.get("min_language_share", 0),
        min_keyword_repos=primary.get("min_keyword_repos", 0),
        manifest_file=manifest.get("file"),
        manifest_contains=[m.lower() for m in manifest.get("contains") or []],
        outreach=data.get("outreach") or {},
    )
