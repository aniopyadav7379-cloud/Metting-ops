"""Customer support contact-form: support_requests table.

Revision ID: 037_support_requests
Revises: 036_processing_job_id
Create Date: 2026-05-30

Captures messages submitted via the public /contact page (and the
authed in-app "Contact Support" footer link). Open to anyone, but
rate-limited at the API layer (`backend/api/support.py`, 3 / email /
hour). Unlike `invite_requests` we keep multiple rows per email so the
support inbox sees every distinct conversation.

  - `email` is NOT unique (intentional — repeat customers send
    multiple messages over time).
  - `subject` + `message` carry the body; the API fires a Postmark mail
    on insert so the human inbox is the canonical workflow.
  - `created_at` for FIFO triage.
  - `user_id` is best-effort: stamped when an authenticated user
    submits the form, NULL for anonymous contacts from the public
    landing page.
  - `resolved_at` is the simple triage flag — flip in the DB when the
    ticket closes; no app code reads it back today.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "037_support_requests"
down_revision = "036_processing_job_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "support_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_support_requests_created_at",
        "support_requests",
        ["created_at"],
    )
    op.create_index(
        "ix_support_requests_email",
        "support_requests",
        ["email"],
    )


def downgrade() -> None:
    op.drop_index("ix_support_requests_email", table_name="support_requests")
    op.drop_index("ix_support_requests_created_at", table_name="support_requests")
    op.drop_table("support_requests")
