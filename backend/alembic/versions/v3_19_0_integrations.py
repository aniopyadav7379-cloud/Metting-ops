"""Per-org integrations config (Brigade / Project-Ops / Contact-Ops / Accounting-Ops / Stable).

Adds ``organizations.integrations`` JSONB column. Each key is one
upstream service; secrets (api_key_encrypted) stored Fernet-encrypted at
rest using the existing ``PROVIDER_ENCRYPTION_KEY`` helper from
``services.providers.crypto``. Existing orgs default to an empty dict so
the writers fall through to the env-var defaults (backwards compatible —
nothing breaks on upgrade for orgs that haven't opted in yet).

Revision ID: v3_19_0_integrations
Revises: 032_personal_access_tokens
Create Date: 2026-05-29 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "v3_19_0_integrations"
down_revision = "032_personal_access_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # JSONB on Postgres, JSON fallback on SQLite (test fixture). Use a
    # variant so the same migration runs cleanly under SQLite during
    # tests without needing a separate path.
    json_type = sa.JSON().with_variant(JSONB(), "postgresql")
    op.add_column(
        "organizations",
        sa.Column(
            "integrations",
            json_type,
            nullable=False,
            server_default=sa.text("'{}'::jsonb")
            if op.get_bind().dialect.name == "postgresql"
            else sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("organizations", "integrations")
