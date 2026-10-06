"""Send a message and write down its metadata (never its body)."""

from datetime import datetime
from typing import Optional

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.channels.whatsapp.messages import OutboundMessage, TemplateMessage
from app.channels.whatsapp.meta_client import MetaCloudClient, SendResult
from app.db.models.whatsapp import WhatsAppContact, WhatsAppMessage


def send_and_record(db: Session, client: MetaCloudClient, contact: WhatsAppContact,
                    message: OutboundMessage) -> SendResult:
    result = client.send(contact.wa_id, message)
    record_outbound(db, contact.id, message, result)
    return result


def record_outbound(db: Session, contact_id, message: OutboundMessage, result: SendResult) -> None:
    template_name: Optional[str] = message.name if isinstance(message, TemplateMessage) else None
    category: Optional[str] = message.category if isinstance(message, TemplateMessage) else None
    now = datetime.utcnow()
    values = dict(contact_id=contact_id, direction="outbound", type=message.kind, template_name=template_name,
                  category=category, created_at=now, updated_at=now)
    if result.ok:
        # Upsert: a delivery callback may already have created the row (see ingest._apply_status). Its
        # status is further along than "sent", so only the descriptive columns are filled in.
        stmt = insert(WhatsAppMessage).values(**values, meta_message_id=result.meta_message_id, status="sent")
        stmt = stmt.on_conflict_do_update(
            index_elements=["meta_message_id"],
            set_={"type": stmt.excluded.type, "template_name": stmt.excluded.template_name,
                  "category": stmt.excluded.category, "updated_at": now},
        )
    else:
        stmt = insert(WhatsAppMessage).values(**values, meta_message_id=None, status="failed",
                                              error_code=result.error_code)
    db.execute(stmt)
    db.commit()
