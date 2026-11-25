"""Clinician-approved care template text, the Elderly template for any caregiver, and the caregiver documentation space.

Revision ID: 0031_care_templates_docs_space
Revises: 0030_bank_name_review
Create Date: 2026-11-25

* **Template text** (clinician review pack, section 1). The four templates the clinical lead approved "as annotated" are
  rewritten in place and their ``version`` goes 1 -> 2: every one gets the line "Contact the family and inform the family's doctor"
  (what "report at once" means); Diabetes gets the glucose wording and "document" on the meal task; Cardiac gets the discharge
  wording on the activity task; Post-surgery gets the pain task its goal ("Pain reported and recorded") was missing. Plans
  already made from version 1 keep their own copy of the text (``care_plans.template_version`` says which).
  ``clinical_review_status`` is deliberately left as it is: sign-off is the admin console's ``careplans.templates`` action, and
  this migration does not sign anything off in anyone's name.
* ``care_plan_templates.caregiver_can_use`` (boolean, default false): true for the Elderly template, which the clinician
  wants handled "either Nurse or Caregiver or both". Every other template stays nurse-only.
* ``visit_documentation``: a caregiver's own documentation for a visit. Encrypted, append-only, private to its author and the
  admin console, never sent to the patient or the family.

Downgrade drops the table and the column and puts the version 1 text back (reversing only the edits made here). Do not
downgrade once caregivers have written documentation: the table and its rows are dropped.
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0031_care_templates_docs_space'
down_revision: Union[str, None] = '0030_bank_name_review'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FAMILY_LINE = "Contact the family and inform the family's doctor"
SLOT_TIMES = {"morning": "08:00", "afternoon": "13:00", "evening": "18:00"}
EVERY_DAY = [1, 2, 3, 4, 5, 6, 7]

# key -> the edits. rename_tasks: {old title: new title}; add_tasks: [(title, slots)]; rename_instructions: {old: new};
# add_instructions: [text]. Applied to the stored body; the downgrade reverses each one.
EDITS = {
    "stroke_recovery": {"add_instructions": [FAMILY_LINE]},
    "diabetes_management": {
        "rename_tasks": {"Meal support, note what was eaten": "Meal support, note what was eaten and document"},
        "rename_instructions": {
            "Report dizziness, sweating or unusual drowsiness at once":
                "Report dizziness, low or high blood glucose, sweating or unusual drowsiness at once"},
        "add_instructions": [FAMILY_LINE],
    },
    "post_surgery_recovery": {
        "add_tasks": [("Ask about pain and record it", ("morning", "evening"))],
        "add_instructions": [FAMILY_LINE],
    },
    "cardiac_rehabilitation": {
        "rename_tasks": {"Light activity as instructed":
                         "Light activity as instructed by care team or following post-hospital discharge instructions"},
        "add_instructions": [FAMILY_LINE],
    },
}


def _task(title: str, slot: str) -> dict:
    return {"title": title, "time_slot": slot, "time_of_day": SLOT_TIMES[slot], "days_of_week": EVERY_DAY}


def _forward(body: dict, edit: dict) -> dict:
    for old, new in edit.get("rename_tasks", {}).items():
        for task in body["tasks"]:
            if task["title"] == old:
                task["title"] = new
    for title, slots in edit.get("add_tasks", []):
        if not any(t["title"] == title for t in body["tasks"]):
            body["tasks"].extend(_task(title, slot) for slot in slots)
    for old, new in edit.get("rename_instructions", {}).items():
        body["special_instructions"] = [new if text == old else text for text in body["special_instructions"]]
    for text in edit.get("add_instructions", []):
        if text not in body["special_instructions"]:
            body["special_instructions"].append(text)
    return body


def _backward(body: dict, edit: dict) -> dict:
    for old, new in edit.get("rename_tasks", {}).items():
        for task in body["tasks"]:
            if task["title"] == new:
                task["title"] = old
    added = {title for title, _ in edit.get("add_tasks", [])}
    body["tasks"] = [t for t in body["tasks"] if t["title"] not in added]
    for old, new in edit.get("rename_instructions", {}).items():
        body["special_instructions"] = [old if text == new else text for text in body["special_instructions"]]
    drop = set(edit.get("add_instructions", []))
    body["special_instructions"] = [text for text in body["special_instructions"] if text not in drop]
    return body


def _rewrite(direction, version_from: int, version_to: int) -> None:
    conn = op.get_bind()
    for key, edit in EDITS.items():
        row = conn.execute(sa.text("SELECT id, body FROM care_plan_templates WHERE key = :k"), {"k": key}).first()
        if row is None:
            continue
        body = row.body if isinstance(row.body, dict) else json.loads(row.body)
        conn.execute(
            sa.text("UPDATE care_plan_templates SET body = CAST(:body AS jsonb), version = :v, updated_at = now() "
                    "WHERE id = :id AND version = :vf"),
            {"body": json.dumps(direction(body, edit)), "v": version_to, "vf": version_from, "id": row.id})


def upgrade() -> None:
    op.add_column('care_plan_templates', sa.Column('caregiver_can_use', sa.Boolean(), nullable=False,
                                                   server_default=sa.text('false')))
    op.execute("UPDATE care_plan_templates SET caregiver_can_use = true WHERE key = 'elderly_daily_living_dementia'")
    _rewrite(_forward, 1, 2)

    op.create_table(
        'visit_documentation',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('caregiver_id', sa.UUID(), nullable=False),
        sa.Column('booking_id', sa.UUID(), nullable=False),
        sa.Column('plan_id', sa.UUID(), nullable=True),
        sa.Column('body_encrypted', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['caregiver_id'], ['caregivers.id']),
        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id']),
        sa.ForeignKeyConstraint(['plan_id'], ['care_plans.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_visit_documentation_booking_created', 'visit_documentation', ['booking_id', 'created_at'])
    op.create_index('ix_visit_documentation_caregiver_created', 'visit_documentation',
                    ['caregiver_id', sa.text('created_at DESC')])


def downgrade() -> None:
    op.drop_table('visit_documentation')
    _rewrite(_backward, 2, 1)
    op.drop_column('care_plan_templates', 'caregiver_can_use')
