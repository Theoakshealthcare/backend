"""PUT /auth/profile, the account part: name, phone, photo (and the refusals for email and password).

Why this lives in a service and loads the user itself: ``get_current_user`` opens its session from ``app.api.deps.get_db``
while the profile endpoint's ``db`` comes from ``app.db.session.get_db``. Those are two different sessions, so setting
fields on the ``current_user`` object and committing the endpoint's session saved nothing (DP1-33). The user is loaded
here, in the session that commits.

Name and phone are also copied onto the patient / caregiver row (the apps read ``user.patient`` and ``user.caregiver``
from /auth/me), so all three are written together or not at all.
"""
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.caregiver import Caregiver
from app.db.models.patient import Patient
from app.core.phone import normalize_e164, phone_variants
from app.db.models.user import User
from app.schemas.user import UserProfileUpdate


class ProfileError(Exception):
    status_code = 400
    code = ""
    message = ""


class EmailChangeNotSupported(ProfileError):
    code = "email_change_not_supported"
    message = ("Your email address is your sign-in identity and can't be changed here yet. "
               "Contact support if you need to change it.")


class PasswordChangeNotSupported(ProfileError):
    code = "use_change_password"
    message = "Passwords are changed with POST /auth/change-password, which asks for the current password."


class PhotoUploadOnly(ProfileError):
    code = "photo_upload_only"
    message = ("A profile photo can only be set by uploading it (POST /auth/profile/upload-image). "
               "Send null here to remove it.")


class PhoneNumberRequired(ProfileError):
    code = "phone_number_required"
    message = "Your phone number is how you sign in, so it can't be removed from an account with no email address."


class PhoneNumberTaken(ProfileError):
    status_code = 409
    code = "phone_number_taken"
    message = "That phone number is already used by another account."


class ProfileService:
    def __init__(self, db: Session):
        self.db = db

    def update_account_fields(self, user_id: UUID, data: UserProfileUpdate) -> User:
        sent = data.model_fields_set
        user = self.db.get(User, user_id)

        # Refuse before anything is touched, so a refused request saves nothing.
        if data.email is not None and (user.email or "").lower() != data.email.lower():
            raise EmailChangeNotSupported()
        if data.password:
            raise PasswordChangeNotSupported()
        if "profile_image_url" in sent and data.profile_image_url is not None \
                and data.profile_image_url != user.profile_image_url:
            # Account erasure and the upload route delete the stored URL's file from Cloudinary, so a stored URL must
            # always be one of ours that this user uploaded: a URL typed in could point at someone else's photo.
            raise PhotoUploadOnly()
        if "phone_number" in sent and data.phone_number is None and not user.email:
            raise PhoneNumberRequired()
        if data.phone_number and self._phone_in_use(data.phone_number, user.id):
            raise PhoneNumberTaken()

        profile = None
        if user.user_type == "patient":
            profile = self.db.query(Patient).filter(Patient.user_id == user.id).first()
        elif user.user_type == "caregiver":
            profile = self.db.query(Caregiver).filter(Caregiver.user_id == user.id).first()

        for field in ("full_name", "phone_number", "profile_image_url"):
            if field in sent:
                setattr(user, field, getattr(data, field))
                if profile is not None and field != "profile_image_url":
                    setattr(profile, field, getattr(data, field))
        try:
            self.db.commit()
        except IntegrityError as exc:  # someone took the number between the check and the write
            self.db.rollback()
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None) or str(exc.orig)
            if "phone" not in constraint.lower():
                raise
            raise PhoneNumberTaken()
        return user

    def _phone_in_use(self, phone: str, user_id: UUID) -> bool:
        """Another account holds this number in ANY spelling (+234..., 234..., 080...): WhatsApp matching and
        erasure treat those as one number, so a second account must not take a different spelling of it."""
        e164 = normalize_e164(phone)
        spellings = phone_variants(e164) | {phone} if e164 else {phone}
        return self.db.query(User.id).filter(User.phone_number.in_(spellings), User.id != user_id).first() is not None
