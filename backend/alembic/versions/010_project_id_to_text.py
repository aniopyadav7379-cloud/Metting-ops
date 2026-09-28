"""widen project_id columns to TEXT for cross-app UUIDs

Revision ID: 010_project_id_to_text
Revises: 009_tts_jobs
Create Date: 2026-05-07 00:00:00.000000

Project linking now points at project-ops (UUIDs like
17004202-cfe1-4b6e-833e-1290e4367b21) and potentially crisis-ops
(also UUID-shaped). The legacy INT column shape was chosen when
project-ops was a stub that didn't yet exist.

The columns are NULLABLE so this conversion is non-destructive: any
existing INT values get cast to their text representation, NULLs stay
NULL.
"""

from alembic import op
import sqlalchemy as sa


revision = "010_project_id_to_text"
down_revision = "009_tts_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # recording_sessions.project_id  Integer -> Text
    op.alter_column(
        "recording_sessions",
        "project_id",
        existing_type=sa.Integer(),
        type_=sa.Text(),
        existing_nullable=True,
        postgresql_using="project_id::text",
    )
    # meeting_digest.project_id  Integer -> Text
    op.alter_column(
        "meeting_digest",
        "project_id",
        existing_type=sa.Integer(),
        type_=sa.Text(),
        existing_nullable=True,
        postgresql_using="project_id::text",
    )


def downgrade() -> None:
    # Best effort: text values that aren't valid integers will fail the
    # cast and rollback the downgrade. That's intentional — once the
    # column is holding UUIDs we should not silently drop them.
    op.alter_column(
        "meeting_digest",
        "project_id",
        existing_type=sa.Text(),
        type_=sa.Integer(),
        existing_nullable=True,
        postgresql_using="project_id::integer",
    )
    op.alter_column(
        "recording_sessions",
        "project_id",
        existing_type=sa.Text(),
        type_=sa.Integer(),
        existing_nullable=True,
        postgresql_using="project_id::integer",
    )
