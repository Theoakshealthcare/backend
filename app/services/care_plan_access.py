"""Who may see or change a care plan, and whose plan it is.

Every caregiver rule here is the client profile's rule (``caregiver_clients.build_client_profile``) unchanged: the booking
is the caller's, status ``accepted``, ``en_route`` or ``in_progress``; a caregiver who is not approved only for
``in_progress``; the patient on the booking is active. Anything else raises ``PlanNotAvailable``, which the endpoints
answer with one 404, so a response never says whether a booking, a plan or a client exists. A caregiver loses access the
moment the booking completes, is cancelled or they drop out.

The *subject* is whose plan it is (design doc section 3.2): the patient for a visit booked for oneself, the care recipient
for a visit a booker made for somebody else, and nobody for an app booking for someone else (no identifiable client).

``resolve_family_access`` is groundwork for a later family view (section 4.4): written and tested, called by no route.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.db.models.booking import Booking, BookingStatus
from app.db.models.care import CareRecipient, CareRelationship
from app.db.models.care_plan import CarePlan, CarePlanGoal, CarePlanTask
from app.db.models.caregiver import Caregiver, CaregiverSkill
from app.db.models.consent import Consent
from app.db.models.patient import Patient
from app.db.models.user import User
from app.services.caregiver_clients import LIVE_STATUSES

# Who may write a plan (decision D1; assumption A5). Widening to doctors or physiotherapists is this one line.
CARE_PLAN_AUTHOR_SERVICE_TYPES = ("nurse",)
FAMILY_ROLES = ("family", "guardian")


class PlanNotAvailable(Exception):
    """No access of any kind. The endpoints answer one 404, "Care plan not found"."""


class NotANurse(Exception):
    """The caller is a caregiver with access but not an author (403 ``not_a_nurse``)."""


@dataclass(frozen=True)
class Subject:
    """Whose plan it is. Exactly one of the two keys is the plan's subject key."""
    kind: str  # "patient" or "recipient"
    id: UUID
    full_name: str
    date_of_birth: Optional[date]
    gender: Optional[str]

    @property
    def key(self) -> Tuple[str, UUID]:
        return (self.kind, self.id)


@dataclass
class PlanAccess:
    """What a caregiver may do through one live booking."""
    booking: Booking
    patient: Patient  # the patient row on the booking: the client for "myself", otherwise the booker
    subject: Optional[Subject]  # None: an app booking for someone else, so no plan is possible

    def require_subject(self) -> Subject:
        if self.subject is None:
            raise PlanNotAvailable()
        return self.subject


def is_self_booking(booking: Booking) -> bool:
    # The patient app sends "Myself", the WhatsApp channel "myself": same thing.
    return (booking.booking_for or "").strip().lower() == "myself"


def is_care_plan_author(db: Session, caregiver: Caregiver) -> bool:
    """The nurse test: an admin-assigned skill row. The self-declared ``specialization`` text is never used."""
    return db.query(CaregiverSkill.id).filter(
        CaregiverSkill.caregiver_id == caregiver.id,
        CaregiverSkill.service_type.in_(CARE_PLAN_AUTHOR_SERVICE_TYPES),
    ).first() is not None


def require_author(db: Session, caregiver: Caregiver) -> None:
    if not is_care_plan_author(db, caregiver):
        raise NotANurse()


def _subject_of(booking: Booking, patient: Patient, recipients: Dict[UUID, CareRecipient]) -> Optional[Subject]:
    if is_self_booking(booking):
        return Subject("patient", patient.id, patient.full_name, patient.date_of_birth, patient.gender)
    recipient = recipients.get(booking.care_recipient_id) if booking.care_recipient_id else None
    if recipient is None:
        return None
    return Subject("recipient", recipient.id, recipient.full_name, recipient.date_of_birth, recipient.gender)


def _recipients_for(db: Session, bookings: List[Booking]) -> Dict[UUID, CareRecipient]:
    ids = {b.care_recipient_id for b in bookings if b.care_recipient_id and not is_self_booking(b)}
    if not ids:
        return {}
    return {r.id: r for r in db.query(CareRecipient).filter(CareRecipient.id.in_(ids))}


def _live_query(db: Session, caregiver: Caregiver):
    """The caregiver's live bookings (with the booking's patient), nearest first: a visit under way, then by date."""
    query = (
        db.query(Booking, Patient)
        .join(Patient, Patient.id == Booking.patient_id)
        .filter(Booking.caregiver_id == caregiver.id, Booking.status.in_(LIVE_STATUSES), Patient.is_active.is_(True))
        .order_by(case((Booking.status == BookingStatus.IN_PROGRESS, 0), else_=1), Booking.scheduled_date)
    )
    if caregiver.profile_status != "approved":
        query = query.filter(Booking.status == BookingStatus.IN_PROGRESS)
    return query


def resolve_plan_access(db: Session, caregiver: Caregiver, booking_id: UUID) -> PlanAccess:
    """Access through one booking id (the routes keyed by booking). Raises ``PlanNotAvailable`` unless the booking is the
    caller's, live, and its patient is active. An unidentified client is *not* a refusal here: ``subject`` is None."""
    row = _live_query(db, caregiver).filter(Booking.id == booking_id).first()
    if row is None:
        raise PlanNotAvailable()
    booking, patient = row
    return PlanAccess(booking, patient, _subject_of(booking, patient, _recipients_for(db, [booking])))


def resolve_plan_access_for_plan(db: Session, caregiver: Caregiver, plan: CarePlan) -> PlanAccess:
    """Access to a plan by its id: the plan's subject must be the subject of one of the caller's live bookings (the nearest
    one is used). Raises ``PlanNotAvailable`` otherwise."""
    query = _live_query(db, caregiver)
    myself = func.lower(func.trim(Booking.booking_for)) == "myself"
    if plan.patient_id is not None:
        query = query.filter(Booking.patient_id == plan.patient_id, myself)
    else:
        query = query.filter(Booking.care_recipient_id == plan.care_recipient_id, ~myself)
    row = query.first()
    if row is None:
        raise PlanNotAvailable()
    booking, patient = row
    subject = _subject_of(booking, patient, _recipients_for(db, [booking]))
    if subject is None or subject.id != (plan.patient_id or plan.care_recipient_id):
        raise PlanNotAvailable()
    return PlanAccess(booking, patient, subject)


def accessible_subjects(db: Session, caregiver: Caregiver) -> Dict[Tuple[str, UUID], PlanAccess]:
    """Every identifiable client of the caller's live bookings, mapped to the nearest booking that gives access."""
    rows = _live_query(db, caregiver).all()
    recipients = _recipients_for(db, [b for b, _ in rows])
    found: Dict[Tuple[str, UUID], PlanAccess] = {}
    for booking, patient in rows:
        subject = _subject_of(booking, patient, recipients)
        if subject is not None and subject.key not in found:
            found[subject.key] = PlanAccess(booking, patient, subject)
    return found


# --- family-view groundwork (section 4.4): tested, not exposed by any v1 route -------------------------------------

@dataclass
class FamilyAccess:
    """What one relative may see of one plan: only the items flagged ``family_visible``. Never the header (diagnosis,
    risk), allergies, instructions, who wrote or ticked anything, the event history or progress."""
    plan_id: UUID
    goals: List[CarePlanGoal] = field(default_factory=list)
    tasks: List[CarePlanTask] = field(default_factory=list)


def _latest_family_consent_granted(db: Session, recipient_id: UUID) -> bool:
    latest = (
        db.query(Consent.decision)
        .filter(Consent.subject_recipient_id == recipient_id, Consent.type == "share_with_family")
        .order_by(Consent.created_at.desc(), Consent.id.desc())
        .first()
    )
    return latest is not None and latest[0] == "granted"


def resolve_family_access(db: Session, user: User, plan_id: UUID) -> FamilyAccess:
    """Allow a relative only when all hold: a ``care_relationships`` row of role family or guardian links them to the plan's
    care recipient and has ``clinical_access``; the latest ``share_with_family`` consent for that recipient is granted; the
    relative's account is active; and the plan is live (not archived, not erased). Plans keyed by ``patient_id`` are refused
    until the family view decides how an app-only patient is linked (assumption A12). One refusal for everything."""
    plan = db.get(CarePlan, plan_id)
    if (plan is None or plan.care_recipient_id is None or plan.status == "archived"
            or plan.diagnosis_encrypted is None or not user.is_active):
        raise PlanNotAvailable()
    linked = db.query(CareRelationship.id).filter(
        CareRelationship.contact_user_id == user.id,
        CareRelationship.recipient_id == plan.care_recipient_id,
        CareRelationship.role.in_(FAMILY_ROLES),
        CareRelationship.clinical_access.is_(True),
    ).first()
    if linked is None or not _latest_family_consent_granted(db, plan.care_recipient_id):
        raise PlanNotAvailable()
    goals = (db.query(CarePlanGoal).filter(CarePlanGoal.plan_id == plan.id, CarePlanGoal.family_visible.is_(True))
             .order_by(CarePlanGoal.sort_order).all())
    tasks = (db.query(CarePlanTask).filter(CarePlanTask.plan_id == plan.id, CarePlanTask.family_visible.is_(True),
                                           CarePlanTask.is_active.is_(True)).order_by(CarePlanTask.sort_order).all())
    return FamilyAccess(plan.id, goals, tasks)
