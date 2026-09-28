# Image Registry Decision (Issue #98)

**Date:** 2026-05-20
**Status:** Decision — implementation deferred until next painful speaker-svc deploy
**Recommendation:** Forgejo container registry (already enabled, zero new infra)

## Current state — the scp dance

Every speaker-svc update on midboy2 requires:

```bash
# on bigboy
docker save meet-speaker-svc:local | gzip > /tmp/speaker-svc.tar.gz   # ~5.8GB
scp /tmp/speaker-svc.tar.gz midboy2:/tmp/

# on midboy2
gunzip -c /tmp/speaker-svc.tar.gz | docker load
```

- Image size: 10.3GB uncompressed, 5.8GB gzipped on the wire
- Network: bigboy↔midboy2 is Tailscale, typically 200-400 Mbps real throughput
- Transfer wall time: ~3-5 minutes per deploy
- Operator overhead: hand-driven, easy to forget steps, no audit trail
- Failure mode: a botched `docker load` can leave midboy2 on the old image with no rollback target

This is fine for a one-off; it's annoying by the 3rd time.

## Options assessed

### Option 1 — Forgejo container registry (RECOMMENDED)

**What it is.** Forgejo ships an OCI-compatible container registry as part of its built-in Packages feature. Our existing `unicorn-forgejo` container at `git.unicorncommander.ai` exposes it at `/v2/`.

**Status today.**
- Probed `https://git.unicorncommander.ai/v2/` → returns `HTTP 401` (= registry online, auth required — correct OCI behavior)
- Storage path: `/data/gitea/packages` inside the container (4KB used, no images yet)
- No `[packages]` block in app.ini → Forgejo defaults apply (ENABLED, no size cap)

**Pros.**
- Zero new infrastructure — already running, already TLS-terminated, already in DNS
- Auth via existing Forgejo Personal Access Tokens (scope: `write:package` / `read:package`)
- Per-org namespacing: `git.unicorncommander.ai/unicorncommander/uc-meeting-ops-speaker-svc:latest`
- Garbage collection + UI for browsing tags built in
- Pulls work over Tailscale (Forgejo is reachable on the tailnet too)
- Audit trail: package downloads logged

**Cons.**
- Forgejo on bigboy = single point of failure for image pulls (acceptable; bigboy goes down means everything is sad anyway)
- Storage on bigboy disk (not Garage) — fine at our scale, would matter at 100+ images
- Public DNS (`git.unicorncommander.ai`) so we should keep images on private repos or use tailnet-only DNS for pulls

**Effort.** ~15 minutes:
1. Create Forgejo PAT with `write:package` scope (web UI)
2. `docker login git.unicorncommander.ai -u aaron -p <pat>` on bigboy AND midboy2
3. `docker tag meet-speaker-svc:local git.unicorncommander.ai/unicorncommander/uc-meeting-ops-speaker-svc:v$(date +%Y%m%d)`
4. `docker push git.unicorncommander.ai/...`
5. On midboy2: `docker pull git.unicorncommander.ai/unicorncommander/uc-meeting-ops-speaker-svc:v...`
6. Update midboy2's compose to reference the registry tag instead of `meet-speaker-svc:local`

### Option 2 — Garage-backed standalone registry

Spin up `docker/registry:2` on bigboy with the S3 driver pointing at unicorn-garage.

**Pros.**
- Storage on Garage (federated, dedup-friendly)
- Decoupled from Forgejo lifecycle

**Cons.**
- Another service to maintain (TLS, auth proxy, GC config, monitoring)
- Re-implements what Forgejo already gives us
- Auth story is awkward (htpasswd or third-party SSO bridge)

**Effort.** 1-2 hours plus ongoing maintenance.

### Option 3 — Keep scp

**Pros.**
- Zero new state, zero new auth, no new failure modes
- Works offline of Forgejo

**Cons.**
- Already documented as annoying (issue #98)
- Will get worse as the model grows or as more services need cross-host image moves

### Option 4 — Headscale-tunneled local registry on bigboy

`docker/registry:2` on bigboy bound to the tailnet IP only, filesystem storage at `/var/lib/registry`.

**Pros.**
- Tailnet-only by design (no public TLS to manage)
- Simpler than Forgejo (no packages model, no UI, no users)

**Cons.**
- Still a new service to maintain
- No UI to inspect what's pushed
- Forgejo already does this with more features for the same effort

**Effort.** 30 minutes.

## Recommendation: Option 1 (Forgejo container registry)

Already enabled, already authenticated against our SSO-adjacent identity surface, costs us nothing to start using. Option 2 and Option 4 are both "build a thing Forgejo already built." Option 3 is the status quo we want to stop doing.

If Forgejo's storage on bigboy ever becomes a problem (>50GB of images), revisit and either (a) point Forgejo packages at Garage via S3 storage driver, or (b) graduate to Option 2.

## Implementation plan (when ready)

```bash
# 1. One-time setup on bigboy
docker login git.unicorncommander.ai -u aaron
#   → use a Forgejo PAT with scope: write:package, read:package
#   → save creds to ~/.docker/config.json (chmod 600)

# 2. One-time setup on midboy2 (read-only pulls)
ssh midboy2 'docker login git.unicorncommander.ai -u aaron'
#   → use a separate PAT with scope: read:package

# 3. Per-deploy on bigboy
TAG=v$(date +%Y%m%d-%H%M)
docker tag meet-speaker-svc:local \
  git.unicorncommander.ai/unicorncommander/uc-meeting-ops-speaker-svc:$TAG
docker tag meet-speaker-svc:local \
  git.unicorncommander.ai/unicorncommander/uc-meeting-ops-speaker-svc:latest
docker push git.unicorncommander.ai/unicorncommander/uc-meeting-ops-speaker-svc:$TAG
docker push git.unicorncommander.ai/unicorncommander/uc-meeting-ops-speaker-svc:latest

# 4. Per-deploy on midboy2
ssh midboy2 'docker pull git.unicorncommander.ai/unicorncommander/uc-meeting-ops-speaker-svc:latest'
# update midboy2 compose to reference the registry tag, then:
ssh midboy2 'cd /path/to/midboy2/compose && docker compose up -d --force-recreate speaker-svc'
```

## When to execute

**Trigger:** The next time we need to ship speaker-svc to midboy2 AND someone notices the scp dance is annoying. Don't pre-build the workflow — we're paying for our own deferral, and the work itself is cheap (~15 min) when it's actually needed.

**Don't trigger on:** A single speaker-svc deploy. The setup overhead is non-zero (PAT, login on two hosts, compose edit on midboy2). It pays off at deploy #2 or #3.

## Open questions

- Should we use `git.unicorncommander.ai` (public Cloudflare-fronted) or a tailnet-only Forgejo DNS for pulls? Public is simpler; tailnet-only is more paranoid. Default to public until there's a real reason to lock it down.
- Should non-meeting-ops services use the same registry? Yes — single registry per org is cleaner. Reserve `git.unicorncommander.ai/unicorncommander/*` for org images.
- Garbage collection cadence? Forgejo defaults are reasonable; revisit only if `/data/gitea/packages` grows beyond ~50GB.
