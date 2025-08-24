import secrets
import json
from typing import Any, Dict, List, Optional, Union

from pydantic import AnyHttpUrl, EmailStr, PostgresDsn, validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str = secrets.token_urlsafe(32)
    # 60 minutes * 24 hours * 8 days = 8 days
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8
    ALGORITHM: str = "HS256"
    # BACKEND_CORS_ORIGINS is a JSON-formatted list of origins
    # e.g: '["http://localhost", "http://localhost:4200", "http://localhost:3000"]'
    BACKEND_CORS_ORIGINS: List[AnyHttpUrl] = []

    @validator("BACKEND_CORS_ORIGINS", pre=True)
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> Union[List[str], str]:
        if isinstance(v, str):
            if v.startswith("["):
                try:
                    return json.loads(v)
                except json.JSONDecodeError:
                    return [i.strip() for i in v.split(",")]
            else:
                return [i.strip() for i in v.split(",")]
        elif isinstance(v, list):
            return v
        raise ValueError(v)

    PROJECT_NAME: str = "Oaklycare"
    
    # Database
    POSTGRES_SERVER: str = "ljrcyoonkqpbdsddqmwk.supabase.co"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "Oaklycarehealth"
    POSTGRES_DB: str = "postgres"
    DATABASE_URL: Optional[str] = None

    @validator("DATABASE_URL", pre=True)
    def assemble_db_connection(cls, v: Optional[str], values: Dict[str, Any]) -> Any:
        if isinstance(v, str):
            return v
        return "postgresql://postgres.ljrcyoonkqpbdsddqmwk:Oaklycarehealth@aws-0-eu-north-1.pooler.supabase.com:6543/postgres"

    # Email
    SMTP_TLS: bool = True
    SMTP_PORT: Optional[int] = None
    SMTP_HOST: Optional[str] = None
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    EMAILS_FROM_EMAIL: Optional[EmailStr] = None
    EMAILS_FROM_NAME: Optional[str] = None

    @validator("EMAILS_FROM_NAME")
    def get_project_name(cls, v: Optional[str], values: Dict[str, Any]) -> str:
        if not v:
            return values["PROJECT_NAME"]
        return v

    EMAIL_RESET_TOKEN_EXPIRE_HOURS: int = 48
    EMAIL_TEMPLATES_DIR: str = "app/templates/emails"
    
    # OTP
    OTP_EXPIRE_MINUTES: int = 10
    OTP_LENGTH: int = 5

    # First superuser
    FIRST_SUPERUSER_EMAIL: EmailStr = "admin@oaklycare.com"
    FIRST_SUPERUSER_PASSWORD: str = "admin"
    FIRST_SUPERUSER_FULLNAME: str = "Admin"
    
    # Onfido Configuration
    ONFIDO_API_KEY: str = ""
    ONFIDO_WEBHOOK_TOKEN: Optional[str] = None
    ONFIDO_REGION: str = "US"  # US, EU, CA
    
    # Paystack Configuration
    PAYSTACK_SECRET_KEY: str = ""
    PAYSTACK_PUBLIC_KEY: str = ""
    PAYSTACK_WEBHOOK_SECRET: str = ""
    PAYMENT_CALLBACK_URL: str = "https://www.oaklycare.com/payment/callback"
    PAYMENT_CURRENCY: str = "NGN"
    MAX_DEPOSIT_AMOUNT: int = 1000000  # 1 Million Naira
    MIN_DEPOSIT_AMOUNT: int = 100  # 100 Naira

    # Gemini AI Configuration
    GEMINI_API_KEY: Optional[str] = None
    
    # Encryption Key
    VITALS_ENCRYPTION_KEY: Optional[str] = None

    # Push Notifications
    FCM_SERVER_KEY: Optional[str] = None
    APNS_KEY_ID: Optional[str] = None
    APNS_TEAM_ID: Optional[str] = None

    # Cloudinary Configuration
    CLOUDINARY_CLOUD_NAME: Optional[str] = None
    CLOUDINARY_API_KEY: Optional[str] = None
    CLOUDINARY_API_SECRET: Optional[str] = None

    class Config:
        case_sensitive = True
        env_file = ".env"


settings = Settings()