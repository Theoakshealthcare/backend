"""Admin identity, roles, MFA sessions and rate limits (work package A0).

Revision ID: 0003_admin_identity
Revises: 0002_caregiver_mobile
Create Date: 2026-10-06

- Drops the old ``admins`` table (a row linking a patient-style user to "is admin"). There is no
  data, so nothing is converted.
- Creates ``admin_users`` (own accounts, not ``users`` rows), ``admin_roles``,
  ``admin_role_permissions``, ``admin_user_roles``, ``admin_refresh_tokens`` and
  ``admin_rate_limits``.
- Seeds the seven roles and their permissions (PRD section 8.2). The data below is a frozen copy:
  a later permission change ships as a new migration, never an edit here.
- Re-points every "decided by an admin" column at ``admin_users``: ``admin_audit_log.admin_user_id``,
  ``caregivers.approved_by``, ``caregivers.background_check_decided_by``,
  ``caregiver_documents.reviewed_by`` and ``caregiver_skills.created_by_admin_id``.

Downgrade: the re-pointed columns are cleared (their ids belong to admin_users, not users), the
audit log's foreign key is re-added NOT VALID so existing audit rows are kept, and the old empty
``admins`` table is recreated.
"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0003_admin_identity'
down_revision: Union[str, None] = '0002_caregiver_mobile'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column, default Postgres FK name)
REPOINTED = [
    ('admin_audit_log', 'admin_user_id', 'admin_audit_log_admin_user_id_fkey'),
    ('caregivers', 'approved_by', 'caregivers_approved_by_fkey'),
    ('caregivers', 'background_check_decided_by', 'caregivers_background_check_decided_by_fkey'),
    ('caregiver_documents', 'reviewed_by', 'caregiver_documents_reviewed_by_fkey'),
    ('caregiver_skills', 'created_by_admin_id', 'caregiver_skills_created_by_admin_id_fkey'),
]

# Frozen copy of app/core/admin_permissions.py as of this revision.
ROLE_NAMES = {
    'super_admin': 'Super Admin',
    'operations': 'Operations',
    'verification': 'Verification',
    'finance': 'Finance',
    'support': 'Support',
    'clinical_reviewer': 'Clinical Reviewer',
    'read_only_analyst': 'Read-only Analyst',
}
ALL_PERMISSIONS = [
    'dashboard.read', 'caregivers.read', 'caregivers.approve', 'caregivers.suspend', 'documents.read',
    'bookings.read', 'bookings.dispatch', 'bookings.assign', 'bookings.cancel', 'bookings.cancel_request',
    'bookings.address', 'bookings.correct_status', 'payments.read', 'payments.reconcile',
    'refunds.request', 'refunds.approve', 'payouts.execute', 'ledger.adjust', 'ledger.adjust_request',
    'people.read', 'people.pii', 'people.suspend', 'clinical.read', 'pricing.write', 'pricing.request',
    'settings.write', 'support.inbox', 'fallguard.write', 'prescriptions.manage', 'whatsapp.read',
    'admins.manage', 'audit.read', 'exports.pii',
]
ROLE_PERMISSIONS = {
    'super_admin': ALL_PERMISSIONS,
    'operations': [
        'dashboard.read', 'caregivers.read', 'caregivers.suspend', 'bookings.read', 'bookings.dispatch',
        'bookings.assign', 'bookings.cancel', 'bookings.address', 'refunds.request', 'people.read',
        'people.pii', 'people.suspend', 'settings.write', 'support.inbox', 'whatsapp.read',
    ],
    'verification': [
        'dashboard.read', 'caregivers.read', 'caregivers.approve', 'caregivers.suspend', 'documents.read',
        'people.read',
    ],
    'finance': [
        'dashboard.read', 'caregivers.read', 'bookings.read', 'payments.read', 'payments.reconcile',
        'refunds.request', 'refunds.approve', 'payouts.execute', 'ledger.adjust_request', 'people.read',
        'pricing.request', 'exports.pii',
    ],
    'support': [
        'dashboard.read', 'caregivers.read', 'bookings.read', 'bookings.cancel_request', 'refunds.request',
        'people.read', 'support.inbox', 'whatsapp.read',
    ],
    'clinical_reviewer': [
        'dashboard.read', 'caregivers.read', 'documents.read', 'bookings.read', 'people.read',
        'clinical.read', 'support.inbox', 'fallguard.write', 'prescriptions.manage',
    ],
    'read_only_analyst': ['dashboard.read'],
}


def upgrade() -> None:
    op.drop_table('admins')

    op.create_table('admin_roles',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key')
    )
    op.create_table('admin_role_permissions',
    sa.Column('role_id', sa.UUID(), nullable=False),
    sa.Column('permission', sa.String(), nullable=False),
    sa.ForeignKeyConstraint(['role_id'], ['admin_roles.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('role_id', 'permission')
    )
    op.create_table('admin_users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(), nullable=False),
    sa.Column('full_name', sa.String(), nullable=False),
    sa.Column('password_hash', sa.String(), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('mfa_secret_enc', sa.String(), nullable=True),
    sa.Column('mfa_enabled', sa.Boolean(), nullable=False),
    sa.Column('mfa_last_step', sa.BigInteger(), nullable=True),
    sa.Column('failed_logins', sa.Integer(), nullable=False),
    sa.Column('mfa_lock_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('locked_until', sa.DateTime(), nullable=True),
    sa.Column('last_login_at', sa.DateTime(), nullable=True),
    sa.Column('invite_token_hash', sa.String(), nullable=True),
    sa.Column('invite_expires_at', sa.DateTime(), nullable=True),
    sa.Column('created_by', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("status IN ('invited','active','disabled')", name='ck_admin_users_status'),
    sa.ForeignKeyConstraint(['created_by'], ['admin_users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email'),
    sa.UniqueConstraint('invite_token_hash')
    )
    op.create_table('admin_user_roles',
    sa.Column('admin_user_id', sa.UUID(), nullable=False),
    sa.Column('role_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['admin_user_id'], ['admin_users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['role_id'], ['admin_roles.id'], ),
    sa.PrimaryKeyConstraint('admin_user_id', 'role_id')
    )
    op.create_table('admin_refresh_tokens',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('admin_user_id', sa.UUID(), nullable=False),
    sa.Column('family_id', sa.UUID(), nullable=False),
    sa.Column('token_hash', sa.String(), nullable=False),
    sa.Column('session_started_at', sa.DateTime(), nullable=False),
    sa.Column('issued_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('replaced_by_id', sa.UUID(), nullable=True),
    sa.ForeignKeyConstraint(['admin_user_id'], ['admin_users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['replaced_by_id'], ['admin_refresh_tokens.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_admin_refresh_tokens_admin_user_id'), 'admin_refresh_tokens', ['admin_user_id'], unique=False)
    op.create_index('ix_admin_refresh_tokens_family', 'admin_refresh_tokens', ['family_id'], unique=False)
    op.create_table('admin_rate_limits',
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('window_start', sa.DateTime(), nullable=False),
    sa.Column('count', sa.Integer(), nullable=False),
    sa.Column('blocked_until', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('key', 'window_start')
    )
    op.create_index('ix_admin_rate_limits_updated_at', 'admin_rate_limits', ['updated_at'], unique=False)

    # Seed roles and permissions.
    roles = sa.table('admin_roles', sa.column('id', sa.UUID()), sa.column('key', sa.String()), sa.column('name', sa.String()))
    perms = sa.table('admin_role_permissions', sa.column('role_id', sa.UUID()), sa.column('permission', sa.String()))
    role_ids = {key: uuid.uuid4() for key in ROLE_NAMES}
    op.bulk_insert(roles, [{'id': role_ids[k], 'key': k, 'name': n} for k, n in ROLE_NAMES.items()])
    op.bulk_insert(perms, [
        {'role_id': role_ids[k], 'permission': p} for k, plist in ROLE_PERMISSIONS.items() for p in plist
    ])

    # "Decided by an admin" columns now point at admin_users.
    for table, column, fk_name in REPOINTED:
        op.drop_constraint(fk_name, table, type_='foreignkey')
        op.create_foreign_key(fk_name, table, 'admin_users', [column], ['id'])


def downgrade() -> None:
    for table, column, fk_name in REPOINTED:
        op.drop_constraint(fk_name, table, type_='foreignkey')
        if table != 'admin_audit_log':  # the audit log is append-only; its rows are kept as they are
            op.execute(sa.table(table, sa.column(column)).update().values({column: None}))
        op.create_foreign_key(
            fk_name, table, 'users', [column], ['id'], postgresql_not_valid=(table == 'admin_audit_log')
        )

    op.drop_index('ix_admin_rate_limits_updated_at', table_name='admin_rate_limits')
    op.drop_table('admin_rate_limits')
    op.drop_index('ix_admin_refresh_tokens_family', table_name='admin_refresh_tokens')
    op.drop_index(op.f('ix_admin_refresh_tokens_admin_user_id'), table_name='admin_refresh_tokens')
    op.drop_table('admin_refresh_tokens')
    op.drop_table('admin_user_roles')
    op.drop_table('admin_users')
    op.drop_table('admin_role_permissions')
    op.drop_table('admin_roles')

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
