"""Baseline placeholder for the pre-Alembic schema.

Revision ID: 001_baseline_current_schema
Revises:
Create Date: 2026-05-03
"""
from alembic import op


revision = "001_baseline_current_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The live deployment was initialized with create_all/manual scripts before
    # Alembic was checked in. This placeholder establishes the chain without
    # trying to recreate the whole historical schema.
    pass


def downgrade() -> None:
    pass
