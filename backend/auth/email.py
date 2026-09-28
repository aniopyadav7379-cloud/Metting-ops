"""Transactional auth emails (verification, password reset).

Delivery path: Postmark HTTP API, matching the rest of the v3.21.x email
flows (`api/landing.py` invite-request, `api/support.py` contact form,
`api/session_emails.py`, `api/session_permissions.py`). Same token resolution
pattern (`POSTMARK_API_TOKEN` then `POSTMARK_SERVER_TOKEN`), same message
stream, same soft-fail semantics. SMTP was the original transport; it was
never wired in prod, so signup verification silently no-op'd until v3.22.1.

From-address resolution (sender signature MUST match a Postmark verified
signature or the API returns 422):
  1. `POSTMARK_FROM_EMAIL` — canonical for the Meeting-Ops compose env
     (`no-reply@meeting-ops.unicorncommander.ai` on VPS prod).
  2. `POSTMARK_FROM` — back-compat with the older call sites in api/.
  3. Default `no-reply@meeting-ops.unicorncommander.ai`.
If `POSTMARK_FROM_NAME` is set we render `Name <email>`; bare email otherwise.

Graceful degradation: if `POSTMARK_API_TOKEN`/`POSTMARK_SERVER_TOKEN` is
not configured (local dev, tests) we log the link at WARNING and return
False. We never raise — email is best-effort enrichment of the auth flow;
a failed send must not 500 a signup or a password-reset request.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

APP_NAME = "Meeting-Ops"

_POSTMARK_TOKEN_ENVS = ("POSTMARK_API_TOKEN", "POSTMARK_SERVER_TOKEN")
_DEFAULT_FROM_EMAIL = "no-reply@meeting-ops.unicorncommander.ai"


def _postmark_token() -> str:
    for name in _POSTMARK_TOKEN_ENVS:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _postmark_sender() -> str:
    """Resolve the `From` header. Prefers POSTMARK_FROM_EMAIL (compose env),
    falls back to POSTMARK_FROM (older call sites), then a hardcoded default
    matching the Postmark sender signature."""
    email = (
        os.getenv("POSTMARK_FROM_EMAIL", "").strip()
        or os.getenv("POSTMARK_FROM", "").strip()
        or _DEFAULT_FROM_EMAIL
    )
    name = os.getenv("POSTMARK_FROM_NAME", "").strip()
    if name:
        return f"{name} <{email}>"
    return email


def _send(to_email: str, subject: str, html_body: str, text_body: str) -> bool:
    """Send one transactional email via the Postmark HTTP API. Returns True
    on a 2xx from Postmark, False (logged) otherwise. Never raises — a
    failed send is a logged warning, the caller proceeds."""
    token = _postmark_token()
    if not token:
        logger.warning(
            "Postmark not configured (POSTMARK_API_TOKEN unset); not sending "
            "%r to %s. Body preview: %s",
            subject, to_email, text_body[:200],
        )
        return False

    sender = _postmark_sender()
    stream = os.getenv("POSTMARK_MESSAGE_STREAM", "outbound").strip() or "outbound"

    try:
        import httpx
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                "https://api.postmarkapp.com/email",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Postmark-Server-Token": token,
                },
                json={
                    "From": sender,
                    "To": to_email,
                    "Subject": subject,
                    "HtmlBody": html_body,
                    "TextBody": text_body,
                    "MessageStream": stream,
                },
            )
        if resp.status_code != 200:
            logger.warning(
                "auth email: postmark non-200 (%s) sending %r to %s: %s",
                resp.status_code, subject, to_email, resp.text[:300],
            )
            return False
        logger.info("Sent %r to %s via Postmark", subject, to_email)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "auth email: postmark send failed for %r to %s: %s",
            subject, to_email, exc,
        )
        return False


def _button_html(title: str, intro: str, url: str, cta: str, outro: str) -> str:
    return f"""\
<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;color:#333;background:#f5f5f5;padding:24px">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden">
    <div style="background:linear-gradient(135deg,#7c3aed,#a855f7);color:#fff;padding:24px 28px">
      <div style="font-size:20px;font-weight:700">{APP_NAME}</div>
    </div>
    <div style="padding:28px">
      <h2 style="margin:0 0 12px;font-size:18px">{title}</h2>
      <p style="margin:0 0 20px;font-size:14px;line-height:1.6;color:#555">{intro}</p>
      <a href="{url}" style="display:inline-block;background:#7c3aed;color:#fff;text-decoration:none;
         padding:12px 22px;border-radius:8px;font-size:14px;font-weight:600">{cta}</a>
      <p style="margin:20px 0 0;font-size:12px;color:#888">{outro}</p>
      <p style="margin:12px 0 0;font-size:12px;color:#aaa;word-break:break-all">{url}</p>
    </div>
  </div>
</body></html>"""


def send_verification_email(to_email: str, verify_url: str) -> bool:
    """Email a user a link to verify their address."""
    return _send(
        to_email,
        f"Verify your {APP_NAME} email",
        _button_html(
            "Confirm your email",
            f"Welcome to {APP_NAME}. Confirm this address to finish setting up your account.",
            verify_url,
            "Verify email",
            "This link expires in 24 hours. If you didn't create an account, you can ignore this email.",
        ),
        f"Welcome to {APP_NAME}. Verify your email: {verify_url}\n"
        "This link expires in 24 hours.",
    )


def send_password_reset_email(to_email: str, reset_url: str) -> bool:
    """Email a user a link to reset their password."""
    return _send(
        to_email,
        f"Reset your {APP_NAME} password",
        _button_html(
            "Reset your password",
            "We received a request to reset your password. Use the button below to choose a new one.",
            reset_url,
            "Reset password",
            "This link expires in 24 hours. If you didn't request this, you can safely ignore it.",
        ),
        f"Reset your {APP_NAME} password: {reset_url}\nThis link expires in 24 hours.",
    )
