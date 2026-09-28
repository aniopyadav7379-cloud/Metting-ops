"""merge v3.21.0 parallel-agent heads

Two parallel feature agents both branched from 036_processing_job_id at
v3.20.0 head, each shipping a 037_* migration:

- 037_founding_member_and_password_reset (auth-features agent)
- 037_support_requests (frontend-polish agent)

This is a no-op merge migration so alembic has a single head again.
Both forward migrations are independent — neither table touches the
other's schema, so the merge is purely topological.

Revision ID: 038_merge_v3_21
Revises: 037a_founding_pwreset, 037_support_requests
Create Date: 2026-05-30
"""

from alembic import op  # noqa: F401


# revision identifiers, used by Alembic.
revision = "038_merge_v3_21"
down_revision = (
    "037a_founding_pwreset",
    "037_support_requests",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op — both branches already applied their forward schema."""
    pass


def downgrade() -> None:
    """No-op — split back into two heads on downgrade."""
    pass
