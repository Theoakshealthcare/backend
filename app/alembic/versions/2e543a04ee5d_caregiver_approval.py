"""caregiver approval: skills, admin audit log, decision columns

Revision ID: 2e543a04ee5d
Revises: 6fb30639d5f4
Create Date: 2026-10-05

Adds caregiver_skills and admin_audit_log (append-only, enforced by a trigger), the approval
columns on caregivers and the review columns on caregiver_documents. No data is converted:
the new status values (suspended, changes_requested, cleared, failed) are plain strings, and
old code treats an unknown profile_status as "not approved", so downgrade is lossless apart
from the dropped columns and tables.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "2e543a04ee5d"
down_revision: Union[str, None] = "6fb30639d5f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.add_column("caregivers", sa.Column("approved_by", UUID, sa.ForeignKey("users.id"), nullable=True))
    op.add_column("caregivers", sa.Column("approved_at", sa.DateTime(), nullable=True))
    op.add_column("caregivers", sa.Column("suspended_at", sa.DateTime(), nullable=True))
    op.add_column("caregivers", sa.Column("suspension_reason", sa.Text(), nullable=True))
    op.add_column("caregivers", sa.Column("rejection_reason", sa.Text(), nullable=True))
    op.add_column("caregivers", sa.Column("changes_requested", sa.JSON(), nullable=True))
    op.add_column("caregivers", sa.Column("background_check_evidence", sa.JSON(), nullable=True))
    op.add_column("caregivers", sa.Column("background_check_decided_by", UUID, sa.ForeignKey("users.id"), nullable=True))
    op.add_column("caregivers", sa.Column("background_check_decided_at", sa.DateTime(), nullable=True))

    op.add_column("caregiver_documents", sa.Column("reviewed_by", UUID, sa.ForeignKey("users.id"), nullable=True))
    op.add_column("caregiver_documents", sa.Column("rejection_reason", sa.Text(), nullable=True))
    op.add_column("caregiver_documents", sa.Column("expires_on", sa.Date(), nullable=True))

    op.create_table(
        "caregiver_skills",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("caregiver_id", UUID, sa.ForeignKey("caregivers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("service_type", sa.String(), nullable=False),
        sa.Column("sub_specialty", sa.String(), nullable=True),
        sa.Column("created_by_admin_id", UUID, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "service_type IN ('nurse','doctor','physiotherapist','caregiver','nutritionist')",
            name="ck_caregiver_skills_service_type",
        ),
    )
    op.create_index(
        "uq_caregiver_skills_caregiver_service_sub",
        "caregiver_skills",
        ["caregiver_id", "service_type", sa.text("coalesce(sub_specialty, '')")],
        unique=True,
    )

    op.create_table(
        "admin_audit_log",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("admin_user_id", UUID, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=True),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("ip", sa.String(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_admin_audit_log_entity", "admin_audit_log", ["entity_type", "entity_id"])
    op.create_index("ix_admin_audit_log_admin_created", "admin_audit_log", ["admin_user_id", "created_at"])

    # Append-only for every client, not just this app's ORM.
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
    op.execute("DROP TRIGGER IF EXISTS admin_audit_log_no_change ON admin_audit_log")
    op.execute("DROP FUNCTION IF EXISTS admin_audit_log_immutable()")
    op.drop_index("ix_admin_audit_log_admin_created", table_name="admin_audit_log")
    op.drop_index("ix_admin_audit_log_entity", table_name="admin_audit_log")
    op.drop_table("admin_audit_log")

    op.drop_index("uq_caregiver_skills_caregiver_service_sub", table_name="caregiver_skills")
    op.drop_table("caregiver_skills")

    for col in ("expires_on", "rejection_reason", "reviewed_by"):
        op.drop_column("caregiver_documents", col)
    for col in (
        "background_check_decided_at", "background_check_decided_by", "background_check_evidence",
        "changes_requested", "rejection_reason", "suspension_reason", "suspended_at", "approved_at",
        "approved_by",
    ):
        op.drop_column("caregivers", col)
