from __future__ import annotations

import pytest
from fastapi import HTTPException

from api import sessions_participants as participants


@pytest.mark.asyncio
async def test_manual_link_reconciles_stale_id_and_ignores_forged_provenance(monkeypatch):
    monkeypatch.setattr(
        participants.contact_ops_resolver,
        "is_configured",
        lambda: True,
    )

    async def resolve(_person_id, _workspace_id):
        return {
            "requested_person_id": "stale",
            "canonical_person_id": "canonical",
            "alias_ids": ["stale"],
            "status": "redirected",
        }

    monkeypatch.setattr(
        participants.contact_ops_resolver,
        "resolve_person_id",
        resolve,
    )
    assert await participants._verified_contact_link(
        contact_id="stale",
        confidence=1.0,
        basis="forged_exact_email",
        workspace_id="workspace-1",
    ) == {
        "contact_id": "canonical",
        "contact_match_confidence": None,
        "contact_match_basis": "manual_selection",
        "contact_link_source": "manual",
    }


@pytest.mark.asyncio
async def test_manual_link_fails_closed_when_contact_ops_is_dormant(monkeypatch):
    monkeypatch.setattr(
        participants.contact_ops_resolver,
        "is_configured",
        lambda: False,
    )
    with pytest.raises(HTTPException) as exc:
        await participants._verified_contact_link(
            contact_id="person-1",
            confidence=1.0,
            basis="exact_email",
            workspace_id="workspace-1",
        )
    assert exc.value.status_code == 503
