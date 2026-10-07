"""Seed throwaway companies and users for the web app's browser tests.

Refuses to run unless the database names start with `e2e_`, so it can never wipe real data.
Usage (done by the web repo's Playwright config):
    PLATFORM_DB=e2e_platform TENANT_DB_PREFIX=e2e_t_ python scripts/seed_e2e.py
"""

import os
import sys

PROJECTS = ("phone", "tablet", "desktop")  # one user per browser project, so parallel tests never share a password
INITIAL_PASSWORD = "Initial-Pass-123"
READY_PASSWORD = "A-brand-new-password-1"  # for the `ready-*` users, whose password change is already done
def role_for(project: str) -> dict:
    # One role (and so one set of candidates) per browser project, so parallel tests never share data.
    return {
        "name": f"php-dev-{project}", "languages": ["PHP"], "keywords": ["laravel"], "locations": ["India"],
        "experience": {"min_years": 2, "max_years": 6},
        "outreach": {"role_title": "PHP Developer", "subject": "Role at {company_name}", "body": "Hi {first_name}, we like {repo}.\nCareer page: https://example.com/careers"},
    }


COMPANIES = (("e2e-a", "Alpha Co", ("ann", "bob", "cy")), ("e2e-b", "Beta Co", ("zed",)))


def main() -> None:
    if not os.getenv("PLATFORM_DB", "").startswith("e2e_") or not os.getenv("TENANT_DB_PREFIX", "").startswith("e2e_"):
        sys.exit("Refusing to seed: PLATFORM_DB and TENANT_DB_PREFIX must start with 'e2e_'")

    from sourcer import roles_store
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import client, platform_db, tenant_db, tenant_db_name

    client().drop_database(platform_db().name)
    for slug, _, _ in COMPANIES:
        client().drop_database(tenant_db_name(slug))

    for slug, name, logins in COMPANIES:
        registry.create_tenant(slug, name)
        db = tenant_db(slug)
        for project in PROJECTS:
            role = role_for(project)
            roles_store.import_role(db, role)
            for i, login in enumerate(logins):
                db["candidates"].insert_one({
                    "_id": f"{role['name']}:{login}", "role": role["name"], "login": login, "name": login.title(), "score": 90 - i * 10,
                    "status": "new", "notes": "", "location": "Bengaluru, India", "est_years": 3.0, "language_share": 0.5,
                    "keyword_repos": 3, "language_repos": 5, "matching_repos": [f"{login}-shop"], "last_active": "2026-09-01",
                    "url": f"https://github.com/{login}", "email": f"{login}@example.com", "public_repos": 10, "followers": 1,
                    # Only ann has a LinkedIn link and a phone; only bob has a portfolio, so the filters have something to find.
                    **({"linkedin_url": f"https://www.linkedin.com/in/{login}", "portfolio_phones": ["+919876543210"]} if login == "ann" else {}),
                    **({"portfolio_url": f"https://{login}.example.dev"} if login == "bob" else {}),
                })
        for project in PROJECTS:
            # hr-*: a fresh account that must change its password (only the password-change test uses it)
            registry.create_user(f"hr-{project}@{slug}.test", slug, "admin", INITIAL_PASSWORD)
            # ready-*: signed up and past the forced change, so other tests can log in straight away
            ready = f"ready-{project}@{slug}.test"
            registry.create_user(ready, slug, "admin", INITIAL_PASSWORD)
            registry.change_password(ready, INITIAL_PASSWORD, READY_PASSWORD)
    print("seeded", ", ".join(slug for slug, _, _ in COMPANIES))


if __name__ == "__main__":
    main()
