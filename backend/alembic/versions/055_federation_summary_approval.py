"""federation: bind explicit approval to the current summary projection

Revision ID: 055_federation_summary_approval
Revises: 054_project_ops_action_lifecycle
Create Date: 2026-07-24
"""

from alembic import op
import sqlalchemy as sa


revision = "055_federation_summary_approval"
down_revision = "054_project_ops_action_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recording_sessions",
        sa.Column(
            "federation_summary_approved_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "recording_sessions",
        sa.Column(
            "federation_summary_approved_by_user_id",
            sa.Integer(),
            nullable=True,
        ),
    )
    op.add_column(
        "recording_sessions",
        sa.Column(
            "federation_summary_approved_digest",
            sa.String(length=64),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "recording_sessions",
        "federation_summary_approved_digest",
    )
    op.drop_column(
        "recording_sessions",
        "federation_summary_approved_by_user_id",
    )
    op.drop_column(
        "recording_sessions",
        "federation_summary_approved_at",
    )
