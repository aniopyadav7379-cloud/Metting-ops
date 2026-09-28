"""Project-Ops HTTP client.

Server-to-server caller for the cross-app project linking feature. Auth is
the user's realm JWT forwarded from the meeting-ops request — project-ops
verifies it against the same Keycloak realm (uchub) via the dual-token
strategy added in project-ops 215fc50, JIT-provisions a User row if needed,
and applies the same RBAC the browser flow does.

Per-user RBAC is honored end-to-end: meeting-ops never holds a service
account credential; each cross-app call carries the actual user's identity.

Failure mode: every method swallows network errors and returns an empty
result. Project-ops being down should NOT break meeting-ops; the picker
just shows "no projects available".
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable, Optional

import httpx

logger = logging.getLogger(__name__)

DEFAULT_PROJECT_OPS_URL = "http://project-ops-backend:3201/api/v1"
DEFAULT_TIMEOUT_SECONDS = 5.0


def _base_url() -> str:
    return (
        os.getenv("PROJECT_OPS_URL")
        or os.getenv("PROJECTOPS_BASE_URL")
        or os.getenv("PROJECTOPS_API_BASE_URL")
        or DEFAULT_PROJECT_OPS_URL
    ).rstrip("/")


def _timeout() -> float:
    try:
        return float(os.getenv("PROJECT_OPS_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS


class ProjectOpsClient:
    """Async HTTP client for project-ops cross-app calls.

    Construct per-request with the caller's realm JWT — never cache an
    instance across users. The class is intentionally small and stateless;
    subclassing is easier than mocking out a singleton in tests.
    """

    def __init__(self, bearer_token: Optional[str], *, base_url: Optional[str] = None) -> None:
        self.bearer_token = bearer_token
        self.base_url = (base_url or _base_url()).rstrip("/")

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.bearer_token:
            h["Authorization"] = f"Bearer {self.bearer_token}"
        return h

    async def list_projects(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return the active user's projects (per project-ops RBAC).

        Project-ops paginates: the response is `{ data: [...], pagination: {...} }`
        for some routes and a bare list for others. We accept both shapes.
        """
        if not self.bearer_token:
            logger.debug("project-ops: no bearer token, returning empty projects")
            return []
        url = f"{self.base_url}/projects"
        try:
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                resp = await client.get(
                    url,
                    headers=self._headers(),
                    params={"limit": str(limit)},
                )
        except httpx.HTTPError as exc:
            logger.warning(f"project-ops list_projects network error: {exc}")
            return []

        if resp.status_code == 401:
            # Decode without verifying so we can log claims that might
            # explain the rejection (iss, aud, exp, alg, kid). Never logs
            # the signature or the full token; these claims are non-secret
            # and diagnostic for cross-app federation issues.
            try:
                import base64 as _b64
                import json as _json
                parts = (self.bearer_token or "").split(".")
                if len(parts) >= 2:
                    def _pad(s: str) -> str:
                        return s + "=" * (-len(s) % 4)
                    header = _json.loads(_b64.urlsafe_b64decode(_pad(parts[0])))
                    payload = _json.loads(_b64.urlsafe_b64decode(_pad(parts[1])))
                    logger.info(
                        "project-ops list_projects rejected token (401) "
                        "alg=%s kid=%s iss=%s aud=%s typ=%s exp=%s email_present=%s preferred_username=%s",
                        header.get("alg"),
                        (header.get("kid") or "")[:8] + "...",
                        payload.get("iss"),
                        payload.get("aud"),
                        payload.get("typ"),
                        payload.get("exp"),
                        bool(payload.get("email")),
                        payload.get("preferred_username"),
                    )
                else:
                    logger.info("project-ops list_projects rejected token (401) - token unparseable")
            except Exception as exc:
                logger.info(f"project-ops list_projects rejected token (401) - decode failed: {exc}")
            return []
        if resp.status_code >= 400:
            logger.warning(
                f"project-ops list_projects HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return []

        try:
            payload = resp.json()
        except ValueError:
            logger.warning("project-ops list_projects non-JSON response")
            return []

        projects = _extract_list(payload, ("projects", "data", "items"))
        return [_normalize_project(p) for p in projects if isinstance(p, dict)]


def _extract_list(payload: Any, candidate_keys: Iterable[str]) -> list[Any]:
    """Pull a list out of one of several response shapes."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in candidate_keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def _normalize_project(raw: dict[str, Any]) -> dict[str, Any]:
    """Map the project-ops Project shape onto the meeting-ops ProjectResponse.

    Project-ops fields used:
      - id (UUID string)               -> id
      - title                          -> name
      - projectNumber (PROJ-2026-...)  -> slug fallback
      - clientName, status, etc.       -> passed through for richer UX
    """
    project_id = str(raw.get("id") or "")
    title = raw.get("title") or raw.get("name") or "Untitled project"
    project_number = raw.get("projectNumber") or ""
    return {
        "id": project_id,
        "name": title,
        "slug": project_number or project_id,
        "app": "project-ops",
        "project_number": project_number,
        "status": raw.get("status"),
        "priority": raw.get("priority"),
        "client_name": raw.get("clientName"),
        "due_date": raw.get("dueDate"),
    }
