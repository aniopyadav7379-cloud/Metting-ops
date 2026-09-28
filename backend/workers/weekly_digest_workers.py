"""Arq cron: automated WEEKLY meeting digest email — v3.24.0.

`api/digests.py` only ever built digests on demand (a logged-in user hits
`GET /api/digests`). There was no schedule and no email. This module adds the
missing piece: a once-a-week arq cron that, for every opted-in organization,
generates last week's digest via the *existing* `_generate_digest()` and emails
it through the *existing* Postmark sender (`auth.email._send`).

Where it runs
-------------
Registered on `workers.bulk_import_worker.WorkerSettings.cron_jobs`, so it fires
inside the dedicated `meet-bulk-import-worker` arq process (the same place the
session-watchdog + media-retention crons already run). The uvicorn API process
is NOT involved.

Schedule
--------
`cron(weekly_digest_cron, weekday=\"mon\", hour={13}, minute={0})` — Monday
13:00 UTC (~start of the US work-week morning). The window is configurable only
in code here; everything else is env-driven.

Opt-in / recipient model
------------------------
- Global kill-switch: `WEEKLY_DIGEST_ENABLED` (default \"true\"). When false the
  cron is an immediate no-op.
- Per-org opt-in lives in `Organization.settings[\"weekly_digest\"]`:
      {\"enabled\": bool, \"recipients\": [\"a@x.com\", ...]}
  Resolution precedence for whether an org is included:
    1. settings.weekly_digest.enabled is False  -> skipped (explicit opt-out)
    2. settings.weekly_digest.enabled is True    -> included
    3. key absent                                -> included ONLY when
       `WEEKLY_DIGEST_DEFAULT_ON` is true (default \"false\" — opt-IN, not
       opt-out, so we never surprise existing orgs with mail).
- Recipients: explicit `settings.weekly_digest.recipients` if present and
  non-empty; otherwise every active+verified admin/manager member of the org
  (UserOrganization.role in {admin, manager}).
- Tier guard: `_generate_digest()` runs the server LLM, which is a paid-tier
  capability at the HTTP layer. To keep parity we only process orgs whose
  `plan` is paid (not \"free\") OR that have at least one non-free member. Free
  orgs are skipped so the cron never spends LLM budget for a tier that can't
  use it. Override with `WEEKLY_DIGEST_REQUIRE_PAID=false` for testing.

Idempotency (don't double-send)
-------------------------------
The digest row is keyed by `(organization_id, period=\"week\", date)`. We stamp
`MeetingDigest.emailed_at` (added in alembic 040) the first time a send
succeeds. On re-entry (cron restart, manual re-run, two-worker race) we skip any
row that already has `emailed_at` set. The column is read defensively via
getattr so the cron degrades to \"may re-send\" rather than crashing if it runs
against a pre-040 schema.

Safety posture (mirrors session_watchdog)
-----------------------------------------
Best-effort, never raises out of the cron entrypoint, bounded by a per-pass org
cap, one DB session per org so a single bad org can't poison the batch, and a
structured `weekly_digest.sent` log line per send for Loki.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ----------------------------- env knobs ---------------------------------- #

def _enabled() -> bool:
    return os.getenv("WEEKLY_DIGEST_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _default_on() -> bool:
    """When an org has NO weekly_digest setting at all, do we include it?

    Default false = opt-IN. Flip `WEEKLY_DIGEST_DEFAULT_ON=true` to make the
    digest opt-OUT (every org gets it unless they explicitly disable)."""
    return os.getenv("WEEKLY_DIGEST_DEFAULT_ON", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _require_paid() -> bool:
    return os.getenv("WEEKLY_DIGEST_REQUIRE_PAID", "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _max_orgs_per_pass() -> int:
    try:
        return max(1, int(os.getenv("WEEKLY_DIGEST_MAX_ORGS_PER_PASS", "500")))
    except ValueError:
        return 500


def _public_base_url() -> str:
    return (
        os.getenv("MEETING_OPS_PUBLIC_URL", "").strip().rstrip("/")
        or os.getenv("APP_PUBLIC_URL", "").strip().rstrip("/")
        or os.getenv("APP_BASE_URL", "").strip().rstrip("/")
        or "https://meetingops.magicunicorn.dev"
    )


# --------------------------- date resolution ------------------------------ #

def _last_week_date_str(now: datetime | None = None) -> str:
    """The `date` argument `_generate_digest(\"week\", date)` should receive to
    summarize the PREVIOUS completed Mon-Sun week.

    `api.digests._get_date_range(\"week\", date)` snaps `date` to Monday of its
    ISO week and spans 7 days. Passing any day inside last week yields last
    week's window. We pass last Monday explicitly so the cached digest's
    `date` is stable + human-meaningful (always a Monday)."""
    now = now or datetime.now(timezone.utc)
    this_monday = (now - timedelta(days=now.weekday())).date()
    last_monday = this_monday - timedelta(days=7)
    return last_monday.strftime("%Y-%m-%d")


# ----------------------------- recipients --------------------------------- #

def _resolve_recipients(db, org) -> list[str]:
    """Recipient emails for an org's weekly digest.

    Explicit list in settings wins; else active+verified admin/manager members.
    De-duplicated, lower-cased compare, original casing preserved on first
    occurrence."""
    from auth.models import User, UserOrganization

    settings = org.settings if isinstance(org.settings, dict) else {}
    cfg = settings.get("weekly_digest") if isinstance(settings.get("weekly_digest"), dict) else {}
    explicit = cfg.get("recipients")
    out: list[str] = []
    seen: set[str] = set()

    def _add(email: str | None) -> None:
        e = (email or "").strip()
        if not e or e.lower() in seen:
            return
        seen.add(e.lower())
        out.append(e)

    if isinstance(explicit, list) and explicit:
        for e in explicit:
            if isinstance(e, str):
                _add(e)
        return out

    rows = (
        db.query(User)
        .join(UserOrganization, UserOrganization.user_id == User.id)
        .filter(
            UserOrganization.organization_id == org.id,
            UserOrganization.role.in_(("admin", "manager")),
            User.is_active.is_(True),
            User.is_verified.is_(True),
        )
        .all()
    )
    for u in rows:
        _add(u.email)
    return out


def _org_is_paid(db, org) -> bool:
    """True if the org's plan is paid OR any member is on a non-free tier."""
    if (org.plan or "free").strip().lower() not in ("free", "", "none"):
        return True
    from auth.models import User, UserOrganization

    paid_member = (
        db.query(User.id)
        .join(UserOrganization, UserOrganization.user_id == User.id)
        .filter(
            UserOrganization.organization_id == org.id,
            User.tier.isnot(None),
            User.tier != "free",
        )
        .first()
    )
    return paid_member is not None


def _org_opted_in(org) -> bool:
    settings = org.settings if isinstance(org.settings, dict) else {}
    cfg = settings.get("weekly_digest") if isinstance(settings.get("weekly_digest"), dict) else None
    if cfg is None or "enabled" not in cfg:
        return _default_on()
    return bool(cfg.get("enabled"))


# ------------------------------- email ------------------------------------ #

def _render_email_html(org_name: str, period_label: str, content: str, dashboard_url: str) -> str:
    import html as _html

    safe_org = _html.escape(org_name or "your organization")
    safe_period = _html.escape(period_label)
    # `content` is LLM-generated plain text (paragraphs). Escape + preserve
    # newlines; never inject as raw HTML.
    safe_content = _html.escape(content or "No completed meetings this week.").replace("\n", "<br>")
    return f"""\
<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;color:#1f2937;background:#f5f5f5;padding:24px">
  <div style="max-width:640px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden">
    <div style="background:linear-gradient(135deg,#7c3aed,#a855f7);color:#fff;padding:28px 32px">
      <div style="font-size:20px;font-weight:700">Meeting-Ops</div>
      <div style="font-size:13px;opacity:.9;margin-top:4px">Weekly digest for {safe_org}</div>
    </div>
    <div style="padding:28px 32px">
      <h2 style="margin:0 0 6px;font-size:18px">Your week in meetings</h2>
      <p style="margin:0 0 18px;font-size:13px;color:#6b7280">{safe_period}</p>
      <div style="font-size:14px;line-height:1.7;color:#374151">{safe_content}</div>
      <p style="margin:28px 0 0">
        <a href="{dashboard_url}" style="display:inline-block;background:#7c3aed;color:#fff;text-decoration:none;
           padding:11px 22px;border-radius:8px;font-size:14px;font-weight:600">Open Meeting-Ops</a>
      </p>
    </div>
    <div style="padding:18px 32px;border-top:1px solid #eee;color:#9ca3af;font-size:12px">
      You're receiving this because weekly digests are enabled for {safe_org}.
      An org admin can turn them off in Settings.
    </div>
  </div>
</body></html>"""


def _send_digest_email(*, recipients: list[str], org_name: str, period_label: str,
                       content: str, dashboard_url: str) -> int:
    """Send the digest to each recipient via the existing Postmark sender.

    Returns the number of successful sends. Uses `auth.email._send`, which is
    already env-agnostic across both prod nodes (POSTMARK_API_TOKEN or
    POSTMARK_SERVER_TOKEN; POSTMARK_FROM_EMAIL or POSTMARK_FROM) and soft-fails
    (logs, returns False) when Postmark isn't configured."""
    from auth.email import _send

    subject = f"Your weekly Meeting-Ops digest — {period_label}"
    html_body = _render_email_html(org_name, period_label, content, dashboard_url)
    text_body = (
        f"Weekly Meeting-Ops digest for {org_name} ({period_label}).\n\n"
        f"{content}\n\nOpen Meeting-Ops: {dashboard_url}"
    )
    sent = 0
    for email in recipients:
        try:
            if _send(email, subject, html_body, text_body):
                sent += 1
        except Exception as exc:  # noqa: BLE001 — _send already soft-fails; belt + suspenders
            logger.warning("weekly_digest: send to %s raised: %s", email, exc)
    return sent


# ------------------------------ core pass --------------------------------- #

async def send_weekly_digests_for_all_orgs(
    *, date_str: str | None = None, dry_run: bool = False,
) -> dict[str, Any]:
    """Generate + email last week's digest for every opted-in org.

    Returns a summary dict. Never raises. Idempotent via
    `MeetingDigest.emailed_at`. One DB session per org.
    """
    if not _enabled():
        return {"enabled": False}

    from database.database import SessionLocal
    from auth.models import Organization
    # Register FK targets in SQLAlchemy metadata before any commit (same
    # gotcha the session_watchdog hit — MeetingDigest -> organizations).
    from database.models import MeetingDigest  # noqa: F401

    period = "week"
    date_str = date_str or _last_week_date_str()
    # Human label e.g. "May 26 – Jun 1, 2026"
    try:
        from api.digests import _get_date_range
        start_dt, end_dt = _get_date_range(period, date_str)
        last_day = end_dt - timedelta(days=1)
        period_label = f"{start_dt.strftime('%b %-d')} – {last_day.strftime('%b %-d, %Y')}"
    except Exception:
        period_label = f"week of {date_str}"

    base_url = _public_base_url()
    dashboard_url = f"{base_url}/dashboard"
    cap = _max_orgs_per_pass()
    require_paid = _require_paid()

    summary: dict[str, Any] = {
        "enabled": True,
        "period": period,
        "date": date_str,
        "orgs_considered": 0,
        "orgs_processed": 0,
        "skipped_opt_out": 0,
        "skipped_free": 0,
        "skipped_no_recipients": 0,
        "skipped_already_emailed": 0,
        "skipped_no_meetings": 0,
        "emails_sent": 0,
        "errors": 0,
        "dry_run": dry_run,
    }

    # Pull the org id list up front in a short-lived session so we don't hold
    # one session open across the (potentially minutes-long) LLM work.
    idx_db = SessionLocal()
    try:
        org_ids = [
            row[0]
            for row in idx_db.query(Organization.id)
            .filter(Organization.is_active.is_(True))
            .order_by(Organization.id)
            .limit(cap)
            .all()
        ]
    finally:
        idx_db.close()

    summary["orgs_considered"] = len(org_ids)

    for org_id in org_ids:
        db = SessionLocal()
        try:
            org = db.query(Organization).filter(Organization.id == org_id).first()
            if org is None:
                continue

            if not _org_opted_in(org):
                summary["skipped_opt_out"] += 1
                continue

            if require_paid and not _org_is_paid(db, org):
                summary["skipped_free"] += 1
                continue

            recipients = _resolve_recipients(db, org)
            if not recipients:
                summary["skipped_no_recipients"] += 1
                continue

            # Idempotency pre-check against an existing cached row.
            existing = (
                db.query(MeetingDigest)
                .filter(
                    MeetingDigest.organization_id == org.id,
                    MeetingDigest.period == period,
                    MeetingDigest.date == date_str,
                )
                .first()
            )
            if existing is not None and getattr(existing, "emailed_at", None) is not None:
                summary["skipped_already_emailed"] += 1
                continue

            # Generate (or reuse cached) the digest. `_generate_digest`
            # upserts the MeetingDigest row keyed by (org, period, date).
            from api.digests import _generate_digest
            result = await _generate_digest(db, org.id, period, date_str, None)

            if (result.meeting_count or 0) <= 0:
                # No completed meetings last week — don't email an empty digest.
                # We intentionally do NOT stamp emailed_at: _generate_digest writes
                # no MeetingDigest row for an empty window, so a stamp would no-op.
                # Re-checking an empty org on a later run is cheap and never
                # double-sends (there is nothing to send).
                summary["skipped_no_meetings"] += 1
                continue

            if dry_run:
                summary["orgs_processed"] += 1
                continue

            sent = _send_digest_email(
                recipients=recipients,
                org_name=org.name,
                period_label=period_label,
                content=result.content,
                dashboard_url=dashboard_url,
            )
            summary["emails_sent"] += sent
            summary["orgs_processed"] += 1

            if sent > 0:
                _stamp_emailed(db, org.id, period, date_str)
                logger.info(
                    "weekly_digest.sent org_id=%s org=%s period=%s date=%s "
                    "recipients=%d sent=%d meetings=%d",
                    org.id, org.slug, period, date_str,
                    len(recipients), sent, result.meeting_count,
                )
            else:
                # Postmark unconfigured or every send failed — leave emailed_at
                # NULL so a later pass retries.
                logger.warning(
                    "weekly_digest: 0/%d sends succeeded org_id=%s date=%s "
                    "(Postmark unconfigured or rejected) — will retry next pass",
                    len(recipients), org.id, date_str,
                )
        except asyncio.CancelledError:
            db.close()
            raise
        except Exception as exc:  # noqa: BLE001
            summary["errors"] += 1
            logger.warning("weekly_digest: org_id=%s failed: %s", org_id, exc)
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()

    logger.info("weekly_digest pass: %s", summary)
    return summary


def _stamp_emailed(db, org_id: int, period: str, date_str: str) -> None:
    """Set emailed_at on the cached digest row. Defensive: if the column
    doesn't exist yet (pre-040 schema) the attribute set + commit will raise;
    we swallow so the cron stays best-effort."""
    from database.models import MeetingDigest

    try:
        row = (
            db.query(MeetingDigest)
            .filter(
                MeetingDigest.organization_id == org_id,
                MeetingDigest.period == period,
                MeetingDigest.date == date_str,
            )
            .first()
        )
        if row is None:
            return
        row.emailed_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("weekly_digest: emailed_at stamp failed org_id=%s: %s", org_id, exc)
        try:
            db.rollback()
        except Exception:
            pass


# ----------------------------- arq entrypoint ----------------------------- #

async def weekly_digest_cron(ctx: dict[str, Any]) -> dict[str, Any]:
    """Weekly cron entrypoint registered on WorkerSettings.cron_jobs.

    Thin wrapper around `send_weekly_digests_for_all_orgs()` so the cron
    signature matches arq's `(ctx)` convention and the heavy logic stays
    independently testable + manually invokable."""
    result = await send_weekly_digests_for_all_orgs()
    logger.info("arq cron: weekly_digest result=%s", result)
    return result
