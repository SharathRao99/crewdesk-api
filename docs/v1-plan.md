# v1 plan: a multi-company hiring app

Status: decisions made, ready for Phase 0. v0 (tag `v0`) is the working pipeline: discover and score candidates from public GitHub data, look up portfolio contact links, store in MongoDB, export CSV/XLSX and a standalone HR report.

## Goal

A product that more than one company can use. Each company (a **tenant**) gets its own isolated database and its own users. A logged-in recruiter can create a hiring requirement, run the sourcing for it, review scored candidates with the reasons, contact them using saved email templates, and collect budget answers through a form.

Out of scope for v1: scraping LinkedIn or Naukri (against their terms; use their paid tools by hand and import), automatic sending of email (drafts stay manual), salary "guessing", self-service signup, hosting.

## Decisions (settled)

| Topic | Decision |
|---|---|
| Product direction | Multi-company. Possible commercial product later. |
| Isolation | **One MongoDB database per tenant**, plus one small `platform` database holding the tenant registry and users. No `tenant_id` filtering inside tenant data, so a query bug cannot leak across companies. |
| Users | Predefined only. Created by command, no signup page and no admin UI in v1. |
| Login | Email + password (hashed with argon2). No Google or domain allowlist, so it works for any company. |
| Hosting | **Deferred.** Build and test locally with Docker Compose first; decide after. |
| Repos | Two repos, no outer group: `talent-sourcer` (Python API + worker) and `talent-sourcer-web` (Next.js), both private. |
| Retention | 180 days for candidate data and form answers, then deleted. |
| Email text | Saved templates per tenant, reusable across a role, with dynamic placeholders. |

## Architecture

- **Web (`talent-sourcer-web`)**: Next.js + TypeScript. Recruiter screens plus the public candidate form. The browser only talks to Next.js.
- **API + worker (`talent-sourcer`)**: FastAPI and a background worker built on the v0 pipeline. The API is reached only by the Next.js server with a secret service key, and it is never exposed to browsers. Every request carries the tenant resolved from the logged-in user.
- **Why a worker:** one sourcing run takes 2-3 hours because of GitHub rate limits (about 1,000 candidates per hour per token), so it cannot run inside a web request. The worker takes jobs from the database and writes progress back.
- **Local run:** `docker compose up` starts MongoDB, API, worker and web.

### Multi-tenancy

- `platform` database: `tenants` (slug, display name, status, settings), `users` (email, password hash, tenant, role).
- One database per tenant, named `tenant_<slug>`: `roles`, `candidates`, `candidate_events`, `jobs`, `email_templates`, `screening_links`, `screening_responses`, `audit_log`.
- Each tenant brings **its own GitHub token** (stored encrypted with a key from the environment). This keeps rate limits and responsibility per company.
- Tenant settings: company name (used in emails and the form), retention days, branding later.
- Offboarding a tenant = drop its database.

### Admin commands (no UI in v1)

```
sourcer-admin tenant create <slug> --name "Company"
sourcer-admin user create --tenant <slug> --email a@b.com --role recruiter   # prints a one-time password
sourcer-admin user reset-password --email a@b.com
sourcer-admin tenant set-github-token <slug>
sourcer-admin tenant delete <slug>
```

## Email templates

A recruiter writes a template once and reuses it for every candidate of the same role.

- Stored per tenant in `email_templates`: name, subject, body, optional role link, created by, version.
- **Placeholders:** `{first_name}`, `{full_name}`, `{repo}`, `{location}`, `{company_name}`, `{role_title}`, `{sender}`, `{form_link}`. An unknown placeholder is rejected when saving, so a typo never reaches a candidate.
- Live preview against a real candidate before saving. Editing creates a new version; drafts already opened keep the text they were generated from.
- A role can have a default template, so every candidate under that role shows the same message by default.
- Output keeps what v0 proved: Gmail/Outlook/mail-app links, **formatted copy with clickable links**, and a trailing line break so a final URL becomes a link.

## Data model additions (per tenant)

- `roles`: the YAML profile as a document (languages, keywords, locations, excluded locations, experience range, primary-skill rules, **budget**, default template). The CLI keeps reading YAML.
  - `budget`: `min`, `max`, and an optional `stretch_max`, in lakh per annum (INR) by default. Optional when a run starts and editable any time.
- `candidate_events`: who changed what and when. Replaces overwriting status.
- `jobs`: `role_id`, status (queued/running/paused/failed/done), `discovered_logins`, `processed`, `kept`, `errors`, `started_by`, timestamps.
- `screening_links`: one per candidate per role; stores a hash of the token (never the token), expiry, sent/opened/submitted times.
- `screening_responses`: current CTC, expected CTC, notice period, consent flag, submitted_at.

## Phases

Sizes are rough for one developer: S about 2-3 days, M about a week, L about 2 weeks.

### Phase 0: Foundation (M)
- Create `talent-sourcer-web`. Add Docker Compose with Mongo.
- Platform database, tenant registry, `sourcer-admin` commands, per-tenant database access layer (one function returns the right database from the tenant; no code touches `MongoClient` directly).
- Move v0 storage code onto that layer. Role schema with YAML loader still working.
- API contract (OpenAPI from FastAPI) and the service-key link between web and API.
- Optional: change the git author email on v0.

Done when: two tenants exist locally and a test proves tenant A cannot read tenant B's candidates.

### Phase 1: Login, candidate review and email templates (M-L)
- Email + password login (Auth.js credentials), sessions, change password.
- Candidate list: search, filters (score, status, has email/phone/LinkedIn, location), sort, pagination.
- Candidate detail: the "why this score" breakdown, profile, links, contact, repos.
- Status and notes with history. Export CSV/XLSX with mail links.
- Saved email templates with placeholders, preview and per-role default, and the formatted-copy buttons.
- Port of today's HR report, which this fully replaces.

Done when: a recruiter at tenant A signs in, reviews candidates, uses a saved template to open a draft with clickable links, changes statuses, and a colleague sees it; tenant B sees none of it.

### Phase 2: Screening form (M) — built
- Role budget (HR enters it on the requirement, before or while starting a run; a small role-settings form covers this until the full builder in Phase 3). `{form_link}` in templates.
- Public page `/s/<token>`: privacy notice and consent checkbox, current CTC, expected CTC, notice period, submit. No login. Token expires and is single use.
- Responses on the candidate detail with **within budget / stretch / over budget**, worked out when the page is viewed from the role's *current* budget, so changing the budget updates every existing answer. GitHub has no salary data, so the budget does not filter the scrape itself; it only judges the answers.
- Rate limiting and spam protection on the public route.
- 180-day deletion job, and a "delete this candidate" action that removes their data everywhere.

Done when: a candidate answers from a link and the recruiter filters for "within budget".

### Phase 3: Requirements and sourcing runs (L)
- Role builder: create or clone a requirement with a live preview of the rules. The same form holds the **budget**, and its "Run sourcing" button starts the job, so HR fills in everything in one place.
- "Run sourcing" creates a job; the worker runs discover, enrich, score and contacts as one pipeline, with progress from job counters.
- Fix v0 gaps: jobs **resume after a crash** (discovered logins saved, finished ones skipped), cleanup never runs on an incomplete job, cancel and pause, one job at a time per GitHub token.

Done when: a recruiter creates a requirement, starts a run, closes the tab and comes back to the results.

### Phase 4: Growth (open-ended)
More sources (Stack Overflow, Dev.to) with duplicates merged, CSV import from Naukri/LinkedIn exports, scheduled re-runs, an admin UI, optional Gmail API sending, backups and alerts, then hosting.

## Mobile and responsive (requirement)

The whole app, not just the public form, must work well on a phone. Details are in [frontend-plan.md](frontend-plan.md). Candidates will mostly open the screening form on mobile, so that page is designed for a phone first.

## Risks

- **Selling it.** GitHub's terms restrict using data from the service for unsolicited email or spam. A product built around collecting public emails for cold outreach could conflict with that. Get a legal review, and a privacy policy and data-processing terms for customers, before charging anyone. Design to stay on the safe side: manual outreach only, personal messages, low volume, easy removal.
- **Personal data (DPDP Act, UK/EU GDPR).** Public data only; consent on the form; stated purpose and retention; delete and export on request; audit log of access. In a product, you may become a processor on behalf of each company, which needs written terms.
- **Salary answers are sensitive.** Recruiters only, encrypted at rest, not in exports unless chosen.
- **Tenant isolation.** The per-tenant database layer is the main safeguard; it needs tests from day one.
- **Credentials.** Passwords hashed, one-time passwords shown once, GitHub tokens encrypted, nothing secret in git or logs.
- **Weak data.** Experience is an estimate, location is free text, and only people active on GitHub appear. The UI must keep saying so.
- **Cost.** Small for local; estimate before hosting.

## What carries over from v0

All of `sourcer/`: discovery, enrichment, scoring and explanation, portfolio crawl, role loading, export and mail-link code. The standalone report's UI logic becomes React components. Tests stay.

## Also settled

- The budget is **not** mentioned in the email; it is only used for the "within budget" check after a candidate answers the form.
- The name stays `talent-sourcer` for now (and `talent-sourcer-web`).
- Each tenant uses **its own GitHub token**.
- Frontend packages: see [frontend-plan.md](frontend-plan.md).

## Still open

Nothing blocking. The budget for each role is entered by HR in the app, so it is not needed to build anything.


## Status

- Phase 0 (foundation) and Phase 1 (login, candidates, templates) are built.
- **Phase 2 is built:** signed single-use links (14 days), the public form (current and expected salary, notice period, consent), answers shown on the candidate with a within/stretch/over verdict from the role's current budget, list filters and sorting, `{form_link}` in templates and exports, role budgets (Roles page), admin-only deletion of a candidate and everything held about them, and the 180-day retention command (`sourcer-admin retention run`).
- Not yet scheduled: running the retention command automatically. Run it from cron or a job runner once hosting exists.
- Next: Phase 3 (role builder and sourcing runs with a worker, resumable).
