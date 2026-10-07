import logging

import anyio.from_thread
from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    create_access_token,
    create_refresh_token,
    verify_refresh_token,
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
    UserProfileUpdate,
    UserProfileResponse,
    RefreshTokenRequest,
    DetailResponse,
)
from app.services.email import send_verification_email, send_password_reset_email
from app.db.models.media_purge import PendingMediaPurge
from app.services.websocket_manager import websocket_manager
from app.services.account_erasure import AccountErasureService, ErasureBlocked
from app.services.user import UserService
from app.services.cloudinary_service import cloudinary_service
from app.db.models.patient import Patient
from app.db.models.caregiver import Caregiver
from app.core.security import get_password_hash
from app.db.models.user import User
from app.schemas.user import User as UserSchema

logger = logging.getLogger(__name__)
router = APIRouter()

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login")


def get_user_service(db: Session = Depends(get_db)) -> UserService:
    return UserService(db)


@router.post("/register", response_model=UserSchema)
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
    refresh_token_expires = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    return {
        "access_token": create_access_token(
            user.id, expires_delta=access_token_expires
        ),
        "refresh_token": create_refresh_token(
            user.id, expires_delta=refresh_token_expires
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
    refresh_token_expires = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    return {
        "access_token": create_access_token(
            user.id, expires_delta=access_token_expires
        ),
        "refresh_token": create_refresh_token(
            user.id, expires_delta=refresh_token_expires
        ),
        "token_type": "bearer",
    }


@router.post("/refresh", response_model=Token)
def refresh_token(
    refresh_request: RefreshTokenRequest,
    user_service: UserService = Depends(get_user_service),
) -> Any:
    """
    Refresh access token using refresh token.
    """
    user_id = verify_refresh_token(refresh_request.refresh_token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )
    
    # Verify user still exists and is active
    user = user_service.get_user(user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user",
        )
    
    # Generate new tokens
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    refresh_token_expires = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    return {
        "access_token": create_access_token(
            user.id, expires_delta=access_token_expires
        ),
        "refresh_token": create_refresh_token(
            user.id, expires_delta=refresh_token_expires
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


@router.put("/profile", response_model=UserProfileResponse)
def update_profile(
    update_data: UserProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Update User fields
    if update_data.full_name:
        current_user.full_name = update_data.full_name
    if update_data.email:
        current_user.email = update_data.email
    if update_data.phone_number:
        current_user.phone_number = update_data.phone_number
    if update_data.profile_image_url:
        current_user.profile_image_url = update_data.profile_image_url
    if update_data.password:
        current_user.hashed_password = get_password_hash(update_data.password)
    db.commit()
    # Update Patient or Caregiver fields
    patient = None
    caregiver = None
    if current_user.user_type == "patient":
        patient = db.query(Patient).filter(Patient.user_id == current_user.id).first()
        if patient:
            if update_data.address:
                patient.address = update_data.address
            if update_data.gender:
                patient.gender = update_data.gender
            if update_data.date_of_birth:
                patient.date_of_birth = update_data.date_of_birth
            if update_data.emergency_contact_name:
                patient.emergency_contact_name = update_data.emergency_contact_name
            if update_data.emergency_contact_phone:
                patient.emergency_contact_phone = update_data.emergency_contact_phone
            if update_data.emergency_contact_address:
                patient.emergency_contact_address = update_data.emergency_contact_address
            db.commit()
    elif current_user.user_type == "caregiver":
        caregiver = db.query(Caregiver).filter(Caregiver.user_id == current_user.id).first()
        if caregiver:
            if update_data.address:
                caregiver.address = update_data.address
            if update_data.gender:
                caregiver.gender = update_data.gender
            if update_data.date_of_birth:
                caregiver.date_of_birth = update_data.date_of_birth
            if update_data.emergency_contact_name:
                caregiver.emergency_contact_name = update_data.emergency_contact_name
            if update_data.emergency_contact_phone:
                caregiver.emergency_contact_phone = update_data.emergency_contact_phone
            if update_data.emergency_contact_address:
                caregiver.emergency_contact_address = update_data.emergency_contact_address
            db.commit()
    # Re-query the user to ensure it is attached to the session
    user = db.query(User).filter(User.id == current_user.id).first()
    return UserProfileResponse(
        id=user.id,
        full_name=user.full_name,
        email=user.email,
        phone_number=user.phone_number,
        profile_image_url=user.profile_image_url,
        user_type=user.user_type,
        address=getattr(caregiver if user.user_type == 'caregiver' else patient, 'address', None),
        gender=getattr(caregiver if user.user_type == 'caregiver' else patient, 'gender', None),
        date_of_birth=getattr(caregiver if user.user_type == 'caregiver' else patient, 'date_of_birth', None),
        emergency_contact_name=getattr(caregiver if user.user_type == 'caregiver' else patient, 'emergency_contact_name', None),
        emergency_contact_phone=getattr(caregiver if user.user_type == 'caregiver' else patient, 'emergency_contact_phone', None),
        emergency_contact_address=getattr(caregiver if user.user_type == 'caregiver' else patient, 'emergency_contact_address', None),
        created_at=str(user.created_at),
        updated_at=str(user.updated_at),
    )

@router.delete("/profile", response_model=DetailResponse)
def delete_account(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Erase the account (anonymisation). Bookings, payments and consent records are kept for legal
    retention; the person's data is wiped and the account can no longer sign in. 409 while the person
    has active bookings or money owed, with a detail that says what to do."""
    erasure = AccountErasureService(db)
    try:
        erasure.settle_pending_deposits(current_user.id)  # talks to Paystack, so it runs before any lock
        result = erasure.erase(current_user.id)
        db.commit()
    except ErasureBlocked as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except Exception:
        db.rollback()
        logger.exception("account erasure failed user=%s", current_user.id)
        raise
    # After the commit and outside the transaction: none of this may undo or fail the erasure.
    _close_caregiver_socket(current_user.id)
    _delete_cloudinary_image(db, current_user.id, result.profile_image_url)
    return DetailResponse(detail="Account deleted successfully.")


def _close_caregiver_socket(user_id) -> None:
    """Drop the caregiver's open location socket (the manager lives on the event loop; this endpoint runs in
    a worker thread). Sockets on other workers close on their next location_update, which checks the user."""
    try:
        anyio.from_thread.run(websocket_manager.close_caregiver, str(user_id))
    except Exception:
        logger.warning("could not close the socket of erased user %s", user_id, exc_info=True)


def _delete_cloudinary_image(db: Session, user_id, url: Optional[str]) -> None:
    if not url:
        return
    public_id = cloudinary_service.get_public_id_from_url(url)
    if not public_id:
        return
    try:
        deleted = cloudinary_service.delete_image(public_id)
    except Exception:
        deleted = False
    if deleted:
        return
    # The database reference is already gone, so queue the file for a later purge. Never log the URL.
    logger.warning("profile image of erased user %s was not deleted at the provider; queued for purge", user_id)
    try:
        db.add(PendingMediaPurge(provider="cloudinary", public_id=public_id, reason="account_erased",
                                 user_id=user_id))
        db.commit()
    except Exception:
        db.rollback()
        logger.error("could not queue the profile image of erased user %s for purge", user_id, exc_info=True)


@router.post("/profile/upload-image", response_model=dict)
def upload_profile_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        print(f"=== UPLOAD DEBUG START ===")
        print(f"User ID: {current_user.id}")
        print(f"File name: {file.filename}")
        print(f"File content type: {file.content_type}")
        print(f"Current profile image URL: {current_user.profile_image_url}")
        
        # Delete old profile image from Cloudinary if exists
        if current_user.profile_image_url:
            old_public_id = cloudinary_service.get_public_id_from_url(current_user.profile_image_url)
            print(f"Old public ID: {old_public_id}")
            if old_public_id:
                deleted = cloudinary_service.delete_image(old_public_id)
                print(f"Old image deleted: {deleted}")
        
        # Upload new image to Cloudinary
        print("Starting Cloudinary upload...")
        image_url = cloudinary_service.upload_image(file, folder="oaklycare/profile_images")
        print(f"Cloudinary returned URL: {image_url}")
        
        # Get user from database using the current session
        user_id = current_user.id
        print(f"Getting user from database with ID: {user_id}")
        user = db.query(User).filter(User.id == user_id).first()
        
        if not user:
            print("ERROR: User not found in database!")
            raise HTTPException(status_code=404, detail="User not found")
        
        print(f"Current profile image URL from DB: {user.profile_image_url}")
        
        # Update user's profile image URL
        print(f"Setting profile_image_url to: {image_url}")
        user.profile_image_url = image_url
        print(f"Before commit - user.profile_image_url: {user.profile_image_url}")
        
        # Commit the transaction
        db.commit()
        print("Database commit completed")
        
        # Refresh the user object to get the latest data
        db.refresh(user)
        print(f"After refresh - user.profile_image_url: {user.profile_image_url}")
        print(f"=== UPLOAD DEBUG END ===")
        
        return {"profile_image_url": user.profile_image_url}
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"=== UPLOAD ERROR ===")
        print(f"Error: {str(e)}")
        print(f"Error type: {type(e)}")
        import traceback
        traceback.print_exc()
        print(f"=== ERROR END ===")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to upload profile image: {str(e)}"
        )
