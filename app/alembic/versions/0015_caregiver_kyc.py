"""Caregiver KYC (Dojah, was Prembly until 2026-10-30): attempts, webhook ledger, system-decision trail; drop the Onfido-era columns.

Revision ID: 0015_caregiver_kyc
Revises: 0014_refunds_v2
Create Date: 2026-10-27

Spec: docs/plans/2026-10-27-caregiver-kyc-design.md sections 7 and 9.

- ``identity_verifications``: one row per attempt (status CHECK, one active attempt per caregiver by a partial unique index).
- ``kyc_webhook_events``: the webhook ledger (``token`` UNIQUE is the dedupe key). The body is never stored.
- ``identity_verification_events``: append-only trail of system decisions, refused for UPDATE/DELETE/TRUNCATE by a trigger like
  ``consents`` (``admin_audit_log.admin_user_id`` is NOT NULL, so a system actor cannot be written there).
- ``caregivers``: CHECK on ``identity_verification_status`` (gains ``needs_review``); drops ``onfido_applicant_id``,
  ``onfido_check_id``, ``document_verification_result``, ``face_verification_result``, ``face_verification_status`` (the two
  JSON columns held whatever the client posted, which could include images).
- ``caregiver_documents``: drops ``onfido_document_id``.
- ``consents``: the ``identity_verification`` type.

Downgrade drops the three tables, restores the dropped columns EMPTY (their data is gone), maps ``needs_review`` back to
``in_progress`` and deletes ``identity_verification`` consent rows (the old type CHECK cannot hold them; the append-only trigger is
switched off for that one delete and back on). Do not downgrade once real verifications exist; write a forward migration instead.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0015_caregiver_kyc'
down_revision: Union[str, None] = '0014_refunds_v2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACTIVE = "'created','started','awaiting_result'"
STATUSES = ACTIVE + ",'passed','failed','needs_review','expired','error'"
ID_TYPES = "'nin','bvn','drivers_licence','voters_card','passport'"
OUTCOMES = "'applied','duplicate','orphan','ignored_terminal','disagreement','error'"
IDENTITY_OLD = "'pending','in_progress','verified','failed'"
IDENTITY_NEW = "'pending','in_progress','needs_review','verified','failed'"
CONSENT_TYPES_OLD = "'terms', 'health_processing', 'marketing', 'share_with_family'"
CONSENT_TYPES_NEW = CONSENT_TYPES_OLD + ", 'identity_verification'"


def upgrade() -> None:
    # A value outside the new CHECK would fail the upgrade; anything unknown starts over as pending (no human decision is lost:
    # only verified and failed are decisions, and both are kept).
    op.execute(f"UPDATE caregivers SET identity_verification_status = 'pending' "
               f"WHERE identity_verification_status NOT IN ({IDENTITY_NEW})")
    op.create_check_constraint('ck_caregivers_identity_verification_status', 'caregivers',
                               f"identity_verification_status IN ({IDENTITY_NEW})")
    for column in ('onfido_applicant_id', 'onfido_check_id', 'document_verification_result', 'face_verification_result',
                   'face_verification_status'):
        op.drop_column('caregivers', column)
    op.drop_column('caregiver_documents', 'onfido_document_id')

    op.drop_constraint('ck_consents_type', 'consents', type_='check')
    op.create_check_constraint('ck_consents_type', 'consents', f"type IN ({CONSENT_TYPES_NEW})")

    op.create_table(
        'identity_verifications',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('caregiver_id', sa.UUID(), nullable=False),
        sa.Column('attempt_no', sa.Integer(), nullable=False),
        sa.Column('reference', sa.UUID(), nullable=False),
        sa.Column('provider', sa.String(), server_default='dojah', nullable=False),
        sa.Column('provider_session_id', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('reason_codes', postgresql.ARRAY(sa.String()), server_default=sa.text("'{}'::varchar[]"), nullable=False),
        sa.Column('id_type', sa.String(), nullable=True),
        sa.Column('face_confidence', sa.Numeric(5, 2), nullable=True),
        sa.Column('liveness_confidence', sa.Numeric(5, 2), nullable=True),
        sa.Column('face_threshold', sa.Integer(), nullable=True),
        sa.Column('name_match', sa.Boolean(), nullable=True),
        sa.Column('dob_match', sa.Boolean(), nullable=True),
        sa.Column('id_fingerprint', sa.String(), nullable=True),
        sa.Column('provider_reference', sa.String(), nullable=True),
        sa.Column('provider_cost', sa.Numeric(12, 2), nullable=True),
        sa.Column('decided_via', sa.String(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('reference'),
        sa.UniqueConstraint('provider_session_id'),
        sa.UniqueConstraint('caregiver_id', 'attempt_no', name='uq_identity_verifications_caregiver_attempt'),
        sa.CheckConstraint(f"status IN ({STATUSES})", name='ck_identity_verifications_status'),
        sa.CheckConstraint('attempt_no >= 1', name='ck_identity_verifications_attempt_no'),
        sa.CheckConstraint(f"id_type IS NULL OR id_type IN ({ID_TYPES})", name='ck_identity_verifications_id_type'),
        sa.CheckConstraint("decided_via IS NULL OR decided_via IN ('provider','admin')",
                           name='ck_identity_verifications_decided_via'),
        sa.CheckConstraint('face_confidence IS NULL OR (face_confidence >= 0 AND face_confidence <= 100)',
                           name='ck_identity_verifications_face_confidence'),
        sa.CheckConstraint('liveness_confidence IS NULL OR (liveness_confidence >= 0 AND liveness_confidence <= 100)',
                           name='ck_identity_verifications_liveness_confidence'),
        sa.CheckConstraint('face_threshold IS NULL OR (face_threshold >= 0 AND face_threshold <= 100)',
                           name='ck_identity_verifications_face_threshold'),
        sa.CheckConstraint('provider_cost IS NULL OR provider_cost >= 0', name='ck_identity_verifications_provider_cost'),
    )
    op.create_index('uq_identity_verifications_one_active', 'identity_verifications', ['caregiver_id'], unique=True,
                    postgresql_where=sa.text(f"status IN ({ACTIVE})"))
    op.create_index('ix_identity_verifications_caregiver', 'identity_verifications', ['caregiver_id'])
    op.create_index('ix_identity_verifications_status', 'identity_verifications', ['status', 'created_at'])
    op.create_index('ix_identity_verifications_fingerprint', 'identity_verifications', ['id_fingerprint'])

    op.create_table(
        'kyc_webhook_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('token', sa.String(), nullable=False),
        sa.Column('provider_session_id', sa.String(), nullable=True),
        sa.Column('event_type', sa.String(), nullable=True),
        sa.Column('signature_ok', sa.Boolean(), nullable=False),
        sa.Column('body_sha256', sa.String(), nullable=True),
        sa.Column('received_at', sa.DateTime(), nullable=False),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.Column('outcome', sa.String(), nullable=True),
        sa.Column('verification_id', sa.UUID(), nullable=True),
        sa.ForeignKeyConstraint(['verification_id'], ['identity_verifications.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token'),
        sa.CheckConstraint(f"outcome IS NULL OR outcome IN ({OUTCOMES})", name='ck_kyc_webhook_events_outcome'),
    )
    op.create_index('ix_kyc_webhook_events_session', 'kyc_webhook_events', ['provider_session_id'])
    op.create_index('ix_kyc_webhook_events_verification', 'kyc_webhook_events', ['verification_id'])

    op.create_table(
        'identity_verification_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('verification_id', sa.UUID(), nullable=False),
        sa.Column('caregiver_id', sa.UUID(), nullable=False),
        sa.Column('at', sa.DateTime(), nullable=False),
        sa.Column('actor', sa.String(), nullable=False),
        sa.Column('from_status', sa.String(), nullable=True),
        sa.Column('to_status', sa.String(), nullable=False),
        sa.Column('reason_codes', postgresql.ARRAY(sa.String()), server_default=sa.text("'{}'::varchar[]"), nullable=False),
        sa.ForeignKeyConstraint(['verification_id'], ['identity_verifications.id']),
        sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("actor IN ('provider','reconcile','client') OR actor LIKE 'admin:%'",
                           name='ck_identity_verification_events_actor'),
        sa.CheckConstraint(f"from_status IS NULL OR from_status IN ({STATUSES})",
                           name='ck_identity_verification_events_from_status'),
        sa.CheckConstraint(f"to_status IN ({STATUSES})", name='ck_identity_verification_events_to_status'),
    )
    op.create_index('ix_identity_verification_events_verification', 'identity_verification_events', ['verification_id', 'at'])
    op.create_index('ix_identity_verification_events_caregiver', 'identity_verification_events', ['caregiver_id', 'at'])

    # Append-only at the database level (the ORM guards it too), like consents; TRUNCATE needs its own statement trigger.
    op.execute(
        """
        CREATE FUNCTION identity_verification_events_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'identity_verification_events is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER identity_verification_events_no_change
        BEFORE UPDATE OR DELETE ON identity_verification_events
        FOR EACH ROW EXECUTE FUNCTION identity_verification_events_immutable()
        """
    )
    op.execute(
        """
        CREATE TRIGGER identity_verification_events_no_truncate
        BEFORE TRUNCATE ON identity_verification_events
        FOR EACH STATEMENT EXECUTE FUNCTION identity_verification_events_immutable()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS identity_verification_events_no_truncate ON identity_verification_events")
    op.execute("DROP TRIGGER IF EXISTS identity_verification_events_no_change ON identity_verification_events")
    op.execute("DROP FUNCTION IF EXISTS identity_verification_events_immutable()")
    op.drop_table('identity_verification_events')
    op.drop_table('kyc_webhook_events')
    op.drop_table('identity_verifications')

    # consents is append-only: switch its change trigger off for this one delete, then on again.
    op.execute("ALTER TABLE consents DISABLE TRIGGER consents_no_change")
    op.execute("DELETE FROM consents WHERE type = 'identity_verification'")
    op.execute("ALTER TABLE consents ENABLE TRIGGER consents_no_change")
    op.drop_constraint('ck_consents_type', 'consents', type_='check')
    op.create_check_constraint('ck_consents_type', 'consents', f"type IN ({CONSENT_TYPES_OLD})")

    op.add_column('caregiver_documents', sa.Column('onfido_document_id', sa.String(), nullable=True))
    op.add_column('caregivers', sa.Column('face_verification_status', sa.String(), nullable=True))
    op.add_column('caregivers', sa.Column('face_verification_result', sa.JSON(), nullable=True))
    op.add_column('caregivers', sa.Column('document_verification_result', sa.JSON(), nullable=True))
    op.add_column('caregivers', sa.Column('onfido_check_id', sa.String(), nullable=True))
    op.add_column('caregivers', sa.Column('onfido_applicant_id', sa.String(), nullable=True))
    op.drop_constraint('ck_caregivers_identity_verification_status', 'caregivers', type_='check')
    op.execute("UPDATE caregivers SET identity_verification_status = 'in_progress' "
               "WHERE identity_verification_status = 'needs_review'")
