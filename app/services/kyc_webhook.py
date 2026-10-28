"""The Prembly webhook ledger: record a verified event once, or forget it when it could not be queued.

The Meta pattern (app/channels/whatsapp/ingest.py), not Paystack's: the per-webhook ``token`` is UNIQUE and inserted BEFORE the job
is enqueued, so a duplicate delivery is recognised and a failed enqueue is undone (``compensate``) so the provider's retry is
processed instead of being deduped away. Only events whose signature verified reach this module: an unsigned request never writes.

The body is NOT stored (design 8.2: it carries the registry record). The ledger keeps the token, the session id, a sanitised event
type and a hash of the body.
"""
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models.identity_verification import KycWebhookEvent
from app.services.kyc_seam import WebhookEvent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Ingested:
    job: dict
    is_new: bool  # False: the token was seen before but its event was never processed (see ``ingest``)


def ingest(db: Session, *, token: str, raw_body: bytes, event: WebhookEvent) -> Optional[Ingested]:
    """Record the event. None when this ``token`` was seen before AND already processed (a duplicate or a replay: answer 200).

    A duplicate whose earlier row is still unprocessed is returned with ``is_new=False``: the first delivery may have died between
    the insert and the enqueue (or its compensation failed), and Prembly's retry must not be deduped into silence. The endpoint
    enqueues it again under the same ARQ job id, so a job that is genuinely still queued just refuses the second one."""
    statement = (
        insert(KycWebhookEvent)
        .values(id=uuid.uuid4(), token=token, provider_session_id=event.session_id, event_type=event.event_type, signature_ok=True,
                body_sha256=hashlib.sha256(raw_body).hexdigest(), received_at=datetime.utcnow())
        .on_conflict_do_nothing(index_elements=["token"])
        .returning(KycWebhookEvent.id)
    )
    row = db.execute(statement).first()
    db.commit()
    is_new = row is not None
    event_id = row[0] if is_new else db.query(KycWebhookEvent.id).filter(
        KycWebhookEvent.token == token, KycWebhookEvent.processed_at.is_(None)).scalar()
    if event_id is None:
        return None
    return Ingested({"event_id": str(event_id), "session_id": event.session_id,
                     "oakly_ref": str(event.oakly_ref) if event.oakly_ref else None, "claim": event.claim}, is_new)


def compensate(db: Session, event_id: str) -> None:
    """The queue refused the job: forget the ledger row so the provider's retry is not deduped away."""
    db.rollback()
    db.query(KycWebhookEvent).filter(KycWebhookEvent.id == uuid.UUID(event_id), KycWebhookEvent.processed_at.is_(None)).delete(
        synchronize_session=False)
    db.commit()
