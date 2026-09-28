"""Tests for cross-app reference hints on Meeting-Ops MCP tool results.

The MCP server emits lightweight handles to sibling Unicorn Commander
apps (Contact-Ops, Project-Ops, Crisis-Ops) on top of meeting payloads
so the user's AI client can hop between apps without Meeting-Ops calling
them server-side. These tests verify:

* The pure-function populator
  (:func:`services.mcp_cross_app.build_cross_app_references`) emits the
  documented schema and derives hints from participants + structured
  pointers + lightweight text scans.
* The MCP tools that wrap it (``get_meeting_details``,
  ``get_meeting_transcript``, ``get_cross_app_hints``) surface the
  payload through the FastMCP layer without breaking on missing data.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Pure-function populator
# ---------------------------------------------------------------------------


def test_empty_session_returns_canonical_empty_shape():
    """An empty session still returns the documented three-key shape."""
    from services.mcp_cross_app import build_cross_app_references

    refs = build_cross_app_references({})
    assert set(refs.keys()) == {
        "mentioned_contacts",
        "mentioned_projects",
        "mentioned_cases",
    }
    assert refs["mentioned_contacts"] == []
    assert refs["mentioned_projects"] == []
    assert refs["mentioned_cases"] == []


def test_non_dict_session_returns_empty_shape():
    """A defensive call with the wrong type must not raise."""
    from services.mcp_cross_app import build_cross_app_references

    refs = build_cross_app_references(None)  # type: ignore[arg-type]
    assert refs == {
        "mentioned_contacts": [],
        "mentioned_projects": [],
        "mentioned_cases": [],
    }


def test_participants_become_contact_handles():
    """Each participant row becomes a contact handle with the structured
    confidence and a Contact-Ops query keyed off email."""
    from services.mcp_cross_app import (
        CONF_STRUCTURED,
        CONTACT_OPS_URL,
        build_cross_app_references,
    )

    session = {
        "participants": [
            {"name": "Aaron Stransky", "email": "aaron@magicunicorn.tech"},
            {"name": "Shafen Khan", "email": "shafen@example.com"},
            # Empty/garbage rows must be skipped.
            {"name": "", "email": ""},
            "not a dict",
        ],
    }

    refs = build_cross_app_references(session)
    contacts = refs["mentioned_contacts"]
    assert len(contacts) == 2
    aaron = next(c for c in contacts if c["email"] == "aaron@magicunicorn.tech")
    assert aaron["name"] == "Aaron Stransky"
    assert aaron["confidence"] == CONF_STRUCTURED
    hint = aaron["contact_ops_hint"]
    assert hint["app"] == "contact-ops"
    assert hint["url"] == CONTACT_OPS_URL
    assert hint["query"] == "email:aaron@magicunicorn.tech"


def test_participant_emails_dedupe_against_diarized_speakers():
    """Speaker names that match an existing participant must not duplicate."""
    from services.mcp_cross_app import build_cross_app_references

    session = {
        "participants": [
            {"name": "Aaron Stransky", "email": "aaron@magicunicorn.tech"},
        ],
        "transcript_diarized": {
            "speakers": [
                {"name": "Aaron Stransky"},
                {"name": "Jason Allen"},
                "Speaker 1",  # generic label — must be skipped
                "SPK_00",
                {"label": "Hina Khan"},
            ],
        },
    }

    refs = build_cross_app_references(session)
    names = [c["name"] for c in refs["mentioned_contacts"]]
    # Aaron appears once (the email-keyed participant wins).
    assert names.count("Aaron Stransky") == 1
    assert "Jason Allen" in names
    assert "Hina Khan" in names
    # No generic placeholders.
    assert not any(n.lower().startswith("speaker") for n in names)
    assert not any(n.upper().startswith("SPK") for n in names)


def test_structured_project_pointer_is_high_confidence():
    """A session already linked to a Project-Ops project surfaces with
    structured confidence and a slug-keyed hint."""
    from services.mcp_cross_app import (
        CONF_STRUCTURED,
        PROJECT_OPS_URL,
        build_cross_app_references,
    )

    session = {
        "project_app": "project-ops",
        "project_slug": "meeting-ops-launch",
        "project_id": 55,
    }

    refs = build_cross_app_references(session)
    assert len(refs["mentioned_projects"]) >= 1
    pointer = refs["mentioned_projects"][0]
    assert pointer["confidence"] == CONF_STRUCTURED
    assert pointer["project_ops_hint"]["app"] == "project-ops"
    assert pointer["project_ops_hint"]["url"] == PROJECT_OPS_URL
    assert pointer["project_ops_hint"]["query"] == "slug:meeting-ops-launch"


def test_text_scan_lifts_project_mentions_from_summary():
    """Heuristic 'X project' / 'Project X' phrases bubble up with low confidence."""
    from services.mcp_cross_app import CONF_WEAK_TEXT, build_cross_app_references

    session = {
        "title": "Atlas project sync",
        "final_summary": {
            "executive": "We aligned on the Atlas project milestones for next quarter.",
        },
    }
    refs = build_cross_app_references(session)
    names = {p["name"] for p in refs["mentioned_projects"]}
    assert any("Atlas" in n for n in names)
    assert refs["mentioned_projects"][0]["confidence"] == CONF_WEAK_TEXT


def test_text_scan_lifts_case_mentions():
    """'case X' style phrases land in mentioned_cases with a Crisis-Ops hint."""
    from services.mcp_cross_app import CRISIS_OPS_URL, build_cross_app_references

    session = {
        "title": "Case Sudano debrief",
        "final_summary": {"summary": "Discussed the Sudano case and next motions."},
    }
    refs = build_cross_app_references(session)
    assert refs["mentioned_cases"], refs
    first = refs["mentioned_cases"][0]
    assert first["crisis_ops_hint"]["app"] == "crisis-ops"
    assert first["crisis_ops_hint"]["url"] == CRISIS_OPS_URL
    assert first["crisis_ops_hint"]["query"].startswith("name:")


def test_render_markdown_section_is_empty_when_no_refs():
    """Empty refs produce an empty string so callers can concatenate freely."""
    from services.mcp_cross_app import (
        empty_cross_app_references,
        render_cross_app_section,
    )

    assert render_cross_app_section(empty_cross_app_references()) == ""


def test_render_markdown_section_embeds_json_block():
    """The rendered section carries the parseable JSON payload AI clients
    rely on, alongside human-readable bullets."""
    from services.mcp_cross_app import (
        build_cross_app_references,
        render_cross_app_section,
    )

    session = {
        "participants": [
            {"name": "Aaron Stransky", "email": "aaron@magicunicorn.tech"},
        ],
    }
    refs = build_cross_app_references(session)
    section = render_cross_app_section(refs)

    assert "## Cross-App References" in section
    assert "aaron@magicunicorn.tech" in section
    # JSON block is the structured channel for AI clients.
    assert "```json" in section
    start = section.index("```json") + len("```json")
    end = section.index("```", start)
    parsed = json.loads(section[start:end])
    assert "cross_app_references" in parsed
    assert parsed["cross_app_references"]["mentioned_contacts"][0]["email"] == (
        "aaron@magicunicorn.tech"
    )


# ---------------------------------------------------------------------------
# MCP tool integration — stub the backend HTTP client
# ---------------------------------------------------------------------------


def _fake_session_payload() -> dict:
    return {
        "id": "sess-xyz",
        "session_id": "sess-xyz",
        "title": "Atlas project sync",
        "name": "Atlas project sync",
        "status": "completed",
        "created_at": "2026-05-30T15:00:00",
        "duration": 1800,
        "participants": [
            {"name": "Aaron Stransky", "email": "aaron@magicunicorn.tech"},
            {"name": "Shafen Khan", "email": None},
        ],
        "transcript_diarized": {
            "speakers": [
                {"name": "Aaron Stransky"},
                "Speaker 2",
            ],
            "segments": [
                {"speaker": "Aaron Stransky", "text": "Welcome to the Atlas project sync."},
                {"speaker": "Speaker 2", "text": "Let's review the Sudano case status."},
            ],
        },
        "transcript_simple": "Welcome to the Atlas project sync. Let's review the Sudano case status.",
        "project_app": "project-ops",
        "project_slug": "atlas-rollout",
        "project_id": 7,
        "final_summary": {
            "executive": "Atlas project on track; Sudano case needs follow-up.",
        },
    }


class _StubResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _StubClient:
    """Pretends to be httpx.AsyncClient and returns a canned session payload
    for any GET against the recording-sessions endpoint."""

    def __init__(self, payload):
        self._payload = payload

    def __call__(self, *_a, **_kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, url, headers=None, params=None):
        return _StubResponse(self._payload)

    async def post(self, url, headers=None, json=None):
        return _StubResponse(self._payload)


@pytest.fixture()
def bound_pat():
    """Pin a fake PAT to the contextvar so the tool's _require_pat() passes."""
    from services.mcp_app import set_pat, reset_pat

    tok = set_pat("mops_pat_unit_test_cross_app")
    try:
        yield
    finally:
        reset_pat(tok)


def test_get_meeting_details_appends_cross_app_section(bound_pat):
    """get_meeting_details surfaces the cross-app section in the response body
    AND embeds the structured JSON payload for the AI client."""
    from services.mcp_app import get_meeting_details

    fake = _fake_session_payload()
    stub = _StubClient(fake)
    with patch("services.mcp_app.httpx.AsyncClient", new=stub):
        result = asyncio.run(get_meeting_details("sess-xyz"))

    assert "Atlas project sync" in result
    assert "## Cross-App References" in result
    assert "aaron@magicunicorn.tech" in result
    # Structured project pointer present.
    assert "atlas-rollout" in result
    # JSON block parseable.
    start = result.index("```json") + len("```json")
    end = result.index("```", start)
    parsed = json.loads(result[start:end])
    refs = parsed["cross_app_references"]
    assert set(refs.keys()) == {
        "mentioned_contacts",
        "mentioned_projects",
        "mentioned_cases",
    }
    assert any(c.get("email") == "aaron@magicunicorn.tech" for c in refs["mentioned_contacts"])
    assert any(p.get("name") == "atlas-rollout" for p in refs["mentioned_projects"])


def test_get_meeting_transcript_appends_cross_app_section(bound_pat):
    """get_meeting_transcript also surfaces the cross-app block."""
    from services.mcp_app import get_meeting_transcript

    fake = _fake_session_payload()
    stub = _StubClient(fake)
    with patch("services.mcp_app.httpx.AsyncClient", new=stub):
        result = asyncio.run(get_meeting_transcript("sess-xyz"))

    assert "# Transcript:" in result
    assert "## Cross-App References" in result
    assert "aaron@magicunicorn.tech" in result


def test_get_cross_app_hints_returns_standalone_json(bound_pat):
    """get_cross_app_hints returns only the cross_app_references block as JSON."""
    from services.mcp_app import get_cross_app_hints

    fake = _fake_session_payload()
    stub = _StubClient(fake)
    with patch("services.mcp_app.httpx.AsyncClient", new=stub):
        result = asyncio.run(get_cross_app_hints("sess-xyz"))

    parsed = json.loads(result)
    assert parsed["session_id"] == "sess-xyz"
    refs = parsed["cross_app_references"]
    assert set(refs.keys()) == {
        "mentioned_contacts",
        "mentioned_projects",
        "mentioned_cases",
    }
    contact_emails = {c.get("email") for c in refs["mentioned_contacts"]}
    assert "aaron@magicunicorn.tech" in contact_emails
    # Structured + heuristic project entries both present.
    project_names = {p["name"] for p in refs["mentioned_projects"]}
    assert "atlas-rollout" in project_names


def test_get_cross_app_hints_returns_empty_shape_on_backend_error(bound_pat):
    """When the backend errors, the tool still returns the canonical empty
    schema so AI clients can rely on the shape unconditionally."""
    from services.mcp_app import get_cross_app_hints

    class _BoomClient(_StubClient):
        async def get(self, url, headers=None, params=None):
            raise RuntimeError("backend down")

    stub = _BoomClient({})
    with patch("services.mcp_app.httpx.AsyncClient", new=stub):
        result = asyncio.run(get_cross_app_hints("missing-id"))

    parsed = json.loads(result)
    assert parsed["session_id"] == "missing-id"
    refs = parsed["cross_app_references"]
    assert refs == {
        "mentioned_contacts": [],
        "mentioned_projects": [],
        "mentioned_cases": [],
    }


def test_get_cross_app_hints_listed_in_readonly_tools():
    """The new tool is part of the canonical read-only surface so the stdio
    and HTTP transports stay in lockstep."""
    from services.mcp_app import READONLY_TOOL_NAMES

    assert "get_cross_app_hints" in READONLY_TOOL_NAMES
