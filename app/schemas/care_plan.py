"""Care plan request and response shapes (caregiver app).

Responses are closed lists: no address, phone, email, booking condition or special instructions of the booking, and no
ciphertext. ``family_visible`` appears in no request or response body in v1 (family view is groundwork only).
"""

from datetime import date, datetime
from typing import Annotated, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

RiskLevel = Literal["low", "medium", "high"]
GoalFrequency = Literal["daily", "weekly", "ongoing", "one_time"]
GoalStatus = Literal["active", "achieved", "discontinued"]
TimeSlot = Literal["morning", "afternoon", "evening", "night"]

MAX_GOALS, MAX_TASKS, MAX_LIST_ITEMS = 30, 60, 30

Diagnosis = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Target = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
FreeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
Duration = Annotated[int, Field(ge=1, le=365)]
ReviewEvery = Annotated[int, Field(ge=7, le=90)]


def _check_days(days: List[int]) -> List[int]:
    if any(d < 1 or d > 7 for d in days) or len(set(days)) != len(days):
        raise ValueError("days_of_week must be unique numbers from 1 (Monday) to 7 (Sunday)")
    return sorted(days)


# --- requests -------------------------------------------------------------------------------------------------------

class GoalIn(BaseModel):
    title: Title
    target: Optional[Target] = None
    frequency: GoalFrequency


class TaskIn(BaseModel):
    title: Title
    time_slot: TimeSlot
    time_of_day: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$", examples=["08:00"],
                                       description="Display time, HH:MM.")
    days_of_week: List[int] = Field(min_length=1, max_length=7, description="1 = Monday .. 7 = Sunday.")

    @field_validator("days_of_week")
    @classmethod
    def _days(cls, days: List[int]) -> List[int]:
        return _check_days(days)


class CarePlanCreate(BaseModel):
    template_id: Optional[UUID] = Field(None, description="Copy goals, tasks, instructions and defaults from this template. "
                                                          "Anything sent below wins over the template.")
    diagnosis: Diagnosis
    risk_level: Optional[RiskLevel] = Field(None, description="Required unless the template supplies a default.")
    start_date: Optional[date] = Field(None, description="Defaults to today (Africa/Lagos).")
    duration_days: Optional[Duration] = Field(None, description="Defaults to the template's, else 30.")
    review_every_days: Optional[ReviewEvery] = None
    goals: Optional[List[GoalIn]] = Field(None, max_length=MAX_GOALS)
    tasks: Optional[List[TaskIn]] = Field(None, max_length=MAX_TASKS)
    allergies: Optional[List[FreeText]] = Field(None, max_length=MAX_LIST_ITEMS)
    special_instructions: Optional[List[FreeText]] = Field(None, max_length=MAX_LIST_ITEMS)


class CarePlanPatch(BaseModel):
    """Only the fields sent change. ``review_every_days``, ``allergies`` and ``special_instructions`` accept null to clear."""
    expected_version: int = Field(ge=1)
    diagnosis: Optional[Diagnosis] = None
    risk_level: Optional[RiskLevel] = None
    start_date: Optional[date] = None
    duration_days: Optional[Duration] = None
    review_every_days: Optional[ReviewEvery] = None
    allergies: Optional[List[FreeText]] = Field(None, max_length=MAX_LIST_ITEMS)
    special_instructions: Optional[List[FreeText]] = Field(None, max_length=MAX_LIST_ITEMS)
    status: Optional[Literal["completed", "archived"]] = Field(
        None, description="Completing or archiving makes the plan read-only and frees the client for a new active plan.")

    @model_validator(mode="after")
    def _required_fields_are_not_cleared(self):
        for name in ("diagnosis", "risk_level", "start_date", "duration_days", "status"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class GoalCreate(GoalIn):
    expected_version: Optional[int] = Field(None, ge=1, description="Optional: adding never overwrites anything.")


class GoalPatch(BaseModel):
    expected_version: int = Field(ge=1)
    title: Optional[Title] = None
    target: Optional[Target] = Field(None, description="null clears it.")
    frequency: Optional[GoalFrequency] = None
    status: Optional[GoalStatus] = None

    @model_validator(mode="after")
    def _required_fields_are_not_cleared(self):
        for name in ("title", "frequency", "status"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


# --- responses ------------------------------------------------------------------------------------------------------

class ClientOut(BaseModel):
    full_name: str
    age: Optional[int] = None
    gender: Optional[str] = None


class TemplateRef(BaseModel):
    id: UUID
    name: str
    version: int


class AuthorOut(BaseModel):
    name: str
    kind: Literal["caregiver", "admin"]


class GoalOut(BaseModel):
    id: UUID
    title: str
    target: Optional[str] = None
    frequency: GoalFrequency
    status: GoalStatus


class TaskOut(BaseModel):
    id: UUID
    title: str
    time_slot: TimeSlot
    time_of_day: Optional[str] = Field(None, description="HH:MM")
    days_of_week: List[int]


class Capabilities(BaseModel):
    can_edit_plan: bool


class CarePlanOut(BaseModel):
    id: UUID
    version: int
    status: Literal["active", "completed", "archived"]
    booking_id: UUID = Field(description="The live booking that gives this caller access.")
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
    tasks: List[TaskOut]
    capabilities: Capabilities
    created_at: datetime
    updated_at: datetime


class CarePlanSummary(BaseModel):
    id: UUID
    version: int
    status: Literal["active", "completed", "archived"]
    diagnosis: str
    risk_level: RiskLevel
    start_date: date
    duration_days: int
    review_every_days: Optional[int] = None
    review_due_on: Optional[date] = None
    goal_count: int = Field(description="Goals that are not discontinued.")
    task_count: int


class VisitCarePlan(BaseModel):
    booking_id: UUID
    subject_ready: bool = Field(description="false: an app booking for someone else, so there is no identifiable client "
                                            "and no care plan is possible.")
    can_author: bool = Field(description="A nurse, and a client who can have a plan.")
    plan: Optional[CarePlanSummary] = Field(None, description="The client's active plan; null if there is none yet.")


class CarePlanListItem(BaseModel):
    id: UUID
    booking_id: UUID
    client: ClientOut
    diagnosis: str
    risk_level: RiskLevel
    status: Literal["active", "completed", "archived"]
    start_date: date


class CarePlanList(BaseModel):
    items: List[CarePlanListItem]


class Candidate(BaseModel):
    booking_id: UUID
    client: ClientOut
    booking_status: str
    scheduled_date: datetime


class CandidateList(BaseModel):
    items: List[Candidate]


class GoalWriteResult(BaseModel):
    goal: GoalOut
    plan_version: int = Field(description="The plan's version after this write; use it as the next expected_version.")


class TemplateListItem(BaseModel):
    id: UUID
    key: str
    name: str
    summary: str
    duration_days: int
    review_every_days: Optional[int] = None
    default_risk_level: RiskLevel
    clinical_review_status: Literal["pending", "approved"] = Field(
        description="While pending, show 'Not yet clinically reviewed'.")
    version: int


class TemplateGoal(BaseModel):
    title: str
    target: Optional[str] = None
    frequency: GoalFrequency


class TemplateTask(BaseModel):
    title: str
    time_slot: TimeSlot
    time_of_day: Optional[str] = None
    days_of_week: List[int]


class TemplateList(BaseModel):
    items: List[TemplateListItem]


class TemplateDetail(TemplateListItem):
    goals: List[TemplateGoal]
    tasks: List[TemplateTask]
    special_instructions: List[str]
