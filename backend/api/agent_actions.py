"""HTTP API for Meeting-Ops agent write actions."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from database.database import get_db
from services.agent_actions import cancel_action, confirm_action, propose_action

router = APIRouter(prefix="/api/agent-actions", tags=["agent-actions"])


class AgentActionProposeRequest(BaseModel):
    action: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentActionConfirmRequest(BaseModel):
    confirmation_token: str = Field(..., min_length=8)
    # Optional extra friction for destructive actions (e.g. delete_session):
    # the proposal returns `required_typed_confirmation`, the user types it
    # here, the applier validates the exact match before mutating.
    typed_confirmation: Optional[str] = None


class AgentActionCancelRequest(BaseModel):
    confirmation_token: str = Field(..., min_length=8)


def _request_context(request: Request) -> dict[str, Any]:
    client_host = request.client.host if request.client else None
    return {
        "ip_address": client_host,
        "user_agent": request.headers.get("user-agent"),
    }


@router.post("/propose")
async def propose(
    body: AgentActionProposeRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    return await propose_action(
        db=db,
        user=current_user,
        org_id=active_org.organization.id,
        action=body.action,
        payload=body.payload,
        request_context=_request_context(request),
    )


@router.post("/confirm")
async def confirm(
    body: AgentActionConfirmRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    return await confirm_action(
        db=db,
        user=current_user,
        org_id=active_org.organization.id,
        confirmation_token=body.confirmation_token,
        typed_confirmation=body.typed_confirmation,
        request_context=_request_context(request),
        background_tasks=background_tasks,
    )


@router.post("/cancel")
async def cancel(
    body: AgentActionCancelRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    return await cancel_action(
        db=db,
        user=current_user,
        org_id=active_org.organization.id,
        confirmation_token=body.confirmation_token,
        request_context=_request_context(request),
    )
