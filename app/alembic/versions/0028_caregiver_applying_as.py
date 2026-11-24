"""Caregiver "I apply as" request: ``caregivers.applying_as`` (nullable text[]).

Revision ID: 0028_caregiver_applying_as
Revises: 0027_booking_minutes
Create Date: 2026-11-24

The caregiver says which role(s) they apply as in setup step 2 (nurse, doctor, physiotherapist, caregiver,
nutritionist). It is only a REQUEST shown to the admin: skills (``caregiver_skills``) stay admin-confirmed and remain the
only thing that drives matching, care-plan authoring and approval. The column is NULL on rows created before the field
existed. A CHECK keeps the values inside the same five-value allow-list the skills use (the API also rejects
duplicates and more than five).

Downgrade drops the check and the column (the requested roles are lost; nothing else reads them).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0028_caregiver_applying_as'
down_revision: Union[str, None] = '0027_booking_minutes'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('caregivers', sa.Column('applying_as', sa.ARRAY(sa.String()), nullable=True))
    op.create_check_constraint(
        'ck_caregivers_applying_as', 'caregivers',
        "applying_as <@ ARRAY['nurse','doctor','physiotherapist','caregiver','nutritionist']::varchar[]",
    )


def downgrade() -> None:
    op.drop_constraint('ck_caregivers_applying_as', 'caregivers', type_='check')
    op.drop_column('caregivers', 'applying_as')
