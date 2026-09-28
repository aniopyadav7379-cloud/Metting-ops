"""Hosted Meeting-Ops MCP endpoint over streamable HTTP.

Mounts the shared FastMCP server (defined in
``backend.services.mcp_app``) at ``/mcp`` on the main FastAPI app, so
external AI clients (Claude Desktop, Cursor, Cline, Continue, Zed,
ChatGPT Desktop, etc.) can connect without cloning the repo.

Auth: every request must carry ``Authorization: Bearer mops_pat_<...>``.
The token is validated against the existing PAT store (the SAME path
used by the JWT-or-PAT dependency at ``auth.dependencies``). On success,
the resolved PAT is pinned to a contextvar for the duration of the
request so the shared tool code can forward it to the backend REST API
and inherit per-user RBAC for free.

The hosted transport is gated by ``MCP_HOSTED_ENABLED`` (default true).
When false, ``mount_mcp_app`` is a no-op and ``/mcp`` 404s.

Health check: :func:`mcp_health_status` returns ``"ok"``, ``"disabled"``,
or ``"error: <reason>"`` so the top-level ``/health`` endpoint can
surface MCP availability at a glance.
"""

from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable

from fastapi import FastAPI


logger = logging.getLogger(__name__)


# Mirrors the FastMCP default; we mount at "/mcp" and let FastMCP own
# the trailing path internals.
_MOUNT_PATH = "/mcp"

# Origins explicitly allowed for browser-installed MCP clients. The
# wildcard catches arbitrary user-installed clients; the named entries
# are documentation for the known-good targets. We do NOT send
# credentials on the MCP endpoint (bearer-only), so a permissive Origin
# allowlist is safe.
_ALLOWED_ORIGIN_HINTS = (
    "https://claude.ai",
    "https://cursor.sh",
)


# Track readiness for /health.
_state: dict[str, str] = {"status": "disabled"}

# Holder for the currently-active inner FastMCP ASGI app. Recomputed
# every time the lifespan starts so the test harness (which spins
# lifespans up + down within one process) and uvicorn --reload always
# see a fresh, run-once session manager. Production hits this exactly
# once on startup.
_active_inner: dict[str, object] = {"app": None}


def _enabled() -> bool:
    raw = os.getenv("MCP_HOSTED_ENABLED", "true").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def mcp_health_status() -> str:
    """Return the current MCP hosted-endpoint health status."""
    return _state.get("status", "disabled")


# ── ASGI middleware: PAT auth + ContextVar pinning ─────────────────────


class _PATAuthMiddleware:
    """Minimal ASGI middleware that gates the mounted FastMCP app on PATs.

    Wraps the streamable-HTTP ASGI sub-app produced by FastMCP. On every
    HTTP request:

    1. Pull ``Authorization: Bearer mops_pat_...`` from the headers.
    2. Resolve the PAT via the existing ``auth.pat.resolve_pat`` helper
       (same code path as the JWT-or-PAT dependency, so behavior stays
       in lockstep).
    3. Pin the validated PAT onto the shared ``set_pat`` contextvar and
       force the backend URL to localhost (the MCP tools call the same
       FastAPI app they're mounted on — no round-trip outside the
       container).
    4. Reset the contextvar after the inner app finishes, even on error.

    Preflight (CORS ``OPTIONS``) requests are answered directly without
    auth so browser-installed MCP clients (Claude Web, Cursor) can probe
    the endpoint.
    """

    def __init__(self, inner_app):
        self._inner = inner_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._inner(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }

        method = scope.get("method", "GET").upper()
        origin = headers.get("origin")

        # Preflight: respond with the permissive CORS headers and bail
        # before any auth.
        if method == "OPTIONS":
            await self._send_cors_preflight(send, origin)
            return

        # Extract bearer
        auth = headers.get("authorization", "")
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()

        # Import lazily so the api/mcp_http import doesn't pull in
        # database modules during conftest's reload dance.
        from auth.pat import TOKEN_PREFIX as PAT_PREFIX, resolve_pat
        from database.database import SessionLocal

        if not token or not token.startswith(PAT_PREFIX):
            await self._send_401(
                send,
                origin,
                "Missing or malformed Authorization header. Expected "
                "'Authorization: Bearer mops_pat_...'.",
            )
            return

        user = None
        db = SessionLocal()
        try:
            try:
                user = resolve_pat(db, plaintext=token)
            except Exception:
                logger.exception("PAT resolution failed for prefix=%s", token[:12])
                user = None
        finally:
            db.close()

        if not user:
            await self._send_401(
                send,
                origin,
                "Invalid or revoked Personal Access Token.",
            )
            return

        # Pin PAT + backend URL onto the shared contextvars for the
        # duration of this request, then dispatch to FastMCP. Tools read
        # those contextvars to forward calls back into the same backend
        # API (over localhost) on behalf of the authenticated user.
        from services.mcp_app import (
            set_backend_url,
            set_pat,
            reset_backend_url,
            reset_pat,
        )

        pat_token = set_pat(token)
        backend_url = os.getenv("MCP_BACKEND_URL", "http://localhost:9050")
        backend_token = set_backend_url(backend_url)

        # Wrap `send` so we can stamp CORS headers onto the FastMCP
        # response without touching the FastMCP internals.
        async def send_with_cors(message):
            if message.get("type") == "http.response.start":
                message = _augment_headers_with_cors(message, origin)
            await send(message)

        try:
            await self._inner(scope, receive, send_with_cors)
        finally:
            reset_pat(pat_token)
            reset_backend_url(backend_token)

    async def _send_cors_preflight(self, send, origin: str | None) -> None:
        headers = _cors_headers(origin) + [
            (b"content-length", b"0"),
        ]
        await send({
            "type": "http.response.start",
            "status": 204,
            "headers": headers,
        })
        await send({"type": "http.response.body", "body": b""})

    async def _send_401(self, send, origin: str | None, detail: str) -> None:
        import json as _json

        body = _json.dumps({"error": "unauthorized", "detail": detail}).encode("utf-8")
        headers = _cors_headers(origin) + [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"www-authenticate", b'Bearer realm="meeting-ops-mcp"'),
        ]
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": headers,
        })
        await send({"type": "http.response.body", "body": body})


def _cors_headers(origin: str | None) -> list[tuple[bytes, bytes]]:
    """Build the permissive CORS headers used by every MCP response.

    Echoing the caller's Origin (instead of a wildcard) keeps the
    response compatible with future ``credentials: 'include'`` callers
    without us changing behavior. For unknown origins we still echo —
    we're bearer-auth-only, so origin isn't a trust boundary here.
    """

    allow_origin = (origin or "*").encode("latin-1")
    return [
        (b"access-control-allow-origin", allow_origin),
        (b"access-control-allow-methods", b"GET, POST, OPTIONS, DELETE"),
        (
            b"access-control-allow-headers",
            b"Authorization, Content-Type, MCP-Protocol-Version, MCP-Session-Id, "
            b"Accept, Last-Event-ID",
        ),
        (
            b"access-control-expose-headers",
            b"MCP-Session-Id, MCP-Protocol-Version",
        ),
        (b"access-control-max-age", b"600"),
        (b"vary", b"Origin"),
    ]


def _augment_headers_with_cors(start_message: dict, origin: str | None) -> dict:
    existing = list(start_message.get("headers", []))
    cors = _cors_headers(origin)
    # Drop any pre-existing CORS headers from the inner app so ours win.
    cors_keys = {h[0] for h in cors}
    filtered = [h for h in existing if h[0].lower() not in cors_keys]
    start_message = dict(start_message)
    start_message["headers"] = filtered + cors
    return start_message


# ── Mount entry point ──────────────────────────────────────────────────


def mount_mcp_app(app: FastAPI) -> None:
    """Mount the hosted MCP streamable-HTTP transport onto the FastAPI app.

    Idempotent and gated on ``MCP_HOSTED_ENABLED``. Logs a one-line
    summary on either path so deploy logs make the activation state
    obvious.
    """

    if not _enabled():
        _state["status"] = "disabled"
        logger.info(
            "Hosted MCP endpoint disabled (MCP_HOSTED_ENABLED=false); "
            "%s will 404.",
            _MOUNT_PATH,
        )
        return

    try:
        # We don't build the inner FastMCP ASGI app here. Instead, mount
        # a proxy that resolves the current inner app at request time,
        # so the lifespan can rebuild it on each start (the underlying
        # session manager is single-use). See open_mcp_lifespan().
        wrapped = _PATAuthMiddleware(_LazyInnerApp())
        # FastMCP's streamable_http_app already owns the path "/mcp"
        # internally. Mount it at "/" of our sub-tree so the public URL
        # ends up as "/mcp" exactly once.
        app.mount(_MOUNT_PATH, _MountedAtRoot(wrapped))

        _state["status"] = "ok"
        logger.info(
            "Hosted MCP endpoint mounted at %s (transport=streamable_http)",
            _MOUNT_PATH,
        )
    except Exception as exc:  # pragma: no cover - import-time failure
        _state["status"] = f"error: {exc}"
        logger.exception("Failed to mount hosted MCP endpoint: %s", exc)


class _LazyInnerApp:
    """Dispatches to the FastMCP ASGI app currently registered in
    ``_active_inner``. Lets the parent FastAPI app mount a stable
    handler that the lifespan can swap out on each start.
    """

    async def __call__(self, scope, receive, send):
        inner = _active_inner.get("app")
        if inner is None:
            # Lifespan hasn't run yet — return 503 so the failure mode
            # is obvious to a probing client.
            if scope.get("type") == "http":
                import json as _json
                body = _json.dumps({
                    "error": "service_unavailable",
                    "detail": "Hosted MCP session manager not started.",
                }).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
            return
        await inner(scope, receive, send)


class _MountedAtRoot:
    """Make FastMCP's ``/mcp`` route reachable through a FastAPI mount.

    Mount semantics (Starlette ``Mount``) extend ``root_path`` to include
    the mount prefix, so the sub-app's ``get_route_path()`` returns the
    request URL minus the prefix. FastMCP's streamable-HTTP app routes
    its handler at the literal path ``/mcp`` (its server-internal mount
    expects to live at the URL root). When we mount it under our own
    ``/mcp``, the sub-app sees ``route_path = "/"`` (or ``""``) and 404s.

    Fix: drop the mount prefix from ``root_path`` before handing the
    scope to the sub-app, AND force ``path`` to ``/mcp`` so the inner
    Starlette routes match exactly. This makes the sub-app behave as if
    it were the URL root receiving ``/mcp`` directly — which is what
    FastMCP's ``streamable_http_app()`` was designed for.
    """

    def __init__(self, inner):
        self._inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            scope = dict(scope)
            # Remove our mount prefix from root_path so the inner app
            # resolves /mcp at its own route table root.
            root_path = scope.get("root_path", "") or ""
            if root_path.endswith(_MOUNT_PATH):
                scope["root_path"] = root_path[: -len(_MOUNT_PATH)]
            # Force the path to /mcp regardless of trailing-slash
            # variants. FastMCP's only route is /mcp; we never want the
            # parent Starlette to issue a 307 redirect to /mcp/ which
            # would then 404.
            scope["path"] = "/mcp"
            scope["raw_path"] = b"/mcp"
        await self._inner(scope, receive, send)


class McpCorsBypassMiddleware:
    """ASGI middleware that owns CORS for /mcp paths only.

    The main app's :class:`fastapi.middleware.cors.CORSMiddleware` is
    configured for the SPA's known origins (LAN, claude.ai, cursor.sh).
    The MCP endpoint is browser-installable from arbitrary AI clients,
    so we need a permissive Origin policy that doesn't loosen the rest
    of the app. This middleware:

    * Returns CORS preflight (OPTIONS) for /mcp synchronously, before
      any downstream router runs.
    * Stamps CORS headers onto every non-preflight /mcp response so the
      browser-installed clients can read the body.
    * Passes through any non-/mcp request untouched.

    Added to the FastAPI app AFTER the main CORSMiddleware so it runs
    FIRST on each request (Starlette middleware is LIFO). The net effect:
    /mcp gets the permissive policy below; everything else continues to
    use the strict app-wide CORS policy.
    """

    def __init__(self, app):
        self._inner = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self._inner(scope, receive, send)
            return

        path = scope.get("path", "")
        if not (path == _MOUNT_PATH or path.startswith(_MOUNT_PATH + "/")):
            await self._inner(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }
        origin = headers.get("origin")
        method = scope.get("method", "GET").upper()

        if method == "OPTIONS":
            preflight_headers = _cors_headers(origin) + [
                (b"content-length", b"0"),
            ]
            await send({
                "type": "http.response.start",
                "status": 204,
                "headers": preflight_headers,
            })
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message):
            if message.get("type") == "http.response.start":
                message = _augment_headers_with_cors(message, origin)
            await send(message)

        await self._inner(scope, receive, send_with_cors)


# ── Lifespan plumbing ──────────────────────────────────────────────────


async def open_mcp_lifespan():
    """Start FastMCP's StreamableHTTP session manager.

    FastMCP backs the streamable transport with a long-lived task group.
    When the hosted MCP app is mounted under FastAPI (instead of being
    Starlette's own root app), its inner lifespan does NOT fire — so the
    session manager never runs and every request returns 404.

    We call this from the parent FastAPI app's lifespan to drive the
    session manager ourselves. Returns an opaque handle the caller MUST
    pass to :func:`close_mcp_lifespan` on shutdown.

    No-op if hosted MCP is disabled.
    """
    if not _enabled():
        return None
    try:
        from services.mcp_app import mcp as fastmcp_instance
        # The session manager is single-use: ``.run()`` can only be called
        # once per instance. Reset the cached one so a fresh manager is
        # built — this matters for the test harness, which spins the
        # FastAPI lifespan up + down many times per session, and for
        # uvicorn --reload during dev.
        fastmcp_instance._session_manager = None
        inner_app = fastmcp_instance.streamable_http_app()
        # Publish the freshly-built ASGI sub-app so _LazyInnerApp picks
        # it up on subsequent requests.
        _active_inner["app"] = inner_app
        cm = fastmcp_instance.session_manager.run()
        await cm.__aenter__()
        logger.info("Hosted MCP session manager started.")
        return cm
    except Exception as exc:
        logger.exception("Failed to start hosted MCP session manager: %s", exc)
        _state["status"] = f"error: {exc}"
        _active_inner["app"] = None
        return None


async def close_mcp_lifespan(cm) -> None:
    if cm is None:
        return
    try:
        await cm.__aexit__(None, None, None)
        logger.info("Hosted MCP session manager stopped.")
    except Exception as exc:  # pragma: no cover
        logger.warning("Hosted MCP session manager shutdown error: %s", exc)
    finally:
        _active_inner["app"] = None


__all__ = [
    "mount_mcp_app",
    "mcp_health_status",
    "McpCorsBypassMiddleware",
    "open_mcp_lifespan",
    "close_mcp_lifespan",
]
