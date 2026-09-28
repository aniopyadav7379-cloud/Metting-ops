# Local Development Quickstart (Windows PowerShell)

This is the deterministic sequence — no ad-hoc `$env:` exports needed for
anything covered here, because `backend/.env` is now actually loaded (see
"What was fixed" below).

## 1. Docker infrastructure (PostgreSQL, Qdrant, Redis)

```powershell
cd "C:\Hi Dev Hackthon\meeting-ops-final-delivery\meeting-ops"
docker compose -f docker-compose-full-stack.yml up -d postgres qdrant redis
```

`llama-gpu` is intentionally excluded from this command — it's now opt-in
via a Compose profile (see "GPU / llama.cpp" below), since it hardcodes an
AMD iGPU device path (`/dev/dri`, `/dev/kfd`) most machines, including
Docker Desktop on Windows without a passed-through iGPU, don't have. This
is not a required service; LLM/embedding providers are configured per-org
in Settings → Provider Settings, and CPU-based providers (Lyzr, OpenAI,
Anthropic, LiteLLM against any OpenAI-compatible endpoint) work without it.

## 2. Backend environment

```powershell
cd backend
Copy-Item .env.example .env
```

Edit `.env` and set at minimum:

```
DATABASE_URL=postgresql://meetingops:meetingops123@localhost:5434/meeting_sessions
QDRANT_URL=http://localhost:6335
REDIS_URL=redis://localhost:6381
SECRET_KEY=<run: openssl rand -hex 32, or `python -c "import secrets; print(secrets.token_hex(32))"`>
ENVIRONMENT=dev
```

`ENVIRONMENT=dev` is required for local work: without a real 32+ character
`SECRET_KEY`, the backend refuses to boot on purpose (a forgeable JWT
signing key is a full auth bypass) unless `ENVIRONMENT` is one of
`dev`/`development`/`test`/`testing`/`local`/`ci`. Setting a real
`SECRET_KEY` works in any environment; the fallback only exists for
`ENVIRONMENT=dev`. **Keep this the same value across every terminal
session** — see "What was fixed" below for why that matters.

Database bootstrap — this project's actual bootstrap script, not a
from-empty `alembic upgrade head` (see docs/deployment.md for why):

```powershell
python create_all_tables.py
python -m alembic stamp head
```

## 3. Backend startup

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Use the **full** `requirements.txt`, not `requirements-essential.txt` —
`rapidfuzz`, `aiohttp`, `mcp`, and `arq` (needed for the speakers/
bulk-import/hosted-MCP/job-queue routers) are already declared there.

Verify:
```powershell
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/docs
curl http://127.0.0.1:8000/openapi.json
```

## 4. Frontend environment

```powershell
cd ..\frontend
Copy-Item .env.example .env
```

Edit `.env` (this file, unlike the backend's, IS auto-loaded by Vite —
no fix was needed here):

```
VITE_API_URL=http://localhost:8000
VITE_WS_URL=ws://localhost:8000
VITE_BACKEND_URL=http://localhost:8000
```

The project's own coded default backend port is **9050** (see
`backend/main.py`'s `uvicorn.run(..., port=9050)` and
`frontend/vite.config.ts`'s proxy default) — every `9050` you may have
seen is that default, not a bug. Setting the three variables above
overrides it to `8000` deterministically, through the override mechanism
the code already provides, with zero source changes.

## 5. Frontend startup

```powershell
npm install
npm run dev
```

Open `http://localhost:7777`.

## 6. Login (local admin)

```
email:    admin@meetingops.local
username: admin
password: admin123
```

(Existing local seed account — not a production credential; not stored in
source control.)

## 7. Health check

```powershell
curl http://127.0.0.1:8000/health
```

## 8. Tests

```powershell
cd backend
pytest tests/ -q --timeout=90

cd ..\frontend
npx vitest run
npm run build
```

## 9. Known optional limitations

- `llama-gpu` requires a genuine AMD iGPU passthrough
  (`--profile gpu` to start it); without it, local dev uses whichever
  LLM/embedding provider you configure in Settings → Provider Settings.
- `agent_management`'s underlying table (`meeting_agents`) is now created
  by `create_all_tables.py`, but has no dedicated Alembic migration of its
  own — a pre-existing gap in the migration history (this project's
  primary schema source is `create_all_tables.py`, not a from-empty
  Alembic replay). Not fixed here — resolving it means writing a new
  migration and is out of scope for a local-stabilization pass.
- `docs/vocabulary_models.py` (a legacy, apparently-unused duplicate of
  `models/vocabulary.py`'s `VocabularySet`) was deliberately left
  unimported — investigating and safely retiring it needs its own review.

## What was fixed (root causes, not symptoms)

1. **`backend/.env` was never loaded.** No `load_dotenv()` call existed
   anywhere in the backend. Copying `.env.example` to `.env` did nothing
   on its own — every variable had to be re-exported by hand in every
   terminal session. This is the most likely explanation for the
   SECRET_KEY-related and port-related inconsistencies observed across
   separate terminal sessions: a value set via `$env:X=...` in one
   PowerShell window vanishes the moment that window closes. Fixed by
   adding `load_dotenv()` as the first statement in `main.py`,
   `create_all_tables.py`, and `alembic/env.py` — never overrides a real
   OS-level environment variable, so Docker/production deployments that
   already inject environment variables are unaffected.
2. **`meeting_agents` table missing.** `create_all_tables.py` never
   imported `models/agent_system.py` (the module defining it), the same
   class of gap as an already-fixed `database/models_rooms.py` omission
   from Phase 4. Fixed by adding the import. Two other model modules
   (`database/vocabulary_models.py`, `models/agent_config.py`) were
   investigated and deliberately NOT added — see the code comment in
   `create_all_tables.py` for why (inconsistent Base sources, and a
   duplicate `VocabularySet` class that would risk a metadata conflict).
3. **Duplicate FastAPI operation ID.** `api/meeting_management.py` had a
   dead, explicitly-unreachable stub function also named `create_session`,
   colliding with the real one in `api/simple_recording_db.py`. Renamed
   the dead function; zero behavior change since it was unreachable
   either way (shadowed by `api/sessions.py`, loaded earlier).
4. **`llama-gpu` failing on `/dev/dri`.** Made it opt-in via a Compose
   `profiles: ["gpu"]` entry rather than part of the default `up`.
