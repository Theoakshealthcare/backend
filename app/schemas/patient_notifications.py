from datetime import datetime
from typing import Any, Dict, List
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class NotificationOut(BaseModel):
    id: UUID
    type: str
    title: str
    body: str
    data: Dict[str, Any]
    read_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class NotificationList(BaseModel):
    items: List[NotificationOut]
    total: int
    limit: int
    offset: int
    unread_count: int


class ReadAllResult(BaseModel):
    marked_read: int
    unread_count: int
