"""Care alerts: ``care_alerts`` (the admin queue) and ``care_alert_occurrences`` (the dedupe ledger).

Revision ID: 0035_care_alerts
Revises: 0034_booking_reschedule
Create Date: 2026-11-25

Slice 1c of the clinician-approved build. Two new tables, no change to an existing one and no new admin permission (the console
queue sits behind ``support.inbox``). The number 0035 was reserved for this slice; the current single head on develop (after #87) is 0034, so ``down_revision`` points there and the lead re-points the chain when the other slices merge.

Downgrade drops both tables (every alert and its ledger are lost; nothing existed before this feature).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0035_care_alerts'
down_revision: Union[str, None] = '0034_booking_reschedule'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'care_alerts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('type', sa.String(), nullable=False),
        sa.Column('severity', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default=sa.text("'open'")),
        sa.Column('patient_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('patients.id'), nullable=False),
        sa.Column('care_recipient_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('care_recipients.id'), nullable=True),
        sa.Column('booking_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('bookings.id'), nullable=True),
        sa.Column('vital_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('patient_vitals.id', ondelete='SET NULL'), nullable=True),
        sa.Column('summary', sa.String(), nullable=False),
        sa.Column('detail_encrypted', sa.Text(), nullable=True),
        sa.Column('occurrence_count', sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column('last_occurred_at', sa.DateTime(), nullable=False),
        sa.Column('family_notified_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('family_notified_at', sa.DateTime(), nullable=True),
        sa.Column('admin_notified_at', sa.DateTime(), nullable=True),
        sa.Column('acknowledged_by_admin_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('admin_users.id'), nullable=True),
        sa.Column('acknowledged_at', sa.DateTime(), nullable=True),
        sa.Column('resolved_by_admin_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('admin_users.id'), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('note_encrypted', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint("type IN ('vital_out_of_range', 'task_missed')", name='ck_care_alerts_type'),
        sa.CheckConstraint("severity IN ('warning', 'critical')", name='ck_care_alerts_severity'),
        sa.CheckConstraint("status IN ('open', 'acknowledged', 'resolved')", name='ck_care_alerts_status'),
        sa.CheckConstraint('occurrence_count >= 1', name='ck_care_alerts_occurrences'),
    )
    op.create_index('ix_care_alerts_queue', 'care_alerts', ['status', 'severity', sa.text('last_occurred_at DESC')])
    op.create_index('ix_care_alerts_debounce', 'care_alerts', ['patient_id', 'type', 'severity', sa.text('last_occurred_at DESC')])

    op.create_table(
        'care_alert_occurrences',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('alert_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('care_alerts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('source_key', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('source_key', name='uq_care_alert_occurrences_source_key'),
    )
    op.create_index('ix_care_alert_occurrences_alert_id', 'care_alert_occurrences', ['alert_id'])


def downgrade() -> None:
    op.drop_index('ix_care_alert_occurrences_alert_id', table_name='care_alert_occurrences')
    op.drop_table('care_alert_occurrences')
    op.drop_index('ix_care_alerts_debounce', table_name='care_alerts')
    op.drop_index('ix_care_alerts_queue', table_name='care_alerts')
    op.drop_table('care_alerts')
