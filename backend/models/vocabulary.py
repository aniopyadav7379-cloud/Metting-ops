"""
SQLAlchemy models for custom vocabulary system
"""

from sqlalchemy import Column, String, Integer, Boolean, Text, ForeignKey, TIMESTAMP, UniqueConstraint, Index
from sqlalchemy.dialects.postgresql import UUID, ARRAY, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from database.database import Base


class CustomVocabulary(Base):
    """Model for custom vocabulary terms and acronyms"""
    __tablename__ = "custom_vocabulary"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    term = Column(String(100), nullable=False, index=True)
    expansion = Column(String(500), nullable=False)
    category = Column(String(50), nullable=True)  # defense, medical, finance, etc.
    industry = Column(String(50), nullable=True)
    priority = Column(Integer, default=0)  # Higher priority = prefer this expansion
    context_hints = Column(ARRAY(Text), nullable=True)  # Words that suggest this meaning
    case_sensitive = Column(Boolean, default=False)
    regex_pattern = Column(String(200), nullable=True)  # Optional regex for complex matching
    is_active = Column(Boolean, default=True)
    usage_count = Column(Integer, default=0)

    # Metadata
    created_by = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())
    # NOT NULL on Postgres per 004_multi_org_scoping; every row must be
    # scoped to an organization. Was created nullable in 003 then enforced
    # in 004 once the default org was backfilled.
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)

    # Constraints + legacy indexes from 003_add_custom_vocabulary.
    # idx_* names predate the ix_* convention; kept verbatim so autogenerate
    # stops flagging them.
    __table_args__ = (
        UniqueConstraint('organization_id', 'term', 'category', name='uq_vocabulary_term_category'),
        Index('idx_vocabulary_term', 'term'),
        Index('idx_vocabulary_category', 'category'),
        Index('idx_vocabulary_active', 'is_active'),
    )
    
    def to_dict(self):
        """Convert to dictionary for API responses"""
        return {
            'id': str(self.id),
            'term': self.term,
            'expansion': self.expansion,
            'category': self.category,
            'industry': self.industry,
            'priority': self.priority,
            'context_hints': self.context_hints,
            'case_sensitive': self.case_sensitive,
            'regex_pattern': self.regex_pattern,
            'is_active': self.is_active,
            'usage_count': self.usage_count,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class VocabularySet(Base):
    """Model for vocabulary sets/collections"""
    __tablename__ = "vocabulary_sets"
    # idx_vocabulary_sets_active is a legacy index from 003_add_custom_vocabulary;
    # kept so autogenerate doesn't flag it for drop.
    __table_args__ = (
        UniqueConstraint('organization_id', 'name', name='uq_vocabulary_sets_org_name'),
        Index('idx_vocabulary_sets_active', 'is_active'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)  # Auto-apply to all sessions
    industry = Column(String(50), nullable=True)
    category = Column(String(50), nullable=True)
    vocab_ids = Column(ARRAY(UUID(as_uuid=True)), nullable=True)
    settings = Column(JSONB, nullable=True)  # Additional configuration

    # Metadata
    created_by = Column(Integer, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())
    # NOT NULL on Postgres per 004_multi_org_scoping.
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)

    # Relationships
    session_vocabularies = relationship("SessionVocabulary", back_populates="vocabulary_set")
    
    def to_dict(self):
        """Convert to dictionary for API responses"""
        return {
            'id': str(self.id),
            'name': self.name,
            'description': self.description,
            'is_active': self.is_active,
            'is_default': self.is_default,
            'industry': self.industry,
            'category': self.category,
            'vocab_ids': [str(vid) for vid in self.vocab_ids] if self.vocab_ids else [],
            'settings': self.settings,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


class SessionVocabulary(Base):
    """Model for linking recording sessions to vocabulary sets"""
    __tablename__ = "session_vocabulary"
    # idx_session_vocabulary_session is the legacy 003-era index on session_id;
    # session_id already has index=True which produces ix_session_vocabulary_session_id,
    # but the legacy idx_* name also exists in Postgres and must be declared so
    # autogenerate stops trying to drop it.
    __table_args__ = (
        Index('idx_session_vocabulary_session', 'session_id'),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    vocabulary_set_id = Column(UUID(as_uuid=True), ForeignKey('vocabulary_sets.id', ondelete='CASCADE'), nullable=False)
    applied_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    applied_to_live = Column(Boolean, default=False)
    applied_to_final = Column(Boolean, default=False)
    # NOT NULL on Postgres per 004_multi_org_scoping.
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    
    # Relationships
    vocabulary_set = relationship("VocabularySet", back_populates="session_vocabularies")
    
    def to_dict(self):
        """Convert to dictionary for API responses"""
        return {
            'id': str(self.id),
            'session_id': str(self.session_id),
            'vocabulary_set_id': str(self.vocabulary_set_id),
            'applied_at': self.applied_at.isoformat() if self.applied_at else None,
            'applied_to_live': self.applied_to_live,
            'applied_to_final': self.applied_to_final,
        }
