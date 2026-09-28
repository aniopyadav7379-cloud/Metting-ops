"""cascade-delete children of recording_sessions

Revision ID: 012_cascade_session_children
Revises: 011_upload_transcription_options
Create Date: 2026-05-15 00:00:00.000000

Today the FKs from upload_jobs / agent_sessions / meeting_chunk_embedding
/ tts_jobs to recording_sessions are NO ACTION, so deleting a recording
session 500s with a foreign-key violation if it ever had an upload job,
agent conversation, embedding row, or queued TTS render. All four are
derived data, not user-meaningful state — they should die with the
parent. Flip them to ON DELETE CASCADE.

speaker_voice_sample stays SET NULL (voice samples outlive any single
meeting they were enrolled from). speaker_session_link is already
CASCADE, no change needed.
"""

from alembic import op


revision = "012_cascade_session_children"
down_revision = "011_upload_transcription_options"
branch_labels = None
depends_on = None


# (table, fk_name, column) tuples that should cascade on parent delete.
_FKS = [
    ("upload_jobs", "upload_jobs_session_id_fkey", "session_id"),
    ("agent_sessions", "agent_sessions_meeting_session_id_fkey", "meeting_session_id"),
    ("meeting_chunk_embedding", "meeting_chunk_embedding_meeting_id_fkey", "meeting_id"),
    ("tts_jobs", "tts_jobs_session_id_fkey", "session_id"),
]


def upgrade() -> None:
    for table, fk, col in _FKS:
        op.drop_constraint(fk, table, type_="foreignkey")
        op.create_foreign_key(
            fk,
            source_table=table,
            referent_table="recording_sessions",
            local_cols=[col],
            remote_cols=["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    for table, fk, col in _FKS:
        op.drop_constraint(fk, table, type_="foreignkey")
        op.create_foreign_key(
            fk,
            source_table=table,
            referent_table="recording_sessions",
            local_cols=[col],
            remote_cols=["id"],
        )
