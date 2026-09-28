"""Map-reduce summary coverage honesty (audit: long-meeting summary correctness).

`_summarize_mapreduce` reduces whatever chunk digests succeed. Previously the
caller then stamped `summary_truncated=False` even when a chunk's map call
failed — silently omitting a time range while claiming full coverage. The
function now returns the dropped chunks so the caller can stamp truncation +
the missing ranges. These tests exercise that contract directly with a mock LLM
(no network).
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _reloaded_app(app):
    """Run the conftest model-reload (the session ``app`` fixture, which reloads
    the model graph and imports ``main`` → all routers) BEFORE these tests touch
    ``api.uploads``. Without it a pure-unit test that runs first would bind the
    app modules to the pre-reload Base, causing a mapper split in later DB tests."""
    return app


class _MockLLM:
    """chat() succeeds for CHUNK_OK map prompts, raises for CHUNK_FAIL, and
    returns a non-empty synthesis for the reduce prompt."""

    async def chat(self, *, system_prompt, user_prompt, max_tokens, temperature, extra_params=None):
        if "CHUNK_FAIL" in user_prompt:
            raise RuntimeError("simulated chunk map failure")
        if "CHUNK_OK" in user_prompt:
            return (
                '{"key_points": ["a point"], "decisions": [], "action_items": [], '
                '"quotes": [], "open_questions": [], "notable": []}'
            )
        return "## Summary\nFinal synthesized notes covering the meeting."


def _run(chunks):
    from api import uploads  # lazy: avoid binding app modules at collection time
    return asyncio.run(
        uploads._summarize_mapreduce(
            _MockLLM(),
            system_prompt="sys",
            speaker_context="",
            chunks=chunks,
            style_prefix="",
        )
    )


def test_mapreduce_reports_dropped_chunk():
    chunks = [
        {"index": 0, "start": 0.0, "end": 60.0, "text": "CHUNK_OK one", "speakers": ["Alice"]},
        {"index": 1, "start": 60.0, "end": 120.0, "text": "CHUNK_FAIL two", "speakers": ["Bob"]},
    ]
    text, missing = _run(chunks)
    assert text.strip()
    assert len(missing) == 1
    assert missing[0]["index"] == 1
    assert "–" in missing[0]["range"]  # a clock range was recorded


def test_mapreduce_full_coverage_when_all_succeed():
    chunks = [
        {"index": 0, "start": 0.0, "end": 60.0, "text": "CHUNK_OK a", "speakers": []},
        {"index": 1, "start": 60.0, "end": 120.0, "text": "CHUNK_OK b", "speakers": []},
    ]
    text, missing = _run(chunks)
    assert text.strip()
    assert missing == []
