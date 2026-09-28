"""Unit tests for services.media_retention — local-disk reclamation.

Focus on the new, safety-relevant logic: the LRU cache cap, the disabled
guard, and the garage-off safety (eviction must no-op, never crash, never
delete without a verified Garage copy). The full DB eviction loop is exercised
live by the cutover (scripts/evict_local_audio.py) and shares the same
verify-then-delete contract.
"""
import importlib
import os
import time

import pytest


@pytest.fixture()
def retention(tmp_path, monkeypatch):
    import services.media_storage as ms
    importlib.reload(ms)
    monkeypatch.setattr(ms, "MEDIA_CACHE_ROOT", tmp_path / "media-cache")
    import services.media_retention as mr
    importlib.reload(mr)
    return mr, ms


def _make_cache_file(root, rel, size, atime):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    os.utime(p, (atime, atime))
    return p


def test_cap_media_cache_lru_evicts_oldest_until_under_budget(retention):
    mr, ms = retention
    root = ms.MEDIA_CACHE_ROOT
    now = time.time()
    # 3 files of 1000 bytes each; oldest -> newest by atime
    old = _make_cache_file(root, "a/old.wav", 1000, now - 3000)
    mid = _make_cache_file(root, "a/mid.wav", 1000, now - 2000)
    new = _make_cache_file(root, "a/new.wav", 1000, now - 1000)
    # budget 1500 bytes: must evict the two oldest (old, mid), keep new
    summary = mr.cap_media_cache(max_bytes=1500)
    assert summary["cache_bytes_before"] == 3000
    assert summary["removed"] == 2
    assert summary["freed_bytes"] == 2000
    assert not old.exists() and not mid.exists()
    assert new.exists()


def test_cap_media_cache_noop_when_under_budget(retention):
    mr, ms = retention
    _make_cache_file(ms.MEDIA_CACHE_ROOT, "b/x.wav", 500, time.time())
    summary = mr.cap_media_cache(max_bytes=10_000)
    assert summary["removed"] == 0
    assert summary["cache_bytes_before"] == 500


def test_cap_media_cache_dry_run_keeps_files(retention):
    mr, ms = retention
    f = _make_cache_file(ms.MEDIA_CACHE_ROOT, "c/x.wav", 2000, time.time() - 100)
    summary = mr.cap_media_cache(max_bytes=100, dry_run=True)
    assert summary["removed"] == 1  # would remove
    assert f.exists()              # but didn't


def test_cap_media_cache_ignores_partial_files(retention):
    mr, ms = retention
    root = ms.MEDIA_CACHE_ROOT
    _make_cache_file(root, "d/done.wav", 1000, time.time() - 100)
    _make_cache_file(root, "d/partial.wav.dl", 5000, time.time() - 200)
    _make_cache_file(root, "d/partial.wav.uploading", 5000, time.time() - 300)
    summary = mr.cap_media_cache(max_bytes=10_000)
    # only the real .wav counts toward cache size; partials are skipped
    assert summary["cache_bytes_before"] == 1000


def test_run_retention_disabled(retention, monkeypatch):
    mr, _ = retention
    monkeypatch.setenv("MEDIA_RETENTION_ENABLED", "false")
    assert mr.run_retention() == {"enabled": False}


def test_evict_completed_local_safe_when_garage_off(retention, monkeypatch):
    mr, ms = retention
    monkeypatch.setattr(ms, "GARAGE_ENDPOINT_URL", "")  # garage not configured
    summary = mr.evict_completed_local()
    assert summary["evicted"] == 0 and summary["freed_bytes"] == 0
