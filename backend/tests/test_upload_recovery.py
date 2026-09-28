from datetime import datetime, timezone
from unittest.mock import AsyncMock
import uuid

import pytest


@pytest.mark.asyncio
async def test_recover_pending_uploads_reenqueues_job_with_durable_bytes(app, monkeypatch):
    from auth.models import Organization, User
    from database.database import SessionLocal
    from database.models import UploadJob
    from api import uploads

    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == "magic-unicorn").first()
        user = db.query(User).filter(User.username == "admin").first()
        upload_id = uuid.uuid4()
        job = UploadJob(
            organization_id=org.id,
            user_id=user.id,
            upload_id=upload_id,
            filename="recover.wav",
            action="transcribe",
            total_size=4,
            bytes_received=4,
            chunks_received=1,
            total_chunks=1,
            stage="summarizing",
            created_at=datetime.now(timezone.utc),
        )
        db.add(job)
        db.commit()
        job_dir = uploads._job_dir(org.slug, str(upload_id))
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "recover.wav").write_bytes(b"RIFF")
    finally:
        db.close()

    enqueue = AsyncMock()
    monkeypatch.setattr(uploads.upload_pipeline_queue, "enqueue", enqueue)
    recovered = await uploads.recover_pending_uploads()

    assert recovered >= 1
    enqueue.assert_any_await(str(upload_id))
