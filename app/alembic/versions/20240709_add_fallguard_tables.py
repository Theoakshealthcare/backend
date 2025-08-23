from alembic import op
import sqlalchemy as sa
import uuid

revision = '20240709_add_fallguard_tables'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        'fallguard_questions',
        sa.Column('id', sa.UUID(), primary_key=True, default=uuid.uuid4),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('options', sa.Text(), nullable=False),
        sa.Column('order', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), default=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_table(
        'fallguard_assessments',
        sa.Column('id', sa.UUID(), primary_key=True, default=uuid.uuid4),
        sa.Column('patient_id', sa.UUID(), sa.ForeignKey('patients.id'), nullable=False),
        sa.Column('total_score', sa.Integer(), nullable=False),
        sa.Column('ai_summary_encrypted', sa.Text(), nullable=True),
        sa.Column('ai_recommendations_encrypted', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_table(
        'fallguard_answers',
        sa.Column('id', sa.UUID(), primary_key=True, default=uuid.uuid4),
        sa.Column('assessment_id', sa.UUID(), sa.ForeignKey('fallguard_assessments.id'), nullable=False),
        sa.Column('question_id', sa.UUID(), sa.ForeignKey('fallguard_questions.id'), nullable=False),
        sa.Column('answer_encrypted', sa.Text(), nullable=False),
        sa.Column('score', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )

def downgrade():
    op.drop_table('fallguard_answers')
    op.drop_table('fallguard_assessments')
    op.drop_table('fallguard_questions') 