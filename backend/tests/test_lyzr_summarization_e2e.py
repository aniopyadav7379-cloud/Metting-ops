"""End-to-end proof that selecting provider_name="lyzr" in an org's Provider
Settings makes the REAL summarization pipeline (api.uploads._summarize_session)
route through LyzrProvider -> the documented Lyzr Agent API endpoints, not a
stand-in. httpx is mocked at the transport boundary only (no Lyzr SDK
shortcuts, no bypassing LyzrProvider's own request-building code).
"""
from __future__ import annotations

import asyncio
import json as json_mod
import uuid as _uuid

import httpx
import pytest


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict):
        self.status_code = status_code
        self._json = json_data
        self.text = str(json_data)

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=httpx.Response(self.status_code))


def _mk_session_with_transcript(org_id: int):
    from database.database import SessionLocal
    from database.models import RecordingSession

    db = SessionLocal()
    segs = [
        {"speaker": "Aaron", "text": "We need to decide on the migration timeline.", "start": 0.0, "end": 5.0},
        {"speaker": "Shafen", "text": "Let's ship the migration next Friday.", "start": 5.0, "end": 10.0},
    ]
    s = RecordingSession(
        session_id=str(_uuid.uuid4()),
        name="lyzr-e2e-test",
        status="completed",
        transcript_simple="We need to decide on the migration timeline. Let's ship the migration next Friday.",
        transcript_diarized={"segments": segs, "speakers": ["Aaron", "Shafen"]},
        organization_id=org_id,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return db, s


def test_org_configured_for_lyzr_summarizes_via_real_lyzr_endpoints(client, monkeypatch):
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_PROVIDER_ID", "prov-e2e")
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_LLM_CREDENTIAL_ID", "cred-e2e")

    calls = {"agent_create": 0, "chat": 0, "urls_hit": []}

    async def fake_post(self, url, headers=None, json=None):
        calls["urls_hit"].append(url)
        if url == "https://agent-prod.studio.lyzr.ai/v3/agents/":
            calls["agent_create"] += 1
            assert headers["x-api-key"] == "lyzr-super-secret-key"
            return _FakeResponse(200, {"id": "agent-e2e-1"})
        if url == "https://agent-prod.studio.lyzr.ai/v3/inference/chat/":
            calls["chat"] += 1
            # Prove the transcript actually reached Lyzr's request body.
            assert "migration" in json["message"].lower()
            return _FakeResponse(
                200,
                {
                    "status": "success",
                    "agent_response": json_mod.dumps(
                        {
                            "executive": "The team decided to ship the migration next Friday.",
                            "bullets": ["Migration timeline agreed"],
                            "actions": [],
                            "decisions": ["Ship migration next Friday"],
                            "title": "Migration planning",
                        }
                    ),
                    "session_id": json["session_id"],
                },
            )
        raise AssertionError(f"unexpected URL hit: {url}")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    # 1. Set up the org's Provider Settings row directly (what the
    #    /api/providers/... endpoint would persist from the Settings UI).
    from database.database import SessionLocal
    from database.models import OrgProviderSettings
    from services.providers.crypto import encrypt_api_key

    db = SessionLocal()
    db.query(OrgProviderSettings).filter(OrgProviderSettings.organization_id == 1, OrgProviderSettings.service_kind == "llm").delete()
    db.commit()
    row = OrgProviderSettings(
        organization_id=1,
        service_kind="llm",
        provider_name="lyzr",
        endpoint_url="https://agent-prod.studio.lyzr.ai",
        api_key_encrypted=encrypt_api_key("lyzr-super-secret-key"),
        model_name="",
        overrides={},
    )
    db.add(row)
    db.commit()
    db.close()

    # 2. Create a real session with a real transcript.
    db, session = _mk_session_with_transcript(org_id=1)

    # 3. Run the REAL production summarization entrypoint — no shortcuts.
    import api.uploads as uploads_mod

    monkeypatch.setattr(uploads_mod, "_direct_summarizer_provider", lambda: None)
    try:
        asyncio.run(uploads_mod._summarize_session(db, session, template="standard", force=True))
    finally:
        db.close()

    # 4. Verify Lyzr's actual documented endpoints were hit, in the right
    #    order, exactly once each (agent provisioning is cached).
    assert calls["agent_create"] == 1
    assert calls["chat"] == 1
    assert calls["urls_hit"] == [
        "https://agent-prod.studio.lyzr.ai/v3/agents/",
        "https://agent-prod.studio.lyzr.ai/v3/inference/chat/",
    ]

    # 5. Verify the Lyzr-produced summary was actually persisted onto the session.
    db2 = SessionLocal()
    from database.models import RecordingSession as RS

    refreshed = db2.query(RS).filter(RS.id == session.id).first()
    summary_data = json_mod.loads(refreshed.summary)
    assert "Friday" in summary_data["executive"]
    assert summary_data["decisions"] == ["Ship migration next Friday"]
    db2.close()

    # 6. Verify the agent_id was cached on OrgProviderSettings for reuse
    #    (idempotent provisioning — a real operational requirement, not
    #    just a nicety).
    db3 = SessionLocal()
    settings_row = (
        db3.query(OrgProviderSettings)
        .filter(OrgProviderSettings.organization_id == 1, OrgProviderSettings.service_kind == "llm")
        .first()
    )
    assert settings_row.overrides.get("lyzr_agent_id") == "agent-e2e-1"
    db3.close()
