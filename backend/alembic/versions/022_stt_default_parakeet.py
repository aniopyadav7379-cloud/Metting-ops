"""Migrate STT provider default from local_whisper to parakeet-server.

Revision ID: 022_stt_default_parakeet
Revises: b18939734458
Create Date: 2026-05-19 00:00:00.000000

Backfills `org_provider_settings` rows where any org explicitly chose
the legacy local_whisper STT provider. The new code-level default in
ProviderRegistry.get_stt() is already `parakeet`, so orgs with no DB
row get Parakeet for free; this migration only matters for orgs that
proactively saved local_whisper / whisper through the AI Providers UI.

Rationale: the legacy `meet-whisper` container (whisper.cpp on bigboy
RTX 6000, ~2GB VRAM) was retired 2026-05-19. The cluster-wide cloud
STT path is Parakeet 1.1B on midboy2 (or browser Parakeet 0.6B for
the live always-on path). Orgs that customized to local_whisper get
silently migrated to parakeet so we don't strand uploads pointing at
an endpoint that no longer exists.

Endpoint + model fields get cleared when migrating; the registry will
then resolve PARAKEET_SERVER_URL + parakeet-tdt-1.1b from env defaults.
Any org that wants to keep whisper can flip provider back to
`local_whisper` and supply their own endpoint via Provider Settings.

Idempotent: re-run is a no-op once the rows have moved.

Down: revert provider_name back to local_whisper, leaving endpoint /
model blank (we can't recover what they used to be). Users would have
to re-enter custom endpoints. Acceptable: down is an emergency rollback,
not a normal workflow.
"""
from __future__ import annotations

import logging

from alembic import op
from sqlalchemy import text


# revision identifiers
revision = "022_stt_default_parakeet"
down_revision = "b18939734458"
branch_labels = None
depends_on = None


logger = logging.getLogger("alembic.022_stt_default_parakeet")


def upgrade() -> None:
    conn = op.get_bind()
    # Count rows first for logging/audit. We move every row pointing at
    # the legacy whisper provider regardless of when it was last updated:
    # the underlying container no longer exists, so leaving them would
    # mean broken uploads. Anyone who explicitly wants whisper can flip
    # it back through the AI Providers settings panel.
    matching = conn.execute(
        text(
            "SELECT id, organization_id, provider_name, endpoint_url, model_name "
            "FROM org_provider_settings "
            "WHERE service_kind = 'stt' "
            "  AND lower(provider_name) IN ('local_whisper', 'whisper')"
        )
    ).fetchall()
    if matching:
        logger.info(
            "Migrating %d STT provider row(s) from local_whisper -> parakeet",
            len(matching),
        )
        for row in matching:
            logger.info("  org_id=%s prev=%s endpoint=%s model=%s",
                        row.organization_id, row.provider_name,
                        row.endpoint_url, row.model_name)
    else:
        logger.info("No legacy local_whisper STT rows to migrate; no-op.")

    conn.execute(
        text(
            "UPDATE org_provider_settings "
            "SET provider_name = 'parakeet', "
            "    endpoint_url = NULL, "
            "    model_name = NULL, "
            "    updated_at = NOW() "
            "WHERE service_kind = 'stt' "
            "  AND lower(provider_name) IN ('local_whisper', 'whisper')"
        )
    )


def downgrade() -> None:
    # Best-effort revert: name only; can't recover custom endpoint/model.
    conn = op.get_bind()
    conn.execute(
        text(
            "UPDATE org_provider_settings "
            "SET provider_name = 'local_whisper', "
            "    updated_at = NOW() "
            "WHERE service_kind = 'stt' "
            "  AND lower(provider_name) = 'parakeet'"
        )
    )
