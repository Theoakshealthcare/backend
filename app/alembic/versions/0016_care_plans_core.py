"""Care plans, foundation: templates (seeded), plans, goals, task definitions, events.

Revision ID: 0016_care_plans_core
Revises: 0015_caregiver_kyc
Create Date: 2026-10-27

The design doc (docs/plans/2026-10-27-caregiver-care-plans-design.md) calls this revision ``0015``; the caregiver KYC
slice took ``0015_caregiver_kyc`` first, so care plans start at ``0016`` and the later slices move up one (B2 ``0017``, B3
``0018``, B4 ``0019``).

- ``care_plan_templates``: the plan templates a nurse copies from, with five drafts seeded as ``clinical_review_status =
  'pending'`` (go-live gate: a clinician reviews or removes every one). They carry routine tasks and goal wording only:
  no medicines, no doses, no numeric clinical thresholds. Idempotent (``ON CONFLICT (key) DO NOTHING``).
- ``care_plans``: one plan per client, keyed by ``patient_id`` or ``care_recipient_id`` (exactly one, CHECKed). A partial
  unique index allows one ``active`` plan per client. Diagnosis, allergies and special instructions are Fernet ciphertext
  (``*_encrypted``); the columns are nullable only so that account erasure can wipe them.
- ``care_plan_goals`` / ``care_plan_tasks``: goals and task definitions (title and target encrypted), each with
  ``family_visible`` defaulting to false (family-view groundwork; nothing reads or sets it in v1).
- ``care_plan_events``: who changed a plan and when (field names and ids, never health text).

Downgrade drops the five tables (and the seeded templates with them). Do not downgrade once real plans exist.
"""
import json
import uuid
from datetime import datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0016_care_plans_core'
down_revision: Union[str, None] = '0015_caregiver_kyc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RISKS = "'low','medium','high'"
SLOT_TIMES = {"morning": "08:00", "afternoon": "13:00", "evening": "18:00", "night": "21:00"}
EVERY_DAY = [1, 2, 3, 4, 5, 6, 7]
MEALS = ("morning", "afternoon", "evening")
ALL_DAY = ("morning", "afternoon", "evening")

# (key, name, summary, duration_days, review_every_days, default_risk, goals [(title, frequency)],
#  tasks [(title, slots)], special_instructions). Drafts for a clinician to rewrite (design doc section 7).
TEMPLATES = [
    ("stroke_recovery", "Stroke Recovery", "Daily support after a stroke: safe movement, therapy exercises, swallowing and skin care.",
     30, 14, "high",
     [("Safe mobility and transfers", "daily"), ("Daily range-of-motion practice", "daily"),
      ("Support speech and swallowing exercises as taught by the therapist", "daily"), ("Prevent falls", "ongoing")],
     [("Check and record BP and pulse", ("morning", "evening")), ("Assisted washing and dressing", ("morning",)),
      ("Prescribed exercises with the therapist's sheet", ("afternoon",)), ("Safe-swallow check at meals", MEALS),
      ("Skin and pressure-point check", ("evening",))],
     ["Follow the therapist's exercise sheet", "Report new weakness, slurred speech or confusion at once"]),
    ("diabetes_management", "Diabetes Management", "Meals, readings, foot care and gentle activity for a client living with diabetes.",
     30, 14, "medium",
     [("Regular meals and fluids", "daily"), ("Blood glucose checked and recorded as prescribed", "daily"),
      ("Daily foot check", "daily"), ("Gentle daily activity", "daily")],
     [("Check and record blood glucose as the prescriber ordered", ("morning", "evening")),
      ("Meal support, note what was eaten", MEALS), ("Foot and skin check", ("evening",)),
      ("30-minute walk if able", ("afternoon",))],
     ["Record readings, do not interpret them", "Report dizziness, sweating or unusual drowsiness at once"]),
    ("post_surgery_recovery", "Post-Surgery Recovery", "Wound care, comfort, gentle movement and nutrition while healing after an operation.",
     21, 7, "medium",
     [("Wound kept clean and dry as instructed", "daily"), ("Pain reported and recorded", "daily"),
      ("Gradual return to movement", "ongoing"), ("Adequate fluids and nutrition", "daily")],
     [("Wound check and record appearance", ("morning", "evening")), ("Assisted short walk", ("morning", "afternoon")),
      ("Hygiene and clean clothing", ("morning",)), ("Fluids and meal support", MEALS)],
     ["Wound care exactly as the surgical team instructed", "Report fever, spreading redness or discharge at once"]),
    ("cardiac_rehabilitation", "Cardiac Rehabilitation", "Light activity, resting readings and low-salt meals while recovering from a heart event.",
     45, 14, "high",
     [("Progressive light activity as cleared by the care team", "daily"), ("Resting BP and pulse recorded", "daily"),
      ("Low-salt, regular meals", "daily"), ("Report chest symptoms early", "ongoing")],
     [("Check and record BP and pulse", ("morning", "evening")), ("Light activity as instructed", ("afternoon",)),
      ("Weigh and record weight", ("morning",)), ("Meal support", MEALS)],
     ["Stop activity and call for help for chest pain, breathlessness or fainting",
      "Follow the cardiac team's activity limits"]),
    ("elderly_daily_living_dementia", "Elderly Daily Living and Dementia Support",
     "A calm daily routine with meals, fluids, safety and engagement for an older client or a client with dementia.",
     30, 14, "medium",
     [("Calm, predictable daily routine", "daily"), ("Safe home and fall prevention", "ongoing"),
      ("Regular meals, fluids and toileting", "daily"), ("Keep the client engaged", "daily")],
     [("Morning routine: wash, dress, breakfast", ("morning",)), ("Hydration prompts", ALL_DAY),
      ("Gentle activity or conversation", ("afternoon",)), ("Evening routine and a calm bedtime", ("evening",)),
      ("Note mood and confusion", ("evening",))],
     ["Do not leave the client alone if wandering is a risk", "Note any new confusion or change in behaviour"]),
]


def _body(goals, tasks, instructions) -> dict:
    return {
        "goals": [{"title": title, "target": None, "frequency": freq} for title, freq in goals],
        "tasks": [{"title": title, "time_slot": slot, "time_of_day": SLOT_TIMES[slot], "days_of_week": EVERY_DAY}
                  for title, slots in tasks for slot in slots],
        "special_instructions": list(instructions),
    }


def upgrade() -> None:
    op.create_table(
        'care_plan_templates',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('duration_days', sa.Integer(), nullable=False),
        sa.Column('review_every_days', sa.Integer(), nullable=True),
        sa.Column('default_risk_level', sa.String(), nullable=False),
        sa.Column('body', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('clinical_review_status', sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column('reviewed_by_admin_id', sa.UUID(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['reviewed_by_admin_id'], ['admin_users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('key'),
        sa.CheckConstraint(f"default_risk_level IN ({RISKS})", name='ck_care_plan_templates_risk'),
        sa.CheckConstraint("clinical_review_status IN ('pending','approved')", name='ck_care_plan_templates_review'),
    )

    op.create_table(
        'care_plans',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('patient_id', sa.UUID(), nullable=True),
        sa.Column('care_recipient_id', sa.UUID(), nullable=True),
        sa.Column('template_id', sa.UUID(), nullable=True),
        sa.Column('template_version', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(), server_default=sa.text("'active'"), nullable=False),
        sa.Column('risk_level', sa.String(), nullable=False),
        sa.Column('diagnosis_encrypted', sa.Text(), nullable=True),
        sa.Column('allergies_encrypted', sa.Text(), nullable=True),
        sa.Column('special_instructions_encrypted', sa.Text(), nullable=True),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('duration_days', sa.Integer(), nullable=False),
        sa.Column('review_every_days', sa.Integer(), nullable=True),
        sa.Column('created_by_caregiver_id', sa.UUID(), nullable=True),
        sa.Column('created_by_admin_id', sa.UUID(), nullable=True),
        sa.Column('created_from_booking_id', sa.UUID(), nullable=True),
        sa.Column('version', sa.Integer(), server_default=sa.text('1'), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('archived_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['patient_id'], ['patients.id']),
        sa.ForeignKeyConstraint(['care_recipient_id'], ['care_recipients.id']),
        sa.ForeignKeyConstraint(['template_id'], ['care_plan_templates.id']),
        sa.ForeignKeyConstraint(['created_by_caregiver_id'], ['caregivers.id']),
        sa.ForeignKeyConstraint(['created_by_admin_id'], ['admin_users.id']),
        sa.ForeignKeyConstraint(['created_from_booking_id'], ['bookings.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("num_nonnulls(patient_id, care_recipient_id) = 1", name='ck_care_plans_one_subject'),
        sa.CheckConstraint("num_nonnulls(created_by_caregiver_id, created_by_admin_id) = 1", name='ck_care_plans_one_author'),
        sa.CheckConstraint("status IN ('active','completed','archived')", name='ck_care_plans_status'),
        sa.CheckConstraint(f"risk_level IN ({RISKS})", name='ck_care_plans_risk'),
        sa.CheckConstraint("duration_days BETWEEN 1 AND 365", name='ck_care_plans_duration'),
        sa.CheckConstraint("review_every_days IS NULL OR review_every_days BETWEEN 7 AND 90", name='ck_care_plans_review'),
    )
    op.create_index('uq_care_plans_active_patient', 'care_plans', ['patient_id'], unique=True,
                    postgresql_where=sa.text("status = 'active' AND patient_id IS NOT NULL"))
    op.create_index('uq_care_plans_active_recipient', 'care_plans', ['care_recipient_id'], unique=True,
                    postgresql_where=sa.text("status = 'active' AND care_recipient_id IS NOT NULL"))
    op.create_index('ix_care_plans_created_by_caregiver', 'care_plans', ['created_by_caregiver_id'])
    op.create_index('ix_care_plans_status_updated', 'care_plans', ['status', sa.text('updated_at DESC')])

    op.create_table(
        'care_plan_goals',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('plan_id', sa.UUID(), nullable=False),
        sa.Column('title_encrypted', sa.Text(), nullable=True),
        sa.Column('target_encrypted', sa.Text(), nullable=True),
        sa.Column('frequency', sa.String(), nullable=False),
        sa.Column('status', sa.String(), server_default=sa.text("'active'"), nullable=False),
        sa.Column('sort_order', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('family_visible', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['care_plans.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("frequency IN ('daily','weekly','ongoing','one_time')", name='ck_care_plan_goals_frequency'),
        sa.CheckConstraint("status IN ('active','achieved','discontinued')", name='ck_care_plan_goals_status'),
    )
    op.create_index('ix_care_plan_goals_plan_sort', 'care_plan_goals', ['plan_id', 'sort_order'])

    op.create_table(
        'care_plan_tasks',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('plan_id', sa.UUID(), nullable=False),
        sa.Column('title_encrypted', sa.Text(), nullable=True),
        sa.Column('time_slot', sa.String(), nullable=False),
        sa.Column('time_of_day', sa.Time(), nullable=True),
        sa.Column('days_of_week', postgresql.ARRAY(sa.SmallInteger()), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('sort_order', sa.Integer(), server_default=sa.text('0'), nullable=False),
        sa.Column('family_visible', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['care_plans.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("time_slot IN ('morning','afternoon','evening','night')", name='ck_care_plan_tasks_slot'),
        sa.CheckConstraint("cardinality(days_of_week) BETWEEN 1 AND 7", name='ck_care_plan_tasks_days'),
    )
    op.create_index('ix_care_plan_tasks_plan_active', 'care_plan_tasks', ['plan_id', 'is_active'])

    op.create_table(
        'care_plan_events',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('plan_id', sa.UUID(), nullable=False),
        sa.Column('actor_type', sa.String(), nullable=False),
        sa.Column('actor_id', sa.UUID(), nullable=True),
        sa.Column('action', sa.String(), nullable=False),
        sa.Column('detail', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['plan_id'], ['care_plans.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("actor_type IN ('caregiver','admin','system')", name='ck_care_plan_events_actor'),
    )
    op.create_index('ix_care_plan_events_plan_created', 'care_plan_events', ['plan_id', 'created_at'])

    insert = sa.text(
        "INSERT INTO care_plan_templates (id, key, name, summary, duration_days, review_every_days, default_risk_level, "
        "body, version, is_active, clinical_review_status, created_at, updated_at) "
        "VALUES (:id, :key, :name, :summary, :duration, :review, :risk, CAST(:body AS jsonb), 1, true, 'pending', "
        ":now, :now) ON CONFLICT (key) DO NOTHING"
    )
    now = datetime.utcnow()
    for key, name, summary, duration, review, risk, goals, tasks, instructions in TEMPLATES:
        op.get_bind().execute(insert, {
            "id": uuid.uuid4(), "key": key, "name": name, "summary": summary, "duration": duration, "review": review,
            "risk": risk, "body": json.dumps(_body(goals, tasks, instructions)), "now": now,
        })


def downgrade() -> None:
    op.drop_table('care_plan_events')
    op.drop_table('care_plan_tasks')
    op.drop_table('care_plan_goals')
    op.drop_table('care_plans')
    op.drop_table('care_plan_templates')
