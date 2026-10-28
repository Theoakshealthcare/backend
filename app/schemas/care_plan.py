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
MAX_MEDICATIONS, MAX_MEDICATION_TIMES = 40, 12

# The "not clinical advice" notice for medications (decision D4). One constant: the OpenAPI descriptions of the medication
# routes and of the plan's `medications` field are built from it, and the mobile app shows the same words as a banner. The
# wording is for clinical sign-off; change it here and nowhere else.
MEDICATION_NOTICE = (
    "This is a record of what the prescriber ordered. Oakly does not check doses or interactions and does not remind "
    "anyone to give medicines. Follow the prescriber's instructions."
)

Diagnosis = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Target = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
FreeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
Duration = Annotated[int, Field(ge=1, le=365)]
ReviewEvery = Annotated[int, Field(ge=7, le=90)]
Day = date  # an alias, so a field called `date` does not shadow the type inside its own class
ObservationText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
ObservationCategory = Literal["observation", "special_note"]
MedicationFrequency = Literal["once_daily", "twice_daily", "three_times_daily", "four_times_daily", "weekly", "as_needed", "other"]
MedicationStatus = Literal["active", "stopped"]
MedicationName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
MedicationShort = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
MedicationReason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
HHMM = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$", examples=["08:00"])]  # [0-9], not \d: no Unicode digits


def _check_times(times: List[str]) -> List[str]:
    if len(set(times)) != len(times):
        raise ValueError("times_of_day must not repeat a time")
    return sorted(times)


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


class TaskCreate(TaskIn):
    expected_version: Optional[int] = Field(None, ge=1, description="Optional: adding never overwrites anything.")


class TaskPatch(BaseModel):
    """Only the fields sent change. ``time_of_day`` accepts null to clear it. Editing the days or the slot applies to every
    day, including days already past (the plan keeps one definition of a task)."""
    expected_version: int = Field(ge=1)
    title: Optional[Title] = None
    time_slot: Optional[TimeSlot] = None
    time_of_day: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$", examples=["08:00"],
                                       description="Display time, HH:MM. null clears it.")
    days_of_week: Optional[List[int]] = Field(None, min_length=1, max_length=7, description="1 = Monday .. 7 = Sunday.")

    @field_validator("days_of_week")
    @classmethod
    def _days(cls, days: Optional[List[int]]) -> Optional[List[int]]:
        return _check_days(days) if days is not None else None

    @model_validator(mode="after")
    def _required_fields_are_not_cleared(self):
        for name in ("title", "time_slot", "days_of_week"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class CompletionIn(BaseModel):
    date: Day = Field(description="The day to tick, YYYY-MM-DD. Only today (Africa/Lagos) is accepted.")
    done: bool = Field(description="true ticks the task, false unticks it. Repeating a call changes nothing.")


class ObservationIn(BaseModel):
    body: ObservationText
    category: ObservationCategory = Field("observation", description="`special_note` shows under Allergies and Special "
                                                                     "Instructions; `observation` under Notes.")


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


class MedicationFields(BaseModel):
    """What the prescriber ordered, as the nurse records it. Record only: nothing here is checked against anything."""
    name: MedicationName
    strength: Optional[MedicationShort] = Field(None, examples=["500 mg"], description="The product's strength, as written.")
    dose_amount: Optional[MedicationShort] = Field(None, examples=["1"], description="Text, so '1/2' and '5' both fit.")
    dose_unit: Optional[MedicationShort] = Field(None, examples=["tablet"])
    route: Optional[MedicationShort] = Field(None, examples=["oral"])
    reason: Optional[MedicationReason] = None
    prescribed_by: Optional[MedicationName] = None
    note: Optional[FreeText] = None
    frequency: MedicationFrequency
    times_of_day: List[HHMM] = Field(default_factory=list, max_length=MAX_MEDICATION_TIMES,
                                     description="Display times, HH:MM. Empty for as_needed or other.")
    start_date: Optional[date] = Field(None, description="Defaults to today (Africa/Lagos).")
    end_date: Optional[date] = Field(None, description="Not before `start_date`; null while ongoing.")

    @field_validator("times_of_day")
    @classmethod
    def _times(cls, times: List[str]) -> List[str]:
        return _check_times(times)


class MedicationCreate(MedicationFields):
    expected_version: Optional[int] = Field(None, ge=1, description="Optional: adding never overwrites anything.")


class MedicationPatch(BaseModel):
    """Only the fields sent change. Every optional field accepts null to clear it. A stopped medication cannot be edited:
    record a new one."""
    expected_version: int = Field(ge=1)
    name: Optional[MedicationName] = None
    strength: Optional[MedicationShort] = None
    dose_amount: Optional[MedicationShort] = None
    dose_unit: Optional[MedicationShort] = None
    route: Optional[MedicationShort] = None
    reason: Optional[MedicationReason] = None
    prescribed_by: Optional[MedicationName] = None
    note: Optional[FreeText] = None
    frequency: Optional[MedicationFrequency] = None
    times_of_day: Optional[List[HHMM]] = Field(None, max_length=MAX_MEDICATION_TIMES)
    start_date: Optional[date] = None
    end_date: Optional[date] = None

    @field_validator("times_of_day")
    @classmethod
    def _times(cls, times: Optional[List[str]]) -> Optional[List[str]]:
        return _check_times(times) if times is not None else None

    @model_validator(mode="after")
    def _required_fields_are_not_cleared(self):
        for name in ("name", "frequency", "times_of_day", "start_date"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class MedicationStop(BaseModel):
    expected_version: int = Field(ge=1)


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


class MedicationOut(BaseModel):
    id: UUID
    name: str
    strength: Optional[str] = None
    dose_amount: Optional[str] = None
    dose_unit: Optional[str] = None
    route: Optional[str] = None
    reason: Optional[str] = None
    prescribed_by: Optional[str] = None
    note: Optional[str] = None
    frequency: MedicationFrequency
    times_of_day: List[str] = Field(description="HH:MM, earliest first; may be empty.")
    start_date: date
    end_date: Optional[date] = None
    status: MedicationStatus
    stopped_at: Optional[datetime] = Field(None, description="Set when `status` is `stopped`, null otherwise.")


class MedicationWriteResult(BaseModel):
    medication: MedicationOut
    plan_version: int = Field(description="The plan's version after this write; use it as the next expected_version.")


class Capabilities(BaseModel):
    can_edit_plan: bool = Field(description="A nurse, and the plan is active.")
    can_tick_tasks: bool = Field(description="The plan is active and the caller has a visit for this client in progress.")
    can_add_observation: bool = Field(description="Same rule as `can_tick_tasks`.")


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
    medications: List[MedicationOut] = Field(description="Active ones first, then stopped ones (history). " + MEDICATION_NOTICE)
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
    progress_percent: Optional[int] = Field(None, description="The plan's overall progress (see the progress call); null "
                                                               "while there is nothing to measure.")


class CarePlanList(BaseModel):
    items: List[CarePlanListItem]


class Candidate(BaseModel):
    booking_id: UUID
    client: ClientOut
    booking_status: str
    scheduled_date: datetime


class CandidateList(BaseModel):
    items: List[Candidate]


class TaskWriteResult(BaseModel):
    task: TaskOut
    plan_version: int = Field(description="The plan's version after this write; use it as the next expected_version.")


class DayTask(BaseModel):
    id: UUID
    title: str
    time_of_day: Optional[str] = Field(None, description="HH:MM")
    done: bool
    done_by: Optional[str] = Field(None, description="Name of the caregiver who ticked it; null when not done.")
    done_at: Optional[datetime] = None


class DaySlot(BaseModel):
    slot: TimeSlot
    time_of_day: Optional[str] = Field(None, description="The earliest task time in this slot, for the heading.")
    tasks: List[DayTask]


class TaskDay(BaseModel):
    date: Day
    is_today: bool
    can_tick: bool = Field(description="Today, the plan is active, and the caller has a visit for this client in progress.")
    slots: List[DaySlot] = Field(description="Only slots that have a task that day, in day order.")


class CompletionOut(BaseModel):
    task_id: UUID
    date: Day
    done: bool
    done_by: Optional[str] = None
    done_at: Optional[datetime] = None


class ObservationOut(BaseModel):
    id: UUID
    category: ObservationCategory
    body: str
    author: AuthorOut
    created_at: datetime


class ObservationList(BaseModel):
    items: List[ObservationOut]
    next_before: Optional[datetime] = Field(None, description="Pass as `before` for the next (older) page; null on the last.")
    next_before_id: Optional[UUID] = Field(None, description="Pass as `before_id` together with `before` (it breaks ties "
                                                              "between notes with the same timestamp); null on the last.")


class ProgressDay(BaseModel):
    date: Day
    scheduled: int
    completed: int


class GoalCounts(BaseModel):
    active: int
    achieved: int
    discontinued: int


class Progress(BaseModel):
    overall_percent: Optional[int] = Field(None, description="Ticked / scheduled task occurrences over visit days only; "
                                                              "null when there is nothing to measure yet.")
    scheduled: int
    completed: int
    visit_days: int
    day: int = Field(description="Day number of the plan, 0 before it starts, never above `of_days`.")
    of_days: int
    review_due_on: Optional[date] = None
    by_day: List[ProgressDay] = Field(description="The last 14 visit days, oldest first.")
    goals: GoalCounts
    last_observation_at: Optional[datetime] = None


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
