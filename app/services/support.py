"""Human-handover tickets (``support_conversations``): the queue the Admin console inbox will read.

Rules
- One live handover per contact (a partial unique index backs this). A second request for the same
  contact reuses it; an emergency escalates it to urgent instead of opening another.
- A ``data_withdrawal`` task is a compliance record, separate from any handover.
- Resolving a handover hands the conversation back to the bot.
"""

import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.support import SupportConversation
from app.db.models.whatsapp import WhatsAppConversation
from app.services.consent import ConsentService

logger = logging.getLogger(__name__)

COMPLIANCE_REASON = "data_withdrawal"


class SupportService:
    def __init__(self, db: Session):
        self.db = db

    def _open_ticket(self, contact_id: UUID, compliance: bool) -> Optional[SupportConversation]:
        query = self.db.query(SupportConversation).filter(
            SupportConversation.contact_id == contact_id, SupportConversation.status != "resolved"
        )
        if compliance:
            query = query.filter(SupportConversation.reason == COMPLIANCE_REASON)
        else:
            query = query.filter(SupportConversation.reason != COMPLIANCE_REASON)
        return query.with_for_update().first()

    def open(self, *, contact_id: UUID, reason: str, priority: str = "normal") -> SupportConversation:
        compliance = reason == COMPLIANCE_REASON
        existing = self._open_ticket(contact_id, compliance)
        if existing is not None:
            return self._merge(existing, reason, priority)
        ticket = SupportConversation(contact_id=contact_id, reason=reason, priority=priority, status="open")
        try:
            with self.db.begin_nested():
                self.db.add(ticket)
        except IntegrityError:  # a concurrent worker opened it first: join theirs
            existing = self._open_ticket(contact_id, compliance)
            if existing is None:
                raise
            return self._merge(existing, reason, priority)
        if priority == "urgent":
            # Ops alert hook: this is the line to wire to paging/Sentry once alerting exists.
            logger.error("whatsapp URGENT handover opened: ticket=%s reason=%s", ticket.id, reason)
        return ticket

    def _merge(self, ticket: SupportConversation, reason: str, priority: str) -> SupportConversation:
        if priority == "urgent" and ticket.priority != "urgent":
            ticket.priority = "urgent"
            ticket.reason = reason
            logger.error("whatsapp ticket %s escalated to URGENT (%s)", ticket.id, reason)
            self.db.flush()
        return ticket

    def resolve(self, ticket_id: UUID, agent_user_id: UUID) -> SupportConversation:
        ticket = self.db.query(SupportConversation).filter_by(id=ticket_id).with_for_update().one()
        if ticket.status == "resolved":
            return ticket
        ticket.status = "resolved"
        ticket.resolved_by_user_id = agent_user_id
        ticket.resolved_at = datetime.utcnow()
        if ticket.reason != COMPLIANCE_REASON:
            conversation = (
                self.db.query(WhatsAppConversation)
                .filter_by(contact_id=ticket.contact_id).with_for_update().one()
            )
            if conversation.state in ("human", "emergency"):
                has_consent = ConsentService(self.db).has_required_consent(contact_id=ticket.contact_id)
                conversation.state = "idle" if has_consent else "consent_pending"
                conversation.automation_paused = False
                conversation.assigned_agent_id = None
                conversation.context = {**(conversation.context or {}), "misses": 0}
        self.db.flush()
        return ticket
