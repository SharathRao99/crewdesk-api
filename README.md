# talent-sourcer

Role-driven developer sourcing from **public GitHub data**, with rule-based scoring and manual-outreach tracking in MongoDB. A new role is a new YAML file; no code changes.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env   # add GITHUB_TOKEN (no scopes needed) and MONGODB_URI
```

A `GITHUB_TOKEN` is effectively required: unauthenticated search allows ~10 requests/min and the run will mostly sit waiting.

## Usage

```bash
sourcer run roles/laravel-dev-india-3y.yaml --dry-run --max-candidates 20   # try it, no DB
sourcer run roles/laravel-dev-india-3y.yaml --csv out/laravel.csv           # full run, saves to MongoDB
sourcer list laravel-dev-india-3y --min-score 60
sourcer status laravel-dev-india-3y some-login contacted --note "emailed 5 Oct"
sourcer export roles/laravel-dev-india-3y.yaml out/shortlist.csv --sender "Your Name" --min-score 60   # CSV with ready-to-paste email drafts
sourcer report roles/laravel-dev-india-3y.yaml out/hr-report.html                                        # standalone HTML for HR
```

Re-running a role refreshes candidate data but keeps their `status` and `notes`.

## How it works

1. **Discover**: GitHub search for users by language + profile location, and for owners of recently active repos matching the role keywords.
2. **Enrich**: fetch each profile and their own (non-fork) repos. Candidates whose profile location matches none of the role's locations are dropped.
3. **Score (0-100, rule-based)**: keyword repos (40), language repos (10), experience fit (25), recent activity (15), public contact info (10). Weights can be overridden per role.
4. **Store / export**: MongoDB `candidates` collection, keyed `role:login`, plus CSV.

## Limits

- Experience is **estimated** from the first public repo in a role language. GitHub has no experience field, so treat it as a hint.
- Only developers active on GitHub are visible; many are not. Use this alongside job boards and referrals.
- Search results are capped at 1,000 per query, so discovery fans out across locations/keywords.
- Location is free text and noisy.

## Responsible use

Only public profile data is collected, and email is taken only if the person lists it on their profile. Outreach is manual by design. Keep it personal and low volume (GitHub's terms prohibit spam), and honour deletion/opt-out requests (India's DPDP Act; UK/EU GDPR if candidates are there). Don't add commit-email harvesting.

## Tests

```bash
.venv/bin/pytest
```
