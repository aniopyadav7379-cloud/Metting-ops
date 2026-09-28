"""Add google_calendar_integrations (Phase 3F). Per-user OAuth connection
(a calendar event is created on behalf of whichever user confirmed the
action item, not the organization as a whole) so token ownership matches
who actually granted consent.

Revision ID: 063_google_calendar
Revises: 062_knowledge_documents
Create Date: 2026-09-14
"""

from alembic import op
import sqlalchemy as sa

revision = "063_google_calendar"
down_revision = "062_knowledge_documents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "google_calendar_integrations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("organization_id", sa.Integer(), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("google_email", sa.String(length=320), nullable=True),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="disconnected"),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("organization_id", "user_id", name="uq_google_calendar_org_user"),
    )

    # Mirrors the existing project_ops_* sync-state columns on action_items
    # (same convention, new integration) so an action item's calendar sync
    # status is queryable the same way its project-ops sync status already is.
    op.add_column("action_items", sa.Column("google_calendar_event_id", sa.String(length=200), nullable=True))
    op.add_column("action_items", sa.Column("google_calendar_link_state", sa.String(length=32), nullable=False, server_default="none"))
    op.add_column("action_items", sa.Column("google_calendar_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("action_items", sa.Column("google_calendar_sync_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("action_items", "google_calendar_sync_error")
    op.drop_column("action_items", "google_calendar_synced_at")
    op.drop_column("action_items", "google_calendar_link_state")
    op.drop_column("action_items", "google_calendar_event_id")
    op.drop_table("google_calendar_integrations")
