"""
Provider Settings API — per-org provider configuration.
Only org admins (or superusers) can edit.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import Optional

from database.database import get_db
from database.models import OrgProviderSettings
from auth.dependencies import get_current_organization, get_current_user, require_admin
from auth.organization import ActiveOrganization
from auth.models import User
from services.providers.crypto import encrypt_api_key, decrypt_api_key

router = APIRouter(prefix="/api/providers", tags=["Provider Settings"])

SERVICE_KINDS = ("llm", "stt", "tts", "embeddings", "reranking", "diarization")
VALID_PROVIDERS = {
    "llm": ("litellm", "openai", "anthropic", "openrouter", "lambda", "bedrock", "lyzr"),
    # parakeet      = NVIDIA NeMo Parakeet-TDT on :8881 (word-level timestamps).
    #                 Default for new orgs as of 2026-05-19 when the legacy
    #                 meet-whisper container was retired.
    # local_whisper = whisper.cpp HTTP server (no longer shipped by default;
    #                 kept as a valid choice so orgs that bring their own
    #                 whisper endpoint can still select it).
    "stt": ("parakeet", "local_whisper", "deepgram", "assemblyai", "openai"),
    "tts": ("kokoro", "vibevoice", "openai"),
    "embeddings": ("infinity", "openai", "voyage", "cohere"),
    "reranking": ("infinity", "cohere", "voyage"),
    # "off" = skip diarization for every new session in this org. The
    # PipelineStatusPicker "Save as default" button promotes the per-session
    # toggle here; backend chunk handlers read it via _org_diarization_off
    # and skip the pyannote/speaker-svc call.
    "diarization": ("local_diarization", "off", "deepgram", "assemblyai"),
}


class ProviderSettingsResponse(BaseModel):
    id: int
    organization_id: int
    service_kind: str
    provider_name: str
    endpoint_url: Optional[str] = None
    has_api_key: bool = False
    model_name: Optional[str] = None
    overrides: Optional[dict] = None


class ProviderSettingsUpdate(BaseModel):
    provider_name: str
    endpoint_url: Optional[str] = None
    api_key: Optional[str] = None  # Plaintext in, encrypted at rest
    model_name: Optional[str] = None
    overrides: Optional[dict] = Field(default_factory=dict)


def _mask_settings(row: OrgProviderSettings) -> ProviderSettingsResponse:
    return ProviderSettingsResponse(
        id=row.id,
        organization_id=row.organization_id,
        service_kind=row.service_kind,
        provider_name=row.provider_name,
        endpoint_url=row.endpoint_url,
        has_api_key=bool(row.api_key_encrypted),
        model_name=row.model_name,
        overrides=row.overrides,
    )


@router.get("", response_model=list[ProviderSettingsResponse])
async def list_provider_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    rows = (
        db.query(OrgProviderSettings)
        .filter(OrgProviderSettings.organization_id == active_org.organization.id)
        .all()
    )
    existing = {r.service_kind: r for r in rows}
    defaults = []
    for kind in SERVICE_KINDS:
        if kind in existing:
            defaults.append(_mask_settings(existing[kind]))
        else:
            defaults.append(ProviderSettingsResponse(
                id=0,
                organization_id=active_org.organization.id,
                service_kind=kind,
                provider_name=VALID_PROVIDERS[kind][0],
                has_api_key=False,
            ))
    return defaults


@router.get("/{service_kind}", response_model=ProviderSettingsResponse)
async def get_provider_setting(
    service_kind: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    if service_kind not in SERVICE_KINDS:
        raise HTTPException(status_code=400, detail=f"Invalid service_kind. Must be one of: {', '.join(SERVICE_KINDS)}")
    row = (
        db.query(OrgProviderSettings)
        .filter(
            OrgProviderSettings.organization_id == active_org.organization.id,
            OrgProviderSettings.service_kind == service_kind,
        )
        .first()
    )
    if not row:
        return ProviderSettingsResponse(
            id=0,
            organization_id=active_org.organization.id,
            service_kind=service_kind,
            provider_name=VALID_PROVIDERS[service_kind][0],
            has_api_key=False,
        )
    return _mask_settings(row)


@router.put("/{service_kind}", response_model=ProviderSettingsResponse)
async def upsert_provider_setting(
    service_kind: str,
    settings: ProviderSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    if service_kind not in SERVICE_KINDS:
        raise HTTPException(status_code=400, detail=f"Invalid service_kind")
    if settings.provider_name not in VALID_PROVIDERS.get(service_kind, ()):
        raise HTTPException(status_code=400, detail=f"Invalid provider for {service_kind}")

    if active_org.role not in ("admin", "superuser") and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Only org admins can edit provider settings")

    row = (
        db.query(OrgProviderSettings)
        .filter(
            OrgProviderSettings.organization_id == active_org.organization.id,
            OrgProviderSettings.service_kind == service_kind,
        )
        .first()
    )

    if not row:
        row = OrgProviderSettings(
            organization_id=active_org.organization.id,
            service_kind=service_kind,
        )
        db.add(row)

    row.provider_name = settings.provider_name
    row.endpoint_url = settings.endpoint_url
    row.model_name = settings.model_name
    row.overrides = settings.overrides or {}

    if settings.api_key is not None and settings.api_key != "":
        row.api_key_encrypted = encrypt_api_key(settings.api_key)
    elif settings.api_key == "":
        row.api_key_encrypted = None

    db.commit()
    db.refresh(row)
    return _mask_settings(row)


@router.get("/available/{service_kind}")
async def get_available_providers(
    service_kind: str,
    current_user: User = Depends(get_current_user),
):
    if service_kind not in SERVICE_KINDS:
        raise HTTPException(status_code=400, detail=f"Invalid service_kind")
    return {"service_kind": service_kind, "providers": list(VALID_PROVIDERS.get(service_kind, []))}
