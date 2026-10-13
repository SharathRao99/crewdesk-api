"""Seed throwaway companies and users for the web app's browser tests.

Refuses to run unless the database names start with `e2e_`, so it can never wipe real data.
Usage (done by the web repo's Playwright config):
    PLATFORM_DB=e2e_platform TENANT_DB_PREFIX=e2e_t_ python scripts/seed_e2e.py
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

PROJECTS = ("phone", "tablet", "desktop", "narrow")  # one user per browser project, so parallel tests never share a password
INITIAL_PASSWORD = "Initial-Pass-123"
READY_PASSWORD = "A-brand-new-password-1"  # for the `ready-*` users, whose password change is already done
def role_for(project: str) -> dict:
    # One role (and so one set of candidates) per browser project, so parallel tests never share data.
    return {
        "name": f"php-dev-{project}", "languages": ["PHP"], "keywords": ["laravel"], "locations": ["India"],
        "experience": {"min_years": 2, "max_years": 6},
        "outreach": {"role_title": "PHP Developer", "subject": "Role at {company_name}", "body": "Hi {first_name}, we like {repo}.\nCareer page: https://example.com/careers"},
    }


def trash_role_for(project: str) -> dict:
    # A role of its own holding one person the delete test removes, so nothing else is disturbed.
    return {**role_for(project), "name": f"php-dev-{project}-trash"}


COMPANIES = (("e2e-a", "Alpha Co", ("ann", "bob", "cy")), ("e2e-b", "Beta Co", ("zed",)))


def seed_screening(db, slug: str, tokens: dict) -> None:
    """Cy has already answered the form; Bob has an expired link; plus a throwaway role to delete from."""
    from sourcer import roles_store, screening

    now = datetime.now(timezone.utc)
    for project in PROJECTS:
        role = role_for(project)["name"]
        cy_id, bob_id = f"{role}:cy", f"{role}:bob"
        used = {"candidate_id": cy_id, "role": role, "login": "cy", "created_by": "seed", "created_at": now - timedelta(days=2),
                "expires_at": now + timedelta(days=12), "used_at": now - timedelta(days=1), "opened_at": now - timedelta(days=1), "revoked": False}
        used_id = db["screening_links"].insert_one(used).inserted_id
        db["screening_responses"].insert_one({"link_id": used_id, "candidate_id": cy_id, "role": role, "login": "cy", "current_ctc": 10.0,
                                              "expected_ctc": 14.0, "notice_days": 30, "comment": "Happy to relocate", "consent": True,
                                              "consent_version": screening.CONSENT_VERSION, "submitted_at": now - timedelta(days=1)})
        db["candidates"].update_one({"_id": cy_id}, {"$set": {"status": "replied", "screening": {"current_ctc": 10.0, "expected_ctc": 14.0, "notice_days": 30, "answered_at": now - timedelta(days=1)}}})
        expired = {"candidate_id": bob_id, "role": role, "login": "bob", "created_by": "seed", "created_at": now - timedelta(days=30),
                   "expires_at": now - timedelta(days=16), "used_at": None, "opened_at": None, "revoked": False}
        expired_id = db["screening_links"].insert_one(expired).inserted_id
        tokens[project] = {"answered": screening.make_token(slug, used_id), "expired": screening.make_token(slug, expired_id)}

        trash = trash_role_for(project)
        roles_store.import_role(db, trash)
        for login, name in (("temp", "Temp"), ("form", "Form")):  # temp is deleted by one test, form answers the form in another
            db["candidates"].insert_one({
                "_id": f"{trash['name']}:{login}", "role": trash["name"], "login": login, "name": name, "score": 50, "status": "new", "notes": "",
                "location": "Bengaluru, India", "est_years": 3.0, "language_share": 0.5, "keyword_repos": 3, "language_repos": 5,
                "matching_repos": [f"{login}-shop"], "last_active": "2026-09-01", "url": f"https://github.com/{login}", "email": f"{login}@example.com",
                "public_repos": 10, "followers": 1})


def seed_jobs(db, project: str) -> None:
    """A finished run and a failed run on the main role, and a running one (waiting on GitHub) on the throwaway role."""
    from sourcer import jobs

    now = datetime.now(timezone.utc)
    role, trash = role_for(project)["name"], trash_role_for(project)["name"]

    done = jobs.create(db, role, {"pages": 2}, "seed@e2e-a.test")
    db["jobs"].update_one({"_id": done["_id"]}, {"$set": {
        "status": "done", "phase": "finished", "total": 10, "cursor": 10, "kept": 3, "not_primary": 4, "dropped": 2, "errors": 1,
        "contacts_total": 3, "contacts_done": 3, "started_at": now - timedelta(hours=3), "finished_at": now - timedelta(hours=1)}})
    for message in ("Found 10 possible people to check", "Checked everyone: 3 kept, 4 not mainly in the role's skill, 2 outside the role's places", "Finished"):
        jobs.event(db, done["_id"], "info", message)

    failed = jobs.create(db, role, {"pages": 2}, "seed@e2e-a.test")
    db["jobs"].update_one({"_id": failed["_id"]}, {"$set": {
        "status": "failed", "phase": "processing", "total": 50, "cursor": 12, "kept": 5, "errors": 10, "started_at": now - timedelta(hours=6),
        "finished_at": now - timedelta(hours=5), "error": "10 people in a row failed, so the run stopped. Progress is kept."}})
    jobs.event(db, failed["_id"], "error", "Stopped: 10 people in a row failed, so the run stopped. Progress is kept.")

    running = jobs.create(db, trash, {"pages": 2}, "seed@e2e-a.test")
    db["jobs"].update_one({"_id": running["_id"]}, {"$set": {
        "status": "running", "phase": "processing", "total": 100, "cursor": 40, "kept": 12, "not_primary": 20, "dropped": 8,
        "started_at": now - timedelta(minutes=30), "heartbeat_at": now,
        "note": "Waiting on GitHub (rate limit) for about 20 min. This is normal."}})


def seed_ordering_and_paging(db, project: str) -> None:
    """A role where a lower-scoring Bengaluru person must outrank a higher-scoring Hyderabad one, and a role with
    250 people so the pagination bar shows "1-25 of 250" and "1 / 10" (the text that overflowed a 320px screen)."""
    from sourcer import roles_store

    def person(role: str, login: str, name: str, score: float, location: str) -> dict:
        return {"_id": f"{role}:{login}", "role": role, "login": login, "name": name, "score": score, "status": "new", "notes": "",
                "location": location, "est_years": 3.0, "language_share": 0.5, "keyword_repos": 3, "language_repos": 5,
                "matching_repos": [f"{login}-shop"], "last_active": "2026-09-01", "url": f"https://github.com/{login}",
                "email": f"{login}@example.com", "public_repos": 10, "followers": 1}

    pref = f"php-dev-{project}-pref"
    roles_store.import_role(db, {**role_for(project), "name": pref, "preferred_locations": ["Bengaluru", "Bangalore"]})
    db["candidates"].insert_many([
        person(pref, "far", "Far", 90, "Hyderabad, India"),
        person(pref, "home", "Home", 60, "Bengaluru, India"),
        person(pref, "nearby", "Nearby", 40, "Bangalore"),
    ])
    roles_store.apply_preferred(db, pref)  # flags people saved after the role was imported

    many = f"php-dev-{project}-many"
    roles_store.import_role(db, {**role_for(project), "name": many})
    db["candidates"].insert_many([person(many, f"m{i:03d}", f"Person {i:03d}", round(95 - i * 0.2, 1), "Bengaluru, India") for i in range(250)])


def seed_people(project: str, kind: str) -> None:
    """A company just for People tests in this browser project (employees are not scoped by project, so it needs its own).

    kind "p": read by the list tests and never changed (33 people, some leave, a holiday).
    kind "q": where tests add and edit people and record leave (Asha, Ben, Chen only; default rules, never changed).
    kind "r": where tests change the company's rules (nobody in it, so rule changes cannot disturb other tests).
    """
    from datetime import date, timedelta

    from sourcer.people import leave as leave_store
    from sourcer.people import policy as company_policy
    from sourcer.people import store
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import tenant_db

    slug = f"e2e-{kind}-{project}"
    names = {"p": "People {} Co", "q": "Write {} Co", "r": "Rules {} Co"}
    registry.create_tenant(slug, names[kind].format(project.title()))
    db = tenant_db(slug)
    if kind != "r":
        base = {"phone": None, "department": "Engineering"}
        asha = store.create(db, {**base, "name": "Asha Rao", "work_email": f"asha@{slug}.test", "joining_date": "2022-04-01",
                                 "designation": "Engineering Manager", "employee_no": "E-001"}, "seed")
        ben = store.create(db, {**base, "name": "Ben Carter", "work_email": f"ben@{slug}.test", "joining_date": "2023-06-12",
                                "designation": "Developer", "employee_no": "E-002", "manager_id": str(asha["_id"])}, "seed")
        chen = store.create(db, {**base, "name": "Chen Wei", "work_email": f"chen@{slug}.test", "joining_date": "2021-01-04",
                                 "designation": "Designer", "department": "Design", "employee_no": "E-003"}, "seed")
        store.update(db, chen["_id"], {"status": "inactive"}, "seed")
        if kind == "p":
            for i in range(1, 31):  # enough for several pages
                store.create(db, {"name": f"Many Person {i:02d}", "work_email": f"many{i:02d}@{slug}.test", "joining_date": "2024-01-15",
                                  "department": "Operations", "employee_no": f"M-{i:03d}"}, "seed")
            # Ben took three planned days (Mon-Wed of the first full week of March this year) and had one leave cancelled.
            march = date(date.today().year, 3, 1)
            monday = march + timedelta(days=(7 - march.weekday()) % 7)
            types = {t["code"]: t for t in company_policy.list_types(db)}
            leave_store.create(db, {"employee_id": str(ben["_id"]), "type_id": str(types["PL"]["_id"]), "start_date": monday.isoformat(),
                                    "end_date": (monday + timedelta(days=2)).isoformat(), "reason": "Family trip", "approved_by_id": str(asha["_id"])}, "seed")
            gone = leave_store.create(db, {"employee_id": str(ben["_id"]), "type_id": str(types["SL"]["_id"]),
                                           "start_date": (monday + timedelta(days=7)).isoformat()}, "seed")
            leave_store.cancel(db, gone["_id"], "Plans changed", "seed")
            company_policy.create_holiday(db, {"date": date(date.today().year, 12, 25).isoformat(), "name": "Year End Day"}, "seed")
    for role, label in (("admin", "ready"), ("recruiter", "recruiter"), ("employee", "employee")):
        email = f"{label}-{project}@{slug}.test"
        registry.create_user(email, slug, role, INITIAL_PASSWORD)
        registry.change_password(email, INITIAL_PASSWORD, READY_PASSWORD)


def main() -> None:
    if not os.getenv("PLATFORM_DB", "").startswith("e2e_") or not os.getenv("TENANT_DB_PREFIX", "").startswith("e2e_"):
        sys.exit("Refusing to seed: PLATFORM_DB and TENANT_DB_PREFIX must start with 'e2e_'")

    from sourcer import roles_store
    from sourcer.tenancy import registry
    from sourcer.tenancy.db import client, platform_db, tenant_db, tenant_db_name

    client().drop_database(platform_db().name)
    for slug, _, _ in COMPANIES:
        client().drop_database(tenant_db_name(slug))
    for project in PROJECTS:
        client().drop_database(tenant_db_name(f"e2e-p-{project}"))
        client().drop_database(tenant_db_name(f"e2e-q-{project}"))
        client().drop_database(tenant_db_name(f"e2e-r-{project}"))
    tokens: dict = {}

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
        if slug == "e2e-a":
            seed_screening(db, slug, tokens)
            registry.set_github_token(slug, "ghp_fake_token_for_tests_only")  # so runs can be queued; company B has none
            for project in PROJECTS:
                seed_jobs(db, project)
                seed_ordering_and_paging(db, project)
        for project in PROJECTS:
            # hr-*: a fresh account that must change its password (only the password-change test uses it)
            registry.create_user(f"hr-{project}@{slug}.test", slug, "admin", INITIAL_PASSWORD)
            # ready-*: signed up and past the forced change, so other tests can log in straight away
            ready = f"ready-{project}@{slug}.test"
            registry.create_user(ready, slug, "admin", INITIAL_PASSWORD)
            registry.change_password(ready, INITIAL_PASSWORD, READY_PASSWORD)
            if slug == "e2e-a":  # a non-admin, to show only administrators can delete people
                rec = f"recruiter-{project}@{slug}.test"
                registry.create_user(rec, slug, "recruiter", INITIAL_PASSWORD)
                registry.change_password(rec, INITIAL_PASSWORD, READY_PASSWORD)
    for project in PROJECTS:
        for kind in ("p", "q", "r"):
            seed_people(project, kind)
    out = os.getenv("E2E_SEED_OUT")
    if out:
        with open(out, "w") as f:
            json.dump(tokens, f)
    print("seeded", ", ".join(slug for slug, _, _ in COMPANIES))


if __name__ == "__main__":
    main()
