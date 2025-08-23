from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
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
    UserProfileUpdate,
    UserProfileResponse,
)
from app.services.email import send_verification_email, send_password_reset_email
from app.services.user import UserService
from app.db.models.patient import Patient
from app.db.models.caregiver import Caregiver
from app.core.security import get_password_hash
import os
from uuid import uuid4
from app.db.models.user import User
from app.schemas.user import User as UserSchema

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

@router.delete("/profile", response_model=dict)
def delete_account(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Delete Patient or Caregiver record
    if current_user.user_type == "patient":
        db.query(Patient).filter(Patient.user_id == current_user.id).delete()
    elif current_user.user_type == "caregiver":
        db.query(Caregiver).filter(Caregiver.user_id == current_user.id).delete()
    # Delete User record
    db.delete(current_user)
    db.commit()
    return {"detail": "Account deleted successfully."}


@router.post("/profile/upload-image", response_model=dict)
def upload_profile_image(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Validate file type
    allowed_types = ["image/jpeg", "image/png", "image/jpg"]
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Invalid file type. Only JPEG and PNG are allowed.")
    # Save file
    upload_dir = "uploads/profile_images"
    os.makedirs(upload_dir, exist_ok=True)
    ext = file.filename.split(".")[-1]
    filename = f"{uuid4()}.{ext}"
    file_path = os.path.join(upload_dir, filename)
    with open(file_path, "wb") as f:
        f.write(file.file.read())
    # Set the URL (absolute for frontend access)
    url = f"/uploads/profile_images/{filename}"
    print(f"Saving profile image to: {file_path}")
    print(f"Setting profile_image_url to: {url}")
    current_user.profile_image_url = url
    print(f"Before commit: {current_user.profile_image_url}")
    db.commit()
    user = db.query(User).filter(User.id == current_user.id).first()
    print(f"After commit: {user.profile_image_url}")
    return {"profile_image_url": user.profile_image_url}


