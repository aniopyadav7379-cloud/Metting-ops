"""Authenticated proxy for Contact-Ops person search.

Used by the Meeting-Ops participant editor to manually stamp a
Contact-Ops person id onto a meeting participant. The outbound call uses
services.contact_ops_resolver, which performs the Meeting-Ops -> Brigade ->
Contact-Ops token exchange and binds the Contact-Ops request to the active
organization's federation workspace_id.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from services import contact_ops_resolver

router = APIRouter(prefix="/api/contact-ops", tags=["contact-ops"])


class ContactOpsPersonOut(BaseModel):
    person_id: str
    display_name: str
    email: Optional[str] = None
    match_confidence: float
    match_basis: str


class ContactOpsPeopleSearchOut(BaseModel):
    items: list[ContactOpsPersonOut]
    ambiguous: bool


@router.get("/people/search", response_model=ContactOpsPeopleSearchOut)
async def search_people(
    q: str = Query(..., min_length=2, max_length=120),
    limit: int = Query(default=10, ge=1, le=25),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    workspace_id = (getattr(active_org.organization, "workspace_id", None) or "").strip()
    if not workspace_id:
        raise HTTPException(
            status_code=400,
            detail="Active organization is not mapped to a federation workspace",
        )
    people = await contact_ops_resolver.search_people(q, workspace_id, limit=limit)
    top = people[0].get("match_confidence") if people else None
    ambiguous = bool(
        top is not None
        and sum(1 for person in people if person.get("match_confidence") == top) > 1
    )
    return {"items": people, "ambiguous": ambiguous}
