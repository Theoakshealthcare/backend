"""0014_refunds_v2: the refund tables and their constraints, the new payment status, the caregiver owed balance and the new
ledger type. Constraint tests run against the suite database (built by ``alembic upgrade head``)."""

import uuid

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.models.booking import PaymentStatus
from app.db.models.patient import PatientTransaction
from app.db.models.refund import (
    Refund, RefundAttempt, REFUND_COMMITTED_STATUSES, REFUND_OPEN_STATUSES, REFUND_STATUSES,
)
from app.tests.factories import make_booking, make_caregiver, make_caregiver_wallet, make_patient
from app.tests.test_dropout_migration import _run, scratch  # noqa: F401  (scratch is a fixture)

THIS, PREVIOUS = "0014_refunds_v2", "0013_finance_audit_read"


def _refund(booking, status="pending_approval", amount=1000, **kw):
    fields = dict(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                  status=status, amount_kobo=amount)
    fields.update(kw)
    return Refund(**fields)


@pytest.fixture
def booking(db):
    return make_booking(db, make_patient(db))


def test_0014_follows_0013():
    assert ScriptDirectory.from_config(Config("alembic.ini")).get_revision(THIS).down_revision == PREVIOUS


def test_upgrade_downgrade_round_trip(scratch):  # noqa: F811
    _run(scratch, command.upgrade, THIS)
    assert {"refunds", "refund_attempts"} <= set(inspect(scratch).get_table_names())
    _run(scratch, command.downgrade, PREVIOUS)
    names = set(inspect(scratch).get_table_names())
    assert not {"refunds", "refund_attempts"} & names
    assert "owed_kobo" not in {c["name"] for c in inspect(scratch).get_columns("caregiver_wallets")}
    _run(scratch, command.upgrade, THIS)
    assert {"refunds", "refund_attempts"} <= set(inspect(scratch).get_table_names())


def test_status_sets():
    assert set(REFUND_OPEN_STATUSES) == {"pending_approval", "approved", "processing", "needs_attention", "needs_review", "failed"}
    assert set(REFUND_COMMITTED_STATUSES) == {"approved", "processing", "needs_attention", "failed", "needs_review", "succeeded"}
    assert set(REFUND_STATUSES) == set(REFUND_OPEN_STATUSES) | {"succeeded", "rejected", "closed"}


def test_one_open_refund_per_booking(db, booking):
    db.add(_refund(booking))
    db.commit()
    db.add(_refund(booking))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_a_failed_refund_keeps_the_slot(db, booking):
    db.add(_refund(booking, status="failed"))
    db.commit()
    db.add(_refund(booking))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_closed_and_succeeded_free_the_slot(db, booking):
    db.add_all([_refund(booking, status="succeeded"), _refund(booking, status="closed"), _refund(booking, status="rejected")])
    db.commit()
    db.add(_refund(booking))
    db.commit()


@pytest.mark.parametrize("amount", [0, -5])
def test_refund_amount_must_be_positive(db, booking, amount):
    db.add(_refund(booking, amount=amount))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_amount_may_be_null_until_approval(db, booking):
    db.add(_refund(booking, amount=None, suggested_amount_kobo=None))
    db.commit()


@pytest.mark.parametrize("field,value", [
    ("take_back_kobo", -1), ("status", "bogus"), ("reason_code", "bogus"), ("origin", "bogus"), ("destination", "cash"),
])
def test_refund_checks(db, booking, field, value):
    db.add(_refund(booking, **{field: value}))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_defaults_and_attempts(db, booking):
    r = _refund(booking)
    db.add(r)
    db.commit()
    assert r.take_back_kobo == 0 and r.take_back_from_caregiver is False and r.destination is None
    assert r.current_attempt is None
    db.add_all([RefundAttempt(refund_id=r.id, attempt=1, status="failed"), RefundAttempt(refund_id=r.id, attempt=2, status="sending")])
    db.commit()
    db.refresh(r)
    assert [a.attempt for a in r.attempts] == [1, 2] and r.current_attempt.attempt == 2
    db.add(RefundAttempt(refund_id=r.id, attempt=2, status="sending"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_attempt_status_check(db, booking):
    r = _refund(booking)
    db.add(r)
    db.commit()
    db.add(RefundAttempt(refund_id=r.id, attempt=1, status="bogus"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_owed_kobo_cannot_go_negative(db):
    wallet = make_caregiver_wallet(db, make_caregiver(db))
    assert wallet.owed_kobo == 0
    wallet.owed_kobo = -1
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_payment_status_accepts_partially_refunded(db):
    b = make_booking(db, make_patient(db), payment_status=PaymentStatus.PARTIALLY_REFUNDED)
    db.refresh(b)
    assert b.payment_status == PaymentStatus.PARTIALLY_REFUNDED
    assert b.paystack_fee_kobo is None


def test_ledger_accepts_refund_take_back_type(db):
    cg = make_caregiver(db)
    db.execute(text("INSERT INTO caregiver_transactions (id, caregiver_id, type, amount, status) "
                    "VALUES (:i, :c, 'refund_take_back', 100, 'successful')"), {"i": uuid.uuid4(), "c": cg.id})
    db.commit()
    with pytest.raises(IntegrityError):
        db.execute(text("INSERT INTO caregiver_transactions (id, caregiver_id, type, amount, status) "
                        "VALUES (:i, :c, 'nonsense', 100, 'successful')"), {"i": uuid.uuid4(), "c": cg.id})
        db.commit()
    db.rollback()


def test_patient_transaction_has_refund_id():
    assert "refund_id" in PatientTransaction.__table__.columns


def test_several_take_back_rows_per_booking_but_one_earning(db, booking):
    cg = make_caregiver(db)

    def insert(kind):
        db.execute(text("INSERT INTO caregiver_transactions (id, caregiver_id, booking_id, type, amount, status) "
                        "VALUES (:i, :c, :b, :t, 100, 'successful')"),
                   {"i": uuid.uuid4(), "c": cg.id, "b": booking.id, "t": kind})
        db.commit()

    insert("refund_take_back")
    insert("refund_take_back")
    insert("earning")
    with pytest.raises(IntegrityError):
        insert("earning")
    db.rollback()


def test_0015_event_check_matches_the_model_and_round_trips(scratch):  # noqa: F811
    import importlib.util
    from pathlib import Path
    from app.db.models.whatsapp_booking import BOOKING_EVENTS
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0015_refund_bank_details_event.py"
    spec = importlib.util.spec_from_file_location("mig0015", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert set(mig.NEW_EVENTS) == set(BOOKING_EVENTS)
    _run(scratch, command.upgrade, "0015_refund_bank_details")
    _run(scratch, command.downgrade, THIS)
    _run(scratch, command.upgrade, "0015_refund_bank_details")
