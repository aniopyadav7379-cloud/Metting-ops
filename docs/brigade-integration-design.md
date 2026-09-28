# Brigade Integration: Knowledge Graph, Agent Runtime, and Multi-Tenant Deployment

Status: Draft for approval. Implementation tickets at the end of this doc.
Owner: Meeting-Ops team.
Authoring date: 2026-05-20.

## 1. Purpose and Framing

UC-Meeting-Ops produces a stream of high-signal entities every time someone
records a meeting: speakers, decisions, action items, topics, attached
documents, room/device bindings, organization scope. Today, that data
lives in three silos — Postgres tables (relational), Qdrant collections
(vector), and ephemeral LLM-rendered summaries (text). None of those
shapes are right for the question Aaron actually wants to ask:

> *"Show me everyone Mohsin Ali has been mentioned with in the last 90
> days, what action items they own collectively, and which meetings
> produced those — visually, as a graph I can navigate."*

That's a graph query, and the ecosystem already has the right backend
for it: **Unicorn Brigade**. Brigade owns FalkorDB, the agent runtime,
the OIDC/Keycloak integration, the existing 3D viewer, and an Agentic
RAG loop that fuses graph + vector retrieval. Re-implementing any of
that inside Meeting-Ops would be a strategic mistake.

Aaron's verbatim framing:

> *"We're supposed to be using brigade agents for the actual agentic work
> and stuff, on the backend, I think it's included in the infrastructure,
> but we need to make the gui aspect, and maybe also integrate the
> backend in or something?"*
>
> *"Brigade also has that 3d graph pattern for agents."*

This doc designs that integration without disturbing the work Meeting-Ops
already does well (recording, transcription, diarization, action-item
extraction, RBAC). The integration is **additive** — Meeting-Ops writes
its entities into Brigade's knowledge graph after a session completes,
reads them back for visualization and Agentic RAG, and keeps doing
everything it does today.

A second framing dimension is tenancy and compliance. Aaron's verbatim:

> *"As far as the DB, I'm thinking by org id, but depending on the
> server, it can be configured either way. I mean for our specific
> deployment. Because if we do testing on magicunicorn.dev and then
> want to stand up a production instance for customers, we can move
> them easily to the other server."*
>
> *"Shafen's legacy1 server has a lot of ram and could host those and
> probably meeting ops. It would probably help with compliance like
> hipaa and stuff, right?"*

So the design also has to support **three tenancy modes** behind a
single env var — cheap shared graph for dev, per-org graph for SaaS
hardening, per-org dedicated FalkorDB instance for HIPAA-grade
isolation on customer-owned infrastructure (e.g., Shafen's Legacy1).

The design goals are:

1. **Brigade is the canonical agent runtime + graph store.** Meeting-Ops
   becomes a producer (and consumer) of graph data, not a parallel graph
   stack.
2. **Don't break what works.** The existing diarization pipeline,
   action-item extractor, Brigade *agent federation* (already wired in
   `services/agents/brigade.py`), the meeting-rag agent, RBAC, and the
   recording flow all stay. Graph writes are best-effort, queued,
   retryable, and never block a recording from completing.
3. **Tenancy is a deployment knob, not an architecture.** One env var
   picks shared / per-org-graph / per-org-instance. The Meeting-Ops
   code path is identical across the three.
4. **3D viewer is reused, not rebuilt.** Brigade's `KnowledgeGraph.jsx`
   page already wires `react-force-graph-3d` against
   `/api/v1/knowledge/graph` — that exact viewer (or its dependency
   tree, transplanted) is what Meeting-Ops will surface.
5. **HIPAA path is a real product surface, not an afterthought.** The
   per-org-instance deployment is what makes Genesis Flow Labs viable
   on Legacy1 and what we sell to anyone in healthcare downstream.

## 2. What Already Exists (Survey Result)

Before designing anything new, a survey of the codebase on
`magicunicorn` (bigboy) and the Mac dev tree shows the following
already in place.

### 2.1 Unicorn Brigade

**Location**

- Source: `/Volumes/Studio Storage/Development/Unicorn-Brigade/` (Mac
  dev tree, 66 top-level entries, last touched 2026-05-09).
- Containers on bigboy:
  - `unicorn-brigade` (image `unicorn-brigade:latest`,
    `0.0.0.0:8101->8100/tcp`) — FastAPI backend, **Brigade API v1.13.0**.
  - `unicorn-brigade-ui` (`0.0.0.0:8102->80/tcp`) — Vite/React
    frontend served via nginx.
  - `unicorn-falkordb` (`falkordb/falkordb:latest`,
    `0.0.0.0:3030->3000/tcp` UI, `0.0.0.0:6380->6379/tcp` Redis/Cypher).
- Compose project: `uc-cloud-production`, file
  `/srv/uc-cloud/docker-compose.brigade.yml`.
- Public URL: `https://brigade.magicunicorn.dev` (Traefik labels on the
  container — `Host('brigade.magicunicorn.dev') && PathPrefix('/api')`,
  letsencrypt resolver).

**API surface (304 paths total)**

Highlights relevant to this integration:

| Group | Endpoints | Notes |
|---|---|---|
| `/api/v1/knowledge/*` (13) | `entities`, `relationships`, `graph`, `stats`, `search`, `query` (NL), `rag`, `store/{entity,relationship,fact}`, `path`, `context/{entity_name}` | The whole graph surface. `/graph` returns `{nodes, links}` ready for `react-force-graph-3d`. |
| `/api/v1/agents/*` (19) | `agents`, `agents/domains`, `execute`, `execute/stream`, `{id}/chat`, `{id}/chat/local`, `export/json` | The agent runtime. `services/agents/brigade.py` in Meeting-Ops already federates this list. |
| `/api/v1/a2a/*` (7) | `agents`, `discover`, `agents/{id}/invoke`, `agents/{id}/card`, `tasks/{id}/status` | Agent-to-agent protocol. |
| `/api/v1/mcp/*` (19) | `info`, `tools/list`, `tools/call`, `registry/*` | MCP tool surface, 61 tools registered. |
| `/api/v1/memory/*` | `graphs`, `query`, `conversations` | mem0 + per-conversation memory. |
| `/api/v1/chat/completions` | OpenAI-compatible | Used by Meeting-Ops `meeting_rag` agent today via the proxy chain. |
| `/api/v1/auth/*` (10) | OIDC login/callback/me/logout/check-permission | Keycloak `uchub` realm, same as Meeting-Ops. |

**Auth model**

`app/auth/middleware.py` exposes a unified `AuthUser` produced from one
of three sources:

1. **X-API-Key header** — validated against `api_keys` /
   `user_api_keys` tables, OR a master `BRIGADE_ADMIN_KEY` env var. The
   master key is the path Meeting-Ops will use for service-to-service
   writes.
2. **Authorization: Bearer JWT** — validated against the Keycloak
   `uchub` realm via JWKS. This is what user-facing API calls use; the
   oauth2-proxy session that Meeting-Ops already runs in front of its
   frontend produces the same token.
3. **Session cookie** (`brigade_session`) — only relevant when the
   user is in Brigade's own UI; we will not use this path.

**Verified working from inside the bigboy container network**

```
curl -s http://localhost:8101/api/v1/knowledge/stats \
  -H 'X-API-Key: 363d63130af29815a6baf5b9ae412a9a1b2e2bf7b3c1bc805bb15f081a91210b' \
| jq
# → {"success":true,"status":"connected","enabled":true,
#    "host":"unicorn-falkordb","port":6379,
#    "graphs":{"brigade_global":{"nodes":6,"relationships":4}},
#    "entity_types":["Concept","Legal Case","Person"],
#    "relationship_types":["PREFERS_FORMAT", ...],
#    "total_entities":6,"total_relationships":4}
```

Note: `BRIGADE_SERVICE_KEY=sk-brigade-service-key-2025` is set in env
but is **not a real key** — only the admin key authenticates. We will
mint a proper `meeting-ops` service key via `/api/v1/admin/user-keys`
during Phase 1.

**Existing graph storage model (`app/knowledge/graph_manager.py`,
1281 lines)**

- One FalkorDB **instance** is shared by all of Brigade.
- Inside that instance, **graphs are partitioned by name**:
  - `brigade_global` — the global graph (default).
  - `agent_<id>` — per-agent graphs (one per agent, auto-created on
    first use).
  - `<user_id>__<agent_name>` — per-user-per-agent graphs (e.g.,
    `7990db95_7f7c_45cb_8f43_293a76e60774__project_ops_assistant`).
  - Domain graphs created by hand (e.g., `majiks_research` — 539
    nodes / Person, Company, Investor, FundingRound, Lawsuit,
    Regulation, MonitorRun, Tripwire, Source, AcademicPaper).
- A `ContextVar` (`kg_target_graph_override`) lets a request pin all
  graph operations to a specific graph name. Used today by the
  `kg_query` tool to route maj-recon → `majiks_research`.

This naming model is exactly the lever we need for per-org-graph
tenancy — we'll add a `meeting_ops__<org_slug>` (or shared
`meeting_ops` with `org_id` property) without touching the existing
code path.

**Existing graph contents (FalkorDB `GRAPH.LIST` on `unicorn-falkordb`)**

```
brigade_global                    6 nodes  (test entries)
majiks_research                   539 nodes (Maj. Recon — see memory)
agent_brigade_*__col_finance     (multiple per-agent graphs)
<user-uuid>__project_ops_assistant
admin__settlement_modeler
... ~25 graphs total
```

So FalkorDB is live, populated, and proven at scale.

### 2.2 Brigade 3D viewer

`/Volumes/Studio Storage/Development/Unicorn-Brigade/frontend/src/pages/KnowledgeGraph.jsx`
(735 lines) is a full-featured 3D graph browser. Key features:

- **Library:** `react-force-graph-3d` (`^1.29.0`) — listed in
  `frontend/package.json`. Also has `reactflow ^11.11.4` (used for
  agent workflow viz, not the knowledge graph).
- **Data fetch:** `GET /api/v1/knowledge/graph?entity_type=&name_pattern=&limit=`
  returns `{success, nodes: [...], links: [...]}` — the canonical shape
  for `react-force-graph-3d`'s `graphData` prop.
- **Entity type colors** hard-coded (`Person → emerald`,
  `Company → blue`, `Concept → purple`, `Location → amber`,
  `Product → pink`, `Event → cyan`, `Organization → red`, default
  `gray`).
- **Features:**
  - 2D ↔ 3D toggle (`numDimensions={viewMode === '3d' ? 3 : 2}`)
  - Search + entity-type filter + 200-node default limit
  - Click a node → fetches `/api/v1/knowledge/entity/{name}` for the
    detail panel + camera tween onto the node
  - Pause / resume animation, reset camera, fullscreen
  - Stats bar (entities / relationships / entity types / rel types)
  - Directional particles on links (subtle animation)
  - Background grid effect

### 2.3 Crisis-Ops 3D viewer (sibling pattern)

`/srv/crisis-management-ops/frontend/src/pages/graphrag/Graph3DView.jsx`.

- Same library (`react-force-graph-3d`) but also imports
  `three-spritetext` for selected-node labels riding above spheres
  via `nodeThreeObject`.
- Lazy-loaded from `GraphRAG.jsx` so three.js doesn't bloat the main
  bundle for users who never open the graph view. We will copy this
  pattern in Meeting-Ops.
- Custom `useElementSize` hook with `ResizeObserver` to pass explicit
  width/height to `ForceGraph3D` (without this, it falls back to
  `window.innerWidth`/`Height` and breaks page layout).
- Uses `nodeVal` to scale spheres by `influence` or `risk`,
  `getNodeColor` callback by category — both pluggable per-view.

The Crisis-Ops graph backend **does not call Brigade** today — it has
its own EntityContext that pulls from the local crisis-ops Postgres.
That's an opportunity for follow-up consolidation but **out of scope
for this design**. The pattern we're copying is the frontend shell
(Graph3DView + GraphRAG page).

### 2.4 Songwriter / majiks.online

The "Songwriter / Songwriter-Agent → 3D graph for related songs" graph
viewer Aaron referenced does not appear to live in a discoverable
place under `/srv/Production/` or the dev tree as a standalone
viewer. The graph data behind it — songs related by topic, ecosystem
tags, syncedLyrics co-occurrence — lives in the public majiks.online
API (`756 published, public API exposes syncedLyrics+ecosystem+everything`
per memory).

For this design we'll treat the Brigade viewer + Crisis-Ops viewer as
the canonical references, since both are 3D graphs against the same
`react-force-graph-3d` library. If a third songwriter viewer exists we
can consolidate later — the pattern is the same.

### 2.5 Meeting-Ops Brigade integration today

`backend/services/agents/brigade.py` (204 lines) **already federates
Brigade agents into Meeting-Ops**:

- Forwards the user's bearer JWT (same uchub realm) so Brigade applies
  its own RBAC.
- 60-second Redis cache per `(org_id, user_email)` to avoid hammering
  Brigade.
- Maps Brigade's agent JSON shape to Meeting-Ops' `AgentDescriptor`.
- Default `BRIGADE_URL=http://unicorn-brigade:8100` (in-cluster).

This means Aaron's "we need to make the gui aspect, and maybe also
integrate the backend in or something" — the *backend* part is half
done. The agent list shows up in Meeting-Ops' agent picker today.

**What's missing:** the knowledge-graph half. Meeting-Ops never
writes its session data into Brigade's graph, never reads it back,
and has no graph viewer.

### 2.6 Meeting-Ops data model (what becomes graph nodes/edges)

From `backend/database/models.py` and `backend/database/models_rooms.py`:

| Postgres table | Maps to graph node | Cardinality |
|---|---|---|
| `recording_sessions` | `:Meeting` | One per recording |
| `speaker` (SpeakerProfile) | `:Speaker` | Org-level identity, ~10-1000 / org |
| `users` (in `ucpro`) | `:User` | Org-level user, ~5-50 / org |
| `action_items` | `:ActionItem` | First-class since alembic 021, dozens / org / month |
| `session_attachments` | `:Document` | Just landed v0.7.3 |
| `conference_rooms` | `:Room` | ~1-20 per org |
| `organizations` | `:Organization` | The tenancy boundary |
| `transcriptions` | (not a node — stays in Postgres + Qdrant) | Millions of rows; not graph-shaped |

Implicit entities surfaced by LLM analysis (no Postgres table today;
LLM produces them, we promote to graph):

- `:Topic` — themes discussed in a meeting (extracted by the
  summarizer / action-item extractor). Dedup across meetings.
- `:Decision` — explicit "we decided X" statements promoted out of
  `final_summary`. Dedup is trickier (same decision across follow-up
  meetings → SUPERSEDES edge, not duplicate node).
- `:Mention` — speaker says a name that's *not* in `speaker` table
  yet (external person). Triages into "do we promote this to a real
  Speaker?" later.

The graph is therefore a **layered surface**:

```
Layer 1 (deterministic — write-on-completion):
  Meeting, Speaker, User, ActionItem, Document, Room, Organization

Layer 2 (LLM-derived — write-after-summary):
  Topic, Decision, Mention

Layer 3 (computed — write-after-batch):
  WORKS_WITH (co-meeting count), FOLLOWS_UP_FROM (topic co-occurrence)
```

Phases 1-2 ship Layer 1 + 2. Layer 3 is a follow-up batch job.

## 3. Strategic Decisions Up Front

### 3.1 Brigade is the canonical agent runtime

Meeting-Ops will not grow a parallel agent stack. Today's `meeting-rag-agent`
in `backend/services/agents/meeting_rag.py` is a local tool-use loop
that calls Qdrant + Postgres directly. That's the right call for v0
(no Brigade dependency, snappy responses, ships before Brigade is
ready). But as Brigade matures and the user-facing surface grows
beyond "search my meetings" (now: "show me everyone connected to
Mohsin in the last 90 days who owns an open action item"), the
orchestration belongs upstream in Brigade.

**Decision:** keep `meeting-rag-agent` running locally for v1 (this
phase). In Phase 4 (out of scope here, scheduled separately), migrate
to a Brigade-hosted "Meeting Assistant" agent that has access to
Meeting-Ops MCP tools. The graph writes from Phase 1 are what make
that migration actually useful — without graph data, a Brigade
agent has nothing new to offer over the local one.

### 3.2 Graph data is auxiliary, not authoritative

Postgres remains the source of truth for everything Meeting-Ops needs
to function. The graph is a **denormalized read model** — fast to
query for connection patterns, lossy compared to the relational store.
If FalkorDB goes down, recording, transcription, summarization,
action-item extraction, and the meeting-rag agent **must keep working**.

Concrete implication: graph writes are best-effort and idempotent. A
nightly reconciliation job re-syncs Postgres → graph for any sessions
where the write failed.

### 3.3 Tenancy mode is set per-deployment, not per-org

The same Meeting-Ops code path supports all three tenancy modes. The
selection is a single env var (`BRIGADE_GRAPH_TENANCY_MODE`). Choosing
the mode is a deployment-time decision tied to the customer's
compliance posture, not a runtime feature flag.

Three modes are defined in Section 8.

### 3.4 The 3D viewer is iframed initially, native by Phase 3

To minimize risk in Phase 1-2, the small in-page graph widget on the
session-detail page is a **native React component** (we have all the
ingredients), but the full-screen "/insights" page deep-links into
Brigade's existing viewer at `https://brigade.magicunicorn.dev/knowledge`
with a `?graph=meeting_ops&filter=org:<id>` URL parameter. We harden
the native render in Phase 3 with feature parity (fullscreen, search,
filters, drill-through), at which point the deep-link to Brigade can
become a "view in Brigade UI" affordance rather than the primary
path.

This phasing lets Phase 1 + 2 ship with a working visual surface in
two weeks without bottlenecking on viewer parity.

## 4. Graph Schema

The schema below is intentionally conservative — every node has a
stable `id`, an `org_id` for tenancy enforcement, and only properties
that are useful for queries (not denormalized text that's already in
Postgres / Qdrant).

### 4.1 Node labels

```cypher
// Recording-level
(:Meeting {
  id,                  // recording_sessions.id (integer, stable)
  uuid,                // recording_sessions.session_id (string, stable)
  title,               // recording_sessions.title or .name
  started_at,          // ISO8601
  ended_at,            // ISO8601
  duration_sec,
  meeting_type,        // recording_sessions.meeting_type
  source_type,         // browser_always_on | satellite_stream | satellite_upload | conference_room | upload
  org_id,              // organizations.id (integer)
  room_id?,            // conference_rooms.id (integer, optional)
  status,              // active | completed | failed
  created_at
})

// People & identities
(:Speaker {
  id,                  // speaker.id (SpeakerProfile)
  display_name,        // speaker.display_name
  email?,
  phone?,
  company?,            // speaker.company (their employer, NOT our org)
  has_voice_sample,    // bool — is this an enrolled voice?
  linked_user_id?,     // user.id if speaker is also a system user
  org_id,
  created_at
})

(:User {
  id,                  // user.id (ucpro)
  email,
  display_name,
  role,                // admin | user | viewer
  org_id,
  created_at
})

// LLM-derived
(:Topic {
  id,                  // hash(canonical_label, org_id)
  label,               // "Q3 budget", "patient onboarding flow"
  canonical_label,     // lowercased / stripped for dedup
  org_id,
  first_seen_at,
  last_seen_at,
  mention_count
})

(:Decision {
  id,                  // uuid
  text,                // 1-2 sentence verbatim or paraphrase
  confidence,          // LLM-reported 0-1
  meeting_id,          // recording_sessions.id (originating)
  org_id,
  created_at,
  superseded_by_id?    // Decision.id (if applicable)
})

(:Mention {
  id,                  // uuid
  name,                // raw name as said in transcript
  resolution_status,   // unresolved | linked_to_speaker | external
  resolved_speaker_id?,
  org_id,
  first_seen_at
})

// First-class entities (already in Postgres)
(:ActionItem {
  id,                  // action_items.id
  text,                // action_items.text
  status,              // open | in_progress | done | cancelled
  due_date?,
  owner_speaker_id?,
  source_meeting_id,   // recording_sessions.id
  org_id,
  created_at,
  completed_at?
})

(:Document {
  id,                  // session_attachments.id
  filename,
  attachment_type,     // pdf | image | audio | other
  size_bytes,
  meeting_id,          // recording_sessions.id
  org_id,
  uploaded_at
})

(:Room {
  id,                  // conference_rooms.id
  name,
  location?,
  capabilities,        // JSON: {mic_count, has_camera, ...}
  org_id,
  created_at
})

(:Organization {
  id,                  // organizations.id
  name,
  slug
})
```

### 4.2 Edge types

```cypher
// Participation
(Speaker)-[:SPOKE_IN {word_count, talk_time_sec, segment_count}]->(Meeting)
(User)-[:ATTENDED {role}]->(Meeting)    // role = host | invited | shared

// Topical
(Meeting)-[:DISCUSSED {confidence, mentions}]->(Topic)
(Speaker)-[:RAISED {first_at_sec}]->(Topic)   // speaker first introduced topic in meeting

// Outputs
(Meeting)-[:PRODUCED]->(Decision)
(Meeting)-[:PRODUCED]->(ActionItem)
(ActionItem)-[:OWNED_BY]->(Speaker)
(ActionItem)-[:MENTIONS]->(Speaker)    // when text mentions someone other than owner
(Decision)-[:SUPERSEDES]->(Decision)
(Decision)-[:REFERENCES]->(Topic)

// Attachments & rooms
(Meeting)-[:REFERENCES]->(Document)
(Meeting)-[:IN_ROOM]->(Room)
(Meeting)-[:IN_ORG]->(Organization)

// Derived (computed in batch — Layer 3)
(Speaker)-[:WORKS_WITH {co_meeting_count, last_co_meeting_at}]->(Speaker)
(Topic)-[:FOLLOWS_UP_FROM {co_occurrence_count, time_gap_days}]->(Topic)

// RBAC
(User)-[:CAN_VIEW]->(Meeting)   // explicit per-user ACL (created from session_collaborators)
(User)-[:MEMBER_OF]->(Organization)
```

### 4.3 Indexes

```cypher
CREATE INDEX IF NOT EXISTS FOR (m:Meeting) ON (m.id);
CREATE INDEX IF NOT EXISTS FOR (m:Meeting) ON (m.org_id);
CREATE INDEX IF NOT EXISTS FOR (m:Meeting) ON (m.started_at);
CREATE INDEX IF NOT EXISTS FOR (s:Speaker) ON (s.id);
CREATE INDEX IF NOT EXISTS FOR (s:Speaker) ON (s.org_id);
CREATE INDEX IF NOT EXISTS FOR (s:Speaker) ON (s.display_name);
CREATE INDEX IF NOT EXISTS FOR (a:ActionItem) ON (a.id);
CREATE INDEX IF NOT EXISTS FOR (a:ActionItem) ON (a.org_id);
CREATE INDEX IF NOT EXISTS FOR (a:ActionItem) ON (a.status);
CREATE INDEX IF NOT EXISTS FOR (t:Topic) ON (t.canonical_label);
CREATE INDEX IF NOT EXISTS FOR (t:Topic) ON (t.org_id);
CREATE INDEX IF NOT EXISTS FOR (u:User) ON (u.org_id);
CREATE INDEX IF NOT EXISTS FOR (d:Document) ON (d.meeting_id);
```

These are the same `CREATE INDEX IF NOT EXISTS` calls Brigade's
`_ensure_graph_schema` already uses — we just extend the schema
bootstrap with Meeting-Ops labels.

### 4.4 Stable IDs

Every node uses a Postgres-side stable ID where one exists
(`recording_sessions.id`, `speaker.id`, etc). This gives us a clean
upsert key on every re-write:

```cypher
MERGE (m:Meeting {id: $id, org_id: $org_id})
ON CREATE SET m += $props
ON MATCH SET m += $props, m.updated_at = $now
```

For LLM-derived nodes (Topic, Decision, Mention), the ID is a
deterministic hash so re-extraction of the same fact yields the same
node:

- `Topic.id = sha256(org_id || ':topic:' || canonical_label)[:16]`
- `Decision.id = sha256(org_id || ':decision:' || meeting_id || ':' || normalized_text)[:16]`
- `Mention.id = sha256(org_id || ':mention:' || meeting_id || ':' || lower(name))[:16]`

This is the same shape `graph_manager.store_entity` uses (MERGE
by `name` today; we extend with `id`-keyed MERGE for Meeting-Ops
nodes to avoid collisions between orgs).

## 5. Write Pipeline: Meeting-Ops → Brigade

### 5.1 When writes happen

Trigger points (existing Meeting-Ops code paths):

| Trigger | Code path | What writes |
|---|---|---|
| Session marked `completed` | `services.auto_summarization_service` / `api.simple_recording_db` finalize / `api.satellite_api` finalize | Meeting node + IN_ORG + IN_ROOM edges |
| Speakers identified | `services.speaker_service` after diarization + identify | Speaker nodes (idempotent) + SPOKE_IN edges with talk_time |
| Action items extracted | `services.action_items_extractor.persist_action_items` | ActionItem nodes + PRODUCED + OWNED_BY + MENTIONS |
| Summary completed | `services.auto_summarization_service` | Topic + Decision + Mention nodes + DISCUSSED + PRODUCED + REFERENCES (Decision→Topic) |
| Attachment uploaded | `api.session_attachments` | Document node + REFERENCES |
| Session shared | `api.session_permissions` (session_collaborators insert) | User node + CAN_VIEW edge |
| Session deleted | `api.sessions` DELETE | DETACH DELETE meeting + cascade |
| Session moved between orgs | `api.session_move_org` (just landed) | Update org_id on Meeting + cascade to children, re-tag like Qdrant does today |

All triggers funnel into a single service: `backend/services/brigade_writer.py`
(new module).

### 5.2 Module shape

```python
# backend/services/brigade_writer.py
"""Best-effort graph writer for Meeting-Ops → Unicorn Brigade.

Public surface (call from finalize/summarize paths):

  await write_meeting(session)
  await write_speakers_for_meeting(session, speaker_links)
  await write_action_items(session, action_items)
  await write_summary_entities(session, topics, decisions, mentions)
  await write_attachment(attachment)
  await write_collaborator(session, user)
  await delete_meeting(session_id)
  await reassign_meeting_org(session_id, new_org_id)

All functions are fire-and-forget from the caller's perspective. They:
- enqueue to Redis (`meetingops:brigade_writer:queue`) on failure
- never raise into the caller's path
- log structured failure for the reconciliation job

Tenancy mode resolved at call time from BRIGADE_GRAPH_TENANCY_MODE.
"""
```

Internals call `POST /api/v1/knowledge/store/{entity,relationship,fact}`
with `X-API-Key: $BRIGADE_ADMIN_KEY` (the master key from env), or in
Phase 1.5, a minted per-service `meeting-ops` API key (cleaner). One
write per node; relationship writes batched per meeting to keep round
trips bounded.

### 5.3 Idempotency

Every entity write uses Brigade's existing `store_entity` MERGE
semantics. We extend the call shape to include our stable `id`
property so Brigade's `MERGE (e:Entity {name: $name})` doesn't
collide between orgs (two orgs both having a `Speaker {name: "John
Doe"}` is a real scenario; the existing code merges them, which is
a leak).

**Decision:** We pass our own stable `id` as the primary MERGE key for
Meeting-Ops nodes. To do this cleanly we add a Phase-1 PR to Brigade's
`graph_manager.py` to accept a `merge_key` parameter on
`store_entity` (defaulting to `name` for backward compat). This is
the **only Brigade-side code change** in this design.

### 5.4 Failure mode + retry

Failure paths:

| Failure | Behavior |
|---|---|
| Brigade unreachable | Enqueue to `meetingops:brigade_writer:queue` in Redis with payload + retry-after timestamp. Background worker drains queue with exponential backoff (5s, 30s, 5min, 1h, 6h, dead-letter). |
| Brigade 4xx (bad payload) | Log + dead-letter immediately. Don't retry — payload won't get better. |
| FalkorDB connection error (Brigade reports `status: disconnected`) | Same as unreachable — enqueue + retry. |
| Network slow (>5s) | Default `HTTP_TIMEOUT=5.0`. Timeout treated as unreachable. |
| Org-id mismatch (sanity check) | Refuse to write (log + alert). Indicates upstream bug. |

A nightly reconciliation job (`scripts/reconcile_brigade_graph.py`)
walks recent sessions and re-issues any missing writes. This is the
safety net for "Brigade was down all night" scenarios.

### 5.5 Sample write — meeting completion

```python
# Called from services.auto_summarization_service after summary completes
async def write_meeting_full(session: RecordingSession, db: Session):
    writer = BrigadeWriter()  # picks up env config

    # 1. Meeting node + edges
    await writer.write_meeting(session)

    # 2. Speakers
    speakers = db.query(SpeakerProfile).join(
        SpeakerSessionLink,
        SpeakerProfile.id == SpeakerSessionLink.speaker_profile_id
    ).filter(
        SpeakerSessionLink.session_id == session.id
    ).all()
    await writer.write_speakers_for_meeting(session, speakers)

    # 3. Action items (already in DB from extractor)
    action_items = db.query(ActionItem).filter(
        ActionItem.session_id == session.id
    ).all()
    await writer.write_action_items(session, action_items)

    # 4. LLM-derived (topics/decisions/mentions from summary JSON)
    summary = session.final_summary or {}
    topics = extract_topics(summary)
    decisions = extract_decisions(summary)
    mentions = extract_mentions(summary, speakers)
    await writer.write_summary_entities(session, topics, decisions, mentions)

    # 5. Attachments
    attachments = db.query(SessionAttachment).filter(
        SessionAttachment.session_id == session.id
    ).all()
    for a in attachments:
        await writer.write_attachment(a)
```

Every call is `try/except` wrapped at the writer level. None of them
can break the summarization path.

### 5.6 Session move between orgs

The 2026-05-19 commit `cae99e0` already handles cross-org reassignment
in Postgres + Qdrant (re-tag). The Brigade writer's `reassign_meeting_org`
mirrors that:

```cypher
MATCH (m:Meeting {id: $session_id})
SET m.org_id = $new_org_id
WITH m
MATCH (m)-[r]-(child)
WHERE 'org_id' IN keys(child) AND child.org_id <> $new_org_id
  AND any(label IN labels(child) WHERE label IN ['ActionItem', 'Document', 'Topic', 'Decision', 'Mention'])
SET child.org_id = $new_org_id
WITH m
MATCH (m)-[r:IN_ORG]->(:Organization)
DELETE r
WITH m
MATCH (o:Organization {id: $new_org_id})
MERGE (m)-[:IN_ORG]->(o)
```

Speakers and Users are **not** cascaded — they're org-level identities
that don't follow a single meeting.

## 6. Read Pipeline: Brigade → Meeting-Ops

### 6.1 Read endpoints needed

Two new Meeting-Ops API routes (proxy + filter):

```
GET /api/insights/graph
  Query params: org_id (from auth), session_id?, speaker_id?,
                topic_id?, entity_type?, limit?, depth?
  Returns: { nodes: [...], links: [...] }
  Proxies to Brigade /api/v1/knowledge/graph with org filter applied

GET /api/insights/entity/{node_label}/{node_id}
  Returns: entity details + immediate neighbors
  Proxies to Brigade /api/v1/knowledge/entity/{name}
```

The Meeting-Ops side applies **two** filters before forwarding:

1. **Org filter** (always). Inject `org_id = $user.organization_id`
   into the Cypher params. The user's org_id is taken from the
   authenticated session, not from the request body — never trust the
   client.
2. **Session visibility filter** (per-session). If `session_id` is in
   the query, also confirm `session_id ∈ user.visible_session_ids` —
   joins `session_collaborators` and ownership. This is the same
   check the existing session detail endpoint already does.

### 6.2 Cypher template (server-side)

```python
def org_scoped_graph_query(org_id: int, filters: dict) -> tuple[str, dict]:
    """Build a parameterized Cypher query that always includes org_id.

    Returns (cypher, params). Never interpolates user-supplied strings.
    """
    cypher = """
        MATCH (m:Meeting {org_id: $org_id})
        OPTIONAL MATCH (m)<-[r:SPOKE_IN]-(s:Speaker {org_id: $org_id})
        OPTIONAL MATCH (m)-[d:DISCUSSED]->(t:Topic {org_id: $org_id})
        OPTIONAL MATCH (m)-[p:PRODUCED]->(a:ActionItem {org_id: $org_id})
        WHERE m.started_at > $since
        RETURN m, s, r, t, d, a, p
        LIMIT $limit
    """
    params = {
        "org_id": org_id,
        "since": filters.get("since", "1970-01-01"),
        "limit": min(filters.get("limit", 200), 1000),
    }
    return cypher, params
```

Every property comparison goes through `$param`, never string
interpolation. This is enforced by a CI check (Section 9.2).

### 6.3 Existing `/api/v1/knowledge/graph` does NOT enforce org isolation today

The Brigade endpoint takes `entity_type` and `name_pattern` filters
but no `org_id` — it queries the global graph. For the **per-org
graph** tenancy mode this isn't a problem (the graph itself is
already org-scoped). For the **shared graph + org_id property** mode,
we have two options:

- **Option A (chosen):** Meeting-Ops issues its **own** Cypher via
  `POST /api/v1/knowledge/query` (the NL query endpoint exists; we'd
  need a `/api/v1/knowledge/cypher` endpoint that takes Cypher
  directly). This requires a small Brigade PR to add a raw-Cypher
  endpoint behind admin auth.

- **Option B:** Add an `org_id` query param to Brigade's
  `/api/v1/knowledge/graph` endpoint. Brigade-side change, more
  invasive.

We pick **A** — it's the more powerful escape hatch and several
agents (maj-recon, etc.) already use raw Cypher via the `kg_query`
tool. The endpoint exists internally; we just expose it on the API
surface.

## 7. Frontend: 3D Graph Viewer

### 7.1 Three rendering surfaces

| Surface | Location | Phase | Implementation |
|---|---|---|---|
| Session-detail mini-widget | `/sessions/:id` right rail | Phase 2 | Native React, `react-force-graph-3d`, lazy-loaded |
| Full-screen Insights page | `/insights` | Phase 3 | Native React, full feature parity |
| Brigade deep-link | "Open in Brigade →" | Phase 2-3 | iframe-less link to `brigade.magicunicorn.dev/knowledge?graph=...` |

### 7.2 Mini-widget (Phase 2)

Right rail on session detail. Shows:
- This meeting's local subgraph (depth=1): speakers, action items,
  topics, mentioned documents.
- ~20-50 nodes max.
- Click a node → opens its detail in a side panel (re-uses the
  existing `SessionContext` shell).
- "Expand to full graph" → navigates to `/insights?seed=meeting:{id}`.

Component layout:

```
frontend/src/components/dashboard/SessionGraphWidget.tsx
  - Defines props (sessionId, height)
  - Lazy-imports Graph3DView
  - Fetches /api/insights/graph?session_id=X
  - Passes graphData to Graph3DView
frontend/src/components/graph/Graph3DView.tsx
  - Direct copy/adapt of crisis-management-ops Graph3DView.jsx
  - Uses react-force-graph-3d + three-spritetext
  - Generic — accepts graphData, getNodeColor, getLinkColor,
    onNodeClick callbacks
```

### 7.3 Full Insights page (Phase 3)

`/insights` — top-level navigation entry. Layout:

```
+----------------+-----------------------------+
| Sidebar        | 3D Graph (full-height)      |
| - All entities |                             |
| - Filter type  |                             |
| - Search       |                             |
| - Time range   |                             |
| - Org switcher |                             |
|                |                             |
+----------------+-----------------------------+
| Selected entity detail (bottom drawer)       |
+----------------------------------------------+
```

Feature parity with Brigade's `KnowledgeGraph.jsx`:
- 2D ↔ 3D toggle, fullscreen, camera reset, pause animation
- Entity type filter chips, free-text search
- Click-through to detail
- Org switcher (only visible to multi-org users — most users have
  one org and the switcher is hidden)

### 7.4 Brigade deep-link

A button on the Insights page: **"Open in Brigade UI →"**. Constructs:

```
https://brigade.magicunicorn.dev/knowledge?graph=meeting_ops__{org_slug}
```

Brigade's `KnowledgeGraph.jsx` (line 70):

```js
const apiUrl = import.meta.env.VITE_API_URL || 'http://localhost:8100/api/v1';
```

— it reads from a single graph (`brigade_global`) today. A small
Phase-3 Brigade PR adds `?graph=<name>` URL param support so the
deep-link works. This is a 30-line frontend change in Brigade.

### 7.5 Why native + deep-link rather than iframe-only

Iframe-only was tempting (zero new viewer code in Meeting-Ops). Three
reasons we chose native + deep-link instead:

1. **Auth.** Iframes need to share session cookies across origins;
   our oauth2-proxy + cross-domain setup makes that fiddly. A
   native render owns its data fetch under the Meeting-Ops origin.
2. **Layout integration.** The session-detail page needs the graph
   to live in a sidebar next to other session content (action
   items, transcript, summary). An iframe with its own
   header/sidebar/control panel doesn't fit.
3. **Per-org filtering.** The Brigade viewer queries
   `/api/v1/knowledge/graph` directly; our org filter has to be
   server-side. Putting Meeting-Ops in the data path is mandatory
   for the shared-graph tenancy mode.

The deep-link is the "I want the rich Brigade exploration UI" escape
hatch, not the primary surface.

## 8. Tenancy and Compliance Modes

| Mode | Code | Brigade URL | Graph layout | Use case |
|---|---|---|---|---|
| **Shared graph** | `shared` | Single Brigade instance | All orgs share `meeting_ops` graph, filtered by `org_id` property | dev / staging |
| **Per-org graph** | `per_org_graph` | Single Brigade instance | One graph per org (`meeting_ops__<org_slug>`) | SaaS production, multi-tenant |
| **Per-org instance** | `per_org_instance` | Different URL per org | Dedicated Brigade + FalkorDB on customer infra | HIPAA, customer-owned |

### 8.1 Env vars

```bash
# Required for all modes
BRIGADE_API_BASE_URL=http://unicorn-brigade:8100   # or full https URL for offhost
BRIGADE_ADMIN_KEY=...                              # for service-to-service writes

# Required for tenancy selection
BRIGADE_GRAPH_TENANCY_MODE=shared|per_org_graph|per_org_instance

# Required for shared mode
BRIGADE_GRAPH_NAME=meeting_ops                     # default if unset

# Required for per_org_graph mode
BRIGADE_GRAPH_NAME_PREFIX=meeting_ops              # prefix; per-org slug appended
                                                   # → meeting_ops__rocky
                                                   # → meeting_ops__shafen

# Required for per_org_instance mode
BRIGADE_INSTANCE_RESOLVER=db                       # how to resolve URL per org
# When resolver=db, Meeting-Ops reads org.brigade_url
# from a new column on organizations.
```

### 8.2 magicunicorn.dev (dev) defaults

```bash
BRIGADE_API_BASE_URL=http://unicorn-brigade:8100
BRIGADE_GRAPH_TENANCY_MODE=per_org_graph
BRIGADE_GRAPH_NAME_PREFIX=meeting_ops_dev
```

Why per_org_graph from day one: the cross-org-leak test (Section 9)
catches bugs earlier. Shared mode is too forgiving — a missing
filter looks fine until you log in as another org.

### 8.3 Legacy1 (HIPAA) defaults

```bash
# Meeting-Ops backend connects only to local Brigade
BRIGADE_API_BASE_URL=http://brigade-legacy:8100   # over Tailscale or co-located
BRIGADE_GRAPH_TENANCY_MODE=per_org_graph
BRIGADE_GRAPH_NAME_PREFIX=meeting_ops
```

Even though only one org (Legacy OB/GYN) is on this instance, we still
use per_org_graph — keeps the code path identical to magicunicorn.dev
and means a future second customer on the same box doesn't require
re-architecture.

For a customer who wants their own Brigade instance entirely
(per_org_instance), the deployment recipe is:

1. Spin up Brigade + FalkorDB stack on customer infra (or our
   colo'd box for them).
2. Same Keycloak realm (`uchub`) federated, or customer's own.
3. Meeting-Ops connects to `BRIGADE_API_BASE_URL=<customer-instance>`
   and we set `BRIGADE_GRAPH_TENANCY_MODE=per_org_instance`.

### 8.4 HIPAA-specific extras

- **BAA** with FalkorDB / Brigade host operator. For Legacy1 this is
  Shafen + Hina's own infra, so the BAA is already in place per the
  Legacy OB/GYN partnership (memory:
  `project_hina_khan_legacy_equity`).
- **Encryption at rest.** Postgres + Garage already encrypted at the
  zfs/luks layer on Legacy1. FalkorDB stores its dump on the same
  volume, inherits encryption.
- **Audit log.** Meeting-Ops audit log already captures
  user-initiated CRUD. Extend to log all `BrigadeWriter` calls
  with (user, action, target_node, target_id). Brigade-side audit
  is separate; we don't double-log.
- **No PHI in node properties.** A patient's name as a `:Speaker`
  is PHI. We're already handling that lawfully because Speaker
  records exist in the relational DB under the same compliance
  posture. The graph doesn't add new PHI exposure — it just gives
  it a graph shape.
- **Auditable deletion.** When a meeting is deleted, the graph
  writer issues `DETACH DELETE` immediately, not on a schedule. A
  failed delete falls into the retry queue and is alerted on if it
  stays there >24h.

## 9. RBAC and Cross-Org Leak Testing

### 9.1 Three layers of enforcement

1. **Graph-level isolation** (per_org_graph mode): different orgs
   literally can't see each other's data because they're in different
   FalkorDB graphs. The Cypher `MATCH` only sees nodes in the named
   graph.
2. **Property-level filter** (shared mode): every Cypher query
   includes `WHERE node.org_id = $org_id`.
3. **API gateway** (always): Meeting-Ops backend resolves
   `org_id` from the authenticated user's session, never from the
   request body. The user can't request another org's data because
   they can't ask for it.

### 9.2 CI enforcement

Add to `tests/test_cross_org_leak.py`:

```python
async def test_graph_query_isolation_shared_mode(monkeypatch):
    """In shared mode, org A user must not see org B's nodes."""
    monkeypatch.setenv("BRIGADE_GRAPH_TENANCY_MODE", "shared")
    # Seed Brigade with 2 orgs' data via direct FalkorDB
    seed_org_data(org_a_id=1, meetings=3, speakers=5)
    seed_org_data(org_b_id=2, meetings=2, speakers=4)

    # User in org A queries the graph
    resp = await client.get("/api/insights/graph",
                            headers=auth_headers(user_id=1, org_id=1))
    nodes = resp.json()["nodes"]

    # Every node must have org_id=1
    assert all(n["org_id"] == 1 for n in nodes)
    # Specifically: no org B speakers or meetings leak in
    assert not any(n["id"] in org_b_meeting_ids for n in nodes if n["label"] == "Meeting")

async def test_graph_query_isolation_per_org_graph(monkeypatch):
    """In per_org_graph mode, the graph itself is different."""
    monkeypatch.setenv("BRIGADE_GRAPH_TENANCY_MODE", "per_org_graph")
    # ...same seeding, separate graphs
    resp_a = await client.get("/api/insights/graph", headers=auth_headers(org_id=1))
    resp_b = await client.get("/api/insights/graph", headers=auth_headers(org_id=2))
    # Disjoint
    a_ids = {n["id"] for n in resp_a.json()["nodes"]}
    b_ids = {n["id"] for n in resp_b.json()["nodes"]}
    assert a_ids.isdisjoint(b_ids)

async def test_cypher_param_sanitization():
    """User-supplied search query must not break out of params."""
    payload = {"query": "'); MATCH (n) DETACH DELETE n; //"}
    resp = await client.post("/api/insights/search", json=payload,
                             headers=auth_headers(org_id=1))
    # Should sanitize, not destroy the graph
    assert resp.status_code == 200
    # And the graph still has nodes
    stats = await client.get("/api/insights/stats", headers=auth_headers(org_id=1))
    assert stats.json()["total_nodes"] > 0
```

The Cypher-injection test is the one that catches the dumb mistake.
String-interpolated Cypher is the easy bug to write and the leak we
must not ship.

### 9.3 Linter check

Add a pre-commit hook (or pytest item) that greps `backend/services/brigade_writer.py`
and `backend/api/insights*.py` for `f".*MATCH"` / `f".*MERGE"` /
`% formatting` patterns. If found, fail. Forces parameterized Cypher
only.

## 10. Phase 4: Migrating `meeting-rag-agent` to Brigade

**This phase is scoped out of this design** and tracked as a
separate ticket (P4-MIGRATE in Section 12). Documented here so the
data we write in Phase 1 supports the migration when it happens.

Today's `meeting_rag_agent` (`backend/services/agents/meeting_rag.py`)
runs a local tool-use loop:

```
User → Meeting-Ops → meeting_rag agent (local)
                        ├→ search_meetings (Qdrant)
                        ├→ chat_with_meeting (Postgres + LLM)
                        └→ ask_about_meetings (synthesis)
                     → SSE stream back to UI
```

Phase 4 swaps the agent runtime:

```
User → Meeting-Ops → Brigade /api/v1/agents/{meeting-assistant}/chat
                        ├→ MCP tools registered with Brigade:
                        │    - search_meetings
                        │    - chat_with_meeting
                        │    - query_meeting_graph (NEW — Cypher)
                        │    - find_action_items
                        │    - speaker_connections
                        └→ Agentic RAG fuses graph + vector
                     → SSE stream back to UI
```

The new tools that make the migration worth doing all use the graph:

- `query_meeting_graph` — Cypher over the meeting subgraph
- `speaker_connections(speaker_id)` — return WORKS_WITH neighbors
- `topic_drift(topic_id, days)` — find FOLLOWS_UP_FROM chain

These don't exist today and don't make sense without the graph being
populated first.

Phase 4 effort: ~2 weeks. Out of scope here. The graph writes from
Phase 1 are a prerequisite, not a dependency.

## 11. Phased Rollout

| Phase | Scope | Effort | Output |
|---|---|---|---|
| **1** | Brigade writer service + schema bootstrap + write-on-session-completion. No UI. | 1 week | `services/brigade_writer.py`, Phase-1 Brigade PR for `merge_key`, env vars, alembic noop migration, CI cross-org-leak tests |
| **2** | Read endpoints + session-detail mini-widget. Brigade deep-link button. | 1 week | `api/insights.py`, `components/dashboard/SessionGraphWidget.tsx`, `components/graph/Graph3DView.tsx` |
| **3** | Full `/insights` page with filters, search, fullscreen, time range. | 1-2 weeks | `pages/Insights.tsx`, Brigade PR for `?graph=` URL param |
| **4** | `meeting-rag-agent` migration to Brigade-hosted Meeting Assistant agent with new graph tools. | 2 weeks | Separate ticket — out of this design |
| **5** | Per-org-instance deployment scripts + HIPAA runbook for Legacy1. | 1 week | `deploy/brigade-isolated/` compose, runbook in docs |

**Total Phase 1-3:** 3-4 weeks. **Total to feature-complete + HIPAA-ready:** 5-7 weeks.

## 12. Implementation Tickets

Tag legend: **S** = ≤1 day, **M** = 1-3 days, **L** = 3-5 days.

### Phase 1

1. **P1-BRIGADE-WRITER (M)** — Create `backend/services/brigade_writer.py`
   with the public surface in Section 5.2. Implement `write_meeting`,
   `write_speakers_for_meeting`, `write_action_items` first.
   Failure paths queue to Redis. Env-driven tenancy mode.
   No call sites yet.

2. **P1-BRIGADE-PR-MERGE-KEY (S)** — Open PR in Unicorn-Brigade to
   add `merge_key` parameter to `app/knowledge/graph_manager.store_entity`
   (defaults to `name`). Backward compat. Add unit test.

3. **P1-BRIGADE-PR-CYPHER-ENDPOINT (S)** — Open PR in
   Unicorn-Brigade exposing `POST /api/v1/knowledge/cypher` for
   admin-auth parameterized Cypher (used by Meeting-Ops insights
   API). Refuses unparameterized strings via a lint pass.

4. **P1-WRITER-CALL-SITES (M)** — Wire `BrigadeWriter.write_meeting_full`
   into:
   - `services.auto_summarization_service` (after summary completes)
   - `api.simple_recording_db` finalize
   - `api.satellite_api` finalize
   - `api.session_attachments` (attachment uploaded)
   - `api.session_move_org` (reassignment)
   - `api.sessions` DELETE (DETACH DELETE)

5. **P1-RECONCILE-JOB (S)** — `scripts/reconcile_brigade_graph.py`
   walks sessions completed in the last 7 days, re-issues missing
   writes. Cron entry in deploy.

6. **P1-CROSS-ORG-TESTS (S)** — Implement the three tests in
   Section 9.2. Wire into CI.

### Phase 2

7. **P2-INSIGHTS-API (M)** — Create `backend/api/insights.py` with
   `GET /api/insights/graph`, `GET /api/insights/entity/{label}/{id}`,
   `GET /api/insights/stats`. Org filter enforced server-side. Uses
   the Cypher endpoint from P1-BRIGADE-PR-CYPHER-ENDPOINT.

8. **P2-GRAPH3D-COMPONENT (M)** — Port
   `crisis-management-ops/frontend/src/pages/graphrag/Graph3DView.jsx`
   to `frontend/src/components/graph/Graph3DView.tsx`. Make it
   generic — props are `graphData`, callbacks, sizing config.
   Add `react-force-graph-3d` and `three-spritetext` deps.

9. **P2-SESSION-WIDGET (M)** — `SessionGraphWidget.tsx` on the
   session-detail page right rail. Lazy-loads Graph3DView. Shows
   depth-1 subgraph for this meeting. Click → side panel detail.
   "Expand" button → /insights deep-link (placeholder until
   Phase 3).

10. **P2-BRIGADE-DEEPLINK (S)** — "Open in Brigade UI →" button on
    the session-detail page next to the widget. Links to
    `https://brigade.magicunicorn.dev/knowledge?graph=meeting_ops_<slug>`
    (which doesn't work fully until P3-BRIGADE-PR-GRAPH-PARAM ships;
    we can ship the button anyway, it'll degrade to a graph list).

### Phase 3

11. **P3-INSIGHTS-PAGE (L)** — `frontend/src/pages/Insights.tsx`.
    Full-screen 3D viewer with sidebar (entity types, search, time
    range, org switcher), bottom drawer for selected entity detail,
    2D/3D toggle, fullscreen mode, pause animation. Feature parity
    with Brigade's `KnowledgeGraph.jsx`.

12. **P3-INSIGHTS-FILTERS (M)** — Time range slider, entity type
    chips, free-text search. Server-side filter pushdown to the
    Cypher template.

13. **P3-BRIGADE-PR-GRAPH-PARAM (S)** — Brigade PR to support
    `?graph=<name>` URL parameter in `KnowledgeGraph.jsx`. ~30 lines.

### Phase 5 (HIPAA / Legacy1)

14. **P5-LEGACY1-BRIGADE-DEPLOY (M)** — `deploy/brigade-legacy1/`
    compose file for a standalone Brigade + FalkorDB stack on
    Legacy1. Tailscale connection from Meeting-Ops backend.
    Runbook.

15. **P5-HIPAA-RUNBOOK (S)** — Add HIPAA-specific section to
    Meeting-Ops deploy docs: BAA checklist, encryption-at-rest
    verification, audit log retention, breach response, customer
    data export.

## 13. Open Questions for Aaron

1. **Default tenancy on magicunicorn.dev:** I'm recommending
   **per_org_graph** from day one (vs shared) because the
   cross-org-leak test is more catchable when graphs are physically
   separated. Confirm or override?

2. **New service key vs reuse `BRIGADE_ADMIN_KEY`:** Phase 1 can ship
   using `BRIGADE_ADMIN_KEY` for the writer. Phase 1.5 mints a
   `meeting-ops` user-scoped service key via Brigade's admin API
   so we can revoke independently. OK to ship with admin key first
   and rotate before customer #1?

3. **Existing `majiks_research` graph:** keep it isolated from
   Meeting-Ops (different graph name), or eventually federate
   (news → meeting → action item cross-graph queries)? Current
   design keeps it separate. Aaron's stated vision in the prompt
   suggests separate is fine for now.

4. **`Meeting-RAG-Agent` migration urgency:** Phase 4 ships when?
   Right now the local agent works fine and only the graph would
   make it better. Is there a customer-facing reason to expedite,
   or do we let Phase 1-3 land + bake before swapping the agent
   runtime?

5. **`react-force-graph-3d` vs sigma.js / cytoscape:** This design
   commits to `react-force-graph-3d` because Brigade and Crisis-Ops
   both use it. If you want a different lib (cytoscape is more
   feature-rich for 2D / large graphs), now is the time. The
   Graph3DView component is the right abstraction layer to swap
   underneath.

6. **`Brigade ?graph=` parameter PR:** I'm assuming I can open this
   PR against Unicorn-Brigade without waiting on the Brigade owner
   (presumably Aaron). Confirm or route through a different process?

7. **Audit log retention for HIPAA:** Today Meeting-Ops audit log
   retains forever. HIPAA wants 6 years minimum. Confirm 6 years
   for Legacy1 deploy, or is retention already explicitly set?

8. **`session_collaborators` → `CAN_VIEW` edges:** Today
   `session_collaborators` is the only per-session ACL. In the
   shared-graph tenancy mode, do we materialize `CAN_VIEW` edges
   eagerly (write-time), or query-time? Eager is faster to read,
   slower to write + more nodes to maintain. Current design says
   eager — confirm.

## 14. Effort Estimate

- **Phase 1-3 (working visualization + writes):** 3-4 weeks
  focused.
- **Phase 4 (agent migration):** +2 weeks.
- **Phase 5 (HIPAA deploy):** +1 week.
- **Total to "Aaron can sell this to Shafen for Legacy OB/GYN":**
  6-7 weeks if Phase 4 runs in parallel with Phase 5.

A single engineer can land Phase 1 in week 1 with no Brigade-side
PR blocker (the `merge_key` PR is ~50 lines and self-mergeable).

## 15. Why this design and not the alternatives

- **Why not embed FalkorDB directly in Meeting-Ops?** Then we
  duplicate the agent runtime, the schema bootstrap, the
  Cypher-injection guardrails, the OIDC integration, the
  visualization, and the cross-app federation. Brigade already
  did that work. Doing it again would be the slow expensive
  mistake.
- **Why not skip the graph entirely and just add more Postgres
  columns?** Because the questions Aaron actually wants to ask
  ("everyone Mohsin has been mentioned with, with their action
  items, across meetings, in the last 90 days") are graph
  queries. They're hideous in SQL and trivial in Cypher. And
  they get more valuable as the data grows — exactly the
  scenario where Meeting-Ops adoption inside an org becomes the
  product moat.
- **Why not let Brigade write the entities itself by ingesting
  Meeting-Ops via a webhook?** Because Brigade doesn't know the
  Meeting-Ops schema. Inverting the dependency means Brigade has
  to understand `recording_sessions` + `speaker_session_link` +
  `action_items`, which couples the two services in the wrong
  direction. The writer in Meeting-Ops translates Meeting-Ops
  shapes to graph shapes once, in one place, and Brigade stays
  domain-agnostic.
- **Why per_org_graph as default rather than shared?** Because
  one missing `WHERE org_id = $org_id` clause leaks every other
  customer's data. Per-org-graph makes the leak structurally
  impossible — the Cypher physically can't see another graph's
  nodes. We pay a small per-org overhead to make a whole class
  of bug impossible.
- **Why not require HIPAA deployment to mean per_org_instance
  (separate Brigade)?** Because we want the same code path on
  dev / SaaS / customer-isolated. Forcing different code branches
  for different compliance modes is how compliance bugs ship. Per_org_graph
  gives strong-enough isolation for the "shared infrastructure
  with BAA in place" model; per_org_instance is for the customer
  who wants their own metal. Both are first-class.

## 16. Risk Register

| Risk | Mitigation |
|---|---|
| Brigade outage breaks recordings | Graph writes are best-effort + queued. Recording path never depends on graph. |
| Cypher injection via search input | Parameterized queries only. CI grep linter. Cross-org-leak test exercises injection payloads. |
| FalkorDB OOMs with one giant org | Per_org_graph isolates blast radius. Limit `/api/insights/graph?limit=200` default + hard cap 1000. |
| Schema drift between Postgres and graph | Reconciliation job runs nightly. Audit alert on >1% drift. |
| Brigade `BRIGADE_ADMIN_KEY` leak | Rotate. Phase 1.5 moves to scoped `meeting-ops` user-key. |
| User asks "where's my data really stored?" (HIPAA audit) | Document the layered storage model: Postgres = authoritative, Qdrant = vector index, FalkorDB = denormalized read model. All three under the same BAA. |
| 3D viewer perf with >2000 nodes | Filter chips + time range default to last 90 days. Aggregate stale meetings into `:MeetingCluster` nodes (Phase 6 stretch). |

## 17. Glossary

- **Brigade** — Unicorn Brigade, the agent runtime + knowledge graph
  service. API at `brigade.magicunicorn.dev`, backend container
  `unicorn-brigade`.
- **FalkorDB** — the graph database Brigade uses for its knowledge
  graph. Container `unicorn-falkordb`.
- **Graph (FalkorDB sense)** — a named partition inside a FalkorDB
  instance. Brigade has ~25 today (`brigade_global`,
  `majiks_research`, `agent_*`, `<user>__<agent>`).
- **Per-org graph** — Meeting-Ops adds graphs named
  `meeting_ops__<org_slug>`. Each org is in its own FalkorDB graph.
- **Per-org instance** — A whole separate Brigade + FalkorDB stack
  for one customer (e.g., Legacy1 for Legacy OB/GYN). Same code
  path, different `BRIGADE_API_BASE_URL`.
- **`react-force-graph-3d`** — the 3D graph viz library Brigade
  and Crisis-Ops use. Picked for Meeting-Ops too.
- **`meeting-rag-agent`** — Meeting-Ops' current local LLM tool-use
  loop for question-answering. Migrates to Brigade in Phase 4.
- **Layer 1 / 2 / 3** — deterministic / LLM-derived / batch-computed
  nodes and edges. Phase 1-2 ship Layer 1+2. Layer 3 is follow-up.

---

End of design.
