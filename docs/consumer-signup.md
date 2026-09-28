# Consumer self-serve signup (Option B)

Self-serve email/password signup for the free tier, coexisting with the
existing Keycloak enterprise SSO. The backend already dual-trusts Keycloak
forward-auth headers AND its own HS256 JWT (`auth/dependencies.py`), so
consumer signup is the JWT path; enterprise SSO is untouched.

## What shipped (code complete, v3.2.0)

Backend, all tested (`tests/test_consumer_signup.py`):
- `AuthService.create_user(personal_org=True)` provisions a **private** per-user
  org (`{username}-personal`, user = admin), so consumers never share a
  workspace. New users are `tier="free"` (column default) + `is_verified=False`.
- Self-serve `POST /api/auth/register` (no admin caller) uses `personal_org`
  and sends a verification email (best-effort; a mail failure does not fail
  signup).
- `GET /api/auth/verify-email?token=…` flips `is_verified` and redirects to
  `{APP_BASE_URL}/#/login?verify=success|invalid`.
- `POST /api/auth/resend-verification` — generic 200 (no account enumeration).
- `auth/email.py` — transactional email via `auth_config` SMTP; **logs the
  link and returns False if SMTP is unconfigured** (never 500s a signup).
- Email-verification tokens are type-isolated from password-reset tokens.

Already existed (not rebuilt): account lockout (`authenticate_user` →
`failed_login_attempts` / `locked_until` / `MAX_LOGIN_ATTEMPTS`), free-tier
default (`User.tier`), and the SSO new-user personal-org convention.

**This is INERT until activated.** With `ALLOW_REGISTRATION=false` (current
default) `/register` still requires an admin, and the new endpoints sit
behind oauth2-proxy (not in the skip list), so anonymous users can't reach
them. Deploying the code changes nothing for current users.

## Activation checklist (gated — flip when ready to launch free tier)

1. **Set a real `SECRET_KEY`** in `deploy/bigboy/.env.bigboy` (it currently
   defaults to a placeholder in `auth/config.py`). HS256 consumer JWTs are
   only as safe as this secret. **Do this first.**
2. `ALLOW_REGISTRATION=true` in `.env.bigboy`.
3. `APP_BASE_URL=https://meetingops.magicunicorn.dev` (used to build email
   links; default already correct for prod).
4. SMTP via Postmark: `SMTP_HOST=smtp.postmarkapp.com`, `SMTP_PORT=587`,
   `SMTP_USER` / `SMTP_PASSWORD` (Postmark server token), `SMTP_FROM_EMAIL`
   (a verified Postmark sender). Without these, verification emails only log.
5. **oauth2-proxy** (`deploy/bigboy/docker-compose.bigboy.yml`,
   `OAUTH2_PROXY_SKIP_AUTH_ROUTES`): add the public auth + signup routes
   (regex, `$`→`$$`):
   `^/api/auth/register$$`, `^/api/auth/login$$`, `^/api/auth/refresh$$`,
   `^/api/auth/verify-email$$`, `^/api/auth/resend-verification$$`, and the
   SPA signup/login routes (`^/signup`, `^/login`) + their assets. Verify a
   logged-out visitor can load the signup page without a Keycloak bounce.
6. **Rate limiting** on the now-public `/register` + `/login` + `/resend-…`
   — add a Traefik rate-limit middleware (per-IP) in front of these. Account
   lockout covers per-account brute force; this covers signup-spam / IP abuse.
   Consider a CAPTCHA on `/register`.
7. **Frontend (not built yet):** a `/signup` page (email/password → `POST
   /api/auth/login` after register, store JWT), a `/login` page that keeps a
   "Sign in with SSO" button (existing Keycloak flow), and handling of the
   `?verify=success|invalid` query on login. The free-tier upgrade prompt
   already links to `/pricing` (also not built).

## Still to do before free-tier launch
- Frontend signup/login pages (#7 above) + the `/pricing` page.
- Password reset (forgot/reset) endpoints — the token helper pattern is in
  place (`generate/verify_password_reset_token` + `send_password_reset_email`);
  the endpoints aren't wired yet.
- Decide whether to hard-gate any features on `is_verified` (today nothing
  blocks an unverified free user — fine for browser-only, revisit for Pro).
- On-device transcript-quality validation (the voice test) — the free-tier
  product gate.
