"""add_patient_vitals_table

Revision ID: 20240708
Revises: 4da1b7440491
Create Date: 2024-07-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlalchemy.dialects.postgresql as pg

# revision identifiers, used by Alembic.
revision = '20240708'
down_revision = '4da1b7440491'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        'patient_vitals',
        sa.Column('id', pg.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('patient_id', pg.UUID(as_uuid=True), sa.ForeignKey('patients.id'), nullable=False),
        sa.Column('recorded_by_user_id', pg.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('source', sa.Enum('manual', 'wearable', name='vitalsource'), nullable=False, server_default='manual'),
        sa.Column('vital_type', sa.String(), nullable=False),
        sa.Column('value', sa.String(), nullable=False),
        sa.Column('unit', sa.String(), nullable=True),
        sa.Column('recorded_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('notes', sa.String(), nullable=True),
    )

def downgrade():
    op.drop_table('patient_vitals')
    op.execute('DROP TYPE IF EXISTS vitalsource;') 