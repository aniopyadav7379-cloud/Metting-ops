"""Stable transcript ingest provenance and idempotency.

Revision ID: 058_stable_ingest
Revises: 057_upload_dedupe
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa

revision = "058_stable_ingest"
down_revision = "057_upload_dedupe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recording_sessions",
        sa.Column("external_source", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "recording_sessions",
        sa.Column("external_id", sa.String(length=128), nullable=True),
    )
    op.create_unique_constraint(
        "uq_recording_sessions_org_external_source_id",
        "recording_sessions",
        ["organization_id", "external_source", "external_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_recording_sessions_org_external_source_id",
        "recording_sessions",
        type_="unique",
    )
    op.drop_column("recording_sessions", "external_id")
    op.drop_column("recording_sessions", "external_source")
