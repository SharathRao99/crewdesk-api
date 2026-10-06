"""Find candidate GitHub logins for a role."""

import logging
from datetime import date, timedelta
from typing import Dict, Set

from sourcer.github import GitHubClient
from sourcer.role import Role

logger = logging.getLogger(__name__)

PER_PAGE = 100
MAX_SEARCH_PAGES = 10  # GitHub caps search results at 1,000 per query


def discover(client: GitHubClient, role: Role, pages: int = 2) -> Set[str]:
    """Return candidate logins from two complementary searches.

    1. Users by language + profile location (fanned out over each configured
       location, since each query is capped at 1,000 results).
    2. Owners of recently active repos matching the role keywords. Their
       location is checked later during enrichment.
    """
    pages = min(pages, MAX_SEARCH_PAGES)
    logins: Set[str] = set()

    for language in role.languages:
        for location in role.locations:
            query = f'language:{language} location:"{location}" type:user repos:>={max(role.min_public_repos, 1)}'
            found = _search_users(client, query, pages)
            logger.info(f"users {query!r}: {len(found)}")
            logins |= found

    cutoff = date.today() - timedelta(days=30 * role.active_within_months)
    for keyword in role.keywords:
        for language in role.languages:
            query = f"{keyword} language:{language} in:name,description,topics pushed:>{cutoff.isoformat()} fork:false"
            found = _search_repo_owners(client, query, pages)
            logger.info(f"repos {query!r}: {len(found)} owners")
            logins |= found

    logger.info(f"Discovered {len(logins)} unique candidates")
    return logins


def _search_users(client: GitHubClient, query: str, pages: int) -> Set[str]:
    logins: Set[str] = set()
    for page in range(1, pages + 1):
        data = client.get(
            "/search/users",
            {"q": query, "sort": "repositories", "per_page": PER_PAGE, "page": page},
            search=True,
        )
        items = (data or {}).get("items", [])
        logins |= {item["login"] for item in items if item.get("type") == "User"}
        if len(items) < PER_PAGE:
            break
    return logins


def _search_repo_owners(client: GitHubClient, query: str, pages: int) -> Set[str]:
    owners: Dict[str, None] = {}
    for page in range(1, pages + 1):
        data = client.get(
            "/search/repositories",
            {"q": query, "sort": "updated", "per_page": PER_PAGE, "page": page},
            search=True,
        )
        items = (data or {}).get("items", [])
        for item in items:
            if item["owner"].get("type") == "User":
                owners[item["owner"]["login"]] = None
        if len(items) < PER_PAGE:
            break
    return set(owners)
