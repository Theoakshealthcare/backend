from typing import Generator, Optional
from fastapi import Depends, HTTPException, status
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
