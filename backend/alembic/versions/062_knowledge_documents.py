"""Add knowledge_documents: PDF/DOCX/TXT/MD documents indexed into the same
Qdrant collection meetings and lectures use (Phase 3 module K). No second
vector store, no second pipeline — see services/document_knowledge.py.

Revision ID: 062_knowledge_documents
Revises: 061_important_moments
Create Date: 2026-09-14
"""

from alembic import op
import sqlalchemy as sa

revision = "062_knowledge_documents"
down_revision = "061_important_moments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("doc_id", sa.String(length=64), nullable=False, unique=True, index=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("uploaded_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False, index=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="processing"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("organization_id", "content_hash", name="uq_knowledge_documents_org_hash"),
    )


def downgrade() -> None:
    op.drop_table("knowledge_documents")
