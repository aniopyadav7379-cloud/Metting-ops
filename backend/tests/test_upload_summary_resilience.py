"""Upload pipeline summary resilience.

A summary-LLM failure (timeout / model unavailable) must NOT discard a
successful transcript + diarization. The upload pipeline flags the session
``needs_summary`` (the same marker the always-on finalize path uses — see
workers/finalize_workers.py + services/session_watchdog.py) and completes the
upload; the summary is then retried automatically or via a manual reprocess.

These cover the failure-marking helper directly. api.uploads is imported INSIDE
each test (not at collection time) to avoid binding to pre-reload models and
splitting the SQLAlchemy mapper registry against the app fixture (see
tests/conftest.py).
"""


def test_flag_summary_needs_retry_sets_markers_and_preserves_metadata():
    import api.uploads as uploads

    class _S:
        processing_metadata = {"meeting_provenance": {"x": 1}}

    s = _S()
    uploads._flag_summary_needs_retry(s, RuntimeError("LLM gateway unavailable"))
    assert s.processing_metadata["needs_summary"] is True
    assert "LLM gateway unavailable" in s.processing_metadata["summary_error"]
    # Existing metadata (e.g. the provenance ledger) must be preserved.
    assert s.processing_metadata["meeting_provenance"] == {"x": 1}


def test_flag_summary_needs_retry_handles_none_metadata():
    import api.uploads as uploads

    class _S:
        processing_metadata = None

    s = _S()
    uploads._flag_summary_needs_retry(s, TimeoutError("summary timed out"))
    assert s.processing_metadata["needs_summary"] is True
    assert "timed out" in s.processing_metadata["summary_error"]


def test_flag_summary_needs_retry_truncates_long_errors():
    import api.uploads as uploads

    class _S:
        processing_metadata = {}

    s = _S()
    uploads._flag_summary_needs_retry(s, RuntimeError("x" * 2000))
    assert s.processing_metadata["needs_summary"] is True
    assert len(s.processing_metadata["summary_error"]) <= 500
