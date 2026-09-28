"""Idempotency guard for the summary input-hash short-circuit (v3.29.1, item #3).

`_summarize_session` runs the (expensive) server LLM at finalize AND on every
reprocess. When a reprocess re-transcribes to a byte-identical transcript +
speaker set, re-running the LLM is pure waste. v3.29.1 added a guard that
hashes the EXACT LLM input (system + body prompt — which fold in
transcript_simple, the speaker labels, the template, and the route) and skips
the call when it matches the input that produced the CURRENT stored summary —
while still re-running whenever the transcript, speaker labels, or template
actually change, and always re-running on force=True.

This drives the REAL `_summarize_session` with a spy LLM provider and asserts
the call count across: first run, identical re-run (skipped), changed
transcript (re-run), and force=True (re-run). Mirrors the monkeypatch + seed
style of test_reprocess_indexing.py. Uses asyncio.run (py3.14-safe).
"""
from __future__ import annotations

import asyncio
import json
import uuid as _uuid

from auth.utils import get_password_hash


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession
    return Organization, User, UserOrganization, SessionLocal, RecordingSession


def _seed_user_and_org(slug: str, username: str = "summ_idem_admin"):
    Organization, User, UserOrganization, SessionLocal, _ = _models()
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if not org:
            org = Organization(name=slug.replace("-", " ").title(), slug=slug, is_active=True)
            db.add(org)
            db.commit()
            db.refresh(org)
        user = db.query(User).filter(User.username == username).first()
        if not user:
            user = User(
                email=f"{username}@meeting-ops.local",
                username=username,
                hashed_password=get_password_hash("admin123"),
                is_active=True,
                is_verified=True,
                tier="enterprise",
                is_superuser=False,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        return org.id, user.id
    finally:
        db.close()


def _create_session(org_id: int, user_id: int, transcript: str) -> int:
    _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        sid = str(_uuid.uuid4())
        s = RecordingSession(
            session_id=sid,
            name=f"summ-{sid[:8]}",
            title=f"summ-{sid[:8]}",
            meeting_type="always_on",
            mode="always_on",
            status="processing",
            duration=10.0,
            user_id=user_id,
            organization_id=org_id,
            transcript_simple=transcript,
            transcript_diarized={
                "segments": [
                    {"text": transcript, "speaker": "SPEAKER_00", "start": 0, "end": 5}
                ]
            },
            processing_metadata={},
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        return s.id
    finally:
        db.close()


class _SpyLLM:
    """Fake summarizer provider: counts chat() calls, returns fixed JSON."""

    def __init__(self):
        self.calls = 0

    async def chat(self, system_prompt, user_prompt, *, max_tokens=500,
                   temperature=0.7, extra_params=None):
        self.calls += 1
        return json.dumps({
            "executive": "exec summary",
            "bullets": ["b1"],
            "actions": [],
            "decisions": [],
            "title": "Spied Title",
        })


def test_summary_skips_redundant_llm_call(client, monkeypatch):
    import api.uploads as uploads_mod
    import services.action_items_extractor as ai_mod
    from database.database import SessionLocal
    from database.models import RecordingSession

    spy = _SpyLLM()
    # Force the direct-summarizer route to our spy + no-op the best-effort
    # action-item promotion so the test is isolated to the guard.
    monkeypatch.setattr(uploads_mod, "_direct_summarizer_provider", lambda: spy)
    monkeypatch.setattr(ai_mod, "persist_action_items", lambda db, session: None)

    org_id, user_id = _seed_user_and_org("summ-idem-org")
    pk = _create_session(org_id, user_id, "Alice said hello to Bob about the budget.")

    def _run(force: bool = False):
        db = SessionLocal()
        try:
            s = db.query(RecordingSession).filter(RecordingSession.id == pk).first()
            asyncio.run(uploads_mod._summarize_session(db, s, force=force))
        finally:
            db.close()

    # 1) First summary -> LLM runs once + the input hash is stamped.
    _run()
    assert spy.calls == 1
    db = SessionLocal()
    try:
        s = db.query(RecordingSession).filter(RecordingSession.id == pk).first()
        assert s.summary
        assert (s.processing_metadata or {}).get("summary_input_hash")
    finally:
        db.close()

    # 2) Identical input -> guard skips the LLM (count unchanged).
    _run()
    assert spy.calls == 1

    # 3) Transcript changes -> input hash differs -> re-run. Since v3.33.0
    # the prompt is built from the DIARIZED segments (transcript_simple is
    # only the fallback), so the mutation must hit the segments — changing
    # transcript_simple alone is correctly treated as a no-op by the guard.
    db = SessionLocal()
    try:
        from sqlalchemy.orm.attributes import flag_modified

        s = db.query(RecordingSession).filter(RecordingSession.id == pk).first()
        s.transcript_simple = "A completely different conversation about hiring."
        diarized = dict(s.transcript_diarized or {})
        diarized["segments"] = [
            {
                "speaker": "Speaker 1",
                "text": "A completely different conversation about hiring.",
                "start": 0.0,
                "end": 5.0,
            }
        ]
        s.transcript_diarized = diarized
        flag_modified(s, "transcript_diarized")
        db.commit()
    finally:
        db.close()
    _run()
    assert spy.calls == 2

    # 4) force=True on now-identical input -> re-run anyway.
    _run(force=True)
    assert spy.calls == 3
