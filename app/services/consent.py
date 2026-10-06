"""Consent records (append-only; see app/db/models/consent.py).

The current answer for a subject and type is the latest row. ``record`` flushes but does not commit,
so a consent row commits or rolls back together with the conversation change it belongs to.
"""

from typing import Dict, Iterable, List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.consent import Consent

REQUIRED_TYPES = ("terms", "health_processing")


class ConsentService:
    def __init__(self, db: Session):
        self.db = db

    def current(self, *, contact_id: UUID) -> Dict[str, str]:
        """{consent type: latest decision} for a contact."""
        rows = (
            self.db.query(Consent.type, Consent.decision)
            .filter(Consent.contact_id == contact_id)
            .distinct(Consent.type)
            .order_by(Consent.type, Consent.created_at.desc())
            .all()
        )
        return {t: d for t, d in rows}

    def has_required_consent(self, *, contact_id: UUID) -> bool:
        current = self.current(contact_id=contact_id)
        return all(current.get(t) == "granted" for t in REQUIRED_TYPES)

    def record(
        self,
        *,
        types: Iterable[str],
        decision: str,
        channel: str,
        evidence_ref: Optional[str],
        contact_id: Optional[UUID] = None,
        subject_user_id: Optional[UUID] = None,
        subject_recipient_id: Optional[UUID] = None,
        granted_by_user_id: Optional[UUID] = None,
        policy_version: Optional[str] = None,
    ) -> List[Consent]:
        """Append one row per type. A withdrawal of something that was never granted, or a grant that
        already stands under the same policy version, adds nothing (no noise in the evidence trail)."""
        version = policy_version or settings.WHATSAPP_POLICY_VERSION
        standing = self.current(contact_id=contact_id) if contact_id else {}
        latest_version = self._latest_versions(contact_id) if contact_id else {}
        rows: List[Consent] = []
        for consent_type in types:
            was = standing.get(consent_type)
            if decision == "withdrawn" and was != "granted":
                continue
            if decision == "granted" and was == "granted" and latest_version.get(consent_type) == version:
                continue
            row = Consent(
                contact_id=contact_id, subject_user_id=subject_user_id,
                subject_recipient_id=subject_recipient_id, granted_by_user_id=granted_by_user_id,
                type=consent_type, decision=decision, policy_version=version, channel=channel,
                evidence_ref=evidence_ref,
            )
            self.db.add(row)
            rows.append(row)
        self.db.flush()
        return rows

    def _latest_versions(self, contact_id: UUID) -> Dict[str, str]:
        rows = (
            self.db.query(Consent.type, Consent.policy_version)
            .filter(Consent.contact_id == contact_id)
            .distinct(Consent.type)
            .order_by(Consent.type, Consent.created_at.desc())
            .all()
        )
        return {t: v for t, v in rows}
