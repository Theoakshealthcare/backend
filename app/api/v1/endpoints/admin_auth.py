"""Admin console sign-in: /admin/auth/* (AD-FR-81).

Login is two steps. POST /login checks the password and returns a short-lived ``mfa_token``;
POST /mfa/verify checks the TOTP code and returns the token pair. Errors use the project's
``{"detail": "..."}`` shape.
"""

from typing import Any, Callable, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.admin_auth import (
    InviteAcceptIn,
    InviteAcceptOut,
    LoginIn,
    LoginOut,
    MfaVerifyIn,
    MfaVerifyOut,
    RefreshIn,
    TokenPairOut,
)
from app.services.admin_auth import (
    AccountLocked,
    AdminAuthService,
    InvalidCredentials,
    InvalidInvite,
    InvalidMfaCode,
    InvalidMfaToken,
    InvalidRefreshToken,
    RateLimited,
    WeakPassword,
)
from app.services.admin_users import me_payload

router = APIRouter()


def get_service(db: Session = Depends(get_db)) -> AdminAuthService:
    return AdminAuthService(db)


def _unauthorized(detail: str) -> NoReturn:
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def _run(fn: Callable[[], Any]) -> Any:
    """Call a service method and translate its errors into HTTP responses."""
    try:
        return fn()
    except InvalidCredentials:
        _unauthorized("Invalid email or password")
    except InvalidMfaToken:
        _unauthorized("Invalid or expired MFA token")
    except InvalidMfaCode:
        _unauthorized("Invalid verification code")
    except InvalidRefreshToken:
        _unauthorized("Invalid or expired refresh token")
    except InvalidInvite:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invite link is invalid or has expired")
    except AccountLocked as exc:
        raise HTTPException(
            status.HTTP_423_LOCKED, detail="Account temporarily locked. Try again later.",
            headers={"Retry-After": str(exc.retry_after)},
        )
    except RateLimited as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many requests. Try again later.",
            headers={"Retry-After": str(exc.retry_after)},
        )
    except WeakPassword as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.post("/login", response_model=LoginOut, response_model_exclude_none=True)
def login(body: LoginIn, request: Request, service: AdminAuthService = Depends(get_service)) -> Any:
    return _run(lambda: service.login(body.email, body.password, request))


@router.post("/mfa/verify", response_model=MfaVerifyOut)
def verify_mfa(body: MfaVerifyIn, request: Request, service: AdminAuthService = Depends(get_service)) -> Any:
    result = _run(lambda: service.verify_mfa(body.mfa_token, body.code, request))
    return {**result, "admin": me_payload(result["admin"])}


@router.post("/refresh", response_model=TokenPairOut)
def refresh(body: RefreshIn, request: Request, service: AdminAuthService = Depends(get_service)) -> Any:
    return _run(lambda: service.refresh(body.refresh_token, request))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(body: RefreshIn, service: AdminAuthService = Depends(get_service)) -> Response:
    service.logout(body.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/invite/accept", response_model=InviteAcceptOut)
def accept_invite(body: InviteAcceptIn, request: Request, service: AdminAuthService = Depends(get_service)) -> Any:
    admin = _run(lambda: service.accept_invite(body.invite_token, body.password, request))
    return {"accepted": True, "email": admin.email}
