"""WhatsApp foundations: phone-first users and the channel tables (W1).

Revision ID: 0003_whatsapp_foundations
Revises: 0002_caregiver_mobile
Create Date: 2026-10-06

- ``users.email`` and ``patients.email`` become nullable (a WhatsApp patient may have no email).
  The mobile app's registration schema still requires one.
- New tables: whatsapp_contacts, whatsapp_conversations, whatsapp_messages (metadata only),
  care_recipients, care_relationships, consents (append-only, enforced by a trigger),
  support_conversations (one live handover per contact, enforced by a partial unique index).

Downgrade: rows with a NULL email cannot go back to NOT NULL, so they get a ``.invalid`` placeholder
address (RFC 2606, never deliverable) before the constraint returns.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0003_whatsapp_foundations'
down_revision: Union[str, None] = '0002_caregiver_mobile'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('users', 'email', existing_type=sa.String(), nullable=True)
    op.alter_column('patients', 'email', existing_type=sa.String(), nullable=True)

    op.create_table('whatsapp_contacts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('wa_id', sa.String(), nullable=False),
    sa.Column('phone_e164', sa.String(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=True),
    sa.Column('display_name', sa.String(), nullable=True),
    sa.Column('language', sa.String(), server_default='en', nullable=False),
    sa.Column('source', sa.String(), nullable=True),
    sa.Column('opted_in_at', sa.DateTime(), nullable=True),
    sa.Column('marketing_opt_in', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('opted_out_at', sa.DateTime(), nullable=True),
    sa.Column('last_inbound_at', sa.DateTime(), nullable=True),
    sa.Column('blocked', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("language IN ('en', 'pcm')", name='ck_whatsapp_contacts_language'),
    sa.CheckConstraint("phone_e164 ~ '^\\+[1-9][0-9]{7,14}$'", name='ck_whatsapp_contacts_phone_e164'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('phone_e164'),
    sa.UniqueConstraint('wa_id')
    )
    op.create_index(op.f('ix_whatsapp_contacts_user_id'), 'whatsapp_contacts', ['user_id'], unique=False)

    op.create_table('whatsapp_conversations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=False),
    sa.Column('state', sa.String(), server_default='new', nullable=False),
    sa.Column('context', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('assigned_agent_id', sa.UUID(), nullable=True),
    sa.Column('automation_paused', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("state IN ('new', 'consent_pending', 'declined', 'idle', 'human', 'emergency')", name='ck_whatsapp_conversations_state'),
    sa.ForeignKeyConstraint(['assigned_agent_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['contact_id'], ['whatsapp_contacts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('contact_id')
    )

    op.create_table('whatsapp_messages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=False),
    sa.Column('direction', sa.String(), nullable=False),
    sa.Column('meta_message_id', sa.String(), nullable=True),
    sa.Column('type', sa.String(), nullable=False),
    sa.Column('template_name', sa.String(), nullable=True),
    sa.Column('category', sa.String(), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('error_code', sa.String(), nullable=True),
    sa.Column('cost_category', sa.String(), nullable=True),
    sa.Column('processed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("direction IN ('inbound', 'outbound')", name='ck_whatsapp_messages_direction'),
    sa.CheckConstraint("status IN ('received', 'sent', 'delivered', 'read', 'failed')", name='ck_whatsapp_messages_status'),
    sa.ForeignKeyConstraint(['contact_id'], ['whatsapp_contacts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('meta_message_id')
    )
    op.create_index('ix_whatsapp_messages_contact_created', 'whatsapp_messages', ['contact_id', 'created_at'], unique=False)

    op.create_table('care_recipients',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('full_name', sa.String(), nullable=False),
    sa.Column('date_of_birth', sa.Date(), nullable=True),
    sa.Column('gender', sa.String(), nullable=True),
    sa.Column('phone_e164', sa.String(), nullable=True),
    sa.Column('address', sa.Text(), nullable=True),
    sa.Column('city', sa.String(), nullable=True),
    sa.Column('state', sa.String(), nullable=True),
    sa.Column('lat', sa.Float(), nullable=True),
    sa.Column('lng', sa.Float(), nullable=True),
    sa.Column('notes_encrypted', sa.Text(), nullable=True),
    sa.Column('created_by_user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_care_recipients_created_by_user_id'), 'care_recipients', ['created_by_user_id'], unique=False)

    op.create_table('care_relationships',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('contact_user_id', sa.UUID(), nullable=False),
    sa.Column('recipient_id', sa.UUID(), nullable=False),
    sa.Column('role', sa.String(), nullable=False),
    sa.Column('clinical_access', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("role IN ('self', 'family', 'guardian')", name='ck_care_relationships_role'),
    sa.ForeignKeyConstraint(['contact_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['recipient_id'], ['care_recipients.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('contact_user_id', 'recipient_id', name='uq_care_relationships_user_recipient')
    )
    op.create_index(op.f('ix_care_relationships_contact_user_id'), 'care_relationships', ['contact_user_id'], unique=False)
    op.create_index(op.f('ix_care_relationships_recipient_id'), 'care_relationships', ['recipient_id'], unique=False)

    op.create_table('consents',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=True),
    sa.Column('subject_user_id', sa.UUID(), nullable=True),
    sa.Column('subject_recipient_id', sa.UUID(), nullable=True),
    sa.Column('granted_by_user_id', sa.UUID(), nullable=True),
    sa.Column('type', sa.String(), nullable=False),
    sa.Column('decision', sa.String(), nullable=False),
    sa.Column('policy_version', sa.String(), nullable=False),
    sa.Column('channel', sa.String(), nullable=False),
    sa.Column('evidence_ref', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("contact_id IS NOT NULL OR subject_user_id IS NOT NULL OR subject_recipient_id IS NOT NULL", name='ck_consents_has_subject'),
    sa.CheckConstraint("decision IN ('granted', 'withdrawn')", name='ck_consents_decision'),
    sa.CheckConstraint("type IN ('terms', 'health_processing', 'marketing', 'share_with_family')", name='ck_consents_type'),
    sa.ForeignKeyConstraint(['contact_id'], ['whatsapp_contacts.id'], ),
    sa.ForeignKeyConstraint(['granted_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['subject_recipient_id'], ['care_recipients.id'], ),
    sa.ForeignKeyConstraint(['subject_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_consents_contact_type_created', 'consents', ['contact_id', 'type', 'created_at'], unique=False)
    op.create_index(op.f('ix_consents_subject_recipient_id'), 'consents', ['subject_recipient_id'], unique=False)
    op.create_index(op.f('ix_consents_subject_user_id'), 'consents', ['subject_user_id'], unique=False)

    op.create_table('support_conversations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('contact_id', sa.UUID(), nullable=False),
    sa.Column('reason', sa.String(), nullable=False),
    sa.Column('priority', sa.String(), server_default='normal', nullable=False),
    sa.Column('status', sa.String(), server_default='open', nullable=False),
    sa.Column('assigned_agent_id', sa.UUID(), nullable=True),
    sa.Column('resolved_by_user_id', sa.UUID(), nullable=True),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("priority IN ('normal', 'urgent')", name='ck_support_conversations_priority'),
    sa.CheckConstraint("reason IN ('talk_to_person', 'two_misses', 'emergency', 'data_withdrawal', 'booking_placeholder')", name='ck_support_conversations_reason'),
    sa.CheckConstraint("status IN ('open', 'assigned', 'resolved')", name='ck_support_conversations_status'),
    sa.ForeignKeyConstraint(['assigned_agent_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['contact_id'], ['whatsapp_contacts.id'], ),
    sa.ForeignKeyConstraint(['resolved_by_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_support_conversations_contact_id'), 'support_conversations', ['contact_id'], unique=False)
    op.create_index('ix_support_conversations_queue', 'support_conversations', ['status', 'priority', 'created_at'], unique=False)
    op.create_index('uq_support_conversations_one_open_handover', 'support_conversations', ['contact_id'], unique=True,
                    postgresql_where=sa.text("status <> 'resolved' AND reason <> 'data_withdrawal'"))

    # consents is append-only at the database level (the ORM guards it too).
    op.execute(
        """
        CREATE FUNCTION consents_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'consents is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER consents_no_change
        BEFORE UPDATE OR DELETE ON consents
        FOR EACH ROW EXECUTE FUNCTION consents_immutable()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS consents_no_change ON consents")
    op.execute("DROP FUNCTION IF EXISTS consents_immutable()")
    op.drop_index('uq_support_conversations_one_open_handover', table_name='support_conversations',
                  postgresql_where=sa.text("status <> 'resolved' AND reason <> 'data_withdrawal'"))
    op.drop_index('ix_support_conversations_queue', table_name='support_conversations')
    op.drop_index(op.f('ix_support_conversations_contact_id'), table_name='support_conversations')
    op.drop_table('support_conversations')
    op.drop_index(op.f('ix_consents_subject_user_id'), table_name='consents')
    op.drop_index(op.f('ix_consents_subject_recipient_id'), table_name='consents')
    op.drop_index('ix_consents_contact_type_created', table_name='consents')
    op.drop_table('consents')
    op.drop_index(op.f('ix_care_relationships_recipient_id'), table_name='care_relationships')
    op.drop_index(op.f('ix_care_relationships_contact_user_id'), table_name='care_relationships')
    op.drop_table('care_relationships')
    op.drop_index(op.f('ix_care_recipients_created_by_user_id'), table_name='care_recipients')
    op.drop_table('care_recipients')
    op.drop_index('ix_whatsapp_messages_contact_created', table_name='whatsapp_messages')
    op.drop_table('whatsapp_messages')
    op.drop_table('whatsapp_conversations')
    op.drop_index(op.f('ix_whatsapp_contacts_user_id'), table_name='whatsapp_contacts')
    op.drop_table('whatsapp_contacts')

    op.execute("UPDATE patients SET email = 'wa-' || id::text || '@placeholder.invalid' WHERE email IS NULL")
    op.execute("UPDATE users SET email = 'wa-' || id::text || '@placeholder.invalid' WHERE email IS NULL")
    op.alter_column('patients', 'email', existing_type=sa.String(), nullable=False)
    op.alter_column('users', 'email', existing_type=sa.String(), nullable=False)
