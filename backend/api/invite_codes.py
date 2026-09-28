"""Phase-1 beta invite-code endpoints.

Three routes (contract fixed with the frontend stream):

  POST /api/admin/invite-codes        (admin only)
      Mint `count` single-use codes. Body
      {for_user_id?: int, count: int=5 (1..50), note?: str}. The codes are
      attributed to `for_user_id` (so a seed user's /mine lists them) or, if
      absent, to the calling admin. Returns the minted codes.

  GET  /api/invite-codes/mine         (authed)
      The caller's own codes (created_by_user_id == me), newest first, in the
      contract shape:
      [{code, is_active, redeemed, redeemed_by_email, redeemed_at, created_at}].

  GET  /api/invite-codes/config       (anonymous OK)
      {"require_invite_code": auth_config.REQUIRE_INVITE_CODE} so the signup
      page knows whether to show the invite-code field.

Minting/redemption/comp live in auth/invite_codes.py and are shared with the
self-serve register hook.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from auth.config import auth_config
from auth.dependencies import get_current_user
from auth.email import _send as send_transactional_email
from auth.invite_codes import generate_code
from auth.models import BetaInviteCode, User
from auth.service import AuthService
from api.admin_comps import require_superuser
from database.database import get_db

logger = logging.getLogger(__name__)

# Admin mint endpoint reads naturally under /api/admin; the caller-facing
# read endpoints live under /api/invite-codes. Both are mounted from this
# module (see main.py: router + admin_router).
router = APIRouter(prefix="/api/invite-codes", tags=["invite-codes"])
admin_router = APIRouter(prefix="/api/admin/invite-codes", tags=["invite-codes"])

_MAX_MINT_PER_CALL = 50


class MintInviteCodesRequest(BaseModel):
    for_user_id: Optional[int] = None
    count: int = Field(default=5, ge=1, le=_MAX_MINT_PER_CALL)
    note: Optional[str] = None


class InviteCodeMinted(BaseModel):
    code: str
    is_active: bool
    max_redemptions: int
    created_at: Optional[datetime] = None


class InviteCodeMineItem(BaseModel):
    code: str
    is_active: bool
    redeemed: bool
    redeemed_by_email: Optional[str] = None
    redeemed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


class InviteConfigResponse(BaseModel):
    require_invite_code: bool


class InviteCodeAdminItem(BaseModel):
    code: str
    is_active: bool
    redeemed: bool
    redeemed_by_email: Optional[str] = None
    redeemed_at: Optional[datetime] = None
    emailed_at: Optional[datetime] = None
    created_by_email: Optional[str] = None
    note: Optional[str] = None
    cohort: Optional[str] = None  # parsed from note ("cohort=<x> ...")
    created_at: Optional[datetime] = None


class InviteCodeEmailRecipient(BaseModel):
    email: EmailStr
    code: str


class InviteCodeEmailDryRun(BaseModel):
    code: str
    email: EmailStr
    status: str
    subject: Optional[str] = None
    text_body: Optional[str] = None
    html_body: Optional[str] = None
    error: Optional[str] = None
    emailed_at: Optional[datetime] = None


class InviteCodeEmailResponse(BaseModel):
    dry_run: bool
    total: int
    sent: int
    skipped: int
    failed: int
    results: list[InviteCodeEmailDryRun]


class SendInviteCodesRequest(BaseModel):
    recipients: list[InviteCodeEmailRecipient] = Field(default_factory=list)
    dry_run: bool = True


class SendInviteCodesCohortRequest(BaseModel):
    cohort: str
    recipients: list[EmailStr] = Field(default_factory=list)
    dry_run: bool = True


_COHORT_RE = re.compile(r"cohort=(\S+)")


def _cohort_from_note(note: Optional[str]) -> Optional[str]:
    if not note:
        return None
    m = _COHORT_RE.search(note)
    return m.group(1) if m else None


def _invite_redeem_url(code: str) -> str:
    base = auth_config.APP_BASE_URL.rstrip("/")
    return f"{base}/#/signup?code={quote(code)}"


def _template_name_for_cohort(cohort: Optional[str]) -> str:
    if (cohort or "").strip() == "meeting_ops_v1":
        return "free"
    return "one_dollar"


def _render_invite_email(
    *,
    code: str,
    cohort: Optional[str],
    to_email: str,
) -> tuple[str, str, str]:
    """Render the cohort-specific invite email."""
    link = _invite_redeem_url(code)
    template = _template_name_for_cohort(cohort)
    safe_code = html.escape(code)
    safe_link = html.escape(link)
    safe_to = html.escape(to_email)

    if template == "free":
        subject = "30 days of Meeting-Ops Pro, on us"
        text_body = (
            f"Hi {to_email},\n\n"
            "You've got 30 days of Meeting-Ops Pro, on us.\n"
            f"Your invite code: {code}\n"
            f"Redeem it here: {link}\n\n"
            "If the link does not open, paste the code into the signup page.\n"
        )
        html_body = f"""<!DOCTYPE html>
<html>
  <body style="margin:0;background:#0a0a0a;padding:24px;font-family:Arial,sans-serif;color:#e4e4e7;">
    <div style="max-width:640px;margin:0 auto;border:1px solid #27272a;border-radius:16px;overflow:hidden;background:#111827;">
      <div style="padding:28px 32px;background:linear-gradient(135deg,#7c3aed,#4f46e5);color:#fff;">
        <div style="font-size:14px;letter-spacing:.12em;text-transform:uppercase;opacity:.8;">Meeting-Ops</div>
        <h1 style="margin:10px 0 0;font-size:28px;line-height:1.2;">30 days of Meeting-Ops Pro, on us</h1>
      </div>
      <div style="padding:32px;background:#18181b;">
        <p style="margin:0 0 16px;font-size:16px;line-height:1.6;color:#e4e4e7;">Hi {safe_to},</p>
        <p style="margin:0 0 20px;font-size:16px;line-height:1.6;color:#d4d4d8;">
          Use the invite code below to create your account and unlock 30 days of Pro.
        </p>
        <div style="margin:20px 0;padding:18px 20px;border:1px solid #3f3f46;border-radius:12px;background:#09090b;">
          <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#a1a1aa;margin-bottom:8px;">Invite code</div>
          <div style="font-size:24px;font-weight:700;letter-spacing:.12em;color:#fff;font-family:Courier New,monospace;">{safe_code}</div>
        </div>
        <p style="margin:0 0 24px;">
          <a href="{safe_link}" style="display:inline-block;background:#7c3aed;color:#fff;text-decoration:none;padding:12px 20px;border-radius:10px;font-weight:700;">Redeem invite</a>
        </p>
        <p style="margin:0;font-size:13px;line-height:1.6;color:#a1a1aa;word-break:break-all;">{safe_link}</p>
      </div>
    </div>
  </body>
</html>"""
        return subject, text_body, html_body

    subject = "Your first month of Meeting-Ops Pro is $1"
    text_body = (
        f"Hi {to_email},\n\n"
        "You've got a first month of Meeting-Ops Pro for $1.\n"
        f"Your invite code: {code}\n"
        f"Redeem it here: {link}\n\n"
        "TODO($1 cohort link): if this cohort should use a Stripe coupon checkout instead of invite-code redemption, wire that flow here.\n"
    )
    html_body = f"""<!DOCTYPE html>
<html>
  <body style="margin:0;background:#0a0a0a;padding:24px;font-family:Arial,sans-serif;color:#e4e4e7;">
    <div style="max-width:640px;margin:0 auto;border:1px solid #27272a;border-radius:16px;overflow:hidden;background:#111827;">
      <div style="padding:28px 32px;background:linear-gradient(135deg,#7c3aed,#4f46e5);color:#fff;">
        <div style="font-size:14px;letter-spacing:.12em;text-transform:uppercase;opacity:.8;">Meeting-Ops</div>
        <h1 style="margin:10px 0 0;font-size:28px;line-height:1.2;">Your first month of Meeting-Ops Pro is $1</h1>
      </div>
      <div style="padding:32px;background:#18181b;">
        <p style="margin:0 0 16px;font-size:16px;line-height:1.6;color:#e4e4e7;">Hi {safe_to},</p>
        <p style="margin:0 0 20px;font-size:16px;line-height:1.6;color:#d4d4d8;">
          Use the invite code below to get started.
        </p>
        <div style="margin:20px 0;padding:18px 20px;border:1px solid #3f3f46;border-radius:12px;background:#09090b;">
          <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#a1a1aa;margin-bottom:8px;">Invite code</div>
          <div style="font-size:24px;font-weight:700;letter-spacing:.12em;color:#fff;font-family:Courier New,monospace;">{safe_code}</div>
        </div>
        <p style="margin:0 0 12px;color:#d4d4d8;">TODO($1 cohort link): wire the Stripe coupon checkout link here if this cohort does not redeem via invite-code signup.</p>
        <p style="margin:0 0 24px;">
          <a href="{safe_link}" style="display:inline-block;background:#7c3aed;color:#fff;text-decoration:none;padding:12px 20px;border-radius:10px;font-weight:700;">Redeem invite</a>
        </p>
        <p style="margin:0;font-size:13px;line-height:1.6;color:#a1a1aa;word-break:break-all;">{safe_link}</p>
      </div>
    </div>
  </body>
</html>"""
    return subject, text_body, html_body


def _email_result(
    *,
    code: str,
    email: str,
    status: str,
    subject: Optional[str] = None,
    text_body: Optional[str] = None,
    html_body: Optional[str] = None,
    error: Optional[str] = None,
    emailed_at: Optional[datetime] = None,
) -> InviteCodeEmailDryRun:
    return InviteCodeEmailDryRun(
        code=code,
        email=email,
        status=status,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        error=error,
        emailed_at=emailed_at,
    )


def _send_one_invite(
    *,
    db: Session,
    row: BetaInviteCode,
    to_email: str,
    actor_id: int,
    dry_run: bool,
) -> InviteCodeEmailDryRun:
    if getattr(row, "emailed_at", None) is not None:
        return _email_result(
            code=row.code,
            email=to_email,
            status="skipped_emailed",
            emailed_at=row.emailed_at,
        )
    if row.redemption_count > 0 or not row.is_active:
        return _email_result(
            code=row.code,
            email=to_email,
            status="error",
            error="Invite code is already redeemed or inactive.",
        )

    subject, text_body, html_body = _render_invite_email(
        code=row.code,
        cohort=_cohort_from_note(row.note),
        to_email=to_email,
    )
    if dry_run:
        return _email_result(
            code=row.code,
            email=to_email,
            status="preview",
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )

    sent = send_transactional_email(to_email, subject, html_body, text_body)
    if not sent:
        return _email_result(
            code=row.code,
            email=to_email,
            status="error",
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            error="Postmark rejected the send.",
        )

    row.emailed_at = datetime.now(timezone.utc)
    db.add(row)
    db.commit()
    try:
        AuthService.log_action(
            db,
            actor_id,
            "invite_code_emailed",
            resource_type="invite_code",
            resource_id=row.code,
            details={"to": to_email},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("invite code audit log failed for %s -> %s: %s", row.code, to_email, exc)
    return _email_result(
        code=row.code,
        email=to_email,
        status="sent",
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        emailed_at=row.emailed_at,
    )


def _summarize_results(results: list[InviteCodeEmailDryRun]) -> tuple[int, int, int]:
    sent = sum(1 for result in results if result.status == "sent")
    skipped = sum(1 for result in results if result.status.startswith("skipped"))
    failed = sum(1 for result in results if result.status == "error")
    return sent, skipped, failed


@admin_router.post("", response_model=list[InviteCodeMinted])
async def mint_invite_codes(
    payload: MintInviteCodesRequest,
    current_user: User = Depends(require_superuser),
    db: Session = Depends(get_db),
) -> list[InviteCodeMinted]:
    """Mint `count` single-use invite codes. PLATFORM-SUPERUSER ONLY.

    Phase-1 deliberately restricts minting to a true platform superuser (not an
    org admin): the whole point of the invite gate is controlled scarcity, so
    Aaron mints codes FOR seed users (`for_user_id`) and those users only SHARE
    them (via /mine) — they don't mint their own. NOTE: `require_admin` resolves
    "admin" from the caller's ACTIVE org, and every self-serve user is admin of
    their own {username}-personal org, so require_admin here would let ANY
    signed-in user mint — hence the explicit is_superuser gate. (Phase 2 will
    add a scoped grant that lets invitees mint their own.)

    Attributed to `for_user_id` when supplied (so a seed user's /mine lists
    them), else to the calling superuser. `count` is bounded 1..50 by the schema.
    A `for_user_id` that doesn't resolve to a real user is rejected (400) so
    codes aren't minted into the void."""
    owner_id = payload.for_user_id or current_user.id
    if payload.for_user_id is not None:
        target = db.query(User).filter(User.id == payload.for_user_id).first()
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="for_user_id does not match any user",
            )

    note = (payload.note or "").strip() or None
    minted: list[BetaInviteCode] = []
    # Generate distinct codes; the unique index is the real guard but we
    # pre-dedupe within the batch + retry on the astronomically rare
    # collision so a single dup can't fail the whole mint.
    seen: set[str] = set()
    attempts = 0
    while len(minted) < payload.count and attempts < payload.count * 10:
        attempts += 1
        code = generate_code()
        if code in seen:
            continue
        seen.add(code)
        row = BetaInviteCode(
            code=code,
            created_by_user_id=owner_id,
            max_redemptions=1,
            redemption_count=0,
            is_active=True,
            note=note,
        )
        db.add(row)
        minted.append(row)
    db.commit()

    AuthService.log_action(
        db,
        current_user.id,
        "invite_codes_minted",
        details={
            "count": len(minted),
            "for_user_id": owner_id,
        },
    )

    return [
        InviteCodeMinted(
            code=row.code,
            is_active=row.is_active,
            max_redemptions=row.max_redemptions,
            created_at=row.created_at,
        )
        for row in minted
    ]


@admin_router.get("", response_model=list[InviteCodeAdminItem])
async def list_invite_codes(
    cohort: Optional[str] = Query(default=None, description="match note cohort=<x>"),
    redeemed: Optional[bool] = Query(default=None),
    active: Optional[bool] = Query(default=None),
    limit: int = Query(default=1000, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(require_superuser),
    db: Session = Depends(get_db),
) -> list[InviteCodeAdminItem]:
    """ALL invite codes, newest first. PLATFORM-SUPERUSER ONLY (same gate as the
    minter — the whole point of the invite program is a single operator seeing
    the full pool). Filters: `cohort` (substring of the note's cohort=<x> tag),
    `redeemed`, `active`. This is the read side of the launch console's code
    tracker."""
    query = db.query(BetaInviteCode)
    if cohort:
        query = query.filter(BetaInviteCode.note.ilike(f"%cohort={cohort}%"))
    if redeemed is True:
        query = query.filter(BetaInviteCode.redemption_count > 0)
    elif redeemed is False:
        query = query.filter(BetaInviteCode.redemption_count == 0)
    if active is not None:
        query = query.filter(BetaInviteCode.is_active.is_(active))

    rows = (
        query.order_by(BetaInviteCode.created_at.desc(), BetaInviteCode.id.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )

    # Batch-resolve redeemer + creator emails (avoid N+1).
    ids = {r.redeemed_by_user_id for r in rows if r.redeemed_by_user_id}
    ids |= {r.created_by_user_id for r in rows if r.created_by_user_id}
    emails: dict[int, str] = {}
    if ids:
        for u in db.query(User).filter(User.id.in_(ids)).all():
            emails[u.id] = u.email

    return [
        InviteCodeAdminItem(
            code=row.code,
            is_active=row.is_active,
            redeemed=row.redemption_count > 0,
            redeemed_by_email=emails.get(row.redeemed_by_user_id),
            redeemed_at=row.redeemed_at,
            emailed_at=getattr(row, "emailed_at", None),
            created_by_email=emails.get(row.created_by_user_id),
            note=row.note,
            cohort=_cohort_from_note(row.note),
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/mine", response_model=list[InviteCodeMineItem])
async def my_invite_codes(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[InviteCodeMineItem]:
    """The caller's own invite codes (created_by_user_id == me), newest
    first. `redeemed` reflects whether the code has been consumed at all;
    `redeemed_by_email` is resolved from the redeemer for the seed user's
    hand-out tracking."""
    rows = (
        db.query(BetaInviteCode)
        .filter(BetaInviteCode.created_by_user_id == current_user.id)
        .order_by(BetaInviteCode.created_at.desc(), BetaInviteCode.id.desc())
        .all()
    )

    # Batch-resolve redeemer emails (avoid N+1).
    redeemer_ids = {r.redeemed_by_user_id for r in rows if r.redeemed_by_user_id}
    emails: dict[int, str] = {}
    if redeemer_ids:
        for u in db.query(User).filter(User.id.in_(redeemer_ids)).all():
            emails[u.id] = u.email

    return [
        InviteCodeMineItem(
            code=row.code,
            is_active=row.is_active,
            redeemed=row.redemption_count > 0,
            redeemed_by_email=emails.get(row.redeemed_by_user_id),
            redeemed_at=row.redeemed_at,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/config", response_model=InviteConfigResponse)
async def invite_codes_config() -> InviteConfigResponse:
    """Anonymous: tells the signup page whether an invite code is required."""
    return InviteConfigResponse(
        require_invite_code=auth_config.REQUIRE_INVITE_CODE
    )


@admin_router.post("/send", response_model=InviteCodeEmailResponse)
async def send_invite_codes(
    payload: SendInviteCodesRequest,
    current_user: User = Depends(require_superuser),
    db: Session = Depends(get_db),
) -> InviteCodeEmailResponse:
    if not payload.recipients:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one recipient is required",
        )

    results: list[InviteCodeEmailDryRun] = []
    for recipient in payload.recipients:
        row = (
            db.query(BetaInviteCode)
            .filter(BetaInviteCode.code == recipient.code)
            .first()
        )
        if row is None:
            results.append(
                _email_result(
                    code=recipient.code,
                    email=str(recipient.email),
                    status="error",
                    error="Invite code not found.",
                )
            )
            continue
        results.append(
            _send_one_invite(
                db=db,
                row=row,
                to_email=str(recipient.email),
                actor_id=current_user.id,
                dry_run=payload.dry_run,
            )
        )

    sent, skipped, failed = _summarize_results(results)
    if payload.dry_run:
        sent = 0
        failed = 0
    return InviteCodeEmailResponse(
        dry_run=payload.dry_run,
        total=len(results),
        sent=sent,
        skipped=skipped,
        failed=failed,
        results=results,
    )


@admin_router.post("/send-cohort", response_model=InviteCodeEmailResponse)
async def send_invite_codes_for_cohort(
    payload: SendInviteCodesCohortRequest,
    current_user: User = Depends(require_superuser),
    db: Session = Depends(get_db),
) -> InviteCodeEmailResponse:
    if not payload.recipients:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one recipient is required",
        )
    if not payload.cohort.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A cohort is required",
        )

    query = (
        db.query(BetaInviteCode)
        .filter(BetaInviteCode.note.ilike(f"%cohort={payload.cohort}%"))
        .filter(BetaInviteCode.redemption_count == 0)
        .filter(BetaInviteCode.is_active.is_(True))
        .filter(BetaInviteCode.emailed_at.is_(None))
        .order_by(BetaInviteCode.created_at.asc(), BetaInviteCode.id.asc())
    )
    rows = query.limit(len(payload.recipients)).all()
    if len(rows) < len(payload.recipients):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not enough available invite codes in that cohort",
        )

    results: list[InviteCodeEmailDryRun] = []
    for row, recipient in zip(rows, payload.recipients):
        results.append(
            _send_one_invite(
                db=db,
                row=row,
                to_email=str(recipient),
                actor_id=current_user.id,
                dry_run=payload.dry_run,
            )
        )

    sent, skipped, failed = _summarize_results(results)
    if payload.dry_run:
        sent = 0
        failed = 0
    return InviteCodeEmailResponse(
        dry_run=payload.dry_run,
        total=len(results),
        sent=sent,
        skipped=skipped,
        failed=failed,
        results=results,
    )
