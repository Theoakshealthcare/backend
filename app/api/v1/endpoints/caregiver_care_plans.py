"""Care plans for a caregiver's clients (slice B1). Mounted under /caregiver.

Every refusal for lack of access is one 404 ``"Care plan not found"``: the same body whether the booking, the plan or the
client exists, so a response never works as an oracle. A caregiver who has access but is not a nurse gets 403
``not_a_nurse`` on authoring routes (access is checked first, so that 403 never leaks the existence of a plan). Health data:
every response is ``Cache-Control: no-store``.
"""

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.api.deps import get_current_caregiver, get_db
from app.db.models.care_plan import CarePlan, CarePlanGoal
from app.db.models.caregiver import Caregiver
from app.db.models.user import User
from app.schemas.care_plan import (
    CandidateList, CarePlanCreate, CarePlanList, CarePlanOut, CarePlanPatch, GoalCreate, GoalPatch, GoalWriteResult,
    TemplateDetail, TemplateList, VisitCarePlan,
)
from app.services import care_plans as svc
from app.services.care_plan_access import (
    NotANurse, PlanAccess, PlanNotAvailable, require_author, resolve_plan_access, resolve_plan_access_for_plan,
)

NOT_FOUND = "Care plan not found"
NOT_A_NURSE = {"code": "not_a_nurse", "message": "Only nurses can create or change a care plan."}


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


class ScrubbedValidationRoute(APIRoute):
    """FastAPI's 422 echoes the rejected value (`input`). On care plan routes that value is health text, so the error names
    the field and the rule, never the value."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def scrubbed(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                raise RequestValidationError([{k: v for k, v in e.items() if k != "input"} for e in exc.errors()]) from None

        return scrubbed


router = APIRouter(dependencies=[Depends(_no_store)], route_class=ScrubbedValidationRoute)

NO_ACCESS = {404: {"description": "No access of any kind (not your booking, not live, no such plan or client). "
                                  "One answer for all of these."}}
NURSE_ONLY = {403: {"description": "`not_a_nurse`: the caregiver has no nurse skill."}}


def _caregiver(db: Session, user: User) -> Caregiver:
    caregiver = db.query(Caregiver).filter(Caregiver.user_id == user.id).first()
    if caregiver is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    return caregiver


def _nurse(db: Session, caregiver: Caregiver) -> None:
    try:
        require_author(db, caregiver)
    except NotANurse:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NOT_A_NURSE)


def _plan_access(db: Session, caregiver: Caregiver, plan_id: UUID) -> tuple[CarePlan, PlanAccess]:
    plan = db.get(CarePlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    try:
        return plan, resolve_plan_access_for_plan(db, caregiver, plan)
    except PlanNotAvailable:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)


def _booking_access(db: Session, caregiver: Caregiver, booking_id: UUID) -> PlanAccess:
    try:
        return resolve_plan_access(db, caregiver, booking_id)
    except PlanNotAvailable:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)


def _refuse(exc: svc.CarePlanError, **extra: Any) -> HTTPException:
    codes = {
        svc.PlanNotEditable: status.HTTP_409_CONFLICT, svc.VersionConflict: status.HTTP_409_CONFLICT,
        svc.CarePlanExists: status.HTTP_409_CONFLICT, svc.ClientNotIdentified: status.HTTP_409_CONFLICT,
        svc.TemplateNotFound: status.HTTP_404_NOT_FOUND, svc.InvalidPlan: status.HTTP_422_UNPROCESSABLE_ENTITY,
    }
    return HTTPException(status_code=codes[type(exc)], detail={"code": exc.code, "message": exc.message, **extra})


def _conflict(db: Session, caregiver: Caregiver, plan_id: UUID, exc: svc.VersionConflict) -> HTTPException:
    """409 with the current plan, so the app can reload without another round trip."""
    db.expire_all()
    plan, access = _plan_access(db, caregiver, plan_id)
    return _refuse(exc, plan=CarePlanOut.model_validate(svc.build_plan(db, caregiver, access, plan)).model_dump(mode="json"))


# --- templates (nurses) ---------------------------------------------------------------------------------------------

@router.get("/care-plan-templates", response_model=TemplateList, responses=NURSE_ONLY)
def list_templates(current_user: User = Depends(get_current_caregiver), db: Session = Depends(get_db)) -> Any:
    """The seeded templates for the Templates tab. Not yet clinically reviewed while ``clinical_review_status`` is pending."""
    _nurse(db, _caregiver(db, current_user))
    return svc.list_templates(db)


@router.get("/care-plan-templates/{template_id}", response_model=TemplateDetail,
            responses={**NURSE_ONLY, 404: {"description": "No such (active) template."}})
def get_template(template_id: UUID, current_user: User = Depends(get_current_caregiver),
                 db: Session = Depends(get_db)) -> Any:
    """Preview: the goals, tasks and special instructions a plan created from this template starts with."""
    _nurse(db, _caregiver(db, current_user))
    try:
        return svc.template_detail(svc.get_template(db, template_id))
    except svc.TemplateNotFound as exc:
        raise _refuse(exc)


# --- lists and the visit summary --------------------------------------------------------------------------------------

@router.get("/care-plans", response_model=CarePlanList)
def list_care_plans(
    plan_status: str = Query("active", alias="status", pattern="^(active|completed)$"),
    q: Optional[str] = Query(None, max_length=100, description="Search by client name."),
    current_user: User = Depends(get_current_caregiver), db: Session = Depends(get_db),
) -> Any:
    """The Assigned Care Plan tab: plans for the clients of my live visits (accepted, on the way or in progress)."""
    return svc.list_assigned(db, _caregiver(db, current_user), plan_status, q)


@router.get("/care-plans/candidates", response_model=CandidateList, responses=NURSE_ONLY)
def list_candidates(current_user: User = Depends(get_current_caregiver), db: Session = Depends(get_db)) -> Any:
    """The client picker for "Use template": clients of my live visits who have no active plan yet. Declared before
    ``/care-plans/{plan_id}`` so the word is not read as an id."""
    caregiver = _caregiver(db, current_user)
    _nurse(db, caregiver)
    return svc.list_candidates(db, caregiver)


@router.get("/bookings/{booking_id}/care-plan", response_model=VisitCarePlan, responses=NO_ACCESS)
def get_visit_care_plan(booking_id: UUID, current_user: User = Depends(get_current_caregiver),
                        db: Session = Depends(get_db)) -> Any:
    """Care plan summary for one visit. 200 with ``plan: null`` means access is fine and the client has no active plan yet;
    ``subject_ready: false`` means this visit's client cannot be identified (an app booking for someone else); 404 means no
    access."""
    caregiver = _caregiver(db, current_user)
    return svc.visit_summary(db, caregiver, _booking_access(db, caregiver, booking_id))


# --- create, read, edit -----------------------------------------------------------------------------------------------

@router.post("/bookings/{booking_id}/care-plan", response_model=CarePlanOut, status_code=status.HTTP_201_CREATED,
             responses={**NO_ACCESS, **NURSE_ONLY,
                        409: {"description": "`care_plan_exists` (body has `plan_id`) or `client_not_identified`."},
                        422: {"description": "Validation, or `invalid_care_plan`."}})
def create_care_plan(booking_id: UUID, body: CarePlanCreate, current_user: User = Depends(get_current_caregiver),
                     db: Session = Depends(get_db)) -> Any:
    """Create the client's plan, blank or from a template (the template only fills what the body leaves out)."""
    caregiver = _caregiver(db, current_user)
    access = _booking_access(db, caregiver, booking_id)
    _nurse(db, caregiver)
    try:
        plan = svc.create_plan(db, caregiver, access, body)
    except svc.CarePlanExists as exc:
        raise _refuse(exc, plan_id=str(exc.plan_id))
    except svc.CarePlanError as exc:
        raise _refuse(exc)
    return svc.build_plan(db, caregiver, access, plan)


@router.get("/care-plans/{plan_id}", response_model=CarePlanOut, responses=NO_ACCESS)
def get_care_plan(plan_id: UUID, current_user: User = Depends(get_current_caregiver), db: Session = Depends(get_db)) -> Any:
    """The full plan, visible while any of my live visits is for this client. A completed or archived plan stays visible
    as history."""
    caregiver = _caregiver(db, current_user)
    plan, access = _plan_access(db, caregiver, plan_id)
    return svc.build_plan(db, caregiver, access, plan)


@router.patch("/care-plans/{plan_id}", response_model=CarePlanOut,
              responses={**NO_ACCESS, **NURSE_ONLY, 409: {"description": "`version_conflict` (body has the current `plan`) "
                                                                         "or `plan_not_editable`."}})
def update_care_plan(plan_id: UUID, body: CarePlanPatch, current_user: User = Depends(get_current_caregiver),
                     db: Session = Depends(get_db)) -> Any:
    """Header fields, allergies, special instructions, and `status` to `completed` or `archived`. Needs the plan's
    `expected_version`."""
    caregiver = _caregiver(db, current_user)
    plan, access = _plan_access(db, caregiver, plan_id)
    _nurse(db, caregiver)
    try:
        plan = svc.update_plan(db, caregiver, plan_id, body)
    except svc.VersionConflict as exc:
        raise _conflict(db, caregiver, plan_id, exc)
    except svc.CarePlanError as exc:
        raise _refuse(exc)
    return svc.build_plan(db, caregiver, access, plan)


# --- goals ------------------------------------------------------------------------------------------------------------

def _goal_access(db: Session, caregiver: Caregiver, goal_id: UUID) -> CarePlanGoal:
    goal = db.get(CarePlanGoal, goal_id)
    if goal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    _plan_access(db, caregiver, goal.plan_id)
    return goal


def _goal_result(goal: CarePlanGoal, version: int) -> dict:
    return {"goal": svc.goal_out(goal), "plan_version": version}


@router.post("/care-plans/{plan_id}/goals", response_model=GoalWriteResult, status_code=status.HTTP_201_CREATED,
             responses={**NO_ACCESS, **NURSE_ONLY})
def add_goal(plan_id: UUID, body: GoalCreate, current_user: User = Depends(get_current_caregiver),
             db: Session = Depends(get_db)) -> Any:
    """Add a goal. `expected_version` is optional here: adding never overwrites anything."""
    caregiver = _caregiver(db, current_user)
    _plan_access(db, caregiver, plan_id)
    _nurse(db, caregiver)
    try:
        return _goal_result(*svc.add_goal(db, caregiver, plan_id, body))
    except svc.VersionConflict as exc:
        raise _conflict(db, caregiver, plan_id, exc)
    except svc.CarePlanError as exc:
        raise _refuse(exc)


@router.patch("/care-plan-goals/{goal_id}", response_model=GoalWriteResult, responses={**NO_ACCESS, **NURSE_ONLY})
def update_goal(goal_id: UUID, body: GoalPatch, current_user: User = Depends(get_current_caregiver),
                db: Session = Depends(get_db)) -> Any:
    """Edit a goal, or mark it achieved or discontinued. Needs the plan's `expected_version`."""
    caregiver = _caregiver(db, current_user)
    goal = _goal_access(db, caregiver, goal_id)
    _nurse(db, caregiver)
    plan_id = goal.plan_id
    try:
        return _goal_result(*svc.update_goal(db, caregiver, goal, body))
    except svc.VersionConflict as exc:
        raise _conflict(db, caregiver, plan_id, exc)
    except svc.CarePlanError as exc:
        raise _refuse(exc)


@router.delete("/care-plan-goals/{goal_id}", response_model=GoalWriteResult, responses={**NO_ACCESS, **NURSE_ONLY})
def remove_goal(goal_id: UUID, expected_version: int = Query(ge=1), current_user: User = Depends(get_current_caregiver),
                db: Session = Depends(get_db)) -> Any:
    """Remove a goal: it is marked `discontinued` and kept. Needs the plan's `expected_version` as a query parameter."""
    caregiver = _caregiver(db, current_user)
    goal = _goal_access(db, caregiver, goal_id)
    _nurse(db, caregiver)
    plan_id = goal.plan_id
    try:
        return _goal_result(*svc.remove_goal(db, caregiver, goal, expected_version))
    except svc.VersionConflict as exc:
        raise _conflict(db, caregiver, plan_id, exc)
    except svc.CarePlanError as exc:
        raise _refuse(exc)
