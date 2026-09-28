"""Vocabulary system database models"""
from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, JSON, ForeignKey, Table, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
import uuid
from database.database import Base

# Association table for many-to-many relationship between sets and terms
vocabulary_set_terms = Table(
    'vocabulary_set_terms',
    Base.metadata,
    Column('set_id', String, ForeignKey('vocabulary_sets.id')),
    Column('term_id', String, ForeignKey('vocabulary_terms.id'))
)

class VocabularyTerm(Base):
    """Individual vocabulary term with expansion"""
    __tablename__ = "vocabulary_terms"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    term = Column(String(100), nullable=False, unique=True)  # Original term/acronym
    expansion = Column(String(500), nullable=False)  # Full expansion
    category = Column(String(50))  # defense, medical, finance, etc.
    priority = Column(Integer, default=0)  # Higher priority = prefer this expansion
    context_hints = Column(JSON, default=list)  # Words that suggest this meaning
    created_by = Column(Integer)  # User ID who created this
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=True, index=True)
    
    # Relationship to sets
    sets = relationship("VocabularySet", secondary=vocabulary_set_terms, back_populates="terms")
    
    def to_dict(self):
        return {
            "id": self.id,
            "term": self.term,
            "expansion": self.expansion,
            "category": self.category,
            "priority": self.priority,
            "context_hints": self.context_hints,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }

class VocabularySet(Base):
    """Collection of vocabulary terms that can be activated/deactivated"""
    __tablename__ = "vocabulary_sets"
    __table_args__ = (
        UniqueConstraint('organization_id', 'name', name='uq_vocabulary_sets_org_name'),
    )
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)  # "Defense Acronyms", "Medical Terms"
    description = Column(Text)
    is_active = Column(Boolean, default=True)
    industry = Column(String(50))  # defense, medical, finance, tech
    created_by = Column(Integer)  # User ID
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=True, index=True)
    
    # Relationship to terms
    terms = relationship("VocabularyTerm", secondary=vocabulary_set_terms, back_populates="sets")
    
    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "is_active": self.is_active,
            "industry": self.industry,
            "term_count": len(self.terms) if self.terms else 0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }
