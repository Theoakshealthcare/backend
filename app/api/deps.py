from typing import Callable, Generator, Optional, Union
from uuid import UUID

from fastapi import Depends, HTTPException, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordBearer
from jose import jwt, JWTError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.admin_security import AdminTokenError, decode_admin_token
from app.core.config import settings
from app.core.security import ALGORITHM
from app.db.session import SessionLocal
from app.db.models.admin import AdminRefreshToken, AdminUser
from app.db.models.user import User
from app.services.user import UserService

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login")
# Admin routes use their own bearer scheme so the OpenAPI document (and the generated console
# client) shows that an admin token is a different credential from an app-user token.
admin_bearer_scheme = HTTPBearer(scheme_name="AdminBearer", auto_error=False)


def get_db() -> Generator:
    """Get database session"""
    try:
        db = SessionLocal()
        yield db
    finally:
        db.close()


def _user_from_token(db: Session, token: str) -> User:
    """Resolve an app-user access token. Admin tokens (aud="admin") fail here: python-jose refuses
    any token that carries an audience when none is expected."""
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


async def get_current_user(
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme)
) -> User:
    """Get current authenticated user"""
    return _user_from_token(db, token)


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


# --- admin console auth (AD-FR-81, AD-FR-82) -----------------------------------------------------


def _admin_unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _admin_from_token(db: Session, token: str) -> AdminUser:
    """Resolve an admin access token to an active admin whose session is still live."""
    try:
        payload = decode_admin_token(token, "access")
        admin_id, session_id = UUID(payload["sub"]), UUID(payload["sid"])
    except (AdminTokenError, KeyError, ValueError):
        raise _admin_unauthorized()
    admin = db.get(AdminUser, admin_id)
    if admin is None or admin.status != "active":
        raise _admin_unauthorized()
    # Logout, a disabled account, an MFA reset and refresh-token reuse all revoke the session's
    # refresh tokens; checking here makes that take effect immediately instead of in 15 minutes.
    live = db.scalar(
        select(AdminRefreshToken.id)
        .where(AdminRefreshToken.family_id == session_id, AdminRefreshToken.revoked_at.is_(None))
        .limit(1)
    )
    if live is None:
        raise _admin_unauthorized()
    return admin


def _forbidden(permissions: tuple) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Access denied. Missing permission: {', '.join(permissions)}",
    )


def get_current_admin_user(
    db: Session = Depends(get_db),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(admin_bearer_scheme),
) -> AdminUser:
    """Any signed-in admin, whatever their permissions (used by /admin/me)."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _admin_from_token(db, credentials.credentials)


def require_permission(*permissions: str) -> Callable[..., AdminUser]:
    """The one admin check (AD-FR-82). Use as ``admin = Depends(require_permission("x.y"))``.

    Needs a valid admin access token and every listed permission. A token that is missing or
    invalid is 401; a valid admin without the permission is 403.
    """
    if not permissions:
        raise ValueError("require_permission needs at least one permission")

    def dependency(admin: AdminUser = Depends(get_current_admin_user)) -> AdminUser:
        held = admin.permissions
        if not all(p in held for p in permissions):
            raise _forbidden(permissions)
        return admin

    dependency.required_permissions = permissions  # type: ignore[attr-defined]  # read by the permission-matrix test
    return dependency


def get_user_or_admin(permission: str) -> Callable[..., Union[User, AdminUser]]:
    """For the few routes shared by app users and the console (booking detail, document download,
    prescription reads): an app-user token, or an admin token holding ``permission``.

    The two token kinds are told apart by the ``aud`` claim and each is verified by its own
    decoder, so an admin token never authenticates as a user (or the reverse).
    """

    def dependency(
        db: Session = Depends(get_db), token: str = Depends(oauth2_scheme)
    ) -> Union[User, AdminUser]:
        try:
            claims = jwt.get_unverified_claims(token)
        except JWTError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if claims.get("aud") is not None:
            admin = _admin_from_token(db, token)
            if not admin.has_permission(permission):
                raise _forbidden((permission,))
            return admin
        return _user_from_token(db, token)

    dependency.required_permissions = (permission,)  # type: ignore[attr-defined]
    return dependency


def is_admin(actor: Union[User, AdminUser]) -> bool:
    """True for a console admin acting on a shared route (the permission was already checked)."""
    return isinstance(actor, AdminUser)


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
