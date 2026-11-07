"""The app patient's notification feed. Every query is scoped to the signed-in user: someone else's notification is
indistinguishable from one that does not exist."""
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.api.deps import get_current_patient, get_db
from app.db.models.notification import Notification
from app.db.models.user import User
from app.schemas.patient_notifications import NotificationList, NotificationOut, ReadAllResult

router = APIRouter()


def _unread_count(db: Session, user_id: UUID) -> int:
    return db.query(func.count(Notification.id)).filter(
        Notification.user_id == user_id, Notification.read_at.is_(None)).scalar() or 0


@router.get("/notifications", response_model=NotificationList)
def list_notifications(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_patient),
    db: Session = Depends(get_db),
) -> NotificationList:
    """Newest first. ``unread_count`` covers the whole feed, not just this page."""
    base = db.query(Notification).filter(Notification.user_id == current_user.id)
    rows = base.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit).offset(offset).all()
    return NotificationList(
        items=[NotificationOut.model_validate(r) for r in rows], total=base.count(), limit=limit, offset=offset,
        unread_count=_unread_count(db, current_user.id))


@router.post("/notifications/read-all", response_model=ReadAllResult)
def mark_all_read(current_user: User = Depends(get_current_patient), db: Session = Depends(get_db)) -> ReadAllResult:
    """Mark every unread notification read. Idempotent: a second call marks nothing."""
    marked = db.execute(
        update(Notification)
        .where(Notification.user_id == current_user.id, Notification.read_at.is_(None))
        .values(read_at=datetime.utcnow())
    ).rowcount
    db.commit()
    return ReadAllResult(marked_read=marked, unread_count=_unread_count(db, current_user.id))


@router.post("/notifications/{notification_id}/read", response_model=NotificationOut)
def mark_read(
    notification_id: UUID,
    current_user: User = Depends(get_current_patient),
    db: Session = Depends(get_db),
) -> Notification:
    """Mark one notification read. Idempotent: reading it again keeps the original ``read_at``."""
    row = db.query(Notification).filter(
        Notification.id == notification_id, Notification.user_id == current_user.id).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found.")
    if row.read_at is None:
        row.read_at = datetime.utcnow()
        db.commit()
        db.refresh(row)
    return row
