"""v3.21.0 auth features: rename is_founder -> is_founding_member,
add founding_cohort, create password_reset_tokens.

Revision ID: 037_founding_member_and_password_reset
Revises: 036_processing_job_id
Create Date: 2026-05-30

Two related auth features ship in one migration:

1. Founding 100 cohort mechanic.
   The v3.19.0 column `users.is_founder` was a single boolean. v3.21.0
   introduces explicit cohort labelling so the Founding 100 mechanic can
   scale to a second cohort (e.g. `crisis_ops_v1`) without a second
   migration. We:
       - Rename `users.is_founder` -> `users.is_founding_member`.
       - Add `users.founding_cohort` (varchar(64), nullable).
       - Backfill: every existing is_founder=true row gets
         founding_cohort = 'meeting_ops_v1'.
       - Rename the index `ix_users_is_founder` -> `ix_users_is_founding_member`
         and add `ix_users_founding_cohort` for the
         "seats taken for cohort X" hot path.

   Aaron's pricing stance is unchanged: founding members pay the same
   $12/mo as everyone else; the cohort label only controls early-access
   + ecosystem bundle eligibility.

2. Password reset.
   New `password_reset_tokens` table. Tokens are stored hashed
   (sha256 of the cryptographically random 32-byte base64url token), with
   a 1-hour expiry and a single-use `used_at` stamp. We also index by
   user_id so the rate-limit / "expire previous tokens on new request"
   path stays fast.

Down: reverses all of the above. Backfill is destructive on the way down
(cohort information is dropped); production data isn't in this column at
release time so that's acceptable.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "037a_founding_pwreset"
down_revision = "036_processing_job_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # ---- Founding 100 cohort rename + new column ---------------------------
    op.add_column(
        "users",
        sa.Column("founding_cohort", sa.String(length=64), nullable=True),
    )

    # Backfill: existing is_founder=true rows become meeting_ops_v1 cohort.
    op.execute(
        sa.text(
            "UPDATE users SET founding_cohort = 'meeting_ops_v1' "
            "WHERE is_founder = TRUE"
        )
    )

    # Drop the old index before the column rename so the name doesn't
    # collide with the new one we want to create. Use idempotent SQL
    # rather than a Python try/except: in Postgres, a failed DDL aborts
    # the whole transaction and `except` only catches the Python
    # exception — every subsequent statement then fails with
    # `InFailedSqlTransaction`. `DROP INDEX IF EXISTS` lets Postgres
    # itself short-circuit when the index is missing, keeping the
    # transaction healthy. SQLite supports the same syntax.
    op.execute(sa.text("DROP INDEX IF EXISTS ix_users_is_founder"))

    # Column rename. alter_column is the portable path here (works on both
    # SQLite via batch + PostgreSQL).
    if dialect == "sqlite":
        with op.batch_alter_table("users", recreate="auto") as batch_op:
            batch_op.alter_column(
                "is_founder", new_column_name="is_founding_member"
            )
    else:
        op.alter_column(
            "users",
            "is_founder",
            new_column_name="is_founding_member",
        )

    op.create_index(
        "ix_users_is_founding_member",
        "users",
        ["is_founding_member"],
    )
    op.create_index(
        "ix_users_founding_cohort",
        "users",
        ["founding_cohort"],
    )

    # ---- Password reset tokens --------------------------------------------
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # sha256 hex digest of the plaintext token. 64 chars fixed.
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_password_reset_tokens_token_hash",
        "password_reset_tokens",
        ["token_hash"],
        unique=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.drop_index(
        "ix_password_reset_tokens_token_hash",
        table_name="password_reset_tokens",
    )
    op.drop_table("password_reset_tokens")

    op.drop_index("ix_users_founding_cohort", table_name="users")
    op.drop_index("ix_users_is_founding_member", table_name="users")

    if dialect == "sqlite":
        with op.batch_alter_table("users", recreate="auto") as batch_op:
            batch_op.alter_column(
                "is_founding_member", new_column_name="is_founder"
            )
    else:
        op.alter_column(
            "users",
            "is_founding_member",
            new_column_name="is_founder",
        )

    op.create_index(
        "ix_users_is_founder",
        "users",
        ["is_founder"],
    )
    op.drop_column("users", "founding_cohort")
