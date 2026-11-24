"""E-mail the admins who review caregivers when something needs their eyes (caregiver onboarding events, v1).

Three events, one e-mail each:

* ``submitted`` / ``resubmitted``: the profile moved into ``under_review`` (the consent step, first time or after changes).
* ``identity_review``: an identity check ended as ``needs_review``; a person has to decide it.
* ``document_added``: a document was uploaded while the application is ``under_review`` or ``changes_requested``. At most one
  per caregiver per 30 minutes (see ``_recently_uploaded``).

Recipients: every ACTIVE admin whose roles carry ``caregivers.approve`` (so Super Admin is in), plus ``ADMIN_NOTIFY_EMAIL`` when set.

Rules every call site relies on:

* Best effort. ``notify`` and the two ``on_*`` helpers never raise: a failure is logged and swallowed, so a mail problem cannot
  fail the caregiver's request. The callers invoke them AFTER the database commit.
* The lookups use a short session of their own, so a failed query cannot poison the caller's session or expire the objects the
  caller is about to return. The HTTP calls to the mail provider run on a worker thread, so a slow provider never holds the
  caregiver's request; that thread touches no database.
* Minimal PII: the caregiver's full name, one sentence and a link to the console page. Never a document, an ID number, a photo, an
  address, a phone number or the reason a check failed.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy import select

from app.core.config import settings
from app.db.models.admin import AdminRolePermission, AdminUser, AdminUserRole
from app.db.models.caregiver import Caregiver, CaregiverDocument
from app.db.session import SessionLocal
from app.services import email as email_service

logger = logging.getLogger(__name__)

APPROVE_PERMISSION = "caregivers.approve"
DOCUMENT_DEBOUNCE = timedelta(minutes=30)
OPEN_STATUSES = ("under_review", "changes_requested")

# event -> (heading, the sentence that follows the caregiver's name)
COPY = {
    "submitted": ("Caregiver profile submitted for review", "has submitted their profile for review."),
    "resubmitted": ("Caregiver profile resubmitted for review",
                    "has resubmitted their profile for review after the changes that were requested."),
    "identity_review": ("Caregiver identity check needs a review",
                        "has an identity check that ended as needs review and is waiting for an admin."),
    "document_added": ("Caregiver added a document", "added a document while their application is open."),
}

# Test hook: True sends on the calling thread, so a test can read the mail transport straight after the call.
_send_inline = False
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="admin-notify")


def notify(event: str, *, caregiver_id: Optional[UUID] = None, user_id: Optional[UUID] = None,
           document_id: Optional[UUID] = None) -> None:
    """Tell the reviewers about ``event``. Never raises."""
    try:
        mails = _prepare(event, caregiver_id, user_id, document_id)
        if not mails:
            return
        if _send_inline:
            _send_all(mails)
        else:
            _executor.submit(_send_all, mails)
    except Exception:  # noqa: BLE001 - best effort: the caregiver's request has already been committed
        logger.warning("admin notify failed event=%s", event, exc_info=True)


def on_profile_status_change(caregiver_id: UUID, before: Optional[str], after: Optional[str]) -> None:
    """The consent step ran: mail only when the profile just ENTERED ``under_review`` (a repeat of the same state is silent)."""
    if after == "under_review" and before != "under_review":
        notify("resubmitted" if before == "changes_requested" else "submitted", caregiver_id=caregiver_id)


def on_document_uploaded(caregiver_id: UUID, document_id: UUID) -> None:
    notify("document_added", caregiver_id=caregiver_id, document_id=document_id)


def _prepare(event: str, caregiver_id: Optional[UUID], user_id: Optional[UUID], document_id: Optional[UUID]) -> list[tuple[str, str, str]]:
    """(to, subject, html) for every recipient; empty when nobody is to be told."""
    heading, message = COPY[event]
    with SessionLocal() as db:
        query = select(Caregiver)
        query = query.where(Caregiver.id == caregiver_id) if caregiver_id is not None else query.where(Caregiver.user_id == user_id)
        caregiver = db.scalars(query).first()
        if caregiver is None:
            return []
        if event == "document_added" and not _document_is_news(db, caregiver, document_id):
            return []
        recipients = _recipients(db)
        full_name, link_id = caregiver.full_name or "A caregiver", caregiver.id
    if not recipients:
        return []
    link = f"{settings.ADMIN_CONSOLE_URL.rstrip('/')}/caregivers/applications/{link_id}"
    html = email_service.render_template("admin_caregiver_notice", {
        "project_name": settings.PROJECT_NAME, "heading": heading, "message": message, "full_name": full_name, "link": link})
    subject = f"{settings.PROJECT_NAME} - {heading}"
    return [(to, subject, html) for to in recipients]


def _recipients(db) -> list[str]:
    """Active admins holding the approve permission, plus the optional extra address; lower-cased, no duplicates."""
    with_permission = (select(AdminUserRole.admin_user_id)
                       .join(AdminRolePermission, AdminRolePermission.role_id == AdminUserRole.role_id)
                       .where(AdminRolePermission.permission == APPROVE_PERMISSION))
    emails = db.scalars(select(AdminUser.email).where(AdminUser.status == "active", AdminUser.id.in_(with_permission))).all()
    found = {e.strip().lower() for e in emails if e and e.strip()}
    extra = (settings.ADMIN_NOTIFY_EMAIL or "").strip().lower()
    if extra:
        found.add(extra)
    return sorted(found)


def _document_is_news(db, caregiver: Caregiver, document_id: Optional[UUID]) -> bool:
    """The application must be open, and this must be the first upload of its burst."""
    if caregiver.profile_status not in OPEN_STATUSES or document_id is None:
        return False
    document = db.get(CaregiverDocument, document_id)
    return document is not None and not _recently_uploaded(db, caregiver.id, document)


def _recently_uploaded(db, caregiver_id: UUID, document: CaregiverDocument) -> bool:
    """True when this caregiver uploaded another document in the 30 minutes before this one. That is the debounce: the first
    upload of a burst sends the mail, every upload within 30 minutes of the previous one stays silent, so a caregiver adding five
    files costs the admins one e-mail. It reads the uploads themselves (no extra column or cache, so it is the same on every
    worker); only the EARLIER of two near-simultaneous uploads counts as "previous", so exactly one of them sends."""
    earlier = (CaregiverDocument.uploaded_at < document.uploaded_at) | (
        (CaregiverDocument.uploaded_at == document.uploaded_at) & (CaregiverDocument.id < document.id))
    other = db.scalars(select(CaregiverDocument.id).where(
        CaregiverDocument.caregiver_id == caregiver_id, CaregiverDocument.id != document.id, earlier,
        CaregiverDocument.uploaded_at >= document.uploaded_at - DOCUMENT_DEBOUNCE).limit(1)).first()
    return other is not None


def _send_all(mails: list[tuple[str, str, str]]) -> None:
    for to, subject, html in mails:
        try:
            email_service.send_email(to, subject, html)  # returns False and logs on a provider error; never raises
        except Exception:  # noqa: BLE001
            logger.warning("admin notify email failed", exc_info=True)
