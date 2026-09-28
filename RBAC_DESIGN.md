# Meeting-Ops RBAC + Invitations Design

Status: design only. Implementation pending.

## Goals

1. Org admins can invite people to their meeting-ops org.
2. The same Keycloak (uchub) account can belong to multiple orgs and switch between them.
3. A single recording can be shared with an external email via a magic link, without granting full org membership.
4. Federation across separate UC instances (e.g. magicunicorn.dev ↔ genesisflowlabs.com) is **out of scope** for v1. Flagged for a future design pass; will require federation tokens and cross-realm trust.

## Identity model (no change)

Keycloak `uchub` realm remains the identity provider. Every meeting-ops user already has a Keycloak account (oauth2-proxy enforces this on every request). Org membership in meeting-ops is the *authorization* layer on top of that identity. App-level roles here (Admin / Manager / Member / Viewer) are **independent** of the platform-wide Unicorn Commander admin role (which lives in Keycloak realm-roles and applies to ops-center / brigade, not the apps).

## Roles (4 tiers, matches existing `user_organizations.role` column)

| Role    | Can do                                                            |
| ------- | ----------------------------------------------------------------- |
| Admin   | Everything: invite, remove members, change roles, delete org      |
| Manager | Invite Members + Viewers (not Managers / Admins), edit any session |
| Member  | Create + edit own sessions, view all org sessions                 |
| Viewer  | Read-only on all org sessions                                     |

`user_organizations.role` already accepts these strings. No migration needed for the column itself.

## Schema changes

### Invitation table (new)

```sql
CREATE TABLE invitations (
  id              SERIAL PRIMARY KEY,
  organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  email           CITEXT NOT NULL,
  role            VARCHAR(20) NOT NULL,  -- admin | manager | member | viewer
  token           UUID NOT NULL UNIQUE,
  invited_by_user_id INTEGER REFERENCES users(id),
  expires_at      TIMESTAMPTZ NOT NULL,  -- default now() + 7 days
  accepted_at     TIMESTAMPTZ,
  accepted_by_user_id INTEGER REFERENCES users(id),
  revoked_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (organization_id, email)  -- one pending invite per email per org
);
CREATE INDEX idx_invitations_token ON invitations (token) WHERE accepted_at IS NULL AND revoked_at IS NULL;
```

### Session share table (new, for magic-link sharing)

```sql
CREATE TABLE session_shares (
  id              SERIAL PRIMARY KEY,
  session_id      INTEGER NOT NULL REFERENCES recording_sessions(id) ON DELETE CASCADE,
  organization_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  shared_with_email CITEXT,         -- nullable for fully-public links
  token           UUID NOT NULL UNIQUE,
  access_level    VARCHAR(20) NOT NULL DEFAULT 'read',  -- read | comment
  created_by_user_id INTEGER NOT NULL REFERENCES users(id),
  expires_at      TIMESTAMPTZ,      -- nullable = never expires
  revoked_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_session_shares_token ON session_shares (token) WHERE revoked_at IS NULL;
```

## Endpoints

### Invitations

| Method | Path                                              | Who | Purpose                                            |
| ------ | ------------------------------------------------- | --- | -------------------------------------------------- |
| POST   | `/api/orgs/{slug}/invitations`                    | Admin / Manager | Send invite (email, role)                     |
| GET    | `/api/orgs/{slug}/invitations`                    | Admin / Manager | List pending invites                          |
| DELETE | `/api/orgs/{slug}/invitations/{id}`               | Admin / Manager (or inviter)  | Revoke a pending invite          |
| GET    | `/api/invitations/{token}`                        | Public          | View invitation (org name, role) before accept |
| POST   | `/api/invitations/{token}/accept`                 | Authenticated   | Accept invite, creates UserOrganization row    |

Manager role-restriction enforced in handler: Manager can only invite Member / Viewer roles, not other Managers or Admins.

### Members

| Method | Path                                              | Who | Purpose                                            |
| ------ | ------------------------------------------------- | --- | -------------------------------------------------- |
| GET    | `/api/orgs/{slug}/members`                        | Any member | List members + roles                          |
| PATCH  | `/api/orgs/{slug}/members/{user_id}`              | Admin      | Change role                                   |
| DELETE | `/api/orgs/{slug}/members/{user_id}`              | Admin      | Remove member                                 |

Admins cannot remove the last Admin. Admins cannot downgrade themselves below Admin without another Admin's blessing.

### Org switching (multi-org user)

| Method | Path                                              | Who | Purpose                                            |
| ------ | ------------------------------------------------- | --- | -------------------------------------------------- |
| GET    | `/api/auth/orgs`                                  | Authenticated | List orgs the current user belongs to       |
| POST   | `/api/auth/orgs/{slug}/activate`                  | Authenticated | Set active org for this session (cookie)    |

Currently meeting-ops gets active org from `X-MeetingOps-Org` header or `?org=` query. The activation endpoint just writes it to a cookie so subsequent requests don't need to pass it manually. Frontend gets an org-switcher dropdown in the header.

### Session shares

| Method | Path                                              | Who | Purpose                                            |
| ------ | ------------------------------------------------- | --- | -------------------------------------------------- |
| POST   | `/api/simple/recording-sessions/{id}/shares`      | Admin / Manager / owner | Create magic link                  |
| GET    | `/api/simple/recording-sessions/{id}/shares`      | Admin / Manager / owner | List active shares                  |
| DELETE | `/api/simple/recording-sessions/{id}/shares/{id}` | Admin / Manager / owner | Revoke a share                      |
| GET    | `/api/shares/{token}`                              | Public                 | View shared session                  |

Shared session view is its own simplified page (read-only, no AI Chat, no Re-process). Auth bypass via the token. If `shared_with_email` is set, the magic link login flow requires that email in Keycloak (still uses SSO, just gates by email match).

## Email delivery

Postmark transactional server. Templates:
- `invitation.created` — "You've been invited to {org_name} on Meeting-Ops" with the accept URL
- `share.created`      — "{user_name} shared a meeting with you" with the view URL
- `member.added`       — confirmation to inviter
- `role.changed`       — heads-up to the member when their role changes

## RBAC enforcement

Per-endpoint dependency `Depends(require_role("admin"))` / `require_role(["admin", "manager"])`. Implementation: read current_user + active_org, look up `user_organizations.role`, raise 403 if not in allowed set. Centralize so the role list is in one place per endpoint.

## Frontend

- New page `/settings/members` — list members, pending invites, role dropdowns, remove button
- New page `/settings/profile` — your active org switcher
- New header dropdown — org switcher (when user belongs to >1 org)
- SessionDetails header — "Share" button → modal with email input + role picker + copy-link
- Login flow — if user has a pending invitation matching their email, auto-accept on first login

## Rollout plan

Phase 1: invitations + members + role enforcement. Get the simple internal-org case working end-to-end.
Phase 2: session share magic links.
Phase 3: multi-org switching UI (the backend already supports the cookie + header pattern).
Phase 4 (future): federation across UC instances.

## Audit

Every role change, invitation, acceptance, share creation, and share access lands in `audit_logs` (already exists).

## Open questions

- Do we want a self-serve "request to join" flow (user finds an org by slug and asks the admin)? Or strictly invite-only? Defaulting to invite-only for v1.
- For the magic link, do anonymous viewers need to log in at all, or is the token alone sufficient? Recommend: require any logged-in user (cheap login wall via Keycloak) — easier to revoke and audit who actually viewed.
- Org deletion: hard-delete with cascade, or soft-delete with retention? Recommend soft-delete (`is_active = false`) plus a separate Admin-only hard-delete with a confirmation token.
