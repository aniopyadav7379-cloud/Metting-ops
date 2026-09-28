"""Durable object storage for meeting media (audio, TTS, exports).

Companion to `attachment_storage.py` (which handles session attachments).
Same Garage (S3-compatible) client pattern + env vars, but a separate
`meeting-ops-audio` bucket and an audio/media-oriented API:

  - `put_path` / `put_stream`   — upload a local file / stream to Garage.
  - `open_object`               — read stream back (for proxy-streaming downloads).
  - `cached_local_path`         — return a LOCAL path for processing (ffmpeg /
                                  STT / diarize). Downloads from Garage into a
                                  cache dir on miss. This is the "Garage =
                                  canonical, local = cache" model.
  - `delete_object` / `delete_prefix` — purge on session/user delete.

Design notes:
  - Garage = durable canonical store; local disk = transient processing cache.
  - `preferred_backend()` is 'garage' when configured, else 'local' (so a
    deploy without Garage creds still works on disk, and the per-session
    `audio_storage_backend` column records which backend actually holds each
    object — exactly like attachments).
  - Free tier never reaches here (its audio stays in the browser).
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import BinaryIO, Iterator, Optional

logger = logging.getLogger(__name__)

RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", "/app/recordings"))
# Local cache root for objects pulled from Garage for processing/serving.
MEDIA_CACHE_ROOT = Path(os.getenv("MEDIA_CACHE_DIR", str(RECORDINGS_DIR / "media-cache")))

GARAGE_ENDPOINT_URL = os.getenv("GARAGE_ENDPOINT_URL", "").strip()
GARAGE_ACCESS_KEY = os.getenv("GARAGE_ACCESS_KEY", "").strip()
GARAGE_SECRET_KEY = os.getenv("GARAGE_SECRET_KEY", "").strip()
GARAGE_REGION = os.getenv("GARAGE_REGION", "garage").strip() or "garage"
GARAGE_AUDIO_BUCKET = os.getenv("GARAGE_AUDIO_BUCKET", "meeting-ops-audio").strip()


def garage_configured() -> bool:
    # MEDIA_STORAGE_DISABLED forces the local backend regardless of creds —
    # used by the test suite (so tests never touch real Garage) and as a
    # per-deploy escape hatch. Read dynamically so it survives import order.
    if os.getenv("MEDIA_STORAGE_DISABLED", "").lower() in ("1", "true", "yes"):
        return False
    return bool(GARAGE_ENDPOINT_URL and GARAGE_ACCESS_KEY and GARAGE_SECRET_KEY)


def preferred_backend() -> str:
    return "garage" if garage_configured() else "local"


_s3_client = None
_bucket_ensured = False


def _client():
    """Lazy, process-cached boto3 client pointed at Garage (path-style).
    Returns None when Garage is unconfigured/disabled (checked first so the
    disable flag wins even if a client was cached earlier)."""
    global _s3_client
    if not garage_configured():
        return None
    if _s3_client is not None:
        return _s3_client
    try:
        import boto3
        from botocore.config import Config

        _s3_client = boto3.client(
            "s3",
            endpoint_url=GARAGE_ENDPOINT_URL,
            aws_access_key_id=GARAGE_ACCESS_KEY,
            aws_secret_access_key=GARAGE_SECRET_KEY,
            region_name=GARAGE_REGION,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=10,
                read_timeout=120,
                retries={"max_attempts": 3},
            ),
        )
        logger.info(
            "media_storage: garage client ready, endpoint=%s bucket=%s",
            GARAGE_ENDPOINT_URL, GARAGE_AUDIO_BUCKET,
        )
        return _s3_client
    except Exception as e:  # noqa: BLE001
        logger.warning("media_storage: garage client init failed: %s", e)
        return None


def ensure_bucket() -> bool:
    """Create the audio bucket if it doesn't exist. Idempotent; safe to call
    repeatedly. Returns True if the bucket is usable."""
    global _bucket_ensured
    if _bucket_ensured:
        return True
    client = _client()
    if client is None:
        return False
    # Don't probe with head_bucket — Garage returns 400 on HEAD with this
    # botocore. Just attempt create and treat "already owned/exists" as
    # success (the common case: the bucket is provisioned out-of-band).
    try:
        client.create_bucket(Bucket=GARAGE_AUDIO_BUCKET)
        logger.info("media_storage: created bucket %s", GARAGE_AUDIO_BUCKET)
        _bucket_ensured = True
        return True
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "AlreadyOwned" in msg or "AlreadyExists" in msg or "BucketAlreadyOwnedByYou" in msg:
            _bucket_ensured = True
            return True
        # Can't create (e.g. data key lacks create perm) but the bucket may
        # already exist + be writable — assume usable and let put/get surface
        # any real problem rather than blocking on a permission we don't need.
        logger.info("media_storage: ensure_bucket assuming existing bucket (%s)", msg)
        _bucket_ensured = True
        return True


# ---------------------------------------------------------------------------
# Key convention: {org_id}/{session_id}/{kind}/{name}
#   kind ∈ audio | tts | export | import
# ---------------------------------------------------------------------------

def media_key(org_id: int | str, session_id: str, name: str, *, kind: str = "audio") -> str:
    safe = os.path.basename(str(name)).replace("\x00", "").replace("\\", "")[:300] or "media.bin"
    return f"{org_id}/{session_id}/{kind}/{safe}"


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _upload_file_to_garage(local_path: str | Path, key: str, content_type: Optional[str]) -> bool:
    """Upload a local file (by path, so it's re-openable/retry-safe) to Garage.
    Returns True on success, False on failure (caller falls back to local)."""
    if preferred_backend() != "garage":
        return False
    try:
        ensure_bucket()
        client = _client()
        if client is None:
            return False
        extra = {"ContentType": content_type} if content_type else None
        client.upload_file(str(local_path), GARAGE_AUDIO_BUCKET, key, ExtraArgs=extra)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("media_storage: garage upload failed key=%s, keeping local: %s", key, e)
        return False


def upload_to_garage(*, key: str, path: str | Path, content_type: Optional[str] = None) -> bool:
    """Push a local file to Garage WITHOUT any local-copy fallback. Returns
    True on success, False if Garage is unconfigured/unavailable/errored. Use
    this when the file already lives at its own durable local location (e.g.
    the recording pipeline's session.wav) and you only want to ADD a Garage
    copy — so a transient Garage failure never duplicates a large file into
    the cache; you just leave the durability columns NULL and retry later."""
    return _upload_file_to_garage(path, key, content_type)


def put_path(*, key: str, path: str | Path, content_type: Optional[str] = None) -> str:
    """Push an existing local file to Garage. Returns the backend that holds the
    durable copy ('garage' on success, 'local' on failure). The source file is
    NOT moved or deleted — local stays the working copy; Garage is the durable
    canonical store. (`upload_file` is path-based, so a transient failure is
    retried/recovered cleanly, unlike streaming.)"""
    if _upload_file_to_garage(path, key, content_type):
        return "garage"
    # Garage unavailable/failed: ensure the bytes live at the cache path so
    # open_object(backend='local') can find them, then record 'local'.
    target = MEDIA_CACHE_ROOT / key
    if Path(path).resolve() != target.resolve():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    return "local"


def put_stream(*, key: str, stream: BinaryIO, content_type: Optional[str] = None) -> str:
    """Spool a stream to a local temp file first (durable on disk regardless of
    Garage), then push that file to Garage. Returns the backend holding the
    durable copy. The local spool stays as a warm cache entry."""
    target = MEDIA_CACHE_ROOT / key
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".uploading")
    with open(tmp, "wb") as out:
        shutil.copyfileobj(stream, out, length=4 * 1024 * 1024)
    os.replace(tmp, target)
    if _upload_file_to_garage(target, key, content_type):
        return "garage"
    return "local"


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def object_size(*, key: str) -> Optional[int]:
    """Return the Garage object's byte size, or None if absent/unconfigured.
    Uses list_objects_v2 (not head_object — Garage 400s on HEAD). Used by
    retention/eviction to verify a durable copy exists at the expected size
    before deleting a local working file."""
    client = _client()
    if client is None:
        return None
    try:
        resp = client.list_objects_v2(Bucket=GARAGE_AUDIO_BUCKET, Prefix=key)
        for o in resp.get("Contents", []):
            if o["Key"] == key:
                return o["Size"]
    except Exception as e:  # noqa: BLE001
        logger.warning("media_storage: object_size failed key=%s: %s", key, e)
    return None


def open_object(*, backend: str, key: str) -> BinaryIO:
    """Open the object for read (for proxy-streaming). Caller closes it."""
    if backend == "garage":
        client = _client()
        if client is None:
            raise FileNotFoundError("media backend=garage but client unconfigured")
        return client.get_object(Bucket=GARAGE_AUDIO_BUCKET, Key=key)["Body"]
    elif backend == "local":
        return open(MEDIA_CACHE_ROOT / key, "rb")
    raise ValueError(f"unknown backend {backend!r}")


def iter_object(*, backend: str, key: str, chunk: int = 1024 * 1024) -> Iterator[bytes]:
    """Yield the object in chunks — for FastAPI StreamingResponse (proxy
    download). Closes the underlying stream when exhausted."""
    body = open_object(backend=backend, key=key)
    try:
        while True:
            data = body.read(chunk)
            if not data:
                break
            yield data
    finally:
        try:
            body.close()
        except Exception:
            pass


def cached_local_path(*, backend: str, key: str) -> Path:
    """Return a LOCAL filesystem path for processing (ffmpeg/STT/diarize).
    For 'local' backend the object already lives in the cache root. For
    'garage' it's downloaded into the cache on miss. The canonical copy
    stays in Garage; this local file is an evictable cache entry."""
    local = MEDIA_CACHE_ROOT / key
    if backend == "local":
        return local
    if local.exists() and local.stat().st_size > 0:
        return local
    client = _client()
    if client is None:
        raise FileNotFoundError("media backend=garage but client unconfigured")
    local.parent.mkdir(parents=True, exist_ok=True)
    tmp = local.with_suffix(local.suffix + ".dl")
    # Stream via get_object rather than client.download_file: download_file
    # issues a HEAD (head_object) first, and Garage returns 400 on HEAD with
    # this botocore (same family as the @aws-sdk checksum/HEAD gotchas). GET
    # works fine, so we copy the body straight to disk.
    body = client.get_object(Bucket=GARAGE_AUDIO_BUCKET, Key=key)["Body"]
    try:
        with open(tmp, "wb") as out:
            shutil.copyfileobj(body, out, length=4 * 1024 * 1024)
    finally:
        try:
            body.close()
        except Exception:
            pass
    os.replace(tmp, local)
    return local


# ---------------------------------------------------------------------------
# Delete (session/user purge → privacy/GDPR)
# ---------------------------------------------------------------------------

def delete_object(*, backend: str, key: str) -> None:
    if backend == "garage":
        client = _client()
        if client is not None:
            try:
                client.delete_object(Bucket=GARAGE_AUDIO_BUCKET, Key=key)
            except Exception as e:  # noqa: BLE001
                logger.warning("media_storage: garage delete failed key=%s: %s", key, e)
    # always also clear any local cache copy
    try:
        (MEDIA_CACHE_ROOT / key).unlink(missing_ok=True)
    except Exception:
        pass


def delete_prefix(*, prefix: str) -> int:
    """Delete every object under a key prefix (e.g. a whole session:
    '{org}/{session_id}/'). Returns count deleted. Used on session/user
    deletion so no orphaned audio survives."""
    deleted = 0
    client = _client()
    if client is not None:
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=GARAGE_AUDIO_BUCKET, Prefix=prefix):
                objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                if objs:
                    client.delete_objects(Bucket=GARAGE_AUDIO_BUCKET, Delete={"Objects": objs})
                    deleted += len(objs)
        except Exception as e:  # noqa: BLE001
            logger.warning("media_storage: delete_prefix failed prefix=%s: %s", prefix, e)
    # clear local cache subtree
    try:
        cache_dir = MEDIA_CACHE_ROOT / prefix
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
    except Exception:
        pass
    return deleted
