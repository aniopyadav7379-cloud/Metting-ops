"""Guard the local-then-upload data-loss fix in the session watchdog.

Under the v3.26.9 "buffer in browser, upload at Stop" recording model, a
desktop session that is still recording (or briefly offline) has NO server
audio and NO server transcript — so ``_alwayson_session_is_empty`` reports it
empty even though the user's browser holds the whole meeting and can re-upload
it via the orphan banner. The watchdog must therefore only HARD-DELETE empty
always-on rows that are OLD enough that a client-side resume is implausible;
recent ones flip to ``failed`` (row preserved -> resume still resolves).

This pins ``_empty_alwayson_safe_to_delete`` so the 501 landmine can't recur.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from services.session_watchdog import (
    DEFAULT_EMPTY_DELETE_MINUTES,
    _empty_alwayson_safe_to_delete,
    _empty_delete_threshold_minutes,
)

NOW = datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)


def _sess(created_at):
    # started_at intentionally absent on most rows so we exercise the
    # created_at branch; the helper falls back to started_at when needed.
    return SimpleNamespace(created_at=created_at, started_at=None)


def test_recent_empty_is_not_deleted():
    """A just-started recording (5 min old) must NOT be hard-deleted —
    its audio is still in the browser; deleting the row would 404 resume."""
    recent = _sess(NOW - timedelta(minutes=5))
    assert _empty_alwayson_safe_to_delete(recent, NOW) is False


def test_idle_but_recent_empty_is_not_deleted():
    """Idle for well past the 120-min reap threshold but created only 90 min
    ago (e.g. laptop slept mid-recording) — still inside the recovery window,
    so flip-to-failed, never delete."""
    row = _sess(NOW - timedelta(minutes=90))
    assert _empty_alwayson_safe_to_delete(row, NOW) is False


def test_old_empty_is_deletable():
    """A genuinely abandoned empty from 2 days ago is safe to hard-delete."""
    old = _sess(NOW - timedelta(days=2))
    assert _empty_alwayson_safe_to_delete(old, NOW) is True


def test_exactly_at_threshold_is_deletable():
    edge = _sess(NOW - timedelta(minutes=DEFAULT_EMPTY_DELETE_MINUTES))
    assert _empty_alwayson_safe_to_delete(edge, NOW) is True


def test_missing_timestamp_is_conservative():
    """No timestamp to reason about -> never delete (flip-to-failed instead)."""
    assert _empty_alwayson_safe_to_delete(_sess(None), NOW) is False


def test_started_at_fallback():
    """created_at NULL but started_at present -> use started_at."""
    row = SimpleNamespace(created_at=None, started_at=NOW - timedelta(days=3))
    assert _empty_alwayson_safe_to_delete(row, NOW) is True


def test_naive_timestamp_treated_as_utc():
    """A tz-naive created_at must not raise (it's coerced to UTC)."""
    row = SimpleNamespace(
        created_at=(NOW - timedelta(days=2)).replace(tzinfo=None),
        started_at=None,
    )
    assert _empty_alwayson_safe_to_delete(row, NOW) is True


def test_default_threshold_is_24h():
    assert _empty_delete_threshold_minutes() == DEFAULT_EMPTY_DELETE_MINUTES == 24 * 60
