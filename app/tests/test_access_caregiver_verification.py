"""Caregivers can't mark their own identity as verified (WP-C review finding)."""

import pytest

from app.tests.factories import auth_headers, make_caregiver, make_patient, refresh_headers

URL = "/api/v1/caregiver/identity-verification/result"


def post(client, user, vtype, status):
    return client.post(
        URL, json={"type": vtype, "result": {"status": status, "ref": "abc"}}, headers=auth_headers(user)
    )


@pytest.mark.parametrize("claimed", ["verified", "failed", "approved", "anything"])
def test_client_cannot_set_document_status_beyond_in_progress(db, client, claimed):
    caregiver = make_caregiver(db, approved=False)
    assert post(client, caregiver.user, "document", claimed).status_code == 200
    db.refresh(caregiver)
    assert caregiver.identity_verification_status == "in_progress"
    assert not hasattr(caregiver, "document_verification_result")  # the payload is not stored anywhere


@pytest.mark.parametrize("claimed", ["verified", "failed"])
def test_client_cannot_set_the_identity_status_with_a_face_result(db, client, claimed):
    caregiver = make_caregiver(db, approved=False)
    before = caregiver.identity_verification_status
    assert post(client, caregiver.user, "face", claimed).status_code == 200
    db.refresh(caregiver)
    assert caregiver.identity_verification_status == before
    assert caregiver.profile_completion_step >= 3


def test_resubmitting_cannot_overwrite_an_admin_decision(db, client):
    caregiver = make_caregiver(db, approved=True)  # identity already "verified"
    assert post(client, caregiver.user, "document", "failed").status_code == 200
    db.refresh(caregiver)
    assert caregiver.identity_verification_status == "verified"


def test_resubmitting_cannot_pull_a_caregiver_out_of_review(db, client):
    caregiver = make_caregiver(db, approved=False, identity_verification_status="needs_review")
    assert post(client, caregiver.user, "document", "verified").status_code == 200
    db.refresh(caregiver)
    assert caregiver.identity_verification_status == "needs_review"


def test_requires_token_and_caregiver_role(db, client):
    patient = make_patient(db)
    caregiver = make_caregiver(db)
    body = {"type": "document", "result": {"status": "verified"}}
    assert client.post(URL, json=body).status_code == 401
    assert client.post(URL, json=body, headers=refresh_headers(caregiver.user)).status_code == 401
    assert client.post(URL, json=body, headers=auth_headers(patient.user)).status_code == 403
