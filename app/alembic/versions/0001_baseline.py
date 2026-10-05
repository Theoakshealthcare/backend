"""Baseline schema: the whole database in one migration.

Revision ID: 0001_baseline
Revises: (none)
Create Date: 2026-10-05

Replaces the previous 25+ migrations (two roots, tables created outside migrations) with one
history generated from the models. There was no production data, so nothing is converted.
Raw SQL that autogenerate can't express is added by hand: the append-only trigger on
admin_audit_log, and dropping the Postgres enum types on downgrade.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0001_baseline'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('booking_settings',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('base_fee_kobo', sa.BigInteger(), nullable=False),
    sa.Column('platform_fee_percent', sa.Float(), nullable=False),
    sa.Column('multipliers', sa.JSON(), nullable=True),
    sa.Column('surge_pricing', sa.Boolean(), nullable=True),
    sa.Column('surge_multiplier', sa.Float(), nullable=True),
    sa.Column('discount', sa.Float(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('fallguard_questions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('options', sa.Text(), nullable=False),
    sa.Column('order', sa.Integer(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('prescription_settings',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('base_fee_kobo', sa.BigInteger(), nullable=False),
    sa.Column('multipliers', sa.JSON(), nullable=True),
    sa.Column('surge_pricing', sa.Boolean(), nullable=True),
    sa.Column('surge_multiplier', sa.Float(), nullable=True),
    sa.Column('discount', sa.Float(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(), nullable=False),
    sa.Column('phone_number', sa.String(), nullable=True),
    sa.Column('full_name', sa.String(), nullable=False),
    sa.Column('hashed_password', sa.String(), nullable=False),
    sa.Column('user_type', sa.String(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=True),
    sa.Column('is_verified', sa.Boolean(), nullable=True),
    sa.Column('verification_code', sa.String(), nullable=True),
    sa.Column('verification_code_expires_at', sa.DateTime(), nullable=True),
    sa.Column('password_reset_token', sa.String(), nullable=True),
    sa.Column('password_reset_expires_at', sa.DateTime(), nullable=True),
    sa.Column('profile_image_url', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_phone_number'), 'users', ['phone_number'], unique=True)
    op.create_table('admin_audit_log',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('admin_user_id', sa.UUID(), nullable=False),
    sa.Column('action', sa.String(), nullable=False),
    sa.Column('entity_type', sa.String(), nullable=False),
    sa.Column('entity_id', sa.String(), nullable=True),
    sa.Column('before', sa.JSON(), nullable=True),
    sa.Column('after', sa.JSON(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('ip', sa.String(), nullable=True),
    sa.Column('user_agent', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['admin_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_admin_audit_log_admin_created', 'admin_audit_log', ['admin_user_id', 'created_at'], unique=False)
    op.create_index('ix_admin_audit_log_entity', 'admin_audit_log', ['entity_type', 'entity_id'], unique=False)
    op.create_table('admins',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(), nullable=False),
    sa.Column('full_name', sa.String(), nullable=False),
    sa.Column('phone_number', sa.String(), nullable=True),
    sa.Column('permissions', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id')
    )
    op.create_table('caregivers',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(), nullable=False),
    sa.Column('full_name', sa.String(), nullable=False),
    sa.Column('phone_number', sa.String(), nullable=True),
    sa.Column('date_of_birth', sa.Date(), nullable=True),
    sa.Column('gender', sa.String(), nullable=True),
    sa.Column('state', sa.String(), nullable=True),
    sa.Column('address', sa.Text(), nullable=True),
    sa.Column('emergency_contact_phone', sa.String(), nullable=True),
    sa.Column('emergency_contact_name', sa.String(), nullable=True),
    sa.Column('emergency_contact_address', sa.String(), nullable=True),
    sa.Column('specialization', sa.String(), nullable=True),
    sa.Column('years_of_experience', sa.Integer(), nullable=True),
    sa.Column('preferred_work_hours', sa.String(), nullable=True),
    sa.Column('service_areas', postgresql.ARRAY(sa.String()), nullable=True),
    sa.Column('profile_completion_step', sa.Integer(), nullable=True),
    sa.Column('identity_verification_status', sa.String(), nullable=True),
    sa.Column('background_check_status', sa.String(), nullable=True),
    sa.Column('profile_status', sa.String(), nullable=True),
    sa.Column('approved_by', sa.UUID(), nullable=True),
    sa.Column('approved_at', sa.DateTime(), nullable=True),
    sa.Column('suspended_at', sa.DateTime(), nullable=True),
    sa.Column('suspension_reason', sa.Text(), nullable=True),
    sa.Column('rejection_reason', sa.Text(), nullable=True),
    sa.Column('changes_requested', sa.JSON(), nullable=True),
    sa.Column('background_check_evidence', sa.JSON(), nullable=True),
    sa.Column('background_check_decided_by', sa.UUID(), nullable=True),
    sa.Column('background_check_decided_at', sa.DateTime(), nullable=True),
    sa.Column('onfido_applicant_id', sa.String(), nullable=True),
    sa.Column('onfido_check_id', sa.String(), nullable=True),
    sa.Column('terms_accepted', sa.Boolean(), nullable=True),
    sa.Column('privacy_policy_accepted', sa.Boolean(), nullable=True),
    sa.Column('background_check_consent', sa.Boolean(), nullable=True),
    sa.Column('consent_date', sa.DateTime(), nullable=True),
    sa.Column('document_verification_result', sa.JSON(), nullable=True),
    sa.Column('face_verification_result', sa.JSON(), nullable=True),
    sa.Column('face_verification_status', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['approved_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['background_check_decided_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id')
    )
    op.create_table('patients',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(), nullable=False),
    sa.Column('full_name', sa.String(), nullable=False),
    sa.Column('phone_number', sa.String(), nullable=True),
    sa.Column('date_of_birth', sa.Date(), nullable=True),
    sa.Column('gender', sa.String(), nullable=True),
    sa.Column('address', sa.Text(), nullable=True),
    sa.Column('city', sa.String(), nullable=True),
    sa.Column('state', sa.String(), nullable=True),
    sa.Column('postal_code', sa.String(), nullable=True),
    sa.Column('emergency_contact_name', sa.String(), nullable=True),
    sa.Column('emergency_contact_phone', sa.String(), nullable=True),
    sa.Column('emergency_contact_relationship', sa.String(), nullable=True),
    sa.Column('emergency_contact_address', sa.String(), nullable=True),
    sa.Column('blood_group', sa.String(), nullable=True),
    sa.Column('allergies', sa.Text(), nullable=True),
    sa.Column('medical_conditions', sa.Text(), nullable=True),
    sa.Column('medications', sa.Text(), nullable=True),
    sa.Column('preferred_language', sa.String(), nullable=True),
    sa.Column('special_requirements', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=True),
    sa.Column('profile_completion_step', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id')
    )
    op.create_table('bookings',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('booking_code', sa.String(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('caregiver_id', sa.UUID(), nullable=True),
    sa.Column('service_type', sa.String(), nullable=False),
    sa.Column('assistance_type', sa.String(), nullable=False),
    sa.Column('condition', sa.String(), nullable=True),
    sa.Column('duration_hours', sa.Integer(), nullable=False),
    sa.Column('scheduled_date', sa.DateTime(), nullable=False),
    sa.Column('scheduled_end_date', sa.DateTime(), nullable=False),
    sa.Column('actual_start_time', sa.DateTime(), nullable=True),
    sa.Column('actual_end_time', sa.DateTime(), nullable=True),
    sa.Column('patient_address', sa.Text(), nullable=False),
    sa.Column('patient_latitude', sa.Float(), nullable=False),
    sa.Column('patient_longitude', sa.Float(), nullable=False),
    sa.Column('patient_city', sa.String(), nullable=False),
    sa.Column('patient_state', sa.String(), nullable=False),
    sa.Column('status', sa.Enum('PENDING', 'ACCEPTED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED', 'DECLINED', name='bookingstatus'), nullable=True),
    sa.Column('payment_status', sa.Enum('PENDING', 'PAID', 'FAILED', 'REFUNDED', name='paymentstatus'), nullable=True),
    sa.Column('payment_expires_at', sa.DateTime(), nullable=True),
    sa.Column('cancel_reason', sa.String(), nullable=True),
    sa.Column('refund_required', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('refund_required_reason', sa.Text(), nullable=True),
    sa.Column('dispatch_state', sa.String(), nullable=True),
    sa.Column('dispatch_started_at', sa.DateTime(), nullable=True),
    sa.Column('total_amount_kobo', sa.BigInteger(), nullable=False),
    sa.Column('service_fee_kobo', sa.BigInteger(), nullable=False),
    sa.Column('platform_fee_kobo', sa.BigInteger(), nullable=False),
    sa.Column('paystack_reference', sa.String(), nullable=True),
    sa.Column('payment_failure_reason', sa.Text(), nullable=True),
    sa.Column('special_instructions', sa.Text(), nullable=True),
    sa.Column('booking_for', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.CheckConstraint("dispatch_state IN ('dispatching','matched','unmatched')", name='ck_bookings_dispatch_state'),
    sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id'], ),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('booking_code')
    )
    op.create_table('caregiver_bank_accounts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('caregiver_id', sa.UUID(), nullable=False),
    sa.Column('bank_name', sa.String(), nullable=False),
    sa.Column('account_number', sa.String(), nullable=False),
    sa.Column('account_name', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('caregiver_id')
    )
    op.create_table('caregiver_certifications',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('caregiver_id', sa.UUID(), nullable=False),
    sa.Column('certification_name', sa.String(), nullable=False),
    sa.Column('issuing_organization', sa.String(), nullable=False),
    sa.Column('issue_date', sa.Date(), nullable=True),
    sa.Column('expiry_date', sa.Date(), nullable=True),
    sa.Column('certification_number', sa.String(), nullable=True),
    sa.Column('file_path', sa.String(), nullable=True),
    sa.Column('verification_status', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('caregiver_documents',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('caregiver_id', sa.UUID(), nullable=False),
    sa.Column('document_type', sa.String(), nullable=False),
    sa.Column('document_name', sa.String(), nullable=False),
    sa.Column('file_path', sa.String(), nullable=False),
    sa.Column('file_size', sa.Integer(), nullable=True),
    sa.Column('mime_type', sa.String(), nullable=True),
    sa.Column('verification_status', sa.String(), nullable=True),
    sa.Column('onfido_document_id', sa.String(), nullable=True),
    sa.Column('extraction_data', sa.JSON(), nullable=True),
    sa.Column('uploaded_at', sa.DateTime(), nullable=True),
    sa.Column('verified_at', sa.DateTime(), nullable=True),
    sa.Column('reviewed_by', sa.UUID(), nullable=True),
    sa.Column('rejection_reason', sa.Text(), nullable=True),
    sa.Column('expires_on', sa.Date(), nullable=True),
    sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id'], ),
    sa.ForeignKeyConstraint(['reviewed_by'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('caregiver_locations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('caregiver_id', sa.UUID(), nullable=False),
    sa.Column('latitude', sa.Float(), nullable=False),
    sa.Column('longitude', sa.Float(), nullable=False),
    sa.Column('accuracy', sa.Float(), nullable=True),
    sa.Column('heading', sa.Float(), nullable=True),
    sa.Column('speed', sa.Float(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=True),
    sa.Column('is_available', sa.Boolean(), nullable=True),
    sa.Column('recorded_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('caregiver_skills',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('caregiver_id', sa.UUID(), nullable=False),
    sa.Column('service_type', sa.String(), nullable=False),
    sa.Column('sub_specialty', sa.String(), nullable=True),
    sa.Column('created_by_admin_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("service_type IN ('nurse','doctor','physiotherapist','caregiver','nutritionist')", name='ck_caregiver_skills_service_type'),
    sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['created_by_admin_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('uq_caregiver_skills_caregiver_service_sub', 'caregiver_skills', ['caregiver_id', 'service_type', sa.literal_column("coalesce(sub_specialty, '')")], unique=True)
    op.create_table('caregiver_transactions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('caregiver_id', sa.UUID(), nullable=False),
    sa.Column('type', sa.String(), nullable=False),
    sa.Column('amount', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('bank_name', sa.String(), nullable=True),
    sa.Column('account_number', sa.String(), nullable=True),
    sa.Column('account_name', sa.String(), nullable=True),
    sa.Column('reference', sa.String(), nullable=True),
    sa.Column('narration', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('caregiver_wallets',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('caregiver_id', sa.UUID(), nullable=False),
    sa.Column('balance', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('caregiver_id')
    )
    op.create_table('fallguard_assessments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('total_score', sa.Integer(), nullable=False),
    sa.Column('ai_summary_encrypted', sa.Text(), nullable=True),
    sa.Column('ai_recommendations_encrypted', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('patient_caregiver_access',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('caregiver_id', sa.UUID(), nullable=False),
    sa.Column('granted_by_patient', sa.Boolean(), nullable=False),
    sa.Column('granted_at', sa.DateTime(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id'], ),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('patient_family_members',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('full_name', sa.String(), nullable=False),
    sa.Column('relation_type', sa.String(), nullable=False),
    sa.Column('phone_number', sa.String(), nullable=True),
    sa.Column('email', sa.String(), nullable=True),
    sa.Column('is_emergency_contact', sa.Boolean(), nullable=True),
    sa.Column('can_make_medical_decisions', sa.Boolean(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('patient_medical_records',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('record_type', sa.String(), nullable=False),
    sa.Column('title', sa.String(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('date_recorded', sa.Date(), nullable=True),
    sa.Column('doctor_name', sa.String(), nullable=True),
    sa.Column('hospital_clinic', sa.String(), nullable=True),
    sa.Column('severity', sa.String(), nullable=True),
    sa.Column('file_path', sa.String(), nullable=True),
    sa.Column('file_name', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('patient_vitals',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('recorded_by_user_id', sa.UUID(), nullable=False),
    sa.Column('source', sa.Enum('manual', 'wearable', name='vitalsource'), nullable=False),
    sa.Column('vital_type', sa.String(), nullable=False),
    sa.Column('value', sa.String(), nullable=False),
    sa.Column('unit', sa.String(), nullable=True),
    sa.Column('recorded_at', sa.DateTime(), nullable=True),
    sa.Column('notes', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ),
    sa.ForeignKeyConstraint(['recorded_by_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('patient_wallets',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('balance_kobo', sa.BigInteger(), nullable=False),
    sa.Column('total_deposited_kobo', sa.BigInteger(), nullable=False),
    sa.Column('total_spent_kobo', sa.BigInteger(), nullable=False),
    sa.Column('total_refunded_kobo', sa.BigInteger(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('patient_id')
    )
    op.create_table('prescriptions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('requested_by_user_id', sa.UUID(), nullable=False),
    sa.Column('medication_name', sa.String(), nullable=False),
    sa.Column('dosage', sa.String(), nullable=True),
    sa.Column('frequency', sa.String(), nullable=True),
    sa.Column('duration', sa.String(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('additional_info', sa.Text(), nullable=True),
    sa.Column('doctor_note_path', sa.String(), nullable=True),
    sa.Column('delivery_option', sa.String(), nullable=False),
    sa.Column('address', sa.Text(), nullable=False),
    sa.Column('status', sa.Enum('PENDING', 'AVAILABLE', 'PAID', 'DELIVERING', 'DELIVERED', 'CANCELLED', name='prescriptionstatus'), nullable=True),
    sa.Column('payment_status', sa.String(), nullable=True),
    sa.Column('total_cost_kobo', sa.BigInteger(), nullable=True),
    sa.Column('paystack_reference', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ),
    sa.ForeignKeyConstraint(['requested_by_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('booking_dispatch_waves',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('booking_id', sa.UUID(), nullable=False),
    sa.Column('wave_no', sa.Integer(), nullable=False),
    sa.Column('notified_caregiver_ids', postgresql.ARRAY(sa.UUID()), nullable=False),
    sa.Column('sent_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('booking_id', 'wave_no', name='uq_booking_dispatch_waves_booking_wave')
    )
    op.create_table('booking_notes',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('booking_id', sa.UUID(), nullable=False),
    sa.Column('caregiver_id', sa.UUID(), nullable=False),
    sa.Column('title', sa.String(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('audio_transcript', sa.Text(), nullable=True),
    sa.Column('audio_file_url', sa.String(), nullable=True),
    sa.Column('ai_summary', sa.Text(), nullable=True),
    sa.Column('ai_keywords', sa.JSON(), nullable=True),
    sa.Column('ai_recommendations', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], ),
    sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('booking_notifications',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('booking_id', sa.UUID(), nullable=False),
    sa.Column('caregiver_id', sa.UUID(), nullable=False),
    sa.Column('title', sa.String(), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('notification_type', sa.String(), nullable=False),
    sa.Column('is_read', sa.Boolean(), nullable=True),
    sa.Column('is_sent', sa.Boolean(), nullable=True),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.Column('read_at', sa.DateTime(), nullable=True),
    sa.Column('response', sa.String(), nullable=True),
    sa.Column('response_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], ),
    sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('chat_messages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('booking_id', sa.UUID(), nullable=False),
    sa.Column('sender_id', sa.UUID(), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('message_type', sa.String(), nullable=True),
    sa.Column('attachment_url', sa.String(), nullable=True),
    sa.Column('is_read', sa.Boolean(), nullable=True),
    sa.Column('read_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'], ),
    sa.ForeignKeyConstraint(['sender_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('fallguard_answers',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('assessment_id', sa.UUID(), nullable=False),
    sa.Column('question_id', sa.UUID(), nullable=False),
    sa.Column('answer_encrypted', sa.Text(), nullable=False),
    sa.Column('score', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['assessment_id'], ['fallguard_assessments.id'], ),
    sa.ForeignKeyConstraint(['question_id'], ['fallguard_questions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('patient_transactions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('wallet_id', sa.UUID(), nullable=False),
    sa.Column('type', sa.String(), nullable=False),
    sa.Column('amount_kobo', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('paystack_reference', sa.String(), nullable=True),
    sa.Column('paystack_status', sa.String(), nullable=True),
    sa.Column('payment_method', sa.String(), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('booking_id', sa.UUID(), nullable=True),
    sa.Column('refund_reason', sa.Text(), nullable=True),
    sa.Column('admin_approved', sa.Boolean(), nullable=True),
    sa.Column('admin_id', sa.UUID(), nullable=True),
    sa.Column('approved_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ),
    sa.ForeignKeyConstraint(['wallet_id'], ['patient_wallets.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('paystack_reference')
    )
    op.create_index('idx_patient_transactions_created_at', 'patient_transactions', ['created_at'], unique=False)
    op.create_index('idx_patient_transactions_patient_id', 'patient_transactions', ['patient_id'], unique=False)
    op.create_index('idx_patient_transactions_paystack_reference', 'patient_transactions', ['paystack_reference'], unique=False)
    op.create_index('idx_patient_transactions_status', 'patient_transactions', ['status'], unique=False)
    op.create_table('patient_refund_requests',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('patient_id', sa.UUID(), nullable=False),
    sa.Column('transaction_id', sa.UUID(), nullable=False),
    sa.Column('amount_kobo', sa.BigInteger(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('status', sa.String(), nullable=True),
    sa.Column('admin_id', sa.UUID(), nullable=True),
    sa.Column('admin_notes', sa.Text(), nullable=True),
    sa.Column('processed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['patient_id'], ['patients.id'], ),
    sa.ForeignKeyConstraint(['transaction_id'], ['patient_transactions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_patient_refund_requests_created_at', 'patient_refund_requests', ['created_at'], unique=False)
    op.create_index('idx_patient_refund_requests_patient_id', 'patient_refund_requests', ['patient_id'], unique=False)
    op.create_index('idx_patient_refund_requests_status', 'patient_refund_requests', ['status'], unique=False)

    # admin_audit_log is append-only at the database level (the ORM guards it too).
    op.execute(
        """
        CREATE FUNCTION admin_audit_log_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'admin_audit_log is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER admin_audit_log_no_change
        BEFORE UPDATE OR DELETE ON admin_audit_log
        FOR EACH ROW EXECUTE FUNCTION admin_audit_log_immutable()
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP TRIGGER IF EXISTS admin_audit_log_no_change ON admin_audit_log")
    op.execute("DROP FUNCTION IF EXISTS admin_audit_log_immutable()")
    op.drop_index('idx_patient_refund_requests_status', table_name='patient_refund_requests')
    op.drop_index('idx_patient_refund_requests_patient_id', table_name='patient_refund_requests')
    op.drop_index('idx_patient_refund_requests_created_at', table_name='patient_refund_requests')
    op.drop_table('patient_refund_requests')
    op.drop_index('idx_patient_transactions_status', table_name='patient_transactions')
    op.drop_index('idx_patient_transactions_paystack_reference', table_name='patient_transactions')
    op.drop_index('idx_patient_transactions_patient_id', table_name='patient_transactions')
    op.drop_index('idx_patient_transactions_created_at', table_name='patient_transactions')
    op.drop_table('patient_transactions')
    op.drop_table('fallguard_answers')
    op.drop_table('chat_messages')
    op.drop_table('booking_notifications')
    op.drop_table('booking_notes')
    op.drop_table('booking_dispatch_waves')
    op.drop_table('prescriptions')
    op.drop_table('patient_wallets')
    op.drop_table('patient_vitals')
    op.drop_table('patient_medical_records')
    op.drop_table('patient_family_members')
    op.drop_table('patient_caregiver_access')
    op.drop_table('fallguard_assessments')
    op.drop_table('caregiver_wallets')
    op.drop_table('caregiver_transactions')
    op.drop_index('uq_caregiver_skills_caregiver_service_sub', table_name='caregiver_skills')
    op.drop_table('caregiver_skills')
    op.drop_table('caregiver_locations')
    op.drop_table('caregiver_documents')
    op.drop_table('caregiver_certifications')
    op.drop_table('caregiver_bank_accounts')
    op.drop_table('bookings')
    op.drop_table('patients')
    op.drop_table('caregivers')
    op.drop_table('admins')
    op.drop_index('ix_admin_audit_log_entity', table_name='admin_audit_log')
    op.drop_index('ix_admin_audit_log_admin_created', table_name='admin_audit_log')
    op.drop_table('admin_audit_log')
    op.drop_index(op.f('ix_users_phone_number'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
    op.drop_table('prescription_settings')
    op.drop_table('fallguard_questions')
    op.drop_table('booking_settings')
    # ### end Alembic commands ###

    # Postgres keeps enum types after their tables are dropped.
    sa.Enum(name="bookingstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="paymentstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="prescriptionstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="vitalsource").drop(op.get_bind(), checkfirst=True)
