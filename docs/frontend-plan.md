# Frontend plan: `talent-sourcer-web`

Companion to [v1-plan.md](v1-plan.md). Versions are not pinned here: install the current stable release of each package at Phase 0 and record them in `package.json`.

## Stack at a glance

| Concern | Choice | Notes |
|---|---|---|
| Framework | **Next.js** (App Router), React, TypeScript `strict` | Public candidate form and recruiter app in one project |
| UI components | **shadcn/ui** on Tailwind CSS | Components are copied into `src/components/ui`, so we own them |
| Server state | **TanStack Query** | All data from the API: cache, mutations, optimistic updates, job-progress polling |
| Tables | **TanStack Table** + shadcn data-table pattern | Server-side sorting, filtering and pagination |
| Forms | **TanStack Form** + **Zod** | Role builder, template editor, screening form |
| Client UI state | **Zustand** | Only state that is not server data, not a form, and not in the URL |
| URL state | **nuqs** | Filters, sort and page live in the URL so refresh, back and shared links work |
| API types and client | **openapi-typescript** + **openapi-fetch** | Types generated from FastAPI's OpenAPI, so web and API cannot drift |
| Auth | **Signed session cookie** with `jose`, plus server actions | Email and password; the Python API verifies. Chosen over Auth.js after reading Next 16's own guidance (see Phase 1 findings) |
| Icons, toasts, theme | lucide-react, sonner, next-themes | shadcn defaults |
| Dates | date-fns | |
| Env validation | @t3-oss/env-nextjs + Zod | App refuses to start with a missing secret |
| Package manager | pnpm | |

Tests: **Vitest** and React Testing Library (units and components), **MSW** (mock the API in tests), **Playwright** (end-to-end). Lint and format: ESLint (Next config), Prettier with the Tailwind plugin.

## Choices worth a second look

- **No TanStack Router.** Next.js already routes. Mixing two routers adds cost and no benefit. We use TanStack Query, Table and Form, which are router-independent.
- **TanStack Form instead of React Hook Form.** RHF is shadcn's default and has more examples, so this is a real trade-off. TanStack Form keeps us in one family and has strong type inference; shadcn documents an integration. If it slows us down in Phase 1, switch: forms are isolated per feature.
- **nuqs on top of Zustand.** Candidate filters belong in the URL, not in a store. Zustand handles the small remainder.
- **Auth.js credentials.** Its credentials provider is deliberately minimal (JWT sessions only, no built-in password reset). That is fine for predefined users created by command, with the Python API doing the real password check. Revisit if we add self-service signup or SSO.
- **No rich-text editor.** Templates are plain text with placeholder chips; web addresses become links automatically (proven in v0). Add Tiptap only if HTML emails become a requirement.
- **No virtualisation or charts yet.** Add TanStack Virtual if a list passes a few thousand rows, and shadcn charts (Recharts) when a dashboard exists.

## Where state lives (the rule)

| Kind of state | Lives in | Example |
|---|---|---|
| Data from the API | TanStack Query | candidates, roles, templates, job progress |
| Shareable view state | URL (nuqs) | score filter, status filter, search, sort, page |
| Form values | TanStack Form | role builder, template editor |
| Transient UI | Zustand | open detail panel, selected row ids, sidebar collapsed, sender-name draft |

Never copy API data into a Zustand store. Mutations update the Query cache (optimistically for status and notes) and invalidate on settle.

## How the browser reaches the API

The browser never holds the API key.

```
browser -> Next.js catch-all route  /api/backend/[...path]  -> Python API
            (checks the session, adds tenant + user headers and the service key)
```

- One generic proxy route handler instead of one handler per endpoint. It rejects requests with no session.
- `openapi-fetch` is created once with `baseUrl: "/api/backend"`. Thin query hooks wrap it, with a **query-key factory** per feature (`candidateKeys.list(filters)`).
- The public form uses a separate proxy path that accepts a signed token and no session, and only reaches the screening endpoints.

## Structure

```
src/
  app/
    (auth)/login/
    (app)/                      # recruiter shell: sidebar layout, session required
      candidates/  candidates/[login]/
      roles/  roles/[id]/
      templates/
      jobs/
      settings/
    s/[token]/                  # public screening form, own minimal layout
    api/backend/[...path]/      # session-checking proxy
  components/ui/                # shadcn, generated
  features/
    candidates/  roles/  templates/  jobs/  screening/
      api/  (query hooks + key factory)   components/   schema.ts (Zod)   store.ts (Zustand, if needed)
  lib/                          # api client, auth config, env, clipboard + mail-link helpers
  stores/                       # app-wide Zustand (sidebar, theme)
```

Mail helpers are ported from v0: placeholder filling, `mailto`/Gmail/Outlook links with the trailing line break, and formatted (rich) clipboard copy.

## shadcn components for v1

button, input, textarea, label, select, checkbox, switch, badge, card, separator, tabs, table, tooltip, popover, command, dropdown-menu, dialog, **sheet** (candidate detail panel on desktop; focus trapping and Esc handling come with it), **drawer** (vaul; the same panel as a bottom sheet on phones), sidebar (off-canvas on mobile), skeleton, progress (job progress), scroll-area, alert, sonner, and the data-table pattern.

## Responsive and mobile

Mobile is a requirement for the recruiter app too, and the public form is designed **phone first** because candidates will mostly use a phone.

- **Approach:** mobile-first Tailwind (base styles are the phone layout; `sm`, `md`, `lg` add space). Prefer CSS over JavaScript media queries.
- **Navigation:** the shadcn sidebar becomes an off-canvas menu on small screens, opened from a top bar.
- **Candidate list:** the data table is a table from tablet width up. On a phone each candidate is a **card** (name, score, location, status, one-tap links) driven by the same TanStack Table rows, so sorting and filtering stay identical. Filters collapse into a "Filters" sheet.
- **Candidate detail:** a side sheet on desktop, a full-height bottom drawer on phones, with the same sections. Previous/next stay reachable with the thumb.
- **Forms** (role builder, template editor): one column on phones, labels above inputs, a sticky bottom action bar, and the correct `inputmode` and `type` so the right keyboard appears.
- **Touch:** controls at least 44px, no hover-only actions, no horizontal page scroll (tables scroll inside their own container only where unavoidable).
- **Mail on phones:** the plain `mailto` link opens the phone's mail app and the app turns web addresses into links, so on mobile it is the primary button. The Gmail/Outlook formatted-paste flow stays as a secondary option, because pasting formatted text is unreliable in some mobile apps.
- **Public form extras:** large inputs, numeric keyboard for salary fields, visible progress, and nothing that needs a desktop (no drag, no hover).
- **Later, not v1:** an installable PWA.

## Screens by phase

- **Phase 1:** login, candidates list (data table), candidate detail sheet with the score breakdown, templates (list, editor with placeholder chips and a live preview against a real candidate), status and notes with history, export.
- **Phase 2:** public screening form, budget badge on candidates.
- **Phase 3:** roles list and builder, "Run sourcing" and job progress.

## Quality gates

- TypeScript strict, ESLint and Vitest run on every change; Playwright covers three flows from day one: login, tenant isolation (user A never sees tenant B), and template to draft link.
- Playwright runs the main flows at three viewports: phone (375x812), tablet (768x1024) and desktop. A check fails on any horizontal page scroll or on a control smaller than 44px at phone width.
- Accessibility basics: keyboard navigation, focus management, labelled controls. shadcn and Radix give most of it.
- The public form is checked for no data leakage: it only ever returns the candidate's own first name and the role title.

## Phase 0 findings

- **shadcn now defaults to a Base UI based style (`base-nova`)** instead of Radix. The component list above is unchanged; wherever this plan says Radix, read Base UI. Components still live in `src/components/ui`.
- Installed so far: Next.js 16, React 19, TanStack Query, openapi-fetch/openapi-typescript, Zod 4, @t3-oss/env-nextjs, next-themes, sonner, Vitest, MSW, Playwright. **TanStack Table, TanStack Form, Zustand, nuqs and Auth.js are installed in the phase that first uses them**, not before.
- Playwright uses the installed Chrome (no browser download) and its own port, 3110, so it never attaches to another app. Local ports: web dev 3100, Python API 8001, old v0 report UI 8010 (3000 and 8000 are often taken).
- shadcn init wrote `--font-sans: var(--font-sans)` (self-referencing). It must point at the Geist variable: `var(--font-geist-sans)`. Fixed in `globals.css`.

## Phase 1 findings

- **Auth.js was dropped** for Next's documented pattern: a signed httpOnly cookie (`jose`), server actions for login, a data-access layer (`requireSession`) in every page and layout, and `proxy.ts` (Next 16's name for middleware) for optimistic redirects only. The cookie holds user id, company, role and a must-change flag, and no email address. The API re-checks the user on every request, so disabling an account takes effect immediately.
- **One Sheet for the detail panel** (right on desktop, bottom on phones) instead of Sheet plus Drawer.
- **Table from 1024px up, cards below.** At tablet width the 256px sidebar leaves too little room for a table; the content area also needs `min-w-0` or the page scrolls sideways.
- **TanStack Table v9** uses `useTable` and `tableFeatures`, not `useReactTable`. Sorting and paging are server-side (in the URL), so only the core table model is used. TanStack Form 1.33 reads values with `useStore(form.store, ...)`.
- **A link is not a button.** `Button render={<Link/>}` makes Base UI set `role="button"`; navigation uses `<Link className={buttonVariants(...)}>` instead.
- **Reserve space for validation messages** and stop chip buttons stealing focus: an error appearing on blur shifted the layout and swallowed the next click.
- `pnpm typecheck` runs `next typegen` first, because route types (`LayoutProps`) are generated into `.next/`.
- Browser tests use seeded `ready-*` accounts. An earlier helper tried a wrong password on purpose and triggered the 5-attempt lockout, which is a good sign for the lockout and a bad design for the test.
- `/s/*` (the public screening form, Phase 2) must be added as a signed-out route in `proxy.ts` when it is built.
