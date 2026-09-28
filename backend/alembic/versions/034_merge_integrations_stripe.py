"""Merge v3_19_0_integrations + 033_stripe_billing into a single head.

Both migrations branched off 032_personal_access_tokens during the 2026-05-29
multi-agent swarm release — the Integrations-UI agent and the Stripe agent
worked in parallel worktrees and each chose `032_personal_access_tokens` as
their `down_revision`. This is a no-op merge that just declares the new
single head so `alembic upgrade head` stops failing with "Multiple head
revisions are present".

Revision ID: 034_merge_integrations_stripe
Revises: v3_19_0_integrations, 033_stripe_billing
Create Date: 2026-05-29

"""
from __future__ import annotations

# revision identifiers, used by Alembic.
revision = "034_merge_integrations_stripe"
down_revision = ("v3_19_0_integrations", "033_stripe_billing")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No-op: the two parallel migrations did all the schema work.
    pass


def downgrade() -> None:
    # No-op: downgrading splits the head back into two. Use
    # `alembic downgrade <specific-rev>` if you really need that.
    pass
