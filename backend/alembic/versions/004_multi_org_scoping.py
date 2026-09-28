"""Add multi-organization scoping.

Revision ID: 004_multi_org_scoping
Revises: 003_custom_vocabulary
Create Date: 2026-05-03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "004_multi_org_scoping"
down_revision = "003_custom_vocabulary"
branch_labels = None
depends_on = None

DEFAULT_ORG_SLUG = "magic-unicorn"
DEFAULT_ORG_NAME = "Magic Unicorn"


def _refresh_inspector():
    return sa.inspect(op.get_bind())


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _table_exists(inspector, table_name: str) -> bool:
    return table_name in inspector.get_table_names()


def _has_index(inspector, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _has_unique_constraint(inspector, table_name: str, constraint_name: str) -> bool:
    return any(
        constraint["name"] == constraint_name
        for constraint in inspector.get_unique_constraints(table_name)
    )


def _ensure_default_org() -> None:
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO organizations (
                name,
                slug,
                created_at,
                updated_at,
                plan,
                storage_quota_gb,
                storage_used_gb,
                max_users,
                max_monthly_hours,
                settings,
                is_active
            )
            SELECT
                :name,
                :slug,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP,
                'free',
                10,
                0,
                5,
                100,
                '{}'::jsonb,
                TRUE
            WHERE NOT EXISTS (
                SELECT 1 FROM organizations WHERE slug = :slug
            )
            """
        ),
        {"name": DEFAULT_ORG_NAME, "slug": DEFAULT_ORG_SLUG},
    )


def _default_org_id() -> int:
    bind = op.get_bind()
    return bind.execute(
        sa.text("SELECT id FROM organizations WHERE slug = :slug"),
        {"slug": DEFAULT_ORG_SLUG},
    ).scalar_one()


def _add_org_column(table_name: str, *, nullable: bool = True) -> None:
    inspector = _refresh_inspector()
    if not _table_exists(inspector, table_name):
        return
    if not _has_column(inspector, table_name, "organization_id"):
        op.add_column(
            table_name,
            sa.Column("organization_id", sa.Integer(), nullable=nullable),
        )


def _create_org_fk(table_name: str, constraint_name: str) -> None:
    inspector = _refresh_inspector()
    if not _table_exists(inspector, table_name) or not _has_column(inspector, table_name, "organization_id"):
        return
    existing_fks = {fk["name"] for fk in inspector.get_foreign_keys(table_name)}
    if constraint_name not in existing_fks:
        op.create_foreign_key(
            constraint_name,
            table_name,
            "organizations",
            ["organization_id"],
            ["id"],
        )


def _create_index_if_missing(table_name: str, index_name: str, columns: list[str]) -> None:
    inspector = _refresh_inspector()
    if not _table_exists(inspector, table_name):
        return
    if not _has_index(inspector, table_name, index_name):
        op.create_index(index_name, table_name, columns)


def upgrade() -> None:
    _ensure_default_org()
    default_org_id = _default_org_id()

    _add_org_column("recording_sessions", nullable=True)
    _create_org_fk("recording_sessions", "fk_recording_sessions_organization_id")
    op.get_bind().execute(
        sa.text(
            "UPDATE recording_sessions SET organization_id = :org_id WHERE organization_id IS NULL"
        ),
        {"org_id": default_org_id},
    )
    op.alter_column("recording_sessions", "organization_id", nullable=False)
    _create_index_if_missing(
        "recording_sessions",
        "ix_recording_sessions_organization_id",
        ["organization_id"],
    )

    _add_org_column("audio_files", nullable=True)
    _create_org_fk("audio_files", "fk_audio_files_organization_id")
    op.get_bind().execute(
        sa.text(
            """
            UPDATE audio_files af
            SET organization_id = rs.organization_id
            FROM recording_sessions rs
            WHERE af.organization_id IS NULL
              AND rs.session_id = af.session_id
            """
        )
    )
    op.get_bind().execute(
        sa.text(
            "UPDATE audio_files SET organization_id = :org_id WHERE organization_id IS NULL"
        ),
        {"org_id": default_org_id},
    )
    op.alter_column("audio_files", "organization_id", nullable=False)
    _create_index_if_missing("audio_files", "ix_audio_files_organization_id", ["organization_id"])

    _add_org_column("chat_history", nullable=True)
    _create_org_fk("chat_history", "fk_chat_history_organization_id")
    op.get_bind().execute(
        sa.text(
            """
            UPDATE chat_history ch
            SET organization_id = rs.organization_id
            FROM recording_sessions rs
            WHERE ch.organization_id IS NULL
              AND rs.session_id = ch.session_key
            """
        )
    )
    op.get_bind().execute(
        sa.text(
            """
            UPDATE chat_history ch
            SET organization_id = rs.organization_id
            FROM recording_sessions rs
            WHERE ch.organization_id IS NULL
              AND rs.id::text = ch.session_key
            """
        )
    )
    op.get_bind().execute(
        sa.text(
            "UPDATE chat_history SET organization_id = :org_id WHERE organization_id IS NULL"
        ),
        {"org_id": default_org_id},
    )
    op.alter_column("chat_history", "organization_id", nullable=False)
    _create_index_if_missing("chat_history", "ix_chat_history_organization_id", ["organization_id"])

    _add_org_column("custom_vocabulary", nullable=True)
    _create_org_fk("custom_vocabulary", "fk_custom_vocabulary_organization_id")
    op.get_bind().execute(
        sa.text(
            "UPDATE custom_vocabulary SET organization_id = :org_id WHERE organization_id IS NULL"
        ),
        {"org_id": default_org_id},
    )
    inspector = _refresh_inspector()
    if _has_unique_constraint(inspector, "custom_vocabulary", "uq_vocabulary_term_category"):
        op.drop_constraint("uq_vocabulary_term_category", "custom_vocabulary", type_="unique")
    op.alter_column("custom_vocabulary", "organization_id", nullable=False)
    op.create_unique_constraint(
        "uq_vocabulary_term_category",
        "custom_vocabulary",
        ["organization_id", "term", "category"],
    )
    _create_index_if_missing(
        "custom_vocabulary",
        "ix_custom_vocabulary_organization_id",
        ["organization_id"],
    )

    _add_org_column("vocabulary_sets", nullable=True)
    _create_org_fk("vocabulary_sets", "fk_vocabulary_sets_organization_id")
    op.get_bind().execute(
        sa.text(
            "UPDATE vocabulary_sets SET organization_id = :org_id WHERE organization_id IS NULL"
        ),
        {"org_id": default_org_id},
    )
    inspector = _refresh_inspector()
    if _has_unique_constraint(inspector, "vocabulary_sets", "vocabulary_sets_name_key"):
        op.drop_constraint("vocabulary_sets_name_key", "vocabulary_sets", type_="unique")
    if _has_unique_constraint(inspector, "vocabulary_sets", "uq_vocabulary_sets_org_name"):
        op.drop_constraint("uq_vocabulary_sets_org_name", "vocabulary_sets", type_="unique")
    op.alter_column("vocabulary_sets", "organization_id", nullable=False)
    op.create_unique_constraint(
        "uq_vocabulary_sets_org_name",
        "vocabulary_sets",
        ["organization_id", "name"],
    )
    _create_index_if_missing(
        "vocabulary_sets",
        "ix_vocabulary_sets_organization_id",
        ["organization_id"],
    )

    _add_org_column("session_vocabulary", nullable=True)
    _create_org_fk("session_vocabulary", "fk_session_vocabulary_organization_id")
    op.get_bind().execute(
        sa.text(
            """
            UPDATE session_vocabulary sv
            SET organization_id = rs.organization_id
            FROM recording_sessions rs
            WHERE sv.organization_id IS NULL
              AND rs.session_id = sv.session_id::text
            """
        )
    )
    op.get_bind().execute(
        sa.text(
            """
            UPDATE session_vocabulary sv
            SET organization_id = vs.organization_id
            FROM vocabulary_sets vs
            WHERE sv.organization_id IS NULL
              AND vs.id = sv.vocabulary_set_id
            """
        )
    )
    op.get_bind().execute(
        sa.text(
            "UPDATE session_vocabulary SET organization_id = :org_id WHERE organization_id IS NULL"
        ),
        {"org_id": default_org_id},
    )
    op.alter_column("session_vocabulary", "organization_id", nullable=False)
    _create_index_if_missing(
        "session_vocabulary",
        "ix_session_vocabulary_organization_id",
        ["organization_id"],
    )

    _add_org_column("satellite_devices", nullable=True)
    _create_org_fk("satellite_devices", "fk_satellite_devices_organization_id")
    op.get_bind().execute(
        sa.text(
            "UPDATE satellite_devices SET organization_id = :org_id WHERE organization_id IS NULL"
        ),
        {"org_id": default_org_id},
    )
    _create_index_if_missing(
        "satellite_devices",
        "ix_satellite_devices_organization_id",
        ["organization_id"],
    )

    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO user_organizations (user_id, organization_id, role, joined_at)
            SELECT
                users.id,
                :org_id,
                CASE WHEN users.is_superuser THEN 'admin' ELSE 'user' END,
                CURRENT_TIMESTAMP
            FROM users
            WHERE NOT EXISTS (
                SELECT 1
                FROM user_organizations uo
                WHERE uo.user_id = users.id
                  AND uo.organization_id = :org_id
            )
            """
        ),
        {"org_id": default_org_id},
    )
    inspector = _refresh_inspector()
    if not _has_unique_constraint(inspector, "user_organizations", "uq_user_organizations_user_org"):
        op.create_unique_constraint(
            "uq_user_organizations_user_org",
            "user_organizations",
            ["user_id", "organization_id"],
        )


def downgrade() -> None:
    inspector = _refresh_inspector()

    if _has_unique_constraint(inspector, "user_organizations", "uq_user_organizations_user_org"):
        op.drop_constraint("uq_user_organizations_user_org", "user_organizations", type_="unique")

    for table_name, index_name in [
        ("satellite_devices", "ix_satellite_devices_organization_id"),
        ("session_vocabulary", "ix_session_vocabulary_organization_id"),
        ("vocabulary_sets", "ix_vocabulary_sets_organization_id"),
        ("custom_vocabulary", "ix_custom_vocabulary_organization_id"),
        ("chat_history", "ix_chat_history_organization_id"),
        ("audio_files", "ix_audio_files_organization_id"),
        ("recording_sessions", "ix_recording_sessions_organization_id"),
    ]:
        inspector = _refresh_inspector()
        if _has_index(inspector, table_name, index_name):
            op.drop_index(index_name, table_name=table_name)

    inspector = _refresh_inspector()
    if _has_unique_constraint(inspector, "vocabulary_sets", "uq_vocabulary_sets_org_name"):
        op.drop_constraint("uq_vocabulary_sets_org_name", "vocabulary_sets", type_="unique")
    if not _has_unique_constraint(inspector, "vocabulary_sets", "vocabulary_sets_name_key"):
        op.create_unique_constraint("vocabulary_sets_name_key", "vocabulary_sets", ["name"])

    inspector = _refresh_inspector()
    if _has_unique_constraint(inspector, "custom_vocabulary", "uq_vocabulary_term_category"):
        op.drop_constraint("uq_vocabulary_term_category", "custom_vocabulary", type_="unique")
    op.create_unique_constraint("uq_vocabulary_term_category", "custom_vocabulary", ["term", "category"])

    for table_name, constraint_name in [
        ("satellite_devices", "fk_satellite_devices_organization_id"),
        ("session_vocabulary", "fk_session_vocabulary_organization_id"),
        ("vocabulary_sets", "fk_vocabulary_sets_organization_id"),
        ("custom_vocabulary", "fk_custom_vocabulary_organization_id"),
        ("chat_history", "fk_chat_history_organization_id"),
        ("audio_files", "fk_audio_files_organization_id"),
        ("recording_sessions", "fk_recording_sessions_organization_id"),
    ]:
        inspector = _refresh_inspector()
        if _has_column(inspector, table_name, "organization_id"):
            existing_fks = {fk["name"] for fk in inspector.get_foreign_keys(table_name)}
            if constraint_name in existing_fks:
                op.drop_constraint(constraint_name, table_name, type_="foreignkey")
            op.drop_column(table_name, "organization_id")
