"""Action Item -> user confirmation -> external provider (Phase 3G).

Nothing here fires automatically on extraction - every push/notify call is
an explicit user-confirmed action, matching the same safe-default pattern
as api/google_calendar.py's create-event endpoint from Phase 3F.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from database.database import get_db
from database.models import ActionItem
from services.integrations import org_config
from services.integrations.notifiers import (
    ProviderActionError as NotifyError,
    ProviderNotConfigured as NotifyNotConfigured,
    send_email,
    send_slack_message,
    send_teams_message,
)
from services.integrations.task_providers import (
    ProviderActionError,
    ProviderNotConfigured,
    create_clickup_task,
    create_confluence_page,
    create_jira_issue,
    create_notion_page,
    create_todoist_task,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/action-items", tags=["Action Item Integrations"])

_TASK_RESOLVERS = {
    "jira": org_config.resolve_jira,
    "todoist": org_config.resolve_todoist,
    "clickup": org_config.resolve_clickup,
    "notion": org_config.resolve_notion,
    "confluence": org_config.resolve_confluence,
}
_NOTIFY_RESOLVERS = {
    "slack": org_config.resolve_slack,
    "teams": org_config.resolve_teams,
    "email": org_config.resolve_email,
}


class PushResult(BaseModel):
    ok: bool
    ref: Optional[dict] = None
    error: Optional[str] = None


class PushRequest(BaseModel):
    # Provider-specific target identifiers the user picks in the confirm
    # dialog (a Jira project key, a ClickUp list, a Notion database, a
    # Confluence space) - never guessed/defaulted server-side.
    project_key: Optional[str] = None
    list_id: Optional[str] = None
    database_id: Optional[str] = None
    space_key: Optional[str] = None


class NotifyRequest(BaseModel):
    to_address: Optional[str] = None  # required for email only


def _get_item(db: Session, org_id: int, action_item_id: int) -> ActionItem:
    item = db.query(ActionItem).filter(ActionItem.id == action_item_id, ActionItem.organization_id == org_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Action item not found")
    return item


def _record_ref(item: ActionItem, provider: str, ref: dict, db: Session) -> None:
    refs = dict(item.external_task_refs or {})
    refs[provider] = {**ref, "synced_at": datetime.now(timezone.utc).isoformat()}
    item.external_task_refs = refs
    db.commit()


@router.post("/{action_item_id}/push/{provider}", response_model=PushResult)
async def push_action_item(
    action_item_id: int,
    provider: str,
    request: PushRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    if provider not in _TASK_RESOLVERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider '{provider}'. Supported: {sorted(_TASK_RESOLVERS)}")
    org_id = active_org.organization.id
    item = _get_item(db, org_id, action_item_id)
    config = _TASK_RESOLVERS[provider](db, org_id)

    try:
        if provider == "jira":
            if not request.project_key:
                raise HTTPException(status_code=422, detail="project_key is required for Jira")
            ref = await create_jira_issue(config, project_key=request.project_key, summary=item.text or "Action item", description=f"Owner: {item.owner or 'unassigned'}")
        elif provider == "todoist":
            ref = await create_todoist_task(config, content=item.text or "Action item")
        elif provider == "clickup":
            if not request.list_id:
                raise HTTPException(status_code=422, detail="list_id is required for ClickUp")
            ref = await create_clickup_task(config, list_id=request.list_id, name=item.text or "Action item", description=f"Owner: {item.owner or 'unassigned'}")
        elif provider == "notion":
            if not request.database_id:
                raise HTTPException(status_code=422, detail="database_id is required for Notion")
            ref = await create_notion_page(config, database_id=request.database_id, title=item.text or "Action item", content=f"Owner: {item.owner or 'unassigned'}")
        elif provider == "confluence":
            if not request.space_key:
                raise HTTPException(status_code=422, detail="space_key is required for Confluence")
            ref = await create_confluence_page(config, space_key=request.space_key, title=(item.text or "Action item")[:200], body_html=f"<p>{item.text}</p><p>Owner: {item.owner or 'unassigned'}</p>")
        else:  # pragma: no cover - guarded above
            raise HTTPException(status_code=404, detail="Unknown provider")
    except ProviderNotConfigured as exc:
        return PushResult(ok=False, error=str(exc))
    except ProviderActionError as exc:
        return PushResult(ok=False, error=str(exc))

    _record_ref(item, provider, ref, db)
    return PushResult(ok=True, ref=ref)


@router.post("/{action_item_id}/notify/{provider}", response_model=PushResult)
async def notify_action_item(
    action_item_id: int,
    provider: str,
    request: NotifyRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    if provider not in _NOTIFY_RESOLVERS:
        raise HTTPException(status_code=404, detail=f"Unknown provider '{provider}'. Supported: {sorted(_NOTIFY_RESOLVERS)}")
    org_id = active_org.organization.id
    item = _get_item(db, org_id, action_item_id)
    config = _NOTIFY_RESOLVERS[provider](db, org_id)
    text = f"Action item: {item.text or '(no description)'} (owner: {item.owner or 'unassigned'})"

    try:
        if provider == "slack":
            ref = await send_slack_message(config, text=text)
        elif provider == "teams":
            ref = await send_teams_message(config, text=text, title="Meeting-Ops action item")
        elif provider == "email":
            if not request.to_address:
                raise HTTPException(status_code=422, detail="to_address is required for email")
            ref = await asyncio.to_thread(send_email, config, to_address=request.to_address, subject="Meeting-Ops action item", body=text)
        else:  # pragma: no cover - guarded above
            raise HTTPException(status_code=404, detail="Unknown provider")
    except NotifyNotConfigured as exc:
        return PushResult(ok=False, error=str(exc))
    except NotifyError as exc:
        return PushResult(ok=False, error=str(exc))

    _record_ref(item, provider, ref, db)
    return PushResult(ok=True, ref=ref)
