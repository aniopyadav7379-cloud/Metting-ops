"""Stripe billing fields: User.stripe_customer_id, User.is_founder, Organization.stripe_subscription_id.

Revision ID: 033_stripe_billing
Revises: 032_personal_access_tokens
Create Date: 2026-05-29 00:00:00.000000

Adds the columns the Stripe Subscriptions integration writes to.

`users.stripe_customer_id` is populated when we first need to talk to
Stripe for a user (Checkout, Billing Portal, or the `customer.created`
webhook). It's the link the webhook uses to find the local user when
Stripe sends us subscription events.

`users.is_founder` records the Founders 100 flag. Aaron's pricing
decision is locked: founders pay the SAME $12/mo as everyone else.
The flag is access + bundle eligibility (early access to new features,
ecosystem bundles like Project-Ops / Accounting-Ops), NOT a discount.
We backfill nothing — the gate fires at signup time when
FOUNDERS_100_ACTIVE is on and the count of existing founders is below
FOUNDERS_100_LIMIT.

`organizations.stripe_subscription_id` is reserved for v2 org-level
billing. v1 is individual subscriptions only (Aaron locked: Pro = $12
individual), but adding the column now means we don't need a second
migration when org-level lands.

Indexes on both new columns. `stripe_customer_id` index supports the
webhook's hot path (look up user by Stripe customer ID on every event);
`is_founder` index supports the "how many founder slots left" query.

Down: drop everything. Safe to rollback before any real Stripe activity
flips columns; once the production webhook has populated rows it's a
data-loss operation, like every other downgrade in this tree.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "033_stripe_billing"
down_revision = "032_personal_access_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("stripe_customer_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "is_founder",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "ix_users_stripe_customer_id",
        "users",
        ["stripe_customer_id"],
        unique=True,
    )
    op.create_index(
        "ix_users_is_founder",
        "users",
        ["is_founder"],
    )

    op.add_column(
        "organizations",
        sa.Column("stripe_subscription_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_organizations_stripe_subscription_id",
        "organizations",
        ["stripe_subscription_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organizations_stripe_subscription_id",
        table_name="organizations",
    )
    op.drop_column("organizations", "stripe_subscription_id")
    op.drop_index("ix_users_is_founder", table_name="users")
    op.drop_index("ix_users_stripe_customer_id", table_name="users")
    op.drop_column("users", "is_founder")
    op.drop_column("users", "stripe_customer_id")
