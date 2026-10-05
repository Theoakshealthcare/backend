"""Self-tests for the harness: real DB, real auth, isolation, and the network guard."""

import pytest

from app.db.models.user import User
from app.tests.guards import NetworkBlocked
from app.tests.factories import auth_headers, make_patient


def test_health(client):
    assert client.get("/health").json() == {"status": "healthy"}


def test_authenticated_request_hits_test_database(db, client):
    patient = make_patient(db)
    r = client.get("/api/v1/auth/me", headers=auth_headers(patient.user))
    assert r.status_code == 200
    assert r.json()["email"] == patient.email


def test_tables_are_empty_at_start_of_each_test(db):
    assert db.query(User).count() == 0


def test_outbound_http_is_blocked():
    import requests

    with pytest.raises(NetworkBlocked):
        requests.get("https://api.paystack.co")


def test_smtp_is_blocked():
    import smtplib

    with pytest.raises(NetworkBlocked):
        smtplib.SMTP_SSL("smtp.example.test", 465)
