# Workspaces: two-level model (Org tenant + Spaces within)

Status: **DESIGN / proposed** (decided 2026-06-08; not yet built). Owner: Aaron.

## Why
Today "workspace" == Organization == tenant. That's the right isolation primitive for the
B2B suite (a client/company's meetings, speakers, contacts, KG, billing are fully separated,
and the whole UC suite federates on `organization` via Brigade). But the product treats every
user as multi-tenant with an always-on switcher and no good "organize within my org" story —
so a single business that wants to separate **divisions/teams/projects** has to abuse separate
*orgs*, which silos the shared things (speaker library, contacts) that should span the business.

Decision: keep the tenant, **add a lightweight second level inside it.**

## Model
1. **Organization = tenant** (unchanged). Isolation + billing + membership. A user may have a
   personal org and belong to business org(s). Separate companies/clients = separate orgs.
2. **Space (a.k.a. Workspace/Division) — INSIDE an org.** A lightweight grouping of meetings.
   **Not** an isolation boundary.

### Where data lives (the key choice)
- **Org level (shared across all Spaces in the org):** speaker library (`SpeakerProfile`),
  contacts, knowledge graph, billing/plan, members, vocabulary, provider settings.
- **Space level:** just the grouping of **meetings** (recording sessions) + per-space defaults
  (e.g. default summary template). A meeting belongs to exactly one Space; it can be moved
  between Spaces within the same org.
- Net: divisions of one business **share the people library + contacts + KG**, they only
  organize the meetings. This is exactly what fixes the "my speakers disappeared in another
  workspace" problem.

## Tiering — all-or-nothing, no granular gates
Meeting-Ops stays **free = browser-only / on-device** vs **paid = the full server completion
pass** (one capability line: `canonical_reprocess`/`qwen36_summary`). Do **not** add per-feature
gates. Spaces and orgs are cheap (shared infra, `org:<id>` prefixing), so:
- **Spaces per org: unlimited, every tier.**
- **Orgs: unlimited too** — but each org carries **its own plan**. A free org is browser-only
  ($0 to us); server features require *that org* to be on a paid plan. Cost is always covered
  without count caps (soft anti-abuse cap ~25). Upsell = per-org plan, not a workspace count.

## Build outline
**Data model**
- New table `space` (`id`, `organization_id` FK, `name`, `slug` unique-per-org, `created_by`,
  `is_default`, `created_at`). One auto-created **"General"** space per existing org on migration.
- `recording_sessions.space_id` FK (nullable → backfill to the org's General space).
- (Later, optional) space-level ACL if divisions need member restrictions; v1 = all org members
  see all spaces.

**Scoping**
- Org scoping stays the security boundary (unchanged — `X-MeetingOps-Org` / `?org=`).
- Space is an **additional filter** on the session list/queries only (`space_id`), never a
  security boundary. Speaker/contact/KG queries stay **org-scoped** (do NOT add space to them).

**API**
- Space CRUD under the active org: `GET/POST /api/spaces`, `PATCH/DELETE /api/spaces/{id}`.
- Session list/search accept an optional `space_id` filter; create-session takes a `space_id`.
- `POST /api/sessions/{id}/move-space` (within-org move; reuses the org-move guardrails).

**Frontend**
- Two-level selector in the nav: **Org switcher** (tenant) → **Space switcher/filter** (within).
- Sessions page filters by Space; a "Move to space" action on a session.
- Reuse the existing org context; add a parallel (lighter) `activeSpace` that only filters
  meetings.

## Prereqs / cleanups to do first (these also fix today's bugs)
1. **Per-user primary/sticky org** (`UserOrganization.is_default` or `User.default_organization_id`)
   so both the frontend default and the backend no-selector fallback resolve to the user's home
   org — replaces the v3.29.4 dogfood-specific `DEFAULT_ORG_SLUG` preference and works on the
   customer node too.
2. **Consolidate org-context to ONE fetch interceptor** (today there are two `window.fetch`
   wraps — `AuthContext` org-injection + `installFetchInterceptor` 401-handling — which is
   order-fragile; the org header can fail to ship). One interceptor that always attaches the
   active org + handles 401.

## Out of scope / non-goals
- Spaces are not tenants: no separate billing, no cross-org data, no separate speaker library.
- Brigade/Contact-Ops federation stays **org-level** (spaces are below the federation boundary).
