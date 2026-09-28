# Speaker intelligence design

Status: live. Shipped across v3.42.0–v3.45.0; running on prod and dogfood as
of v3.45.0 (2026-06-20).

This document is the engineer-facing reference for how Meeting-Ops turns an
anonymous diarized transcript ("Speaker 1", "Speaker 2") into stable, named
people whose identity is recognised across every future meeting — and how
naming a speaker once corrects them everywhere, instantly.

Primary code:
- `backend/services/speaker_service.py` — embedding math, identify lifecycle,
  persistent-identity auto-create, live name hydration.
- `backend/api/speakers.py` — speaker library + session-link API, rename
  propagation, re-summarize.
- `backend/database/models.py` — `SpeakerProfile`, `SpeakerVoiceSample`,
  `SpeakerSessionLink`.
- `services/speaker-svc/main.py` — `meet-speaker-svc`: pyannote diarization +
  wespeaker embeddings (`/diarize`, `/embed`, `/identify`).

## 1. Overview

The pipeline is **diarize → identify → persist identity → name once, fixed
everywhere**:

1. The server completion pass diarizes a meeting into per-turn clusters with a
   voice embedding per turn (`meet-speaker-svc`, pyannote).
2. `identify_speakers()` pools one voiceprint per cluster and matches it against
   the org's enrolled speakers by cosine similarity.
3. Any unmatched voice (above a minimum clean-speech duration) becomes a
   **persistent UNNAMED profile** with a stable handle (e.g. `Speaker 3F2A`),
   so the *same voice* matches the *same profile* in the next meeting.
4. The display name for every transcript segment is **resolved live at serve
   time** from the current `SpeakerProfile` via the per-session link. Naming a
   speaker is therefore one profile-row update that takes effect everywhere on
   the next render — no transcript rewrite.

The unit of identity is the org-level `SpeakerProfile`; voiceprints are hard
tenant-scoped (biometric privacy). The single sanctioned cross-scope path is a
user's own account-level "My Voice" (`UserVoiceProfile`), matched only inside
workspaces the user is a member of — see `my_voice_candidates_for_org()`.

## 2. Data model

Three tables (`backend/database/models.py`):

### `speaker` — `SpeakerProfile`
One row per real person an org wants to recognise across meetings.
- `organization_id` — hard tenant scope. `(organization_id, display_name)` is a
  unique index (`ix_speaker_org_name`), so a same-name profile for a different
  person is suffixed ` (2)`.
- `display_name` — the name shown in transcripts; for auto-created profiles this
  is the handle until a human names it.
- `centroid_embedding` (`LargeBinary` → Postgres `bytea`) + `embedding_dim`
  (`Integer`) + `embedding_model` (`String`) — the enrolled voiceprint. The
  centroid is the running mean of the profile's samples (L2-normalized).
- `external_refs` (JSONB) — `{"auto_generated": true}` flags an unnamed
  auto-created profile (`is_auto_generated_speaker()`).
- `sample_count` — number of folded voice samples behind the centroid.
- Contact fields (`email`, `phone`, `title`, `company`, `contact_id`,
  `linked_user_id`, …) make the profile double as a per-org contact record.

### `speaker_voice_sample` — `SpeakerVoiceSample`
One embedding (and optionally the audio bytes) that contributed to a centroid.
- `speaker_id`, `organization_id`.
- `source` — `"enrollment"` (uploaded clip), `"session"` (enroll-from-meeting),
  `"assign_confirm"` (harvested on a confirmed tag), `"auto_identify"`
  (auto-created profile bootstrap).
- `source_session_id` — used for **idempotency**: at most one sample per
  `(speaker_id, source_session_id)`.
- `embedding` (`LargeBinary`, not null) + `embedding_dim` + `embedding_model`.
- `similarity_to_centroid` — diagnostic cosine of the sample to the resulting
  centroid.
- `audio_path` — only set when `STORE_SPEAKER_AUDIO=true` (off by default).

### `speaker_session_link` — `SpeakerSessionLink`
Maps one session's raw diarized label to a profile. One row per
`(session_id, raw_label)` (unique index `uq_speaker_session_link_label`). **This
table is the join that makes live name rendering possible.**
- `session_id`, `organization_id`.
- `raw_label` — the diarizer's cluster id (e.g. `SPEAKER_00`). Survives the
  "Speaker N" display normalization so a re-identify still lines up with the
  diarizer's clusters.
- `speaker_id` — the resolved profile (nullable: an unmatched cluster has a row
  with `speaker_id IS NULL` so the tagging UI has something to attach to).
- `similarity` — cosine of the match (`1.0` for user-asserted).
- `source` — `"auto"` (identify) or `"manual"` (user tagged).
- `confirmed` — user intent. Confirmed links are never overwritten by a
  re-identify and never pruned.

### Embedding serialization
Embeddings are stored as **raw little-endian float32 bytes** plus an
`embedding_dim` INT — see `encode_embedding()` / `decode_embedding()`
(`numpy.asarray(vec, dtype="<f4").tobytes()`). Decode always derives the count
from the byte length; `embedding_dim` is a cross-check. This layout is
model-agnostic: the current wespeaker resnet34 (256-d) and any future
256-/768-d model share the same row shape.

## 3. Diarization

Diarization runs in `meet-speaker-svc` (`services/speaker-svc/main.py`) on the
GPU during the completion pass. It lazy-loads
`pyannote/speaker-diarization-community-1`, falling back to
`pyannote/speaker-diarization-3.1` if community-1 can't load; both require a
`HUGGINGFACE_TOKEN`. Per-turn embeddings come from
`pyannote/wespeaker-voxceleb-resnet34-LM` (`Inference(window="whole")`). With no
HF token the service degrades to a single whole-clip "speaker"
(`ecapa-cluster-fallback`).

**Clustering threshold.** The pyannote clustering threshold is fixed at startup
from `PYANNOTE_CLUSTERING_THRESHOLD` (default **0.72**) with
`PYANNOTE_MIN_DURATION_OFF` (default 0.5). 0.72 sits a hair above the model
default and biases toward merging when in doubt: lower (~0.65) over-splits one
person's varied speech into several clusters; the model default (~0.7045) merged
distinct voices into one on real meetings. Per-request threshold overrides were
**removed** (Task #79 v2) — they didn't map cleanly onto the pyannote 3.1/4.x
parameter graph and caused over-splitting; tune via env per deploy instead.

The diarizer persists fine-grained turns in
`transcript_diarized["speaker_turns"]`. Identification prefers these over the
coarse multi-minute text segments, whose single pooled embedding is too mushy to
match anyone (a 5-minute "segment" averages crosstalk/echo to ~0.05
self-similarity).

## 4. Identification

`identify_speakers(session, db)` (`speaker_service.py`) is the post-completion
hook:

1. Group segments by `raw_label`; pool one voiceprint per cluster via
   `pooled_embedding()` — up to the 5 longest turns in a 2–45 s clean band,
   L2-normalized → averaged → re-normalized.
2. Build candidate centroids: org enrolled speakers (`candidates_for_org()`,
   matched through `provider.identify`) plus the org's members' "My Voice"
   profiles (`my_voice_candidates_for_org()`, matched **locally** with numpy
   cosine — never sent to the external provider).
3. Match by **cosine similarity**. A cluster is identified when its best match
   is at or above `SPEAKER_IDENTIFY_THRESHOLD` (default **0.55**). The
   speaker-svc `/identify` endpoint uses the same 0.55 default. Higher
   similarity wins; ties go to the org library.
4. Persist one `SpeakerSessionLink` per cluster (`source="auto"`,
   `similarity`), and prune stale unconfirmed links from earlier passes.

`cosine_similarity()` is the standard normalized dot product. Centroids are
maintained as an **online running mean** by `update_centroid()`
(`(old*count + new)/(count+1)`, then L2-normalized), so enrollment never has to
re-read all samples.

## 5. Persistent identity (v3.43)

Before v3.43 an unmatched voice produced a throwaway per-session "Speaker N"
that meant nothing in the next meeting. Now, when a cluster matches no enrolled
speaker, `auto_create_speaker()` creates a **stable UNNAMED profile** and
enrolls its voiceprint:

- `_generate_speaker_handle()` mints an org-unique handle like `Speaker 3F2A`
  (4 hex chars) and stores it as `display_name`.
- `external_refs = {"auto_generated": true}` flags it; `is_auto_generated_speaker()`
  reads the flag so the UI can prompt for a one-click name and a rename knows to
  fix history.
- The pooled embedding is folded in via `add_voice_sample(source="auto_identify")`.

Guard: auto-create only fires when `SPEAKER_AUTOCREATE_ENABLED` (default on) and
the cluster has at least `SPEAKER_AUTOCREATE_MIN_SECONDS` (default **4.0**) of
clean speech, so noise/crosstalk clusters don't spawn junk profiles. It is
skipped for a label the user already confirmed. It covers both the bootstrap
case (org has no enrolled speakers yet) and the enrolled-but-no-match case
(`_autocreate_if_eligible()` inside `identify_speakers`).

Result: the **same voice → same profile (same handle)** across meetings, and
naming it once propagates everywhere (section 7).

## 6. Anti-poisoning enrollment floor (v3.42)

A centroid is only as trustworthy as the samples folded into it. A mislabeled or
mixed diarization cluster (two voices pooled, or a wrong confirm) sits at low
cosine to the real voice; averaging it in drifts the centroid toward the wrong
person and compounds future mis-IDs. `add_voice_sample()` guards against this:

- **First sample bootstraps unconditionally** (`sample_count == 0`).
- Once a profile has an enrolled centroid (`sample_count >= 1`), a new sample is
  folded **only if** its cosine to the existing centroid is at or above
  `SPEAKER_ENROLL_CONSISTENCY_FLOOR` (default = the identify threshold, **0.55**
  — a sample that wouldn't even *match* the profile should not *train* it).
  Inconsistent or dim-mismatched samples are skipped (return `None`); the
  session is still labeled correctly, the centroid is just protected.
- **Idempotency:** a sample from an already-seen `source_session_id` is skipped
  (returns the existing row), so re-confirming a session never double-weights
  that cluster in the running mean.

The account-level "My Voice" path has the analogous, more lenient
`MY_VOICE_FOLD_CONSISTENCY_FLOOR` (default 0.30) in
`fold_embedding_into_user_voice()` — looser because it is is_me-gated to a single
identity, and a dim change there would be a voiceprint-replacement primitive so
it is refused outright (re-enroll requires an explicit delete).

## 7. Dynamic name rendering (v3.44 → v3.45)

**This is the key architectural decision.** The display name shown for a speaker
is not baked into the stored transcript — it is resolved **live at serve time**
from the current `SpeakerProfile` through the session's `SpeakerSessionLink`
rows: `raw_label → speaker_id → display_name`.

`hydrate_diarized_speaker_names(diarized, db, session_id)` and the convenience
wrapper `hydrate_diarized_for_session(session)` do this:
- Read the session's links (those with a `speaker_id`), build a
  `raw_label → display_name` map from the current profiles, and return a **copy**
  of the diarized doc with each segment's `speaker` set to the live name.
- Read-only: no commit, no ORM mutation. Cheap (two indexed queries + an
  in-memory map), safe to call on every serve. Segments with no resolvable link
  keep their stored name (backward compatible), and any error falls through to
  the unmodified doc — serving must never fail on hydration.

This hydration is applied at **every transcript display surface**: session
detail, exports, AI-chat / LLM context, and the always-on payload. The
consequence:

- A rename is **one profile-row update** (`PATCH /api/speakers/{id}`). The UI is
  correct on the next render with zero transcript rewriting.
- `apply_rename_to_history()` **no longer rewrites transcripts at all** (v3.44).
  Because the diarized doc renders live, the function now only fixes the
  **AI summary / insights free text** — LLM-generated prose that can't be
  rendered live and so must be string-replaced in place. It is cheap (string ops,
  no LLM, no re-STT).
- That summary fix-up runs **off the request path** via the `_propagate_rename_bg`
  BackgroundTask (opens its own DB session), so the rename returns instantly even
  for a speaker who appears in many meetings.

Single source of truth: the `SpeakerProfile` row.

**Two inherent exceptions** (places that necessarily bake a name at write time
and so are not auto-corrected by hydration):
1. **Ingest paths** — satellite / websocket capture writes labels at capture
   time, before any profile exists.
2. **The Qdrant search index** — the dense/sparse vectors embed the text as it
   was indexed; a rename needs a manual reindex to update search.

## 8. Rename + re-summarize flow

`PATCH /api/speakers/{speaker_id}` (`update_speaker`, `backend/api/speakers.py`):
- Updates `display_name` (admin + `speaker_library` tier gate). If the profile
  was `auto_generated`, the flag is cleared — naming it makes it user-owned.
- On a real rename, schedules `_propagate_rename_bg(speaker_id, org_id, old_name)`
  as a BackgroundTask → `apply_rename_to_history()` (summary/insights free-text
  fix only). Transcripts are already live-correct via hydration.

Inline tagging on a session (`PATCH .../speaker-links/{id}`,
`.../create-speaker`, `.../enroll`, `.../merge`) confirms a link, optionally
harvests a consistent voice sample (`source="assign_confirm"` / `"session"`),
and propagates the name into that session's summary via
`_propagate_speaker_rename()`. Each enqueues a lightweight summary refresh
(`_enqueue_summary_refresh` → `finalize_session_job`: identify → summarize, no
re-STT/diarize, ~40–60 s) so the regenerated summary uses the new name.

`POST /api/speakers/{speaker_id}/resummarize-history` (`resummarize_speaker_history`)
is the optional "regenerate the summaries too" button: it enqueues that
lightweight finalize for every completed past meeting the speaker appears in and
returns how many were enqueued. Transcript labels are already corrected; this
only re-runs the LLM summary.

## 9. Config / env knobs

| Env var | Default | Effect |
| --- | --- | --- |
| `SPEAKER_IDENTIFY_THRESHOLD` | `0.55` | Min cosine for an identify match (org + My Voice). |
| `SPEAKER_ENROLL_CONSISTENCY_FLOOR` | = identify threshold (`0.55`) | Min cosine to fold a new sample into an established centroid (anti-poisoning). |
| `SPEAKER_AUTOCREATE_ENABLED` | `true` | Auto-create persistent UNNAMED profiles for unmatched voices. |
| `SPEAKER_AUTOCREATE_MIN_SECONDS` | `4.0` | Min clean-speech duration before a cluster earns a persistent profile. |
| `SPEAKER_EMBEDDING_MODEL` | `pyannote/wespeaker-voxceleb-resnet34-LM` | Default embedding model recorded on profiles/samples. |
| `MY_VOICE_FOLD_CONSISTENCY_FLOOR` | `0.30` | Min cosine to fold into the account-level "My Voice" profile. |
| `STORE_SPEAKER_AUDIO` | `false` | Persist voice-sample audio bytes to disk (off for privacy). |
| `SPEAKER_CONTACT_AUTOSTAMP_THRESHOLD` | `0.80` | Min link similarity to auto-stamp a confirmed contact as a participant. |
| `PYANNOTE_CLUSTERING_THRESHOLD` | `0.72` | Diarizer clustering threshold (`meet-speaker-svc`, fixed at startup). |
| `PYANNOTE_MIN_DURATION_OFF` | `0.5` | Diarizer segmentation min-off (`meet-speaker-svc`). |
| `HUGGINGFACE_TOKEN` | (unset) | Required to load the pyannote diarizer; absent → whole-clip fallback. |

## 10. Test coverage

- `backend/tests/test_identify_with_embeddings.py` — the core identify contract:
  match when an embedding + enrolled speaker are present (`source="auto"` link
  above threshold); `reason="no_embedding"` for embedding-less segments without
  touching manual links; `reason="below_threshold"` left unmatched; embedding
  preserved through word-level alignment;
  `test_unmatched_voice_auto_creates_persistent_profile` (v3.43);
  `test_hydrate_renders_live_speaker_name` /
  `test_hydrate_for_session_wrapper_renders_live_speaker_name` (v3.44 live
  rendering); `test_rename_propagates_to_history`.
- `backend/tests/test_my_voice.py` — the enrollment floor + idempotency
  (`test_enroll_consistency_floor_rejects_mismatched_voice`,
  `test_org_enroll_floor_and_idempotency`), the My Voice lifecycle, member
  isolation, is_me claim/conflict, and that a dim-mismatched candidate never
  aborts an identify pass.
- `backend/tests/test_speakers_unassigned_and_merge.py` — unassigned-link
  listing + ordering, and profile merge (samples/links moved, source deleted,
  audit written, cross-org/self-merge refused, rollback on error).
- `backend/tests/test_speaker_contact_autostamp.py` — confirmed speaker→contact
  participant stamping gates.
- `backend/tests/test_speaker_labels.py`,
  `backend/tests/test_speaker_self_intro.py` /
  `test_speaker_self_intro_endpoints.py` — label normalization and self-intro
  name suggestions.
- `services/speaker-svc/tests/` — the diarization/embedding service
  (`test_healthz_synthetic.py`).
