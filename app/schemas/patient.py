from datetime import datetime, date
from typing import Optional, List
from pydantic import BaseModel, EmailStr, validator
import uuid


# Base Patient Schemas
class PatientBase(BaseModel):
    email: EmailStr
    full_name: str
    phone_number: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    emergency_contact_relationship: Optional[str] = None
    blood_group: Optional[str] = None
    allergies: Optional[str] = None
    medical_conditions: Optional[str] = None
    medications: Optional[str] = None
    preferred_language: Optional[str] = None
    special_requirements: Optional[str] = None

    @validator('gender')
    def validate_gender(cls, v):
        if v and v not in ['male', 'female', 'other']:
            raise ValueError('gender must be either "male", "female", or "other"')
        return v


class PatientCreate(PatientBase):
    user_id: uuid.UUID


class PatientUpdate(BaseModel):
    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    date_of_birth: Optional[date] = None
    gender: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None
    emergency_contact_relationship: Optional[str] = None
    blood_group: Optional[str] = None
    allergies: Optional[str] = None
    medical_conditions: Optional[str] = None
    medications: Optional[str] = None
    preferred_language: Optional[str] = None
    special_requirements: Optional[str] = None
    is_active: Optional[bool] = None
    profile_completion_step: Optional[int] = None

    @validator('gender')
    def validate_gender(cls, v):
        if v and v not in ['male', 'female', 'other']:
            raise ValueError('gender must be either "male", "female", or "other"')
        return v

    @validator('profile_completion_step')
    def validate_profile_completion_step(cls, v):
        if v is not None and (v < 0 or v > 3):
            raise ValueError('profile_completion_step must be between 0 and 3')
        return v


class Patient(PatientBase):
    id: uuid.UUID
    user_id: uuid.UUID
    is_active: bool
    profile_completion_step: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Patient Medical Record Schemas
class PatientMedicalRecordBase(BaseModel):
    record_type: str
    title: str
    description: Optional[str] = None
    date_recorded: Optional[date] = None
    doctor_name: Optional[str] = None
    hospital_clinic: Optional[str] = None
    severity: Optional[str] = None
    file_path: Optional[str] = None
    file_name: Optional[str] = None

    @validator('record_type')
    def validate_record_type(cls, v):
        valid_types = ['diagnosis', 'medication', 'allergy', 'procedure']
        if v not in valid_types:
            raise ValueError(f'record_type must be one of {valid_types}')
        return v

    @validator('severity')
    def validate_severity(cls, v):
        if v and v not in ['low', 'medium', 'high']:
            raise ValueError('severity must be either "low", "medium", or "high"')
        return v


class PatientMedicalRecordCreate(PatientMedicalRecordBase):
    patient_id: uuid.UUID


class PatientMedicalRecordUpdate(BaseModel):
    record_type: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    date_recorded: Optional[date] = None
    doctor_name: Optional[str] = None
    hospital_clinic: Optional[str] = None
    severity: Optional[str] = None
    file_path: Optional[str] = None
    file_name: Optional[str] = None

    @validator('record_type')
    def validate_record_type(cls, v):
        if v:
            valid_types = ['diagnosis', 'medication', 'allergy', 'procedure']
            if v not in valid_types:
                raise ValueError(f'record_type must be one of {valid_types}')
        return v

    @validator('severity')
    def validate_severity(cls, v):
        if v and v not in ['low', 'medium', 'high']:
            raise ValueError('severity must be either "low", "medium", or "high"')
        return v


class PatientMedicalRecord(PatientMedicalRecordBase):
    id: uuid.UUID
    patient_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Patient Family Member Schemas
class PatientFamilyMemberBase(BaseModel):
    full_name: str
    relationship: str
    phone_number: Optional[str] = None
    email: Optional[EmailStr] = None
    is_emergency_contact: bool = False
    can_make_medical_decisions: bool = False

    @validator('relationship')
    def validate_relationship(cls, v):
        valid_relationships = ['spouse', 'child', 'parent', 'sibling', 'grandparent', 'other']
        if v not in valid_relationships:
            raise ValueError(f'relationship must be one of {valid_relationships}')
        return v


class PatientFamilyMemberCreate(PatientFamilyMemberBase):
    patient_id: uuid.UUID


class PatientFamilyMemberUpdate(BaseModel):
    full_name: Optional[str] = None
    relationship: Optional[str] = None
    phone_number: Optional[str] = None
    email: Optional[EmailStr] = None
    is_emergency_contact: Optional[bool] = None
    can_make_medical_decisions: Optional[bool] = None

    @validator('relationship')
    def validate_relationship(cls, v):
        if v:
            valid_relationships = ['spouse', 'child', 'parent', 'sibling', 'grandparent', 'other']
            if v not in valid_relationships:
                raise ValueError(f'relationship must be one of {valid_relationships}')
        return v


class PatientFamilyMember(PatientFamilyMemberBase):
    id: uuid.UUID
    patient_id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Comprehensive Patient Response (with related data)
class PatientWithDetails(Patient):
    medical_records: List[PatientMedicalRecord] = []
    family_members: List[PatientFamilyMember] = []


# Patient Profile Completion
class PatientProfileCompletion(BaseModel):
    profile_completion_step: int
    missing_fields: List[str] = []

    @validator('profile_completion_step')
    def validate_profile_completion_step(cls, v):
        if v < 0 or v > 3:
            raise ValueError('profile_completion_step must be between 0 and 3')
        return v 