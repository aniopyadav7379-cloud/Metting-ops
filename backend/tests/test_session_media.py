"""Unit tests for services.session_media — session-row ↔ Garage glue.

media_storage is monkeypatched so these are pure logic tests (no infra):
persist records the durable location, resolve prefers local then Garage,
purge targets the right prefix, and everything is best-effort (never raises).
"""
from types import SimpleNamespace

import pytest

import services.session_media as sm


class FakeDB:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def make_session(tmp_path, *, with_audio=True):
    audio = None
    if with_audio:
        audio = tmp_path / "session.wav"
        audio.write_bytes(b"RIFFwav-data")
    return SimpleNamespace(
        id=42,
        session_id="sess-uuid",
        organization_id=3,
        audio_file=str(audio) if audio else None,
        audio_storage_backend=None,
        audio_object_key=None,
    )


def test_persist_records_garage_location_on_success(tmp_path, monkeypatch):
    sess = make_session(tmp_path)
    db = FakeDB()
    captured = {}

    def fake_upload(*, key, path, content_type=None):
        captured["key"] = key
        captured["path"] = str(path)
        return True

    monkeypatch.setattr(sm.media_storage, "upload_to_garage", fake_upload)
    backend = sm.persist_session_audio(db, sess)
    assert backend == "garage"
    assert sess.audio_storage_backend == "garage"
    assert sess.audio_object_key == "3/sess-uuid/audio/session.wav"
    assert captured["key"] == "3/sess-uuid/audio/session.wav"
    assert db.commits == 1


def test_persist_leaves_columns_null_when_garage_fails(tmp_path, monkeypatch):
    sess = make_session(tmp_path)
    db = FakeDB()
    monkeypatch.setattr(sm.media_storage, "upload_to_garage", lambda **k: False)
    backend = sm.persist_session_audio(db, sess)
    assert backend is None
    assert sess.audio_storage_backend is None
    assert sess.audio_object_key is None
    assert db.commits == 0  # nothing to commit


def test_persist_noop_without_local_file(tmp_path, monkeypatch):
    sess = make_session(tmp_path, with_audio=False)
    db = FakeDB()
    called = {"n": 0}
    monkeypatch.setattr(
        sm.media_storage, "upload_to_garage",
        lambda **k: called.__setitem__("n", called["n"] + 1) or True,
    )
    assert sm.persist_session_audio(db, sess) is None
    assert called["n"] == 0  # never attempted an upload


def test_persist_never_raises(tmp_path, monkeypatch):
    sess = make_session(tmp_path)
    db = FakeDB()

    def boom(**k):
        raise RuntimeError("garage exploded")

    monkeypatch.setattr(sm.media_storage, "upload_to_garage", boom)
    # must swallow and return None, not propagate
    assert sm.persist_session_audio(db, sess) is None


def test_resolve_prefers_existing_local_file(tmp_path, monkeypatch):
    sess = make_session(tmp_path)
    sess.audio_object_key = "3/sess-uuid/audio/session.wav"
    sess.audio_storage_backend = "garage"
    # Garage fetch must NOT be called when the local file is present
    monkeypatch.setattr(
        sm.media_storage, "cached_local_path",
        lambda **k: (_ for _ in ()).throw(AssertionError("should not fetch")),
    )
    resolved = sm.resolve_local_path(sess)
    assert resolved is not None and resolved.exists()


def test_resolve_falls_back_to_garage_when_local_gone(tmp_path, monkeypatch):
    sess = make_session(tmp_path, with_audio=False)
    sess.audio_file = str(tmp_path / "missing.wav")  # does not exist
    sess.audio_object_key = "3/sess-uuid/audio/session.wav"
    sess.audio_storage_backend = "garage"
    fetched = tmp_path / "from-garage.wav"
    fetched.write_bytes(b"pulled")
    monkeypatch.setattr(sm.media_storage, "cached_local_path", lambda **k: fetched)
    resolved = sm.resolve_local_path(sess)
    assert resolved == fetched


def test_resolve_none_when_nothing_available(tmp_path):
    sess = make_session(tmp_path, with_audio=False)
    assert sm.resolve_local_path(sess) is None


def test_purge_targets_session_prefix(tmp_path, monkeypatch):
    sess = make_session(tmp_path)
    seen = {}

    def fake_delete_prefix(*, prefix):
        seen["prefix"] = prefix
        return 5

    monkeypatch.setattr(sm.media_storage, "delete_prefix", fake_delete_prefix)
    removed = sm.purge_session_media(sess)
    assert removed == 5
    assert seen["prefix"] == "3/sess-uuid/"
