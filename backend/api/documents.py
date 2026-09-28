"""Documents API (Phase 3 module K) — supporting PDF/DOCX/TXT/MD documents
as first-class searchable knowledge, indexed through the exact same Qdrant
service meetings and lectures use. See services/document_knowledge.py for
the audit finding that motivated this (extraction was missing; indexing
was not)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from database.database import get_db
from database.models import KnowledgeDocument
from services.document_knowledge import (
    DocumentTooLarge,
    SUPPORTED_EXTENSIONS,
    UnsupportedDocumentType,
    content_hash,
    delete_document_index,
    extract_text,
    index_document,
    new_doc_id,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["Documents"])


class DocumentOut(BaseModel):
    doc_id: str
    filename: str
    content_type: Optional[str] = None
    file_size: Optional[int] = None
    status: str
    error: Optional[str] = None
    created_at: str


def _to_out(doc: KnowledgeDocument) -> DocumentOut:
    return DocumentOut(
        doc_id=doc.doc_id,
        filename=doc.filename,
        content_type=doc.content_type,
        file_size=doc.file_size,
        status=doc.status,
        error=doc.error,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
    )


@router.post("/upload", response_model=DocumentOut)
async def upload_document(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    org_id = active_org.organization.id
    filename = file.filename or "untitled"
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=422, detail=f"Unsupported file type. Supported: {sorted(SUPPORTED_EXTENSIONS)}")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="File is empty")

    digest = content_hash(data)
    existing = (
        db.query(KnowledgeDocument)
        .filter(KnowledgeDocument.organization_id == org_id, KnowledgeDocument.content_hash == digest)
        .first()
    )
    if existing:
        # Duplicate content already indexed for this org — return the
        # existing record rather than creating a second copy or re-running
        # extraction/embedding for identical bytes.
        return _to_out(existing)

    try:
        text = extract_text(filename, data)
    except UnsupportedDocumentType as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except DocumentTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("Document text extraction failed for %s: %s", filename, exc)
        raise HTTPException(status_code=422, detail=f"Could not extract text from {filename}: {exc}") from exc

    if not text.strip():
        raise HTTPException(status_code=422, detail="No extractable text found in document")

    doc_id = new_doc_id()
    doc = KnowledgeDocument(
        doc_id=doc_id,
        organization_id=org_id,
        uploaded_by_id=current_user.id,
        filename=filename,
        content_type=file.content_type,
        file_size=len(data),
        content_hash=digest,
        extracted_text=text,
        status="processing",
    )
    db.add(doc)
    try:
        db.commit()
        db.refresh(doc)
    except IntegrityError:
        # Lost a race with a concurrent identical upload — return the row
        # the other request created instead of erroring.
        db.rollback()
        existing = (
            db.query(KnowledgeDocument)
            .filter(KnowledgeDocument.organization_id == org_id, KnowledgeDocument.content_hash == digest)
            .first()
        )
        if existing:
            return _to_out(existing)
        raise

    try:
        index_document(
            doc_id=doc_id,
            title=filename,
            text=text,
            organization_id=org_id,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        doc.status = "indexed"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Qdrant indexing failed for document %s: %s", doc_id, exc)
        doc.status = "failed"
        doc.error = str(exc)[:2000]
    db.commit()
    db.refresh(doc)
    return _to_out(doc)


@router.get("", response_model=List[DocumentOut])
async def list_documents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    rows = (
        db.query(KnowledgeDocument)
        .filter(KnowledgeDocument.organization_id == active_org.organization.id)
        .order_by(desc(KnowledgeDocument.created_at))
        .all()
    )
    return [_to_out(d) for d in rows]


@router.get("/{doc_id}", response_model=DocumentOut)
async def get_document(
    doc_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    doc = (
        db.query(KnowledgeDocument)
        .filter(KnowledgeDocument.doc_id == doc_id, KnowledgeDocument.organization_id == active_org.organization.id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return _to_out(doc)


@router.post("/{doc_id}/reindex", response_model=DocumentOut)
async def reindex_document(
    doc_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    doc = (
        db.query(KnowledgeDocument)
        .filter(KnowledgeDocument.doc_id == doc_id, KnowledgeDocument.organization_id == active_org.organization.id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    if not doc.extracted_text:
        raise HTTPException(status_code=400, detail="Document has no stored text to reindex")

    try:
        index_document(
            doc_id=doc.doc_id,
            title=doc.filename,
            text=doc.extracted_text,
            organization_id=doc.organization_id,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        doc.status = "indexed"
        doc.error = None
    except Exception as exc:  # noqa: BLE001
        doc.status = "failed"
        doc.error = str(exc)[:2000]
    db.commit()
    db.refresh(doc)
    return _to_out(doc)


@router.delete("/{doc_id}")
async def delete_document(
    doc_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    doc = (
        db.query(KnowledgeDocument)
        .filter(KnowledgeDocument.doc_id == doc_id, KnowledgeDocument.organization_id == active_org.organization.id)
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    try:
        delete_document_index(doc_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to remove Qdrant points for document %s: %s", doc_id, exc)
    db.delete(doc)
    db.commit()
    return {"ok": True}
