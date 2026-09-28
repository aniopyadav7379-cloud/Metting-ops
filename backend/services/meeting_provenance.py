"""Meeting provenance — the when/where/who identity signals for a session.

A meeting's "identity" is assembled from several signals of varying
reliability, and we want to keep ALL of them (not just the winner) so the
federation, the cockpit, and future agents can reason about *how confident*
we are about when/where/who a meeting was. This module:

  * extracts the embedded recording timestamp from the audio container
    (ffprobe ``format.tags.creation_time`` — Voice Memos, phone recorders,
    and most capture apps stamp this), and
  * builds a structured ``meeting_provenance`` ledger stored under
    ``RecordingSession.processing_metadata["meeting_provenance"]``.

Ledger shape (every leaf is ``{value, source, confidence}``):

    {
      "when": {
        "recorded_at": {...},          # best estimate of when it happened
        "uploaded_at": {...},          # server ingest time (confidence 1.0)
        "candidates": [ {...}, ... ],  # every timestamp we found, ranked
      },
      "where": {
        "source_type": {...},          # upload | bulk_import | always_on | ...
        "original_filename": {...},
        "room_name": {...},            # when present
        "source_device_id": {...},     # satellite recorders
      },
      "who": {
        "recorder": {...},             # the uploading/recording user
        # the participant LIST (with per-entry contact_id + source) is the
        # authoritative "who"; this section points at it + records the
        # recorder. Participant entries carry their own ``source``
        # (recorder | manual | speaker_fingerprint | upload).
      },
      "built_at": "<iso>",
      "schema": 1,
    }

Confidence ranking for the recorded-at candidates (highest first):
  0.95  audio container creation_time (the device stamped it)
  0.70  a YYYY-MM-DD in the filename (export tooling / user naming)
  0.55  browser File.lastModified (client-reported; uploads only)
  0.50  server filesystem mtime (for an upload this is just the ingest time)
  0.30  server upload time (only a floor — always known)

Everything is best-effort: a probe failure never raises; missing signals are
simply absent from the ledger.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

PROVENANCE_SCHEMA = 1

CONF_AUDIO_META = 0.95
CONF_FILENAME = 0.70
CONF_CLIENT_MTIME = 0.55
CONF_FILE_MTIME = 0.50
CONF_UPLOAD_TIME = 0.30


def _signal(value: Any, source: str, confidence: float) -> dict:
    return {"value": value, "source": source, "confidence": confidence}


def probe_recorded_at(audio_path: str | Path, *, timeout: float = 8.0) -> Optional[datetime]:
    """Read the embedded recording timestamp from the audio container via
    ffprobe (``format.tags.creation_time``). Returns a tz-aware UTC datetime,
    or ``None`` when ffprobe is unavailable, the file has no such tag, or
    anything fails. Never raises."""
    try:
        path = str(audio_path)
        proc = subprocess.run(
            [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_entries", "format_tags=creation_time", path,
            ],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        tags = (json.loads(proc.stdout).get("format") or {}).get("tags") or {}
        raw = tags.get("creation_time") or tags.get("date") or tags.get("DATE")
        if not raw:
            return None
        return _parse_iso(raw)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        logger.debug("probe_recorded_at failed for %s: %s", audio_path, exc)
        return None


def _parse_iso(raw: str) -> Optional[datetime]:
    raw = raw.strip()
    if not raw:
        return None
    # ffmpeg emits e.g. "2026-06-04T21:51:03.000000Z"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw[: len(fmt) + 6], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Public alias — callers (session-create paths) parse a ledger signal's
# ``value`` back into a datetime for the created_at/started_at columns.
def parse_iso(raw: str) -> Optional[datetime]:
    return _parse_iso(raw)


def _mtime(audio_path: str | Path) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(Path(audio_path).stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def build_when_candidates(
    *,
    audio_path: Optional[str | Path],
    filename_date: Optional[date],
    filename_time: Optional[time] = None,
    uploaded_at: datetime,
    client_modified_at: Optional[datetime] = None,
) -> list[dict]:
    """Collect every available timestamp signal, ranked by confidence
    (highest first). Always includes the upload time as a floor."""
    cands: list[dict] = []

    if audio_path:
        rec = probe_recorded_at(audio_path)
        if rec is not None:
            cands.append(_signal(rec.isoformat(), "audio_metadata.creation_time", CONF_AUDIO_META))

    if filename_date is not None:
        when = datetime.combine(
            filename_date, filename_time or time(0, 0), tzinfo=timezone.utc
        )
        cands.append(_signal(when.isoformat(), "filename", CONF_FILENAME))

    # Browser-reported File.lastModified (uploads only). The server file mtime
    # below is just the ingest time for an upload, so this client signal — when
    # the user's own copy was last written — is the real "file date" and ranks
    # above it. Normalized to tz-aware UTC.
    if client_modified_at is not None:
        cm = client_modified_at
        if cm.tzinfo is None:
            cm = cm.replace(tzinfo=timezone.utc)
        cands.append(_signal(cm.astimezone(timezone.utc).isoformat(), "client_file_mtime", CONF_CLIENT_MTIME))

    if audio_path:
        mt = _mtime(audio_path)
        if mt is not None:
            cands.append(_signal(mt.isoformat(), "file_mtime", CONF_FILE_MTIME))

    cands.append(_signal(uploaded_at.isoformat(), "upload_time", CONF_UPLOAD_TIME))

    cands.sort(key=lambda c: c["confidence"], reverse=True)
    return cands


def choose_recorded_at(candidates: list[dict]) -> Optional[dict]:
    """The highest-confidence non-upload-floor candidate, else the floor."""
    for c in candidates:
        if c["source"] != "upload_time":
            return c
    return candidates[0] if candidates else None


def recorded_date_time(recorded: Optional[dict]) -> tuple[Optional[date], Optional[time]]:
    """Split a chosen recorded-at signal into (date, time) for the
    meeting_date / meeting_time columns. Time is dropped when it's exactly
    midnight from a date-only source (so a date-only filename doesn't assert
    a fake 00:00 meeting time)."""
    if not recorded:
        return None, None
    dt = _parse_iso(str(recorded.get("value")))
    if dt is None:
        return None, None
    if recorded.get("source") == "filename" and dt.timetz().replace(tzinfo=None) == time(0, 0):
        return dt.date(), None
    return dt.date(), dt.timetz().replace(tzinfo=None)


def build_meeting_provenance(
    *,
    audio_path: Optional[str | Path],
    original_filename: Optional[str],
    filename_date: Optional[date],
    filename_time: Optional[time],
    uploaded_at: datetime,
    source_type: str,
    recorder: Optional[dict] = None,
    room_name: Optional[str] = None,
    source_device_id: Optional[str] = None,
    client_modified_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Assemble the when/where/who provenance ledger. ``recorder`` is an
    optional ``{name, email, contact_id}`` dict. ``client_modified_at`` is the
    browser-reported File.lastModified (uploads only). ``now`` is injectable for
    deterministic tests."""
    candidates = build_when_candidates(
        audio_path=audio_path,
        filename_date=filename_date,
        filename_time=filename_time,
        uploaded_at=uploaded_at,
        client_modified_at=client_modified_at,
    )
    chosen = choose_recorded_at(candidates)

    where: dict[str, Any] = {
        "source_type": _signal(source_type, "pipeline", 1.0),
    }
    if original_filename:
        where["original_filename"] = _signal(original_filename, "upload", 1.0)
    if room_name:
        where["room_name"] = _signal(room_name, "session", 0.9)
    if source_device_id:
        where["source_device_id"] = _signal(source_device_id, "satellite", 0.9)

    who: dict[str, Any] = {}
    if recorder:
        who["recorder"] = _signal(recorder, "session.user", 1.0)

    return {
        "schema": PROVENANCE_SCHEMA,
        "built_at": (now or datetime.now(timezone.utc)).isoformat(),
        "when": {
            "recorded_at": chosen,
            "uploaded_at": _signal(uploaded_at.isoformat(), "server_ingest", 1.0),
            "candidates": candidates,
        },
        "where": where,
        "who": who,
    }
