# Meeting-Ops production deploy runbook — `meeting-ops.unicorncommander.ai`

Provisioning runbook for the centerdeep VPS (Hostinger KVM8 — 8 vCPU AMD
EPYC 9354P, 31 GiB RAM, 387 GB disk; SSH alias `centerdeep`, Tailscale
IP `<vps-host>`).

This runbook is **manual + idempotent + step-by-step**. Each step lists
the precondition, the command, and a verification command. Stop at any
step that fails — do not "patch around" a failure.

See companion docs:
- `docs/centerdeep-archive-state.md` — what containers get stopped before this work
- `docs/dataintel-auth-repoint.md` — auth migration for the surviving dataintel app
- `deploy/unicorncommander/docker-compose.unicorncommander.yml` — the prod compose
- `deploy/unicorncommander/.env.unicorncommander.example` — secrets template

## Pre-flight (verify before touching anything)

1. **v3.18.x merged + tagged on origin** (current minimum = `v3.18.2`).
2. **Forgejo registry image published** at
   `registry.unicorncommander.ai/unicorn/meet-backend:v3.18.x`.
   If CI is not yet wired, manually build + push:
   ```bash
   cd /Users/aaronstransky/UC-Meeting-Ops-bigboy
   docker buildx build --platform linux/amd64 -t \
     registry.unicorncommander.ai/unicorn/meet-backend:v3.18.2 \
     --push backend/
   ```
3. **Aaron has reviewed** this runbook and pasted the `.env.unicorncommander.example`
   into `.env.unicorncommander` with secrets filled in.

## Step 1 — Archive Center Deep stack (reversible)

Follow `docs/centerdeep-archive-state.md`. Stop the archived containers;
their volumes persist for revert. Confirm `unicorn-redis`,
`unicorn-qdrant`, `unicorn-postgresql`, observability stack, Traefik,
and `dataintel-*` (after auth re-point) remain UP.

Verify:
```bash
ssh centerdeep "docker ps --format '{{.Names}}' | sort"
```
Should show ~10-12 containers (down from ~30), all healthy.

## Step 2 — Stand up dedicated Postgres + Garage

Working dir on VPS: `/srv/UC-Meeting-Ops-bigboy/` (clone the repo
there if it doesn't exist).

```bash
ssh centerdeep
cd /srv
git clone https://aaron:<token>@git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops.git UC-Meeting-Ops-bigboy
cd UC-Meeting-Ops-bigboy
git fetch --tags
git checkout v3.18.2
```

Paste the prepared `.env.unicorncommander` into
`deploy/unicorncommander/.env.unicorncommander` (NOT committed).

Bring up Postgres + Garage first:
```bash
docker compose \
  --env-file deploy/unicorncommander/.env.unicorncommander \
  -f deploy/unicorncommander/docker-compose.unicorncommander.yml \
  up -d meet-postgres meet-garage
```

Verify:
```bash
docker compose -f deploy/unicorncommander/docker-compose.unicorncommander.yml \
  ps meet-postgres meet-garage
# both should be "healthy" within ~30s
```

## Step 3 — Bootstrap Garage layout + buckets

Garage requires a one-time layout assignment for a single-node setup.

```bash
docker exec -it meet-garage /garage status
# Note the node ID (long hex). Apply layout:
docker exec -it meet-garage /garage layout assign -z dc1 -c 100G <node-id>
docker exec -it meet-garage /garage layout apply --version 1
# Create the two buckets:
docker exec -it meet-garage /garage bucket create meeting-ops-prod-audio
docker exec -it meet-garage /garage bucket create meeting-ops-prod-attachments
# Mint the access key the backend will use:
docker exec -it meet-garage /garage key create meet-backend
# Note the access_key + secret_key from the output — paste them into
# .env.unicorncommander as GARAGE_ACCESS_KEY / GARAGE_SECRET_KEY.
# Grant the key full access to both buckets:
docker exec -it meet-garage /garage bucket allow \
  --read --write --owner meeting-ops-prod-audio --key meet-backend
docker exec -it meet-garage /garage bucket allow \
  --read --write --owner meeting-ops-prod-attachments --key meet-backend
```

Verify:
```bash
docker exec -it meet-garage /garage bucket list
# Should show both meeting-ops-prod-* buckets.
```

## Step 4 — Add `meeting-ops-prod` OIDC client to commander Keycloak

On commander (`ssh commander`):

1. Open Keycloak admin at `https://auth.unicorncommander.ai/admin/master/console/`.
2. Select realm `uchub`.
3. Create new client:
   - **Client ID**: `meeting-ops-prod`
   - **Client authentication**: ON (confidential)
   - **Valid redirect URIs**:
     `https://meeting-ops.unicorncommander.ai/*`
     `https://meetingops.unicorncommander.ai/*`
   - **Web origins**: same two URLs
   - **Login theme**: per existing uchub convention
4. After saving, go to **Credentials** tab → copy the client secret.
   Paste it into `.env.unicorncommander` as `KEYCLOAK_CLIENT_SECRET=`.
5. Confirm the realm-level `uc_uid` SPI is enabled (it should already
   be per `feedback_oidc_user_id_canary`).

Verify on the VPS:
```bash
curl -s https://auth.unicorncommander.ai/realms/uchub/.well-known/openid-configuration \
  | jq '.issuer, .authorization_endpoint, .token_endpoint'
```

## Step 5 — DNS + Cloudflare

In Cloudflare (manual, no IaC yet):

1. A record: `meeting-ops` → centerdeep VPS public IP (proxy ON,
   orange-cloud).
2. A record: `meetingops` → same IP (proxy ON).
3. Wait ~30 seconds for propagation.

Verify:
```bash
dig +short meeting-ops.unicorncommander.ai
dig +short meetingops.unicorncommander.ai
```

Both should resolve to Cloudflare's edge IPs.

## Step 6 — Boot the backend + worker

```bash
docker compose \
  --env-file deploy/unicorncommander/.env.unicorncommander \
  -f deploy/unicorncommander/docker-compose.unicorncommander.yml \
  up -d meet-backend meet-bulk-import-worker
```

Watch logs for the first 60 seconds:
```bash
docker logs -f meet-backend
# Look for "Application startup complete" and no migration errors.
```

Verify Traefik picked up the routes:
```bash
ssh centerdeep "curl -s http://localhost:8080/api/http/routers | jq '.[] | select(.rule | contains(\"meeting-ops\")) | {name, rule, status}'"
```

Verify the public URL terminates TLS + returns the app:
```bash
curl -fsS https://meeting-ops.unicorncommander.ai/health
# Expect 200 with router status JSON
```

Verify the redirect:
```bash
curl -sI https://meetingops.unicorncommander.ai/ | grep -E "^(HTTP|location)"
# Expect 301 → https://meeting-ops.unicorncommander.ai/
```

## Step 7 — Invite-only landing (until v3.19 UX rebuild ships)

`ALLOW_REGISTRATION=false` keeps the signup endpoint inert. Until the
UX rebuild + Stripe integration land, the frontend `/signup` page
should be replaced with a static "Private beta — request invite" card.

This is a frontend change shipped in a follow-up release; until then
the prod URL serves the app behind login, and the only path to a new
account is Aaron creating it in commander Keycloak directly.

## Step 8 — Smoke test (Aaron + Shafen)

1. Aaron logs in via `auth.unicorncommander.ai` → `meeting-ops.unicorncommander.ai/dashboard`.
2. Record a 60-second test meeting.
3. Verify completion pass runs (server STT + summary).
4. Verify the audio chunk landed in Garage:
   ```bash
   docker exec -it meet-garage /garage bucket info meeting-ops-prod-audio
   # Should show non-zero object count after stop+finalize.
   ```
5. Same flow for Shafen.

## Step 9 — Stripe LIVE testing (small group, real $12)

After Step 8 passes, switch to Stripe live mode:
1. Confirm `STRIPE_LIVE_*` env vars are populated.
2. Subscribe Aaron, Shafen, 2-3 invited friends.
3. Verify webhooks fire end-to-end (subscription created → tier upgrade
   reflected in `User.tier` → upgrade banner clears).
4. Issue refunds when done if the testers want.

## Step 10 — Public soft launch

When 2-3 weeks of internal dogfooding has passed without incident:
1. Flip frontend landing from invite-only to public signup.
2. Set `ALLOW_REGISTRATION=true`.
3. Restart `meet-backend` to pick up the env change:
   ```bash
   docker compose -f deploy/unicorncommander/docker-compose.unicorncommander.yml \
     up -d --force-recreate meet-backend
   ```
4. Announce via the channels in `project_meeting_ops_competitive_landscape`.

## Rollback (any step)

Every step has a single rollback action:
- Step 1: see `docs/centerdeep-archive-state.md` for the revert command
  per container.
- Step 2-3: `docker compose -f deploy/unicorncommander/docker-compose.unicorncommander.yml down` (volumes preserved).
- Step 4: delete the `meeting-ops-prod` Keycloak client.
- Step 5: lower DNS TTL first, then point A record elsewhere (or
  `cloudflare-cli` rule deletion).
- Step 6+: `docker compose ... stop meet-backend meet-bulk-import-worker`.

## Operational notes

- **Image upgrades**: edit `MEET_IMAGE_TAG` in `.env.unicorncommander`,
  then `docker compose ... up -d meet-backend meet-bulk-import-worker`.
  Postgres and Garage stay running.
- **Logs**: shipped to the existing centerdeep Loki via promtail. Search
  in Grafana at `https://centerdeep.online/grafana/` (label
  `container_name=~"meet-.*"`).
- **Metrics**: cAdvisor + node-exporter on centerdeep already scrape
  Meeting-Ops containers automatically. No additional setup.
- **Backups**: nightly `pg_dump` of `meet_db` to
  `/srv/db-backups/meet-prod/` via the existing centerdeep cron.
  Garage object storage uses local disk; consider rclone-to-bigboy as a
  v1.5 enhancement.

## What this runbook does NOT cover

- Stripe pricing page + signup UX (v3.19 / Stripe activation work).
- Per-org integration toggles UI (v3.19+ frontend work).
- Founders 100 mechanic (separate spec).
- Bigboy `meetingops.magicunicorn.dev` deploys (continues as the
  internal dev surface, untouched).

See PO P-00055 task `622e44b8-6f9e-4f5c-aa66-d4701cb9794f` for the
full v3.18-v3.19+launch sequencing.
# Brigade tenant isolation

Multi-tenant deployments must use `BRIGADE_TENANCY_MODE=per_org_graph`, the
default in the application and customer compose. This stores each workspace in
a separate FalkorDB graph. The `shared` mode is reserved for genuinely
single-tenant appliances and must not be used for SaaS/customer-hosting nodes.
