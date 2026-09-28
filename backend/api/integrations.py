"""Project Integration API — cross-app project linking.

Forwards the caller's Keycloak realm JWT to upstream apps (project-ops today,
crisis-ops next) so per-user RBAC is honored end-to-end. Project-ops accepts
realm JWTs via its dual-token JwtStrategy (project-ops 215fc50). Crisis-ops
will gain the same primitive in a follow-up; until then it returns empty.
"""
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from database.database import get_db
from services.integrations.project_ops_client import ProjectOpsClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/integrations", tags=["Integrations"])


class ProjectResponse(BaseModel):
    id: str
    name: str
    slug: str
    app: str
    project_number: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    client_name: Optional[str] = None
    due_date: Optional[str] = None


def _bearer_from_request(request: Request) -> Optional[str]:
    """Pull the Authorization header forwarded by oauth2-proxy.

    oauth2-proxy is configured with OAUTH2_PROXY_PASS_AUTHORIZATION_HEADER=true
    and OAUTH2_PROXY_SET_AUTHORIZATION_HEADER=true so the upstream backend
    receives the user's realm JWT verbatim. We pass that same token through
    to project-ops so RBAC follows the user, not a service account.
    """
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth.strip()


@router.get("/{app}/projects", response_model=list[ProjectResponse])
async def list_projects(
    app: str,
    organization_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    if app not in ("project-ops", "crisis-ops"):
        raise HTTPException(status_code=400, detail="App must be 'project-ops' or 'crisis-ops'")
    if organization_id != active_org.organization.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Cannot fetch projects for a different organization")

    if app == "project-ops":
        client = ProjectOpsClient(bearer_token=_bearer_from_request(request))
        rows = await client.list_projects()
        return [ProjectResponse(**r) for r in rows]

    # crisis-ops integration not yet wired (returns empty until that app
    # gains the dual-token JWT primitive matching project-ops).
    logger.debug("crisis-ops project listing not yet implemented; returning empty")
    return []
