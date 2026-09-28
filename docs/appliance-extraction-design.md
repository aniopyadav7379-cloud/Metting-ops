# Appliance Build Extraction Design

**Status**: Design only, no extraction performed.
**Date**: 2026-05-20
**Cloud repo branch**: `main` @ v0.7.2 (commit 81e4cf2)
**Author**: Claude (acting on Aaron's design-only directive)

## 1. Why split

UC-Meeting-Ops has been quietly running as two products in one repo:

- **Cloud build** (current production at `meetingops.magicunicorn.dev`): server-side STT
  via `services/providers/registry.py` (Parakeet 1.1B on midboy2 RTX 3060), server-side
  LLM via LiteLLM to bigboy's Qwen 3.6 35B, Postgres + Qdrant + Garage stack, browser
  STT fallback via Parakeet 0.6B INT8. Multi-tenant. Public oauth2-proxy SSO. Air-cooled,
  HA, the whole works.

- **Appliance build** (target: AMD Ryzen AI NPU SMB box): WhisperX on AMD Phoenix/Hawk
  Point NPU via MLIR-AIE2 kernels, local llama.cpp Vulkan LLM (GPT-OSS 20B or Granite
  3.3 2B fallback), local Postgres + Qdrant + Redis, single-tenant, often air-gapped,
  bare-metal Ubuntu Server 24.04+ install via `install-meeting-ops.sh`.

These started life as the same product. Twelve months in, they share frontend, API
surface, database models, and the provider-protocol layer — but the STT/diarization
implementation, the deploy story, the security model, the support model, and the
hardware assumptions have completely diverged. The cloud build's STT runs over HTTP
against Parakeet; the appliance build's STT pokes `/dev/accel/accel0` and mmap's
pre-compiled NPU binaries. They are not the same product.

**Why now**: the cloud build hit v0.7.2 production with `DISABLE_LOCAL_AUDIO=1` as the
gate that keeps appliance code inert in containers. The gate works for runtime, but
the appliance imports are still module-level in 14+ cloud files, the appliance models
still get downloaded by `install-meeting-ops.sh`, the cloud Dockerfile still ships
NPU integration code it can never execute, and every new cloud feature has to "remember"
it's also touching the appliance code path. Two repos with a clean protocol contract
is less friction than one repo with a guard flag.

**What this design is not**: it is NOT a v1 / v2 split. The cloud build is the live
product; the appliance build is the next product. Extraction happens when the cloud
work surface is calm enough to move appliance code without disrupting the cloud
service. Today is not that day.

## 2. New repo proposal

**Recommended name**: `UC-Meeting-Ops-Appliance`

Considered alternatives and rejected:

- `UC-Meeting-Ops-NPU` — too narrow. The appliance is more than NPU; it's also the
  installer, the headless ops model, the SMB-target deploy. NPU is the marketed
  acceleration but not the product identity.
- `UC-Meeting-Box` / `Meeting-Box` — cute, but loses the UC-Meeting-Ops lineage and
  makes the Forgejo group ownership less obvious.
- `Unicorn-Meeting-Appliance` — too long; "UC" is the established prefix across
  Unicorn-Ecosystem and Forgejo.

`UC-Meeting-Ops-Appliance` keeps the lineage explicit, signals that it depends on
UC-Meeting-Ops (cloud), and reads correctly in the Forgejo project list next to
`UC-Meeting-Ops`, `UC-Project-Ops`, `UC-Crisis-Ops`, etc.

**Recommended Forgejo path**: `git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops-Appliance`
(private, same group as cloud).

**Initial version**: v0.1.0. Even though the code is mature, the repo is new and the
deploy/install story is being repackaged. Starting at 0.1.0 lets us iterate on the
appliance packaging without compatibility promises while the cloud repo stays at
0.7.x.

**Suggested repo structure**:

```
UC-Meeting-Ops-Appliance/
├── README.md
├── INSTALL.md
├── install-meeting-ops.sh           # appliance-specific, moves from cloud repo
├── docker-compose.appliance.yml     # the appliance compose (NPU device passthrough, /dev/snd)
├── Dockerfile.appliance             # builds on top of cloud backend image
├── .env.appliance.example
├── core/                            # GIT SUBMODULE → UC-Meeting-Ops @ pinned tag
├── src/
│   ├── stt_engine/
│   │   ├── whisperx_npu_engine.py
│   │   ├── whisperx_npu_engine_real.py
│   │   └── npu_accelerator.py
│   ├── npu_optimization/
│   │   ├── whisperx_npu_integration.py
│   │   ├── unified_stt_diarization.py
│   │   ├── aie2_kernel_driver.py    # already in cloud, will move
│   │   └── direct_npu_runtime.py    # already in cloud, will move
│   ├── services/
│   │   ├── live_recording_transcription.py
│   │   ├── whisper_server_client.py
│   │   ├── npu_whisper_transcriber.py
│   │   ├── whisperx_npu_service.py
│   │   ├── whisperx_py313.py
│   │   ├── real_whisper_service.py
│   │   └── providers/
│   │       ├── impl_stt_npu.py      # NEW: implements STTProvider against NPU stack
│   │       └── impl_diarization_npu.py  # NEW: implements DiarizationProvider against NPU
│   ├── npu_runtime.py
│   └── transcription_with_diarization.py
├── deploy/
│   ├── appliance/
│   │   ├── docker-compose.appliance.yml
│   │   ├── systemd/
│   │   └── udev/                    # /dev/accel/accel0 access rules
│   └── models/                      # WhisperX large-v3 + diarization model fetchers
├── docs/
│   ├── INSTALL.md
│   ├── NPU-SETUP.md
│   ├── air-gap-deployment.md
│   └── companion-app-integration.md
└── tests/
    └── npu_smoke_tests/             # runs only on hardware with /dev/accel/accel0
```

The pattern: the appliance repo is a **thin sidecar** that provides NPU-specific
providers + a different compose/install pathway, and consumes the cloud repo's
backend/frontend through the `core/` submodule. Most of the cloud code stays
upstream and is shared by reference.

## 3. What moves vs what stays

### Moves to appliance repo

The 12 files in the brief, all confirmed present and sized:

| File | LOC | What it does | Cloud-build importers | DISABLE_LOCAL_AUDIO gated? |
|------|-----|--------------|----------------------|---------------------------|
| `backend/services/live_recording_transcription.py` | 491 | Chunked live-recording loop. Calls whisper.cpp server (Vulkan iGPU) primary + transcription_service (CPU faster-whisper) fallback. Redis pub/sub for downstream summarization. | `api/simple_recording_db.py`, `api/websocket_auto_summary.py`, `services/always_on_recorder.py`, `services/unified_agent_service.py`, `services/auto_summarization_service.py` (doc reference) | **Module-level import, NOT gated.** Singleton instantiated on import. |
| `backend/services/whisper_server_client.py` | 105 | HTTP client for whisper.cpp Vulkan server at `:8178` with `large-v3-turbo`. | `api/ai_settings.py`, `api/websocket_remote_audio.py`, `services/providers/impl_stt.py` (doc reference only) | Lazy import inside endpoint handlers. Safe. |
| `backend/services/npu_whisper_transcriber.py` | 93 | Wrapper that imports `WhisperXNPUService` and exposes a `transcribe_audio` shim. | `services/live_transcription_service.py`, `transcription_with_diarization.py` | Module-level. Not gated. |
| `backend/services/whisperx_npu_service.py` | 357 | The NPU singleton. Probes `/dev/accel/accel0`, lazy-loads WhisperX large-v3 + diarization pipeline. **Has DISABLE_LOCAL_AUDIO awareness for logging** but the singleton object still gets created on import. | `api/websocket_transcription.py`, `api/websocket_auto_summary.py` (both `from services.whisperx_npu_service import whisperx_service` at module level) | Partial. Logging is gated; object instantiation is not. |
| `backend/services/whisperx_py313.py` | 231 | Python-3.13-compatible WhisperX-style transcriber (`faster_whisper` + `pyannote`). | Not currently imported by anyone. Dead code or future-experimental? | N/A — unreferenced. |
| `backend/services/real_whisper_service.py` | 195 | NPU-accelerated transcribe-file service. Lazy-loads WhisperX NPU engine. Module-level singleton `real_whisper_service = RealWhisperService(model_name="large-v3")`. | `api/simple_recording_db.py`, `api/websocket_remote_audio.py`, `api/websocket_satellite.py` (all module-level imports) | **Not gated at module level.** |
| `backend/stt_engine/whisperx_npu_engine.py` | 63 | Router shim: imports `whisperx_npu_engine_real` if available, falls back to mock. Honors `CPU_ONLY_MODE` env. | `services/transcription_service.py`, `services/stt_model_manager.py`, `npu_optimization/whisperx_npu_integration.py` | Has its own `CPU_ONLY_MODE` gate, separate from DISABLE_LOCAL_AUDIO. |
| `backend/stt_engine/whisperx_npu_engine_real.py` | 607 | The real NPU implementation. Loads MLIR-AIE2 kernels, drives `/dev/accel/accel0`. | `services/transcription_service.py` (line 240), `stt_engine/whisperx_npu_engine.py` | Lazy, gated by `CPU_ONLY_MODE`. |
| `backend/stt_engine/npu_accelerator.py` | 202 | Lower-level NPU binary mmap'er. | `stt_engine/whisperx_npu_engine_real.py` only | Internal to NPU stack. |
| `backend/npu_optimization/whisperx_npu_integration.py` | 359 | "Production-ready" WhisperX-NPU integration using `aie2_kernel_driver`. | `npu_optimization/whisperx_npu_engine.py` (note: re-exports `WhisperXNPUEngine` from here) | Internal. |
| `backend/npu_optimization/unified_stt_diarization.py` | 263 | Reference list of models that do STT + diarization in one pass. Mostly a config registry. | Not imported by anything currently. | N/A — unreferenced. |
| `backend/npu_runtime.py` | 407 | Low-level NPU runtime: ioctl, mmap, ONNX loading, wave file decoding. | `services/stt_model_manager.py`, `services/transcription_service.py` (line 24, module-level) | Module-level in `transcription_service.py`. Cloud build can't avoid loading this on startup today. |

**Total**: 3,373 lines of clearly-appliance code spread across 12 files.

Additional files that show up as importers and also belong in the appliance repo:

- `backend/npu_optimization/aie2_kernel_driver.py` — MLIR-AIE2 driver wrapper.
- `backend/npu_optimization/direct_npu_runtime.py` — alternative direct runtime path.
- `backend/stt_engine/whisper_npu_transcriber.py` — ONNX-based NPU transcriber, referenced
  by `stt_model_manager.py` model-availability checks.
- `backend/services/transcription_service.py` — **mixed**. Its docstring already calls
  out cloud vs appliance behavior, but it's the unifying coordinator. See "stays" below.
- `backend/transcription_with_diarization.py` — top-level script, appliance-only.

### Stays in cloud repo

- All FastAPI routers (`backend/api/*.py`) — cloud is the canonical API surface.
- Frontend (`frontend/`) — single React UI both products use.
- Database models + migrations (`backend/database/`, `backend/models/`).
- Cloud provider implementations:
  - `backend/services/providers/protocols.py` (the Protocol contract — load-bearing)
  - `backend/services/providers/registry.py`
  - `backend/services/providers/impl_stt.py` (Parakeet + whisper-server HTTP clients)
  - `backend/services/providers/impl_diarization.py`
  - `backend/services/providers/impl_llm.py`
  - `backend/services/providers/impl_tts.py`
  - `backend/services/providers/impl_embeddings.py`
  - `backend/services/providers/impl_reranking.py`
- Settings, agents, summarization orchestration, websocket transport.
- Deploy: `deploy/bigboy/*` (production cloud), `deploy/midboy1/*` (Parakeet worker),
  `docker-compose-full-stack.yml`, `docker-compose.prod.yml`, `docker-compose.dev.yml`.

### Interface layer (load-bearing shared contract)

`backend/services/providers/protocols.py` is the integration point. It defines six
`@runtime_checkable` Protocols: `LLMProvider`, `EmbeddingsProvider`, `STTProvider`,
`TTSProvider`, `RerankingProvider`, `DiarizationProvider`. The cloud repo implements
all six against HTTP services (LiteLLM, Parakeet, Infinity, etc.). The appliance repo
will implement `STTProvider` and `DiarizationProvider` against the local NPU stack —
new files `impl_stt_npu.py` and `impl_diarization_npu.py`.

`STTProvider.transcribe(audio_path, language) -> str` is the minimum contract; the
real production interface (per `impl_stt.py` docstring) returns a richer normalized
dict with `segments`, `words`, `duration`, `language`, `model`, `confidence`, `rtf`.
**Pre-extraction work needed**: tighten the protocol so both implementations promise
the same dict shape, not just a string. See Phase 1.

## 4. Dependency mechanism

**Recommendation: Git submodule**, at least for v1.

| Option | Pros | Cons | Recommendation |
|--------|------|------|----------------|
| **Git submodule** | Zero infra. No PyPI, no Forgejo package registry, no shipping wheels. Appliance team rev's the submodule pointer when they want to pull in cloud changes. Cloud devs do not have to think about the appliance repo at all when shipping. | Submodules are an acquired taste; `git clone --recurse-submodules` is a footgun for new contributors. Updates require a manual bump + commit in the appliance repo. | **Yes for v1.** |
| **Pip package** (`uc-meeting-ops-core` published to internal Forgejo package registry) | Cleaner semantics — appliance has a real dependency declaration. Easier to install in CI. | Requires running and maintaining a private package index. Adds release-management burden to the cloud team. Forgejo does support package registries but we don't run one today. Adds a build step to every cloud push. | Defer to v2. |
| **Vendored copy** | Maximum decoupling — appliance can diverge freely. | Manual sync from upstream is painful and goes stale. Defeats the purpose of "two repos that share a core." | No. |

**The submodule lives at `core/`** in the appliance repo. The appliance Dockerfile
adds `core/backend/` and `appliance/src/` both to PYTHONPATH; appliance modules import
cloud modules via `from services.providers.protocols import STTProvider`, and cloud
modules never import from `appliance/`. One-way dependency.

**Updating the submodule** is a deliberate appliance-team action:
```
cd UC-Meeting-Ops-Appliance/core
git fetch origin
git checkout v0.8.0  # or whatever cloud tag they want
cd ..
git add core
git commit -m "Pin UC-Meeting-Ops to v0.8.0"
```

**Versioning**: appliance pins to cloud **tags**, not branches. The cloud repo's
tagging discipline (`Bump frontend to v0.7.2` etc.) is exactly what we need.

## 5. Migration plan (phased)

### Phase 1 — Pre-split prep (cloud repo, ~2 hours)

This is the only phase that needs to land in the cloud repo before extraction. Goal:
make the cloud repo cleanly buildable and runnable **without any appliance file
imports succeeding** (the cloud repo should not depend on `npu_runtime` or
`whisperx_npu_service` to even start).

Concrete tasks:

1. **Convert appliance-file imports to lazy imports inside cloud entry points.**
   The currently broken cases:
   - `backend/api/websocket_transcription.py:11` → `from services.whisperx_npu_service import whisperx_service`
   - `backend/api/websocket_auto_summary.py:11` → same
   - `backend/api/simple_recording_db.py:27` → `from services.real_whisper_service import real_whisper_service`
   - `backend/services/transcription_service.py:24` → `from npu_runtime import NPURuntime`
   - `backend/services/live_transcription_service.py:15` → `from services.npu_whisper_transcriber import NPUWhisperTranscriber`
   - `backend/services/stt_model_manager.py:156,164,206` → `npu_optimization.whisperx_npu_engine`, `npu_runtime` (already gated by try/except but still surfaces appliance file refs)
   - `backend/services/always_on_recorder.py:281,500,525` → `services.live_recording_transcription`
   - `backend/services/unified_agent_service.py:351,524` → same
   - `backend/api/websocket_remote_audio.py:569,584` → `whisper_server_client`, `real_whisper_service`

   Pattern: move each `from services.<appliance_module> import ...` to inside the
   function that uses it, wrapped in `if not DISABLE_LOCAL_AUDIO:`. Cloud build then
   never touches the appliance files at import time.

2. **Strengthen `STTProvider` protocol to return the rich dict.** Right now:
   ```python
   class STTProvider(Protocol):
       async def transcribe(self, audio_path: str, *, language: str | None = None) -> str: ...
   ```
   Should be:
   ```python
   class STTTranscription(TypedDict, total=False):
       text: str
       segments: list[dict]
       words: list[dict]
       duration: float
       language: str
       model: str
       confidence: float
       rtf: float | None

   class STTProvider(Protocol):
       async def transcribe(self, audio_path: str, *, language: str | None = None) -> STTTranscription: ...
   ```
   Both cloud Parakeet impl and appliance WhisperX impl already produce dicts of this
   shape; we just type it.

3. **Audit `auto_summarization_service.py` and `progressive_interval_manager.py`** —
   they reference `live_recording_transcription` in docstrings and method calls.
   Confirm whether the **business logic** depends on appliance code or only on data
   it produces. If it's only the data, the cloud build needs a non-appliance path to
   feed the same Redis channel. If it's tightly coupled, this needs more design work.
   Spot check: `auto_summarization_service.py:57` explicitly notes "empty in cloud
   builds (where DISABLE_LOCAL_AUDIO=1 keeps that service inert)", suggesting cloud
   has its own path. Verify in Phase 1.

4. **Delete `backend/services/whisperx_py313.py`** if it's truly unreferenced
   (`grep -rn whisperx_py313 backend/` should return nothing after Phase 1.1 finishes).
   Then we don't have to move dead code.

5. **Delete `backend/npu_optimization/unified_stt_diarization.py`** for the same
   reason if it stays unreferenced after Phase 1.1.

6. **Cloud-repo CI**: add a test that imports `backend/main.py` with
   `DISABLE_LOCAL_AUDIO=1` set and asserts that none of the 12 appliance files were
   loaded. Use `sys.modules` inspection. This is the regression gate for Phase 3.

7. **Tag cloud repo `v0.7.3`** (or whatever the next minor is) so the appliance
   submodule has a stable pin target.

Phase 1 changes ONLY the cloud repo. No new repo created yet. After Phase 1, the cloud
build is identical functionally but cleanly buildable without the appliance code on
disk.

### Phase 2 — Create appliance repo (~4 hours)

1. Create `UC-Meeting-Ops-Appliance` private repo on Forgejo under
   `UnicornCommander` group. Same access list as cloud repo unless Aaron specifies
   otherwise (Open Question #1).

2. `git init`, add core/ as submodule:
   ```
   git submodule add https://git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops.git core
   cd core && git checkout v0.7.3 && cd ..
   ```

3. Copy the 12 appliance files plus the additional ones identified (aie2_kernel_driver,
   direct_npu_runtime, whisper_npu_transcriber, transcription_with_diarization) into
   the new layout under `src/`.

4. **Adjust imports** in the moved files:
   - Where they import from cloud modules (e.g., `from services.providers.protocols
     import STTProvider`), the appliance Dockerfile must put `core/backend/` on
     PYTHONPATH. Imports stay the same string.
   - Where cloud modules import from these (none should remain after Phase 1.1).

5. **Write `impl_stt_npu.py` and `impl_diarization_npu.py`** that implement the
   strengthened `STTProvider` and `DiarizationProvider` protocols against the NPU
   stack. These are new files; the underlying engines (whisperx_npu_engine_real, etc.)
   already exist.

6. **Appliance compose + Dockerfile**:
   - Build context = `core/backend` for the base image.
   - Appliance Dockerfile FROM the cloud backend image; layers in NPU runtime,
     copies appliance `src/` onto PYTHONPATH, mounts `/dev/accel/accel0`.
   - Compose passes through `/dev/accel/accel0`, `/dev/snd`, and the audio device
     groups already in `docker-compose.prod.yml`.
   - Env vars: `DISABLE_LOCAL_AUDIO=0`, `STT_PROVIDER=npu`, `DIARIZATION_PROVIDER=npu`.

7. Move `install-meeting-ops.sh`, `INSTALL.md` (NPU bits), and the NPU sections of
   `CLAUDE.md` to the appliance repo. Cloud `CLAUDE.md` keeps only the cloud-relevant
   parts.

8. Initial commit + `v0.1.0` tag in the appliance repo.

### Phase 3 — Cloud repo cleanup (~2 hours)

1. Delete the 12 appliance files (plus the 4 supporting files) from cloud `backend/`.

2. Update `backend/services/transcription_service.py` to remove the `from npu_runtime
   import NPURuntime` and replace the entire `_initialize_npu_runtime` path with
   `ProviderRegistry.get_stt()` — the docstring already says this is the cloud
   behavior, but the code still has NPU paths. Excise them.

3. Update `backend/services/stt_model_manager.py` to remove NPU model entries
   (`whisperx-npu-unified`, `whisperx-npu`, `whisper-onnx-npu`, `npu-runtime`).
   Leave only the HTTP-routed model entries.

4. Remove appliance-targeted sections of:
   - `CLAUDE.md` — keep only cloud architecture. Add a top note: "Appliance build
     (Ryzen AI NPU) lives at `UC-Meeting-Ops-Appliance`."
   - `INSTALL.md` — split: cloud sections stay; NPU sections to appliance repo.
   - `install-meeting-ops.sh` — move to appliance repo entirely; cloud install
     is `docker compose --env-file deploy/bigboy/.env.bigboy -f deploy/bigboy/docker-compose.bigboy.yml up -d`.

5. Update cloud Dockerfile to not COPY appliance directories. Should be automatic
   after the file deletes, but verify with a fresh build.

6. Re-run the CI gate added in Phase 1.6 — it should now pass trivially.

7. Tag cloud repo `v0.8.0` to mark the post-split version.

### Phase 4 — Verify both still work (ongoing)

Cloud:
- Production cloud build at `meetingops.magicunicorn.dev` rebuilt from `v0.8.0`,
  smoke-tested end-to-end (upload a meeting, get a transcript via Parakeet, run a
  summary via LiteLLM). Per `feedback_media_cutover_smoke_test`: verify real
  downstream side effects, not HTTP 200s.

Appliance:
- Aaron does not currently have a Ryzen AI box on hand. Verification limited to:
  - `pip install` + module-import smoke test on a non-NPU box.
  - Docker image builds successfully.
  - `CPU_ONLY_MODE=1` smoke test runs the WhisperX path in CPU fallback and produces a
    transcript.
- Full NPU verification deferred until appliance hardware is available. This is
  explicitly NOT a Phase 4 blocker for declaring the split done.

## 6. Repo permissions + access

Cloud repo `UC-Meeting-Ops` access today (per `git remote -v`): aaron token. Should
be expanded to whoever needs it on the cloud product. Appliance repo defaults to
**same access list**, but Aaron should explicitly decide (Open Question #5).

Particular considerations:
- If appliance becomes SDVOSB-resold product (per Open Question #3), it may need
  separate IP boundaries from cloud — i.e., contractors with cloud access should
  not auto-inherit appliance access.
- NPU kernel binaries that ship in the appliance repo may have AMD licensing
  constraints. Treat them as "internal-only, do not redistribute" until legal-checked.

## 7. CI/CD considerations

Cloud repo: Forgejo Actions (per `.github/workflows`, though contents not inspected
in this design pass). Has lint/test passes for the cloud build.

Appliance repo for v1:
- Lint + import check (no NPU hardware required).
- Build the Docker image to verify Dockerfile syntax.
- **No NPU integration tests in CI.** Those run manually on appliance hardware.
- Tag-based releases: when an appliance tag is cut, a release artifact (the install
  script + a docker-compose bundle) is published to Forgejo releases.

Cross-repo CI: not in scope for v1. The appliance team manually rev's the submodule
pointer.

## 8. Open questions for Aaron

1. **Repo name confirmation**. `UC-Meeting-Ops-Appliance` vs alternative? Default
   recommendation above.

2. **Companion app extraction at the same time?** `docs/companion-app-design.md`
   targets Mac/PC menu-bar capture clients that talk to *either* cloud or appliance
   server. The companion app is its own product surface — does it deserve its own
   repo (`UC-Meeting-Ops-Companion`)? Or stays in cloud as a sibling subdirectory?
   Default recommendation: leave for now, defer to a separate design pass.

3. **SDVOSB resale path or internal-only?** If the appliance is going to be sold to
   federal customers via VBOC/SDVOSB pipeline (per VBOC + CJ Williams memory), the
   appliance repo needs:
   - Clean third-party-license audit (MLIR-AIE2 kernels in particular)
   - Build reproducibility (locked dependency versions, hash-pinned models)
   - FIPS-mode crypto if government deployments are in scope
   - A user-facing license file that's not just internal Magic Unicorn
   These are big items. Default for v1: build for internal-only; add resale-grade
   hardening as a follow-up project.

4. **Backward compatibility with current appliance installs?** Are there shipped
   appliance instances in the field whose `install-meeting-ops.sh` has to keep
   working after the split? If yes, the install script in the appliance repo must
   continue to function against the same model paths (`~/.meeting-ops/models/`) and
   same database schemas. Default assumption: no shipped appliances yet (per "appliance
   product isn't shipping to customers yet" in the brief), so we can do a clean break.

5. **Permissions on the appliance repo.** Default: mirror cloud repo's access. If
   Aaron wants tighter restriction (e.g., not exposing NPU kernel work to non-Magic-
   Unicorn-employees), specify here before Phase 2.

6. **`whisper-server` deployment story for cloud.** `whisper_server_client.py` (a
   "moves" file) talks to whisper.cpp Vulkan iGPU server at `:8178`. The cloud build
   today references this in `api/ai_settings.py` and `api/websocket_remote_audio.py`.
   Is this whisper-server running anywhere in cloud production, or is it appliance-only
   today? If cloud uses it, `whisper_server_client.py` may need to **stay** in cloud
   repo and the appliance repo gets only the NPU-specific pieces. (Bigboy's `deploy/bigboy/whisper/`
   directory exists — suggests there's a cloud-side whisper-server too.) Worth a 30-min
   audit before committing to "move".

## 9. Effort estimate

| Phase | Estimate | Notes |
|-------|----------|-------|
| 1 — Pre-split prep (cloud repo) | ~2 hours | 8 lazy-import conversions + protocol tightening + one CI gate. Self-contained, can land in one PR. |
| 2 — Create appliance repo | ~4 hours | Mechanical file moves + Dockerfile + compose + initial commit. Bulk of the work is wiring up `impl_stt_npu.py` to the strengthened protocol. |
| 3 — Cloud repo cleanup | ~2 hours | File deletes + CLAUDE.md/INSTALL.md updates + tag bump. |
| 4 — Verify both work | Ongoing | Cloud verification immediate; appliance NPU verification deferred to hardware-available milestone. |
| **Total** | **~8 hours** | Across 1-2 dev sessions. NOT urgent. |

**Priority ordering**: this is below cloud-build cleanup. Recommended scheduling:
finish the current cloud session attachments + move-between-orgs + clustering work
first, then queue Phase 1 in a quiet week, then queue Phases 2+3 as a paired session.

## 10. What I found during the audit that changes the picture

1. **Module-level import damage is bigger than the brief implied.** The brief flagged
   `always_on_recorder`, `unified_agent_service`, `simple_recording_db`,
   `websocket_auto_summary`, `websocket_remote_audio`, `ai_settings`. The actual list
   has 14+ files importing appliance modules at module level, including
   `websocket_transcription.py` and `websocket_satellite.py`. The cloud build is
   currently surviving on the fact that the singletons' constructors gracefully
   no-op when `/dev/accel/accel0` doesn't exist — but they DO instantiate, which means
   the cloud build is shipping 3,373 LOC of unreachable NPU code in every container.
   Phase 1.1 (lazy-import conversion) is therefore higher-value than I expected; even
   if extraction never happens, doing Phase 1 alone is a worthwhile cleanup that
   shrinks the cloud image.

2. **`transcription_service.py` straddles the line.** It has explicit
   `_local_audio_disabled()` gating in its constructor AND a hard module-level import
   `from npu_runtime import NPURuntime`. The cloud build technically works because
   `npu_runtime.py` doesn't fail-fast on missing `/dev/accel/accel0` — but the import
   still happens, which means `npu_runtime` is currently a cloud-repo file even
   though it's marketed as appliance-only. Phase 1 must split `transcription_service.py`
   into a cloud-only stub and an appliance-only NPU coordinator, OR push the NPU import
   inside the conditional branch.

3. **`whisperx_py313.py` and `unified_stt_diarization.py` appear unreferenced.**
   `grep -rn whisperx_py313 backend/` returned nothing useful, same for
   `unified_stt_diarization`. These may be safe to delete in Phase 1 rather than move
   in Phase 2. Worth a 5-minute confirmation before scheduling.

4. **`whisper_server_client.py` is mixed-use.** It's referenced by `api/ai_settings.py`
   (cloud) AND by appliance-side pipelines. It probably belongs in the **cloud repo**
   as a generic HTTP client for whisper.cpp servers, and the appliance just *uses* it
   over the submodule. This contradicts the brief's "moves" list. Confirm in Open
   Question #6.

5. **`deploy/bigboy/whisper/` exists.** Suggests cloud production already runs a
   whisper.cpp server too — corroborates point 4 above. The cloud Parakeet vs
   appliance WhisperX-NPU split may have a third axis: cloud also has a whisper.cpp
   Vulkan path on bigboy. Worth an audit before Phase 2.

6. **Provider protocol is too thin today.** `STTProvider.transcribe -> str` doesn't
   reflect what the implementations actually return (a rich dict). Both products
   already produce the rich dict; tightening the type is paperwork, not code. Phase
   1 should land it.

7. **`install-meeting-ops.sh` says "v4.0.0"** even though the rest of the repo is
   v0.7.2 — confirms the brief's intuition that the appliance has its own internal
   versioning that's drifted from the cloud build. A clean v0.1.0 reset in the
   appliance repo makes this explicit.

8. **Companion app is well-designed but undeployed.** `docs/companion-app-design.md`
   is detailed; the Swift implementation does not appear to exist yet. So extracting
   the companion app design is a "move the doc" operation, not a "move the code"
   operation. Low risk to defer.

9. **`backend/tests/test_identify_with_embeddings.py`** is the only test that
   touches appliance code paths in cloud CI today. May or may not need to move —
   depends on whether it tests the embedding *protocol* (stays in cloud) or the
   embedding *NPU implementation* (moves to appliance).

10. **`mcp/meeting_ops_mcp.py`** (the MCP server) is exposed today over the cloud
    API. If appliance ships with its own MCP server too, that's another file to copy
    rather than move. Out of scope for this design pass — flag for follow-up.

---

**Bottom line**: extraction is viable, mostly mechanical, ~8 hours of focused work.
The single highest-value pre-extraction task is Phase 1 (lazy-import conversion +
protocol tightening), because it cleans up the cloud build whether or not we ever
actually create the new repo. Aaron can ship Phase 1 standalone if scheduling for
Phases 2-3 doesn't land before mid-June.
