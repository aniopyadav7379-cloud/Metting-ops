"""Unit tests for the inbound Customer-Ops federation surface.

Covers the auth flow (Brigade token verify + workspace->org binding),
the read-scope gate, JSON-RPC tool dispatch/routing, and serialization.
The JSONB ``@>`` containment SQL is Postgres-only (the suite test DB is
SQLite) so the live query is smoke-tested against the dogfood Postgres
at deploy time, not here.
"""

from __future__ import annotations

import base64
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwt as jose_jwt
from starlette.requests import Request

from api import federation_meetings as fm
from services import brigade_jwt_verifier as bv
from services.brigade_jwt_verifier import BrigadeJWTVerificationResult


def _req(auth: str | None = None) -> Request:
    headers = []
    if auth is not None:
        headers.append((b"authorization", auth.encode()))
    return Request({"type": "http", "headers": headers})


class _FakeOrg:
    def __init__(self, oid: int):
        self.id = oid


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _federation_keypair() -> tuple[str, dict]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return private_pem, {
        "kty": "RSA",
        "kid": "test-brigade-key",
        "alg": "RS256",
        "use": "sig",
        "n": _b64url_uint(numbers.n),
        "e": _b64url_uint(numbers.e),
    }


class _StaticJWKS:
    def __init__(self, key: dict):
        self.key = key

    def resolve_key(self, kid: str):
        return self.key if kid == self.key["kid"] else None


def _signed_federation_token(
    private_pem: str,
    *,
    audience: str = "meeting-ops",
    workspace_id: str | None = "workspace-1",
    exp: int | None = None,
) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://brigade.test",
        "aud": audience,
        "sub": "customer-ops-service",
        "iat": now,
        "nbf": now - 1,
        "exp": exp if exp is not None else now + 300,
    }
    if workspace_id is not None:
        claims["workspace_id"] = workspace_id
    return jose_jwt.encode(
        claims,
        private_pem,
        algorithm="RS256",
        headers={"kid": "test-brigade-key"},
    )


# ── scope extraction ───────────────────────────────────────────────────


def test_extract_scopes_space_delimited():
    assert bv.extract_scopes({"scope": "a b meetings:read"}) == {
        "a",
        "b",
        "meetings:read",
    }


def test_extract_scopes_list_and_missing():
    assert bv.extract_scopes({"scopes": ["x", "y"]}) == {"x", "y"}
    assert bv.extract_scopes({}) == set()
    assert bv.extract_scopes(None) == set()


# ── verifier early rejects (no crypto / network needed) ────────────────


def test_verifier_rejects_empty_and_pat():
    assert bv.verify_brigade_jwt_with_reason("").reason == "personal-access-token"
    assert (
        bv.verify_brigade_jwt_with_reason("mops_pat_abc").reason
        == "personal-access-token"
    )


def test_verifier_rejects_malformed_header():
    res = bv.verify_brigade_jwt_with_reason("not-a-jwt")
    assert res.valid is False
    assert res.reason == "header-unparseable"


def test_verifier_rejects_disallowed_alg():
    # HS256 is not in ALLOWED_BRIGADE_ALGORITHMS -> rejected before any
    # key resolution / network. Exercises header parse + the alg gate.
    from jose import jwt as jose_jwt

    tok = jose_jwt.encode({"sub": "x"}, "secret", algorithm="HS256")
    res = bv.verify_brigade_jwt_with_reason(tok)
    assert res.valid is False
    assert res.reason == "alg-rejected"


@pytest.mark.parametrize(
    ("audience", "workspace_id", "exp", "expected_reason"),
    [
        ("wrong-app", "workspace-1", None, "aud-mismatch"),
        ("meeting-ops", "workspace-1", int(time.time()) - 60, "expired"),
        ("meeting-ops", None, None, "missing-workspace-id"),
    ],
)
def test_verifier_fails_closed_for_audience_expiry_and_workspace(
    monkeypatch,
    audience,
    workspace_id,
    exp,
    expected_reason,
):
    monkeypatch.setenv("BRIGADE_EXPECTED_AUDIENCE", "meeting-ops")
    monkeypatch.setenv("BRIGADE_TRUSTED_ISSUER", "https://brigade.test")
    private_pem, public_jwk = _federation_keypair()
    token = _signed_federation_token(
        private_pem,
        audience=audience,
        workspace_id=workspace_id,
        exp=exp,
    )
    result = bv._verify(token, jwks_cache=_StaticJWKS(public_jwk))
    assert result.valid is False
    assert result.reason == expected_reason


# ── require_brigade_token (auth flow + tenant binding) ─────────────────


def test_require_token_missing_header():
    with pytest.raises(HTTPException) as ei:
        fm.require_brigade_token(_req(None), db=None)
    assert ei.value.status_code == 401


def test_require_token_invalid(monkeypatch):
    monkeypatch.setattr(
        fm,
        "verify_brigade_jwt_with_reason",
        lambda _t: BrigadeJWTVerificationResult(False, None, "signature-invalid"),
    )
    with pytest.raises(HTTPException) as ei:
        fm.require_brigade_token(_req("Bearer xyz"), db=None)
    assert ei.value.status_code == 401


def test_require_token_workspace_not_provisioned(monkeypatch):
    monkeypatch.setattr(
        fm,
        "verify_brigade_jwt_with_reason",
        lambda _t: BrigadeJWTVerificationResult(
            True, {"sub": "u1", "workspace_id": "ws-none", "scope": "meetings:read"}, None
        ),
    )
    monkeypatch.setattr(fm, "org_for_workspace_id", lambda _db, _ws: None)
    with pytest.raises(HTTPException) as ei:
        fm.require_brigade_token(_req("Bearer xyz"), db=object())
    assert ei.value.status_code == 403


def test_require_token_binds_org(monkeypatch):
    monkeypatch.setattr(
        fm,
        "verify_brigade_jwt_with_reason",
        lambda _t: BrigadeJWTVerificationResult(
            True,
            {"sub": "u1", "workspace_id": "ws-1", "scope": "meetings:read other"},
            None,
        ),
    )
    monkeypatch.setattr(fm, "org_for_workspace_id", lambda _db, _ws: _FakeOrg(42))
    ctx = fm.require_brigade_token(_req("Bearer xyz"), db=object())
    assert ctx.org_id == 42
    assert ctx.workspace_id == "ws-1"
    assert ctx.sub == "u1"
    assert "meetings:read" in ctx.scopes


# ── read-scope gate ────────────────────────────────────────────────────


def test_require_read_scope():
    ok = fm.FederationContext(org_id=1, workspace_id="w", sub="s", scopes={fm.READ_SCOPE})
    fm._require_read_scope(ok)  # no raise
    bad = fm.FederationContext(org_id=1, workspace_id="w", sub="s", scopes={"other"})
    with pytest.raises(HTTPException) as ei:
        fm._require_read_scope(bad)
    assert ei.value.status_code == 403


# ── JSON-RPC tool dispatch routing ─────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_requires_contact_id():
    ctx = fm.FederationContext(org_id=1, workspace_id="w", sub="s", scopes={fm.READ_SCOPE})
    with pytest.raises(HTTPException) as ei:
        await fm._dispatch_tool(object(), ctx, "list_meetings_for_contact", {})
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_dispatch_routes_each_tool(monkeypatch):
    ctx = fm.FederationContext(org_id=7, workspace_id="w", sub="s", scopes={fm.READ_SCOPE})
    calls = {}

    async def resolved(_ctx, contact_id):
        return [f"canonical-{contact_id}", f"alias-{contact_id}"]

    monkeypatch.setattr(fm, "_resolved_contact_ids", resolved)
    monkeypatch.setattr(
        fm, "list_meetings_for_contact",
        lambda db, org, cid, **k: calls.setdefault("m", (org, cid, k)) or {"items": []},
    )
    monkeypatch.setattr(
        fm, "list_summaries_for_contact",
        lambda db, org, cid, **k: calls.setdefault("s", (org, cid, k)) or [],
    )
    monkeypatch.setattr(
        fm, "list_action_items_for_contact",
        lambda db, org, cid, **k: calls.setdefault("a", (org, cid, k)) or [],
    )
    await fm._dispatch_tool(object(), ctx, "list_meetings_for_contact", {"contact_id": "c1", "limit": 5})
    await fm._dispatch_tool(object(), ctx, "list_summaries_for_contact", {"contact_id": "c2"})
    await fm._dispatch_tool(object(), ctx, "list_action_items_for_contact", {"contact_id": "c3"})
    assert calls["m"][0] == 7 and calls["m"][1] == ["canonical-c1", "alias-c1"]
    assert calls["s"][1] == ["canonical-c2", "alias-c2"]
    assert calls["a"][1] == ["canonical-c3", "alias-c3"]
    with pytest.raises(KeyError):
        await fm._dispatch_tool(object(), ctx, "nope", {"contact_id": "x"})


@pytest.mark.asyncio
async def test_legacy_rest_and_mcp_use_all_resolved_ids_and_minimized_shape(monkeypatch):
    ctx = fm.FederationContext(org_id=7, workspace_id="w", sub="s", scopes={fm.READ_SCOPE})
    seen: list[list[str]] = []

    async def resolved(_ctx, _contact_id):
        return ["canonical", "merged-alias"]

    def meetings(_db, _org, contact_ids, **_kwargs):
        seen.append(contact_ids)
        return {
            "items": [
                {
                    "participants": [
                        {"contact_id": "canonical", "display_name": "Ada"}
                    ]
                }
            ],
            "count": 1,
            "next_cursor": None,
        }

    monkeypatch.setattr(fm, "_resolved_contact_ids", resolved)
    monkeypatch.setattr(fm, "list_meetings_for_contact", meetings)
    rest = await fm.rest_meetings("requested", ctx=ctx, db=object())
    mcp = await fm._dispatch_tool(
        object(), ctx, "list_meetings_for_contact", {"contact_id": "requested"}
    )
    assert seen == [["canonical", "merged-alias"], ["canonical", "merged-alias"]]
    assert rest["meetings"] == mcp["items"]
    serialized = __import__("json").dumps({"rest": rest, "mcp": mcp})
    assert "email" not in serialized
    assert "embedding" not in serialized
    assert "voice" not in serialized


def test_rpc_envelope_helpers():
    assert fm._rpc_ok(1, {"k": 1}) == {"jsonrpc": "2.0", "id": 1, "result": {"k": 1}}
    err = fm._rpc_err(2, -32601, "boom")
    assert err["error"]["code"] == -32601 and err["error"]["message"] == "boom"


# ── serialization helpers ──────────────────────────────────────────────


def test_participants_public_projection_is_minimized_for_legacy_and_v1():
    class _S:
        participants = [
            {"id": "1", "name": "Alice", "email": "a@x.com", "contact_id": "co-1"},
            {"id": "2", "name": "Bob"},  # no email/contact_id
            "junk",  # non-dict ignored
        ]

    out = fm._participants_public(_S())
    assert out == [
        {"contact_id": "co-1", "display_name": "Alice"},
        {"contact_id": None, "display_name": "Bob"},
    ]
    serialized = __import__("json").dumps(out)
    assert "email" not in serialized


def test_summary_projection_allowlists_text_and_parses_legacy_json():
    class _S:
        final_summary = {
            "executive": {"email": "leak@example.com", "embedding": [1, 2]},
            "bullets": ["Safe point", {"voice_fingerprint": "never"}],
            "decisions": [
                {"text": "Safe decision"},
                {"decision": {"email": "leak@example.com"}},
            ],
        }
        summary = '{"overview":"Legacy safe body","embedding":[1],"speaker":{"voice":"x"}}'

    assert fm._summary_projection(_S()) == {
        "body": "Legacy safe body",
        "key_points": ["Safe point"],
        "decisions": [{"text": "Safe decision"}],
    }

    _S.summary = '{"email":"leak@example.com","embedding":[1]}'
    _S.final_summary = {"executive": {"nested": "not text"}}
    assert fm._summary_projection(_S()) is None


def test_action_projection_drops_free_text_owner_and_non_uuid_task_payload():
    class _Action:
        id = 1
        session_id = 10
        text = "Send the proposal"
        status = "todo"
        owner = "owner@example.com"
        due_date = None
        created_at = None
        raw_payload = {"po_task_id": "leak@example.com"}

    class _Query:
        def filter(self, *_criteria):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return [_Action()]

    class _DB:
        def query(self, _model):
            return _Query()

    action = fm._actions_by_session(_DB(), 1, [10])[10][0]
    assert action["assignee_contact_id"] is None
    assert "assignee_label" not in action
    assert action["project_ops_task_id"] is None
    assert "owner@example.com" not in __import__("json").dumps(action)

    _Action.raw_payload = {"po_task_id": "7d9c21de-7e4a-4fb2-a3d2-c55d33f59631"}
    assert fm._actions_by_session(_DB(), 1, [10])[10][0]["project_ops_task_id"] == (
        "7d9c21de-7e4a-4fb2-a3d2-c55d33f59631"
    )


def test_clamp_limit():
    assert fm._clamp_limit(None) == fm.DEFAULT_LIMIT
    assert fm._clamp_limit(0) == fm.DEFAULT_LIMIT
    assert fm._clamp_limit(5) == 5
    assert fm._clamp_limit(9999) == fm.MAX_LIMIT


def test_require_token_rejects_legacy_org_id_without_workspace(monkeypatch):
    monkeypatch.setattr(
        fm,
        "verify_brigade_jwt_with_reason",
        lambda _t: BrigadeJWTVerificationResult(
            True,
            {"sub": "u1", "org_id": "legacy", "scope": "meetings:read"},
            None,
        ),
    )
    with pytest.raises(HTTPException) as exc:
        fm.require_brigade_token(_req("Bearer xyz"), db=object())
    assert exc.value.status_code == 403


def test_transcript_requires_separate_scope():
    base = fm.FederationContext(
        org_id=1,
        workspace_id="w",
        sub="s",
        scopes={fm.READ_SCOPE},
    )
    with pytest.raises(HTTPException) as exc:
        fm._require_transcript_scope(base)
    assert exc.value.status_code == 403
    fm._require_transcript_scope(
        fm.FederationContext(
            org_id=1,
            workspace_id="w",
            sub="s",
            scopes={fm.READ_SCOPE, fm.TRANSCRIPT_SCOPE},
        )
    )


class _SummarySession:
    id = 10
    title = "Discovery"
    name = "Discovery"
    started_at = None
    ended_at = None
    duration = 60
    status = "summarized"
    created_at = None
    updated_at = None
    transcript = "private raw words"
    transcript_simple = None
    summary = None
    final_summary = {
        "executive": "Approved recap",
        "bullets": ["Pricing"],
        "decisions": ["Proceed"],
    }
    participants = [
        {
            "contact_id": "person-1",
            "name": "Ada",
            "email": "ada@example.com",
            "voice_fingerprint_id": "must-not-leak",
            "embedding": [1, 2, 3],
        }
    ]
    federation_summary_approved_at = None
    federation_summary_approved_digest = None


def test_summary_approval_digest_invalidates_changed_summary():
    session = _SummarySession()
    digest = fm.summary_approval_digest(session)
    assert digest
    session.federation_summary_approved_digest = digest
    session.federation_summary_approved_at = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    )
    status, payload = fm.approved_summary_payload(session)
    assert status == "approved"
    assert payload == {
        "body": "Approved recap",
        "key_points": ["Pricing"],
        "approved_at": session.federation_summary_approved_at.isoformat(),
    }
    session.final_summary = {
        **session.final_summary,
        "executive": "Changed after approval",
    }
    assert fm.approved_summary_payload(session) == ("stale", None)


def test_timeline_participants_exclude_email_biometrics_and_embeddings():
    projected = fm._participants_timeline(_SummarySession())
    assert projected == [
        {"contact_id": "person-1", "display_name": "Ada"},
    ]
    serialized = __import__("json").dumps(projected)
    assert "email" not in serialized
    assert "voice" not in serialized
    assert "embedding" not in serialized


def test_stable_cursor_is_bound_to_contact_and_updated_since(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setenv("FEDERATION_CURSOR_SIGNING_SECRET", "test-cursor-signing-secret")
    since = datetime(2026, 7, 1, tzinfo=timezone.utc)
    cursor = fm._encode_cursor(
        {
            "v": 1,
            "snapshot_at": "2026-07-24T12:00:00+00:00",
            "last_updated_at": "2026-07-23T12:00:00+00:00",
            "last_id": 42,
            "contact_key": fm._cursor_contact_key(["canonical", "alias"]),
            "updated_since": since.isoformat(),
        }
    )
    decoded = fm._decode_cursor(
        cursor,
        contact_ids=["alias", "canonical"],
        updated_since=since,
    )
    assert decoded["last_id"] == 42
    with pytest.raises(HTTPException):
        fm._decode_cursor(
            cursor,
            contact_ids=["different-person"],
            updated_since=since,
        )
    tampered = f"{cursor[:-1]}{'A' if cursor[-1] != 'A' else 'B'}"
    with pytest.raises(HTTPException):
        fm._decode_cursor(
            tampered,
            contact_ids=["alias", "canonical"],
            updated_since=since,
        )


def test_transcript_contract_projects_text_and_never_vectors(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setenv("FEDERATION_CURSOR_SIGNING_SECRET", "test-cursor-signing-secret")
    session = _SummarySession()
    session.created_at = datetime(2026, 7, 24, tzinfo=timezone.utc)
    session.updated_at = session.created_at
    session.transcript = __import__("json").dumps(
        {
            "segments": [
                {
                    "speaker": "SPEAKER_00",
                    "text": "Only this textual sentence may cross.",
                    "embedding": [0.11, 0.22, 0.33],
                    "voice_fingerprint": {"centroid_embedding": [0.44]},
                }
            ],
            "speaker_turns": [{"embedding": [0.55]}],
        }
    )

    class Query:
        def filter(self, *_criteria):
            return self

        def order_by(self, *_args):
            return self

        def limit(self, _limit):
            return self

        def all(self):
            return [session]

    class DB:
        def query(self, _model):
            return Query()

    monkeypatch.setattr(fm, "_actions_by_session", lambda *_args: {})
    result = fm.list_meeting_summaries_v1(
        DB(),
        1,
        ["person-1"],
        include_transcript=True,
    )
    serialized = __import__("json").dumps(result)
    assert result["items"][0]["transcript"] == "Only this textual sentence may cross."
    assert "embedding" not in serialized
    assert "voice_fingerprint" not in serialized
    assert "0.11" not in serialized


@pytest.mark.asyncio
async def test_unavailable_contact_resolution_returns_no_legacy_data(monkeypatch):
    ctx = fm.FederationContext(org_id=7, workspace_id="w", sub="s", scopes={fm.READ_SCOPE})

    async def unavailable(_contact_id, _workspace_id):
        return None

    monkeypatch.setattr(fm.contact_ops_resolver, "resolve_person_id", unavailable)
    assert await fm._dispatch_tool(
        object(), ctx, "list_meetings_for_contact", {"contact_id": "unverified"}
    ) == {"items": [], "count": 0, "next_cursor": None}


def test_timeline_query_has_explicit_workspace_filter():
    class Query:
        def __init__(self):
            self.criteria = []

        def filter(self, *criteria):
            self.criteria.extend(criteria)
            return self

        def order_by(self, *_args):
            return self

        def limit(self, _limit):
            return self

        def all(self):
            return []

    class DB:
        def __init__(self):
            self.query_obj = Query()

        def query(self, _model):
            return self.query_obj

    db = DB()
    result = fm.list_meeting_summaries_v1(
        db,
        77,
        ["person-1"],
        limit=10,
    )
    compiled = " ".join(str(criterion) for criterion in db.query_obj.criteria)
    assert "recording_sessions.organization_id" in compiled
    assert "organization_id" in compiled
    assert result["contract_version"] == fm.CONTRACT_VERSION
    assert result["items"] == []
