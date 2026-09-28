"""Unit tests for services.media_storage — the Garage media client.

Garage-less (local-fallback) round-trips so they run anywhere, plus key
convention + delete behavior. The live-Garage path is exercised separately
in the deploy round-trip check, not here.
"""
import importlib
import io
import os

import pytest


@pytest.fixture()
def local_media(tmp_path, monkeypatch):
    """media_storage with Garage NOT configured → pure local backend rooted
    at a temp cache dir."""
    import services.media_storage as m
    importlib.reload(m)
    monkeypatch.setattr(m, "GARAGE_ENDPOINT_URL", "")
    monkeypatch.setattr(m, "GARAGE_ACCESS_KEY", "")
    monkeypatch.setattr(m, "GARAGE_SECRET_KEY", "")
    monkeypatch.setattr(m, "MEDIA_CACHE_ROOT", tmp_path / "media-cache")
    monkeypatch.setattr(m, "_s3_client", None)
    return m


def test_media_key_convention(local_media):
    m = local_media
    assert m.media_key(7, "sess-abc", "session.wav") == "7/sess-abc/audio/session.wav"
    assert m.media_key(7, "sess-abc", "voice.mp3", kind="tts") == "7/sess-abc/tts/voice.mp3"
    # path traversal / dirnames are stripped to a basename
    assert m.media_key(1, "s", "../../etc/passwd") == "1/s/audio/passwd"


def test_preferred_backend_local_when_unconfigured(local_media):
    assert local_media.garage_configured() is False
    assert local_media.preferred_backend() == "local"


def test_put_stream_and_read_roundtrip_local(local_media):
    m = local_media
    key = m.media_key(0, "rt", "blob.bin")
    data = os.urandom(20000)
    backend = m.put_stream(key=key, stream=io.BytesIO(data))
    assert backend == "local"
    got = m.open_object(backend="local", key=key).read()
    assert got == data
    # cached_local_path for a local object is just the cache path
    p = m.cached_local_path(backend="local", key=key)
    assert p.exists() and p.read_bytes() == data


def test_put_path_local_copies_into_cache(local_media, tmp_path):
    m = local_media
    src = tmp_path / "source.wav"
    src.write_bytes(b"RIFF1234")
    key = m.media_key(0, "rt", "source.wav")
    backend = m.put_path(key=key, path=src)
    assert backend == "local"
    assert m.open_object(backend="local", key=key).read() == b"RIFF1234"


def test_iter_object_streams_chunks(local_media):
    m = local_media
    key = m.media_key(0, "rt", "stream.bin")
    data = b"abcdefghij" * 1000
    m.put_stream(key=key, stream=io.BytesIO(data))
    out = b"".join(m.iter_object(backend="local", key=key, chunk=256))
    assert out == data


def test_delete_object_and_prefix_local(local_media):
    m = local_media
    k1 = m.media_key(0, "del", "a.bin")
    k2 = m.media_key(0, "del", "b.bin", kind="tts")
    m.put_stream(key=k1, stream=io.BytesIO(b"a"))
    m.put_stream(key=k2, stream=io.BytesIO(b"b"))
    m.delete_object(backend="local", key=k1)
    assert not (m.MEDIA_CACHE_ROOT / k1).exists()
    # prefix delete clears the whole session subtree
    removed = m.delete_prefix(prefix="0/del/")
    # local-only: prefix delete returns 0 from Garage but clears local cache
    assert not (m.MEDIA_CACHE_ROOT / "0" / "del").exists()
    assert removed == 0
