"""Admin: care plans and template review (slice B4). Mounted under /admin.

The Clinical Reviewer authors plans here (no console screens in v1): ``careplans.read`` reads, ``careplans.write`` creates and
edits, ``careplans.templates`` signs a template off. The writes are the caregiver routes' own services called with an
``AdminActor``, so the limits, the optimistic ``expected_version`` and the ``409`` shapes are the same. Every write is an
``admin_audit_log`` row in the same transaction, and so is opening a plan or its notes; the audit rows hold field names and ids,
never plan text. A nurse's "only today / only in a visit" rules do not apply to an admin: there is no visit. Health data:
every response is ``Cache-Control: no-store``, and a 422 never echoes the rejected value.
"""

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_permission
from app.api.v1.endpoints.caregiver_care_plans import ScrubbedValidationRoute
from app.db.models.admin import AdminUser
from app.db.models.care_plan import CarePlan, CarePlanGoal, CarePlanMedication, CarePlanTask
from app.schemas.admin_care_plan import (
    AdminCarePlanCreate, AdminCarePlanList, AdminCarePlanOut, AdminTemplate, AdminTemplateList, PlanStatus, TemplateReviewIn,
)
from app.schemas.care_plan import (
    CarePlanPatch, GoalCreate, GoalPatch, GoalWriteResult, MEDICATION_NOTICE, MedicationCreate, MedicationPatch,
    MedicationStop, MedicationWriteResult, ObservationCategory, ObservationIn, ObservationList, ObservationOut,
    TaskCreate, TaskPatch, TaskWriteResult,
)
from app.services import care_plan_admin as admin_svc
from app.services import care_plan_medications as meds
from app.services import care_plan_observations as notes
from app.services import care_plan_tasks as tasks
from app.services import care_plans as svc
from app.services.care_plans import AdminActor

NOT_FOUND = "Care plan not found"


NO_STORE = {"Cache-Control": "no-store"}


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(dependencies=[Depends(_no_store)], route_class=ScrubbedValidationRoute)

READ = require_permission("careplans.read")
WRITE = require_permission("careplans.write")
REVIEW = require_permission("careplans.templates")

WRITE_ERRORS = {
    404: {"description": "No such plan (or goal, task, medication)."},
    409: {"description": "`version_conflict` (body has the current `plan`), `plan_not_editable` (completed or archived plan), "
                         "or for a stopped medication `medication_stopped`."},
    422: {"description": "Validation, or `invalid_care_plan` (a limit, or end before start)."},
}


def _refuse(exc: svc.CarePlanError, **extra: Any) -> HTTPException:
    codes = {
        svc.PlanNotEditable: status.HTTP_409_CONFLICT, svc.VersionConflict: status.HTTP_409_CONFLICT,
        svc.CarePlanExists: status.HTTP_409_CONFLICT, meds.MedicationStopped: status.HTTP_409_CONFLICT,
        svc.TemplateNotFound: status.HTTP_404_NOT_FOUND, admin_svc.ClientNotFound: status.HTTP_404_NOT_FOUND,
        svc.InvalidPlan: status.HTTP_422_UNPROCESSABLE_ENTITY,
    }
    if isinstance(exc, (admin_svc.PlanNotFound, svc.TaskRemoved)):
        return _missing()
    # The dependency's header is lost when an HTTPException is raised, and a 409 carries the whole plan: set it here too.
    return HTTPException(status_code=codes[type(exc)], detail={"code": exc.code, "message": exc.message, **extra},
                         headers=NO_STORE)


def _conflict(db: Session, plan_id: UUID, actor: AdminActor, exc: svc.VersionConflict) -> HTTPException:
    """409 with the current plan, so the console can reload without another round trip. That body is other people's newer
    edits, i.e. a read of the plan, so it is audited as one."""
    db.expire_all()
    plan = admin_svc.get_plan(db, plan_id)
    body = AdminCarePlanOut.model_validate(admin_svc.build_admin_plan(db, plan)).model_dump(mode="json")
    admin_svc.audit_unchanged_read(db, actor, plan_id, "version_conflict")
    return _refuse(exc, plan=body)


def _missing() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND, headers=NO_STORE)


def _plan(db: Session, plan_id: UUID) -> CarePlan:
    try:
        return admin_svc.get_plan(db, plan_id)
    except admin_svc.PlanNotFound:
        raise _missing() from None


def _actor(admin: AdminUser, request: Request) -> AdminActor:
    return AdminActor(admin, request)


def _write(db: Session, plan_id: UUID, actor: AdminActor, fn):
    """Run one plan write (``fn`` receives the actor), mapping the service's refusals the way every admin write answers them.
    A write that changed nothing (the plan's version did not move) still returns plan text, so it is audited as a read."""
    before = admin_svc.get_plan(db, plan_id).version
    try:
        result = fn(actor)
    except svc.VersionConflict as exc:
        raise _conflict(db, plan_id, actor, exc)
    except svc.CarePlanError as exc:
        raise _refuse(exc)
    after = result.version if isinstance(result, CarePlan) else result[1]
    if after == before:
        admin_svc.audit_unchanged_read(db, actor, plan_id, "unchanged_write")
    return result


# --- plans ------------------------------------------------------------------------------------------------------------

@router.get("/care-plans", response_model=AdminCarePlanList)
def list_care_plans(
    plan_status: Optional[PlanStatus] = Query(None, alias="status"),
    q: Optional[str] = Query(None, max_length=100, description="Search by client name."),
    limit: int = Query(50, ge=1, le=admin_svc.LIST_MAX), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db), _admin: AdminUser = Depends(READ),
) -> Any:
    """Plans across all clients, newest change first. The rows carry no plan text; open a plan for that."""
    return admin_svc.list_plans(db, plan_status, q, limit, offset)


@router.post("/care-plans", response_model=AdminCarePlanOut, status_code=status.HTTP_201_CREATED,
             responses={404: {"description": "`client_not_found`."},
                        409: {"description": "`care_plan_exists` (body has `plan_id`): the client already has an active plan."},
                        422: {"description": "Validation, or `invalid_care_plan`."}})
def create_care_plan(body: AdminCarePlanCreate, request: Request, db: Session = Depends(get_db),
                     admin: AdminUser = Depends(WRITE)) -> Any:
    """Create a client's plan, blank or from a template (the template only fills what the body leaves out). Name the client
    with exactly one of `patient_id` or `care_recipient_id`. Audited."""
    try:
        plan = admin_svc.create_plan(db, _actor(admin, request), body)
    except svc.CarePlanExists as exc:
        raise _refuse(exc, plan_id=str(exc.plan_id))
    except svc.CarePlanError as exc:
        raise _refuse(exc)
    return admin_svc.build_admin_plan(db, plan)


@router.get("/care-plans/{plan_id}", response_model=AdminCarePlanOut, responses={404: {"description": "No such plan."}})
def get_care_plan(plan_id: UUID, request: Request, db: Session = Depends(get_db), admin: AdminUser = Depends(READ)) -> Any:
    """The full plan, at any status. **Opening a plan is audited** (`care_plan.view`)."""
    try:
        return admin_svc.view_plan(db, _actor(admin, request), plan_id)
    except svc.CarePlanError as exc:
        raise _refuse(exc)


@router.patch("/care-plans/{plan_id}", response_model=AdminCarePlanOut, responses=WRITE_ERRORS)
def update_care_plan(plan_id: UUID, body: CarePlanPatch, request: Request, db: Session = Depends(get_db),
                     admin: AdminUser = Depends(WRITE)) -> Any:
    """Header fields, allergies, special instructions, and `status` to `completed` or `archived`. Needs the plan's
    `expected_version`. Audited."""
    _plan(db, plan_id)
    plan = _write(db, plan_id, _actor(admin, request), lambda a: svc.update_plan(db, a, plan_id, body))
    return admin_svc.build_admin_plan(db, plan)


# --- notes ------------------------------------------------------------------------------------------------------------

@router.get("/care-plans/{plan_id}/observations", response_model=ObservationList,
            responses={404: {"description": "No such plan."}})
def list_observations(
    plan_id: UUID, request: Request,
    before: Optional[datetime] = Query(None, description="Return notes older than this timestamp (the previous page's "
                                                         "`next_before`)."),
    before_id: Optional[UUID] = Query(None, description="With `before`: the previous page's `next_before_id`."),
    limit: int = Query(notes.DEFAULT_LIMIT, ge=1, le=notes.MAX_LIMIT),
    category: Optional[ObservationCategory] = Query(None, description="Only this category."),
    db: Session = Depends(get_db), admin: AdminUser = Depends(READ),
) -> Any:
    """Visit notes, newest first, from nurses and admins. **Reading them is audited** (`care_plan.observations_view`)."""
    plan = _plan(db, plan_id)
    if before is not None and before.tzinfo is not None:
        before = before.astimezone(timezone.utc).replace(tzinfo=None)
    page = notes.list_observations(db, plan, before, before_id, limit, category)
    admin_svc.audit_notes_view(db, _actor(admin, request), plan)
    return page


@router.post("/care-plans/{plan_id}/observations", response_model=ObservationOut, status_code=status.HTTP_201_CREATED,
             responses={404: {"description": "No such plan."}, 409: {"description": "`plan_not_editable`."}})
def add_observation(plan_id: UUID, body: ObservationIn, request: Request, db: Session = Depends(get_db),
                    admin: AdminUser = Depends(WRITE)) -> Any:
    """Add a note as the Clinical Reviewer (no visit needed; the plan must be active). Notes cannot be edited or deleted: a
    correction is a new note. Does not change the plan's `version`. Audited."""
    _plan(db, plan_id)
    try:
        return notes.add_admin_observation(db, _actor(admin, request), plan_id, body)
    except svc.CarePlanError as exc:
        raise _refuse(exc)


# --- goals ------------------------------------------------------------------------------------------------------------

def _goal(db: Session, goal_id: UUID) -> CarePlanGoal:
    goal = db.get(CarePlanGoal, goal_id)
    if goal is None:
        raise _missing()
    return goal


def _goal_result(goal: CarePlanGoal, version: int) -> dict:
    return {"goal": svc.goal_out(goal), "plan_version": version}


@router.post("/care-plans/{plan_id}/goals", response_model=GoalWriteResult, status_code=status.HTTP_201_CREATED,
             responses=WRITE_ERRORS)
def add_goal(plan_id: UUID, body: GoalCreate, request: Request, db: Session = Depends(get_db),
             admin: AdminUser = Depends(WRITE)) -> Any:
    """Add a goal. `expected_version` is optional here: adding never overwrites anything. Audited."""
    _plan(db, plan_id)
    actor = _actor(admin, request)
    return _goal_result(*_write(db, plan_id, actor, lambda a: svc.add_goal(db, a, plan_id, body)))


@router.patch("/care-plan-goals/{goal_id}", response_model=GoalWriteResult, responses=WRITE_ERRORS)
def update_goal(goal_id: UUID, body: GoalPatch, request: Request, db: Session = Depends(get_db),
                admin: AdminUser = Depends(WRITE)) -> Any:
    """Edit a goal, or mark it achieved or discontinued. Needs the plan's `expected_version`. Audited."""
    goal = _goal(db, goal_id)
    return _goal_result(*_write(db, goal.plan_id, _actor(admin, request), lambda a: svc.update_goal(db, a, goal, body)))


@router.delete("/care-plan-goals/{goal_id}", response_model=GoalWriteResult, responses=WRITE_ERRORS)
def remove_goal(goal_id: UUID, request: Request, expected_version: int = Query(ge=1), db: Session = Depends(get_db),
                admin: AdminUser = Depends(WRITE)) -> Any:
    """Remove a goal: it is marked `discontinued` and kept. Needs the plan's `expected_version` as a query parameter. Audited."""
    goal = _goal(db, goal_id)
    return _goal_result(*_write(db, goal.plan_id, _actor(admin, request), lambda a: svc.remove_goal(db, a, goal, expected_version)))


# --- tasks ------------------------------------------------------------------------------------------------------------

def _task(db: Session, task_id: UUID) -> CarePlanTask:
    task = db.get(CarePlanTask, task_id)
    if task is None:
        raise _missing()
    return task


def _task_result(task: CarePlanTask, version: int) -> dict:
    return {"task": tasks.task_out(task), "plan_version": version}


@router.post("/care-plans/{plan_id}/tasks", response_model=TaskWriteResult, status_code=status.HTTP_201_CREATED,
             responses=WRITE_ERRORS)
def add_task(plan_id: UUID, body: TaskCreate, request: Request, db: Session = Depends(get_db),
             admin: AdminUser = Depends(WRITE)) -> Any:
    """Add a daily task. `expected_version` is optional here. The task is scheduled from today on. Audited."""
    _plan(db, plan_id)
    return _task_result(*_write(db, plan_id, _actor(admin, request), lambda a: tasks.add_task(db, a, plan_id, body)))


@router.patch("/care-plan-tasks/{task_id}", response_model=TaskWriteResult, responses=WRITE_ERRORS)
def update_task(task_id: UUID, body: TaskPatch, request: Request, db: Session = Depends(get_db),
                admin: AdminUser = Depends(WRITE)) -> Any:
    """Edit a task: title, slot, display time or days. Needs the plan's `expected_version`. A removed task is 404. Audited."""
    task = _task(db, task_id)
    return _task_result(*_write(db, task.plan_id, _actor(admin, request), lambda a: tasks.update_task(db, a, task, body)))


@router.delete("/care-plan-tasks/{task_id}", response_model=TaskWriteResult, responses=WRITE_ERRORS)
def remove_task(task_id: UUID, request: Request, expected_version: int = Query(ge=1), db: Session = Depends(get_db),
                admin: AdminUser = Depends(WRITE)) -> Any:
    """Remove a task: it stops being scheduled and leaves `tasks`; its ticks are kept. Needs `expected_version` as a query
    parameter. Audited."""
    task = _task(db, task_id)
    return _task_result(*_write(db, task.plan_id, _actor(admin, request), lambda a: tasks.remove_task(db, a, task, expected_version)))


# --- medications: record only -----------------------------------------------------------------------------------------

def _medication(db: Session, medication_id: UUID) -> CarePlanMedication:
    medication = db.get(CarePlanMedication, medication_id)
    if medication is None:
        raise _missing()
    return medication


def _medication_result(medication: CarePlanMedication, version: int) -> dict:
    return {"medication": meds.medication_out(medication), "plan_version": version}


def _medication_doc(summary: str) -> str:
    return f"{summary}\n\n**Not clinical advice.** {MEDICATION_NOTICE}"


@router.post("/care-plans/{plan_id}/medications", response_model=MedicationWriteResult, status_code=status.HTTP_201_CREATED,
             description=_medication_doc("Record a medicine the prescriber ordered. `expected_version` is optional here. At "
                                         "most 40 active medications per plan. Audited."),
             responses=WRITE_ERRORS)
def add_medication(plan_id: UUID, body: MedicationCreate, request: Request, db: Session = Depends(get_db),
                   admin: AdminUser = Depends(WRITE)) -> Any:
    _plan(db, plan_id)
    return _medication_result(*_write(db, plan_id, _actor(admin, request), lambda a: meds.add_medication(db, a, plan_id, body)))


@router.patch("/care-plan-medications/{medication_id}", response_model=MedicationWriteResult,
              description=_medication_doc("Edit a medication. Only the fields sent change; optional fields accept null to "
                                          "clear. Needs the plan's `expected_version`. A stopped medication cannot be "
                                          "edited. Audited."),
              responses=WRITE_ERRORS)
def update_medication(medication_id: UUID, body: MedicationPatch, request: Request, db: Session = Depends(get_db),
                      admin: AdminUser = Depends(WRITE)) -> Any:
    medication = _medication(db, medication_id)
    return _medication_result(*_write(db, medication.plan_id, _actor(admin, request), lambda a: meds.update_medication(db, a, medication, body)))


@router.post("/care-plan-medications/{medication_id}/stop", response_model=MedicationWriteResult,
             description=_medication_doc("Stop a medication: `status` becomes `stopped` and `stopped_at` is set. It stays on "
                                         "the plan as history and is never deleted. Needs the plan's `expected_version`. "
                                         "Stopping a stopped medication changes nothing. Audited."),
             responses=WRITE_ERRORS)
def stop_medication(medication_id: UUID, body: MedicationStop, request: Request, db: Session = Depends(get_db),
                    admin: AdminUser = Depends(WRITE)) -> Any:
    medication = _medication(db, medication_id)
    return _medication_result(*_write(
        db, medication.plan_id, _actor(admin, request),
        lambda a: meds.stop_medication(db, a, medication, body.expected_version)))


# --- templates --------------------------------------------------------------------------------------------------------

@router.get("/care-plan-templates", response_model=AdminTemplateList)
def list_templates(db: Session = Depends(get_db), _admin: AdminUser = Depends(READ)) -> Any:
    """Every template (active or not) with its content and review state. Go-live gate: each seeded template is reviewed or
    removed."""
    return admin_svc.list_templates(db)


@router.post("/care-plan-templates/{template_id}/review", response_model=AdminTemplate,
             responses={404: {"description": "`template_not_found`."}})
def review_template(template_id: UUID, body: TemplateReviewIn, request: Request, db: Session = Depends(get_db),
                    admin: AdminUser = Depends(REVIEW)) -> Any:
    """Sign a template off (`approved`) or take the sign-off back (`pending`); records the reviewer and time. Audited."""
    try:
        return admin_svc.review_template(db, _actor(admin, request), template_id, body)
    except svc.CarePlanError as exc:
        raise _refuse(exc)
