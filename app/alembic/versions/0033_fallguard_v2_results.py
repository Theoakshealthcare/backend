"""FallGuard v2.0 results: versioned, immutable assessment results on ``fallguard_assessments``.

Revision ID: 0033_fallguard_v2_results
Revises: 0031_care_templates_docs_space
Create Date: 2026-11-25

Expand only (the FallGuard handover allows no destructive migration during the pilot):
- ``total_score`` becomes nullable (an under-60 result has no score and no tier).
- New nullable columns: the questionnaire, algorithm and content versions (SR-8), ``eligible``, the three tiers, ``urgent``,
  ``incomplete``, and two sealed JSON columns (``answers_encrypted``, ``result_encrypted``, Fernet like the other health text).
  Every existing (v1) row has them NULL.
- A trigger makes the result immutable (SR-8): an UPDATE that changes the patient, the score, a tier, a version, ``urgent``,
  ``incomplete`` or ``eligible`` raises. The sealed blobs and the AI columns are left out on purpose, because key rotation
  rewrites ciphertext and the optional AI summary is written after the score.

The old ``fallguard_questions`` and ``fallguard_answers`` tables stay (nothing reads them any more); a later contract migration
drops them. Downgrade removes the trigger and the new columns and sets a NULL score (an under-60 v2 row) to 0 so ``total_score`` can be
NOT NULL again; the v2 detail of those rows is lost with the columns.

Chain: on top of 0031 (care templates). The vitals-flags slice added no migration, so 0032 is unused; if another migration merges
before this one, re-point ``down_revision`` at it so ``alembic heads`` stays single.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0033_fallguard_v2_results'
down_revision: Union[str, None] = '0031_care_templates_docs_space'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = 'fallguard_assessments'
NEW_COLUMNS = [
    sa.Column('questionnaire_version', sa.String(20), nullable=True),
    sa.Column('algorithm_version', sa.String(20), nullable=True),
    sa.Column('content_version', sa.String(20), nullable=True),
    sa.Column('eligible', sa.Boolean(), nullable=True),
    sa.Column('gate_tier', sa.String(16), nullable=True),
    sa.Column('score_tier', sa.String(16), nullable=True),
    sa.Column('final_tier', sa.String(16), nullable=True),
    sa.Column('urgent', sa.Boolean(), nullable=True),
    sa.Column('incomplete', sa.Boolean(), nullable=True),
    sa.Column('answers_encrypted', sa.Text(), nullable=True),
    sa.Column('result_encrypted', sa.Text(), nullable=True),
]
TIERS = "('LOW', 'INTERMEDIATE', 'HIGH')"


def upgrade() -> None:
    op.alter_column(TABLE, 'total_score', existing_type=sa.Integer(), nullable=True)
    for column in NEW_COLUMNS:
        op.add_column(TABLE, column.copy())
    for name in ('gate_tier', 'score_tier', 'final_tier'):
        op.create_check_constraint(f'ck_fallguard_{name}', TABLE, f"{name} IS NULL OR {name} IN {TIERS}")
    op.execute("""
        CREATE FUNCTION fallguard_result_immutable() RETURNS trigger AS $$
        BEGIN
            IF NEW.patient_id IS DISTINCT FROM OLD.patient_id
               OR NEW.total_score IS DISTINCT FROM OLD.total_score
               OR NEW.questionnaire_version IS DISTINCT FROM OLD.questionnaire_version
               OR NEW.algorithm_version IS DISTINCT FROM OLD.algorithm_version
               OR NEW.content_version IS DISTINCT FROM OLD.content_version
               OR NEW.eligible IS DISTINCT FROM OLD.eligible
               OR NEW.gate_tier IS DISTINCT FROM OLD.gate_tier
               OR NEW.score_tier IS DISTINCT FROM OLD.score_tier
               OR NEW.final_tier IS DISTINCT FROM OLD.final_tier
               OR NEW.urgent IS DISTINCT FROM OLD.urgent
               OR NEW.incomplete IS DISTINCT FROM OLD.incomplete THEN
                RAISE EXCEPTION 'a FallGuard result is immutable: start a new assessment instead' USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute(f"CREATE TRIGGER trg_fallguard_result_immutable BEFORE UPDATE ON {TABLE} "
               "FOR EACH ROW EXECUTE FUNCTION fallguard_result_immutable()")


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_fallguard_result_immutable ON {TABLE}")
    op.execute("DROP FUNCTION IF EXISTS fallguard_result_immutable()")
    for name in ('gate_tier', 'score_tier', 'final_tier'):
        op.drop_constraint(f'ck_fallguard_{name}', TABLE, type_='check')
    for column in NEW_COLUMNS:
        op.drop_column(TABLE, column.name)
    op.execute(f"UPDATE {TABLE} SET total_score = 0 WHERE total_score IS NULL")  # only v2 rows can be NULL, and they are gone now
    op.alter_column(TABLE, 'total_score', existing_type=sa.Integer(), nullable=False)
