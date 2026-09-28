"""PostgreSQL schema contract for the Stable transcript import mode."""

import ast
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text


def _configured_stable_ingest_mode() -> str:
    """Read the module constant without importing app/ORM state at collection."""

    source = Path(__file__).parents[1] / "api" / "stable_ingest.py"
    tree = ast.parse(source.read_text())
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "STABLE_INGEST_SESSION_MODE"
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("STABLE_INGEST_SESSION_MODE is not a literal module constant")


def test_stable_ingest_mode_is_accepted_by_postgres_schema():
    """Fail when application semantics drift from the live CHECK constraint.

    The regular API suite intentionally runs on SQLite. Release/CI lanes with
    PostgreSQL set MEETING_OPS_POSTGRES_CONTRACT_URL so this test queries the
    actual constraint rather than silently losing CHECK coverage.
    """

    mode = _configured_stable_ingest_mode()
    url = os.getenv("MEETING_OPS_POSTGRES_CONTRACT_URL")
    if not url:
        pytest.skip("MEETING_OPS_POSTGRES_CONTRACT_URL is required for this contract")

    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            definition = connection.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(oid)
                    FROM pg_constraint
                    WHERE conname = 'ck_recording_sessions_mode'
                    """
                )
            ).scalar_one()
    finally:
        engine.dispose()

    assert f"'{mode}'" in definition
