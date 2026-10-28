"""Caregivers can't mark their own identity as verified (WP-C review finding).

The client-posted result endpoint used to be a non-authoritative stub; since K2 it answers 410 and reads nothing. The status
changes that remain open to a caregiver are covered in ``test_kyc_caregiver_api.py``.
"""

import pytest

from app.tests.factories import auth_headers, make_caregiver, make_patient, refresh_headers

URL = "/api/v1/caregiver/identity-verification/result"


def post(client, user, vtype, status):
    return client.post(
        URL, json={"type": vtype, "result": {"status": status, "ref": "abc"}}, headers=auth_headers(user)
    )


@pytest.mark.parametrize("vtype", ["document", "face"])
@pytest.mark.parametrize("claimed", ["verified", "failed", "approved", "anything"])
@pytest.mark.parametrize("identity", ["pending", "in_progress", "needs_review", "verified", "failed"])
def test_a_client_result_changes_nothing(db, client, identity, claimed, vtype):
    caregiver = make_caregiver(db, approved=False, identity_verification_status=identity, profile_completion_step=2)
    assert post(client, caregiver.user, vtype, claimed).status_code == 410
    db.refresh(caregiver)
    assert caregiver.identity_verification_status == identity
    assert caregiver.profile_completion_step == 2


def test_requires_token_and_caregiver_role(db, client):
    patient = make_patient(db)
    caregiver = make_caregiver(db)
    body = {"type": "document", "result": {"status": "verified"}}
    assert client.post(URL, json=body).status_code == 401
    assert client.post(URL, json=body, headers=refresh_headers(caregiver.user)).status_code == 401
    assert client.post(URL, json=body, headers=auth_headers(patient.user)).status_code == 403
