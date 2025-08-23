"""add_patient_caregiver_access_table

Revision ID: 20240708b
Revises: 20240708
Create Date: 2024-07-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlalchemy.dialects.postgresql as pg

# revision identifiers, used by Alembic.
revision = '20240708b'
down_revision = '20240708'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        'patient_caregiver_access',
        sa.Column('id', pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('patient_id', pg.UUID(as_uuid=True), sa.ForeignKey('patients.id'), nullable=False),
        sa.Column('caregiver_id', pg.UUID(as_uuid=True), sa.ForeignKey('caregivers.id'), nullable=False),
        sa.Column('granted_by_patient', sa.Boolean(), nullable=False, server_default=sa.sql.expression.true()),
        sa.Column('granted_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
    )

def downgrade():
    op.drop_table('patient_caregiver_access') 