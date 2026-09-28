# Agent platform roadmap — conversational + app-driving + voice

Status: **planning**. Last updated: 2026-05-27. Owner arc: agent chat → RAG → graph → app-control via MCP → voice.

The goal: a conversational agent you can talk to (text or voice) that answers
across your meetings, reasons over a knowledge graph of who-said-what /
decisions / action items, and can **act in the app on your behalf** (create a
session, tag/organize, start a recording, kick a reprocess, draft follow-up
emails) through MCP tools — safely. This is augmentation: a real foundation
already exists. This doc inventories it, names the decisions, and lays out a
phased plan so a fresh session/agent can pick up cleanly.

## Current foundation (what already exists — don't rebuild)

- **Per-meeting + cross-meeting chat (RAG).** `backend/api/ai_chat.py`:
  `POST /sessions/{id}/messages` (per-meeting), `POST /rag/query` +
  `GET /rag/history` (cross-meeting). Retrieval via
  `backend/services/semantic_search_service.py` over **Qdrant** (hybrid dense +
  BM25). Answers cite source meetings.
- **Knowledge graph (via Brigade).** `backend/services/brigade_writer.py` writes
  `Meeting / Speaker / ActionItem / Topic / Decision` nodes to Brigade's
  **FalkorDB** on session completion. Tenancy modes: `shared` /
  `per_org_graph` / `per_org_instance`. Reconciliation:
  `scripts/reconcile_brigade_graph.py`. Design: `docs/brigade-integration-design.md`.
- **MCP server (read-only today).** `mcp/meeting_ops_mcp.py` exposes 8 tools:
  `search_meetings`, `ask_about_meetings`, `list_meetings`,
  `get_meeting_details`, `get_meeting_transcript`, `chat_with_meeting`,
  `get_analytics`, `get_meeting_insights` + 2 resources + 2 prompts. **All
  read-only** — there are no write/control tools yet.
- **Agent framework.** `backend/api/agents.py` + `backend/services/agent_tools.py`
  (audit these first — they define the in-app agent surface).
- **Voice building blocks.** TTS: `backend/api/tts.py` + `services/tts_jobs.py`
  (VibeVoice / Kokoro on midboy2). STT: Parakeet (streaming + 1.1B) +
  whisper path. So both directions of a voice loop already have infra.
- **LLM routing.** Org-configured provider via `services/providers/registry.py`;
  LiteLLM on commander routes to local GPUs. Default `gpt-oss-20b`.

## The gaps (what the vision needs that isn't there yet)

1. **RAG quality + graph-aware retrieval.** Retrieval is hybrid dense + BM25
   (RRF fusion, org-filtered) in `semantic_search_service.py` — not vector-only.
   But the **base cross-meeting retrieval is currently weak** (audit 2026-05-27:
   `ask_about_meetings` failed to rank an exact-title match first), and the
   graph exists but isn't used at query time. Gap: fix the seed retriever, then
   let the agent traverse the graph (e.g. "what did Shafen commit to across the
   last 3 meetings?") not just match chunks.
2. **App-control MCP tools (write surface).** The agent can read but not act.
   Gap: a *safe* set of write tools (create/rename/tag session, start/stop
   record, trigger reprocess, draft email) with auth + confirmation semantics.
3. **An agent runtime that can plan + call tools in a loop**, scoped per
   tenant/user, with the existing agent-scoping pattern (private/team/org/shared).
4. **Voice chat loop.** Wire STT (mic → text) → agent → TTS (text → speech),
   ideally low-latency + barge-in. Building blocks exist; the loop doesn't.

## Phase 1 audit results + ratified decisions (2026-05-27)

Audited against the live backend. Foundations are real; two blockers
re-sequence the work:

- **Base cross-meeting retrieval is weak** — `ask_about_meetings` (and even an
  exact-title search) failed to rank the target meeting first; single-meeting
  `chat_with_meeting` is good. Fix this *before* graph-augmented retrieval (the
  graph layer seeds from this retriever, so a bad seed just gets amplified).
- **The graph is nearly empty** — `agent_meeting_ops_canonical` had ~4
  smoke-test nodes / 1 edge; the writer is correct (session 124 landed) but the
  corpus was never backfilled. Populate it before relying on graph expansion.

Ratified decisions: (1) use Brigade's FalkorDB, no local graph; (2) strict
propose→confirm→mutate for all writes, org + tier scope enforced at the tool
boundary (draft-email safe, send-email separately confirmed); (3) graph-augmented
retrieval is a read-only layer over the existing meeting-rag loop (Qdrant seed →
graph-neighborhood expand → re-rank → existing LLM loop), not a rewrite.

**Re-sequenced near-term order:** (1) diagnose + fix base cross-meeting
retrieval (indexing coverage / payload / RRF fusion weights / org filter),
re-measure; (2) backfill the Brigade graph over all completed sessions
(`scripts/reconcile_brigade_graph.py`); (3) THEN graph-augmented retrieval;
(4) THEN the safe write/control MCP surface.

## Phased plan (proposed)

**Phase 1 — Solidify RAG + decide the graph.** Audit `ai_chat.py` +
`semantic_search_service.py` + `agent_tools.py`/`agents.py`; measure current RAG
answer quality on real meetings. Decide the graph story (see decisions). Make
graph data queryable at RAG time (graph-augmented retrieval) — start read-only.

**Phase 2 — App-control MCP tools (safe write surface).** Add write/control
tools to `mcp/meeting_ops_mcp.py` (and/or a new agent tool module) mirroring
`reference_projectops_mcp_write_tools` pattern: create/rename/tag session,
start/stop recording, trigger reprocess, draft follow-up email. **Every
mutating tool requires explicit confirmation + respects tier + org scoping.**
Read-only stays unguarded; writes are gated.

**Phase 3 — Agent runtime.** A plan→tool-call→observe loop bound to
tenant/workspace/user with private/team/org/shared visibility (the established
agent-scoping pattern). Exposed in-app (chat panel) and over MCP. Keep the
"control the app" actions behind the Phase-2 confirmation gates.

**Phase 4 — Voice.** Re-sequenced: **Stable owns realtime voice/LiveKit**,
Meeting-Ops embeds. Don't reinvent the voice stack in Meeting-Ops. The
home-ops voice + wake-word + barge-in pattern at
`/Volumes/Studio Storage/Development/smart-home-agent/Home-Ops/voice/`
(`voice-router/` + `wake-word/`) is the reference to lift into Stable.
Meeting-Ops integration = embed a Stable room with a Meeting-Ops-scoped agent
on the other side, NOT a new voice stack here.

## Key decisions to make (before/within Phase 1)

- **Graph DB:** lean into Brigade's FalkorDB (already wired, ecosystem-shared) vs
  a Meeting-Ops-local graph. Default recommendation: **use Brigade** — it's
  already receiving the nodes; add read/query paths rather than a second graph.
- **MCP control safety model:** which actions are allowed, what confirmation
  looks like (the agent proposes → user confirms in-app/in-chat before any
  mutation), and how tier/org scoping is enforced on each tool.
- **Agent runtime:** reuse the existing `agents.py`/`agent_tools.py` framework vs
  adopt the ecosystem agent runtime (Brigade orchestrators, Stable @mention).
  Audit before deciding.
- **Voice stack + latency target:** push-to-talk vs duplex; browser STT vs
  server streaming; which TTS voice.
- **Cost posture:** keep browser-first where possible; any always-on server
  inference (e.g. duplex voice) needs the cost owner explicit per
  `docs/compute-economics.md`.

## Operational context (so a fresh agent can execute)

- **Edit locally** at `/Users/aaronstransky/UC-Meeting-Ops-bigboy`, **rsync to
  bigboy** `/srv/meeting-ops/src/`, commit **on bigboy**,
  then build/deploy from the **project root** (not `/src`) with `--env-file`:
  `cd /srv/meeting-ops/ && docker compose --env-file deploy/bigboy/.env.bigboy -f deploy/bigboy/docker-compose.bigboy.yml up -d --force-recreate --build <service>`.
  SSH alias is **`magicunicorn`**, not `bigboy`. Services: `backend`,
  `meet-bulk-import-worker`, `frontend`.
- **Verify the deployed image**, not just the tag (release notes can lie):
  `docker exec meet-backend ...` for code/health checks.
- **Tests:** `docker exec meet-backend python3 -m pytest tests/ -q`. Frontend:
  `npm run build` (tsc) + `npx vitest run`.
- **Releases:** bump `frontend/package.json` + CHANGELOG, annotated tag, push
  `main` + tag to Forgejo `git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops`.
- **Parallel agents** each get their own `git worktree` (never switch branches in
  a shared checkout with WIP).
- **Brigade** API: `brigade.magicunicorn.dev`, FalkorDB. **LLM** via LiteLLM on
  commander. **Voice GPUs:** midboy2 (Kokoro TTS, Parakeet STT).

## Related docs
- `docs/compute-economics.md` — browser-first cost moat + the decision tree.
- `docs/brigade-integration-design.md` — the graph writer + tenancy.
- `docs/phase-b-server-live-streaming.md` — streaming STT (reusable for voice).
- `docs/audio-storage-garage.md` — storage topology.
