"""Notification providers (Phase 3G): Slack, Microsoft Teams (both Incoming
Webhooks - no OAuth needed for this mode), and Email (SMTP)."""
from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText
from typing import Optional

import httpx

from services.integrations.org_config import IntegrationConfig

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


class ProviderNotConfigured(RuntimeError):
    pass


class ProviderActionError(RuntimeError):
    pass


async def send_slack_message(config: IntegrationConfig, *, text: str) -> dict:
    """POST to a Slack Incoming Webhook URL (stored as api_base_url)."""
    if not config.enabled or not config.api_base_url:
        raise ProviderNotConfigured("Slack is not configured (add an Incoming Webhook URL in Integrations settings)")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(config.api_base_url, json={"text": text})
    if resp.status_code >= 400:
        raise ProviderActionError(f"Slack notification failed ({resp.status_code}): {resp.text[:300]}")
    return {"provider": "slack", "ok": True}


async def send_teams_message(config: IntegrationConfig, *, text: str, title: Optional[str] = None) -> dict:
    """POST to a Microsoft Teams Incoming Webhook (connector) URL."""
    if not config.enabled or not config.api_base_url:
        raise ProviderNotConfigured("Microsoft Teams is not configured (add an Incoming Webhook URL in Integrations settings)")
    body = {"@type": "MessageCard", "@context": "http://schema.org/extensions", "summary": title or "Meeting-Ops notification", "text": text}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(config.api_base_url, json=body)
    if resp.status_code >= 400:
        raise ProviderActionError(f"Teams notification failed ({resp.status_code}): {resp.text[:300]}")
    return {"provider": "teams", "ok": True}


def send_email(config: IntegrationConfig, *, to_address: str, subject: str, body: str) -> dict:
    """Real SMTP send (blocking - caller should run via asyncio.to_thread).
    Not mocked as a queue/log entry: this is a genuine smtplib connection
    attempt against the configured host, exactly like every other
    integration in this phase."""
    smtp_host = config.extra.get("smtp_host") if config.extra else None
    if not config.enabled or not smtp_host or not config.api_key:
        raise ProviderNotConfigured("Email is not configured (set SMTP host/username/password in Integrations settings)")
    smtp_port = int(config.extra.get("smtp_port") or 587)
    smtp_username = config.extra.get("smtp_username") or ""
    from_address = config.extra.get("smtp_from_address") or smtp_username
    use_tls = bool(config.extra.get("smtp_use_tls", True))

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_address
    msg["To"] = to_address

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            if use_tls:
                server.starttls()
            if smtp_username and config.api_key:
                server.login(smtp_username, config.api_key)
            server.sendmail(from_address, [to_address], msg.as_string())
    except (smtplib.SMTPException, OSError) as exc:
        raise ProviderActionError(f"Email send failed: {exc}") from exc
    return {"provider": "email", "ok": True, "to": to_address}
