# Crewdesk: one parent app, many modules

Crewdesk is a multi-company work platform. Any company can clone it, run the setup command and use it; nothing in the code is tied to one company.

## Shape

- **One web app, one login, one installable PWA.** Modules live under paths: `/hiring/...` (today's app), `/people/...` (new). The home screen shows the apps the user may open; every module has an "All apps" button back to it.
- **One API, one database per company** (plus `platform`), as now. Each module is a package with its own routes: `sourcer/hiring`, `sourcer/people`.
- **Split later if needed.** A module can move to its own subdomain later because the boundary is already a folder and a route prefix. Not needed now (one PWA install, no cross-domain login).

## Access

Each user has a company role and a list of modules.

| Role | Hiring | People |
|---|---|---|
| admin (HR) | yes | everything |
| employee | no | own profile, own leave, own balance |

The API checks the module on every request, not only the menu.

## Employee onboarding (decided 2026-10-12)

No email is configured, so HR shares a link by hand.

1. HR creates the employee in People.
2. HR clicks "Create login link". The API creates a user (role `employee`, linked to the employee record) with no password and a **single-use, expiring** token. Only the hash is stored, as for the screening links. The full link is shown once; HR copies it into WhatsApp or similar.
3. The employee opens `/join/<token>`, sets a password and is signed in. The link is dead after that.
4. Lost link or forgotten password: HR clicks "Create login link" again. It invalidates older links.

Admin accounts keep today's command (`sourcer-admin user create`).

## People module

- **Employee**: name, work email, phone, employee number, joining date, manager, status, linked login (optional).
- **Leave type** and **policy** per company and year: planned (for example 18 days), sick, casual, others HR defines. Days per year, optional carry-forward, whether it counts weekends.
- **Holidays**: company holiday list; excluded from leave days.
- **Leave record**: employee, type, start, end, day part (full, first half, second half), reason, status (recorded, approved, rejected, cancelled), approved by (optional), note. HR records on behalf of employees; later employees can request.
- **Balance** = entitlement minus approved days (half day = 0.5), shown to HR and to the employee.

## Build order

1. Platform shell: Apps home, "All apps", module permissions, Hiring under `/hiring`, first-run setup command (`sourcer-admin setup --company ... --email ...`), neutral example role instead of the Zibtek one, README quick start, folder rename.
2. People: employees.
3. People: leave policy, holidays, leave records.
4. People: balances and employee login (magic link, `/join/<token>`, self-service view).
