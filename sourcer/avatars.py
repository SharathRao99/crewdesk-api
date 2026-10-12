"""Avatars: a person picks a style and a seed; the web app draws the picture from them. Nothing is uploaded or stored as a file."""

import re
from typing import Any, Dict

# All CC0 (public domain) artwork, so the product can be sold. The web app (src/lib/avatar.ts) draws these.
STYLES = ("thumbs", "lorelei", "notionists", "open-peeps", "shapes")
DEFAULT_STYLE = "thumbs"
SEED_RE = re.compile(r"^[A-Za-z0-9]{1,24}$")


def validate(style: str, seed: str) -> Dict[str, str]:
    if style not in STYLES:
        raise ValueError(f"Style must be one of {', '.join(STYLES)}")
    if not SEED_RE.match(seed or ""):
        raise ValueError("Seed must be 1-24 letters or digits")
    return {"style": style, "seed": seed}


def of(user: Dict[str, Any]) -> Dict[str, str]:
    """The user's avatar, or a stable default made from their id so everyone has one from the start."""
    saved = user.get("avatar")
    if saved and saved.get("style") in STYLES and SEED_RE.match(saved.get("seed", "")):
        return {"style": saved["style"], "seed": saved["seed"]}
    return {"style": DEFAULT_STYLE, "seed": str(user["_id"])[-12:]}
