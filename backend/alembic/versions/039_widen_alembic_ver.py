"""widen alembic_version.version_num to VARCHAR(255)

v3.21.1 — defensive fix for future migration naming. Default alembic
versioning convention picks 32 chars but our naming convention has
drifted to descriptive snake_case like
``037_founding_member_and_password_reset`` (38 chars), which silently
breaks at COMMIT time inside the upgrade transaction — the schema DDL
applies fine, but the version-table INSERT blows up with
``StringDataRightTruncation`` and Postgres rolls the whole upgrade back.

Widening once removes the constraint. 255 is alembic's recommended
modern default for new projects.

Revision ID: 039_widen_alembic_ver
Revises: 038_merge_v3_21
Create Date: 2026-05-30
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "039_widen_alembic_ver"
down_revision = "038_merge_v3_21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """ALTER alembic_version.version_num TYPE VARCHAR(255).

    Safe to run repeatedly — Postgres ALTER TYPE on a VARCHAR-to-wider-
    VARCHAR is a metadata-only change for the rows already there.
    """
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=32),
        type_=sa.String(length=255),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Narrow back to VARCHAR(32).

    Will FAIL if any current row in alembic_version is >32 chars.
    Intentional — downgrading past this is a chooser-of-the-form-of-
    the-destructor action. Caller can manually truncate first if
    they really want it.
    """
    op.alter_column(
        "alembic_version",
        "version_num",
        existing_type=sa.String(length=255),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
