# Launch blocker (6): server-pass metering — verification finding

**Date:** 2026-06-24
**Task:** UC-dev launch handshake item (6) — "confirm the server pass uses the
per-org gateway key (not a shared/hardcoded key); only the end-of-meeting server
pass should meter; on-device live transcript + summary must NOT meter."

## Verdict: the server-pass **summary** is currently NOT metered per-org.

This is a **deploy-config + federation-key** issue, not an app-code bug. The app
code is org-aware; the live env overrides route around the metered gateway.

## Evidence

### 1. App code IS org-aware (good)
The end-of-meeting summary resolves its provider per-org:
- `backend/services/summary_slices.py:205` → `registry.get_llm(org_id, ...)`
- `backend/api/uploads.py:2373` → `registry.get_llm(session.organization_id, ...)`
- `backend/services/unified_agent_service.py:176` → `registry.get_llm(org_id=...)`

### 2. …but the live env forces a DIRECT route that bypasses the gateway
`backend/api/uploads.py:2361-2378` precedence:
1. per-upload `llm_model` → registry (per-org)
2. **env direct route** (`_direct_summarizer_provider`, uploads.py:1882) → wins by default
3. else registry per-org

`_direct_summarizer_provider()` builds a `LiteLLMProvider` pointing straight at
`MEETING_OPS_LLM_URL` / `MEETING_OPS_SUMMARIZER_URL` with a single
`MEETING_OPS_LLM_API_KEY` (unset → empty), bypassing the LiteLLM/metered gateway.

Live `meet-backend` env (checked 2026-06-24):
- **dogfood (bigboy):** `MEETING_OPS_LLM_URL=http://llm-gateway:8088/v1` (midboy2 RTX 6000), `MEETING_OPS_LLM_API_KEY` unset
- **prod (centerdeep):** `MEETING_OPS_LLM_URL=http://llm-gateway:8088/v1`, `MEETING_OPS_LLM_API_KEY` unset

So on **both** nodes the default summary path goes direct to a dedicated Qwen GPU
→ **not** through the metered gateway, **not** a per-org key. The summary is the
main LLM cost of the server pass.

### 3. Even the non-direct path has no distinct per-org gateway key
`ProviderRegistry.get_api_key(org_id, "llm")` (registry.py:50-94) returns the
org's **BYOK** key if set in `OrgProviderSettings`, else the shared
`DEFAULT_LITELLM_API_KEY` (`LITELLM_API_KEY`/`OPENAI_API_KEY`). There is no
separate per-org **gateway** key wired into the LLM call path in this repo, and
no org-identifying header on the call (`impl_llm._build_headers` sends only
`Authorization: Bearer <api_key>`).

### 4. on-device live transcript + summary — correct by architecture
Browser-first: the live path never hits the server, so it is never metered. ✓ No
change needed.

## What the UC dev needs to decide (their billing/federation + deploy domain)

To make the server-pass summary meter to the buyer's org:
1. **Route through the metered gateway.** Either remove `MEETING_OPS_LLM_URL` /
   `MEETING_OPS_SUMMARIZER_URL` (so the per-org `registry.get_llm()` path runs
   via `OPENAI_API_BASE` = the LiteLLM gateway), **or** point those vars at the
   metered gateway instead of the direct `:8088` GPU.
2. **Provision a per-org gateway key.** Ensure each org's gateway key (the
   federation billable key) reaches the LLM call — either by writing it into the
   org's `OrgProviderSettings` (so `get_api_key(org_id)` returns it) or by the
   gateway attributing spend per-org via a mechanism on the gateway side.
3. **Non-LLM server-pass compute** (diarization on `SPEAKER_SVC_URL`, STT on the
   Parakeet endpoint, embeddings on Infinity) are likewise direct Tailscale
   service calls today — confirm whether they should route via `/api/v1/metered`
   for the launch, or are intentionally on-house (no per-call cost).

If the standalone $15 plan's economics rely on a flat-rate dedicated GPU (not
per-token credits), then "unmetered direct route" may be **intentional** and the
only real gate is the entitlement check (Blocker 3, shipped). That's a billing
call for the dev — hence "confirm", not "fix".
