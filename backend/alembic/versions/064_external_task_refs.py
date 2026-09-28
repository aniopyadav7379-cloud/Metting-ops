"""Add action_items.external_task_refs — a single JSON column recording
which external systems (jira/todoist/clickup/notion/confluence/slack/
teams/email) an action item has been pushed to and the resulting
reference, instead of one dedicated column per provider (which the
existing google_calendar_event_id-style columns already do for Calendar,
kept as-is). A dict keeps this additive without a combinatorial explosion
of nullable columns as more providers are added.

Revision ID: 064_external_task_refs
Revises: 063_google_calendar
Create Date: 2026-09-15
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "064_external_task_refs"
down_revision = "063_google_calendar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    json_type = sa.JSON() if bind.dialect.name == "sqlite" else postgresql.JSONB()
    op.add_column("action_items", sa.Column("external_task_refs", json_type, nullable=True))


def downgrade() -> None:
    op.drop_column("action_items", "external_task_refs")
