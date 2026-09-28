# Deployment — Meeting & Lecture Intelligence

## Local development

```bash
cd backend
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in real values
python create_all_tables.py   # bootstraps the schema (see note below)
python main.py   # or: uvicorn main:app --reload --port 9050

cd ../frontend
npm ci
npm run dev   # port 7777 by default
```

## Database bootstrap vs. migrations

This project bootstraps a **fresh** database via
`backend/create_all_tables.py` (SQLAlchemy `Base.metadata.create_all()`),
not by running the full Alembic history from empty — the migration chain
assumes tables already exist at various historical points and isn't
designed to replay from a blank database (a pre-existing characteristic of
this codebase, confirmed while verifying Phase 3's migrations against
real PostgreSQL).

For an **existing** deployment being upgraded (e.g. to pick up Phase 3's
lecture/document/important-moment/Google-Calendar/action-item-integration
tables), run:

```bash
cd backend
alembic upgrade head
```

This was verified end-to-end against real PostgreSQL 16: a database
stamped at revision `059_integration_pat_scope` was upgraded through
`060_lecture_details` → `061_important_moments` → `062_knowledge_documents`
→ `063_google_calendar` → `064_external_task_refs`, all applying cleanly,
and downgraded back to `059` cleanly.

Required Postgres extensions (create once per database):

```sql
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
```

## Docker

`docker-compose.yml` (prod), `docker-compose.dev.yml`, and
`docker-compose-full-stack.yml` define frontend, backend, PostgreSQL,
Qdrant, and supporting services. Standard flow:

```bash
docker compose -f docker-compose.yml up -d --build
docker compose exec backend alembic upgrade head
```

**Status of this document's own verification:** Docker itself was not
available in the environment these Phase 3/4 changes were verified in
(`docker: not found` — confirmed directly, not assumed). Migration
correctness was instead verified against a real, natively-installed
PostgreSQL 16 (see above), which is the part of "Docker verification"
that actually exercises SQL correctness. Container build/startup/health-
check verification remains to be performed in an environment with Docker
available.

## Environment variables

See `.env.example` for the full, categorized list (DATABASE, QDRANT, OMI,
LYZR, LLM, EMBEDDINGS, AUTH, STORAGE, GOOGLE CALENDAR, and a note that
Jira/Slack/Teams/Notion/Todoist/ClickUp/Confluence/Email are configured at
runtime via Settings → Integrations, not environment variables).

## Testing

```bash
cd backend
pip install -r requirements.txt pytest pytest-asyncio pytest-timeout qdrant-client
pytest tests/ -q --timeout=60
```

The full historical test suite (132+ files) is slow in network-
constrained sandboxes; the Phase 1–3 additions (omi, lyzr, lectures,
documents, decisions/risks, important-moments, cross-source Ask AI,
Google Calendar, the 8 task/notification integrations, security) are each
in their own `tests/test_*.py` file and run in well under a minute
together.

```bash
cd frontend
npm ci
npm run build   # production build
npx vitest run  # test suite
```
