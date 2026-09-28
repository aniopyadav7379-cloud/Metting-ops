"""HTTP client for the Project-Ops action-item federation surface.

Used by ``services.projectops_writer`` to propose Meeting-Ops action items and
by ``services.projectops_lifecycle`` to read back approval/task links.

Project-Ops API surface used here (verified live against
``http://project-ops-backend:3201`` on bigboy — a NestJS app, global
prefix ``/api/v1``):

  GET  /api/v1/projects?page=&limit=
    paginated list ``{data: [{id, projectNumber, ...}], meta: {...}}``.
    Used to resolve a human project number ("P-00055") -> the UUID the
    task-create endpoint actually wants. There is NO project-by-number
    route, so we page + match client-side (cached per client instance).

  POST /api/v1/tasks
    body: {projectId(UUID), title, description?, type?, status?,
           priority?, dueDate?(ISO), assignedToId?}
    201 -> the created task incl. ``id`` + ``project.projectNumber``.
    Guarded by JwtAuthGuard + RolesGuard (ADMIN | MANAGER | STAFF).

  POST /api/v1/triage/source-lifecycle
    body: {sourceActionItemIds: [<Meeting-Ops action_items.id>]}
    200 -> content-free proposal/task linkage records for the token workspace.

Auth — two modes, picked at client construction:

  1. **Auto-refresh** (preferred): set ``PROJECTOPS_KC_TOKEN_URL`` +
     ``PROJECTOPS_KC_CLIENT_ID`` + ``PROJECTOPS_KC_CLIENT_SECRET`` and the
     client mints fresh service-account JWTs on demand via the
     ``client_credentials`` grant, caches them until ~30s before exp, and
     re-mints transparently. This is the production-grade shape — no
     human in the loop when KC rotates a token; no annual ``.env`` paste.

  2. **Static bearer** (legacy / smoke-test): set ``PROJECTOPS_API_KEY``
     to a pre-minted JWT. The client uses it as-is and never refreshes.
     Useful for one-shot scripts and as a fallback if KC is briefly
     unreachable at startup, but the JWT's exp is your problem.

If both are set, auto-refresh wins; the static value is ignored. If
neither is set, the client is in log-only mode (``is_live`` False), all
write methods short-circuit, and the writer reports mode='no-op'.

Contract note (differs from brigade_client on purpose): the write methods
RAISE ``ProjectOpsClientError`` on terminal HTTP / network failure. The
writer (``projectops_writer``) is the swallow boundary — it catches and
returns ``ok=False`` so the recording pipeline never sees an exception.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Optional

import httpx


logger = logging.getLogger(__name__)


SOURCE_LIFECYCLE_VERSION = "meeting-ops.action-lifecycle.v1"


# Internal default keeps the client working inside the bigboy stack with
# no env wiring. meet-backend and project-ops-backend share the
# ``unicorn-network`` docker network, so the service name resolves.
# NOTE: 3201 (NestJS) + the ``/api/v1`` global prefix — NOT 8000.
DEFAULT_BASE_URL = "http://project-ops-backend:3201/api/v1"

# Task creation is a single Prisma insert; >8s means a real problem and
# the writer is fire-and-forget anyway. Slightly higher than brigade's
# 5s because we may also page the projects list to resolve a number.
DEFAULT_TIMEOUT_SECONDS = 8.0

# Triage ingest runs an embed + rerank + LLM reasoning pass PER item on
# local hardware (seconds each), so a batch needs far more headroom than
# the single-insert create_task path. Used by submit_candidate_action_items,
# which also issues a SINGLE attempt (no expensive retry on timeout).
TRIAGE_INGEST_TIMEOUT_SECONDS = 240.0

DEFAULT_MAX_RETRIES = 3
DEFAULT_INITIAL_BACKOFF_SECONDS = 0.25
DEFAULT_BACKOFF_MULTIPLIER = 2.0

# Bound on how far we page the projects list when resolving a project
# number -> UUID. 100/page * 50 pages = 5000 projects; far beyond any
# real org and a hard stop against an accidental full-table walk.
_RESOLVE_PAGE_SIZE = 100
_RESOLVE_MAX_PAGES = 50

# Re-mint a token this many seconds BEFORE its KC-declared exp, so an
# in-flight request never goes out with an already-expired bearer.
_TOKEN_REFRESH_SAFETY_SECONDS = 30.0

# Fallback exp for the cached token when we can't decode the JWT (or it
# has no exp). Conservative — re-mint frequently rather than carry a
# token of unknown freshness.
_TOKEN_FALLBACK_LIFETIME_SECONDS = 60.0

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class ProjectOpsClientError(Exception):
    """Terminal failure talking to Project-Ops. Raised by the write
    methods; caught + swallowed by ``projectops_writer``."""


def _env(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v.strip() if v and v.strip() else None


def _base_url() -> str:
    # The bigboy compose historically exposed PROJECTOPS_API_BASE_URL while
    # this client read PROJECTOPS_BASE_URL. Accept both so operator overrides
    # reach the write client instead of silently falling back.
    return (
        _env("PROJECTOPS_BASE_URL")
        or _env("PROJECTOPS_API_BASE_URL")
        or DEFAULT_BASE_URL
    ).rstrip("/")


def _looks_like_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value.strip()))


def _normalize_due_date(due_date: Any) -> Optional[str]:
    """Project-Ops' dueDate is validated by class-validator's IsDateString,
    which accepts any ISO-8601 string. Accept a ``datetime`` (the
    ActionItem.due_date column type) or an already-formatted string."""
    if due_date is None:
        return None
    if isinstance(due_date, datetime):
        return due_date.isoformat()
    text = str(due_date).strip()
    return text or None


def _decode_jwt_exp(token: str) -> Optional[float]:
    """Best-effort extract of the ``exp`` claim from a JWT. Returns the
    epoch-seconds float, or None if the token is malformed / has no
    exp claim. We deliberately do NOT verify the signature — this is a
    self-issued cache-eviction hint, not an auth check."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload + padding)
        data = json.loads(raw.decode("utf-8"))
        exp = data.get("exp")
        return float(exp) if exp is not None else None
    except Exception:  # noqa: BLE001
        return None


# Set PROJECTOPS_EXPECTED_SCOPE to the scope/role the Project-Ops write
# endpoints require (PO's RolesGuard wants ADMIN|MANAGER|STAFF). When set,
# the client logs a one-time WARNING if a minted/static token doesn't carry
# it — an early, specific signal for a misconfigured KC client scope, which
# otherwise only shows up as a 403 deep in _request_with_retries. Log-only;
# never blocks the request. Unset == disabled (no-op).
_EXPECTED_SCOPE = _env("PROJECTOPS_EXPECTED_SCOPE")
_scope_warned = False


def _token_scopes(token: str) -> set[str]:
    """Best-effort set of scope/role strings carried by a JWT. NOT a
    signature check — diagnostic only. Reads the OAuth ``scope`` claim
    (space-delimited) plus Keycloak ``realm_access.roles`` and every
    ``resource_access.*.roles`` array."""
    out: set[str] = set()
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return out
        padding = "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(parts[1] + padding).decode("utf-8"))
        scope = data.get("scope")
        if isinstance(scope, str):
            out.update(s for s in scope.split() if s)
        elif isinstance(scope, list):
            out.update(str(s) for s in scope)
        ra = data.get("realm_access")
        if isinstance(ra, dict) and isinstance(ra.get("roles"), list):
            out.update(str(r) for r in ra["roles"])
        resa = data.get("resource_access")
        if isinstance(resa, dict):
            for client_block in resa.values():
                if isinstance(client_block, dict) and isinstance(client_block.get("roles"), list):
                    out.update(str(r) for r in client_block["roles"])
    except Exception:  # noqa: BLE001
        return out
    return out


def _warn_if_scope_missing(token: str) -> None:
    """Log ONCE per process if the token lacks PROJECTOPS_EXPECTED_SCOPE.
    No-op when the env var is unset. Never raises, never blocks."""
    global _scope_warned
    if not _EXPECTED_SCOPE or _scope_warned:
        return
    try:
        scopes = _token_scopes(token)
        if _EXPECTED_SCOPE not in scopes:
            _scope_warned = True
            logger.warning(
                "projectops_client: PO token is MISSING expected scope/role "
                "%r (token carries: %s). Project-Ops write calls will likely "
                "403 — check the Keycloak client's assigned scopes/roles.",
                _EXPECTED_SCOPE,
                sorted(scopes) or "<none decodable>",
            )
    except Exception:  # noqa: BLE001
        return


class _TokenSource:
    """Resolves bearer tokens for Project-Ops. Two modes, decided at
    construction time so a long-running client doesn't flip behavior
    mid-process when env vars are toggled.

    Auto-refresh mode owns an httpx client for the KC token endpoint
    independent of the main Project-Ops client (different host, often
    different TLS posture). The mint flow is guarded by an asyncio.Lock
    so a burst of concurrent requests after expiry mints exactly once.
    """

    def __init__(
        self,
        *,
        static_token: Optional[str],
        token_url: Optional[str],
        client_id: Optional[str],
        client_secret: Optional[str],
        timeout_seconds: float,
    ) -> None:
        # Auto-refresh wins when both client_id and client_secret are set,
        # and a token endpoint URL is available.
        self._mode_auto = bool(token_url and client_id and client_secret)
        self._static_token = static_token if not self._mode_auto else None
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout_seconds
        self._cached_token: Optional[str] = None
        # Epoch seconds at which the cache is considered stale. Initialize
        # in the past so the first call mints.
        self._cached_until: float = 0.0
        self._lock = asyncio.Lock()
        self._http: Optional[httpx.AsyncClient] = None

    @property
    def mode(self) -> str:
        return "auto-refresh" if self._mode_auto else (
            "static" if self._static_token else "unconfigured"
        )

    @property
    def is_configured(self) -> bool:
        return self._mode_auto or bool(self._static_token)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def get(self) -> str:
        if not self._mode_auto:
            if not self._static_token:
                raise ProjectOpsClientError(
                    "_TokenSource.get called while unconfigured"
                )
            return self._static_token

        now = time.time()
        if self._cached_token and now < self._cached_until:
            return self._cached_token

        async with self._lock:
            # Re-check after lock — a peer may have just refreshed.
            now = time.time()
            if self._cached_token and now < self._cached_until:
                return self._cached_token
            await self._mint_locked()
            assert self._cached_token is not None
            return self._cached_token

    async def _mint_locked(self) -> None:
        """Hit KC's ``client_credentials`` token endpoint and cache the
        result. Caller MUST hold ``self._lock``. Raises
        ``ProjectOpsClientError`` on failure."""
        if self._http is None:
            from middleware.request_context import outbound_request_headers
            self._http = httpx.AsyncClient(timeout=self._timeout, headers=outbound_request_headers())
        assert self._token_url and self._client_id and self._client_secret
        try:
            resp = await self._http.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                },
                headers={"Accept": "application/json"},
            )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
            raise ProjectOpsClientError(
                f"KC token mint network error: {type(exc).__name__}: {exc}"
            )
        if resp.status_code != 200:
            raise ProjectOpsClientError(
                f"KC token mint HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise ProjectOpsClientError(
                f"KC token mint non-JSON 200: {exc}"
            )
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not token:
            raise ProjectOpsClientError(
                f"KC token mint: no access_token in response: {str(payload)[:200]}"
            )

        # Trust the JWT's own exp claim over the response's expires_in,
        # falling back through expires_in -> conservative default. The
        # safety buffer makes the cache horizon strictly earlier than
        # the token's actual exp, so an in-flight request never goes out
        # with an already-expired bearer.
        exp_epoch = _decode_jwt_exp(token)
        if exp_epoch is None:
            expires_in = payload.get("expires_in") if isinstance(payload, dict) else None
            try:
                lifetime = float(expires_in) if expires_in is not None else _TOKEN_FALLBACK_LIFETIME_SECONDS
            except (TypeError, ValueError):
                lifetime = _TOKEN_FALLBACK_LIFETIME_SECONDS
            exp_epoch = time.time() + lifetime

        self._cached_token = token
        self._cached_until = max(
            time.time() + 1.0,
            exp_epoch - _TOKEN_REFRESH_SAFETY_SECONDS,
        )


class ProjectOpsClient:
    """Thin httpx wrapper for Project-Ops' task write endpoints.

    Intended to be short-lived per write batch (the writer creates one
    per session). A per-instance cache memoizes project-number -> UUID
    resolution so a batch of N action items on the same project only
    pages the projects list once.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        initial_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
        backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
        _client: Optional[httpx.AsyncClient] = None,
        token_url: Optional[str] = None,
        kc_client_id: Optional[str] = None,
        kc_client_secret: Optional[str] = None,
    ) -> None:
        self._base_url = (base_url or _base_url()).rstrip("/")
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._initial_backoff = initial_backoff_seconds
        self._backoff_multiplier = backoff_multiplier
        self._injected_client = _client
        self._owned_client: Optional[httpx.AsyncClient] = None
        # project_number (or raw uuid) -> resolved project UUID.
        self._project_id_cache: dict[str, str] = {}

        # Token source: kwargs win over env (test ergonomics + dependency
        # injection); env-resolution at __init__ keeps tests' monkeypatch
        # cycle predictable.
        self._token_source = _TokenSource(
            static_token=(
                api_key if api_key is not None else _env("PROJECTOPS_API_KEY")
            ),
            token_url=(
                token_url if token_url is not None else _env("PROJECTOPS_KC_TOKEN_URL")
            ),
            client_id=(
                kc_client_id if kc_client_id is not None
                else _env("PROJECTOPS_KC_CLIENT_ID")
            ),
            client_secret=(
                kc_client_secret if kc_client_secret is not None
                else _env("PROJECTOPS_KC_CLIENT_SECRET")
            ),
            timeout_seconds=timeout_seconds,
        )

    @property
    def is_live(self) -> bool:
        """True when at least one auth mode is configured. False puts the
        client in log-only mode (write methods become no-ops); the writer
        checks this and reports mode='no-op' without making HTTP calls."""
        return self._token_source.is_configured

    @property
    def auth_mode(self) -> str:
        """``auto-refresh`` | ``static`` | ``unconfigured`` — useful for
        diagnostics + the status endpoint."""
        return self._token_source.mode

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None
        await self._token_source.aclose()

    async def __aenter__(self) -> "ProjectOpsClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Public surface (writer-facing)
    # ------------------------------------------------------------------

    async def create_task(
        self,
        *,
        project_number: str,
        title: str,
        description: Optional[str] = None,
        priority: str = "MEDIUM",
        due_date: Any = None,
        type: str = "OTHER",
    ) -> dict[str, Any]:
        """Create one Project-Ops task. ``project_number`` may be a human
        number ("P-00055") — resolved to a UUID via the projects list — or
        a raw project UUID, used directly.

        Returns ``{"id": <task uuid>, "project_number": <projectNumber>}``.
        Raises ``ProjectOpsClientError`` on terminal failure. Callers are
        expected to gate on ``is_live`` first; if not live this raises
        rather than silently dropping the write.
        """
        if not self.is_live:
            raise ProjectOpsClientError(
                "create_task called while not configured "
                "(PROJECTOPS_API_KEY or KC credentials unset)"
            )

        project_id = await self._resolve_project_id(project_number)
        body: dict[str, Any] = {
            "projectId": project_id,
            "title": title,
            "type": type,
            "priority": priority,
        }
        if description:
            body["description"] = description
        normalized_due = _normalize_due_date(due_date)
        if normalized_due:
            body["dueDate"] = normalized_due

        data = await self._request_with_retries(
            "POST", "/tasks", json=body
        )
        if not isinstance(data, dict) or "id" not in data:
            raise ProjectOpsClientError(
                f"create_task: unexpected response shape: {str(data)[:200]}"
            )
        project_number_out = None
        project_block = data.get("project")
        if isinstance(project_block, dict):
            project_number_out = project_block.get("projectNumber")
        return {"id": data["id"], "project_number": project_number_out}

    async def update_task_status(
        self, *, task_id: str, status: str
    ) -> dict[str, Any]:
        """Legacy explicit task-status client. The action-item lifecycle never
        calls this: Project-Ops owns its Task status, and Meeting-Ops local
        checkbox changes must not imply Project-Ops completion."""
        if not self.is_live:
            raise ProjectOpsClientError(
                "update_task_status called while not configured"
            )
        data = await self._request_with_retries(
            "PATCH", f"/tasks/{task_id}/status", json={"status": status}
        )
        return data if isinstance(data, dict) else {}

    async def get_source_lifecycle(
        self,
        source_action_item_ids: list[str],
        *,
        bearer_override: str,
    ) -> dict[str, Any]:
        """Read proposal/task lifecycle records for up to 100 Meeting-Ops ids.

        The workspace-bound federation bearer is mandatory; there is no
        default-tenant fallback. The Project-Ops response contains ids/statuses
        only, never meeting or action-item text.
        """
        if any(not isinstance(value, str) for value in source_action_item_ids):
            raise ProjectOpsClientError(
                "source lifecycle action-item ids must be strings"
            )
        ids = list(dict.fromkeys(value.strip() for value in source_action_item_ids))
        ids = [value for value in ids if value]
        if len(ids) > 100:
            raise ProjectOpsClientError(
                "source lifecycle reconciliation is limited to 100 items"
            )
        if (
            not isinstance(bearer_override, str)
            or not bearer_override.strip()
        ):
            raise ProjectOpsClientError(
                "source lifecycle requires a workspace-bound federation token"
            )
        bearer = bearer_override.strip()
        data = await self._request_with_retries(
            "POST",
            "/triage/source-lifecycle",
            json={"sourceActionItemIds": ids},
            bearer_override=bearer,
        )
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ProjectOpsClientError(
                "source lifecycle returned an unexpected response shape"
            )
        if data.get("version") != SOURCE_LIFECYCLE_VERSION:
            raise ProjectOpsClientError(
                "source lifecycle returned an unsupported contract version"
            )
        return data

    async def submit_candidate_action_items(
        self,
        items: list[dict[str, Any]],
        *,
        source_type: str = "MEETING_OPS",
        timeout_seconds: Optional[float] = None,
        bearer_override: Optional[str] = None,
    ) -> dict[str, Any]:
        """Submit a batch of candidate action items to Project-Ops' TRIAGE
        agent (``POST /triage/ingest``). Propose-only: the triage agent
        routes + dedups each item and writes a TriageProposal for human
        review — NO task is created until a human approves in Project-Ops.

        Unlike ``create_task`` this needs NO target project: triage resolves
        routing itself (or files the item in the "needs a project" inbox).
        Idempotent on the PO side via ``(sourceType, sourceActionItemId)``,
        so re-submitting the same items is safe (they return as ``skipped``).

        ``items`` is a list of ``{text, owner?, sourceActionItemId?,
        sourceRef?}``. Returns the ingest summary
        ``{created, skipped, proposals}``.

        Timeout/retry: a batch triggers one embed + rerank + LLM pass per
        item on local GPUs, so this uses a generous per-call timeout
        (``TRIAGE_INGEST_TIMEOUT_SECONDS``) and a SINGLE attempt — re-issuing
        the whole expensive batch on a timeout would be wasteful, and the
        writer's eventual-consistency posture (don't stamp on failure,
        re-submit next finalize/backfill, PO dedups) already covers a
        dropped response. Raises ``ProjectOpsClientError`` on terminal
        failure.

        ``bearer_override`` (optional) forces a specific bearer for this push
        — used to send a Brigade-exchanged, workspace-bound federation token
        (``aud=project-ops``) so PO routes the proposals to the correct
        tenant. A non-empty override is sufficient authentication even when
        the legacy PROJECTOPS_API_KEY / PROJECTOPS_KC_* source is unconfigured.
        When None, the configured legacy token source is used.
        """
        override = (
            bearer_override.strip()
            if isinstance(bearer_override, str) and bearer_override.strip()
            else None
        )
        if not self.is_live and override is None:
            raise ProjectOpsClientError(
                "submit_candidate_action_items called while not configured "
                "(no bearer_override, PROJECTOPS_API_KEY, or KC credentials)"
            )
        if not items:
            return {"created": 0, "skipped": 0, "proposals": []}
        body: dict[str, Any] = {"items": items, "sourceType": source_type}
        data = await self._request_with_retries(
            "POST",
            "/triage/ingest",
            json=body,
            timeout=timeout_seconds or TRIAGE_INGEST_TIMEOUT_SECONDS,
            max_attempts=1,
            bearer_override=override,
        )
        if not isinstance(data, dict):
            return {"created": 0, "skipped": 0, "proposals": []}
        return data

    async def resolve_project_id(self, project_number: str) -> str:
        """Public passthrough to the number->UUID resolver (handy for
        scripts / diagnostics)."""
        return await self._resolve_project_id(project_number)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _resolve_project_id(self, project_ref: str) -> str:
        """Resolve a project number ("P-00055") to its UUID. If
        ``project_ref`` already looks like a UUID it is returned as-is
        (the session->PO link stores the UUID directly). Result is cached
        per client instance.

        Project-Ops exposes no by-number route, so we page the list
        endpoint and match ``projectNumber`` client-side. Raises
        ``ProjectOpsClientError`` if not found within the page bound.
        """
        ref = (project_ref or "").strip()
        if not ref:
            raise ProjectOpsClientError("empty project reference")
        if _looks_like_uuid(ref):
            return ref
        if ref in self._project_id_cache:
            return self._project_id_cache[ref]

        _seen_project_ids: set[str] = set()
        for page in range(1, _RESOLVE_MAX_PAGES + 1):
            data = await self._request_with_retries(
                "GET",
                "/projects",
                params={"page": page, "limit": _RESOLVE_PAGE_SIZE},
            )
            rows = data.get("data") if isinstance(data, dict) else data
            if not isinstance(rows, list) or not rows:
                break
            _new_on_page = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if row.get("projectNumber") == ref and row.get("id"):
                    self._project_id_cache[ref] = row["id"]
                    return row["id"]
                _rid = row.get("id")
                if isinstance(_rid, str) and _rid not in _seen_project_ids:
                    _seen_project_ids.add(_rid)
                    _new_on_page += 1
            # No-progress circuit breaker: a full page that contributed zero
            # new project ids means the API is looping (or ignoring `page`);
            # stop rather than walk the full page cap on a degraded backend.
            if _new_on_page == 0:
                logger.warning(
                    "projectops_client _resolve_project_id no-progress at "
                    "page=%d (0 new ids) resolving %r — stopping early",
                    page,
                    ref,
                )
                break
            # Stop once we've walked the last page the API reports.
            meta = data.get("meta") if isinstance(data, dict) else None
            if isinstance(meta, dict):
                total_pages = meta.get("totalPages")
                if isinstance(total_pages, int) and page >= total_pages:
                    break
            if len(rows) < _RESOLVE_PAGE_SIZE:
                break

        raise ProjectOpsClientError(
            f"project number {ref!r} not found in Project-Ops "
            f"(searched up to {_RESOLVE_MAX_PAGES} pages)"
        )

    async def _headers(self, bearer_override: Optional[str] = None) -> dict[str, str]:
        """Build request headers. When ``bearer_override`` is supplied (e.g. a
        Brigade-exchanged, workspace-bound federation token for the triage
        push), it is used as the bearer INSTEAD of the default token source;
        otherwise the configured ``_token_source`` mints/caches as usual."""
        token = bearer_override if bearer_override else await self._token_source.get()
        _warn_if_scope_missing(token)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _client_ctx(self) -> "_AsyncClientCtx":
        if self._injected_client is not None:
            return _AsyncClientCtx(self._injected_client, owns=False)
        if self._owned_client is None:
            from middleware.request_context import outbound_request_headers
            self._owned_client = httpx.AsyncClient(timeout=self._timeout, headers=outbound_request_headers())
        return _AsyncClientCtx(self._owned_client, owns=False)

    async def _request_with_retries(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        max_attempts: Optional[int] = None,
        bearer_override: Optional[str] = None,
    ) -> Any:
        """Issue an HTTP request with bounded retries. Retries on
        connect/timeout/5xx; 4xx is terminal (the payload won't get
        better). Returns parsed JSON on 2xx; raises ProjectOpsClientError
        on terminal failure or after exhausting retries.

        ``timeout`` overrides the per-request read timeout for this call
        (the triage-ingest path needs far more than the 8s default).
        ``max_attempts`` overrides the retry count for this call — pass 1
        for an expensive, idempotent op that must not be re-issued on a
        timeout.
        ``bearer_override`` forces a specific bearer (e.g. a Brigade-exchanged
        federation token) instead of the default token source for this call."""
        url = f"{self._base_url}{path}"
        backoff = self._initial_backoff
        last_detail: Optional[str] = None
        attempts = max_attempts if max_attempts is not None else self._max_retries
        for attempt in range(attempts):
            try:
                headers = await self._headers(bearer_override)
                async with self._client_ctx() as client:
                    resp = await client.request(
                        method,
                        url,
                        json=json,
                        params=params,
                        headers=headers,
                        timeout=timeout if timeout is not None else self._timeout,
                    )
                if resp.status_code < 400:
                    if not resp.content:
                        return {}
                    try:
                        return resp.json()
                    except Exception as exc:  # noqa: BLE001
                        raise ProjectOpsClientError(
                            f"{method} {path}: non-JSON 2xx body: {exc}"
                        )
                if 400 <= resp.status_code < 500:
                    logger.warning(
                        "projectops_client 4xx %s %s %s",
                        resp.status_code,
                        method,
                        path,
                    )
                    raise ProjectOpsClientError(
                        f"HTTP {resp.status_code} from {method} {path}"
                    )
                last_detail = f"HTTP {resp.status_code} from {method} {path}"
            except ProjectOpsClientError:
                raise
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.ConnectTimeout,
                httpx.RemoteProtocolError,
            ) as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001
                last_detail = f"{type(exc).__name__}: {exc}"
                logger.debug(
                    "projectops_client unexpected exc on attempt %d: %s",
                    attempt,
                    exc,
                )
            if attempt < attempts - 1:
                await asyncio.sleep(backoff)
                backoff *= self._backoff_multiplier
        logger.warning(
            "projectops_client request failed after %d attempts %s %s detail=%s",
            attempts,
            method,
            path,
            last_detail,
        )
        raise ProjectOpsClientError(last_detail or "request failed")


class _AsyncClientCtx:
    """Tiny shim so ``async with`` works for both owned and injected
    httpx clients without closing the injected one."""

    def __init__(self, client: httpx.AsyncClient, *, owns: bool) -> None:
        self._client = client
        self._owns = owns

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns:
            await self._client.aclose()


__all__ = [
    "ProjectOpsClient",
    "ProjectOpsClientError",
    "DEFAULT_BASE_URL",
    "TRIAGE_INGEST_TIMEOUT_SECONDS",
]
