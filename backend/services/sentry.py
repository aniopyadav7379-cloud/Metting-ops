"""Privacy-preserving Sentry initialization shared by API and Arq."""
from __future__ import annotations

import os
from typing import Any


_SENSITIVE_HEADERS = {
    "authorization", "cookie", "set-cookie", "x-auth-request-email",
    "x-auth-request-groups", "x-forwarded-user", "x-forwarded-email",
    "x-internal-token", "x-api-key",
}


def scrub_event(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    request = event.get("request")
    if isinstance(request, dict):
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {
                key: ("[Filtered]" if key.lower() in _SENSITIVE_HEADERS or key.lower().startswith("x-auth-") else value)
                for key, value in headers.items()
            }
        request.pop("cookies", None)
        request.pop("data", None)
    event.pop("user", None)
    return event


def init_sentry() -> bool:
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False

    import sentry_sdk
    from sentry_sdk.integrations.arq import ArqIntegration
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT") or os.getenv("APP_ENV") or "production",
        integrations=[FastApiIntegration(), StarletteIntegration(), ArqIntegration()],
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.05")),
        send_default_pii=False,
        before_send=scrub_event,
    )
    return True
