"""Project-Ops action-item approval/backlink lifecycle.

Revision ID: 054_project_ops_action_lifecycle
Revises: 053_invitation_hashes_delivery
Create Date: 2026-07-24 00:00:00.000000

Promotes the Project-Ops linkage out of action_items.raw_payload into explicit,
queryable columns. The local action-item status remains independent.
"""

from alembic import op
import sqlalchemy as sa


revision = "054_project_ops_action_lifecycle"
down_revision = "053_invitation_hashes_delivery"
branch_labels = None
depends_on = None


_STATES = (
    "local_only",
    "proposed",
    "approved_linked",
    "rejected",
    "sync_failed",
)


def upgrade() -> None:
    op.add_column(
        "action_items",
        sa.Column(
            "project_ops_link_state",
            sa.String(length=32),
            nullable=False,
            server_default="local_only",
        ),
    )
    op.add_column(
        "action_items",
        sa.Column("project_ops_proposal_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "action_items",
        sa.Column("project_ops_task_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "action_items",
        sa.Column("project_ops_task_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "action_items",
        sa.Column(
            "project_ops_project_number", sa.String(length=32), nullable=True
        ),
    )
    op.add_column(
        "action_items",
        sa.Column("project_ops_task_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "action_items",
        sa.Column(
            "project_ops_submitted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "action_items",
        sa.Column(
            "project_ops_last_sync_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "action_items",
        sa.Column(
            "project_ops_last_synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "action_items",
        sa.Column(
            "project_ops_remote_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "action_items",
        sa.Column("project_ops_sync_error", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "action_items",
        sa.Column(
            "project_ops_retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    # Preserve legacy links/submissions. PostgreSQL's guarded JSON operators
    # keep non-object/null payloads safe; no meeting content is copied.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            UPDATE action_items
               SET project_ops_task_id = raw_payload->>'po_task_id',
                   project_ops_project_number = raw_payload->>'po_project_number',
                   project_ops_link_state = 'approved_linked'
             WHERE jsonb_typeof(raw_payload) = 'object'
               AND NULLIF(raw_payload->>'po_task_id', '') IS NOT NULL
            """
        )
        op.execute(
            """
            UPDATE action_items
               SET project_ops_link_state = 'proposed'
             WHERE project_ops_link_state = 'local_only'
               AND jsonb_typeof(raw_payload) = 'object'
               AND NULLIF(raw_payload->>'po_triage_submitted_at', '') IS NOT NULL
            """
        )

    op.create_check_constraint(
        "ck_action_items_project_ops_link_state",
        "action_items",
        "project_ops_link_state IN "
        + "("
        + ", ".join(f"'{state}'" for state in _STATES)
        + ")",
    )
    op.create_index(
        "ix_action_items_org_project_ops_state",
        "action_items",
        ["organization_id", "project_ops_link_state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_action_items_org_project_ops_state",
        table_name="action_items",
    )
    op.drop_constraint(
        "ck_action_items_project_ops_link_state",
        "action_items",
        type_="check",
    )
    for column in (
        "project_ops_retry_count",
        "project_ops_sync_error",
        "project_ops_remote_updated_at",
        "project_ops_last_synced_at",
        "project_ops_last_sync_attempt_at",
        "project_ops_submitted_at",
        "project_ops_task_status",
        "project_ops_project_number",
        "project_ops_task_url",
        "project_ops_task_id",
        "project_ops_proposal_id",
        "project_ops_link_state",
    ):
        op.drop_column("action_items", column)
