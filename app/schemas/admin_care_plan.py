"""Care plan admin API shapes (slice B4). The write bodies are the caregiver ones (``app.schemas.care_plan``), reused as they
are; only create gains a way to name the client, and the responses gain the subject and lose the booking and capabilities.

Plan content is health data. The audit log never carries it (see ``care_plans.actor_event``); these responses do, so every
route is ``careplans.read`` or ``careplans.write`` and ``Cache-Control: no-store``.
"""

from datetime import date, datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.schemas.care_plan import (
    AuthorOut, CarePlanCreate, ClientOut, GoalOut, MEDICATION_NOTICE, MedicationOut, RiskLevel, TaskOut, TemplateDetail,
    TemplateRef,
)

PlanStatus = Literal["active", "completed", "archived"]


class AdminCarePlanCreate(CarePlanCreate):
    """The caregiver create body plus the client: exactly one of ``patient_id`` (an app patient, or a visit booked for
    oneself) or ``care_recipient_id`` (a relative a booker made a visit for)."""
    patient_id: Optional[UUID] = None
    care_recipient_id: Optional[UUID] = None

    @model_validator(mode="after")
    def _one_client(self):
        if (self.patient_id is None) == (self.care_recipient_id is None):
            raise ValueError("send exactly one of patient_id or care_recipient_id")
        return self


class SubjectOut(BaseModel):
    kind: Literal["patient", "recipient"] = Field(description="Which table `id` is in: `patient` (patients) or `recipient` "
                                                              "(care_recipients). Send it back as `patient_id` or "
                                                              "`care_recipient_id` respectively.")
    id: UUID


class AdminCarePlanOut(BaseModel):
    id: UUID
    version: int = Field(description="Send as `expected_version` on the next write.")
    status: PlanStatus
    subject: SubjectOut
    client: ClientOut
    diagnosis: str
    risk_level: RiskLevel
    start_date: date
    duration_days: int
    review_every_days: Optional[int] = None
    review_due_on: Optional[date] = None
    template: Optional[TemplateRef] = None
    author: AuthorOut
    allergies: List[str]
    special_instructions: List[str]
    goals: List[GoalOut]
    medications: List[MedicationOut] = Field(description="Active ones first, then stopped ones (history). " + MEDICATION_NOTICE)
    tasks: List[TaskOut]
    created_at: datetime
    updated_at: datetime


class AdminCarePlanListItem(BaseModel):
    """A row of the list. No diagnosis or other plan text: open the plan for that (the open is audited)."""
    id: UUID
    version: int
    status: PlanStatus
    subject: SubjectOut
    client: ClientOut
    risk_level: RiskLevel
    start_date: date
    duration_days: int
    author: AuthorOut
    goal_count: int = Field(description="Goals that are not discontinued.")
    task_count: int = Field(description="Active tasks.")
    medication_count: int = Field(description="Active medications.")
    updated_at: datetime


class AdminCarePlanList(BaseModel):
    items: List[AdminCarePlanListItem]
    total: int
    limit: int
    offset: int


class AdminTemplate(TemplateDetail):
    """A template with its review state. ``clinical_review_status`` is inherited."""
    is_active: bool = Field(description="Inactive templates are hidden from nurses.")
    reviewed_by: Optional[str] = Field(None, description="Name of the admin who last signed it off or reverted it.")
    reviewed_at: Optional[datetime] = None


class AdminTemplateList(BaseModel):
    items: List[AdminTemplate]


class TemplateReviewIn(BaseModel):
    status: Literal["approved", "pending"] = Field(
        description="`approved` after a clinician has read the content; `pending` takes the approval back. Either way the "
                    "reviewer and time are recorded and the call is audited.")
