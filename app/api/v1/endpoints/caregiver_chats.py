"""The caregiver's conversation list. Mounted under /caregiver; the messages themselves stay on the booking chat routes."""

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_caregiver, get_db
from app.db.models.caregiver import Caregiver
from app.db.models.user import User
from app.schemas.caregiver_chat import CaregiverChatList
from app.services import caregiver_chats as chats

router = APIRouter()


@router.get("/chats", response_model=CaregiverChatList)
def list_chats(
    response: Response,
    before: Optional[datetime] = Query(None, description="Return conversations with older activity than this (the previous "
                                                         "page's `next_before`)."),
    before_id: Optional[UUID] = Query(None, description="With `before`: the previous page's `next_before_id`."),
    limit: int = Query(chats.DEFAULT_LIMIT, ge=1, le=chats.MAX_LIMIT),
    current_user: User = Depends(get_current_caregiver),
    db: Session = Depends(get_db),
) -> Any:
    """Your conversations, newest activity first: one per booking whose chat you may open (the bookings assigned to you).

    Each row carries the booking id for `GET /booking/chat/{booking_id}/messages`, the patient's name and photo URL (null when none), the service, the
    booking status, the visit start and the last message cut to 140 characters (null when nobody has written yet). There is
    no unread count: the chat does not record who has read what."""
    caregiver = db.query(Caregiver).filter(Caregiver.user_id == current_user.id).first()
    if caregiver is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Caregiver profile not found")
    if before is not None and before.tzinfo is not None:
        before = before.astimezone(timezone.utc).replace(tzinfo=None)
    response.headers["Cache-Control"] = "no-store"
    return chats.list_chats(db, caregiver, current_user.id, before, before_id, limit)
