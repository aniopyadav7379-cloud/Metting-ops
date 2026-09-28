# Data Intel auth re-point — `centerdeep-keycloak` → `auth.unicorncommander.ai`

2026-05-29 — Center Deep brand is archived but `dataintel-backend` +
`dataintel-frontend` survive (Aaron actively uses dataintel). They
currently authenticate against the standalone `centerdeep-keycloak`
container on the centerdeep VPS. With the Center Deep brand archived,
the cleaner architecture is for dataintel to share SSO with the rest
of the Unicorn Commander ecosystem at `auth.unicorncommander.ai`
(commander `uchub-keycloak`, realm `uchub`).

This document captures the migration so `centerdeep-keycloak` can
retire after dataintel cleanly logs in via the shared SSO.

## Current state (2026-05-29)

- `dataintel-frontend` (nginx static, 8502→80)
- `dataintel-backend` (8501)
- Auth via `centerdeep-keycloak` (own realm, own user DB)
- Users: Aaron + possibly Shafen

## Target state

- `dataintel-frontend` + `dataintel-backend` authenticate via
  `auth.unicorncommander.ai` (uchub realm)
- New OIDC client `dataintel-prod` on the uchub realm
- `centerdeep-keycloak` container + `keycloak_centerdeep` DB retired
- Aaron + Shafen log in once at the unified SSO and reach dataintel
  (and Meeting-Ops, and any future ecosystem app) from the same session

## Migration sequence

### 1. Add `dataintel-prod` OIDC client to commander uchub-keycloak

In Keycloak admin at `https://auth.unicorncommander.ai/admin/master/console/`:

1. Realm: `uchub`
2. New client:
   - **Client ID**: `dataintel-prod`
   - **Client authentication**: ON
   - **Valid redirect URIs**:
     - `https://verify.centerdeep.online/oauth2/callback`
     - `https://verify.centerdeep.online/*` (broader fallback)
   - **Web origins**: `https://verify.centerdeep.online`
3. Save → Credentials tab → copy client secret to a secure note.

### 2. Confirm dataintel's auth integration mode

Two possibilities for how dataintel currently terminates auth:

**Option A — oauth2-proxy sidecar** (most likely given other UC apps):
- Update the `dataintel-frontend` oauth2-proxy env:
  ```
  OAUTH2_PROXY_PROVIDER=oidc
  OAUTH2_PROXY_OIDC_ISSUER_URL=https://auth.unicorncommander.ai/realms/uchub
  OAUTH2_PROXY_CLIENT_ID=dataintel-prod
  OAUTH2_PROXY_CLIENT_SECRET=<from step 1>
  OAUTH2_PROXY_REDIRECT_URL=https://verify.centerdeep.online/oauth2/callback
  OAUTH2_PROXY_COOKIE_DOMAIN=.centerdeep.online
  OAUTH2_PROXY_COOKIE_SECRET=<from existing config, or regenerate openssl rand -base64 32>
  ```
- Restart oauth2-proxy container.

**Option B — native OIDC in the backend app**:
- Update the FastAPI/Flask/whatever app config in
  `dataintel-backend` env:
  ```
  KEYCLOAK_URL=https://auth.unicorncommander.ai
  KEYCLOAK_REALM=uchub
  KEYCLOAK_CLIENT_ID=dataintel-prod
  KEYCLOAK_CLIENT_SECRET=<from step 1>
  ```
- Restart the backend container.

Recon to determine which: `ssh centerdeep "docker inspect dataintel-frontend dataintel-backend --format '{{json .Config.Env}}'" | grep -iE "OAUTH2_PROXY|KEYCLOAK|OIDC"`.

### 3. Verify the new flow works

1. Log out of all current dataintel sessions (clear cookies for
   `.centerdeep.online` domain or open a private window).
2. Visit `https://verify.centerdeep.online`.
3. Expected: redirect to `auth.unicorncommander.ai` login. Sign in as
   Aaron (the commander Keycloak account). Should bounce back to
   dataintel with a valid session.
4. Verify the user identity inside dataintel matches what you'd
   expect — if dataintel keys off `preferred_username` and Aaron's
   username on `uchub` realm differs from his username on the old
   `centerdeep-keycloak`, you'll need a user-mapping shim or just
   accept "Aaron" appears as the new identifier. Recommendation:
   key off `uc_uid` (per `feedback_oidc_user_id_canary`) — that's
   stable across realms.

### 4. Repeat for Shafen if applicable

If Shafen has a dataintel account on the old `centerdeep-keycloak` but
not on `uchub`, create him on `uchub` first
(`auth.unicorncommander.ai/admin/master/console/`), then have him log
in. The dataintel app may need to JIT-provision the new identity into
its own user table if it has one.

### 5. Disable the old auth path

In dataintel config:
- Remove or comment out the `OAUTH2_PROXY_OIDC_ISSUER_URL` pointing at
  `centerdeep-keycloak`
- Remove the old `dataintel` client from `centerdeep-keycloak` (admin
  console, before stopping the container)
- Confirm the only auth path is the new uchub-keycloak one

### 6. Stop `centerdeep-keycloak`

Per `docs/centerdeep-archive-state.md` step 13. The
`keycloak_centerdeep` Postgres DB stays for 7-day soak before drop.

## Rollback

If the new auth flow misbehaves:
1. Revert dataintel env to point at `https://verify.centerdeep.online/auth`
   (or wherever the old `centerdeep-keycloak` was reached internally).
2. Re-enable the old client in `centerdeep-keycloak`.
3. Restart dataintel containers.
4. `centerdeep-keycloak` stays running until this is resolved.

The clean revert path exists for 7 days (until the
`keycloak_centerdeep` DB drop). After that, restoration requires the
pg_dump from `/srv/db-backups/centerdeep-archive-2026-06-05/`.

## What this doc does NOT cover

- Migrating dataintel's per-user data (preferences, saved searches,
  etc.) keyed off the old Keycloak `sub`. If dataintel has per-user
  state keyed off `sub`, write a one-shot migration script that maps
  old-sub → new-sub via the user's email or `preferred_username` BEFORE
  step 6. If dataintel only has org-level state, no migration needed.
- Federation between the two Keycloaks during a transitional period.
  We deliberately skip this — it's a hard cutover for one app, one user.

## Related

- [[reference_keycloak_topology]] — canonical SSO is `auth.unicorncommander.ai`
- [[feedback_oidc_user_id_canary]] — use `uc_uid` not `preferred_username`
- [[project_centerdeep_archive_2026_05_29]] — broader archive context
- `docs/centerdeep-archive-state.md` — gates the `centerdeep-keycloak` stop
- `docs/deploy-unicorncommander-vps.md` — references this doc as a precondition
