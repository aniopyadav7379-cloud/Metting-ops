"""Consolidate Meeting-Ops LLM consumers onto Qwen 3.6 35B-A3B-Vision.

Revision ID: 026_qwen36_consolidation
Revises: 025_session_attachments
Create Date: 2026-05-21 04:30:00.000000

Aligns the `unified_meeting_agents` "Meeting Assistant" row with the rest
of the Meeting-Ops LLM consumer stack:

  - All 5 service-layer consumers (auto_summarization, summary_slices,
    digests, ai_insights, meeting_rag) route through ProviderRegistry,
    which honors the MEETING_OPS_LLM_URL + MEETING_OPS_LLM_MODEL direct
    route. Both env vars point at midboy1's dedicated Qwen 3.6 Vision
    endpoint (http://llm-gateway:8088/v1).
  - The 6th consumer — the Meeting Assistant chat persona — reads its
    model from this DB row at runtime for display purposes. Before this
    migration the row claimed `provider_type=ollama` + `model_name=
    Qwen3.6-35B-A3B-Vision`, which was misleading: actual routing went
    through ProviderRegistry (LiteLLM-style http), not through any ollama
    client. The metadata told a different story than the runtime.

This migration updates the row to `provider_type=litellm` so the UI +
logs reflect what the code actually does. Model name is unchanged
(Qwen3.6-35B-A3B-Vision) since that's the model the direct-route env
points at and the bench-validated consolidation target.

The companion code change (commit at branch head) flips the Python and
.env defaults from gemma-4-26b-moe to Qwen3.6-35B-A3B-Vision so that any
org that explicitly picks provider_type=litellm without specifying a
model also gets the consolidated default.

Benchmark — full results in backend/scripts/qwen36_consolidation_bench.py:

  Test               | gemma-4-26b-moe        | Qwen3.6-35B-A3B-Vision
  -------------------+------------------------+------------------------
  Final summary      | 38.2s, 21 tok/s        | 9.0s, 46 tok/s
                     | missed both action     | both action item
                     | item attributions      | attributions correct
  Slice summary      | 5.9s, <think> bled     | 5.4s, clean output
                     | $40k hallucinated $4k  | accurate dollar figure
  Tool use           | works                  | works, +limit arg
  Latency (5 prompts)| ttfb 0.74s, 14.8 tok/s | ttfb 0.25s, 31.9 tok/s

Idempotent: re-run is a no-op (matches on the current metadata, leaves
already-consolidated rows alone).

Down: restore the legacy ollama metadata. Down is an emergency rollback
only — the .env + code defaults stay on Qwen 3.6 until reverted in a
separate change.
"""
from __future__ import annotations

import logging

from alembic import op
from sqlalchemy import text


# revision identifiers
revision = "026_qwen36_consolidation"
down_revision = "025_session_attachments"
branch_labels = None
depends_on = None


logger = logging.getLogger("alembic.026_qwen36_consolidation")


def upgrade() -> None:
    conn = op.get_bind()
    # Idempotent guard: match on the pre-consolidation provider_type so
    # re-running after the row is already consolidated is a no-op.
    matching = conn.execute(
        text(
            "SELECT id, name, provider_type, model_name "
            "FROM unified_meeting_agents "
            "WHERE provider_type = 'ollama' "
            "  AND model_name IN ('Qwen3.6-35B-A3B-Vision', 'Qwen3.6-35B-A3B', 'qwen3.6-35b-moe')"
        )
    ).fetchall()
    if matching:
        logger.info(
            "Migrating %d unified_meeting_agents row(s) to provider_type=litellm",
            len(matching),
        )
        for row in matching:
            logger.info(
                "  id=%s name=%s prev_provider=%s model=%s",
                row.id, row.name, row.provider_type, row.model_name,
            )
    else:
        logger.info(
            "No ollama+Qwen3.6 rows in unified_meeting_agents; "
            "migration is a no-op."
        )

    conn.execute(
        text(
            "UPDATE unified_meeting_agents "
            "SET provider_type = 'litellm', "
            "    model_name    = 'Qwen3.6-35B-A3B-Vision', "
            "    updated_at    = NOW() "
            "WHERE provider_type = 'ollama' "
            "  AND model_name IN ('Qwen3.6-35B-A3B-Vision', 'Qwen3.6-35B-A3B', 'qwen3.6-35b-moe')"
        )
    )


def downgrade() -> None:
    # Best-effort revert. Restores the legacy ollama metadata for any
    # row that currently carries the consolidated litellm+Qwen 3.6 Vision
    # shape. Use only as an emergency rollback companion to reverting
    # the .env + code changes.
    conn = op.get_bind()
    conn.execute(
        text(
            "UPDATE unified_meeting_agents "
            "SET provider_type = 'ollama', "
            "    updated_at    = NOW() "
            "WHERE provider_type = 'litellm' "
            "  AND model_name = 'Qwen3.6-35B-A3B-Vision'"
        )
    )
