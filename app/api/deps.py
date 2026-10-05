from typing import Generator, Optional
from fastapi import Depends, HTTPException, WebSocket, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import ALGORITHM
from app.db.session import SessionLocal
from app.db.models.user import User, Admin
from app.services.user import UserService

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login")


def get_db() -> Generator:
    """Get database session"""
    try:
        db = SessionLocal()
        yield db
    finally:
        db.close()


async def get_current_user(
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme)
) -> User:
    """Get current authenticated user"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "access":
            # refresh tokens (and untyped tokens) must never authenticate API calls
            raise credentials_exception
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    user_service = UserService(db)
    user = user_service.get_user(user_id)
    if user is None:
        raise credentials_exception
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user"
        )

    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Get current active user (get_current_user already refuses inactive users)."""
    return current_user


async def get_current_caregiver(
    current_user: User = Depends(get_current_active_user),
) -> User:
    """Get current caregiver user"""
    if current_user.user_type != "caregiver":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Caregiver account required."
        )
    return current_user


async def get_current_patient(
    current_user: User = Depends(get_current_active_user),
) -> User:
    """Get current patient user"""
    if current_user.user_type != "patient":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Patient account required."
        )
    return current_user


async def get_current_admin(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> User:
    admin = db.query(Admin).filter(Admin.user_id == current_user.id).first()
    if not admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied. Admin account required."
        )
    return current_user


def user_is_admin(db: Session, user: User) -> bool:
    """True when the user has an Admin row (admins are not a user_type today)."""
    return db.query(Admin).filter(Admin.user_id == user.id).first() is not None


WS_CLOSE_UNAUTHENTICATED = 4401
WS_CLOSE_FORBIDDEN = 4403


def _websocket_token(websocket: WebSocket) -> tuple[Optional[str], Optional[str]]:
    """Find the JWT on a WebSocket handshake.

    Browsers and mobile clients can't set an Authorization header on a WebSocket, so the token
    comes either in the Sec-WebSocket-Protocol header ("bearer, <jwt>"; the server echoes back
    "bearer") or as a `token` query parameter. Returns (token, subprotocol_to_accept).
    """
    protocols = [p.strip() for p in websocket.scope.get("subprotocols", [])]
    if "bearer" in protocols:
        rest = [p for p in protocols if p != "bearer"]
        if rest:
            return rest[0], "bearer"
    return websocket.query_params.get("token"), None


async def authenticate_websocket(
    websocket: WebSocket, db: Session, required_user_type: Optional[str] = None
) -> Optional[tuple[User, Optional[str]]]:
    """Authenticate a WebSocket before it is accepted.

    On failure the socket is accepted and then closed with an application code (4401 bad or
    missing token or inactive user, 4403 wrong role), and None is returned. It must be accepted
    first: closing before accept() makes uvicorn answer the handshake with a plain HTTP 403,
    so real clients would never see the close code. Nothing is sent and no connection is
    registered. On success returns (user, subprotocol_to_accept).
    """
    token, subprotocol = _websocket_token(websocket)

    async def reject(code: int, reason: str) -> None:
        await websocket.accept(subprotocol=subprotocol)
        await websocket.close(code=code, reason=reason)

    if not token:
        await reject(WS_CLOSE_UNAUTHENTICATED, "Authentication required")
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        if payload.get("type") != "access" or not user_id:
            raise JWTError("not an access token")
        user = UserService(db).get_user(user_id)
    except (JWTError, ValueError):
        await reject(WS_CLOSE_UNAUTHENTICATED, "Invalid token")
        return None
    if user is None or not user.is_active:
        await reject(WS_CLOSE_UNAUTHENTICATED, "Invalid token")
        return None
    if required_user_type and user.user_type != required_user_type:
        await reject(WS_CLOSE_FORBIDDEN, "Wrong account type")
        return None
    return user, subprotocol
