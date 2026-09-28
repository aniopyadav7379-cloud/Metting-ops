"""Database models for Meeting-Ops"""
from sqlalchemy import Column, Integer, BigInteger, String, DateTime, Date, Time, Text, Float, Boolean, JSON, ForeignKey, Index, UniqueConstraint, func, event
from sqlalchemy.orm import declarative_base
from sqlalchemy.dialects.postgresql import UUID, JSONB, CITEXT, ARRAY
from datetime import datetime, timezone
import uuid
import json

Base = declarative_base()


# JSON type that renders as JSONB on Postgres and falls back to JSON on
# SQLite (test fixtures). Use this for any column the alembic migrations
# created with JSONB so autogenerate stops flagging "JSONB(astext_type=Text)
# -> JSON" drift. Old `Column(JSON, ...)` declarations are the autogenerate
# false-positives; this preserves SQLite test compat while matching the
# real Postgres schema.
def _JsonB():
    return JSON().with_variant(JSONB(), "postgresql")

class Settings(Base):
    """Settings model"""
    __tablename__ = "settings"
    
    id = Column(Integer, primary_key=True)
    category = Column(String(50), nullable=False)
    key = Column(String(100), nullable=False)
    value = Column(Text)
    description = Column(String(500))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

class RecordingSession(Base):
    """Recording session model"""
    __tablename__ = "recording_sessions"
    # GIN index on tags exists in Postgres (017_session_tags) for cheap
    # @> containment lookups from the list filter. Declared here so
    # autogenerate doesn't try to drop it.
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "external_source",
            "external_id",
            name="uq_recording_sessions_org_external_source_id",
        ),
        Index(
            "ix_recording_sessions_tags_gin",
            "tags",
            postgresql_using="gin",
        ),
    )

    id = Column(Integer, primary_key=True)  # Keep as Integer to match existing table
    session_id = Column(String(100), unique=True, nullable=True)  # Legacy field
    name = Column(String(200))
    description = Column(Text)
    meeting_type = Column(String(100))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    started_at = Column(DateTime)  # Changed from start_time
    ended_at = Column(DateTime)    # Changed from end_time
    # User-editable "when the meeting actually happened" — separate from
    # started_at/created_at which track when the recording landed on the
    # server. Backfilled from DATE(started_at) / DATE(created_at) by
    # alembic 027; foundation for the /import page that ingests Aaron's
    # 526-file Voice Memos archive (each file's date comes from the
    # filename parser, not the upload time).
    meeting_date = Column(Date, nullable=True, index=True)
    meeting_time = Column(Time, nullable=True)
    duration = Column(Float, default=0)
    status = Column(String(50), default="active")
    mode = Column(String(20), default="upload", nullable=False, server_default="upload")
    extra_data = Column(JSON)
    user_id = Column(Integer)  # Foreign key to User
    audio_file = Column(String(500))  # Changed from audio_file_path
    title = Column(String(500))  # Added title column
    # When True, the user has manually set the title and the
    # auto-summary step must not overwrite it on reprocess. Set by
    # the rename endpoint; cleared if the user clears the title.
    title_user_set = Column(Boolean, default=False, nullable=False, server_default="false")
    transcript = Column(Text)  # Added transcript column
    transcript_simple = Column(Text)  # Simple text transcript without speakers
    transcript_diarized = Column(JSON)  # Full diarized transcript with speakers
    speaker_count = Column(Integer, default=0, nullable=False, server_default="0")
    summary = Column(Text)
    progressive_summaries = Column(JSON)  # List of summaries during recording
    final_summary = Column(JSON)  # Comprehensive final analysis
    summary_preview = Column(String(300), nullable=True)
    # Explicit approval for the response-minimized Customer-Ops federation
    # contract. The digest binds approval to the current summary projection, so
    # a re-summary automatically becomes stale without requiring every summary
    # writer to remember to clear a flag.
    federation_summary_approved_at = Column(DateTime(timezone=True), nullable=True)
    federation_summary_approved_by_user_id = Column(Integer, nullable=True)
    federation_summary_approved_digest = Column(String(64), nullable=True)
    ai_insights = Column(JSON)  # Legacy field for compatibility
    processing_metadata = Column(JSON)  # Processing stats and metadata (renamed from metadata)
    generated_emails = Column(JSON)  # Generated follow-up emails
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    # Satellite device fields
    source_device_id = Column(String(100), nullable=True)  # Which satellite recorded this
    source_type = Column(String(50), default="local_mic")  # "local_mic", "satellite_stream", "satellite_upload", "satellite_transcript", "companion_app"
    # Idempotent inbound-federation identity. NULL for native Meeting-Ops
    # sessions; Stable writes ("unicorn-stable", export_id). The org is part of
    # the uniqueness boundary so two tenants can safely use the same upstream
    # identifier without seeing or overwriting one another.
    external_source = Column(String(50), nullable=True)
    external_id = Column(String(128), nullable=True)
    room_name = Column(String(200), nullable=True)  # Physical room name
    # NOT NULL on Postgres per 004_multi_org_scoping.
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    project_app = Column(String(50), nullable=True)  # 'project-ops', 'crisis-ops', or NULL
    # project_id is a string (UUID for project-ops; whatever crisis-ops uses).
    # Migrated from Integer in 010_project_id_to_text.
    project_id = Column(Text, nullable=True)
    project_slug = Column(String(200), nullable=True)
    # List of {name: str, email?: str, role?: str}. JSONB so we can index/query
    # later if attendance reporting needs it; for now it's a plain list scanned
    # in application code.
    participants = Column(JSONB, nullable=False, server_default="[]")
    # Free-form per-session tags. Postgres native TEXT[] so we can use
    # array containment (@>) to filter sessions by tag set without scanning.
    tags = Column(ARRAY(String), nullable=False, server_default="{}")
    # Conference Room linkage (alembic 023). NULL for browser/upload/satellite
    # sessions. SET NULL on parent delete so historical recordings survive
    # the room being deleted.
    room_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conference_rooms.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    room_source_id = Column(
        UUID(as_uuid=True),
        ForeignKey("room_audio_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Brigade integration Phase 1 (alembic 030_brigade_graph_node_id).
    # `brigade_graph_node_id` is the canonical entity name we passed to
    # Brigade's /knowledge/store/entity for this session's :Meeting node
    # (today the writer derives the value as f"meeting_ops_meeting_{id}";
    # stable across re-writes since Brigade's MERGE keys on .name).
    # `brigade_synced_at` is the wall-clock of the last successful write
    # and drives the "View in Brigade graph" link on SessionDetails plus
    # the reconciliation job's "unsynced in the last 7 days" query.
    # Both stay NULL for sessions that completed before Phase 1 landed
    # OR for deployments without BRIGADE_API_KEY set (the writer no-ops).
    brigade_graph_node_id = Column(String(64), nullable=True)
    brigade_synced_at = Column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # Durable write-state for the optional Brigade knowledge-graph projection.
    # A graph outage must never fail a meeting, but it must be observable and
    # explicitly retryable rather than looking like an empty graph forever.
    brigade_sync_status = Column(String(20), nullable=True, index=True)
    brigade_sync_attempted_at = Column(DateTime(timezone=True), nullable=True)
    brigade_sync_error = Column(String(500), nullable=True)
    brigade_sync_attempt_count = Column(Integer, nullable=False, default=0, server_default="0")
    # Durable object-storage (Garage) location for this session's canonical
    # audio (alembic 031). Mirrors the SessionAttachment storage pattern:
    #   audio_storage_backend ∈ 'garage' | 'local' | NULL
    #   audio_object_key      = Garage key '{org}/{session_id}/audio/{name}'
    # The legacy `audio_file` column stays the LOCAL working-copy path
    # (source of truth during cutover); these two record the durable copy.
    # NULL means "not yet pushed to object storage" — read paths fall back
    # to the local `audio_file`, so existing rows keep working untouched.
    # Free-tier audio never populates these (it stays in the browser).
    audio_storage_backend = Column(String(20), nullable=True)
    audio_object_key = Column(String(500), nullable=True)
    # v3.18.3 background-jobs: arq job_id that is currently processing
    # this session (finalize + summarize + insights + Brigade write).
    # Frontend polls /api/jobs/<id> to resume after tab reload, and the
    # worker drift-checks at entry — if this column changed since the
    # worker was enqueued, a newer job is processing the row and we
    # skip side effects to avoid double-writes.
    processing_job_id = Column(String(64), nullable=True, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "name": self.name,
            "description": self.description,
            "meeting_type": self.meeting_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "duration": self.duration,
            "status": self.status,
            "brigade_sync_status": self.brigade_sync_status,
            "brigade_synced_at": self.brigade_synced_at.isoformat() if self.brigade_synced_at else None,
            "brigade_sync_attempted_at": self.brigade_sync_attempted_at.isoformat() if self.brigade_sync_attempted_at else None,
            "brigade_sync_error": self.brigade_sync_error,
            "brigade_sync_attempt_count": self.brigade_sync_attempt_count,
            "extra_data": self.extra_data
        }


@event.listens_for(RecordingSession, "before_insert")
@event.listens_for(RecordingSession, "before_update")
def _sync_recording_session_speaker_count(mapper, connection, target):
    diarized = target.transcript_diarized
    speakers = diarized.get("speakers") if isinstance(diarized, dict) else None
    if isinstance(speakers, list):
        target.speaker_count = len({str(s) for s in speakers if s})
    preview = None
    if isinstance(target.final_summary, dict):
        preview = target.final_summary.get("executive") or target.final_summary.get("summary")
    if not preview and target.summary:
        raw = target.summary
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                preview = parsed.get("executive") or parsed.get("summary") if isinstance(parsed, dict) else raw
            except (ValueError, TypeError):
                preview = raw
        elif isinstance(raw, dict):
            preview = raw.get("executive") or raw.get("summary")
    if preview:
        target.summary_preview = " ".join(str(preview).split())[:300]


Index(
    "ix_recording_sessions_org_created_id",
    RecordingSession.organization_id,
    RecordingSession.created_at.desc(),
    RecordingSession.id.desc(),
)


class SessionCollaborator(Base):
    """Per-meeting explicit collaborator grant.

    Rows in this table live ALONGSIDE the org/project membership checks
    in `has_session_access`. They cover two cases:

    - Existing uchub Keycloak user invited by user_id (we know who they
      are; user_id is set, email may also be cached for display).
    - External email invitee (user_id is NULL until they accept; a
      magic-link secret whose SHA-256 digest is stored in `token_hash`
      is the only way in until then).

    `access_level` is one of: 'read', 'comment', 'edit'.

    The deployed 053 schema keeps the legacy UUID `token` column non-null
    during the bounded rollout/rollback window. New v2 rows store an unrelated
    random UUID there solely for schema compatibility; runtime validation
    never queries it. The approval-gated scrub makes the live column nullable
    and clears existing plaintext only after every runtime is hash-aware.
    """
    __tablename__ = "session_collaborators"
    __table_args__ = (
        Index(
            "ix_session_collaborators_session_user",
            "session_id",
            "user_id",
        ),
        Index(
            "ix_session_collaborators_session_email",
            "session_id",
            "email",
        ),
        Index(
            "ix_session_collaborators_token",
            "token",
            unique=True,
        ),
        Index(
            "ix_session_collaborators_token_hash",
            "token_hash",
            unique=True,
        ),
    )

    id = Column(Integer, primary_key=True)
    session_id = Column(
        Integer,
        ForeignKey("recording_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    # user_id is set when the invitee already has a uchub-Keycloak account
    # in our local users table. NULL means "external email only, not yet
    # resolved to a user record".
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # CITEXT for case-insensitive matching in Postgres. SQLAlchemy will
    # emit `CITEXT` DDL on Postgres and treat it like String elsewhere
    # (SQLite test fixtures fall back to TEXT cleanly).
    email = Column(CITEXT(), nullable=True)
    access_level = Column(
        String(20), nullable=False, default="read", server_default="read"
    )
    invited_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    token = Column(
        UUID(as_uuid=True),
        unique=True,
        # 014/053 deployments remain NOT NULL until the separately approved
        # scrub. The model is permissive so the hash-aware app can continue
        # to read rows after that irreversible schema operation.
        nullable=True,
        default=uuid.uuid4,
    )
    token_hash = Column(String(64), nullable=True)
    token_version = Column(
        Integer,
        nullable=False,
        default=2,
        server_default="2",
    )
    expires_at = Column(DateTime(timezone=True), nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    delivery_state = Column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    delivery_attempt_count = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    last_delivery_attempt_at = Column(DateTime(timezone=True), nullable=True)
    delivery_failure_reason = Column(String(120), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        # 014_session_collaborators created this column with
        # server_default=now(); declared here so autogenerate stops flagging.
        server_default=func.now(),
    )


class GoogleCalendarIntegration(Base):
    """Per-user OAuth connection to Google Calendar (Phase 3F). Tokens are
    encrypted at rest via services.providers.crypto — the same helper every
    other provider credential in this codebase already uses, not a new
    encryption scheme. Per-user (not per-org) because a calendar event is
    created on behalf of whichever user confirmed the action item, matching
    who actually granted OAuth consent.
    """

    __tablename__ = "google_calendar_integrations"

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    google_email = Column(String(320), nullable=True)
    access_token_encrypted = Column(Text, nullable=True)
    refresh_token_encrypted = Column(Text, nullable=True)
    token_expires_at = Column(DateTime(timezone=True), nullable=True)
    scopes = Column(String(500), nullable=True)
    status = Column(String(20), nullable=False, default="disconnected", server_default="disconnected")
    last_sync_error = Column(Text, nullable=True)
    connected_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), server_default=func.now())

    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_google_calendar_org_user"),)


class KnowledgeDocument(Base):
    """A supporting document (PDF/DOCX/TXT/MD) indexed into the SAME Qdrant
    collection meetings and lectures use, via semantic_search_service —
    no second vector store, no second pipeline (see
    services/document_knowledge.py). `doc_id` is the Qdrant "session_id"
    key for this document's points; `content_hash` gives org-scoped
    duplicate detection so re-uploading the same file doesn't re-index it.
    """

    __tablename__ = "knowledge_documents"

    id = Column(Integer, primary_key=True)
    doc_id = Column(String(64), nullable=False, unique=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    uploaded_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    filename = Column(String(500), nullable=False)
    content_type = Column(String(100), nullable=True)
    file_size = Column(Integer, nullable=True)
    content_hash = Column(String(64), nullable=False, index=True)
    extracted_text = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="processing", server_default="processing")
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now())

    __table_args__ = (UniqueConstraint("organization_id", "content_hash", name="uq_knowledge_documents_org_hash"),)


class LectureDetails(Base):
    """Lecture-specific structured fields, one-to-one with a RecordingSession
    whose meeting_type == "lecture". Lectures are NOT a renamed meeting:
    course/instructor/concepts/definitions/examples/study_notes/questions
    have no equivalent on a meeting and live here instead of bloating
    RecordingSession with lecture-only nullable columns. The shared
    transcript/summary/Qdrant/Ask-AI pipeline is identical for both domains.
    """

    __tablename__ = "lecture_details"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("recording_sessions.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    course = Column(String(300), nullable=True)
    subject = Column(String(300), nullable=True)
    instructor = Column(String(300), nullable=True)
    lecture_number = Column(Integer, nullable=True)
    concepts = Column(JSON, nullable=True)
    definitions = Column(JSON, nullable=True)
    examples = Column(JSON, nullable=True)
    questions = Column(JSON, nullable=True)
    study_notes = Column(Text, nullable=True)
    extraction_status = Column(String(20), nullable=False, default="pending", server_default="pending")
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), server_default=func.now())


class ImportantMoment(Base):
    """Timestamp-grounded moment (decision, action item, key explanation,
    question, topic transition, disagreement, conclusion, key lecture
    concept) for the timeline view. `timestamp` is resolved server-side by
    matching `quote` against a real Transcription row — never trusted
    verbatim from the LLM (see services.important_moments.extract_moments),
    so a moment with no matching segment gets timestamp=None rather than a
    fabricated one.
    """

    __tablename__ = "important_moments"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("recording_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    moment_type = Column(String(40), nullable=False)
    description = Column(Text, nullable=False)
    quote = Column(Text, nullable=True)
    timestamp = Column(Float, nullable=True)
    speaker = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), server_default=func.now())


class ActionItem(Base):
    """First-class action item promoted out of `recording_sessions.final_summary`.

    Backfilled by alembic 021_action_items from the legacy JSON columns and
    re-derived on every summarize pass (see
    `services.action_items_extractor.persist_action_items`). Manual entries
    created from the UI use `source='manual'`.

    Status transitions are validated by a CHECK constraint at the DB level;
    when status flips to 'done' the API sets `completed_at`.
    """
    __tablename__ = "action_items"
    __table_args__ = (
        Index("ix_action_items_session", "session_id"),
        Index("ix_action_items_organization", "organization_id"),
        Index("ix_action_items_org_status", "organization_id", "status"),
        Index(
            "ix_action_items_org_project_ops_state",
            "organization_id",
            "project_ops_link_state",
        ),
    )

    id = Column(Integer, primary_key=True)
    session_id = Column(
        Integer,
        ForeignKey("recording_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    text = Column(Text, nullable=False)
    # Free-text for now ("Aaron", "Sarah", "Mike at finance"). When we
    # promote owners to real speaker/user IDs that becomes a side
    # table, not a foreign-key bump here.
    owner = Column(String(200), nullable=True)
    # Best-effort parse from the LLM ("Friday", "next week" etc); the
    # original phrasing is preserved in `raw_payload` so the UI can
    # render the fallback string when this is NULL.
    due_date = Column(DateTime(timezone=True), nullable=True)
    # 'todo' | 'doing' | 'done' | 'cancelled' (CHECK constraint enforces)
    status = Column(
        String(20), nullable=False, default="todo", server_default="todo"
    )
    # Within-session ordering as the LLM produced them.
    sort_order = Column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    completed_at = Column(DateTime(timezone=True), nullable=True)
    # 'final_summary' | 'ai_insights' | 'manual' (CHECK constraint enforces)
    source = Column(
        String(50),
        nullable=False,
        default="final_summary",
        server_default="final_summary",
    )
    raw_payload = Column(JSONB, nullable=True)
    # Project-Ops federation lifecycle. This is deliberately separate from the
    # local `status`: Meeting-Ops owns the checkbox above; Project-Ops owns its
    # Task status. Reconciliation updates only these linkage fields.
    project_ops_link_state = Column(
        String(32),
        nullable=False,
        default="local_only",
        server_default="local_only",
    )
    project_ops_proposal_id = Column(String(64), nullable=True)
    project_ops_task_id = Column(String(64), nullable=True)
    project_ops_task_url = Column(Text, nullable=True)
    project_ops_project_number = Column(String(32), nullable=True)
    project_ops_task_status = Column(String(32), nullable=True)
    project_ops_submitted_at = Column(DateTime(timezone=True), nullable=True)
    project_ops_last_sync_attempt_at = Column(DateTime(timezone=True), nullable=True)
    project_ops_last_synced_at = Column(DateTime(timezone=True), nullable=True)
    # Google Calendar sync state (Phase 3F) — same convention as the
    # project_ops_* columns above, new integration.
    google_calendar_event_id = Column(String(200), nullable=True)
    google_calendar_link_state = Column(String(32), nullable=False, default="none", server_default="none")
    google_calendar_synced_at = Column(DateTime(timezone=True), nullable=True)
    google_calendar_sync_error = Column(Text, nullable=True)
    # Phase 3G — generic ref store for the 8 task/notification providers
    # (jira/todoist/clickup/notion/confluence/slack/teams/email), keyed by
    # provider name: {"jira": {"key": "PROJ-123", "url": "...", "synced_at": "..."}, ...}
    external_task_refs = Column(JSON, nullable=True)
    # The upstream proposal timestamp is kept separately from the local
    # reconciliation timestamp so stale responses cannot regress a link.
    project_ops_remote_updated_at = Column(DateTime(timezone=True), nullable=True)
    project_ops_sync_error = Column(String(200), nullable=True)
    project_ops_retry_count = Column(
        Integer, nullable=False, default=0, server_default="0"
    )


class Transcription(Base):
    """Transcription model - matches actual database schema"""
    __tablename__ = "transcriptions"
    __table_args__ = (Index("ix_transcriptions_session_speaker", "session_id", "speaker"),)
    
    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, nullable=False)  # Foreign key to RecordingSession.id (integer)
    text = Column(Text, nullable=False)
    speaker = Column(String(100))  # Speaker name
    # Note: speaker_id column doesn't exist in actual database
    start_time = Column(Float)  # Start time in seconds
    end_time = Column(Float)    # End time in seconds
    confidence = Column(Float)  # Transcription confidence
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # Creation timestamp
    
    # REMOVED: These columns don't exist in actual database:
    # duration_seconds = Column(Float)  # ❌ REMOVED
    # processing_time_ms = Column(Float)  # ❌ REMOVED  
    # npu_accelerated = Column(Boolean)  # ❌ REMOVED
    # model_used = Column(String(100))  # ❌ REMOVED
    # speaker_confidence = Column(Float)  # ❌ REMOVED
    
    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "text": self.text,
            "speaker": self.speaker,
            "timestamp": self.created_at.isoformat() if self.created_at else None,
            "confidence": self.confidence,
            "start_time": self.start_time,
            "end_time": self.end_time
        }

class Speaker(Base):
    """Speaker model"""
    __tablename__ = "speakers"
    
    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, nullable=False)
    speaker_id = Column(String(100), nullable=False)
    display_name = Column(String(100))
    total_speaking_time = Column(Float, default=0.0)
    total_segments = Column(Integer, default=0)
    first_appearance = Column(Float)
    last_appearance = Column(Float)
    confidence = Column(Float)
    voice_characteristics = Column(JSON)
    
    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "speaker_id": self.speaker_id,
            "name": self.display_name,
            "total_speaking_time": self.total_speaking_time,
            "total_segments": self.total_segments,
            "first_heard": self.first_appearance,
            "last_heard": self.last_appearance,
            "confidence": self.confidence,
            "voice_characteristics": self.voice_characteristics
        }

class AudioFile(Base):
    """Audio file model"""
    __tablename__ = "audio_files"

    id = Column(Integer, primary_key=True)
    file_id = Column(String(100), unique=True, nullable=False)
    session_id = Column(String(100), nullable=False)
    user_id = Column(Integer)
    filename = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_size = Column(Integer)
    file_format = Column(String(10))
    mime_type = Column(String(50))
    duration_seconds = Column(Float)
    sample_rate = Column(Integer)
    channels = Column(Integer)
    uploaded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # NOT NULL on Postgres per 004_multi_org_scoping.
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)


class ChatHistory(Base):
    """Persisted AI chat history for per-meeting and cross-meeting RAG conversations."""
    __tablename__ = "chat_history"

    id = Column(Integer, primary_key=True)
    # session_id for per-meeting chat, or "__rag__" for cross-meeting RAG chat
    session_key = Column(String(200), nullable=False, index=True)
    role = Column(String(20), nullable=False)  # "user" or "assistant"
    content = Column(Text, nullable=False)
    metadata_json = Column(Text, nullable=True)  # JSON string for extra metadata
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # NOT NULL on Postgres per 004_multi_org_scoping.
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)


class SatelliteDevice(Base):
    """Satellite recording device (ESP32-S3, Raspberry Pi, companion app)"""
    __tablename__ = "satellite_devices"

    id = Column(Integer, primary_key=True)
    device_id = Column(String(100), unique=True, nullable=False)
    name = Column(String(200))
    room_name = Column(String(200))
    device_type = Column(String(50))  # "esp32", "esp32-s3", "rpi4", "rpi5", "companion-app"
    capabilities = Column(Text)  # JSON string: {"audio": true, "local_stt": false, "vad": false, "sd_card": true}
    ip_address = Column(String(50))
    last_heartbeat = Column(DateTime)
    status = Column(String(50), default="offline")  # "online", "offline", "recording", "uploading", "error"
    firmware_version = Column(String(50))
    config = Column(Text)  # JSON string: {"sample_rate": 16000, "bit_depth": 16, "channels": 1, "stream_mode": "websocket"}
    api_key = Column(String(200))  # Hashed API key for device authentication
    # Per-device authentication secret (bcrypt-hashed). Issued at pairing-
    # code redemption, required for the satellite WebSocket and every
    # state-mutating satellite HTTP endpoint. Nullable for legacy rows but
    # any row with a NULL secret cannot authenticate — see task #85.
    device_secret = Column(String(200), nullable=True)
    current_session_id = Column(String(100), nullable=True)  # Active recording session
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)


class OrgProviderSettings(Base):
    """Per-organization provider configuration for pluggable AI services."""
    __tablename__ = "org_provider_settings"
    __table_args__ = (
        Index("ix_org_provider_settings_org_kind", "organization_id", "service_kind", unique=True),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    service_kind = Column(String(50), nullable=False)  # llm, stt, tts, embeddings, reranking, diarization
    provider_name = Column(String(50), nullable=False)  # litellm, openai, infinity, kokoro, etc.
    endpoint_url = Column(String(500), nullable=True)
    api_key_encrypted = Column(Text, nullable=True)  # Fernet-encrypted
    model_name = Column(String(200), nullable=True)
    overrides = Column(JSON, nullable=True)  # {"fast": {"model": "x"}, "quality": {"model": "y"}}
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class MeetingDigest(Base):
    """Cached time-window meeting digests."""
    __tablename__ = "meeting_digest"
    __table_args__ = (
        Index("ix_meeting_digest_org_period", "organization_id", "period", "date"),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    period = Column(String(20), nullable=False)  # day, week, month, quarter, year
    date = Column(String(10), nullable=False)  # YYYY-MM-DD
    project_id = Column(Text, nullable=True)
    content = Column(Text, nullable=False)
    meeting_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    # v3.18.3 background-jobs: arq job_id that is currently generating
    # (or last generated) this digest. NULL on cached reads; set when
    # a new generate kicks off.
    generation_job_id = Column(String(64), nullable=True)
    # v3.24.0 weekly-email-digest: timestamp the scheduled weekly-digest
    # cron emailed this row. NULL = never emailed (eligible to send). Set
    # after the first successful Postmark send so a cron restart / re-run /
    # two-worker race can't double-send. Only the "week" period rows are
    # ever stamped today; on-demand day/month/quarter/year digests leave it
    # NULL. See workers/weekly_digest_workers.py.
    emailed_at = Column(DateTime, nullable=True)


class UploadJob(Base):
    """Chunked media upload state and processing pipeline progress."""
    __tablename__ = "upload_jobs"
    __table_args__ = (
        Index("ix_upload_jobs_org_upload_id", "organization_id", "upload_id", unique=True),
        Index("ix_upload_jobs_stage", "stage"),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    upload_id = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4, index=True)
    filename = Column(Text, nullable=False)
    content_type = Column(Text, nullable=True)
    # server_default values match the create-table in 007_uploads. Declared
    # here so autogenerate stops flagging the drift.
    action = Column(String(20), nullable=False, default="transcribe", server_default="transcribe")
    total_size = Column(BigInteger, nullable=False)
    bytes_received = Column(BigInteger, nullable=False, default=0, server_default="0")
    chunks_received = Column(Integer, nullable=False, default=0, server_default="0")
    total_chunks = Column(Integer, nullable=False, default=0, server_default="0")
    stage = Column(String(30), nullable=False, default="queued", server_default="queued")
    last_completed_stage = Column(String(30), nullable=True)
    progress_pct = Column(Integer, nullable=False, default=0, server_default="0")
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0, server_default="0")
    # CASCADE on parent delete per 012_cascade_session_children.
    session_id = Column(
        Integer,
        ForeignKey("recording_sessions.id", ondelete="CASCADE"),
        nullable=True,
    )
    job_started_at = Column(DateTime, nullable=True)
    job_completed_at = Column(DateTime, nullable=True)
    # Per-upload transcription preferences. Falls back to org-level
    # ProviderRegistry settings when fields are missing. Schema in
    # services/transcription_options.py. Renders as JSONB on Postgres
    # (per 011_upload_transcription_options) and JSON on SQLite test fixtures.
    transcription_options = Column(_JsonB(), nullable=True)
    # SHA-256 of the assembled upload, computed once at finalize. Used to
    # skip re-transcribing byte-identical audio, and stamped into the
    # resulting session processing_metadata so later uploads can match it.
    audio_sha256 = Column(String(64), nullable=True, index=True)
    # Browser-reported File.lastModified (the only "file date" the web exposes —
    # there is no OS creation date over HTTP). Fed into the meeting-provenance
    # "recorded at" estimate so an upload defaults its meeting_date to when the
    # file was last saved, not the ingest time (see 050_upload_client_modified_at
    # + services/meeting_provenance.py).
    client_modified_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# ---------- Phase 3: Speaker Library ----------
# These are *org-level* speakers (a person across many meetings), distinct
# from the existing per-session `Speaker` model above which holds raw
# diarized labels for a single recording.
#
# Embeddings are stored as raw bytes + a dim column so we can swap models
# (ECAPA 192-d -> ECAPA-XL 256-d -> WavLM 768-d) without a schema change.
# Encode/decode helpers live in services/speaker_service.py.

from sqlalchemy import LargeBinary  # noqa: E402


class SpeakerProfile(Base):
    """Org-level speaker identity. One row per real person you want to
    recognise across meetings."""
    __tablename__ = "speaker"
    __table_args__ = (
        Index("ix_speaker_org", "organization_id"),
        Index("ix_speaker_org_name", "organization_id", "display_name", unique=True),
        Index("ix_speaker_contact_id", "contact_id"),
        # Added in 015_speaker_profile_contacts; declared here so
        # autogenerate doesn't try to drop it.
        Index("ix_speaker_linked_user", "linked_user_id"),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    display_name = Column(String(200), nullable=False)
    email = Column(String(320), nullable=True)
    # Contact / CRM fields. SpeakerProfile is the per-org contact
    # directory today; these promote it from a name+voice-fingerprint
    # row into a full contact record. Forward path is a shared
    # unicorn-contacts service that other apps reference by id.
    phone = Column(String(64), nullable=True)
    title = Column(String(200), nullable=True)   # CEO, PM, etc — their role at their company
    company = Column(String(200), nullable=True) # their employer / org (NOT our `organization_id`)
    linked_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # External CRM references: {hubspot_id, salesforce_id, ...}. Schema
    # is per-org; backend stores it verbatim, callers normalize. JSONB
    # on Postgres (per 015_speaker_profile_contacts) and JSON on SQLite.
    external_refs = Column(_JsonB(), nullable=True)
    # Contact-Ops person id. Auto participant stamping only uses this when
    # contact_link_confirmed=True; unconfirmed links are suggestions.
    contact_id = Column(String(64), nullable=True)
    contact_link_confirmed = Column(Boolean, nullable=False, default=False, server_default="false")
    contact_link_confirmed_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    contact_link_confirmed_at = Column(DateTime, nullable=True)
    # Meeting-Ops-local avatar override (043_speaker_photo_url). Highest
    # priority in the avatar chain: MO override > Contact-Ops federated
    # photo (via contact_id) > generated initials. Lets a user pin a face
    # even for a speaker not linked to a Contact-Ops person.
    photo_url = Column(String(1000), nullable=True)
    notes = Column(Text, nullable=True)
    centroid_embedding = Column(LargeBinary, nullable=True)
    embedding_dim = Column(Integer, nullable=True)
    embedding_model = Column(String(100), nullable=True)
    sample_count = Column(Integer, nullable=False, default=0, server_default="0")
    created_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class SpeakerVoiceSample(Base):
    """A single embedding + (optionally) the audio bytes that produced it.
    Created by enrollment uploads and by post-meeting auto-identification."""
    __tablename__ = "speaker_voice_sample"
    __table_args__ = (
        Index("ix_speaker_voice_sample_speaker", "speaker_id"),
        Index("ix_speaker_voice_sample_org", "organization_id"),
        Index("ix_speaker_voice_sample_session", "source_session_id"),
    )

    id = Column(Integer, primary_key=True)
    speaker_id = Column(Integer, ForeignKey("speaker.id", ondelete="CASCADE"), nullable=False)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    source = Column(String(50), nullable=False)  # "enrollment" | "session"
    source_session_id = Column(Integer, ForeignKey("recording_sessions.id", ondelete="SET NULL"), nullable=True)
    source_segment_idx = Column(Integer, nullable=True)
    embedding = Column(LargeBinary, nullable=False)
    embedding_dim = Column(Integer, nullable=False)
    embedding_model = Column(String(100), nullable=False)
    audio_path = Column(String(500), nullable=True)  # only set when STORE_SPEAKER_AUDIO=true
    duration_seconds = Column(Float, nullable=True)
    similarity_to_centroid = Column(Float, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class SpeakerSessionLink(Base):
    """Maps a recording_session's raw diarized label (e.g. "SPEAKER_00")
    to an org-level SpeakerProfile. One row per unique (session, raw_label)."""
    __tablename__ = "speaker_session_link"
    __table_args__ = (
        Index("ix_speaker_session_link_session", "session_id"),
        Index("ix_speaker_session_link_org", "organization_id"),
        Index("ix_speaker_session_link_speaker", "speaker_id"),
        Index("uq_speaker_session_link_label", "session_id", "raw_label", unique=True),
    )

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("recording_sessions.id", ondelete="CASCADE"), nullable=False)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    raw_label = Column(String(100), nullable=False)
    speaker_id = Column(Integer, ForeignKey("speaker.id", ondelete="SET NULL"), nullable=True)
    similarity = Column(Float, nullable=True)
    source = Column(String(20), nullable=False, default="auto", server_default="auto")  # "auto" | "manual"
    confirmed = Column(Boolean, nullable=False, default=False, server_default="false")
    confirmed_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class UserVoiceProfile(Base):
    """"My Voice" — ONE portable self-voiceprint per USER ACCOUNT (v3.36.0).

    SpeakerProfile is hard tenant-scoped (biometric privacy: no cross-tenant
    matching, ever). This row is the single sanctioned exception: it belongs
    to the ACCOUNT, and it participates in identification ONLY inside
    workspaces where that user is a member (services.speaker_service joins
    through user_organizations at identify time — the embedding itself never
    crosses a tenant boundary the user isn't already inside).

    consent_at is the biometric consent record — stamped ONLY by explicit
    user action (an is_me claim on a speaker-naming surface, or POST
    /api/me/voice/enroll-from-session; never by implicit inference such as
    an email match) and never cleared while the row exists. Deleting the row
    (DELETE /api/me/voice) destroys the voiceprint and recomputes any org
    SpeakerProfile centroids that were seeded from it.
    """
    __tablename__ = "user_voice_profile"
    __table_args__ = (
        Index("uq_user_voice_profile_user", "user_id", unique=True),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    centroid_embedding = Column(LargeBinary, nullable=True)
    embedding_dim = Column(Integer, nullable=True)
    embedding_model = Column(String(100), nullable=True)
    sample_count = Column(Integer, nullable=False, default=0, server_default="0")
    consent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class TtsJob(Base):
    """Asynchronous TTS rendering jobs (summary single-voice and podcast multi-voice).

    Mirrors the UploadJob shape so the frontend can reuse the same status +
    progress + retry affordances. The recording session referenced by
    session_id provides the source text; output_path holds the rendered
    audio location once stage='done'.
    """
    __tablename__ = "tts_jobs"
    __table_args__ = (
        Index("ix_tts_jobs_org_job_id", "organization_id", "job_id", unique=True),
        Index("ix_tts_jobs_stage", "stage"),
    )

    id = Column(Integer, primary_key=True)
    # No single-column index — the composite ix_tts_jobs_org_job_id above
    # already provides org_id lookups, matching what 009_tts_jobs created.
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    # job_id has unique=True; the index=True was redundant (the UNIQUE
    # constraint already provides lookups) and produced an extra ix_*
    # index in autogenerate. Dropped.
    job_id = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    # CASCADE on parent delete per 012_cascade_session_children.
    session_id = Column(
        Integer,
        ForeignKey("recording_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind = Column(String(20), nullable=False)  # 'summary' | 'podcast'
    voice = Column(String(80), nullable=True)         # used for summary
    host_voice = Column(String(80), nullable=True)    # used for podcast
    analyst_voice = Column(String(80), nullable=True) # used for podcast
    # server_default values match the create-table in 009_tts_jobs. Declared
    # here so autogenerate stops flagging the drift.
    format = Column(String(8), nullable=False, default="mp3", server_default="mp3")
    stage = Column(String(30), nullable=False, default="queued", server_default="queued")
    progress_pct = Column(Integer, nullable=False, default=0, server_default="0")
    output_path = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0, server_default="0")
    job_started_at = Column(DateTime, nullable=True)
    job_completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class SessionAttachment(Base):
    """File attached to a recording session.

    Created by 025_session_attachments. Stores related-but-not-primary
    artifacts the user wants to keep alongside the session: a coworker's
    Granola notes, an external vendor transcript, a slide deck, a PDF
    agenda, a photo of a whiteboard, etc.

    Storage is pluggable — `storage_backend` selects how `storage_key`
    is resolved at download time:

      'garage'  → S3 PutObject on the meeting-ops-attachments bucket,
                  key = "{org_id}/{session_id}/{uuid}/{filename}"
      'local'   → file on disk under RECORDINGS_DIR/attachments/{key}

    Cascade deletes ride the session row; storage-side cleanup happens
    in the API DELETE handler. Org scoping is enforced application-side
    via the `_resolve_session` helper pattern (all attachment endpoints
    re-verify the session belongs to the active org before any read or
    write of this table).
    """

    __tablename__ = "session_attachments"
    __table_args__ = (
        Index("ix_session_attachments_session", "session_id"),
        Index("ix_session_attachments_organization", "organization_id"),
        Index(
            "ix_session_attachments_org_session",
            "organization_id",
            "session_id",
        ),
    )

    # UUID PK lets the API surface attachments with a string id that's
    # safe to embed in URLs without leaking row counts (the integer
    # pk pattern on action_items reveals "we have ~N items" — fine for
    # action items, but attachments may include third-party material we
    # don't want to leak count of).
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(
        Integer,
        ForeignKey("recording_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    uploaded_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    filename = Column(String(500), nullable=False)
    mime_type = Column(String(200), nullable=True)
    size_bytes = Column(BigInteger, nullable=False)
    # Free-form short tag the UI uses to filter and pick an icon.
    # Common values: 'transcript', 'notes', 'document', 'audio', 'image',
    # 'video', 'other'. Intentionally not a CHECK so adding new types
    # doesn't need a migration.
    attachment_type = Column(String(50), nullable=False)
    # Free-form user description ("Granola notes from Mike",
    # "Otter transcript v2").
    source_label = Column(String(200), nullable=True)
    notes = Column(Text, nullable=True)
    # 'garage' | 'local' | 'forgejo' (forgejo reserved for future).
    storage_backend = Column(String(20), nullable=False)
    storage_key = Column(String(500), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )


# ---------------------------------------------------------------------------
# Bulk audio import — Phase 1 (alembic 029_bulk_import).
#
# Two tables that back the new /import page: a BulkImportJob row per
# drag-and-drop session, with N BulkImportFile rows under it (one per
# uploaded audio file). The job tracks aggregate counts + lifecycle
# timestamps that drive the progress UI; each file row tracks per-row
# state through the SHA-256 dedup -> parse -> create-session -> upload
# -> reprocess pipeline run by BulkImportPipelineQueue.
#
# UUID PKs on both so file_id/job_id appear cleanly in URLs. relationship
# is back-populated so worker code can walk job.files / file.job without
# extra queries. cascade='all, delete-orphan' so deleting a job tears
# down its file rows (matches the FK ondelete=CASCADE in the migration).
# session_id_when_created stays nullable + non-FK on purpose — the file
# row is the historical import record and must outlive the session it
# created if that session is later deleted.
# ---------------------------------------------------------------------------

from sqlalchemy.orm import relationship  # noqa: E402  # local re-import


class BulkImportJob(Base):
    """One drag-and-drop bulk import session.

    Status transitions: queued -> processing -> (complete | cancelled | failed).
    The worker pool flips status to processing when it picks up the first
    file; the API flips it to cancelled on POST /cancel. complete is set
    by the worker when it observes total_files == succeeded + failed +
    skipped and the cancelled_at flag is unset.
    """

    __tablename__ = "bulk_import_jobs"
    __table_args__ = (
        Index(
            "ix_bulk_import_jobs_user_status",
            "user_id",
            "status",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    status = Column(
        String(32),
        nullable=False,
        default="queued",
        server_default="queued",
    )
    total_files = Column(Integer, nullable=False, default=0, server_default="0")
    succeeded = Column(Integer, nullable=False, default=0, server_default="0")
    failed = Column(Integer, nullable=False, default=0, server_default="0")
    skipped = Column(Integer, nullable=False, default=0, server_default="0")
    cancelled_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    files = relationship(
        "BulkImportFile",
        back_populates="job",
        cascade="all, delete-orphan",
    )


class BulkImportFile(Base):
    """One uploaded audio file within a bulk import job.

    Per-row status transitions: queued -> uploading -> processing ->
    (complete | failed | skipped). 'uploading' covers the API receiving
    bytes; 'processing' covers the worker pool running the SHA-256 +
    parse + session-create + reprocess pipeline. 'skipped' is the
    duplicate-detection terminal state — file_sha256 matched an
    existing recording_sessions row for the same org and we never spent
    GPU time on it.

    session_id_when_created points at the RecordingSession row this
    file produced (UUID matching recording_sessions.session_id). NULL
    for skipped/failed rows. Intentionally non-FK so the file row
    survives session deletion.
    """

    __tablename__ = "bulk_import_files"
    __table_args__ = (
        Index(
            "ix_bulk_import_files_job_status",
            "job_id",
            "status",
        ),
        Index(
            "ix_bulk_import_files_sha256",
            "file_sha256",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id = Column(
        UUID(as_uuid=True),
        ForeignKey("bulk_import_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    original_filename = Column(Text, nullable=False)
    file_sha256 = Column(String(64), nullable=True)
    parsed_title = Column(Text, nullable=True)
    parsed_date = Column(Date, nullable=True)
    parsed_time = Column(Time, nullable=True)
    parsed_source = Column(String(32), nullable=True)
    parsed_confidence = Column(Float, nullable=True)
    status = Column(
        String(32),
        nullable=False,
        default="queued",
        server_default="queued",
    )
    session_id_when_created = Column(UUID(as_uuid=True), nullable=True)
    error_message = Column(Text, nullable=True)
    bytes_total = Column(BigInteger, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    job = relationship("BulkImportJob", back_populates="files")


class InviteRequest(Base):
    """Public landing-page invite request capture.

    Populated by the unauthenticated `POST /api/landing/invite-request`
    endpoint on the v3.19 polished launch landing page. Email is UNIQUE
    so duplicate submissions become a 200 "ok" no-op (idempotent).
    See `backend/api/landing.py` for the request handler and rate-limit
    behavior, and migration `035_invite_requests.py` for the schema.
    """

    __tablename__ = "invite_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(Text, nullable=False, unique=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    notes = Column(Text, nullable=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_invite_requests_created_at", "created_at"),
    )


class SupportRequest(Base):
    """Customer support contact-form capture (v3.21.0).

    Populated by `POST /api/support/contact` from the /contact page
    (both authed and anonymous). Unlike `InviteRequest`, email is NOT
    unique — repeat customers send multiple messages over time and the
    triage workflow wants the full history. The Postmark notify path
    is the canonical "ticket arrived" surface; this table doubles as a
    durable archive.
    """

    __tablename__ = "support_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(Text, nullable=False)
    name = Column(Text, nullable=True)
    subject = Column(Text, nullable=False)
    message = Column(Text, nullable=False)
    user_id = Column(Integer, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    notes = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_support_requests_created_at", "created_at"),
        Index("ix_support_requests_email", "email"),
    )
