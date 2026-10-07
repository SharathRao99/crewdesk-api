"""Seed throwaway companies and users for the web app's browser tests.

Refuses to run unless the database names start with `e2e_`, so it can never wipe real data.
Usage (done by the web repo's Playwright config):
    PLATFORM_DB=e2e_platform TENANT_DB_PREFIX=e2e_t_ python scripts/seed_e2e.py
"""

import os
import sys

PROJECTS = ("phone", "tablet", "desktop")  # one user per browser project, so parallel tests never share a password
INITIAL_PASSWORD = "Initial-Pass-123"
ROLE = {
    "name": "php-dev", "languages": ["PHP"], "keywords": ["laravel"], "locations": ["India"],
    "experience": {"min_years": 2, "max_years": 6},
    "outreach": {"role_title": "PHP Developer", "subject": "Role at {company_name}", "body": "Hi {first_name}, we like {repo}."},
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
        roles_store.import_role(db, ROLE)
        for i, login in enumerate(logins):
            db["candidates"].insert_one({
                "_id": f"php-dev:{login}", "role": "php-dev", "login": login, "name": login.title(), "score": 90 - i * 10,
                "status": "new", "notes": "", "location": "Bengaluru, India", "est_years": 3.0, "language_share": 0.5,
                "keyword_repos": 3, "language_repos": 5, "matching_repos": [f"{login}-shop"], "last_active": "2026-09-01",
                "url": f"https://github.com/{login}", "email": f"{login}@example.com", "public_repos": 10, "followers": 1,
            })
        for project in PROJECTS:
            registry.create_user(f"hr-{project}@{slug}.test", slug, "admin", INITIAL_PASSWORD)
    print("seeded", ", ".join(slug for slug, _, _ in COMPANIES))


if __name__ == "__main__":
    main()
