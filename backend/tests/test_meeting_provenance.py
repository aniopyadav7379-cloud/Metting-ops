"""Unit tests for services.meeting_provenance — the when/where/who ledger."""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest

from services import meeting_provenance as mp


UP = datetime(2026, 6, 6, 1, 0, 0, tzinfo=timezone.utc)  # upload time
NOW = datetime(2026, 6, 6, 2, 0, 0, tzinfo=timezone.utc)


def test_parse_iso_variants():
    assert mp._parse_iso("2026-06-04T21:51:03.000000Z").isoformat() == "2026-06-04T21:51:03+00:00"
    assert mp._parse_iso("2026-06-04 21:51:03").tzinfo is not None
    assert mp._parse_iso("2026-06-04").date() == date(2026, 6, 4)
    assert mp._parse_iso("") is None
    assert mp._parse_iso("garbage") is None


def test_candidates_ranked_by_confidence(monkeypatch):
    # audio metadata present -> highest; no file mtime (no path stat)
    monkeypatch.setattr(mp, "probe_recorded_at",
                        lambda p, **k: datetime(2026, 6, 4, 21, 51, tzinfo=timezone.utc))
    monkeypatch.setattr(mp, "_mtime", lambda p: datetime(2026, 6, 5, tzinfo=timezone.utc))
    cands = mp.build_when_candidates(
        audio_path="/x.m4a", filename_date=date(2026, 6, 4), uploaded_at=UP)
    sources = [c["source"] for c in cands]
    assert sources == ["audio_metadata.creation_time", "filename", "file_mtime", "upload_time"]
    assert cands[0]["confidence"] == mp.CONF_AUDIO_META
    # always includes upload floor
    assert sources[-1] == "upload_time"


def test_candidates_upload_floor_only(monkeypatch):
    monkeypatch.setattr(mp, "probe_recorded_at", lambda p, **k: None)
    cands = mp.build_when_candidates(audio_path=None, filename_date=None, uploaded_at=UP)
    assert [c["source"] for c in cands] == ["upload_time"]


def test_choose_recorded_at_prefers_non_floor():
    cands = [
        {"value": "a", "source": "audio_metadata.creation_time", "confidence": 0.95},
        {"value": "u", "source": "upload_time", "confidence": 0.3},
    ]
    assert mp.choose_recorded_at(cands)["source"] == "audio_metadata.creation_time"
    # only floor -> returns floor
    assert mp.choose_recorded_at([cands[1]])["source"] == "upload_time"
    assert mp.choose_recorded_at([]) is None


def test_recorded_date_time_drops_midnight_from_filename():
    sig = {"value": "2026-06-04T00:00:00+00:00", "source": "filename", "confidence": 0.7}
    d, t = mp.recorded_date_time(sig)
    assert d == date(2026, 6, 4) and t is None  # no fake 00:00 meeting time
    sig2 = {"value": "2026-06-04T21:51:03+00:00", "source": "audio_metadata.creation_time", "confidence": 0.95}
    d2, t2 = mp.recorded_date_time(sig2)
    assert d2 == date(2026, 6, 4) and t2 == time(21, 51, 3)
    assert mp.recorded_date_time(None) == (None, None)


def test_build_provenance_full(monkeypatch):
    monkeypatch.setattr(mp, "probe_recorded_at",
                        lambda p, **k: datetime(2026, 6, 4, 21, 51, 3, tzinfo=timezone.utc))
    monkeypatch.setattr(mp, "_mtime", lambda p: None)
    prov = mp.build_meeting_provenance(
        audio_path="/x.m4a",
        original_filename="VoiceMemo 2026-06-04.m4a",
        filename_date=date(2026, 6, 4),
        filename_time=None,
        uploaded_at=UP,
        source_type="upload",
        recorder={"name": "Aaron", "email": "a@x.com", "contact_id": None},
        now=NOW,
    )
    assert prov["schema"] == mp.PROVENANCE_SCHEMA
    assert prov["built_at"] == NOW.isoformat()
    assert prov["when"]["recorded_at"]["source"] == "audio_metadata.creation_time"
    assert prov["when"]["uploaded_at"]["confidence"] == 1.0
    assert {c["source"] for c in prov["when"]["candidates"]} >= {
        "audio_metadata.creation_time", "filename", "upload_time"}
    assert prov["where"]["source_type"]["value"] == "upload"
    assert prov["where"]["original_filename"]["value"] == "VoiceMemo 2026-06-04.m4a"
    assert prov["who"]["recorder"]["value"]["email"] == "a@x.com"


CLIENT_MOD = datetime(2026, 5, 20, 14, 30, 0, tzinfo=timezone.utc)  # File.lastModified


def test_client_modified_at_ranks_above_file_mtime(monkeypatch):
    # No embedded metadata, no filename date: the browser File.lastModified is
    # the real "file date" and must beat both the server mtime (= ingest time
    # for an upload) and the upload floor.
    monkeypatch.setattr(mp, "probe_recorded_at", lambda p, **k: None)
    monkeypatch.setattr(mp, "_mtime", lambda p: datetime(2026, 6, 6, 1, 0, tzinfo=timezone.utc))
    cands = mp.build_when_candidates(
        audio_path="/x.m4a", filename_date=None, uploaded_at=UP, client_modified_at=CLIENT_MOD)
    assert [c["source"] for c in cands] == ["client_file_mtime", "file_mtime", "upload_time"]
    chosen = mp.choose_recorded_at(cands)
    assert chosen["source"] == "client_file_mtime"
    assert chosen["value"] == CLIENT_MOD.isoformat()


def test_filename_date_beats_client_modified_at(monkeypatch):
    # An explicit date in the filename is stronger than last-modified.
    monkeypatch.setattr(mp, "probe_recorded_at", lambda p, **k: None)
    monkeypatch.setattr(mp, "_mtime", lambda p: None)
    cands = mp.build_when_candidates(
        audio_path="/x.m4a", filename_date=date(2026, 6, 4), uploaded_at=UP,
        client_modified_at=CLIENT_MOD)
    assert mp.choose_recorded_at(cands)["source"] == "filename"
    assert mp.CONF_FILENAME > mp.CONF_CLIENT_MTIME > mp.CONF_FILE_MTIME > mp.CONF_UPLOAD_TIME


def test_client_modified_at_naive_is_normalized_to_utc(monkeypatch):
    monkeypatch.setattr(mp, "probe_recorded_at", lambda p, **k: None)
    monkeypatch.setattr(mp, "_mtime", lambda p: None)
    naive = datetime(2026, 5, 20, 14, 30, 0)  # no tzinfo
    cands = mp.build_when_candidates(
        audio_path=None, filename_date=None, uploaded_at=UP, client_modified_at=naive)
    sig = mp.choose_recorded_at(cands)
    assert sig["source"] == "client_file_mtime"
    assert sig["value"] == "2026-05-20T14:30:00+00:00"


def test_build_provenance_uses_client_modified_at_for_meeting_date(monkeypatch):
    # End-to-end: no embedded metadata, no filename date -> meeting_date comes
    # from the browser's File.lastModified, NOT the upload time.
    monkeypatch.setattr(mp, "probe_recorded_at", lambda p, **k: None)
    monkeypatch.setattr(mp, "_mtime", lambda p: None)
    prov = mp.build_meeting_provenance(
        audio_path="/x.m4a", original_filename="standup.m4a",
        filename_date=None, filename_time=None, uploaded_at=UP,
        source_type="upload", client_modified_at=CLIENT_MOD, now=NOW)
    recorded = prov["when"]["recorded_at"]
    assert recorded["source"] == "client_file_mtime"
    d, t = mp.recorded_date_time(recorded)
    assert d == date(2026, 5, 20) and t == time(14, 30, 0)


def test_probe_recorded_at_handles_missing_ffprobe(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no ffprobe")
    monkeypatch.setattr(mp.subprocess, "run", _boom)
    assert mp.probe_recorded_at("/x.m4a") is None


def test_probe_recorded_at_parses_creation_time(monkeypatch):
    class _P:
        returncode = 0
        stdout = '{"format": {"tags": {"creation_time": "2026-06-04T21:51:03.000000Z"}}}'
    monkeypatch.setattr(mp.subprocess, "run", lambda *a, **k: _P())
    dt = mp.probe_recorded_at("/x.m4a")
    assert dt.year == 2026 and dt.hour == 21 and dt.minute == 51
