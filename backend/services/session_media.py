"""Session-audio ↔ Garage object-storage glue.

Thin layer between the recording/upload/import pipelines and the storage-only
`media_storage` module. Knows about the RecordingSession row; media_storage
does not. Three operations, all designed so the LOCAL file stays the working
copy / source-of-truth and Garage is an ADDITIVE durable canonical copy:

  persist_session_audio  — after the canonical local audio exists, push a copy
                           to Garage (best-effort) and record where it landed
                           on the session row. Never raises; a Garage outage
                           just leaves the columns NULL to retry next time.

  resolve_local_path     — for processing/serving: prefer the local file if
                           present; only if it's gone (evicted) pull it back
                           from Garage into the local cache. Returns a Path or
                           None.

  purge_session_media    — on session delete, remove the Garage object(s) too,
                           so "delete my data" is actually honored.

Free-tier sessions never have server-side audio, so persist is a no-op for
them (session.audio_file is empty).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from services import media_storage

logger = logging.getLogger(__name__)


def _canonical_id(session) -> str:
    return session.session_id or str(session.id)


def persist_session_audio(
    db,
    session,
    *,
    local_path: Optional[str] = None,
    kind: str = "audio",
    commit: bool = True,
) -> Optional[str]:
    """Best-effort: copy the session's canonical audio to Garage and record the
    durable location on the row. Returns the backend ('garage') on success,
    else None. The local file is neither moved nor deleted."""
    try:
        path = local_path or session.audio_file
        if not path:
            return None
        p = Path(path)
        if not p.exists() or p.stat().st_size == 0:
            return None
        key = media_storage.media_key(
            session.organization_id, _canonical_id(session), p.name, kind=kind
        )
        if media_storage.upload_to_garage(key=key, path=p, content_type="audio/wav"):
            session.audio_storage_backend = "garage"
            session.audio_object_key = key
            if commit:
                db.commit()
            logger.info(
                "session_media: session=%s audio → garage key=%s (%.1f MB)",
                session.id, key, p.stat().st_size / (1024 * 1024),
            )
            return "garage"
        # Garage unavailable — leave columns NULL; local stays source of truth,
        # backfill/retry will pick it up later.
        logger.warning(
            "session_media: session=%s garage push skipped/failed, kept local-only",
            session.id,
        )
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("session_media: persist failed session=%s: %s", getattr(session, "id", None), e)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def resolve_local_path(session) -> Optional[Path]:
    """Return a local filesystem path to the session's canonical audio for
    processing/serving. Prefers the existing local file; falls back to pulling
    it from Garage into the local cache. None if neither is available."""
    if session.audio_file:
        p = Path(session.audio_file)
        if p.exists() and p.stat().st_size > 0:
            return p
        # legacy: basename-only fallback under RECORDINGS_DIR
        alt = media_storage.RECORDINGS_DIR / os.path.basename(session.audio_file)
        if alt.exists() and alt.stat().st_size > 0:
            return alt
    if session.audio_object_key and session.audio_storage_backend == "garage":
        try:
            return media_storage.cached_local_path(
                backend="garage", key=session.audio_object_key
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "session_media: garage fetch failed session=%s key=%s: %s",
                session.id, session.audio_object_key, e,
            )
    return None


def purge_session_media(session) -> int:
    """Delete this session's Garage objects (the whole '{org}/{session_id}/'
    prefix — audio + tts + import) plus any local cache copies. Best-effort;
    returns the count of Garage objects removed."""
    try:
        prefix = f"{session.organization_id}/{_canonical_id(session)}/"
        return media_storage.delete_prefix(prefix=prefix)
    except Exception as e:  # noqa: BLE001
        logger.warning("session_media: purge failed session=%s: %s", getattr(session, "id", None), e)
        return 0
