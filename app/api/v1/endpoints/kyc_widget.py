"""The hosted Dojah widget page (caregiver KYC, KD2). ``GET /api/v1/kyc/widget/{token}``; the signed token is the only credential.

Opening the page writes nothing and logs nothing (the path holds the token: NEVER log it or the reference). A token that is malformed,
forged, signed with another key or for an unknown attempt is the same plain 404. A token that ran out, or an attempt that is no longer
open, gets the "finished" page (410) and never the widget. Every response carries the security headers, including these.
"""
import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.identity_verification import VERIFICATION_CREATED, VERIFICATION_STARTED, IdentityVerification
from app.db.session import get_db
from app.services import kyc_widget as widget
from app.services.kyc_seam import is_our_reference

router = APIRouter()

# Open = the widget may still be launched. ``awaiting_result`` (the app said the widget completed) is finished: a second run under one
# reference would be ambiguous (see the KD1 SOP follow-up).
_OPEN = (VERIFICATION_CREATED, VERIFICATION_STARTED)


def _respond(content: str, status_code: int, nonce: str, media_type: str = "text/html") -> Response:
    return Response(content=content, status_code=status_code, media_type=media_type, headers=widget.security_headers(nonce))


@router.get("/{token}", include_in_schema=False)
def widget_page(token: str, db: Session = Depends(get_db)) -> Response:
    nonce = widget.new_nonce()
    not_found = _respond("Not found", 404, nonce, "text/plain")
    expired = _respond(widget.render_finished(return_url=settings.KYC_RETURN_URL, nonce=nonce), 410, nonce)

    handle = widget.verify_token(token)
    if handle is None:
        # A well-formed token whose MAC is right but whose life is over is a link the app itself minted: tell the person it finished.
        return expired if widget.token_expired(token) else not_found
    row = db.query(IdentityVerification).filter(IdentityVerification.id == uuid.UUID(handle)).first()
    if row is None or not is_our_reference(row.provider_session_id):
        return not_found
    if row.status not in _OPEN:
        return expired
    app_id, public_key, widget_id = settings.DOJAH_APP_ID, settings.DOJAH_PUBLIC_KEY, settings.DOJAH_WIDGET_ID
    if not (app_id and public_key and widget_id):
        return _respond("Unavailable", 503, nonce, "text/plain")
    return _respond(widget.render_page(reference_id=row.provider_session_id, app_id=app_id, public_key=public_key, widget_id=widget_id,
                                       return_url=settings.KYC_RETURN_URL, nonce=nonce), 200, nonce)
