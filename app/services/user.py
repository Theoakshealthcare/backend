import secrets
import string
from datetime import datetime, timedelta
from typing import Optional, Union
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.core.security import get_password_hash, verify_password
from app.db.models.user import User, Caregiver, Admin
from app.db.models.patient import Patient
from app.schemas.user import UserCreate, CaregiverCreate
from app.schemas.patient import PatientCreate


class UserService:
    def __init__(self, db: Session):
        self.db = db

    def get_user(self, user_id: UUID) -> Optional[User]:
        return self.db.query(User).filter(User.id == user_id).first()

    def get_user_by_email(self, email: str) -> Optional[User]:
        return self.db.query(User).filter(User.email == email).first()

    def get_user_by_phone(self, phone: str) -> Optional[User]:
        return self.db.query(User).filter(User.phone_number == phone).first()

    def get_user_by_email_or_phone(self, email_or_phone: str) -> Optional[User]:
        return self.db.query(User).filter(
            or_(User.email == email_or_phone, User.phone_number == email_or_phone)
        ).first()

    def create_user(self, user_in: UserCreate) -> User:
        # Create the main user record
        hashed_password = get_password_hash(user_in.password)
        db_user = User(
            email=user_in.email,
            phone_number=user_in.phone_number,
            full_name=user_in.full_name,
            user_type=user_in.user_type,
            hashed_password=hashed_password,
        )
        self.db.add(db_user)
        self.db.flush()  # Flush to get the user ID

        # Create type-specific record
        if user_in.user_type == "patient":
            patient = Patient(
                user_id=db_user.id,
                email=user_in.email,
                full_name=user_in.full_name,
                phone_number=user_in.phone_number,
            )
            self.db.add(patient)
        elif user_in.user_type == "caregiver":
            caregiver = Caregiver(
                user_id=db_user.id,
                email=user_in.email,
                full_name=user_in.full_name,
                phone_number=user_in.phone_number,
            )
            self.db.add(caregiver)

        self.db.commit()
        self.db.refresh(db_user)
        return db_user

    def authenticate_user(self, email_or_phone: str, password: str) -> Optional[User]:
        user = self.get_user_by_email_or_phone(email_or_phone)
        if not user:
            return None
        if not verify_password(password, user.hashed_password):
            return None
        return user

    def generate_verification_code(self, user_id: UUID) -> str:
        code = ''.join(secrets.choice(string.digits) for _ in range(5))
        expires_at = datetime.utcnow() + timedelta(minutes=10)
        
        user = self.get_user(user_id)
        if user:
            user.verification_code = code
            user.verification_code_expires_at = expires_at
            self.db.commit()
        
        return code

    def verify_otp(self, email: str, code: str) -> bool:
        user = self.get_user_by_email(email)
        if not user:
            return False
        
        if (user.verification_code == code and 
            user.verification_code_expires_at and 
            user.verification_code_expires_at > datetime.utcnow()):
            
            user.is_verified = True
            user.verification_code = None
            user.verification_code_expires_at = None
            self.db.commit()
            return True
        
        return False

    def set_password_reset_token(self, user_id: UUID, token: str) -> bool:
        user = self.get_user(user_id)
        if not user:
            return False
        
        expires_at = datetime.utcnow() + timedelta(hours=1)
        user.password_reset_token = token
        user.password_reset_expires_at = expires_at
        self.db.commit()
        return True

    def verify_password_reset_token_db(self, email: str, token: str) -> bool:
        user = self.get_user_by_email(email)
        if not user:
            return False
        
        return (user.password_reset_token == token and 
                user.password_reset_expires_at and 
                user.password_reset_expires_at > datetime.utcnow())

    def reset_password(self, email: str, new_password: str) -> bool:
        user = self.get_user_by_email(email)
        if not user:
            return False
        
        user.hashed_password = get_password_hash(new_password)
        user.password_reset_token = None
        user.password_reset_expires_at = None
        self.db.commit()
        return True

    # Type-specific methods
    def get_patient(self, user_id: UUID) -> Optional[Patient]:
        return self.db.query(Patient).filter(Patient.user_id == user_id).first()

    def get_caregiver(self, user_id: UUID) -> Optional[Caregiver]:
        return self.db.query(Caregiver).filter(Caregiver.user_id == user_id).first()

    def get_user_with_type_data(self, user_id: UUID) -> Optional[User]:
        user = self.get_user(user_id)
        if not user:
            return None
        
        # Load the appropriate type-specific data
        if user.user_type == "patient":
            user.patient = self.get_patient(user_id)
        elif user.user_type == "caregiver":
            user.caregiver = self.get_caregiver(user_id)
        
        return user

    def update_patient(self, user_id: UUID, **kwargs) -> Optional[Patient]:
        patient = self.get_patient(user_id)
        if not patient:
            return None
        
        for key, value in kwargs.items():
            if hasattr(patient, key):
                setattr(patient, key, value)
        
        patient.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(patient)
        return patient

    def update_caregiver(self, user_id: UUID, **kwargs) -> Optional[Caregiver]:
        caregiver = self.get_caregiver(user_id)
        if not caregiver:
            return None
        
        for key, value in kwargs.items():
            if hasattr(caregiver, key):
                setattr(caregiver, key, value)
        
        caregiver.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(caregiver)
        return caregiver
