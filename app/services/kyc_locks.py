"""The one place KYC code takes its row locks (K1 follow-up, decided in K3): the caregiver row FIRST, then its attempts (by id).

Erasure (``AccountErasureService``) locks user -> caregiver -> attempts, K2's ``start_session`` locks the caregiver, and the K3/K4
worker, the reconcile cron and expiry all go through ``lock_caregiver_then_attempt``. One order everywhere means two of them can
queue behind each other but never deadlock.
"""
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models.caregiver import Caregiver
from app.db.models.identity_verification import IdentityVerification


def lock_caregiver_then_attempt(db: Session, attempt_id: uuid.UUID) -> tuple[Optional[Caregiver], Optional[IdentityVerification]]:
    """Fresh, locked rows, or (None, None) if the attempt does not exist. ``caregiver_id`` never changes, so reading it before the
    locks is safe."""
    caregiver_id = db.query(IdentityVerification.caregiver_id).filter(IdentityVerification.id == attempt_id).scalar()
    if caregiver_id is None:
        return None, None
    caregiver = db.query(Caregiver).filter(Caregiver.id == caregiver_id).populate_existing().with_for_update().one()
    attempt = (db.query(IdentityVerification).filter(IdentityVerification.id == attempt_id)
               .populate_existing().with_for_update().one())
    return caregiver, attempt
