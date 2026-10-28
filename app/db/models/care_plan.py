"""Care plans: a nurse's plan for one client, shared with every caregiver assigned to that client.

Whose plan it is (the subject) is one of two keys, never both: ``patient_id`` for a visit booked for oneself,
``care_recipient_id`` for a visit a booker made for somebody else (docs/plans/2026-10-27-caregiver-care-plans-design.md
section 3.2). Every free-text column that describes a person is ``*_encrypted`` (Fernet, ``app.core.security``);
enums, dates, times and ids stay plaintext so they can be filtered and indexed. The ``*_encrypted`` columns are
nullable only so that account erasure can wipe them; the service refuses to write a plan without them.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text, Time,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSON, JSONB, UUID

from app.db.base import Base

RISK_LEVELS = ("low", "medium", "high")
PLAN_STATUSES = ("active", "completed", "archived")
GOAL_FREQUENCIES = ("daily", "weekly", "ongoing", "one_time")
GOAL_STATUSES = ("active", "achieved", "discontinued")
TIME_SLOTS = ("morning", "afternoon", "evening", "night")
REVIEW_STATUSES = ("pending", "approved")
ACTOR_TYPES = ("caregiver", "admin", "system")


def _in(column: str, values) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class CarePlanTemplate(Base):
    """A starting point a nurse copies into a plan. Not about a person, so nothing here is encrypted. No medicines,
    no doses and no numeric clinical thresholds (those are the nurse's and the prescriber's)."""
    __tablename__ = "care_plan_templates"
    __table_args__ = (
        CheckConstraint(_in("default_risk_level", RISK_LEVELS), name="ck_care_plan_templates_risk"),
        CheckConstraint(_in("clinical_review_status", REVIEW_STATUSES), name="ck_care_plan_templates_review"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key = Column(String, nullable=False, unique=True)
    name = Column(String, nullable=False)
    summary = Column(Text, nullable=False)
    duration_days = Column(Integer, nullable=False)
    review_every_days = Column(Integer, nullable=True)
    default_risk_level = Column(String, nullable=False)
    # {"goals": [{"title", "target", "frequency"}], "tasks": [{"title", "time_slot", "time_of_day", "days_of_week"}],
    #  "special_instructions": [text]}
    body = Column(JSONB, nullable=False)
    version = Column(Integer, nullable=False, default=1, server_default=text("1"))
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    clinical_review_status = Column(String, nullable=False, default="pending", server_default=text("'pending'"))
    reviewed_by_admin_id = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class CarePlan(Base):
    __tablename__ = "care_plans"
    __table_args__ = (
        CheckConstraint("num_nonnulls(patient_id, care_recipient_id) = 1", name="ck_care_plans_one_subject"),
        CheckConstraint("num_nonnulls(created_by_caregiver_id, created_by_admin_id) = 1",
                        name="ck_care_plans_one_author"),
        CheckConstraint(_in("status", PLAN_STATUSES), name="ck_care_plans_status"),
        CheckConstraint(_in("risk_level", RISK_LEVELS), name="ck_care_plans_risk"),
        CheckConstraint("duration_days BETWEEN 1 AND 365", name="ck_care_plans_duration"),
        CheckConstraint("review_every_days IS NULL OR review_every_days BETWEEN 7 AND 90", name="ck_care_plans_review"),
        # One active plan per client; a race between two creates is an IntegrityError the service maps to 409.
        Index("uq_care_plans_active_patient", "patient_id", unique=True,
              postgresql_where=text("status = 'active' AND patient_id IS NOT NULL")),
        Index("uq_care_plans_active_recipient", "care_recipient_id", unique=True,
              postgresql_where=text("status = 'active' AND care_recipient_id IS NOT NULL")),
        Index("ix_care_plans_created_by_caregiver", "created_by_caregiver_id"),
        Index("ix_care_plans_status_updated", "status", text("updated_at DESC")),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("patients.id"), nullable=True)
    care_recipient_id = Column(UUID(as_uuid=True), ForeignKey("care_recipients.id"), nullable=True)
    # Provenance only: the plan is a copy, later template edits never change it.
    template_id = Column(UUID(as_uuid=True), ForeignKey("care_plan_templates.id"), nullable=True)
    template_version = Column(Integer, nullable=True)
    status = Column(String, nullable=False, default="active", server_default=text("'active'"))
    risk_level = Column(String, nullable=False)  # recorded by the author, never computed
    diagnosis_encrypted = Column(Text, nullable=True)  # NOT NULL is enforced by the service; erasure nulls it
    allergies_encrypted = Column(Text, nullable=True)  # encrypted JSON array of strings
    special_instructions_encrypted = Column(Text, nullable=True)  # encrypted JSON array of strings
    start_date = Column(Date, nullable=False)
    duration_days = Column(Integer, nullable=False)
    review_every_days = Column(Integer, nullable=True)
    created_by_caregiver_id = Column(UUID(as_uuid=True), ForeignKey("caregivers.id"), nullable=True)
    created_by_admin_id = Column(UUID(as_uuid=True), ForeignKey("admin_users.id"), nullable=True)
    created_from_booking_id = Column(UUID(as_uuid=True), ForeignKey("bookings.id"), nullable=True)
    # Optimistic concurrency: bumped on every plan or child write.
    version = Column(Integer, nullable=False, default=1, server_default=text("1"))
    completed_at = Column(DateTime, nullable=True)
    archived_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class CarePlanGoal(Base):
    __tablename__ = "care_plan_goals"
    __table_args__ = (
        CheckConstraint(_in("frequency", GOAL_FREQUENCIES), name="ck_care_plan_goals_frequency"),
        CheckConstraint(_in("status", GOAL_STATUSES), name="ck_care_plan_goals_status"),
        Index("ix_care_plan_goals_plan_sort", "plan_id", "sort_order"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id = Column(UUID(as_uuid=True), ForeignKey("care_plans.id"), nullable=False)
    title_encrypted = Column(Text, nullable=True)  # required on write; erasure nulls it
    target_encrypted = Column(Text, nullable=True)
    frequency = Column(String, nullable=False)
    status = Column(String, nullable=False, default="active", server_default=text("'active'"))
    sort_order = Column(Integer, nullable=False, default=0, server_default=text("0"))
    # Family-view groundwork: nothing in v1 sets or reads this, and it defaults to private.
    family_visible = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class CarePlanTask(Base):
    """A task definition ("Check blood pressure", morning, Mon-Fri). The ticks that complete it per day are B2."""
    __tablename__ = "care_plan_tasks"
    __table_args__ = (
        CheckConstraint(_in("time_slot", TIME_SLOTS), name="ck_care_plan_tasks_slot"),
        CheckConstraint("cardinality(days_of_week) BETWEEN 1 AND 7", name="ck_care_plan_tasks_days"),
        Index("ix_care_plan_tasks_plan_active", "plan_id", "is_active"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id = Column(UUID(as_uuid=True), ForeignKey("care_plans.id"), nullable=False)
    title_encrypted = Column(Text, nullable=True)  # required on write; erasure nulls it
    time_slot = Column(String, nullable=False)
    time_of_day = Column(Time, nullable=True)  # display only
    days_of_week = Column(ARRAY(SmallInteger), nullable=False)  # 1 = Monday .. 7 = Sunday
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    sort_order = Column(Integer, nullable=False, default=0, server_default=text("0"))
    family_visible = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class CarePlanEvent(Base):
    """Who changed a plan and when. ``detail`` holds field names, ids and counts, never health text."""
    __tablename__ = "care_plan_events"
    __table_args__ = (
        CheckConstraint(_in("actor_type", ACTOR_TYPES), name="ck_care_plan_events_actor"),
        Index("ix_care_plan_events_plan_created", "plan_id", "created_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id = Column(UUID(as_uuid=True), ForeignKey("care_plans.id"), nullable=False)
    actor_type = Column(String, nullable=False)
    actor_id = Column(UUID(as_uuid=True), nullable=True)  # null for the system (erasure)
    action = Column(String, nullable=False)
    detail = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
