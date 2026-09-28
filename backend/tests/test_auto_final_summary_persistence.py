"""Regression coverage for durable auto-generated final summaries."""

from __future__ import annotations

import asyncio
import sys
import types
import uuid
from types import SimpleNamespace

def test_final_summary_is_persisted_on_canonical_session_row(canonical_db_schema, monkeypatch):
    """A generated final summary survives beyond the service's process memory."""
    # The service only needs this one runtime setting.  Stub the appliance
    # settings module so this focused database test does not depend on the
    # Bigboy-only absolute settings-file path.
    fake_system_service = types.ModuleType("services.system_service")
    fake_system_service.SystemService = type(
        "SystemService",
        (),
        {"load_settings": staticmethod(lambda: SimpleNamespace(enableFinalSummary=True))},
    )
    monkeypatch.setitem(sys.modules, "services.system_service", fake_system_service)
    import services.auto_summarization_service as summary_mod
    # Keep the stub scoped to this test; later tests must import the real
    # service module rather than inherit this focused-test dependency.
    monkeypatch.delitem(sys.modules, "services.auto_summarization_service")
    from database.database import SessionLocal
    from database.models import RecordingSession

    session_key = str(uuid.uuid4())
    transcript = (
        "Alice confirmed the launch plan and Bob agreed to send the customer "
        "update before Friday. The team also decided to review the rollout "
        "metrics during next week's planning meeting."
    )
    db = SessionLocal()
    try:
        session = RecordingSession(
            session_id=session_key,
            name="durable-final-summary",
            status="completed",
            organization_id=canonical_db_schema,
            transcript_simple=transcript,
        )
        db.add(session)
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(
        summary_mod,
        "_summarize_with_org",
        lambda *_args, **_kwargs: {
            "summary": "Launch plan confirmed.",
            "key_points": ["Send customer update before Friday."],
        },
    )

    service = summary_mod.AutoSummarizationService()
    service.session_org_id[session_key] = canonical_db_schema
    generated = asyncio.run(service.generate_final_summary(session_key))

    assert generated is not None
    assert generated["type"] == "final"
    assert generated["summary"] == "[FINAL SUMMARY] Launch plan confirmed."

    db = SessionLocal()
    try:
        persisted = (
            db.query(RecordingSession)
            .filter(RecordingSession.session_id == session_key)
            .one()
        )
        assert persisted.final_summary == generated
        assert persisted.summary_preview == generated["summary"]
    finally:
        db.close()
