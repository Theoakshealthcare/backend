from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from app.db.models.booking import BookingStatus


class ChatLastMessage(BaseModel):
    """The newest message of a conversation, cut to a one-line preview. Never the full body."""
    text: str = Field(description="At most 140 characters; a longer message is cut and ends with an ellipsis.")
    sent_at: datetime
    from_me: bool = Field(description="True when the signed-in caregiver sent it.")


class CaregiverChatOut(BaseModel):
    """One conversation: the booking's chat with its patient. `booking_id` is what the chat routes take."""
    booking_id: UUID
    patient_name: str
    patient_photo_url: Optional[str] = Field(None, description="Absolute URL of the patient's profile photo; null when they "
                                                               "have not uploaded one.")
    service_type: str
    status: BookingStatus
    scheduled_date: datetime = Field(description="When the visit starts.")
    last_message: Optional[ChatLastMessage] = Field(None, description="Null while nobody has written yet.")


class CaregiverChatList(BaseModel):
    items: List[CaregiverChatOut]
    next_before: Optional[datetime] = Field(None, description="Pass as `before` for the next (older) page; null on the last.")
    next_before_id: Optional[UUID] = Field(None, description="Pass as `before_id` together with `before` (it breaks ties "
                                                              "between conversations with the same activity time); null on the last.")
