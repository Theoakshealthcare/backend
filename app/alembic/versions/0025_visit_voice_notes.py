"""AI voice notes: ``visit_voice_notes`` and ``voice_note_events``.

Revision ID: 0025_visit_voice_notes
Revises: 0024_arrival_policy_cancel_note
Create Date: 2026-11-08

Two new tables, no change to an existing one (docs/plans/2026-11-07-ai-note-transcription.md section 4). The plan reserved 0026;
slice 2 took 0024 on the branch this one is cut from, so 0025 is the next free number.

* ``visit_voice_notes``: one row per recording. The consent flag (``consent_confirmed_at``, ``consent_version``) is set at creation. The
  transcript and summary are Fernet-encrypted and nullable so retention and erasure can wipe them. No audio is stored here.
* ``voice_note_events``: the audit trail. No foreign key to the note, so a deleted note keeps its history. It never holds text.

Downgrade drops both tables (any note or event is lost; nothing existed before this feature).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0025_visit_voice_notes'
down_revision: Union[str, None] = '0024_arrival_policy_cancel_note'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATUSES = "'awaiting_audio', 'processing', 'ready', 'failed', 'saved'"


def upgrade() -> None:
    op.create_table(
        'visit_voice_notes',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('booking_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('bookings.id'), nullable=False),
        sa.Column('caregiver_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('caregivers.id'), nullable=False),
        sa.Column('client_request_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default=sa.text("'awaiting_audio'")),
        sa.Column('language', sa.String(), nullable=False),
        sa.Column('duration_seconds', sa.Integer(), nullable=False),
        sa.Column('consent_confirmed_at', sa.DateTime(), nullable=False),
        sa.Column('consent_version', sa.String(), nullable=False),
        sa.Column('audio_bytes', sa.Integer(), nullable=True),
        sa.Column('audio_mime', sa.String(), nullable=True),
        sa.Column('transcript_encrypted', sa.Text(), nullable=True),
        sa.Column('summary_encrypted', sa.Text(), nullable=True),
        sa.Column('edited', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('error_code', sa.String(), nullable=True),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('claimed_at', sa.DateTime(), nullable=True),
        sa.Column('provider', sa.String(), nullable=True),
        sa.Column('model', sa.String(), nullable=True),
        sa.Column('prompt_version', sa.String(), nullable=True),
        sa.Column('tokens_in', sa.Integer(), nullable=True),
        sa.Column('tokens_out', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('ready_at', sa.DateTime(), nullable=True),
        sa.Column('saved_at', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('retain_until', sa.DateTime(), nullable=True),
        sa.CheckConstraint(f"status IN ({STATUSES})", name='ck_visit_voice_notes_status'),
        sa.CheckConstraint("language IN ('en', 'pcm')", name='ck_visit_voice_notes_language'),
        sa.CheckConstraint('duration_seconds BETWEEN 2 AND 600', name='ck_visit_voice_notes_duration'),
    )
    op.create_index('ix_visit_voice_notes_booking_created', 'visit_voice_notes', ['booking_id', 'created_at'])
    op.create_index('ix_visit_voice_notes_caregiver_created', 'visit_voice_notes', ['caregiver_id', 'created_at'])
    op.create_index('uq_visit_voice_notes_request', 'visit_voice_notes', ['booking_id', 'client_request_id'], unique=True,
                    postgresql_where=sa.text('client_request_id IS NOT NULL'))
    op.create_index('ix_visit_voice_notes_open', 'visit_voice_notes', ['updated_at'],
                    postgresql_where=sa.text("status IN ('awaiting_audio', 'processing')"))
    op.create_index('ix_visit_voice_notes_retain', 'visit_voice_notes', ['retain_until'],
                    postgresql_where=sa.text("status = 'saved'"))

    op.create_table(
        'voice_note_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('voice_note_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('booking_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('bookings.id'), nullable=False),
        sa.Column('actor_type', sa.String(), nullable=False),
        sa.Column('actor_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('action', sa.String(), nullable=False),
        sa.Column('detail', postgresql.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint("actor_type IN ('caregiver', 'patient', 'admin', 'system')", name='ck_voice_note_events_actor'),
    )
    op.create_index('ix_voice_note_events_booking_created', 'voice_note_events', ['booking_id', 'created_at'])
    op.create_index('ix_voice_note_events_action_created', 'voice_note_events', ['action', 'created_at'])


def downgrade() -> None:
    op.drop_table('voice_note_events')
    op.drop_table('visit_voice_notes')
