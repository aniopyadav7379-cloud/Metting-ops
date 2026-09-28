# Center Deep archive state — centerdeep VPS

2026-05-29 — Center Deep brand archived per
[[project_centerdeep_archive_2026_05_29]]. No customers were on the
standalone `centerdeep.online` ecosystem; the brand name is preserved
for packaged Unicorn Commander apps in the search/data/analytics families.

This document records what gets stopped, when, and the exact revert
command per container so a future session can resurrect any piece
without grepping git history.

## Containers to STOP (in this order, on `ssh centerdeep`)

Each `docker stop` preserves the volume — the container can be
restarted with `docker start` as long as the docker daemon hasn't
been wiped.

### Order matters because Keycloak goes after dataintel auth re-point

| # | Container | Volume | Revert command |
|---|---|---|---|
| 1 | `bizint_frontend` | (none) | `docker start bizint_frontend` |
| 2 | `bizint_backend` | (none) | `docker start bizint_backend` |
| 3 | `executive-brief-frontend` | (none) | `docker start executive-brief-frontend` |
| 4 | `centerdeep-ai` | (none) | `docker start centerdeep-ai` |
| 5 | `center-deep-tool-academic` | (none) | `docker start center-deep-tool-academic` |
| 6 | `center-deep-tool-deep-search` | (none) | `docker start center-deep-tool-deep-search` |
| 7 | `center-deep-tool-search` | (none) | `docker start center-deep-tool-search` |
| 8 | `center-deep-tool-report` | (none) | `docker start center-deep-tool-report` |
| 9 | `center-deep-search` | (named volumes) | `docker start center-deep-search` |
| 10 | `center-deep-searxng` | (named volumes) | `docker start center-deep-searxng` |
| 11 | `ops-center-centerdeep` | `uc-cloud_*` | `docker start ops-center-centerdeep` |
| 12 | `unicorn-minio` *(after Garage cutover only)* | `uc-cloud_minio_data` (had only `bizint-documents`/4K — empty) | `docker start unicorn-minio` |
| 13 | `centerdeep-keycloak` *(after dataintel auth re-point only)* | named volume + Postgres `keycloak_centerdeep` DB | `docker start centerdeep-keycloak` |

## Containers to KEEP RUNNING

| Container | Why |
|---|---|
| `unicorn-retirement-leads-api` / `-ui` | Aaron keeping live; revival work tracked at P-00056 |
| `mandatemap-frontend` | Aaron keeping live; revival work tracked at P-00057 |
| (any `loopnet-*` if present) | Polish + rename work tracked at P-00058 (takedown notice triggers rename before any public use) |
| `unicorn-postgresql` | Shared DB host for retirement_leads, dataintel_db, woodpecker_db, forgejo_db, unicorn_db, bizint_db (still has data — drop after soak per Step 5) |
| `unicorn-redis` | Shared infra; Meeting-Ops will use db 9 + 10 |
| `unicorn-qdrant` | Shared vector store; Meeting-Ops uses `meet_prod_*` collection prefix |
| `unicorn-tika` | OCR — keep, harmless |
| `dataintel-backend` + `dataintel-frontend` | Aaron actively using; auth re-point per `docs/dataintel-auth-repoint.md` lets `centerdeep-keycloak` retire |
| `unicorn-woodpecker-server` | CI — keep |
| `traefik` | Termination layer for everything |
| Observability stack (`centerdeep-prometheus`, `-grafana`, `-loki`, `-promtail`, `-cadvisor`, `-alertmanager`, `-node-exporter`) | Canonical observability per `project_observability_canonical_centerdeep` |

## Database drops — DEFERRED 7 days after container stop

Container stop is fully reversible. Database drops are NOT. The
sequence is:

1. **2026-05-29**: stop archived containers (this doc, steps 1-13).
2. **+7 days**: take a final `pg_dump` of each candidate DB and save
   under `/srv/db-backups/centerdeep-archive-2026-06-05/`.
3. **+7 days**: drop databases (separate session, separate confirmation).

### Confirmed-drop DBs (after soak)

| DB | Last size | Drop command (when ready) |
|---|---|---|
| `bizint_db` | 10 MB | `DROP DATABASE bizint_db;` |
| `centerdeep_db` | 11 MB | `DROP DATABASE centerdeep_db;` |
| `keycloak_centerdeep` | 13 MB | `DROP DATABASE keycloak_centerdeep;` (after `centerdeep-keycloak` container itself is stopped) |

### KEEP (do not drop)

| DB | Active connections 2026-05-29 | Why |
|---|---|---|
| `retirement_leads` (2.2 GB) | 1 | Aaron keeping (P-00056) |
| `ca_retirement_leads` (107 MB) | (idle) | Aaron keeping |
| `loopnet_leads_db` (22 MB) | (idle) | Aaron keeping (P-00058 rename) |
| `dataintel_db` (92 MB) | (idle) | Aaron actively using |
| `woodpecker_db` (24 MB) | 2 | CI |
| `forgejo_db` (15 MB) | 1 | Active connection — investigate before drop |
| `unicorn_db` (12 MB) | 3 | Active shared infra — do not drop |

## Volume cleanup — DEFERRED 30 days

After 30 days of no revert action, volumes can be removed:
```bash
# Per-archived container, after soak:
docker volume rm <volume-name>
```

Volume names follow `uc-cloud_*` or `centerdeep-*` patterns; verify
the source compose project before removal.

## Revert "all of it" command

If a customer surfaces or Aaron changes course on Center Deep brand:
```bash
ssh centerdeep
for c in bizint_frontend bizint_backend executive-brief-frontend \
         centerdeep-ai \
         center-deep-tool-academic center-deep-tool-deep-search \
         center-deep-tool-search center-deep-tool-report \
         center-deep-search center-deep-searxng \
         ops-center-centerdeep \
         unicorn-minio centerdeep-keycloak; do
  docker start "$c" 2>&1 | tail -1
done
```

Caveat: this works only as long as the docker daemon's container
metadata is intact. Once volumes are removed (30-day soak), revert
requires re-creating the containers from compose.

## Related

- [[project_centerdeep_archive_2026_05_29]] — decision rationale
- [[project_observability_canonical_centerdeep]] — why obs stack stays
- [[project_centerdeep_data_intel]] — dataintel app survives Center Deep ecosystem archive
- `docs/dataintel-auth-repoint.md` — gate for stopping `centerdeep-keycloak`
- PO P-00055 task `622e44b8-6f9e-4f5c-aa66-d4701cb9794f`
- PO P-00056 (Retirement Leads polish + launch)
- PO P-00057 (MandateMap polish + launch)
- PO P-00058 (LoopNet rename + polish + leadgen blast)
