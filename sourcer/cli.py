"""Command line entry point: run / list / status / export."""

import argparse
import logging
import sys
from dataclasses import asdict
from typing import List

from dotenv import load_dotenv

from sourcer.discover import discover
from sourcer.enrich import enrich, location_ok
from sourcer.export import has_contact, has_useful_link, write_csv, write_xlsx
from sourcer.github import GitHubClient
from sourcer.role import load_role
from sourcer.scoring import meets_primary_skill, score

logger = logging.getLogger("sourcer")


def cmd_run(args: argparse.Namespace) -> None:
    from datetime import datetime, timezone

    started = datetime.now(timezone.utc)
    role = load_role(args.role_file)
    collection = None
    if not args.dry_run:
        from sourcer.store import get_collection, prune_stale, save_candidates

        collection = get_collection()

    rows, pending, saved, skipped_primary = [], [], 0, 0
    errors = consecutive_errors = 0
    client = GitHubClient()
    try:
        logins = sorted(discover(client, role, pages=args.pages))
        if args.max_candidates:
            logins = logins[: args.max_candidates]

        for i, login in enumerate(logins, 1):
            if i % 25 == 0:
                logger.info(f"Processed {i}/{len(logins)} ({len(rows)} kept, {skipped_primary} not primary skill)")
            try:
                candidate = enrich(client, role, login)
            except Exception as e:  # one bad profile must not kill a long run
                errors += 1
                consecutive_errors += 1
                logger.warning(f"Skipping {login}: {e}")
                if consecutive_errors >= 10:  # something systemic (network, token): stop, keep checkpoints
                    raise RuntimeError("10 candidates failed in a row; aborting. Saved progress is kept.") from e
                continue
            consecutive_errors = 0
            if not candidate or candidate.public_repos < role.min_public_repos:
                continue
            if not meets_primary_skill(candidate, role):
                skipped_primary += 1
                continue
            breakdown = score(candidate, role)
            row = {**asdict(candidate), "score": breakdown["total"], "score_breakdown": breakdown}
            rows.append(row)
            pending.append(row)
            if collection is not None and len(pending) >= 25:  # checkpoint so a crash loses little
                saved += save_candidates(collection, role.name, pending)
                pending = []
    finally:
        client.close()
        if collection is not None and pending:
            saved += save_candidates(collection, role.name, pending)

    rows.sort(key=lambda r: r["score"], reverse=True)
    logger.info(f"{len(rows)} candidates matched role {role.name!r} ({skipped_primary} dropped: not a primary skill)")

    if args.dry_run:
        for row in rows[:20]:
            print(f"{row['score']:5.1f}  {row['login']:<25} {row['location'] or '':<25} ~{row['est_years']}y  {row['url']}")
    else:
        print(f"Saved {saved} candidates to MongoDB")
        if args.max_candidates:
            pass
        elif errors:  # an incomplete run can't tell who no longer qualifies
            print(f"{errors} candidates failed; skipped removing stale candidates")
        else:
            print(f"Removed {prune_stale(collection, role.name, started)} stale candidates")
    if args.csv:
        write_csv(args.csv, rows)
        print(f"Wrote {args.csv}")


def cmd_list(args: argparse.Namespace) -> None:
    from sourcer.store import find_candidates, get_collection

    for row in find_candidates(get_collection(), args.role, args.status, args.min_score, args.limit):
        print(f"{row['score']:5.1f}  {row['status']:<12} {row['login']:<25} {row.get('email') or '-':<30} {row['url']}")


def cmd_status(args: argparse.Namespace) -> None:
    from sourcer.store import get_collection, set_status

    if not set_status(get_collection(), args.role, args.login, args.status, args.note):
        sys.exit(f"No candidate {args.login!r} for role {args.role!r}")
    print(f"{args.login} -> {args.status}")


def cmd_export(args: argparse.Namespace) -> None:
    from sourcer.store import find_candidates, get_collection

    from sourcer.outreach import DEFAULT_OUTREACH

    role = load_role(args.role_file)
    rows = [r for r in find_candidates(get_collection(), role.name, args.status, args.min_score) if location_ok(r.get("location"), role)]
    outreach = {**DEFAULT_OUTREACH, **role.outreach}
    keep = has_contact if args.require == "contact" else has_useful_link
    write = write_xlsx if args.output.lower().endswith(".xlsx") else write_csv
    print(f"Wrote {write(args.output, rows, outreach, args.sender, keep)} rows to {args.output}")


def cmd_contacts(args: argparse.Namespace) -> None:
    """Backfill company, LinkedIn URL and portfolio contact links for stored candidates."""
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timezone

    from sourcer.enrich import find_linkedin
    from sourcer.portfolio import crawl_portfolio, find_phones
    from sourcer.store import find_candidates, get_collection, update_fields

    collection = get_collection()
    rows = find_candidates(collection, args.role, min_score=args.min_score)
    if not args.refresh:
        rows = [r for r in rows if "contacts_checked_at" not in r]
    if args.limit:
        rows = rows[: args.limit]
    logger.info(f"Looking up contacts for {len(rows)} candidates")

    client = GitHubClient()
    try:
        github = {}
        for row in rows:
            profile = client.get(f"/users/{row['login']}") or {}
            github[row["login"]] = (profile.get("company"), find_linkedin(client, row["login"], profile))
    finally:
        client.close()

    def crawl(row):
        return crawl_portfolio(row.get("blog"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        crawled = list(pool.map(crawl, rows))

    for row, site in zip(rows, crawled):
        company, linkedin = github[row["login"]]
        update_fields(collection, args.role, row["login"], {
            "company": company,
            "linkedin_url": linkedin or site["linkedin"],
            "portfolio_url": site["portfolio_url"],
            "portfolio_emails": site["emails"],
            "resume_links": site["resumes"],
            "portfolio_phones": sorted(set(site["phones"]) | find_phones(row.get("bio") or ""))[:2],
            "contacts_checked_at": datetime.now(timezone.utc),
        })
    with_linkedin = sum(1 for r, s in zip(rows, crawled) if github[r["login"]][1] or s["linkedin"])
    print(f"Updated {len(rows)} candidates; {with_linkedin} have a LinkedIn URL")


def cmd_report(args: argparse.Namespace) -> None:
    from sourcer.report import build_report
    from sourcer.store import find_candidates, get_collection

    role = load_role(args.role_file)
    rows = [r for r in find_candidates(get_collection(), role.name, min_score=args.min_score) if location_ok(r.get("location"), role)]
    count = build_report(role, rows, args.output)
    print(f"Wrote {count} candidates to {args.output}")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("sourcer.web:app", host=args.host, port=args.port)


def build_parser() -> argparse.ArgumentParser:
    from sourcer.store import STATUSES

    parser = argparse.ArgumentParser(prog="sourcer", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="discover, enrich, score and store candidates for a role file")
    run.add_argument("role_file")
    run.add_argument("--pages", type=int, default=2, help="search pages (100 results each) per query")
    run.add_argument("--max-candidates", type=int, default=0, help="cap candidates enriched (0 = no cap)")
    run.add_argument("--dry-run", action="store_true", help="skip MongoDB, print top results")
    run.add_argument("--csv", help="also write results to this CSV path")
    run.set_defaults(func=cmd_run)

    ls = sub.add_parser("list", help="list stored candidates, best score first")
    ls.add_argument("role")
    ls.add_argument("--status", choices=STATUSES)
    ls.add_argument("--min-score", type=float, default=0)
    ls.add_argument("--limit", type=int, default=50)
    ls.set_defaults(func=cmd_list)

    st = sub.add_parser("status", help="record outreach progress for a candidate")
    st.add_argument("role")
    st.add_argument("login")
    st.add_argument("status", choices=STATUSES)
    st.add_argument("--note", default="")
    st.set_defaults(func=cmd_status)

    ex = sub.add_parser("export", help="export stored candidates to CSV, with email drafts")
    ex.add_argument("role_file")
    ex.add_argument("--require", choices=("contact", "links"), default="contact",
                    help="keep rows with an email or phone (default), or with a LinkedIn/portfolio/resume link")
    ex.add_argument("--sender", default="", help="your name, used as {sender} in the email text")
    ex.add_argument("output", help="out.csv, or out.xlsx for clickable mail links in Excel")
    ex.add_argument("--status", choices=STATUSES)
    ex.add_argument("--min-score", type=float, default=0)
    ex.set_defaults(func=cmd_export)

    ct = sub.add_parser("contacts", help="find LinkedIn/portfolio/resume links for stored candidates")
    ct.add_argument("role")
    ct.add_argument("--min-score", type=float, default=0)
    ct.add_argument("--limit", type=int, default=0)
    ct.add_argument("--refresh", action="store_true", help="redo candidates already checked")
    ct.set_defaults(func=cmd_contacts)

    rp = sub.add_parser("report", help="write a standalone HTML report for HR (no server needed)")
    rp.add_argument("role_file")
    rp.add_argument("output")
    rp.add_argument("--min-score", type=float, default=0)
    rp.set_defaults(func=cmd_report)

    sv = sub.add_parser("serve", help="start the web UI")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(func=cmd_serve)

    return parser


def main(argv: List[str] = None) -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = build_parser().parse_args(argv)
    args.func(args)
