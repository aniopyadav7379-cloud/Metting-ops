"""Invite-only landing: invite_requests table.

Revision ID: 035_invite_requests
Revises: 034_merge_integrations_stripe
Create Date: 2026-05-29

Captures email addresses submitted via the public invite-only landing
page at `meeting-ops.unicorncommander.ai/`. The page is open to the
public (no auth) and rate-limited at the API layer
(`backend/api/landing.py`) — this table is intentionally tiny:

  - `email` is UNIQUE so re-submissions are idempotent (the API turns
    a duplicate into a 200 "ok" response without spamming the inbox).
  - `created_at` is when the first request landed (we don't overwrite
    on dupes; we just no-op).
  - `notes` is free-form for the human-side triage workflow (e.g.
    "Aaron knows them, fast-track" / "Pending GitHub follow-up").
  - `processed_at` flips when the invite goes out so the table doubles
    as a tiny CRM without a join.

Down: drop the table. No data depends on it.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "035_invite_requests"
down_revision = "034_merge_integrations_stripe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invite_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("email", name="uq_invite_requests_email"),
    )
    op.create_index(
        "ix_invite_requests_created_at",
        "invite_requests",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_invite_requests_created_at", table_name="invite_requests")
    op.drop_table("invite_requests")
