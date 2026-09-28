"""Pluggable storage backend for session attachments.

The writer auto-selects between Garage (S3-compatible, configured via
``GARAGE_ENDPOINT_URL`` + key/secret env vars + bucket name) and local
disk (under ``RECORDINGS_DIR/attachments``) so a deployment can roll
out without Garage credentials and add them later by simply setting
the env vars and re-uploading new attachments. Old rows keep working
through the per-row ``storage_backend`` field.

Object key convention for both backends:

    {org_id}/{session_id}/{uuid}/{filename}

Why include filename in the key (instead of {uuid} alone): downloads
preserve the original filename in Content-Disposition for the user,
and inspecting the bucket / disk dump is more useful when the keys are
human-readable.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid as _uuid
from pathlib import Path
from typing import BinaryIO, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", "/app/recordings"))
LOCAL_ATTACH_ROOT = RECORDINGS_DIR / "attachments"

GARAGE_ENDPOINT_URL = os.getenv("GARAGE_ENDPOINT_URL", "").strip()
GARAGE_ACCESS_KEY = os.getenv("GARAGE_ACCESS_KEY", "").strip()
GARAGE_SECRET_KEY = os.getenv("GARAGE_SECRET_KEY", "").strip()
GARAGE_REGION = os.getenv("GARAGE_REGION", "garage").strip() or "garage"
GARAGE_ATTACHMENTS_BUCKET = os.getenv(
    "GARAGE_ATTACHMENTS_BUCKET", "meeting-ops-attachments"
).strip()


def _garage_configured() -> bool:
    return bool(
        GARAGE_ENDPOINT_URL and GARAGE_ACCESS_KEY and GARAGE_SECRET_KEY
    )


# ---------------------------------------------------------------------------
# boto3 client (lazy)
# ---------------------------------------------------------------------------

_s3_client = None


def _get_s3_client():
    """Returns a configured boto3 S3 client pointed at Garage, or None
    when the env vars are missing. Cached process-wide."""
    global _s3_client
    if _s3_client is not None:
        return _s3_client
    if not _garage_configured():
        return None
    try:
        import boto3
        from botocore.config import Config

        # path-style addressing is what Garage expects; virtual-host
        # style would resolve to "bucket.unicorn-garage:3900" which
        # has no DNS entry inside the docker network.
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
                read_timeout=60,
                retries={"max_attempts": 3},
            ),
        )
        logger.info(
            "attachment_storage: garage client ready, endpoint=%s bucket=%s",
            GARAGE_ENDPOINT_URL,
            GARAGE_ATTACHMENTS_BUCKET,
        )
        return _s3_client
    except Exception as e:
        logger.warning("attachment_storage: garage client init failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_storage_key(org_id: int, session_pk: int, filename: str) -> tuple[str, str]:
    """Return ``(storage_key, attachment_uuid)``. The uuid is the
    middle directory in the key so a re-upload of the same filename
    by the same user doesn't collide. Callers pass this through to
    the DB row's ``storage_key`` column unchanged."""
    safe_name = _sanitize_filename(filename)
    attach_uuid = str(_uuid.uuid4())
    key = f"{org_id}/{session_pk}/{attach_uuid}/{safe_name}"
    return key, attach_uuid


def _sanitize_filename(name: str) -> str:
    """Defensive — strip directory traversal and control chars but
    keep the rest readable so the user sees something familiar on
    download."""
    cleaned = os.path.basename(name).strip()
    # Remove anything that looks like a path separator or null byte
    # that might have slipped past basename on weird platforms.
    cleaned = cleaned.replace("\x00", "").replace("/", "").replace("\\", "")
    return cleaned[:500] or "attachment.bin"


def preferred_backend() -> str:
    """The writer's choice for new uploads. 'garage' when configured,
    else 'local'."""
    return "garage" if _garage_configured() else "local"


def write_stream(
    *,
    storage_key: str,
    stream: BinaryIO,
    content_type: Optional[str] = None,
) -> str:
    """Persist `stream` to the preferred backend. Returns the actual
    ``storage_backend`` value the caller should record (in case the
    preferred backend failed mid-flight and we fell back to local).

    The caller is responsible for size enforcement BEFORE calling
    this — the function reads the entire stream into the backend.
    """
    backend = preferred_backend()
    if backend == "garage":
        try:
            return _write_garage(
                storage_key=storage_key,
                stream=stream,
                content_type=content_type,
            )
        except Exception as e:
            logger.warning(
                "attachment_storage: garage upload failed for key=%s, falling back to local: %s",
                storage_key,
                e,
            )
            # Rewind the stream if possible so the local fallback can re-read.
            try:
                stream.seek(0)
            except Exception:
                logger.error(
                    "attachment_storage: garage failed AND stream not seekable, attachment for key=%s lost",
                    storage_key,
                )
                raise
    return _write_local(storage_key=storage_key, stream=stream)


def _write_garage(
    *, storage_key: str, stream: BinaryIO, content_type: Optional[str]
) -> str:
    client = _get_s3_client()
    if client is None:
        raise RuntimeError("garage client not configured")
    extra = {}
    if content_type:
        extra["ContentType"] = content_type
    client.upload_fileobj(
        stream,
        GARAGE_ATTACHMENTS_BUCKET,
        storage_key,
        ExtraArgs=extra if extra else None,
    )
    return "garage"


def _write_local(*, storage_key: str, stream: BinaryIO) -> str:
    target = LOCAL_ATTACH_ROOT / storage_key
    target.parent.mkdir(parents=True, exist_ok=True)
    # Use atomic write — tmpfile + rename — so a crash mid-upload
    # doesn't leave a half-written file claiming the final key.
    tmp = target.with_suffix(target.suffix + ".uploading")
    try:
        with open(tmp, "wb") as out:
            shutil.copyfileobj(stream, out, length=4 * 1024 * 1024)
        os.replace(tmp, target)
    except Exception:
        # Best-effort cleanup so a failure doesn't leave stragglers on disk.
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return "local"


def open_stream(*, storage_backend: str, storage_key: str) -> BinaryIO:
    """Open the underlying file for read. Caller is responsible for
    closing the returned object (use ``with``)."""
    if storage_backend == "garage":
        client = _get_s3_client()
        if client is None:
            raise FileNotFoundError(
                f"attachment storage_backend=garage but client not configured"
            )
        resp = client.get_object(
            Bucket=GARAGE_ATTACHMENTS_BUCKET,
            Key=storage_key,
        )
        # ``Body`` is a streaming wrapper around the underlying urllib3
        # response; .read() materializes it. Caller streams via
        # iter(read(N), b'') if they want chunked output.
        return resp["Body"]
    elif storage_backend == "local":
        path = LOCAL_ATTACH_ROOT / storage_key
        return open(path, "rb")
    else:
        raise ValueError(f"Unknown storage_backend: {storage_backend!r}")


def delete_object(*, storage_backend: str, storage_key: str) -> None:
    """Delete the underlying storage object. Idempotent — missing
    objects are not an error (matches DELETE-then-DELETE behavior)."""
    if storage_backend == "garage":
        client = _get_s3_client()
        if client is None:
            logger.warning(
                "attachment_storage: cannot delete garage key=%s, client unconfigured",
                storage_key,
            )
            return
        try:
            client.delete_object(
                Bucket=GARAGE_ATTACHMENTS_BUCKET,
                Key=storage_key,
            )
        except Exception as e:
            logger.warning(
                "attachment_storage: garage delete failed for key=%s: %s",
                storage_key,
                e,
            )
    elif storage_backend == "local":
        path = LOCAL_ATTACH_ROOT / storage_key
        try:
            path.unlink(missing_ok=True)
            # Also clean up empty parent dirs so the local tree stays tidy.
            for parent in [path.parent, path.parent.parent, path.parent.parent.parent]:
                try:
                    parent.rmdir()  # only succeeds if empty
                except OSError:
                    break
                if parent == LOCAL_ATTACH_ROOT:
                    break
        except Exception as e:
            logger.warning(
                "attachment_storage: local delete failed for key=%s: %s",
                storage_key,
                e,
            )
    else:
        logger.warning(
            "attachment_storage: unknown storage_backend=%r, key=%s — refusing to delete",
            storage_backend,
            storage_key,
        )
