"""Inbound federation read surface — Customer-Ops cockpit signal.

Customer-Ops (the relationship/funnel cockpit) renders "this customer's
meetings, summaries, and action items" in its customer-360, federated
from Meeting-Ops at read time. Meeting-Ops stays the system-of-record;
Customer-Ops stores nothing.

The reads are CONTACT-CENTRIC — keyed on a Contact-Ops ``contact_id``
(the canonical Contact-Ops person id). A meeting "belongs to" a contact
when that contact_id appears in the meeting's participant list
(``recording_sessions.participants[].contact_id``; GIN-indexed for the
``@>`` containment lookup — see alembic 041).

Auth (machine-to-machine, NOT the per-user PAT MCP at /mcp):
  * Bearer is a Brigade-minted RS256/EdDSA token (aud=meeting-ops),
    verified by services.brigade_jwt_verifier (JWKS, iss, exp, ...).
  * Tenant is bound from the verified ``workspace_id`` claim ->
    Organization.workspace_id -> org_id. NEVER from a header.
  * The data reads additionally require the ``meetings:read`` scope.

Transports (same handlers underneath; the Customer-Ops client points at
whichever it speaks):
  * REST:  GET /api/federation/v1/contacts/{contact_id}/{meetings|summaries|action-items}
  * MCP :  POST /api/federation/mcp   (JSON-RPC: initialize / tools/list / tools/call)

This surface is read-only; no writes back into Meeting-Ops.
"""

from __future__ import annotations

import base64
import json
import hashlib
import hmac
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from auth.models import Organization
from database.database import get_db
from database.models import ActionItem, RecordingSession
from services.brigade_jwt_verifier import (
    extract_scopes,
    verify_brigade_jwt_with_reason,
)
from services import contact_ops_resolver

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/federation", tags=["federation"])

# The read scope a Brigade token must carry to read meeting signal. The
# Customer-Ops KC client is granted this; Brigade propagates it.
READ_SCOPE = os.getenv("FEDERATION_MEETINGS_READ_SCOPE", "meetings:read")
TRANSCRIPT_SCOPE = os.getenv(
    "FEDERATION_MEETINGS_TRANSCRIPT_SCOPE",
    "meetings:transcript.read",
)
CONTRACT_VERSION = "meeting-summary.v1"

# Default page size for the meeting list; the contract caps to keep the
# cockpit responsive.
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_CURSOR_LENGTH = 2048

# Meeting-Ops action-item status -> the {open,in_progress,done,cancelled}
# vocab the Customer-Ops signal derivation counts on (todo->open etc).
_AI_STATUS_MAP = {"todo": "open", "doing": "in_progress", "done": "done", "cancelled": "cancelled"}


# ── Tenant binding ─────────────────────────────────────────────────────


def org_for_workspace_id(db: Session, workspace_id: str) -> Optional[Organization]:
    """Resolve a uc-registry workspace UUID to a Meeting-Ops org.

    Meeting-Ops tenancy is an integer ``organization_id``; the suite
    identity is the ``workspace_id`` UUID. ``Organization.workspace_id``
    (alembic 041) is the join. Returns None when the workspace is not
    provisioned here — the caller fails closed (403)."""
    if not workspace_id:
        return None
    return (
        db.query(Organization)
        .filter(Organization.workspace_id == workspace_id)
        .first()
    )


@dataclass
class FederationContext:
    org_id: int
    workspace_id: str
    sub: str
    scopes: set = field(default_factory=set)
    claims: dict = field(default_factory=dict)


def require_brigade_token(
    request: Request, db: Session = Depends(get_db)
) -> FederationContext:
    """Verify the Brigade federation bearer and bind the tenant.

    401 on a missing/invalid token; 403 when the asserted workspace is
    not provisioned in Meeting-Ops. Does NOT enforce the read scope —
    that is asserted per data-read so initialize/tools-list stay open to
    any valid meeting-ops token."""
    auth = request.headers.get("authorization", "")
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=401, detail="missing or malformed bearer token")

    result = verify_brigade_jwt_with_reason(parts[1].strip())
    if not result.valid or not result.claims:
        raise HTTPException(
            status_code=401, detail=f"invalid federation token ({result.reason})"
        )

    claims = result.claims
    workspace_id = str(claims.get("workspace_id") or "")
    org = org_for_workspace_id(db, workspace_id)
    if org is None:
        logger.warning(
            "federation: workspace not provisioned workspace_id=%s sub=%s",
            workspace_id,
            claims.get("sub"),
        )
        raise HTTPException(
            status_code=403, detail="workspace not provisioned in meeting-ops"
        )

    return FederationContext(
        org_id=org.id,
        workspace_id=workspace_id,
        sub=str(claims.get("sub") or ""),
        scopes=extract_scopes(claims),
        claims=claims,
    )


def _require_read_scope(ctx: FederationContext) -> None:
    if READ_SCOPE not in ctx.scopes:
        raise HTTPException(
            status_code=403, detail=f"missing required scope: {READ_SCOPE}"
        )


def _require_transcript_scope(ctx: FederationContext) -> None:
    if TRANSCRIPT_SCOPE not in ctx.scopes:
        raise HTTPException(
            status_code=403,
            detail=f"missing required scope: {TRANSCRIPT_SCOPE}",
        )


# ── Serialization helpers ──────────────────────────────────────────────


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _clamp_limit(limit: Optional[int]) -> int:
    if not limit or limit < 1:
        return DEFAULT_LIMIT
    return min(int(limit), MAX_LIMIT)


def _participants_public(session: RecordingSession) -> list[dict]:
    """Project participants to the response-minimized federation shape.

    This helper is deliberately shared by the v1 timeline and the older
    REST/MCP tools.  A caller with ``meetings:read`` must not be able to
    sidestep the Customer-Ops contract by selecting an older transport: email,
    speaker biometric metadata, match provenance, and every unknown JSONB key
    stay in Meeting-Ops.
    """
    raw = session.participants if isinstance(session.participants, list) else []
    out: list[dict] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        out.append(
            {
                "contact_id": p.get("contact_id") or None,
                "display_name": p.get("name") or None,
            }
        )
    return out


def _participants_timeline(session: RecordingSession) -> list[dict]:
    """Data-minimized participant projection for Customer-Ops.

    Email is intentionally excluded: Customer-Ops already keys the customer to
    Contact-Ops, and a timeline does not need a second copy of participant PII.
    """

    return _participants_public(session)


def _structured_transcript_text(value: Any) -> Optional[str]:
    """Extract only textual fields from a legacy transcript document.

    Older ``transcript`` rows can contain diarization documents, including
    speaker turns and embedding vectors. Federation must not expose that
    structure, even to a caller with the explicit transcript scope. This
    deliberately projects the small textual subset we understand rather than
    recursively copying a document and hoping every sensitive key is known.
    """

    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        value = value.get("segments") or value.get("utterances")
    if not isinstance(value, list):
        return None
    parts = [
        part.get("text", "").strip()
        for part in value
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    joined = "\n".join(part for part in parts if part)
    return joined or None


def _sanitized_transcript_text(session: RecordingSession) -> Optional[str]:
    """Return a text-only transcript projection with no biometric metadata."""

    for raw in (session.transcript_simple, session.transcript):
        if raw is None:
            continue
        if isinstance(raw, (dict, list)):
            text = _structured_transcript_text(raw)
        elif isinstance(raw, str):
            candidate = raw.strip()
            if not candidate:
                continue
            try:
                parsed = json.loads(candidate)
            except (TypeError, ValueError):
                # A non-JSON TEXT column is the canonical plain-text form.
                text = candidate
            else:
                text = (
                    parsed.strip()
                    if isinstance(parsed, str) and parsed.strip()
                    else _structured_transcript_text(parsed)
                )
        else:
            text = None
        if text:
            return text
    return None


def _contact_filter(contact_id: str):
    """JSONB containment predicate: participants @> [{"contact_id": X}].

    Backed by the GIN index on recording_sessions.participants (alembic
    041) so the lookup is index-assisted, not a full scan."""
    return RecordingSession.participants.contains([{"contact_id": contact_id}])


def _contact_filter_any(contact_ids: list[str]):
    predicates = [_contact_filter(contact_id) for contact_id in contact_ids]
    if not predicates:
        return RecordingSession.id == -1
    return or_(*predicates)


def _nonempty_text(value: Any) -> Optional[str]:
    """Return only an explicit text scalar, never a stringified container."""

    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _legacy_summary_document(value: Any) -> tuple[dict, Optional[str]]:
    """Parse legacy summary TEXT without widening the approved projection.

    Older rows may contain either plain-text summaries or JSON documents. JSON
    is parsed first and then subject to the same allowlist as ``final_summary``;
    it is never forwarded (or stringified) as an opaque blob.
    """

    if not isinstance(value, str):
        return {}, None
    raw = value.strip()
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}, raw
    return (parsed, None) if isinstance(parsed, dict) else ({}, None)


def _summary_projection(session: RecordingSession) -> Optional[dict]:
    final = session.final_summary if isinstance(session.final_summary, dict) else {}
    legacy, legacy_text = _legacy_summary_document(getattr(session, "summary", None))

    body = next(
        (
            text
            for value in (
                final.get("executive"),
                final.get("summary"),
                final.get("overview"),
                legacy.get("executive"),
                legacy.get("summary"),
                legacy.get("overview"),
                legacy_text,
            )
            if (text := _nonempty_text(value)) is not None
        ),
        None,
    )
    key_points = final.get("key_points") or final.get("bullets") or legacy.get(
        "key_points"
    ) or legacy.get("bullets") or []
    if not isinstance(key_points, list):
        key_points = []
    key_points = [
        text for point in key_points if (text := _nonempty_text(point)) is not None
    ]
    raw_decisions = final.get("decisions") or final.get("key_decisions") or legacy.get(
        "decisions"
    ) or legacy.get("key_decisions") or []
    if not isinstance(raw_decisions, list):
        raw_decisions = []
    decisions: list[dict] = []
    for decision in raw_decisions:
        value = (
            decision.get("text") or decision.get("decision")
            if isinstance(decision, dict)
            else decision
        )
        if (text := _nonempty_text(value)) is not None:
            decisions.append({"text": text})
    if not body and not key_points and not decisions:
        return None
    return {
        "body": body,
        "key_points": key_points,
        "decisions": decisions,
    }


def summary_approval_digest(session: RecordingSession) -> Optional[str]:
    projection = _summary_projection(session)
    if projection is None:
        return None
    encoded = json.dumps(
        projection,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def approved_summary_payload(session: RecordingSession) -> tuple[str, Optional[dict]]:
    projection = _summary_projection(session)
    if projection is None:
        return "unavailable", None
    approved_digest = getattr(
        session,
        "federation_summary_approved_digest",
        None,
    )
    approved_at = getattr(session, "federation_summary_approved_at", None)
    current_digest = summary_approval_digest(session)
    if not approved_digest or not approved_at:
        return "unapproved", None
    if not current_digest or not hmac.compare_digest(approved_digest, current_digest):
        return "stale", None
    return (
        "approved",
        {
            "body": projection["body"],
            "key_points": projection["key_points"],
            "approved_at": _iso(approved_at),
        },
    )


def _action_item_counts(db: Session, org_id: int, session_ids: list[int]) -> dict:
    if not session_ids:
        return {}
    from sqlalchemy import func

    rows = (
        db.query(ActionItem.session_id, func.count(ActionItem.id))
        .filter(
            ActionItem.organization_id == org_id,
            ActionItem.session_id.in_(session_ids),
        )
        .group_by(ActionItem.session_id)
        .all()
    )
    return {sid: int(n) for sid, n in rows}


def _project_ops_task_id(value: Any) -> Optional[str]:
    """Return only a canonical Project-Ops UUID, never arbitrary raw payload."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if len(candidate) != 36:
        return None
    try:
        canonical = str(UUID(candidate))
    except (TypeError, ValueError, AttributeError):
        return None
    return canonical if candidate.lower() == canonical else None


def _actions_by_session(
    db: Session,
    org_id: int,
    session_ids: list[int],
) -> dict[int, list[dict]]:
    if not session_ids:
        return {}
    rows = (
        db.query(ActionItem)
        .filter(
            ActionItem.organization_id == org_id,
            ActionItem.session_id.in_(session_ids),
        )
        .order_by(
            ActionItem.session_id.asc(),
            ActionItem.sort_order.asc(),
            ActionItem.id.asc(),
        )
        .all()
    )
    out: dict[int, list[dict]] = {}
    for item in rows:
        payload = item.raw_payload if isinstance(item.raw_payload, dict) else {}
        task_id = _project_ops_task_id(payload.get("po_task_id"))
        out.setdefault(item.session_id, []).append(
            {
                "id": str(item.id),
                "text": item.text,
                "status": _AI_STATUS_MAP.get(item.status, item.status),
                "assignee_contact_id": None,
                "due_at": _iso(item.due_date),
                "project_ops_task_id": task_id,
                "created_at": _iso(item.created_at),
            }
        )
    return out


def _parse_updated_since(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="updated_since must be an ISO-8601 timestamp",
        ) from exc
    if parsed.tzinfo is None:
        raise HTTPException(
            status_code=400,
            detail="updated_since must include a timezone",
        )
    return parsed.astimezone(timezone.utc)


def _cursor_contact_key(contact_ids: list[str]) -> str:
    joined = "\x00".join(sorted(set(contact_ids))).encode()
    return hashlib.sha256(joined).hexdigest()[:24]


def _cursor_signing_key() -> bytes:
    """Use a deployment secret to make pagination cursors opaque and signed."""

    secret = (
        os.getenv("FEDERATION_CURSOR_SIGNING_SECRET")
        or os.getenv("SECRET_KEY")
        or ""
    ).strip()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="federation cursor signing is not configured",
        )
    return secret.encode()


def _encode_cursor(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(_cursor_signing_key(), raw, hashlib.sha256).digest()
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{encoded}.{encoded_signature}"


def _decode_cursor(
    cursor: str,
    *,
    contact_ids: list[str],
    updated_since: Optional[datetime],
) -> dict:
    try:
        if not cursor or len(cursor) > MAX_CURSOR_LENGTH:
            raise ValueError("cursor length")
        encoded, encoded_signature = cursor.split(".", 1)
        payload_padding = "=" * (-len(encoded) % 4)
        signature_padding = "=" * (-len(encoded_signature) % 4)
        raw = base64.urlsafe_b64decode((encoded + payload_padding).encode())
        supplied_signature = base64.urlsafe_b64decode(
            (encoded_signature + signature_padding).encode()
        )
        expected_signature = hmac.new(
            _cursor_signing_key(), raw, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("cursor signature")
        payload = json.loads(
            raw.decode()
        )
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ValueError("unsupported cursor")
        if payload.get("contact_key") != _cursor_contact_key(contact_ids):
            raise ValueError("cursor contact mismatch")
        expected_since = _iso(updated_since)
        if payload.get("updated_since") != expected_since:
            raise ValueError("cursor updated_since mismatch")
        snapshot_at = _parse_updated_since(payload.get("snapshot_at"))
        last_updated_at = _parse_updated_since(payload.get("last_updated_at"))
        last_id = int(payload.get("last_id"))
        if snapshot_at is None or last_updated_at is None:
            raise ValueError("cursor timestamp missing")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid cursor") from exc
    return {
        "snapshot_at": snapshot_at,
        "last_updated_at": last_updated_at,
        "last_id": last_id,
    }


# ── The three contact-keyed reads ──────────────────────────────────────


def list_meetings_for_contact(
    db: Session,
    org_id: int,
    contact_ids: list[str],
    *,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
) -> dict:
    """Meetings where any canonical or historical contact id participated.

    Keyset pagination on session id (descending); ``cursor`` is the last
    id of the prior page."""
    n = _clamp_limit(limit)
    q = db.query(RecordingSession).filter(
        RecordingSession.organization_id == org_id,
        _contact_filter_any(contact_ids),
    )
    if cursor:
        try:
            q = q.filter(RecordingSession.id < int(cursor))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="invalid cursor")
    rows = q.order_by(RecordingSession.id.desc()).limit(n + 1).all()

    has_more = len(rows) > n
    page = rows[:n]
    counts = _action_item_counts(db, org_id, [s.id for s in page])

    items = []
    for s in page:
        items.append(
            {
                "id": s.id,
                "title": s.title or s.name or None,
                "started_at": _iso(s.started_at),
                "ended_at": _iso(s.ended_at),
                "duration_seconds": int(round(s.duration or 0)),
                "status": s.status,
                "has_summary": bool(s.summary) or bool(s.final_summary),
                "action_item_count": counts.get(s.id, 0),
                "participants": _participants_public(s),
            }
        )
    return {
        "items": items,
        "count": len(items),
        "next_cursor": str(page[-1].id) if (has_more and page) else None,
    }


def list_summaries_for_contact(
    db: Session, org_id: int, contact_ids: list[str], *, limit: Optional[int] = None
) -> list[dict]:
    """Summaries of meetings where any canonical or historical id participated."""
    n = _clamp_limit(limit)
    rows = (
        db.query(RecordingSession)
        .filter(
            RecordingSession.organization_id == org_id,
            _contact_filter_any(contact_ids),
            or_(
                RecordingSession.summary.isnot(None),
                RecordingSession.final_summary.isnot(None),
            ),
        )
        .order_by(RecordingSession.id.desc())
        .limit(n)
        .all()
    )
    out: list[dict] = []
    for s in rows:
        approval_status, approved = approved_summary_payload(s)
        if approval_status != "approved" or approved is None:
            continue
        out.append(
            {
                "id": f"summary-{s.id}",
                "meeting_id": s.id,
                "headline": s.title or s.name or None,
                "body": approved["body"],
                "key_points": approved["key_points"],
                "created_at": approved["approved_at"],
            }
        )
    return out


def list_action_items_for_contact(
    db: Session, org_id: int, contact_ids: list[str], *, limit: Optional[int] = None
) -> list[dict]:
    """Action items from meetings where a canonical or historical id participated.

    ``project_ops_task_id`` is emitted only when the Project-Ops bridge stamped
    a canonical UUID on the item; otherwise it is null."""
    n = _clamp_limit(limit)
    session_ids = [
        r.id
        for r in db.query(RecordingSession.id)
        .filter(
            RecordingSession.organization_id == org_id,
            _contact_filter_any(contact_ids),
        )
        .all()
    ]
    if not session_ids:
        return []
    rows = (
        db.query(ActionItem)
        .filter(
            ActionItem.organization_id == org_id,
            ActionItem.session_id.in_(session_ids),
        )
        .order_by(ActionItem.created_at.desc(), ActionItem.id.desc())
        .limit(n)
        .all()
    )
    out: list[dict] = []
    for it in rows:
        payload = it.raw_payload if isinstance(it.raw_payload, dict) else {}
        task_id = _project_ops_task_id(payload.get("po_task_id"))
        out.append(
            {
                "id": it.id,
                "meeting_id": it.session_id,
                "text": it.text,
                "status": _AI_STATUS_MAP.get(it.status, it.status),
                # owner is free-text today; we do not resolve owner ->
                # contact_id yet, so this is null until that lands.
                "assignee_contact_id": None,
                "due_at": _iso(it.due_date),
                "project_ops_task_id": task_id,
                "created_at": _iso(it.created_at),
            }
        )
    return out


def list_meeting_summaries_v1(
    db: Session,
    org_id: int,
    contact_ids: list[str],
    *,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
    updated_since: Optional[str] = None,
    include_transcript: bool = False,
) -> dict:
    """Versioned Customer-Ops timeline contract.

    Uses snapshot-bound keyset pagination on ``(updated_at, id)``. The snapshot
    keeps a multi-page traversal stable while meetings continue to change; a
    subsequent updated-since traversal picks up changes after that snapshot.
    """

    n = _clamp_limit(limit)
    since = _parse_updated_since(updated_since)
    cursor_data = (
        _decode_cursor(
            cursor,
            contact_ids=contact_ids,
            updated_since=since,
        )
        if cursor
        else None
    )
    snapshot_at = (
        cursor_data["snapshot_at"]
        if cursor_data
        else datetime.now(timezone.utc)
    )
    changed_at = func.coalesce(
        RecordingSession.updated_at,
        RecordingSession.created_at,
    )
    q = db.query(RecordingSession).filter(
        RecordingSession.organization_id == org_id,
        _contact_filter_any(contact_ids),
        changed_at <= snapshot_at,
    )
    if since is not None:
        q = q.filter(changed_at >= since)
    if cursor_data:
        q = q.filter(
            or_(
                changed_at < cursor_data["last_updated_at"],
                (
                    (changed_at == cursor_data["last_updated_at"])
                    & (RecordingSession.id < cursor_data["last_id"])
                ),
            )
        )
    rows = (
        q.order_by(changed_at.desc(), RecordingSession.id.desc())
        .limit(n + 1)
        .all()
    )
    has_more = len(rows) > n
    page = rows[:n]
    actions = _actions_by_session(db, org_id, [session.id for session in page])
    items: list[dict] = []
    for session in page:
        approval_status, approved_summary = approved_summary_payload(session)
        projection = _summary_projection(session)
        changed = session.updated_at or session.created_at
        item = {
            "meeting_id": str(session.id),
            "updated_at": _iso(changed),
            "metadata": {
                "title": session.title or session.name or None,
                "started_at": _iso(session.started_at),
                "ended_at": _iso(session.ended_at),
                "duration_seconds": int(round(session.duration or 0)),
                "status": session.status,
            },
            "participants": _participants_timeline(session),
            "summary_approval_status": approval_status,
            "approved_summary": approved_summary,
            "decisions": (
                projection.get("decisions", [])
                if approval_status == "approved" and projection
                else []
            ),
            "action_items": actions.get(session.id, []),
        }
        if include_transcript:
            item["transcript"] = _sanitized_transcript_text(session)
        items.append(item)

    next_cursor = None
    if has_more and page:
        last = page[-1]
        last_changed = last.updated_at or last.created_at
        next_cursor = _encode_cursor(
            {
                "v": 1,
                "snapshot_at": _iso(snapshot_at),
                "last_updated_at": _iso(last_changed),
                "last_id": last.id,
                "contact_key": _cursor_contact_key(contact_ids),
                "updated_since": _iso(since),
            }
        )
    return {
        "contract_version": CONTRACT_VERSION,
        "items": items,
        "count": len(items),
        "next_cursor": next_cursor,
        "snapshot_at": _iso(snapshot_at),
    }


async def _resolved_contact_ids(
    ctx: FederationContext,
    contact_id: str,
) -> list[str]:
    """Return canonical + historical aliases within the token workspace."""

    requested = contact_id.strip()
    if not requested:
        return []
    resolution = await contact_ops_resolver.resolve_person_id(
        requested,
        ctx.workspace_id,
    )
    if resolution is None:
        # The contact id is only meaningful after the Contact-Ops workspace
        # boundary validates it. Do not turn a resolver outage into an
        # unvalidated cross-tenant lookup.
        logger.warning("federation: Contact-Ops identity resolution unavailable")
        return []
    canonical = resolution.get("canonical_person_id")
    if resolution.get("status") not in {"canonical", "redirected"} or not canonical:
        return []
    ids = [
        canonical,
        *(resolution.get("alias_ids") or []),
    ]
    return sorted({str(person_id) for person_id in ids if person_id})


# ── REST transport ─────────────────────────────────────────────────────


@router.get("/v1/contacts/{contact_id}/meetings")
async def rest_meetings(
    contact_id: str,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
    ctx: FederationContext = Depends(require_brigade_token),
    db: Session = Depends(get_db),
):
    _require_read_scope(ctx)
    contact_ids = await _resolved_contact_ids(ctx, contact_id)
    if not contact_ids:
        return _tool_envelope(
            "list_meetings_for_contact",
            {"items": [], "count": 0, "next_cursor": None},
        )
    return _tool_envelope(
        "list_meetings_for_contact",
        list_meetings_for_contact(
            db,
            ctx.org_id,
            contact_ids,
            limit=limit,
            cursor=cursor,
        ),
    )


@router.get("/v1/contacts/{contact_id}/summaries")
async def rest_summaries(
    contact_id: str,
    limit: Optional[int] = None,
    ctx: FederationContext = Depends(require_brigade_token),
    db: Session = Depends(get_db),
):
    _require_read_scope(ctx)
    contact_ids = await _resolved_contact_ids(ctx, contact_id)
    if not contact_ids:
        return _tool_envelope("list_summaries_for_contact", [])
    return _tool_envelope(
        "list_summaries_for_contact",
        list_summaries_for_contact(db, ctx.org_id, contact_ids, limit=limit),
    )


@router.get("/v1/contacts/{contact_id}/action-items")
async def rest_action_items(
    contact_id: str,
    limit: Optional[int] = None,
    ctx: FederationContext = Depends(require_brigade_token),
    db: Session = Depends(get_db),
):
    _require_read_scope(ctx)
    contact_ids = await _resolved_contact_ids(ctx, contact_id)
    if not contact_ids:
        return _tool_envelope("list_action_items_for_contact", [])
    return _tool_envelope(
        "list_action_items_for_contact",
        list_action_items_for_contact(db, ctx.org_id, contact_ids, limit=limit),
    )


@router.get("/v1/contacts/{contact_id}/meeting-summaries")
async def rest_meeting_summaries_v1(
    contact_id: str,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
    updated_since: Optional[str] = None,
    include_transcript: bool = False,
    ctx: FederationContext = Depends(require_brigade_token),
    db: Session = Depends(get_db),
):
    _require_read_scope(ctx)
    if include_transcript:
        _require_transcript_scope(ctx)
    contact_ids = await _resolved_contact_ids(ctx, contact_id)
    return list_meeting_summaries_v1(
        db,
        ctx.org_id,
        contact_ids,
        limit=limit,
        cursor=cursor,
        updated_since=updated_since,
        include_transcript=include_transcript,
    )


# ── MCP JSON-RPC transport ─────────────────────────────────────────────

_TOOL_SCHEMAS = [
    {
        "name": "list_meeting_summaries_v1",
        "description": (
            "Versioned, response-minimized Customer-Ops meeting timeline. "
            "Includes metadata, participants, approved summary, decisions, "
            "and canonical action status. Transcript is opt-in and separately scoped."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "limit": {"type": "integer"},
                "cursor": {"type": "string"},
                "updated_since": {
                    "type": "string",
                    "format": "date-time",
                },
                "include_transcript": {
                    "type": "boolean",
                    "default": False,
                },
            },
            "required": ["contact_id"],
        },
    },
    {
        "name": "list_meetings_for_contact",
        "description": (
            "Meetings where a Contact-Ops contact was a participant "
            "(newest first, keyset-paginated)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "limit": {"type": "integer"},
                "cursor": {"type": "string"},
            },
            "required": ["contact_id"],
        },
    },
    {
        "name": "list_summaries_for_contact",
        "description": "Summaries of meetings where a contact was a participant.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["contact_id"],
        },
    },
    {
        "name": "list_action_items_for_contact",
        "description": "Action items from meetings where a contact was a participant.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["contact_id"],
        },
    },
]


def _rpc_ok(rpc_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _rpc_err(rpc_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _tool_envelope(name: str, data: Any) -> Any:
    """Map an internal tool result to the federation wire envelope the
    Customer-Ops client reads: a {meetings|summaries|action_items: [...]}
    object (NOT a bare list / `items`)."""
    if name == "list_meetings_for_contact" and isinstance(data, dict):
        return {
            "meetings": data.get("items", []),
            "count": data.get("count", 0),
            "next_cursor": data.get("next_cursor"),
        }
    if name == "list_summaries_for_contact":
        return {"summaries": data}
    if name == "list_action_items_for_contact":
        return {"action_items": data}
    return data


async def _dispatch_tool(
    db: Session, ctx: FederationContext, name: str, args: dict
) -> Any:
    contact_id = str(args.get("contact_id") or "").strip()
    if not contact_id:
        raise HTTPException(status_code=400, detail="contact_id is required")
    limit = args.get("limit")
    contact_ids = await _resolved_contact_ids(ctx, contact_id)
    if name == "list_meeting_summaries_v1":
        include_transcript = args.get("include_transcript") is True
        if include_transcript:
            _require_transcript_scope(ctx)
        return list_meeting_summaries_v1(
            db,
            ctx.org_id,
            contact_ids,
            limit=limit,
            cursor=args.get("cursor"),
            updated_since=args.get("updated_since"),
            include_transcript=include_transcript,
        )
    if not contact_ids:
        if name == "list_meetings_for_contact":
            return {"items": [], "count": 0, "next_cursor": None}
        if name in {"list_summaries_for_contact", "list_action_items_for_contact"}:
            return []
    if name == "list_meetings_for_contact":
        return list_meetings_for_contact(
            db, ctx.org_id, contact_ids, limit=limit, cursor=args.get("cursor")
        )
    if name == "list_summaries_for_contact":
        return list_summaries_for_contact(db, ctx.org_id, contact_ids, limit=limit)
    if name == "list_action_items_for_contact":
        return list_action_items_for_contact(db, ctx.org_id, contact_ids, limit=limit)
    raise KeyError(name)


@router.post("/mcp")
async def federation_mcp(
    request: Request,
    ctx: FederationContext = Depends(require_brigade_token),
    db: Session = Depends(get_db),
):
    """Minimal MCP JSON-RPC: initialize / tools/list / tools/call.

    The bearer is verified + tenant-bound by ``require_brigade_token``;
    ``tools/call`` additionally enforces the read scope."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="invalid JSON-RPC envelope")

    method = body.get("method")
    rpc_id = body.get("id")

    if method == "initialize":
        return _rpc_ok(
            rpc_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "meeting-ops-federation",
                    "version": "1.0.0",
                },
            },
        )
    if method in ("notifications/initialized", "initialized"):
        return _rpc_ok(rpc_id, {})
    if method == "tools/list":
        return _rpc_ok(rpc_id, {"tools": _TOOL_SCHEMAS})
    if method == "tools/call":
        _require_read_scope(ctx)
        params = body.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            data = await _dispatch_tool(
                db,
                ctx,
                str(name),
                args if isinstance(args, dict) else {},
            )
        except KeyError:
            return _rpc_err(rpc_id, -32601, f"unknown tool: {name}")
        except HTTPException as exc:
            return _rpc_err(rpc_id, -32602, str(exc.detail))
        envelope = (
            data
            if str(name) == "list_meeting_summaries_v1"
            else _tool_envelope(str(name), data)
        )
        return _rpc_ok(
            rpc_id,
            {
                "structuredContent": envelope,
                "content": [{"type": "text", "text": json.dumps(envelope, default=str)}],
            },
        )

    return _rpc_err(rpc_id, -32601, f"unknown method: {method}")
