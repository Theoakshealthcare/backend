"""Admin workflow for caregiver onboarding: decisions, skills, approve/reject/suspend.

Every method locks the caregiver row (SELECT ... FOR UPDATE), checks the state, changes it,
writes the audit row and commits once, so two admins can't both win a decision and an audit
row always matches a committed change.
"""

from datetime import date, datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models.audit import AdminAuditLog
from app.db.models.booking import Booking, BookingStatus
from app.db.models.caregiver import Caregiver, CaregiverDocument, CaregiverSkill
from app.services.audit import AuditService

PROFILE_STATUSES = (
    "incomplete", "under_review", "changes_requested", "approved", "rejected", "suspended",
)


class CaregiverNotFound(Exception):
    pass


class DocumentNotFound(Exception):
    pass


class InvalidInput(Exception):
    """A semantically invalid request (maps to 422)."""


class StateConflict(Exception):
    """The caregiver is not in a state that allows the action (maps to 409)."""

    def __init__(self, message: str, unmet: Optional[List[Dict[str, str]]] = None):
        super().__init__(message)
        self.message = message
        self.unmet = unmet or []


def _unmet(code: str, message: str) -> Dict[str, str]:
    return {"code": code, "message": message}


def _snapshot(cg: Caregiver) -> Dict[str, Any]:
    return {
        "profile_status": cg.profile_status,
        "identity_verification_status": cg.identity_verification_status,
        "background_check_status": cg.background_check_status,
        "approved_by": cg.approved_by,
        "approved_at": cg.approved_at,
        "suspended_at": cg.suspended_at,
        "suspension_reason": cg.suspension_reason,
        "rejection_reason": cg.rejection_reason,
        "changes_requested": cg.changes_requested,
    }


class CaregiverApprovalService:
    def __init__(self, db: Session):
        self.db = db
        self.audit = AuditService(db)

    # --- reads ---------------------------------------------------------------

    def list_applications(self, profile_status: str, limit: int, offset: int) -> Dict[str, Any]:
        doc_counts = (
            self.db.query(CaregiverDocument.caregiver_id, func.count(CaregiverDocument.id).label("n"))
            .group_by(CaregiverDocument.caregiver_id)
            .subquery()
        )
        total = self.db.query(Caregiver).filter(Caregiver.profile_status == profile_status).count()
        submitted = func.coalesce(Caregiver.consent_date, Caregiver.created_at)
        rows = (
            self.db.query(Caregiver, func.coalesce(doc_counts.c.n, 0))
            .outerjoin(doc_counts, doc_counts.c.caregiver_id == Caregiver.id)
            .filter(Caregiver.profile_status == profile_status)
            .order_by(submitted.asc(), Caregiver.id.asc())
            .limit(limit)
            .offset(offset)
            .all()
        )
        now = datetime.utcnow()
        items = []
        for cg, count in rows:
            when = cg.consent_date or cg.created_at
            items.append({
                "id": cg.id,
                "full_name": cg.full_name,
                "state": cg.state,
                "specialization": cg.specialization,
                "years_of_experience": cg.years_of_experience,
                "submitted_at": when,
                "age_in_days": (now - when).days if when else None,
                "document_count": count,
                "identity_verification_status": cg.identity_verification_status,
                "background_check_status": cg.background_check_status,
                "profile_status": cg.profile_status,
            })
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def get_detail(self, caregiver_id: UUID, admin_user_id: UUID, request: Request) -> Dict[str, Any]:
        cg = self.db.get(Caregiver, caregiver_id)
        if cg is None:
            raise CaregiverNotFound()
        docs = (
            self.db.query(CaregiverDocument)
            .filter(CaregiverDocument.caregiver_id == cg.id)
            .order_by(CaregiverDocument.uploaded_at)
            .all()
        )
        skills = (
            self.db.query(CaregiverSkill)
            .filter(CaregiverSkill.caregiver_id == cg.id)
            .order_by(CaregiverSkill.service_type, CaregiverSkill.sub_specialty)
            .all()
        )
        doc_ids = [str(d.id) for d in docs]
        history = (
            self.db.query(AdminAuditLog)
            .filter(
                ((AdminAuditLog.entity_type == "caregiver") & (AdminAuditLog.entity_id == str(cg.id)))
                | ((AdminAuditLog.entity_type == "caregiver_document") & AdminAuditLog.entity_id.in_(doc_ids))
            )
            .order_by(AdminAuditLog.created_at.desc())
            .limit(100)
            .all()
        )
        detail = {
            "profile": cg,
            "consents": {
                "terms_accepted": cg.terms_accepted,
                "privacy_policy_accepted": cg.privacy_policy_accepted,
                "background_check_consent": cg.background_check_consent,
                "consent_date": cg.consent_date,
            },
            "documents": [
                {
                    "id": d.id,
                    "document_type": d.document_type,
                    "document_name": d.document_name,
                    "mime_type": d.mime_type,
                    "file_size": d.file_size,
                    "verification_status": d.verification_status,
                    "rejection_reason": d.rejection_reason,
                    "expires_on": d.expires_on,
                    "uploaded_at": d.uploaded_at,
                    "verified_at": d.verified_at,
                    "reviewed_by": d.reviewed_by,
                    # the existing authorized download route; no second file-serving path
                    "download_url": f"/api/v1/caregiver/documents/{d.id}/file",
                }
                for d in docs
            ],
            "identity": {
                "status": cg.identity_verification_status,
                "document_verification_result": cg.document_verification_result,
                "face_verification_status": cg.face_verification_status,
                "face_verification_result": cg.face_verification_result,
            },
            "background_check": {
                "status": cg.background_check_status,
                "evidence": cg.background_check_evidence,
                "decided_by": cg.background_check_decided_by,
                "decided_at": cg.background_check_decided_at,
            },
            "decision": {
                "approved_by": cg.approved_by,
                "approved_at": cg.approved_at,
                "suspended_at": cg.suspended_at,
                "suspension_reason": cg.suspension_reason,
                "rejection_reason": cg.rejection_reason,
                "changes_requested": cg.changes_requested,
            },
            "skills": [{"service_type": s.service_type, "sub_specialty": s.sub_specialty} for s in skills],
            "audit_history": [
                {
                    "id": h.id, "action": h.action, "admin_user_id": h.admin_user_id,
                    "entity_type": h.entity_type, "entity_id": h.entity_id,
                    "reason": h.reason, "created_at": h.created_at,
                }
                for h in history
            ],
        }
        # Viewing identity data and documents is itself an auditable read (AD-FR-85).
        self.audit.record(admin_user_id=admin_user_id, action="caregiver.view",
                          entity_type="caregiver", entity_id=cg.id, request=request)
        self.db.commit()
        return detail

    # --- helpers -------------------------------------------------------------

    def _lock(self, caregiver_id: UUID) -> Caregiver:
        cg = (
            self.db.query(Caregiver)
            .filter(Caregiver.id == caregiver_id)
            .with_for_update()
            .populate_existing()
            .first()
        )
        if cg is None:
            raise CaregiverNotFound()
        return cg

    def _finish(self, cg: Caregiver) -> Caregiver:
        cg.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(cg)
        return cg

    # --- decisions on evidence -------------------------------------------------

    def decide_document(
        self, doc_id: UUID, admin_user_id: UUID, decision: str, reason: Optional[str],
        expires_on: Optional[date], request: Request,
    ) -> tuple[CaregiverDocument, Caregiver, Optional[List[UUID]]]:
        doc = self.db.get(CaregiverDocument, doc_id)
        if doc is None:
            raise DocumentNotFound()
        cg = self._lock(doc.caregiver_id)  # serialise with approve's document checks
        self.db.refresh(doc)
        before = {
            "verification_status": doc.verification_status, "rejection_reason": doc.rejection_reason,
            "expires_on": doc.expires_on, "verified_at": doc.verified_at, "reviewed_by": doc.reviewed_by,
        }
        doc.verification_status = decision
        doc.reviewed_by = admin_user_id
        if decision == "verified":
            doc.verified_at = datetime.utcnow()
            doc.rejection_reason = None
            doc.expires_on = expires_on
        else:
            doc.verified_at = None
            doc.rejection_reason = reason
        self.audit.record(
            admin_user_id=admin_user_id, action=f"caregiver_document.{decision}",
            entity_type="caregiver_document", entity_id=doc.id,
            before=before,
            after={
                "caregiver_id": doc.caregiver_id, "verification_status": doc.verification_status,
                "rejection_reason": doc.rejection_reason, "expires_on": doc.expires_on,
                "verified_at": doc.verified_at, "reviewed_by": doc.reviewed_by,
            },
            reason=reason, request=request,
        )
        affected = None
        if decision == "rejected" and cg.profile_status == "approved":
            self.db.flush()
            if self.unmet_for_approval(cg, check_state=False):
                affected = self._auto_suspend_if_unqualified(cg, admin_user_id, "licence rejected", request)
        cg.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(doc)
        self.db.refresh(cg)
        return doc, cg, affected

    def decide_identity(self, caregiver_id: UUID, admin_user_id: UUID, decision: str, notes: str,
                        request: Request) -> tuple[Caregiver, Optional[List[UUID]]]:
        cg = self._lock(caregiver_id)
        before = _snapshot(cg)
        cg.identity_verification_status = decision
        self.audit.record(admin_user_id=admin_user_id, action="caregiver.identity_decision",
                          entity_type="caregiver", entity_id=cg.id, before=before,
                          after=_snapshot(cg), reason=notes, request=request)
        affected = None
        if decision == "failed":
            affected = self._auto_suspend_if_unqualified(cg, admin_user_id, "identity failed", request)
        return self._finish(cg), affected

    def decide_background_check(
        self, caregiver_id: UUID, admin_user_id: UUID, decision: str, notes: str,
        provider_reference: Optional[str], document_id: Optional[UUID], request: Request,
    ) -> tuple[Caregiver, Optional[List[UUID]]]:
        cg = self._lock(caregiver_id)
        if document_id is not None:
            doc = self.db.get(CaregiverDocument, document_id)
            if doc is None or doc.caregiver_id != cg.id:
                raise InvalidInput("document_id does not belong to this caregiver")
        before = _snapshot(cg)
        cg.background_check_status = decision
        cg.background_check_evidence = {
            "notes": notes,
            "provider_reference": provider_reference,
            "document_id": str(document_id) if document_id else None,
        }
        cg.background_check_decided_by = admin_user_id
        cg.background_check_decided_at = datetime.utcnow()
        self.audit.record(admin_user_id=admin_user_id, action="caregiver.background_check_decision",
                          entity_type="caregiver", entity_id=cg.id, before=before,
                          after={**_snapshot(cg), "evidence": cg.background_check_evidence},
                          reason=notes, request=request)
        affected = None
        if decision == "failed":
            affected = self._auto_suspend_if_unqualified(
                cg, admin_user_id, "background check failed", request)
        return self._finish(cg), affected

    def set_skills(self, caregiver_id: UUID, admin_user_id: UUID, skills: List[Dict[str, Optional[str]]],
                   request: Request) -> List[CaregiverSkill]:
        cg = self._lock(caregiver_id)
        seen = set()
        for s in skills:
            key = (s["service_type"], s.get("sub_specialty") or "")
            if key in seen:
                raise InvalidInput(f"duplicate skill: {s['service_type']} / {s.get('sub_specialty')}")
            seen.add(key)
        existing = self.db.query(CaregiverSkill).filter(CaregiverSkill.caregiver_id == cg.id).all()
        before = [{"service_type": e.service_type, "sub_specialty": e.sub_specialty} for e in existing]
        for e in existing:
            self.db.delete(e)
        self.db.flush()
        new = [
            CaregiverSkill(caregiver_id=cg.id, service_type=s["service_type"],
                           sub_specialty=s.get("sub_specialty") or None, created_by_admin_id=admin_user_id)
            for s in skills
        ]
        self.db.add_all(new)
        self.audit.record(
            admin_user_id=admin_user_id, action="caregiver.skills_set", entity_type="caregiver",
            entity_id=cg.id, before={"skills": before},
            after={"skills": [{"service_type": n.service_type, "sub_specialty": n.sub_specialty} for n in new]},
            request=request,
        )
        cg.updated_at = datetime.utcnow()
        self.db.commit()
        return new

    # --- lifecycle -------------------------------------------------------------

    def unmet_for_approval(self, cg: Caregiver, check_state: bool = True) -> List[Dict[str, str]]:
        unmet: List[Dict[str, str]] = []
        if check_state and cg.profile_status != "under_review":
            unmet.append(_unmet("INVALID_STATE", f"Application is '{cg.profile_status}', not 'under_review'"))
        licences = (
            self.db.query(CaregiverDocument)
            .filter(CaregiverDocument.caregiver_id == cg.id, CaregiverDocument.document_type == "license")
            .all()
        )
        if not any(d.verification_status == "verified" for d in licences):
            unmet.append(_unmet("LICENSE_NOT_VERIFIED", "At least one licence document must be verified"))
        if any(d.verification_status == "pending" for d in licences):
            unmet.append(_unmet("LICENSE_PENDING", "A licence document is still pending review"))
        if cg.identity_verification_status != "verified":
            unmet.append(_unmet("IDENTITY_NOT_VERIFIED", "Identity must be verified"))
        if cg.background_check_status != "cleared":
            unmet.append(_unmet("BACKGROUND_CHECK_NOT_CLEARED", "The background check must be cleared"))
        has_skill = self.db.query(CaregiverSkill.id).filter(CaregiverSkill.caregiver_id == cg.id).first()
        if not has_skill:
            unmet.append(_unmet("NO_SKILLS", "At least one service skill must be set"))
        if not (cg.terms_accepted and cg.privacy_policy_accepted and cg.background_check_consent):
            unmet.append(_unmet(
                "CONSENTS_NOT_ACCEPTED",
                "Terms, privacy policy and background-check consent must all be accepted",
            ))
        return unmet

    def approve(self, caregiver_id: UUID, admin_user_id: UUID, request: Request) -> Caregiver:
        cg = self._lock(caregiver_id)
        unmet = self.unmet_for_approval(cg)
        if unmet:
            raise StateConflict("Cannot approve: preconditions not met", unmet)
        before = _snapshot(cg)
        cg.profile_status = "approved"
        cg.approved_by = admin_user_id
        cg.approved_at = datetime.utcnow()
        cg.rejection_reason = None
        cg.changes_requested = None
        self.audit.record(admin_user_id=admin_user_id, action="caregiver.approve", entity_type="caregiver",
                          entity_id=cg.id, before=before, after=_snapshot(cg), request=request)
        return self._finish(cg)

    def reject(self, caregiver_id: UUID, admin_user_id: UUID, reason: str, request: Request) -> Caregiver:
        cg = self._lock(caregiver_id)
        if cg.profile_status not in ("under_review", "changes_requested"):
            raise StateConflict(f"Cannot reject an application that is '{cg.profile_status}'")
        before = _snapshot(cg)
        cg.profile_status = "rejected"
        cg.rejection_reason = reason
        cg.changes_requested = None
        self.audit.record(admin_user_id=admin_user_id, action="caregiver.reject", entity_type="caregiver",
                          entity_id=cg.id, before=before, after=_snapshot(cg), reason=reason, request=request)
        return self._finish(cg)

    def request_changes(self, caregiver_id: UUID, admin_user_id: UUID, items: List[str], note: str,
                        request: Request) -> Caregiver:
        cg = self._lock(caregiver_id)
        if cg.profile_status != "under_review":
            raise StateConflict(f"Cannot request changes on an application that is '{cg.profile_status}'")
        before = _snapshot(cg)
        cg.profile_status = "changes_requested"
        cg.changes_requested = {"items": items, "note": note}
        self.audit.record(admin_user_id=admin_user_id, action="caregiver.request_changes",
                          entity_type="caregiver", entity_id=cg.id, before=before, after=_snapshot(cg),
                          reason=note, request=request)
        return self._finish(cg)

    def _apply_suspend(self, cg: Caregiver, admin_user_id: UUID, reason: str, request: Request,
                       action: str = "caregiver.suspend") -> List[UUID]:
        """Suspend an approved caregiver inside the caller's transaction (no commit)."""
        before = _snapshot(cg)
        cg.profile_status = "suspended"
        cg.suspended_at = datetime.utcnow()
        cg.suspension_reason = reason
        # Upcoming accepted bookings need reassignment by ops. There is no ops queue yet, so
        # they are listed in the response and the audit row.
        affected = [
            row.id for row in self.db.query(Booking.id)
            .filter(Booking.caregiver_id == cg.id, Booking.status == BookingStatus.ACCEPTED,
                    Booking.scheduled_date > datetime.utcnow())
            .order_by(Booking.scheduled_date).all()
        ]
        self.audit.record(admin_user_id=admin_user_id, action=action, entity_type="caregiver",
                          entity_id=cg.id, before=before,
                          after={**_snapshot(cg), "affected_bookings": affected}, reason=reason,
                          request=request)
        return affected

    def _auto_suspend_if_unqualified(self, cg: Caregiver, admin_user_id: UUID, trigger: str,
                                     request: Request) -> Optional[List[UUID]]:
        """A negative decision on an approved caregiver must not leave them bookable."""
        if cg.profile_status != "approved":
            return None
        return self._apply_suspend(cg, admin_user_id, f"{trigger} after approval", request,
                                   action="caregiver.auto_suspend")

    def suspend(self, caregiver_id: UUID, admin_user_id: UUID, reason: str,
                request: Request) -> tuple[Caregiver, List[UUID]]:
        cg = self._lock(caregiver_id)
        if cg.profile_status != "approved":
            raise StateConflict(f"Cannot suspend a caregiver that is '{cg.profile_status}'")
        affected = self._apply_suspend(cg, admin_user_id, reason, request)
        return self._finish(cg), affected

    def reinstate(self, caregiver_id: UUID, admin_user_id: UUID, request: Request) -> Caregiver:
        cg = self._lock(caregiver_id)
        if cg.profile_status != "suspended":
            raise StateConflict(f"Cannot reinstate a caregiver that is '{cg.profile_status}'")
        unmet = self.unmet_for_approval(cg, check_state=False)
        if unmet:
            raise StateConflict("Cannot reinstate: preconditions not met", unmet)
        before = _snapshot(cg)
        cg.profile_status = "approved"
        cg.suspended_at = None
        cg.suspension_reason = None
        self.audit.record(admin_user_id=admin_user_id, action="caregiver.reinstate", entity_type="caregiver",
                          entity_id=cg.id, before=before, after=_snapshot(cg), request=request)
        return self._finish(cg)
