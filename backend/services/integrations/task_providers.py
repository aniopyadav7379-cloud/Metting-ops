"""Task/knowledge-export provider clients (Phase 3G).

Each function takes a resolved services.integrations.org_config.IntegrationConfig
and the action item's fields, makes ONE real API call against that
provider's documented contract, and returns a small ref dict on success or
raises ProviderActionError on failure. No credentials are fabricated; a
missing/invalid config raises ProviderNotConfigured, matching the pattern
already established for Lyzr (LyzrNotConfigured) and Google Calendar
(GoogleCalendarNotConfigured) in earlier phases.
"""
from __future__ import annotations

import base64
import logging
from typing import Optional

import httpx

from services.integrations.org_config import IntegrationConfig

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class ProviderNotConfigured(RuntimeError):
    pass


class ProviderActionError(RuntimeError):
    pass


def _require(config: IntegrationConfig, provider: str) -> None:
    if not config.enabled or not config.api_key or not config.api_base_url:
        raise ProviderNotConfigured(f"{provider} is not configured (enable it and set base URL + API key in Integrations settings)")


async def create_jira_issue(config: IntegrationConfig, *, project_key: str, summary: str, description: str = "", assignee: Optional[str] = None) -> dict:
    """POST /rest/api/3/issue. api_key is stored as 'email:api_token'
    (Atlassian Basic auth convention - see api/integrations_org.py)."""
    _require(config, "Jira")
    if not project_key:
        raise ProviderActionError("Jira requires a project_key")
    auth_header = base64.b64encode(config.api_key.encode()).decode()
    body = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary[:255],
            "description": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": description or summary}]}]},
            "issuetype": {"name": "Task"},
        }
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{config.api_base_url.rstrip('/')}/rest/api/3/issue",
            json=body,
            headers={"Authorization": f"Basic {auth_header}", "Content-Type": "application/json"},
        )
    if resp.status_code >= 400:
        raise ProviderActionError(f"Jira issue creation failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    return {"provider": "jira", "id": data.get("id"), "key": data.get("key"), "url": f"{config.api_base_url.rstrip('/')}/browse/{data.get('key')}"}


async def create_todoist_task(config: IntegrationConfig, *, content: str, due_string: Optional[str] = None, priority: int = 1) -> dict:
    """POST https://api.todoist.com/rest/v2/tasks."""
    _require(config, "Todoist")
    body = {"content": content[:500]}
    if due_string:
        body["due_string"] = due_string
    if priority:
        body["priority"] = max(1, min(4, priority))
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{config.api_base_url.rstrip('/')}/rest/v2/tasks",
            json=body,
            headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
        )
    if resp.status_code >= 400:
        raise ProviderActionError(f"Todoist task creation failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    return {"provider": "todoist", "id": data.get("id"), "url": data.get("url")}


async def create_clickup_task(config: IntegrationConfig, *, list_id: str, name: str, description: str = "", assignees: Optional[list] = None, due_date_ms: Optional[int] = None) -> dict:
    """POST https://api.clickup.com/api/v2/list/{list_id}/task. ClickUp
    sends its personal token as-is (no 'Bearer ' prefix)."""
    _require(config, "ClickUp")
    if not list_id:
        raise ProviderActionError("ClickUp requires a list_id")
    body: dict = {"name": name[:500], "description": description or name}
    if assignees:
        body["assignees"] = assignees
    if due_date_ms:
        body["due_date"] = due_date_ms
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{config.api_base_url.rstrip('/')}/api/v2/list/{list_id}/task",
            json=body,
            headers={"Authorization": config.api_key, "Content-Type": "application/json"},
        )
    if resp.status_code >= 400:
        raise ProviderActionError(f"ClickUp task creation failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    return {"provider": "clickup", "id": data.get("id"), "url": data.get("url")}


async def create_notion_page(config: IntegrationConfig, *, database_id: str, title: str, content: str = "") -> dict:
    """POST https://api.notion.com/v1/pages. Notion internal-integration
    tokens are plain Bearer tokens (no OAuth flow needed for this mode)."""
    _require(config, "Notion")
    if not database_id:
        raise ProviderActionError("Notion requires a database_id")
    body = {
        "parent": {"database_id": database_id},
        "properties": {"Name": {"title": [{"text": {"content": title[:200]}}]}},
        "children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": content[:2000] or title}}]}}] if content else [],
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{config.api_base_url.rstrip('/')}/v1/pages",
            json=body,
            headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json", "Notion-Version": "2022-06-28"},
        )
    if resp.status_code >= 400:
        raise ProviderActionError(f"Notion page creation failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    return {"provider": "notion", "id": data.get("id"), "url": data.get("url")}


async def create_confluence_page(config: IntegrationConfig, *, space_key: str, title: str, body_html: str) -> dict:
    """POST /wiki/rest/api/content. Same Basic-auth convention as Jira
    (Atlassian Cloud, sibling product)."""
    _require(config, "Confluence")
    if not space_key:
        raise ProviderActionError("Confluence requires a space_key")
    auth_header = base64.b64encode(config.api_key.encode()).decode()
    body = {
        "type": "page",
        "title": title[:200],
        "space": {"key": space_key},
        "body": {"storage": {"value": body_html, "representation": "storage"}},
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{config.api_base_url.rstrip('/')}/wiki/rest/api/content",
            json=body,
            headers={"Authorization": f"Basic {auth_header}", "Content-Type": "application/json"},
        )
    if resp.status_code >= 400:
        raise ProviderActionError(f"Confluence page creation failed ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    return {"provider": "confluence", "id": data.get("id"), "url": (data.get("_links") or {}).get("webui")}
