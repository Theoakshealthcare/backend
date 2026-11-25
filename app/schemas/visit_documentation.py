"""Caregiver own-documentation schemas (see app/services/visit_documentation.py)."""

from datetime import datetime
from typing import Annotated, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints

DocumentText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class DocumentIn(BaseModel):
    body: DocumentText = Field(description="The caregiver's own note for this visit. Private: never shown to the family.")


class DocumentOut(BaseModel):
    id: UUID
    booking_id: UUID
    plan_id: Optional[UUID] = Field(None, description="The client's active care plan when it was written; null if none.")
    body: str
    created_at: datetime


class DocumentList(BaseModel):
    items: List[DocumentOut] = Field(description="The caller's own entries for this visit, newest first.")
    can_add: bool = Field(description="false once the 2-hour window after the visit has closed.")
    closes_at: Optional[datetime] = Field(None, description="When adding closes (2 h after completion); null while the visit "
                                                            "is in progress.")
    readable_until: Optional[datetime] = Field(None, description="When the caregiver stops seeing their entries (24 h after "
                                                                 "the visit ended); null until the visit is completed.")


class AdminDocumentAuthor(BaseModel):
    id: UUID
    name: str


class AdminDocumentOut(DocumentOut):
    caregiver: AdminDocumentAuthor


class AdminDocumentList(BaseModel):
    items: List[AdminDocumentOut]
    total: int
    limit: int
    offset: int
