"""The product modules and which roles open them. The web app mirrors these ids (src/modules/registry.ts)."""

from typing import Any, Dict, List

MODULES = ("hiring", "people")

# What a role may open when the user has no explicit `modules` list.
ROLE_MODULES = {
    "admin": ("hiring", "people"),
    "recruiter": ("hiring",),
    "employee": ("people",),
}


def modules_for(user: Dict[str, Any]) -> List[str]:
    """The modules this user may open: their explicit list if set, else the role's default."""
    explicit = user.get("modules")
    allowed = explicit if explicit is not None else ROLE_MODULES.get(user.get("role", ""), ())
    return [m for m in MODULES if m in allowed]
