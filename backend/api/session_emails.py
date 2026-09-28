"""POST /api/simple/recording-sessions/{id}/email-attendees

Send a meeting summary/transcript/link to selected attendees via Postmark.
Static PDF copy is the safe default. Only an explicitly included link creates
or rotates a session-scoped collaborator grant.
"""
from __future__ import annotations

import html
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Literal
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from auth.dependencies import get_current_organization, get_current_user
from auth.organization import ActiveOrganization
from auth.tier import gate_feature_for_caller
from api.session_permissions import (
    _consolidate_invitation_identity,
    _effective_delivery_state,
    _lock_invitation_parent,
    _verified_user_for_email,
)
from database.database import get_db
from auth.models import User
from database.models import (
    RecordingSession,
    SessionCollaborator,
    SpeakerProfile,
    SpeakerSessionLink,
)
from services.invitation_tokens import (
    INVITATION_TOKEN_VERSION,
    INVITATION_ISSUANCE_UNAVAILABLE,
    build_authenticated_session_url,
    build_invitation_url,
    generate_invitation_secret,
    hash_invitation_secret,
    invitation_resend_minimum_interval_seconds,
    invitation_v2_issuance_enabled,
)
from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/simple/recording-sessions", tags=["session-emails"])


class EmailAttendeesRequest(BaseModel):
    # When speaker_ids is provided, send to those SpeakerProfile emails (existing
    # behavior). When omitted/empty AND additional_recipients is also empty,
    # fall back to session.participants[*].email.
    speaker_ids: Optional[List[int]] = Field(default=None, max_length=64)
    # Arbitrary external email addresses — people who were NOT identified as
    # speakers in this meeting. A collaborator grant is created only when the
    # caller explicitly includes "link".
    additional_recipients: List[EmailStr] = Field(
        default_factory=list,
        max_length=64,
        description="Free-form external email addresses to also send to",
    )
    include: List[str] = Field(
        default_factory=lambda: ["summary_pdf"],
        description="Which artifacts to include: link, summary_pdf, transcript_pdf",
    )
    brand_mode: Literal[
        "default", "meeting_ops", "workspace", "unbranded"
    ] = "default"
    message: Optional[str] = Field(default=None, max_length=4000)


class EmailAttendeesResponse(BaseModel):
    sent: int
    skipped: int
    failures: List[dict] = Field(default_factory=list)


def _get_session_or_404(db: Session, org_id: int, session_id: str) -> RecordingSession:
    query = db.query(RecordingSession).filter(RecordingSession.organization_id == org_id)
    sess = query.filter(RecordingSession.session_id == session_id).first()
    if sess:
        return sess
    try:
        sess = query.filter(RecordingSession.id == int(session_id)).first()
    except (TypeError, ValueError):
        sess = None
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    return sess


def _resolve_link_for_email(
    db: Session,
    session: RecordingSession,
    inviter: User,
    email: str,
) -> tuple[Optional[str], Optional[int], Optional[str]]:
    """Prepare one copy-once link without duplicating the access grant.

    Reusing a pending grant rotates its secret because plaintext is never
    recoverable from the stored digest. Accepted grants are never silently
    rotated, and both share surfaces honor the same bounded resend interval.

    The final string is an operator-safe failure code, never a provider
    response or recipient address.
    """
    normalized_email = email.strip().lower()
    session = _lock_invitation_parent(db, session.id)
    resolved_user = _verified_user_for_email(db, normalized_email)
    existing = _consolidate_invitation_identity(
        db,
        session_id=session.id,
        user_id=resolved_user.id if resolved_user else None,
        email=normalized_email,
    )
    now = datetime.now(timezone.utc)
    if existing and existing.user_id is not None:
        try:
            authenticated_url = build_authenticated_session_url(
                session.session_id or session.id
            )
        except ValueError:
            existing.delivery_failure_reason = "public_url_not_configured"
            db.commit()
            return None, existing.id, "public_url_not_configured"
        existing.delivery_state = "accepted"
        existing.delivery_failure_reason = None
        db.commit()
        return authenticated_url, existing.id, None
    if existing and _effective_delivery_state(existing) == "accepted":
        db.commit()
        return None, existing.id, "invitation_already_accepted"

    if not invitation_v2_issuance_enabled():
        raise HTTPException(
            status_code=503,
            detail=INVITATION_ISSUANCE_UNAVAILABLE,
        )

    if existing and existing.last_delivery_attempt_at is not None:
        last_attempt = existing.last_delivery_attempt_at
        if last_attempt.tzinfo is None:
            last_attempt = last_attempt.replace(tzinfo=timezone.utc)
        if last_attempt + timedelta(
            seconds=invitation_resend_minimum_interval_seconds()
        ) > now:
            db.commit()
            return None, existing.id, "invitation_attempted_recently"

    secret = generate_invitation_secret()
    if existing is None:
        existing = SessionCollaborator(
            session_id=session.id,
            email=normalized_email,
            access_level="read",
            invited_by_user_id=inviter.id,
            token=uuid.uuid4(),
            token_hash=hash_invitation_secret(secret),
            token_version=INVITATION_TOKEN_VERSION,
            delivery_state="pending",
            delivery_attempt_count=1,
            last_delivery_attempt_at=now,
        )
        db.add(existing)
    else:
        existing.token = uuid.uuid4()
        existing.token_hash = hash_invitation_secret(secret)
        existing.token_version = INVITATION_TOKEN_VERSION
        existing.delivery_failure_reason = None
        existing.last_delivery_attempt_at = now
        existing.delivery_attempt_count = (
            existing.delivery_attempt_count or 0
        ) + 1
        if existing.accepted_at is None:
            existing.delivery_state = "pending"

    try:
        invitation_url = build_invitation_url(secret, require_public=True)
    except ValueError:
        existing.delivery_state = "failed"
        existing.delivery_failure_reason = "public_url_not_configured"
        db.commit()
        db.refresh(existing)
        return None, existing.id, "public_url_not_configured"

    db.commit()
    db.refresh(existing)
    return invitation_url, existing.id, None


def _build_html(
    session: RecordingSession,
    inviter: User,
    recipient_name: str,
    link: Optional[str],
    message: Optional[str],
) -> str:
    title = html.escape(session.title or session.name or "Meeting recording")
    sender_name = html.escape(inviter.full_name or inviter.username or inviter.email)
    body_parts = [
        '<div style="font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; max-width: 600px; margin: 0 auto; color: #1f2937; padding: 24px;">',
        f'<h2 style="color: #1f2937; margin: 0 0 12px;">{title}</h2>',
        f'<p style="color: #4b5563;">Hi {html.escape(recipient_name)},</p>',
        f'<p style="color: #4b5563;">{sender_name} shared a meeting recording with you.</p>',
    ]
    if message:
        body_parts.append(
            f'<div style="border-left: 3px solid #9333ea; padding: 12px 16px; background: #faf5ff; margin: 16px 0; color: #1f2937; white-space: pre-wrap;">{html.escape(message)}</div>'
        )
    if link:
        body_parts.append(
            f'<p style="margin: 24px 0;"><a href="{link}" style="background: #7c3aed; color: white; padding: 10px 20px; text-decoration: none; border-radius: 6px; font-weight: 500;">Open the meeting</a></p>'
        )
        body_parts.append(
            f'<p style="color: #6b7280; font-size: 13px;">Or paste this link into your browser: <a href="{link}" style="color: #7c3aed;">{link}</a></p>'
        )
    body_parts.append(
        '<hr style="border: none; border-top: 1px solid #e5e7eb; margin: 24px 0;" />'
    )
    body_parts.append(
        '<p style="color: #9ca3af; font-size: 12px;">Sent by Meeting-Ops on behalf of '
        + sender_name
        + '. Reply to this email to talk to '
        + sender_name
        + ' directly.</p>'
    )
    body_parts.append("</div>")
    return "".join(body_parts)


def _build_attachments(
    db: Session,
    session: RecordingSession,
    include: List[str],
    brand_mode: str = "default",
) -> List[dict]:
    """Generate base64-encoded attachments for any requested PDFs."""
    import base64
    from api.batch_export import ExportOptions, export_to_pdf

    out: List[dict] = []
    safe_title = (session.title or session.name or "meeting").replace(" ", "_")[:80]
    if "summary_pdf" in include or "transcript_pdf" in include:
        opts_summary = ExportOptions(
            includeTranscript=False,
            brandMode=brand_mode,
        )
        opts_transcript = ExportOptions(
            includeTranscript=True,
            brandMode=brand_mode,
        )
        if "summary_pdf" in include:
            try:
                pdf_bytes = export_to_pdf(session, opts_summary)
                out.append({
                    "Name": f"{safe_title}_summary.pdf",
                    "Content": base64.b64encode(pdf_bytes).decode("ascii"),
                    "ContentType": "application/pdf",
                })
            except Exception as exc:  # noqa: BLE001
                logger.warning("Summary PDF generation failed: %s", exc)
        if "transcript_pdf" in include:
            try:
                pdf_bytes = export_to_pdf(session, opts_transcript)
                out.append({
                    "Name": f"{safe_title}_transcript.pdf",
                    "Content": base64.b64encode(pdf_bytes).decode("ascii"),
                    "ContentType": "application/pdf",
                })
            except Exception as exc:  # noqa: BLE001
                logger.warning("Transcript PDF generation failed: %s", exc)
    return out


def _postmark_send(
    *,
    to_email: str,
    to_name: Optional[str],
    subject: str,
    html_body: str,
    reply_to: Optional[str] = None,
    attachments: Optional[List[dict]] = None,
) -> dict:
    import httpx
    from auth.email import _postmark_sender, _postmark_token

    token = _postmark_token()
    sender = _postmark_sender()
    stream = os.getenv("POSTMARK_MESSAGE_STREAM", "outbound").strip() or "outbound"
    if not token:
        return {
            "ok": False,
            "error": "delivery_not_configured",
        }

    to_field = f"{to_name} <{to_email}>" if to_name else to_email
    payload = {
        "From": sender,
        "To": to_field,
        "Subject": subject,
        "HtmlBody": html_body,
        "MessageStream": stream,
    }
    if reply_to:
        payload["ReplyTo"] = reply_to
    if attachments:
        payload["Attachments"] = attachments

    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(
                "https://api.postmarkapp.com/email",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Postmark-Server-Token": token,
                },
                json=payload,
            )
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.status_code == 200:
            return {"ok": True, "message_id": data.get("MessageID")}
        return {"ok": False, "error": "delivery_failed"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("email-attendees delivery failed error_type=%s", type(exc).__name__)
        return {"ok": False, "error": "delivery_failed"}


@router.post(
    "/{session_id}/email-attendees",
    response_model=EmailAttendeesResponse,
)
async def email_attendees(
    session_id: str,
    payload: EmailAttendeesRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Send the meeting link / summary / transcript to selected speakers."""
    # v3.18.3: defense-in-depth tier gate. Free users have no server-side
    # summaries / canonical transcript to email out (browser-only tier), so
    # this endpoint should never be reachable for them. Explicit gate
    # prevents a future regression silently re-opening the surface.
    gate_feature_for_caller(current_user, "canonical_reprocess", active_org)
    session = _get_session_or_404(db, active_org.organization.id, session_id)

    # Auth: org admin/manager or session creator
    is_admin = active_org.role_name in {"owner", "admin", "manager"}
    is_creator = bool(session.user_id) and session.user_id == current_user.id
    if not (is_admin or is_creator or getattr(current_user, "is_superuser", False)):
        raise HTTPException(status_code=403, detail="Not permitted to email attendees for this session")

    # Build the recipient list. The handler supports three input modes,
    # which can combine in a single send:
    #   1. speaker_ids — named speakers identified in the meeting (existing
    #      EmailAttendeesModal flow). Looks up SpeakerProfile.email.
    #   2. additional_recipients — free-form external email addresses for
    #      people NOT identified as attendees. Each gets a session-scoped
    #      magic link.
    #   3. Neither provided — fall back to session.participants[*].email,
    #      preserving the "send to the people who were here" button on
    #      SessionDetails.
    recipients: List[dict] = []
    if payload.speaker_ids:
        speakers = (
            db.query(SpeakerProfile)
            .filter(
                SpeakerProfile.id.in_(payload.speaker_ids),
                SpeakerProfile.organization_id == active_org.organization.id,
            )
            .all()
        )
        if not speakers:
            # Only 404 here if neither input source produced anything; the
            # caller may still have valid additional_recipients.
            if not payload.additional_recipients:
                raise HTTPException(status_code=404, detail="No matching speakers")
        for sp in speakers:
            recipients.append({
                "ref": f"speaker:{sp.id}",
                "speaker_id": sp.id,
                "name": sp.display_name,
                "email": (sp.email or "").strip(),
            })

    if payload.additional_recipients:
        # Dedupe against already-collected speaker emails so we don't double-send
        # to someone who's both a named speaker and was re-typed into the
        # external-recipients box.
        existing_emails = {(r.get("email") or "").lower() for r in recipients if r.get("email")}
        for raw_email in payload.additional_recipients:
            email = str(raw_email).strip()
            if not email or email.lower() in existing_emails:
                continue
            existing_emails.add(email.lower())
            recipients.append({
                "ref": f"external:{email.lower()}",
                "name": None,
                "email": email,
            })

    if not recipients and not payload.speaker_ids:
        # Neither speaker_ids nor additional_recipients supplied — try the
        # participants fallback.
        raw = session.participants if isinstance(session.participants, list) else []
        for p in raw:
            if not isinstance(p, dict):
                continue
            recipients.append({
                "ref": f"participant:{p.get('id')}",
                "participant_id": p.get("id"),
                "name": (p.get("name") or "").strip() or None,
                "email": (p.get("email") or "").strip(),
            })
        if not recipients:
            raise HTTPException(
                status_code=400,
                detail="No recipients: session has no participants and no speaker_ids or additional_recipients passed",
            )

    if not recipients:
        # speaker_ids was passed but resolved to zero usable rows AND no
        # additional_recipients — refuse explicitly so the UI shows a useful
        # error rather than a silent 200 with sent=0.
        raise HTTPException(
            status_code=400,
            detail="No recipients to send to",
        )

    include = [s.lower() for s in (payload.include or ["summary_pdf"])]
    attachments = _build_attachments(
        db,
        session,
        include,
        payload.brand_mode,
    ) or None

    sent = 0
    skipped = 0
    failures: List[dict] = []

    subject_title = session.title or session.name or "Meeting recording"
    subject = f"Meeting: {subject_title}"

    for r in recipients:
        email = (r.get("email") or "").strip()
        name = r.get("name")
        if not email:
            skipped += 1
            failure: dict = {"name": name, "reason": "no email on file"}
            if "speaker_id" in r:
                failure["speaker_id"] = r["speaker_id"]
            elif "participant_id" in r:
                failure["participant_id"] = r["participant_id"]
            elif r.get("ref", "").startswith("external:"):
                failure["external"] = True
            failures.append(failure)
            continue
        collaborator_id: Optional[int] = None
        if "link" in include:
            link, collaborator_id, link_error = _resolve_link_for_email(
                db,
                session,
                current_user,
                email,
            )
            if link_error:
                skipped += 1
                failure = {"name": name, "reason": link_error}
                if "speaker_id" in r:
                    failure["speaker_id"] = r["speaker_id"]
                elif "participant_id" in r:
                    failure["participant_id"] = r["participant_id"]
                elif r.get("ref", "").startswith("external:"):
                    failure["external"] = True
                failures.append(failure)
                continue
        else:
            link = None
        html_body = _build_html(session, current_user, name or email, link, payload.message)
        result = _postmark_send(
            to_email=email,
            to_name=name,
            subject=subject,
            html_body=html_body,
            reply_to=current_user.email,
            attachments=attachments,
        )
        if collaborator_id is not None:
            collaborator = (
                db.query(SessionCollaborator)
                .filter(SessionCollaborator.id == collaborator_id)
                .first()
            )
            if collaborator and collaborator.revoked_at is None:
                if (
                    collaborator.user_id is not None
                    or collaborator.accepted_at is not None
                ):
                    collaborator.delivery_state = "accepted"
                else:
                    collaborator.delivery_state = (
                        "sent" if result.get("ok") else "failed"
                    )
                collaborator.delivery_failure_reason = (
                    None
                    if result.get("ok")
                    else "delivery_not_configured"
                    if result.get("error") == "delivery_not_configured"
                    else "delivery_failed"
                )
                db.commit()
        if result.get("ok"):
            sent += 1
        else:
            failure = {
                "name": name,
                "reason": "delivery failed",
            }
            if "speaker_id" in r:
                failure["speaker_id"] = r["speaker_id"]
            elif "participant_id" in r:
                failure["participant_id"] = r["participant_id"]
            elif r.get("ref", "").startswith("external:"):
                failure["external"] = True
            failures.append(failure)

    sources = []
    if payload.speaker_ids:
        sources.append("speakers")
    if payload.additional_recipients:
        sources.append("external")
    if not sources:
        sources.append("participants")
    logger.info(
        "email-attendees: session=%s sent=%d skipped=%d failed=%d (source=%s)",
        session.id, sent, skipped, len(failures),
        "+".join(sources),
    )
    return EmailAttendeesResponse(sent=sent, skipped=skipped, failures=failures)
