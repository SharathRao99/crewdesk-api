"""`sourcer-admin`: create companies and users, set their GitHub token, migrate v0 data."""

import argparse
import getpass
import os
import sys

from dotenv import load_dotenv

from sourcer.tenancy import crypto, registry
from sourcer.tenancy.db import TenantError, client, tenant_db


def _tenant_create(a):
    t = registry.create_tenant(a.slug, a.name)
    print(f"Created tenant {t['slug']!r} ({t['name']}).")
    print(f"Next: sourcer-admin tenant set-github-token {a.slug}   and   sourcer-admin user create --tenant {a.slug} --email you@company.com --role admin")


def _tenant_list(a):
    for t in registry.list_tenants():
        print(f"{t['slug']:<20} {t['status']:<8} {t['name']}")


def _tenant_token(a):
    token = os.getenv("GITHUB_TOKEN_FOR_TENANT") or getpass.getpass(f"GitHub token for {a.slug} (input hidden): ")
    if not token.strip():
        sys.exit("No token entered")
    registry.set_github_token(a.slug, token)
    print(f"Stored (encrypted) GitHub token for {a.slug!r}.")


def _tenant_delete(a):
    if a.confirm != a.slug:
        sys.exit(f"This permanently deletes the tenant's database and users. Re-run with --confirm {a.slug}")
    registry.delete_tenant(a.slug)
    print(f"Deleted tenant {a.slug!r}.")


def _tenant_import(a):
    """Copy candidates from the single-company v0 database into a tenant."""
    source = client()[a.from_db]["candidates"]
    target = tenant_db(a.slug)["candidates"]
    copied = 0
    for doc in source.find({}):
        target.replace_one({"_id": doc["_id"]}, doc, upsert=True)
        copied += 1
    print(f"Copied {copied} candidates from {a.from_db!r} into tenant {a.slug!r} (source left untouched).")


def _role_import(a):
    from sourcer import roles_store

    role = roles_store.import_role_file(tenant_db(a.tenant), a.file)
    print(f"Imported role {role.name!r} into {a.tenant!r}.")


def _candidates_revalidate(a):
    """Remove not-yet-contacted candidates whose location the role now excludes."""
    from sourcer import roles_store
    from sourcer.enrich import location_ok

    db = tenant_db(a.tenant)
    role, _ = roles_store.get_role(db, a.role)
    removed, kept_contacted = 0, []
    for row in db["candidates"].find({"role": a.role}):
        if location_ok(row.get("location"), role):
            continue
        if row.get("status", "new") == "new":
            db["candidates"].delete_one({"_id": row["_id"]})
            removed += 1
        else:
            kept_contacted.append(row["login"])
    print(f"Removed {removed} candidates outside the role's locations.")
    if kept_contacted:
        print(f"Kept {len(kept_contacted)} that already have a status (review by hand): {', '.join(kept_contacted)}")


def _retention_run(a):
    """Delete form answers and links older than --days (default 180) for one company, or all of them."""
    from sourcer import screening

    slugs = [t["slug"] for t in registry.list_tenants()] if a.all else [a.tenant]
    for slug in slugs:
        result = screening.purge_old(tenant_db(slug), a.days)
        print(f"{slug}: removed {result['responses']} answers and {result['links']} links older than {a.days} days")


def _user_create(a):
    user, password = registry.create_user(a.email, a.tenant, a.role, a.password)
    print(f"Created {user['role']} {user['email']} in {a.tenant!r}.")
    print(f"One-time password (shown once, must be changed at first login): {password}")


def _user_list(a):
    for u in registry.list_users(a.tenant):
        flag = " (disabled)" if u.get("disabled") else ""
        print(f"{u['email']:<35} {u['tenant']:<15} {u['role']}{flag}")


def _user_reset(a):
    print(f"New one-time password for {a.email}: {registry.reset_password(a.email)}")


def _user_disable(a):
    registry.set_disabled(a.email, not a.enable)
    print(f"{a.email} {'enabled' if a.enable else 'disabled'}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sourcer-admin", description=__doc__)
    sub = parser.add_subparsers(dest="group", required=True)

    sub.add_parser("generate-key", help="print a new SECRETS_KEY").set_defaults(func=lambda a: print(crypto.generate_key()))

    tenant = sub.add_parser("tenant").add_subparsers(dest="cmd", required=True)
    p = tenant.add_parser("create"); p.add_argument("slug"); p.add_argument("--name", required=True); p.set_defaults(func=_tenant_create)
    tenant.add_parser("list").set_defaults(func=_tenant_list)
    p = tenant.add_parser("set-github-token"); p.add_argument("slug"); p.set_defaults(func=_tenant_token)
    p = tenant.add_parser("delete"); p.add_argument("slug"); p.add_argument("--confirm", default=""); p.set_defaults(func=_tenant_delete)
    p = tenant.add_parser("import-v0", help="copy candidates from the old single-company database")
    p.add_argument("slug"); p.add_argument("--from-db", default="talent_sourcer"); p.set_defaults(func=_tenant_import)

    role = sub.add_parser("role").add_subparsers(dest="cmd", required=True)
    p = role.add_parser("import", help="load a role YAML into a company"); p.add_argument("--tenant", required=True)
    p.add_argument("--file", required=True); p.set_defaults(func=_role_import)

    cand = sub.add_parser("candidates").add_subparsers(dest="cmd", required=True)
    p = cand.add_parser("revalidate", help="drop candidates outside the role's locations")
    p.add_argument("--tenant", required=True); p.add_argument("--role", required=True); p.set_defaults(func=_candidates_revalidate)

    ret = sub.add_parser("retention").add_subparsers(dest="cmd", required=True)
    p = ret.add_parser("run", help="delete old screening answers and links"); p.add_argument("--tenant"); p.add_argument("--all", action="store_true")
    p.add_argument("--days", type=int, default=180); p.set_defaults(func=_retention_run)

    user = sub.add_parser("user").add_subparsers(dest="cmd", required=True)
    p = user.add_parser("create"); p.add_argument("--tenant", required=True); p.add_argument("--email", required=True)
    p.add_argument("--role", choices=registry.ROLES, default="recruiter"); p.add_argument("--password", help="omit to generate one"); p.set_defaults(func=_user_create)
    p = user.add_parser("list"); p.add_argument("--tenant"); p.set_defaults(func=_user_list)
    p = user.add_parser("reset-password"); p.add_argument("--email", required=True); p.set_defaults(func=_user_reset)
    p = user.add_parser("disable"); p.add_argument("--email", required=True); p.add_argument("--enable", action="store_true"); p.set_defaults(func=_user_disable)
    return parser


def main(argv=None) -> None:
    load_dotenv()
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except (TenantError, ValueError, RuntimeError) as e:
        sys.exit(f"Error: {e}")
