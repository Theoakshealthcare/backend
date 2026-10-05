from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.db.models.push_token import PushToken
from app.db.models.user import User
from app.schemas.devices import PushTokenDelete, PushTokenRegister, PushTokenResponse

router = APIRouter()


@router.post("/push-token", response_model=PushTokenResponse)
def register_push_token(
    body: PushTokenRegister,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Any:
    """Register this device's Expo push token, or refresh it (call after every login and whenever
    the token changes). A token already registered to another account moves to the caller."""
    now = datetime.utcnow()
    stmt = insert(PushToken).values(
        user_id=current_user.id, token=body.token, platform=body.platform, created_at=now, last_seen_at=now,
    )
    # Atomic upsert: two concurrent registrations of the same token cannot collide on the unique key.
    stmt = stmt.on_conflict_do_update(
        index_elements=[PushToken.token],
        set_={"user_id": current_user.id, "platform": body.platform, "last_seen_at": now},
    )
    db.execute(stmt)
    db.commit()
    return PushTokenResponse(token=body.token, platform=body.platform)


@router.delete("/push-token", status_code=status.HTTP_204_NO_CONTENT)
def delete_push_token(
    body: PushTokenDelete,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Forget this device's token (call on logout). Idempotent, and only ever removes the caller's
    own token: someone else's token gets the same 204 and is left alone."""
    db.query(PushToken).filter(
        PushToken.token == body.token, PushToken.user_id == current_user.id
    ).delete(synchronize_session=False)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
