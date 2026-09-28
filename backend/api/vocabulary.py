"""
API endpoints for custom vocabulary management
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from uuid import UUID
import logging

from database.database import get_db
from database.models import RecordingSession as DBRecordingSession
from models.vocabulary import CustomVocabulary, VocabularySet, SessionVocabulary
from auth.dependencies import get_current_organization, get_current_user
from auth.organization import ActiveOrganization
from pydantic import BaseModel
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vocabulary", tags=["vocabulary"])


# Pydantic models for request/response
class VocabularyTermCreate(BaseModel):
    term: str
    expansion: str
    category: Optional[str] = None
    industry: Optional[str] = None
    priority: int = 0
    context_hints: Optional[List[str]] = None
    case_sensitive: bool = False
    regex_pattern: Optional[str] = None


class VocabularyTermUpdate(BaseModel):
    expansion: Optional[str] = None
    category: Optional[str] = None
    industry: Optional[str] = None
    priority: Optional[int] = None
    context_hints: Optional[List[str]] = None
    case_sensitive: Optional[bool] = None
    regex_pattern: Optional[str] = None
    is_active: Optional[bool] = None


class VocabularySetCreate(BaseModel):
    name: str
    description: Optional[str] = None
    industry: Optional[str] = None
    category: Optional[str] = None
    is_default: bool = False
    vocab_ids: Optional[List[str]] = None


class VocabularySetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None
    vocab_ids: Optional[List[str]] = None


class ApplyVocabularyRequest(BaseModel):
    session_id: str
    vocabulary_set_ids: List[str]
    apply_to_live: bool = True
    apply_to_final: bool = True


def _term_query(db: Session, organization_id: int):
    return db.query(CustomVocabulary).filter(
        CustomVocabulary.organization_id == organization_id
    )


def _set_query(db: Session, organization_id: int):
    return db.query(VocabularySet).filter(
        VocabularySet.organization_id == organization_id
    )


# Vocabulary Term Endpoints
@router.get("/terms")
async def get_vocabulary_terms(
    category: Optional[str] = None,
    industry: Optional[str] = None,
    is_active: Optional[bool] = True,
    search: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    current_user=Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Get all vocabulary terms with optional filtering"""
    query = _term_query(db, active_org.organization.id)
    
    if category:
        query = query.filter(CustomVocabulary.category == category)
    if industry:
        query = query.filter(CustomVocabulary.industry == industry)
    if is_active is not None:
        query = query.filter(CustomVocabulary.is_active == is_active)
    if search:
        query = query.filter(
            (CustomVocabulary.term.ilike(f"%{search}%")) |
            (CustomVocabulary.expansion.ilike(f"%{search}%"))
        )
    
    # Get total count
    total = query.count()
    
    # Apply pagination
    terms = query.order_by(CustomVocabulary.priority.desc(), CustomVocabulary.term).offset(offset).limit(limit).all()
    
    return {
        "total": total,
        "items": [term.to_dict() for term in terms],
        "limit": limit,
        "offset": offset
    }


@router.post("/terms")
async def create_vocabulary_term(
    term_data: VocabularyTermCreate,
    current_user=Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Create a new vocabulary term"""
    # Check if term already exists in category
    existing = _term_query(db, active_org.organization.id).filter(
        CustomVocabulary.term == term_data.term,
        CustomVocabulary.category == term_data.category
    ).first()
    
    if existing:
        raise HTTPException(status_code=400, detail=f"Term '{term_data.term}' already exists in category '{term_data.category}'")
    
    vocab = CustomVocabulary(
        term=term_data.term,
        expansion=term_data.expansion,
        category=term_data.category,
        industry=term_data.industry,
        priority=term_data.priority,
        context_hints=term_data.context_hints,
        case_sensitive=term_data.case_sensitive,
        regex_pattern=term_data.regex_pattern,
        created_by=current_user.id if current_user else None,
        organization_id=active_org.organization.id,
    )
    
    db.add(vocab)
    db.commit()
    db.refresh(vocab)
    
    return vocab.to_dict()


@router.get("/terms/{term_id}")
async def get_vocabulary_term(
    term_id: UUID,
    current_user=Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Get a specific vocabulary term"""
    term = _term_query(db, active_org.organization.id).filter(CustomVocabulary.id == term_id).first()
    if not term:
        raise HTTPException(status_code=404, detail="Vocabulary term not found")
    return term.to_dict()


@router.put("/terms/{term_id}")
async def update_vocabulary_term(
    term_id: UUID,
    term_data: VocabularyTermUpdate,
    current_user=Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Update a vocabulary term"""
    term = _term_query(db, active_org.organization.id).filter(CustomVocabulary.id == term_id).first()
    if not term:
        raise HTTPException(status_code=404, detail="Vocabulary term not found")
    
    # Update fields
    for field, value in term_data.dict(exclude_unset=True).items():
        setattr(term, field, value)
    
    term.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(term)
    
    return term.to_dict()


@router.delete("/terms/{term_id}")
async def delete_vocabulary_term(
    term_id: UUID,
    current_user=Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Delete a vocabulary term"""
    term = _term_query(db, active_org.organization.id).filter(CustomVocabulary.id == term_id).first()
    if not term:
        raise HTTPException(status_code=404, detail="Vocabulary term not found")
    
    db.delete(term)
    db.commit()
    
    return {"message": "Vocabulary term deleted successfully"}


# Vocabulary Set Endpoints
@router.get("/sets")
async def get_vocabulary_sets(
    is_active: Optional[bool] = True,
    is_default: Optional[bool] = None,
    industry: Optional[str] = None,
    current_user=Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Get all vocabulary sets"""
    query = _set_query(db, active_org.organization.id)
    
    if is_active is not None:
        query = query.filter(VocabularySet.is_active == is_active)
    if is_default is not None:
        query = query.filter(VocabularySet.is_default == is_default)
    if industry:
        query = query.filter(VocabularySet.industry == industry)
    
    sets = query.order_by(VocabularySet.name).all()
    
    return [vocab_set.to_dict() for vocab_set in sets]


@router.post("/sets")
async def create_vocabulary_set(
    set_data: VocabularySetCreate,
    current_user=Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Create a new vocabulary set"""
    # Check if name already exists
    existing = _set_query(db, active_org.organization.id).filter(VocabularySet.name == set_data.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Vocabulary set '{set_data.name}' already exists")
    
    # If setting as default, unset other defaults
    if set_data.is_default:
        _set_query(db, active_org.organization.id).update({VocabularySet.is_default: False})
    
    vocab_set = VocabularySet(
        name=set_data.name,
        description=set_data.description,
        industry=set_data.industry,
        category=set_data.category,
        is_default=set_data.is_default,
        vocab_ids=[UUID(vid) for vid in set_data.vocab_ids] if set_data.vocab_ids else None,
        created_by=current_user.id if current_user else None,
        organization_id=active_org.organization.id,
    )
    
    db.add(vocab_set)
    db.commit()
    db.refresh(vocab_set)
    
    return vocab_set.to_dict()


@router.get("/sets/{set_id}")
async def get_vocabulary_set(
    set_id: UUID,
    include_terms: bool = False,
    current_user=Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Get a specific vocabulary set"""
    vocab_set = _set_query(db, active_org.organization.id).filter(VocabularySet.id == set_id).first()
    if not vocab_set:
        raise HTTPException(status_code=404, detail="Vocabulary set not found")
    
    result = vocab_set.to_dict()
    
    # Include full term details if requested
    if include_terms and vocab_set.vocab_ids:
        terms = _term_query(db, active_org.organization.id).filter(CustomVocabulary.id.in_(vocab_set.vocab_ids)).all()
        result['terms'] = [term.to_dict() for term in terms]
    
    return result


@router.put("/sets/{set_id}")
async def update_vocabulary_set(
    set_id: UUID,
    set_data: VocabularySetUpdate,
    current_user=Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Update a vocabulary set"""
    vocab_set = _set_query(db, active_org.organization.id).filter(VocabularySet.id == set_id).first()
    if not vocab_set:
        raise HTTPException(status_code=404, detail="Vocabulary set not found")
    
    # If setting as default, unset other defaults
    if set_data.is_default:
        _set_query(db, active_org.organization.id).filter(VocabularySet.id != set_id).update({VocabularySet.is_default: False})
    
    # Update fields
    for field, value in set_data.dict(exclude_unset=True).items():
        if field == 'vocab_ids' and value is not None:
            value = [UUID(vid) for vid in value]
        setattr(vocab_set, field, value)
    
    vocab_set.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(vocab_set)
    
    return vocab_set.to_dict()


@router.delete("/sets/{set_id}")
async def delete_vocabulary_set(
    set_id: UUID,
    current_user=Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Delete a vocabulary set"""
    vocab_set = _set_query(db, active_org.organization.id).filter(VocabularySet.id == set_id).first()
    if not vocab_set:
        raise HTTPException(status_code=404, detail="Vocabulary set not found")
    
    db.delete(vocab_set)
    db.commit()
    
    return {"message": "Vocabulary set deleted successfully"}


@router.post("/sets/{set_id}/terms/{term_id}")
async def add_term_to_set(
    set_id: UUID,
    term_id: UUID,
    current_user=Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Add a vocabulary term to a set"""
    vocab_set = _set_query(db, active_org.organization.id).filter(VocabularySet.id == set_id).first()
    if not vocab_set:
        raise HTTPException(status_code=404, detail="Vocabulary set not found")
    
    term = _term_query(db, active_org.organization.id).filter(CustomVocabulary.id == term_id).first()
    if not term:
        raise HTTPException(status_code=404, detail="Vocabulary term not found")
    
    # Add term to set
    if vocab_set.vocab_ids is None:
        vocab_set.vocab_ids = []
    
    if term_id not in vocab_set.vocab_ids:
        vocab_set.vocab_ids = vocab_set.vocab_ids + [term_id]
        vocab_set.updated_at = datetime.now(timezone.utc)
        db.commit()
    
    return {"message": "Term added to set successfully"}


@router.delete("/sets/{set_id}/terms/{term_id}")
async def remove_term_from_set(
    set_id: UUID,
    term_id: UUID,
    current_user=Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Remove a vocabulary term from a set"""
    vocab_set = _set_query(db, active_org.organization.id).filter(VocabularySet.id == set_id).first()
    if not vocab_set:
        raise HTTPException(status_code=404, detail="Vocabulary set not found")
    
    if vocab_set.vocab_ids and term_id in vocab_set.vocab_ids:
        vocab_set.vocab_ids = [vid for vid in vocab_set.vocab_ids if vid != term_id]
        vocab_set.updated_at = datetime.now(timezone.utc)
        db.commit()
    
    return {"message": "Term removed from set successfully"}


# Session Vocabulary Application
@router.post("/apply")
async def apply_vocabulary_to_session(
    request: ApplyVocabularyRequest,
    current_user=Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Apply vocabulary sets to a recording session"""
    session = db.query(DBRecordingSession).filter(
        DBRecordingSession.organization_id == active_org.organization.id,
        DBRecordingSession.session_id == str(request.session_id),
    ).first()
    if not session:
        raise HTTPException(status_code=404, detail="Recording session not found")

    session_vocabs = []
    
    for set_id in request.vocabulary_set_ids:
        # Check if vocabulary set exists
        vocab_set = _set_query(db, active_org.organization.id).filter(VocabularySet.id == UUID(set_id)).first()
        if not vocab_set:
            logger.warning(f"Vocabulary set {set_id} not found")
            continue
        
        # Check if already applied
        existing = db.query(SessionVocabulary).filter(
            SessionVocabulary.organization_id == active_org.organization.id,
            SessionVocabulary.session_id == UUID(request.session_id),
            SessionVocabulary.vocabulary_set_id == UUID(set_id)
        ).first()
        
        if existing:
            # Update existing
            existing.applied_to_live = request.apply_to_live
            existing.applied_to_final = request.apply_to_final
            session_vocabs.append(existing)
        else:
            # Create new
            session_vocab = SessionVocabulary(
                session_id=UUID(request.session_id),
                vocabulary_set_id=UUID(set_id),
                applied_to_live=request.apply_to_live,
                applied_to_final=request.apply_to_final,
                organization_id=active_org.organization.id,
            )
            db.add(session_vocab)
            session_vocabs.append(session_vocab)
    
    db.commit()
    
    return {
        "message": "Vocabulary sets applied successfully",
        "applied_sets": len(session_vocabs)
    }


@router.get("/sessions/{session_id}")
async def get_session_vocabulary(
    session_id: UUID,
    current_user=Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Get vocabulary sets applied to a session"""
    session_vocabs = db.query(SessionVocabulary).filter(
        SessionVocabulary.organization_id == active_org.organization.id,
        SessionVocabulary.session_id == session_id
    ).all()
    
    result = []
    for sv in session_vocabs:
        vocab_set = _set_query(db, active_org.organization.id).filter(VocabularySet.id == sv.vocabulary_set_id).first()
        if vocab_set:
            result.append({
                "set": vocab_set.to_dict(),
                "applied_to_live": sv.applied_to_live,
                "applied_to_final": sv.applied_to_final,
                "applied_at": sv.applied_at.isoformat() if sv.applied_at else None
            })
    
    return result


# Import/Export Endpoints
@router.get("/export")
async def export_vocabulary(
    set_id: Optional[UUID] = None,
    format: str = Query("json", pattern="^(json|csv)$"),
    current_user=Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Export vocabulary terms or sets"""
    if set_id:
        vocab_set = _set_query(db, active_org.organization.id).filter(VocabularySet.id == set_id).first()
        if not vocab_set:
            raise HTTPException(status_code=404, detail="Vocabulary set not found")
        
        terms = []
        if vocab_set.vocab_ids:
            terms = _term_query(db, active_org.organization.id).filter(CustomVocabulary.id.in_(vocab_set.vocab_ids)).all()
    else:
        terms = _term_query(db, active_org.organization.id).filter(CustomVocabulary.is_active == True).all()
    
    if format == "json":
        return {
            "vocabulary": [term.to_dict() for term in terms],
            "total": len(terms)
        }
    else:  # CSV
        import csv
        from io import StringIO
        from fastapi.responses import Response
        
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(["Term", "Expansion", "Category", "Industry", "Priority"])
        
        for term in terms:
            writer.writerow([
                term.term,
                term.expansion,
                term.category or "",
                term.industry or "",
                term.priority
            ])
        
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=vocabulary_export.csv"}
        )
