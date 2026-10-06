import json
import logging
import os
from typing import Any, Dict, List, Optional, Union

from pydantic import AnyHttpUrl, EmailStr, PostgresDsn, model_validator, validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ENVIRONMENT: str = "development"
    API_V1_STR: str = "/api/v1"
    # Required, no defaults: the app refuses to start without them.
    SECRET_KEY: str
    DATABASE_URL: str
    # Comma-separated Fernet keys. The first encrypts; all of them can decrypt
    # (lets the key be rotated without downtime, see scripts/reencrypt_health_data.py).
    VITALS_ENCRYPTION_KEY: str
    # 60 minutes for access token
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    # 7 days for refresh token
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
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
    MIN_WITHDRAWAL_KOBO: int = 100000  # 1,000 Naira, minimum caregiver withdrawal
    
    # Background jobs (ARQ worker) and dispatch tunables.
    REDIS_URL: str = "redis://localhost:6379/0"
    PAYMENT_LINK_TTL_MINUTES: int = 30  # an unpaid booking is cancelled after this long
    LOCATION_FRESHNESS_MINUTES: int = 15  # a caregiver location older than this is not matched
    # A payment that lands after the unpaid-expiry cancel reinstates the booking only if the visit is
    # at least this far ahead; otherwise it is flagged for a refund.
    LATE_PAYMENT_REINSTATE_MIN_LEAD_MINUTES: int = 60
    MATCH_RADIUS_KM: float = 20
    DISPATCH_WAVE_SIZE: int = 8  # caregivers notified per wave
    DISPATCH_WAVE_MINUTES: int = 10  # how long a wave waits for an accept before the next one
    DISPATCH_ESCALATE_MINUTES: int = 30  # total dispatch time before the booking is unmatched

    # Caregiver app: push notifications (Expo), visit code and home summary.
    EXPO_PUSH_URL: str = "https://exp.host/--/api/v2/push/send"
    EXPO_PUSH_TIMEOUT_SECONDS: float = 5  # the push runs inline after payment settles, so keep it short
    EXPO_ACCESS_TOKEN: Optional[str] = None  # only if "enhanced push security" is on for the Expo project
    VISIT_CODE_MAX_ATTEMPTS: int = 3  # wrong codes before the visit code locks
    VISIT_CODE_LOCK_MINUTES: int = 15
    HOME_TIMEZONE: str = "Africa/Lagos"  # what "today" means on the caregiver home screen

    # Admin console auth (AD-FR-81). Admin tokens are separate from app-user tokens (aud="admin").
    ADMIN_ACCESS_TOKEN_MINUTES: int = 15
    ADMIN_MFA_TOKEN_MINUTES: int = 5  # how long the password step stays valid while the code is entered
    ADMIN_IDLE_TIMEOUT_MINUTES: int = 30  # a refresh token not used within this window dies
    ADMIN_SESSION_MAX_HOURS: int = 12  # absolute cap on one login session, however active
    ADMIN_INVITE_HOURS: int = 48
    ADMIN_MAX_FAILED_LOGINS: int = 10  # wrong TOTP codes (password already proven) before the account locks
    # Lock length per consecutive lock; the last entry repeats. Resets on a successful MFA.
    ADMIN_LOCKOUT_TIERS_MINUTES: List[int] = [15, 60, 1440]
    # Password stage: per (email, client IP), never a hard lock. After this many failures every
    # attempt is answered 429 for a wait that doubles with each further failure, up to the cap.
    ADMIN_PASSWORD_FREE_ATTEMPTS: int = 5
    ADMIN_PASSWORD_BACKOFF_SECONDS: int = 30
    ADMIN_PASSWORD_BACKOFF_MAX_SECONDS: int = 900
    ADMIN_AUTH_IP_LIMIT: int = 30  # login/MFA/invite calls per client IP per window
    ADMIN_AUTH_IP_WINDOW_SECONDS: int = 60
    # Reverse proxies whose X-Forwarded-For is believed (CIDRs or addresses, comma-separated or
    # JSON list). Empty = trust nobody: the client IP is the direct peer.
    TRUSTED_PROXIES: List[str] = []

    @validator("TRUSTED_PROXIES", pre=True)
    def assemble_trusted_proxies(cls, v: Union[str, List[str]]) -> Union[List[str], str]:
        if isinstance(v, str):
            if v.strip().startswith("["):
                return json.loads(v)
            return [i.strip() for i in v.split(",") if i.strip()]
        return v
    ADMIN_TOTP_ISSUER: str = "Oakly Admin"
    # WhatsApp patient channel (Meta Cloud API). See docs/sop/2026-10-06-whatsapp-foundations.md.
    # An empty app secret or verify token makes the webhook refuse every request (fail closed).
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_APP_SECRET: str = ""
    WHATSAPP_VERIFY_TOKEN: str = ""
    WHATSAPP_GRAPH_URL: str = "https://graph.facebook.com"  # tests and the local e2e point this at a fake
    WHATSAPP_GRAPH_VERSION: str = "v23.0"  # check Meta's changelog before launch; old versions are retired
    WHATSAPP_HTTP_TIMEOUT_SECONDS: float = 5
    WHATSAPP_MAX_SEND_ATTEMPTS: int = 3  # per message, including the first try
    WHATSAPP_RETRY_BACKOFF_SECONDS: float = 0.5  # doubles each retry; Retry-After wins when Meta sends it
    WHATSAPP_POLICY_VERSION: str = "2026-10-01"  # recorded on every consent; bump when the notice changes
    WHATSAPP_PRIVACY_POLICY_URL: str = "https://www.oaklycare.com/privacy"
    # Comma-separated numbers shown in the emergency reply. 112 is the national emergency number; add the
    # state-specific lines only after they are verified (PRD WA-FR-40).
    WHATSAPP_EMERGENCY_NUMBERS: str = "112"
    WHATSAPP_SUPPORT_PHONE: str = ""  # optional phone line offered when someone declines consent

    # Frontend URL for payment callbacks
    FRONTEND_URL: str = "https://www.oaklycare.com"

    # Gemini AI Configuration
    GEMINI_API_KEY: Optional[str] = None
    

    # Push Notifications
    FCM_SERVER_KEY: Optional[str] = None
    APNS_KEY_ID: Optional[str] = None
    APNS_TEAM_ID: Optional[str] = None

    # Cloudinary Configuration
    CLOUDINARY_CLOUD_NAME: Optional[str] = None
    CLOUDINARY_API_KEY: Optional[str] = None
    CLOUDINARY_API_SECRET: Optional[str] = None

    # Private file storage (caregiver documents). Must NOT be inside the public /uploads mount;
    # in Docker this is a named volume.
    PRIVATE_UPLOAD_DIR: str = "private_uploads"

    @model_validator(mode="after")
    def _require_paystack_key_in_production(self):
        # An empty key makes webhook HMACs forgeable by anyone; refuse to boot.
        if self.ENVIRONMENT.lower() == "production" and not self.PAYSTACK_SECRET_KEY:
            raise ValueError("PAYSTACK_SECRET_KEY must be set when ENVIRONMENT=production")
        return self

    @model_validator(mode="after")
    def _warn_without_trusted_proxies_in_production(self):
        # Behind a reverse proxy every client otherwise shares the proxy's IP, so one attacker
        # could throttle an admin's logins for everyone. Not fatal: a direct deployment is valid.
        if self.ENVIRONMENT.lower() == "production" and not self.TRUSTED_PROXIES:
            logging.getLogger("app.config").warning(
                "TRUSTED_PROXIES is empty with ENVIRONMENT=production: if the API runs behind a "
                "reverse proxy, set it to the proxy CIDR(s) or all clients share one rate-limit IP."
            )
        return self

    class Config:
        case_sensitive = True
        # OAKLY_ENV_FILE="" disables .env loading (the test suite relies on this so a
        # developer's real .env can never leak into tests).
        env_file = os.environ.get("OAKLY_ENV_FILE", ".env") or None


settings = Settings()