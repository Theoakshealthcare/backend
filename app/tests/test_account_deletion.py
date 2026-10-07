"""DELETE /auth/profile: an erasure request handled as anonymisation.

PII is wiped, the account is closed, and the financial / consent / audit records stay. Active work
(bookings in flight, money owed) blocks it with a 409 that says what to do.
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import select

from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.care import CareRecipient, CareRelationship
from app.db.models.caregiver import (
    Caregiver, CaregiverBankAccount, CaregiverTransaction, CaregiverWallet,
)
from app.db.models.consent import Consent
from app.db.models.patient import Patient
from app.db.models.push_token import PushToken
from app.db.models.support import SupportConversation
from app.db.models.user import User
from app.db.models.whatsapp import WhatsAppContact, WhatsAppConversation
from app.tests.factories import (
    DEFAULT_PASSWORD, auth_headers, make_booking, make_caregiver, make_caregiver_wallet,
    make_patient, refresh_headers,
)
from app.tests.factories_whatsapp import make_contact

URL = "/api/v1/auth/profile"


def _add_history(db, caregiver, patient):
    """A completed visit, its earning, a consent and a support ticket for the caregiver's user."""
    booking = make_booking(db, patient, caregiver, status=BookingStatus.COMPLETED,
                           payment_status=PaymentStatus.PAID)
    db.add(CaregiverTransaction(caregiver_id=caregiver.id, type="earning", amount=3_000_000,
                                status="successful", reference=f"EARN_{uuid.uuid4().hex[:8]}"))
    db.add(Consent(subject_user_id=caregiver.user_id, granted_by_user_id=caregiver.user_id, type="terms",
                   decision="granted", policy_version="v1", channel="app"))
    db.commit()
    return booking


def _reload(db, model, pk):
    db.expire_all()
    return db.get(model, pk)


def test_delete_requires_auth(client):
    assert client.delete(URL).status_code == 401


def test_fresh_caregiver_is_erased(db, client):
    cg = make_caregiver(db, date_of_birth=datetime(1990, 1, 1).date(), address="5 Some Road",
                        emergency_contact_name="Next Of Kin", emergency_contact_phone="+2348000000000",
                        emergency_contact_address="6 Other Road",
                        document_verification_result={"id": "x"}, face_verification_result={"ok": True},
                        face_verification_status="verified", onfido_applicant_id="app-1")
    user_id, old_email = cg.user_id, cg.user.email
    cg.user.profile_image_url = None
    db.commit()

    r = client.delete(URL, headers=auth_headers(cg.user))
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"detail"}

    user = _reload(db, User, user_id)
    assert user.is_active is False
    assert user.full_name == "Deleted user"
    assert user.email != old_email and user.email.startswith("deleted+")
    assert user.phone_number is None
    cg = db.scalar(select(Caregiver).where(Caregiver.user_id == user_id))
    assert cg.full_name == "Deleted user" and cg.email == user.email and cg.phone_number is None
    for field in ("date_of_birth", "address", "emergency_contact_name", "emergency_contact_phone",
                  "emergency_contact_address", "document_verification_result", "face_verification_result",
                  "face_verification_status", "onfido_applicant_id"):
        assert getattr(cg, field) is None, field


def test_caregiver_with_history_keeps_the_records_and_loses_the_pii(db, client):
    cg = make_caregiver(db, address="5 Some Road")
    patient = make_patient(db)
    make_caregiver_wallet(db, cg, balance_kobo=0)
    booking = _add_history(db, cg, patient)
    ids = (cg.id, cg.user_id, booking.id)

    r = client.delete(URL, headers=auth_headers(cg.user))
    assert r.status_code == 200, r.text

    db.expire_all()
    assert db.get(Booking, ids[2]) is not None
    assert db.scalar(select(CaregiverTransaction).where(CaregiverTransaction.caregiver_id == ids[0])) is not None
    assert db.get(CaregiverWallet, db.scalar(select(CaregiverWallet.id))) is not None
    assert db.scalar(select(CaregiverBankAccount)) is None  # payout details are PII, the ledger is not
    grants = db.scalars(select(Consent).where(Consent.subject_user_id == ids[1], Consent.decision == "granted")).all()
    assert len(grants) == 1  # the original grant is untouched (append-only)
    withdrawals = db.scalars(select(Consent).where(Consent.subject_user_id == ids[1],
                                                   Consent.decision == "withdrawn")).all()
    assert withdrawals and all(w.granted_by_user_id == ids[1] and w.channel == "account_erasure"
                               for w in withdrawals)
    assert db.get(User, ids[1]).full_name == "Deleted user"
    assert db.scalar(select(Caregiver.address).where(Caregiver.id == ids[0])) is None


def test_transactions_keep_amounts_but_not_account_details(db, client):
    cg = make_caregiver(db)
    make_caregiver_wallet(db, cg, balance_kobo=0)
    db.add(CaregiverTransaction(caregiver_id=cg.id, type="withdrawal", amount=500_000, status="successful",
                                bank_name="Test Bank", account_number="0123456789", account_name="Real Name",
                                reference="WD_1"))
    db.commit()
    assert client.delete(URL, headers=auth_headers(cg.user)).status_code == 200
    db.expire_all()
    tx = db.scalar(select(CaregiverTransaction))
    assert tx.amount == 500_000 and tx.status == "successful" and tx.reference == "WD_1"
    assert tx.account_name is None
    assert tx.account_number == "******6789"


@pytest.mark.parametrize("status", [BookingStatus.ACCEPTED, BookingStatus.EN_ROUTE, BookingStatus.IN_PROGRESS])
def test_caregiver_with_an_active_booking_is_refused(db, client, status):
    cg = make_caregiver(db)
    make_booking(db, make_patient(db), cg, status=status)
    r = client.delete(URL, headers=auth_headers(cg.user))
    assert r.status_code == 409
    assert "booking" in r.json()["detail"].lower()
    db.expire_all()
    assert db.get(User, cg.user_id).is_active is True
    assert db.get(User, cg.user_id).full_name != "Deleted user"


def test_caregiver_with_a_positive_balance_is_refused(db, client):
    cg = make_caregiver(db)
    make_caregiver_wallet(db, cg, balance_kobo=250_000)
    r = client.delete(URL, headers=auth_headers(cg.user))
    assert r.status_code == 409
    assert "withdraw" in r.json()["detail"].lower()
    db.expire_all()
    assert db.get(User, cg.user_id).is_active is True


def test_caregiver_with_a_pending_withdrawal_is_refused(db, client):
    cg = make_caregiver(db)
    make_caregiver_wallet(db, cg, balance_kobo=0)
    db.add(CaregiverTransaction(caregiver_id=cg.id, type="withdrawal", amount=300_000, status="pending",
                                reference="WD_P"))
    db.commit()
    r = client.delete(URL, headers=auth_headers(cg.user))
    assert r.status_code == 409
    assert "payout" in r.json()["detail"].lower()
    db.expire_all()
    assert db.get(User, cg.user_id).is_active is True


def test_patient_with_an_active_paid_booking_is_refused(db, client):
    patient = make_patient(db)
    make_booking(db, patient, make_caregiver(db), status=BookingStatus.ACCEPTED,
                 payment_status=PaymentStatus.PAID)
    r = client.delete(URL, headers=auth_headers(patient.user))
    assert r.status_code == 409
    assert "cancel" in r.json()["detail"].lower()
    db.expire_all()
    assert db.get(User, patient.user_id).is_active is True


def test_patient_with_history_and_an_unpaid_booking_is_erased(db, client):
    patient = make_patient(db, address="1 Road", date_of_birth=datetime(1980, 5, 5).date(),
                           emergency_contact_name="Kin", allergies="penicillin")
    cg = make_caregiver(db)
    done = make_booking(db, patient, cg, status=BookingStatus.COMPLETED)
    unpaid = make_booking(db, patient, None, status=BookingStatus.PENDING, payment_status=PaymentStatus.PENDING)
    r = client.delete(URL, headers=auth_headers(patient.user))
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(Booking, done.id) is not None and db.get(Booking, unpaid.id) is not None
    p = db.scalar(select(Patient).where(Patient.user_id == patient.user_id))
    assert p.full_name == "Deleted user" and p.phone_number is None and p.email.startswith("deleted+")
    for field in ("address", "date_of_birth", "emergency_contact_name", "allergies"):
        assert getattr(p, field) is None, field
    assert db.get(User, patient.user_id).is_active is False


def test_old_tokens_and_login_are_refused_after_deletion(db, client):
    cg = make_caregiver(db)
    email = cg.user.email
    access, refresh = auth_headers(cg.user), refresh_headers(cg.user)
    assert client.delete(URL, headers=access).status_code == 200

    assert client.get("/api/v1/auth/me", headers=access).status_code in (401, 403)
    assert client.delete(URL, headers=access).status_code in (401, 403)  # not a 500 on a repeat
    token = refresh["Authorization"].split()[1]
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": token}).status_code in (400, 401, 403)
    assert client.post("/api/v1/auth/login", data={"username": email, "password": DEFAULT_PASSWORD}
                       ).status_code in (400, 401)
    assert client.post("/api/v1/auth/login/mobile", json={"email_or_phone": email, "password": DEFAULT_PASSWORD}
                       ).status_code in (400, 401)


def test_the_same_email_can_register_again(db, client, sent_emails):
    cg = make_caregiver(db)
    email, phone = cg.user.email, cg.user.phone_number
    assert client.delete(URL, headers=auth_headers(cg.user)).status_code == 200
    r = client.post("/api/v1/auth/register", json={
        "email": email, "phone_number": phone, "full_name": "Back Again", "password": DEFAULT_PASSWORD,
        "user_type": "caregiver"})
    assert r.status_code == 200, r.text
    assert db.query(User).filter(User.email == email).count() == 1


def test_push_tokens_are_deleted(db, client):
    cg = make_caregiver(db)
    db.add(PushToken(user_id=cg.user_id, token=f"ExponentPushToken[{uuid.uuid4().hex}]", platform="android"))
    db.commit()
    assert client.delete(URL, headers=auth_headers(cg.user)).status_code == 200
    assert db.query(PushToken).filter(PushToken.user_id == cg.user_id).count() == 0


def test_profile_image_is_deleted_after_commit_and_a_cloudinary_failure_is_harmless(db, client, monkeypatch):
    from app.api.v1.endpoints import auth as auth_endpoint
    seen = {}

    def fake_delete(public_id):
        db.expire_all()
        seen["erased_when_called"] = db.get(User, cg.user_id).is_active is False  # the commit already happened
        seen["public_id"] = public_id
        raise RuntimeError("cloudinary is down")

    cg = make_caregiver(db)
    cg.user.profile_image_url = "https://res.cloudinary.com/demo/image/upload/v1/profile_images/abc123.jpg"
    db.commit()
    monkeypatch.setattr(auth_endpoint.cloudinary_service, "delete_image", fake_delete)
    r = client.delete(URL, headers=auth_headers(cg.user))
    assert r.status_code == 200, r.text
    assert seen == {"erased_when_called": True, "public_id": "profile_images/abc123"}
    db.expire_all()
    assert db.get(User, cg.user_id).profile_image_url is None


def test_whatsapp_contact_is_unlinked_anonymised_and_opted_out_like_stop_data(db, client):
    patient = make_patient(db)
    contact = make_contact(db, state="idle", user_id=patient.user_id, display_name="Real Name")
    old_wa, old_phone = contact.wa_id, contact.phone_e164
    db.add(Consent(contact_id=contact.id, subject_user_id=patient.user_id, type="terms", decision="granted",
                   policy_version="v1", channel="whatsapp_button"))
    db.commit()

    assert client.delete(URL, headers=auth_headers(patient.user)).status_code == 200

    db.expire_all()
    c = db.get(WhatsAppContact, contact.id)
    assert c.user_id is None and c.display_name is None
    assert c.wa_id != old_wa and c.phone_e164 != old_phone
    assert c.opted_out_at is not None and c.marketing_opt_in is False
    assert db.scalar(select(WhatsAppConversation.state).where(WhatsAppConversation.contact_id == c.id)) == "declined"
    reasons = db.scalars(select(SupportConversation.reason).where(SupportConversation.contact_id == c.id)).all()
    assert reasons == ["data_withdrawal"]
    withdrawn = {x.type for x in db.scalars(select(Consent).where(Consent.contact_id == c.id,
                                                                  Consent.decision == "withdrawn"))}
    assert {"terms", "health_processing", "marketing"} >= withdrawn and "terms" in withdrawn


def test_self_care_recipient_is_anonymised(db, client):
    patient = make_patient(db)
    rec = CareRecipient(full_name="Real Name", phone_e164="+2348011111111", address="1 Road",
                        created_by_user_id=patient.user_id)
    db.add(rec)
    db.flush()
    db.add(CareRelationship(contact_user_id=patient.user_id, recipient_id=rec.id, role="self"))
    db.commit()
    assert client.delete(URL, headers=auth_headers(patient.user)).status_code == 200
    db.expire_all()
    rec = db.get(CareRecipient, rec.id)
    assert rec.full_name == "Deleted user" and rec.phone_e164 is None and rec.address is None
