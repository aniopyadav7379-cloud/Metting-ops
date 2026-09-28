"""
Meeting-Ops Backend - Production main.py
Clean router loading with status tracking and real timestamps.
"""
# Load backend/.env (if present) BEFORE any other import, since several
# modules resolve configuration (e.g. auth.config's SECRET_KEY) as import-
# time defaults. python-dotenv's load_dotenv() never overrides variables
# already set in the real OS environment, so this is purely additive: a
# genuinely empty environment now honors backend/.env exactly as every
# .env.example in this repo already tells the reader to expect, and a
# deployment that sets real environment variables (Docker, systemd, a
# platform's own env injection) is completely unaffected. Fixes a real gap:
# previously, copying .env.example to .env did nothing on its own for the
# backend (unlike the frontend, where Vite loads .env automatically),
# forcing every local run to re-export every variable by hand each shell
# session — the likely cause of the SECRET_KEY/backend-port inconsistencies
# seen across separate local terminal sessions.
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Depends
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import asyncio
import logging
import os
from typing import List

# Phase B.5: Prometheus /metrics ASGI app. Imported here so it's always
# available, even if api.streaming fails to load (degraded mode).
from prometheus_client import make_asgi_app

# Auth dependency for the few endpoints defined directly on `app` here in
# main.py (the routers in api/ import their own). Used to gate /api/system-info.
from auth.dependencies import get_current_user

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from middleware.request_context import RequestIdMiddleware, configure_request_logging
configure_request_logging()

from services.sentry import init_sentry
init_sentry()

# Router loading status tracking
loaded_routers: List[str] = []
failed_routers: List[dict] = []

audio_monitor_task = None


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply browser security headers to every HTTP response."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy-Report-Only"] = (
            "default-src 'self' data: blob:; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https:; "
            "font-src 'self' data:; "
            "connect-src 'self' https: wss: http: ws: blob:; "
            "worker-src 'self' blob:; frame-ancestors 'none'"
        )
        if request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower() == "https":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response


def _load_router(app: FastAPI, name: str, module_path: str, router_attr: str = "router",
                 prefix: str = "", required: bool = False):
    """Load a router with status tracking. Required routers raise on failure."""
    try:
        module = __import__(module_path, fromlist=[router_attr])
        router = getattr(module, router_attr)
        app.include_router(router, prefix=prefix if prefix else "")
        loaded_routers.append(name)
        logger.info(f"Loaded router: {name}")
    except Exception as e:
        entry = {"name": name, "error": str(e)}
        failed_routers.append(entry)
        if required:
            logger.error(f"REQUIRED router failed: {name} - {e}")
            raise RuntimeError(f"Required router '{name}' failed to load: {e}")
        else:
            logger.warning(f"Optional router skipped: {name} - {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global audio_monitor_task

    logger.info("Starting Meeting-Ops Backend...")

    # ── Hosted MCP session manager startup ─────────────────────────────
    # FastMCP's StreamableHTTP transport spins up a long-lived task group
    # to back its session manager. When mounted as a sub-app, that
    # lifespan never fires unless we explicitly drive it from the parent.
    # We enter it here so the /mcp endpoint is ready for the first
    # external request, and tear it down on shutdown.
    _mcp_session_cm = None
    try:
        from api.mcp_http import open_mcp_lifespan
        _mcp_session_cm = await open_mcp_lifespan()
    except Exception as e:  # pragma: no cover - hosted MCP is optional
        logger.warning(f"Hosted MCP session manager not started: {e}")

    # Initialize database
    try:
        from database.database import init_database
        init_database()
        logger.info("Database initialized")
    except Exception as e:
        logger.warning(f"Database initialization failed: {e}")

    # Clean up stale sessions (created but never started)
    try:
        from database.database import SessionLocal
        from database.models import RecordingSession
        from datetime import timedelta
        cleanup_db = SessionLocal()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        stale = cleanup_db.query(RecordingSession).filter(
            RecordingSession.status == "active",
            RecordingSession.started_at.is_(None),
            RecordingSession.created_at < cutoff
        ).all()
        if stale:
            for s in stale:
                s.status = "abandoned"
            cleanup_db.commit()
            logger.info(f"Cleaned up {len(stale)} stale sessions")
        cleanup_db.close()
    except Exception as e:
        logger.warning(f"Stale session cleanup failed: {e}")

    # Check transcription service. The legacy local NPU path stays inert when
    # DISABLE_LOCAL_AUDIO is set (cloud build); STT then routes through
    # ProviderRegistry.get_stt() instead, which is verified at upload time.
    try:
        import os as _os
        from services.transcription_service import transcription_service
        if transcription_service.is_ready:
            logger.info(f"Local transcription service ready: {transcription_service.current_model_id}")
        elif _os.getenv("DISABLE_LOCAL_AUDIO", "").strip().lower() in ("1", "true", "yes", "on"):
            logger.info("Local transcription service inert (DISABLE_LOCAL_AUDIO); STT via ProviderRegistry")
        else:
            logger.warning("Local transcription service not ready")
    except Exception as e:
        logger.warning(f"Transcription service check failed: {e}")

    try:
        from api.uploads import (
            cleanup_stale_uploads,
            recover_pending_uploads,
            start_upload_pipeline_queue,
        )
        await start_upload_pipeline_queue()
        await recover_pending_uploads()
        await cleanup_stale_uploads()
        logger.info("Upload pipeline initialized")
    except Exception as e:
        logger.warning(f"Upload pipeline initialization failed: {e}")

    try:
        from services.tts_jobs import tts_queue
        await tts_queue.start()
        logger.info("TTS render queue initialized")
    except Exception as e:
        logger.warning(f"TTS render queue initialization failed: {e}")

    try:
        from workers.bulk_import_worker import start_arq_worker
        await start_arq_worker()
        logger.info("Bulk import queue initialized")
    except Exception as e:
        logger.warning(f"Bulk import queue initialization failed: {e}")

    logger.info(f"Routers loaded: {len(loaded_routers)}, failed: {len(failed_routers)}")

    yield

    logger.info("Shutting down...")

    # Tear down the hosted MCP session manager if it started.
    if _mcp_session_cm is not None:
        try:
            from api.mcp_http import close_mcp_lifespan
            await close_mcp_lifespan(_mcp_session_cm)
        except Exception as e:
            logger.warning(f"Hosted MCP session manager shutdown failed: {e}")

    # Phase B.5 streaming-drain: send a server_shutdown JSON frame + 1001
    # close to every active WS session on /ws/sessions/{id}/live so the
    # frontend can reconnect cleanly after the deploy lands. The
    # useReconnectingWebSocket hook treats 1001 as a clean close and
    # does NOT auto-reconnect; the explicit shutdown frame lets the
    # client implement its own "reconnect_after_ms" hint. We give each
    # close a short timeout so a wedged socket doesn't block teardown.
    try:
        from api.streaming import active_sessions as _active_streaming_sessions

        # Snapshot the dict so iteration is safe even if a handler
        # finishes mid-iteration and tries to pop itself.
        snapshot = list(_active_streaming_sessions.items())
        if snapshot:
            logger.info(
                "Streaming drain: closing %d active WS session(s) with "
                "server_shutdown + close 1001",
                len(snapshot),
            )

            async def _drain_one(sid, ws):
                try:
                    await asyncio.wait_for(
                        ws.send_json({
                            "type": "server_shutdown",
                            "reason": "deployment",
                            "reconnect_after_ms": 3000,
                        }),
                        timeout=1.0,
                    )
                except Exception as exc:
                    logger.debug("drain send_json failed sid=%s err=%s", sid, exc)
                try:
                    await asyncio.wait_for(
                        ws.close(code=1001, reason="going_away"),
                        timeout=1.0,
                    )
                except Exception as exc:
                    logger.debug("drain close failed sid=%s err=%s", sid, exc)

            # Run all drains in parallel and cap the overall wait at 5 s.
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(_drain_one(sid, ws) for sid, ws in snapshot),
                        return_exceptions=True,
                    ),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning("Streaming drain hit 5s timeout; proceeding with shutdown")
    except Exception as e:
        # api.streaming may have failed to import (optional router); log
        # and continue without blocking the rest of shutdown.
        logger.debug(f"Streaming drain skipped: {e}")

    try:
        from api.uploads import stop_upload_pipeline_queue
        await stop_upload_pipeline_queue()
    except Exception as e:
        logger.warning(f"Upload pipeline shutdown failed: {e}")
    try:
        from services.tts_jobs import tts_queue
        await tts_queue.stop()
    except Exception as e:
        logger.warning(f"TTS render queue shutdown failed: {e}")
    try:
        from workers.bulk_import_worker import stop_arq_worker
        await stop_arq_worker()
    except Exception as e:
        logger.warning(f"Bulk import queue shutdown failed: {e}")
    if audio_monitor_task and not audio_monitor_task.done():
        audio_monitor_task.cancel()
        try:
            await audio_monitor_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Meeting-Ops API",
    description="AI-powered meeting recording and transcription system",
    version="3.58.0",
    lifespan=lifespan
)

_cors_origins = os.getenv("CORS_ORIGINS", "").split(",") if os.getenv("CORS_ORIGINS") else []

# Hosted MCP endpoint is browser-installable from any AI client. The
# known-good clients (Claude Web, Cursor) sit on their own origins; user-
# installed clients can be anything. The endpoint is bearer-auth-only
# (no cookies, no credentials), so a permissive Origin regex is safe.
# Add the .* allow-origin regex when not explicitly pinned via env. When
# CORS_ORIGINS *is* explicitly set, callers are expected to add their
# trusted MCP origins to that list themselves.
_default_origin_regex = (
    r"^https?://(localhost|127\.0\.0\.1|"
    r"10\.\d+\.\d+\.\d+|"
    r"172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|"
    r"192\.168\.\d+\.\d+|"
    r"claude\.ai|"
    r"cursor\.sh|"
    r"continue\.dev"
    r")(:\d+)?$"
)

app.add_middleware(
    CORSMiddleware,
    # In production: set CORS_ORIGINS env var. For LAN appliance: allow frontend port.
    allow_origins=_cors_origins or [],
    allow_origin_regex=_default_origin_regex if not _cors_origins else None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIdMiddleware)
from middleware.http_metrics import HttpMetricsMiddleware
app.add_middleware(HttpMetricsMiddleware)

# Hosted MCP endpoint bypass for CORS gating. CORSMiddleware above
# enforces app-wide CORS, but the MCP endpoint accepts ANY origin
# (browser-installed AI clients carry the Origin of whatever app the
# user opened them from — claude.ai, cursor.sh, but also a long tail of
# custom clients). Adding this AFTER the CORS middleware means it runs
# FIRST on the request path (Starlette middleware is LIFO), so OPTIONS
# preflight + CORS headers on /mcp are handled here and CORSMiddleware
# never sees them. Non-/mcp paths fall through untouched.
try:
    from api.mcp_http import McpCorsBypassMiddleware
    app.add_middleware(McpCorsBypassMiddleware)
except Exception as e:  # pragma: no cover - import-time failure
    logger.warning(f"MCP CORS bypass middleware not installed: {e}")

# === Phase B.5: Prometheus /metrics endpoint ===
# Mounted as an ASGI sub-app so prometheus_client owns the text-format
# response + Content-Type header. Exposed unauthenticated by design:
# scraped by the centerdeep Prometheus on the internal Tailscale net.
# If you ever expose meeting-ops directly to the public internet, gate
# this behind oauth2-proxy via the ingress config (we run behind it on
# meetingops.magicunicorn.dev, so /metrics is already gated there).
app.mount("/metrics", make_asgi_app())

# === REQUIRED ROUTERS (fail fast if missing) ===
_load_router(app, "auth", "auth.routes", required=True)
# Org/membership endpoints under /api/organizations (e.g. PUT /default to set
# the caller's home workspace). Second router object in auth.routes so the
# code lives with the membership helpers; loaded here via router_attr.
_load_router(app, "organizations", "auth.routes", router_attr="organizations_router")
# Unicorn Commander app-level OIDC SSO (/api/auth/sso/uc/*): direct Keycloak
# login + auto-provision, used by the UC dashboard app-card for zero-click SSO.
_load_router(app, "uc_sso", "auth.oidc_sso")
# session_tags must precede recording so /recording-sessions/tags wins
# routing over /recording-sessions/{session_id} (the wildcard would swallow it).
_load_router(app, "session_tags", "api.sessions_tags")
_load_router(app, "recording", "api.simple_recording_db", required=True)
_load_router(app, "always_on_recordings", "api.recording", required=True)
_load_router(app, "medical_visits", "api.medical_visits")
# Brigade Phase 2 read endpoint — lives in api.recording but as a
# second router (no prefix) so the URL is /api/sessions/{id}/brigade-graph.
_load_router(
    app,
    "brigade_graph",
    "api.recording",
    router_attr="brigade_graph_router",
)
# Knowledge Graph — cross-meeting person-centric subgraph (absolute paths).
_load_router(app, "knowledge_graph", "api.knowledge_graph")
_load_router(app, "sessions", "api.sessions", prefix="/api", required=True)
# Per-meeting permissions (collaborators + magic-link share). Two
# routers in one module: `router` carries the auth'd per-session
# endpoints, `public_router` exposes the anonymous token-resolve.
_load_router(app, "session_permissions", "api.session_permissions")
_load_router(
    app,
    "session_permissions_public",
    "api.session_permissions",
    router_attr="public_router",
)
_load_router(app, "session_emails", "api.session_emails")
_load_router(app, "session_participants", "api.sessions_participants")
_load_router(app, "contact_ops_people", "api.contact_ops_people")
_load_router(
    app,
    "federation_summary_approval",
    "api.federation_summary_approval",
)
_load_router(app, "action_items", "api.action_items")
# Per-session file attachments: Granola notes, external transcripts,
# slide decks, photos, etc. `router` is the CRUD; `counts_router` is the
# cheap per-session paperclip-icon lookup for the Sessions list page.
_load_router(app, "session_attachments", "api.session_attachments")
_load_router(
    app,
    "session_attachment_counts",
    "api.session_attachments",
    router_attr="counts_router",
)
# Move-session-between-orgs: org reassignment with cascade + Qdrant re-tag.
_load_router(app, "session_move_org", "api.session_move_org")

# === CORE ROUTERS (optional but expected) ===
_load_router(app, "meeting_management", "api.meeting_management")
# v3.18.3: pass prefix="" explicitly to honor the routers' internal absolute
# paths (e.g. APIRouter(prefix="/api"). The 4 admin routers below previously
# had documentation drift suggesting a double-mount; passing prefix=""
# explicitly closes that ambiguity and makes the mount behavior obvious.
_load_router(app, "ai_settings", "api.ai_settings", prefix="")
# === PHASE 3 ROUTERS ===
_load_router(app, "agents", "api.agents")

_load_router(app, "agent_management", "api.agent_management_api", prefix="")
_load_router(app, "websocket_transcription", "api.websocket_transcription")
_load_router(app, "websocket_auto_summary", "api.websocket_auto_summary")
_load_router(app, "unified_agent", "api.unified_agent_api", prefix="")
_load_router(app, "live_transcription", "api.live_transcription")
_load_router(app, "analytics", "api.analytics_simple")
_load_router(app, "ai_insights", "api.ai_insights")
_load_router(app, "ai_chat", "api.ai_chat")
_load_router(app, "vocabulary", "api.vocabulary")
_load_router(app, "batch_export", "api.batch_export")
_load_router(app, "simple_settings", "api.simple_settings", prefix="")

# === SATELLITE DEVICE ROUTERS (optional) ===
_load_router(app, "satellite_api", "api.satellite_api")
_load_router(app, "websocket_satellite", "api.websocket_satellite")

# === CONFERENCE ROOM ROUTERS (optional) ===
# `router` carries /api/rooms/* (CRUD + sources + pairing + recordings + ACL).
# `system_router` carries /api/system/audio-devices for the setup wizard.
_load_router(app, "rooms", "api.rooms")
_load_router(app, "rooms_system", "api.rooms", router_attr="system_router")

# === COMPANION APP ROUTERS (optional) ===
_load_router(app, "websocket_remote_audio", "api.websocket_remote_audio")

# === PHASE 2 ROUTERS ===
_load_router(app, "provider_settings", "api.provider_settings")
_load_router(app, "integrations", "api.integrations")
# v3.19.0: per-org integration toggles + secrets (Brigade / Project-Ops /
# Contact-Ops / Accounting-Ops / Stable). See api/integrations_org.py.
_load_router(app, "integrations_org", "api.integrations_org")
_load_router(app, "digests", "api.digests")
_load_router(app, "agent_actions", "api.agent_actions")
_load_router(app, "personal_access_tokens", "api.personal_access_tokens")
# Durable inbound transcript federation from Unicorn Stable. PAT auth, normal
# org selection, and session.create RBAC apply; the endpoint never trusts a
# tenant identifier from the request body.
_load_router(app, "stable_ingest", "api.stable_ingest", required=True)
_load_router(app, "omi_integration", "api.omi_webhooks", required=True)
_load_router(app, "lectures", "api.lectures", required=True)
_load_router(app, "decisions_risks", "api.decisions_risks", required=True)
_load_router(app, "important_moments", "api.important_moments", required=True)
_load_router(app, "documents", "api.documents", required=True)
_load_router(app, "google_calendar", "api.google_calendar", required=True)
_load_router(app, "action_item_integrations", "api.action_item_integrations", required=True)

# === PUBLIC LANDING PAGE (invite-only beta) ===
# Single endpoint /api/landing/invite-request, no auth, rate-limited per-IP.
# Powers the v3.19 public landing at meeting-ops.unicorncommander.ai/.
# Inert in dev if VITE_LANDING_PAGE_ENABLED=false on the frontend; the
# backend endpoint still loads either way so direct curl smoke works.
_load_router(app, "landing", "api.landing")

# === CUSTOMER SUPPORT CONTACT FORM (v3.21.0) ===
# Single endpoint /api/support/contact, accepts public + authed posts,
# rate-limited per email. Postmark notify to SUPPORT_NOTIFY_EMAIL
# (default support@magicunicorn.tech). Soft-fail if Postmark unconfigured.
_load_router(app, "support", "api.support")

# === BILLING (Stripe Subscriptions) ===
# Webhook + checkout/portal/subscription endpoints. Inert until
# STRIPE_API_KEY + STRIPE_WEBHOOK_SECRET + STRIPE_PRO_PRICE_ID are set —
# routes return 503 in that case. See backend/api/billing.py for the
# request shapes and backend/api/stripe_webhook.py for the event
# dispatch table.
_load_router(app, "stripe_webhook", "api.stripe_webhook")
_load_router(app, "billing", "api.billing")

# === FOUNDING 100 (v3.21.0) ===
# Public cohort status (cached 60s, no auth) + admin close endpoint.
# Powers the /founding-100 landing page + Aaron's hard-stop kill switch.
_load_router(app, "founding", "api.founding")
_load_router(app, "founding_admin", "api.founding", router_attr="admin_router")

# === PHASE-1 BETA INVITE CODES ===
# Gate self-serve signup behind single-use codes (REQUIRE_INVITE_CODE);
# redeeming one comps the new user's personal org to Pro. Caller-facing
# /api/invite-codes/{mine,config} + admin /api/admin/invite-codes mint.
_load_router(app, "invite_codes", "api.invite_codes")
_load_router(app, "invite_codes_admin", "api.invite_codes", router_attr="admin_router")

# === LAUNCH CONSOLE: COMPS ADMIN (superuser-only) ===
# See + organize the free / $1 launch cohort from inside Meeting-Ops:
# GET /api/admin/comps[/summary] + POST /api/admin/comps/{grant,revoke}.
# The invite-code LIST (GET /api/admin/invite-codes) lives with its minter above.
_load_router(app, "admin_comps", "api.admin_comps", router_attr="admin_router")

# === PHASE 3 ROUTERS ===
_load_router(app, "speakers", "api.speakers")
# "My Voice" portable account-level voiceprint (v3.36.0)
_load_router(app, "my_voice", "api.my_voice")

# === PHASE 4A ROUTERS ===
_load_router(app, "uploads", "api.uploads")
_load_router(app, "upload_websocket", "api.uploads", router_attr="ws_router")

# === BULK AUDIO IMPORT (B-import.1) ===
# Powers the /import page that ingests Aaron 526-file audio archive (and
# any other bulk import workflow). Per-file pipeline runs in the
# BulkImportPipelineQueue worker pool started by the lifespan hook above.
_load_router(app, "bulk_import", "api.imports")
_load_router(app, "bulk_import_admin", "api.admin_imports")



# === PHASE 4C-2 ROUTERS ===
_load_router(app, "tts", "api.tts")
_load_router(app, "tts_websocket", "api.tts", router_attr="ws_router")
_load_router(app, "system_caps", "api.system_caps")
# v3.18.3 background-jobs: generic poll endpoint for arq job status.
_load_router(app, "jobs", "api.jobs")

# === INBOUND FEDERATION (Customer-Ops cockpit signal) ===
# /api/federation/* — contact-centric READ surface (meetings / summaries
# / action items keyed on a Contact-Ops contact_id), authed by a
# Brigade-minted RS256 token (aud=meeting-ops); tenant bound from the
# workspace_id claim. Separate from the per-user PAT MCP at /mcp. Dormant
# until BRIGADE_JWKS_URL is set + Brigade allow-lists meeting-ops as an
# exchange audience. See api/federation_meetings.py.
_load_router(app, "federation", "api.federation_meetings")

# === PHASE B.1 ROUTERS (server-live streaming scaffolding) ===
# /ws/sessions/{session_id}/live — auth via oauth2-proxy forward headers,
# logs binary frames per the 19-byte header protocol in
# docs/phase-b-server-live-streaming.md and acks them. Real Parakeet
# forwarding is Phase B.2.
_load_router(app, "streaming", "api.streaming")

# === HOSTED MCP ENDPOINT (streamable HTTP) ===
# /mcp serves the same FastMCP server that mcp/meeting_ops_mcp.py serves
# over stdio, so external AI clients (Claude Desktop, Cursor, Cline,
# Continue, Zed, ChatGPT Desktop) can connect by URL + PAT without
# cloning the repo. Per-request PAT auth via the existing PAT store;
# every tool call inherits the calling user's RBAC scope. Gated on
# MCP_HOSTED_ENABLED (default true). See docs/mcp-hosted.md.
try:
    from api.mcp_http import mount_mcp_app
    mount_mcp_app(app)
    loaded_routers.append("mcp_http")
except Exception as e:  # pragma: no cover - import-time failure
    failed_routers.append({"name": "mcp_http", "error": str(e)})
    logger.warning(f"Optional mount skipped: mcp_http - {e}")



# === CORE API ENDPOINTS ===

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "message": "Meeting-Ops API",
        "version": "3.50.0",
        "status": "running",
        "docs": "/docs"
    }


@app.get("/api/health")
@app.get("/health")
async def health_check():
    """Health check with router status.

    Mirrored at /api/health so external monitors can reach it through
    the oauth2-proxy SKIP_AUTH_ROUTES list on bigboy without hitting
    the SPA fallback at /health (which oauth2-proxy currently auth-gates
    because /health isn't in the skip set, only /api/health is).
    """
    has_failures = len(failed_routers) > 0

    # v3.18.3: report streaming-degraded state when neither stdlib audioop
    # nor the audioop-lts back-port is available (py3.13+ moat).
    streaming_audioop_ok = True
    try:
        from api import streaming as _streaming
        streaming_audioop_ok = _streaming.audioop is not None
    except Exception:  # pragma: no cover - streaming router not loaded
        streaming_audioop_ok = True

    degraded = has_failures or not streaming_audioop_ok

    # MCP hosted-endpoint health. "disabled" (env-gated off) is not a
    # degraded state; "error" is.
    mcp_status = "disabled"
    try:
        from api.mcp_http import mcp_health_status as _mcp_health
        mcp_status = _mcp_health()
    except Exception:  # pragma: no cover - api.mcp_http failed to import
        mcp_status = "disabled"

    return {
        "status": "degraded" if degraded else "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "routers_loaded": len(loaded_routers),
        "routers_failed": failed_routers if has_failures else [],
        "streaming_audioop": "ok" if streaming_audioop_ok else "missing",
        "mcp": mcp_status,
    }


@app.get("/health/ready")
@app.get("/api/ready")
async def readiness_check():
    """Deep readiness probe for storage, queues, and completion services."""
    from services.readiness import run_readiness_checks

    payload = await run_readiness_checks()
    payload["timestamp"] = datetime.now(timezone.utc).isoformat()
    return JSONResponse(payload, status_code=200 if payload["ready"] else 503)


@app.get("/api/status")
async def get_status():
    """Get system status"""
    try:
        from services.readiness import run_readiness_checks
        readiness = await run_readiness_checks()
        npu_available = os.path.exists("/dev/accel/accel0")

        from services.transcription_service import transcription_service
        transcription_ready = transcription_service and transcription_service.is_ready
        model_loaded = transcription_service.current_model_id if transcription_service else "none"

        audio_devices = []
        try:
            from services.working_audio_service import WorkingAudioService
            audio_service = WorkingAudioService()
            audio_devices = audio_service.get_audio_devices()
        except Exception as e:
            logger.error(f"Failed to get audio devices: {e}")

        return {
            "status": "healthy" if readiness["ready"] else "degraded",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "transcription": {
                "ready": transcription_ready,
                "model": model_loaded,
                "engine": model_loaded if transcription_ready else "none"
            },
            "npu": {
                "available": npu_available,
                "device": "/dev/accel/accel0" if npu_available else None,
                "status": "active" if npu_available and transcription_ready else "idle"
            },
            "audio": {
                "devices_count": len(audio_devices),
                "server_mic_available": any("usb" in d.get("name", "").lower() or
                                           "m-305" in d.get("name", "").lower()
                                           for d in audio_devices)
            },
            "services": {
                "recording": True,
                "transcription": readiness["dependencies"]["stt"]["ok"],
                "ai_notes": readiness["dependencies"]["llm"]["ok"],
                "websockets": True
            },
            "dependencies": readiness["dependencies"],
            "routers": {
                "loaded": len(loaded_routers),
                "failed": len(failed_routers),
            }
        }
    except Exception as e:
        logger.error(f"Error getting status: {e}")
        return JSONResponse(
            {"status": "error", "error": str(e)},
            status_code=503,
        )


@app.get("/api/audio-devices")
async def get_audio_devices():
    """Get list of available audio input devices"""
    try:
        from services.working_audio_service import WorkingAudioService
        audio_service = WorkingAudioService()
        devices = audio_service.get_audio_devices()
        return {"devices": devices, "status": "success"}
    except Exception as e:
        logger.error(f"Failed to get audio devices: {e}")
        return {"devices": [], "status": "error", "message": str(e)}


@app.get("/api/system-info")
async def get_system_info(current_user=Depends(get_current_user)):
    """Get detailed system information.

    Auth-gated: this leaks host details (platform, Python version, CPU/memory,
    NPU driver) so it must not be public. On the cloud node oauth2-proxy already
    fronts it; this closes the gap on a self-hosted node with no proxy. (Unlike
    /api/status, which is intentionally public for container health checks.)
    """
    import platform
    import psutil

    try:
        npu_available = os.path.exists("/dev/accel/accel0")

        from services.transcription_service import transcription_service
        transcription_ready = transcription_service and transcription_service.is_ready
        model_loaded = transcription_service.current_model_id if transcription_service else "none"

        return {
            "system": {
                "platform": platform.platform(),
                "python_version": platform.python_version(),
                "cpu_count": psutil.cpu_count(),
                "memory_total": psutil.virtual_memory().total,
                "memory_available": psutil.virtual_memory().available
            },
            "npu": {
                "available": npu_available,
                "device_path": "/dev/accel/accel0" if npu_available else None,
                "status": "active" if npu_available and transcription_ready else "idle",
                "driver": "MLIR-AIE2" if npu_available else None
            },
            "transcription": {
                "service_ready": transcription_ready,
                "current_model": model_loaded,
                "engine_type": (model_loaded if transcription_ready else "none"),
                "compute": "npu" if npu_available else "server-gpu"
            },
            "version": "3.50.0",
            "build": "production"
        }
    except Exception as e:
        logger.error(f"Error getting system info: {e}")
        return {"error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9050)
