"""action_items first-class table

Revision ID: 021_action_items
Revises: 020_session_mode
Create Date: 2026-05-19 00:00:00.000000

Promotes action items out of `recording_sessions.final_summary` (and
`recording_sessions.ai_insights`) JSON blobs into a real per-row
table so:

  - the dashboard "Recent action items" panel can read directly from
    a single index-friendly query instead of fetching N session
    details and parsing JSON on the client;
  - users can flip status (todo / doing / done / cancelled) and have
    it stick;
  - future PWA notifications can target real rows.

The migration also backfills every existing session by walking
`final_summary` + `ai_insights` with the same logic the frontend
parser used to use (see `services.action_items_extractor`).

Idempotent: a re-run will not duplicate rows because the backfill
short-circuits when the session already has any rows.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


logger = logging.getLogger("alembic.runtime.migration")

revision = "021_action_items"
down_revision = "020_session_mode"
branch_labels = None
depends_on = None


# --- Inlined extractor ---------------------------------------------------
# Mirrors `services.action_items_extractor` so the migration is
# self-contained and runs even if the service module is unavailable at
# migration time (it would be, but keeping it inline avoids an import-
# time coupling on the live app code from inside alembic). Keep in sync.

_LIST_KEYS = ("action_items", "actions", "tasks", "action-items")
_REL_DAY_RX = re.compile(
    r"\b(today|tonight|tomorrow|next\s+week|this\s+week|end\s+of\s+(?:week|month))\b",
    re.IGNORECASE,
)


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        for k in ("action", "text", "title", "description", "task"):
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _pick_list(bucket: Any) -> List[Any]:
    if not isinstance(bucket, dict):
        return []
    for key in _LIST_KEYS:
        v = bucket.get(key)
        if isinstance(v, list) and v:
            return v
    return []


def _coerce_owner(raw: Any) -> Optional[str]:
    if not isinstance(raw, dict):
        return None
    for k in ("owner", "assignee", "assigned_to", "responsible"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:200]
    return None


def _coerce_due(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, dict):
        return None
    for k in ("due_date", "dueDate", "deadline", "due"):
        v = raw.get(k)
        if not isinstance(v, str):
            continue
        s = v.strip()
        if not s or s.lower() in {"not specified", "n/a", "tbd", "none", "null"}:
            continue
        try:
            iso = s[:-1] + "+00:00" if s.endswith("Z") else s
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        if _REL_DAY_RX.search(s):
            return None
    return None


def _iter_buckets(
    final_summary: Any, ai_insights: Any
) -> Iterable[tuple[str, dict]]:
    if isinstance(final_summary, dict):
        yield "final_summary", final_summary
    if isinstance(ai_insights, dict):
        yield "ai_insights", ai_insights


def _extract(final_summary: Any, ai_insights: Any) -> List[dict]:
    out: List[dict] = []
    seen: set[str] = set()
    for source_label, bucket in _iter_buckets(final_summary, ai_insights):
        for raw in _pick_list(bucket):
            text = _coerce_text(raw)
            if not text:
                continue
            key = re.sub(r"\s+", " ", text.lower())[:120]
            if key in seen:
                continue
            seen.add(key)
            owner = _coerce_owner(raw)
            due = _coerce_due(raw)
            payload = raw if isinstance(raw, dict) else {"text": text}
            out.append(
                {
                    "text": text[:4000],
                    "owner": owner,
                    "due_date": due,
                    "source": source_label,
                    "raw_payload": payload,
                }
            )
    return out


# --- DDL ----------------------------------------------------------------

def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "action_items" not in existing_tables:
        op.create_table(
            "action_items",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "session_id",
                sa.Integer(),
                sa.ForeignKey("recording_sessions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "organization_id",
                sa.Integer(),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("owner", sa.String(length=200), nullable=True),
            sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "status",
                sa.String(length=20),
                nullable=False,
                server_default="todo",
            ),
            sa.Column(
                "sort_order",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "source",
                sa.String(length=50),
                nullable=False,
                server_default="final_summary",
            ),
            sa.Column("raw_payload", JSONB(), nullable=True),
            sa.CheckConstraint(
                "status IN ('todo', 'doing', 'done', 'cancelled')",
                name="ck_action_items_status",
            ),
            sa.CheckConstraint(
                "source IN ('final_summary', 'ai_insights', 'manual')",
                name="ck_action_items_source",
            ),
        )

    # Indexes (idempotent). Backend's create_all() may have raced ahead and
    # built the table without these so the explicit names won't collide.
    existing_indexes = {
        ix["name"] for ix in inspector.get_indexes("action_items")
    } if "action_items" in inspector.get_table_names() else set()
    if "ix_action_items_session" not in existing_indexes:
        op.create_index(
            "ix_action_items_session", "action_items", ["session_id"]
        )
    if "ix_action_items_organization" not in existing_indexes:
        op.create_index(
            "ix_action_items_organization", "action_items", ["organization_id"]
        )
    if "ix_action_items_org_status" not in existing_indexes:
        op.create_index(
            "ix_action_items_org_status",
            "action_items",
            ["organization_id", "status"],
        )

    # CHECK constraints — these are emitted by alembic CREATE TABLE but
    # NOT by SQLAlchemy's Base.metadata.create_all() (which silently
    # drops them on this model definition because they're declared as
    # SchemaItem children of the table, not as Column-level checks).
    # If create_all built the table first, the constraints are missing
    # and we need to add them now.
    existing_checks = {
        cc["name"]
        for cc in inspector.get_check_constraints("action_items")
    } if "action_items" in inspector.get_table_names() else set()
    if "ck_action_items_status" not in existing_checks:
        op.create_check_constraint(
            "ck_action_items_status",
            "action_items",
            "status IN ('todo', 'doing', 'done', 'cancelled')",
        )
    if "ck_action_items_source" not in existing_checks:
        op.create_check_constraint(
            "ck_action_items_source",
            "action_items",
            "source IN ('final_summary', 'ai_insights', 'manual')",
        )

    # Ensure created_at has a server-side default. If create_all() built
    # the table first the column was emitted without DEFAULT — alembic
    # adds it explicitly here so plain inserts succeed.
    op.execute(
        "ALTER TABLE action_items "
        "ALTER COLUMN created_at SET DEFAULT now()"
    )

    _backfill_existing()


def _backfill_existing() -> None:
    """Walk every session that has summary JSON and INSERT action items.

    Idempotent: a session that already has rows is skipped so re-running
    the upgrade (e.g. across staging->prod) doesn't duplicate items."""
    conn = op.get_bind()

    sessions = conn.execute(
        sa.text(
            """
            SELECT
                rs.id,
                rs.organization_id,
                rs.final_summary,
                rs.ai_insights
            FROM recording_sessions rs
            WHERE rs.organization_id IS NOT NULL
              AND (rs.final_summary IS NOT NULL OR rs.ai_insights IS NOT NULL)
              AND NOT EXISTS (
                  SELECT 1 FROM action_items ai WHERE ai.session_id = rs.id
              )
            """
        )
    ).fetchall()

    inserted = 0
    for row in sessions:
        session_id, org_id, final_summary, ai_insights = row
        fs = _coerce_json(final_summary)
        ai = _coerce_json(ai_insights)
        items = _extract(fs, ai)
        if not items:
            continue

        for idx, item in enumerate(items):
            conn.execute(
                sa.text(
                    """
                    INSERT INTO action_items (
                        session_id, organization_id, text, owner, due_date,
                        status, sort_order, source, raw_payload
                    ) VALUES (
                        :session_id, :organization_id, :text, :owner, :due_date,
                        'todo', :sort_order, :source, CAST(:raw_payload AS JSONB)
                    )
                    """
                ),
                {
                    "session_id": session_id,
                    "organization_id": org_id,
                    "text": item["text"],
                    "owner": item["owner"],
                    "due_date": item["due_date"],
                    "sort_order": idx,
                    "source": item["source"],
                    "raw_payload": json.dumps(item["raw_payload"]),
                },
            )
            inserted += 1

    logger.info("021_action_items: backfilled %d items from %d sessions", inserted, len(sessions))


def _coerce_json(value: Any) -> Any:
    """The JSON column can come back as a parsed dict (psycopg2 with
    JSONB) or as a string (older driver/dialect). Normalize."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def downgrade() -> None:
    op.drop_index("ix_action_items_org_status", table_name="action_items")
    op.drop_index("ix_action_items_organization", table_name="action_items")
    op.drop_index("ix_action_items_session", table_name="action_items")
    op.drop_table("action_items")
