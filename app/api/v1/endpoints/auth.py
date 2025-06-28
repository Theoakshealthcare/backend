from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    create_access_token,
    generate_password_reset_token,
    verify_password_reset_token,
)
from app.db.session import get_db
from app.api.deps import get_current_user
from app.schemas.user import (
    User,
    UserCreate,
    UserLogin,
    Token,
    PasswordResetRequest,
    PasswordReset,
    EmailVerificationRequest,
    EmailVerification,
    UserWithTypeData,
)
from app.services.email import send_verification_email, send_password_reset_email
from app.services.user import UserService

router = APIRouter()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login")


def get_user_service(db: Session = Depends(get_db)) -> UserService:
    return UserService(db)


@router.post("/register", response_model=User)
def register(
    user_in: UserCreate,
    user_service: UserService = Depends(get_user_service),
) -> Any:
    """
    Register a new user (patient or caregiver).
    """
    # Check if user already exists
    existing_user = user_service.get_user_by_email(email=user_in.email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user with this email already exists",
        )
    
    # Check phone number if provided
    if user_in.phone_number:
        existing_phone_user = user_service.get_user_by_phone(phone=user_in.phone_number)
        if existing_phone_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A user with this phone number already exists",
            )
    
    # Create user and type-specific record
    user = user_service.create_user(user_in)
    
    # Generate and send verification code
    code = user_service.generate_verification_code(user.id)
    send_verification_email(user.email, code)
    
    return user


@router.post("/login", response_model=Token)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    user_service: UserService = Depends(get_user_service),
) -> Any:
    """
    OAuth2 compatible token login, get an access token for future requests.
    """
    user = user_service.authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email/phone or password",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user",
        )
    
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return {
        "access_token": create_access_token(
            user.id, expires_delta=access_token_expires
        ),
        "token_type": "bearer",
    }


@router.post("/login/mobile", response_model=Token)
def login_mobile(
    login_data: UserLogin,
    user_service: UserService = Depends(get_user_service),
) -> Any:
    """
    Mobile app login endpoint.
    """
    user = user_service.authenticate_user(login_data.email_or_phone, login_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email/phone or password",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user",
        )
    
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return {
        "access_token": create_access_token(
            user.id, expires_delta=access_token_expires
        ),
        "token_type": "bearer",
    }


@router.get("/me", response_model=UserWithTypeData)
def get_current_user_info(
    current_user: User = Depends(get_current_user),
    user_service: UserService = Depends(get_user_service),
) -> Any:
    """
    Get current user information with type-specific data.
    """
    return user_service.get_user_with_type_data(current_user.id)


@router.post("/verify-email", response_model=dict)
def verify_email(
    verification: EmailVerification,
    user_service: UserService = Depends(get_user_service),
) -> Any:
    """
    Verify email with OTP code.
    """
    is_verified = user_service.verify_otp(verification.email, verification.code)
    if not is_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid verification code",
        )
    
    return {"message": "Email verified successfully"}


@router.post("/resend-verification", response_model=dict)
def resend_verification(
    request: EmailVerificationRequest,
    user_service: UserService = Depends(get_user_service),
) -> Any:
    """
    Resend verification code.
    """
    user = user_service.get_user_by_email(request.email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    if user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already verified",
        )
    
    code = user_service.generate_verification_code(user.id)
    send_verification_email(user.email, code)
    
    return {"message": "Verification code sent"}


@router.post("/password-recovery", response_model=dict)
def recover_password(
    request: PasswordResetRequest,
    user_service: UserService = Depends(get_user_service),
) -> Any:
    """
    Password Recovery.
    """
    user = user_service.get_user_by_email(request.email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    token = generate_password_reset_token(user.email)
    user_service.set_password_reset_token(user.id, token)
    send_password_reset_email(user.email, token)
    
    return {"message": "Password recovery email sent"}


@router.post("/reset-password", response_model=dict)
def reset_password_route(
    reset_data: PasswordReset,
    user_service: UserService = Depends(get_user_service),
) -> Any:
    """
    Reset password.
    """
    email = verify_password_reset_token(reset_data.token)
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid token",
        )
    
    is_valid = user_service.verify_password_reset_token_db(email, reset_data.token)
    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired token",
        )
    
    success = user_service.reset_password(email, reset_data.new_password)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    
    return {"message": "Password updated successfully"}


