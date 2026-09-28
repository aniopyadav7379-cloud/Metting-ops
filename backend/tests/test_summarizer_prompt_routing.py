"""v3.35.0 summarizer prompt-routing contracts (audit findings #6/#7/#8).

Pins, via a captured fake LLM provider:
  - memo prompt for short / single-speaker recordings; full prompt otherwise,
  - the anti-fabrication "None identified" instruction in the full prompt,
  - truncation: 200K cap on the direct route, visible disclosure line, and
    the processing_metadata stamps (summary_truncated / summary_prompt_variant),
  - both routes use the structured prompt (the registry route is no longer a
    stripped executive/bullets-only ask).

Models are imported lazily (after the `client` fixture boots the app) so all
FK-referenced tables are registered — the same pattern as test_basic_tier.py.
"""

from __future__ import annotations

import asyncio
import uuid as _uuid

import pytest


class _CaptureLLM:
    """Stands in for LiteLLMProvider; records the prompt, returns valid JSON."""

    def __init__(self):
        self.calls = []

    async def chat(self, *, system_prompt=None, user_prompt="", **kwargs):
        # Matches the LiteLLMProvider.chat keyword interface used by
        # _summarize_session (uploads.py ~1961).
        self.calls.append({"prompt": user_prompt, "system": system_prompt})
        return (
            '```json\n{"executive": "ok", "bullets": [], "actions": [],'
            ' "decisions": [], "title": "t"}\n```'
        )


def _mk_session(transcript: str, speakers: list[str]):
    from database.database import SessionLocal
    from database.models import RecordingSession

    # 50 segments max; chunk size scales so huge transcripts stay fully
    # represented in the diarized segments (the prompt is built from them).
    chunk_len = max(200, len(transcript) // 50 + 1)
    chunks = [transcript[j:j + chunk_len] for j in range(0, len(transcript), chunk_len)][:50] or [transcript]
    segs = [
        {
            "speaker": speakers[i % len(speakers)],
            "text": chunk,
            "start": i * 10.0,
            "end": i * 10.0 + 9.0,
        }
        for i, chunk in enumerate(chunks)
    ]
    db = SessionLocal()
    s = RecordingSession(
        session_id=str(_uuid.uuid4()),
        name="prompt-routing-test",
        status="completed",
        transcript_simple=transcript,
        transcript_diarized={"segments": segs, "speakers": speakers},
        organization_id=1,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return db, s


def _summarize(monkeypatch, db, session, llm):
    import api.uploads as uploads_mod

    monkeypatch.setattr(uploads_mod, "_direct_summarizer_provider", lambda: llm)
    asyncio.run(
        uploads_mod._summarize_session(db, session, template="standard", force=True)
    )


def test_short_memo_gets_memo_prompt(client, monkeypatch):
    llm = _CaptureLLM()
    db, s = _mk_session("quick note about the roof estimate", ["Speaker 1"])
    try:
        _summarize(monkeypatch, db, s, llm)
        prompt = llm.calls[-1]["prompt"]
        assert "short recording" in prompt  # memo template marker
        assert "Key Quotes" not in prompt   # full 7-section format absent
        md = s.processing_metadata or {}
        assert md.get("summary_prompt_variant") == "memo"
        assert md.get("summary_truncated") is False
    finally:
        db.close()


def test_long_multispeaker_gets_full_prompt_with_no_fabrication_rule(client, monkeypatch):
    llm = _CaptureLLM()
    transcript = " ".join(f"word{i}" for i in range(500))
    db, s = _mk_session(transcript, ["Alice", "Bob"])
    try:
        _summarize(monkeypatch, db, s, llm)
        prompt = llm.calls[-1]["prompt"]
        assert "Key Quotes" in prompt
        assert "None identified." in prompt          # anti-fabrication rule
        assert "or implied" not in prompt            # removed per audit #6
        assert (s.processing_metadata or {}).get("summary_prompt_variant") == "full"
    finally:
        db.close()


def test_direct_route_truncates_at_200k_with_disclosure(client, monkeypatch):
    llm = _CaptureLLM()
    # The prompt body is built from the DIARIZED segments (attributed
    # transcript), so the segments themselves must exceed the 200K-char cap:
    # 50 segments x ~5.2K chars ≈ 260K. Multi-speaker + >300 words → full branch.
    transcript = ("hello there friend " * 280)[:5200] * 50
    db, s = _mk_session(transcript, ["Alice", "Bob"])
    try:
        _summarize(monkeypatch, db, s, llm)
        prompt = llm.calls[-1]["prompt"]
        assert "transcript truncated" in prompt       # disclosure visible to model
        md = s.processing_metadata or {}
        assert md.get("summary_truncated") is True
    finally:
        db.close()


def test_registry_route_uses_structured_prompt(client, monkeypatch):
    """Audit #8: with no direct provider, the registry route must still get
    the full structured template (not the old stripped JSON-only ask)."""
    import api.uploads as uploads_mod
    import services.providers.registry as reg_mod

    llm = _CaptureLLM()
    monkeypatch.setattr(uploads_mod, "_direct_summarizer_provider", lambda: None)

    class _Registry:
        def get_llm(self, org_id, **kw):
            return llm

    monkeypatch.setattr(reg_mod, "get_provider_registry", lambda _db: _Registry())

    transcript = " ".join(f"word{i}" for i in range(500))
    db, s = _mk_session(transcript, ["Alice", "Bob"])
    try:
        asyncio.run(
            uploads_mod._summarize_session(db, s, template="standard", force=True)
        )
        prompt = llm.calls[-1]["prompt"]
        assert "Key Quotes" in prompt                 # structured template present
        assert "Return strict JSON with keys executive" not in prompt  # old ask gone
    finally:
        db.close()


def test_empty_llm_response_does_not_persist_completed_summary(client, monkeypatch):
    import api.uploads as uploads_mod
    from services.providers.impl_llm import LLMUnavailable

    class _EmptyLLM:
        async def chat(self, **kwargs):
            return ""

    db, session = _mk_session("meeting transcript with enough content", ["Alice"])
    monkeypatch.setattr(uploads_mod, "_direct_summarizer_provider", lambda: _EmptyLLM())
    try:
        with pytest.raises(LLMUnavailable):
            asyncio.run(uploads_mod._summarize_session(db, session, force=True))
        assert session.final_summary is None
        assert session.summary is None
    finally:
        db.close()
