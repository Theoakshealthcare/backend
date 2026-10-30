"""``users.token_version``: lets a password change end every other session.

Revision ID: 0020_user_token_version
Revises: 0019_care_plan_admin_permissions
Create Date: 2026-10-30

App-user access and refresh tokens are stateless JWTs, so there is nothing to delete when someone changes their
password. The tokens now carry ``tv`` (this number at issue time) and are refused when it no longer matches. Existing rows
get 0 and tokens issued before this revision have no ``tv`` (read as 0), so nobody is signed out by the deploy.

Downgrade drops the column; tokens that carry ``tv`` keep working, since the old code ignores the claim.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0020_user_token_version'
down_revision: Union[str, None] = '0019_care_plan_admin_permissions'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('token_version', sa.Integer(), server_default=sa.text('0'), nullable=False))


def downgrade() -> None:
    op.drop_column('users', 'token_version')
