"""Shared assertions for the Refunds v2 tests. Later tasks extend this."""
from sqlalchemy import func

from app.db.models.booking import Booking
from app.db.models.refund import REFUND_COMMITTED_STATUSES, Refund


def assert_refund_conserved(db, booking) -> None:
    """Money out can never exceed money in: the committed refunds of a booking sum to at most its total."""
    db.expire_all()
    total = db.get(Booking, booking.id).total_amount_kobo
    committed = (db.query(func.coalesce(func.sum(Refund.amount_kobo), 0))
                 .filter(Refund.booking_id == booking.id, Refund.status.in_(REFUND_COMMITTED_STATUSES)).scalar())
    assert int(committed) <= int(total), f"over-refunded: {committed} > {total}"


# --- ground truth for the sequence and race tests ------------------------------------------------------------------
# The fake Paystack is the truth about what left Oakly through Paystack; the patient wallet ledger is the truth about what
# was credited. Neither is read from the refund rows, so a bug that makes our own books agree with themselves cannot hide.
PAYSTACK_LIVE = ("pending", "processing", "needs-attention", "processed")


def paystack_processed_kobo(fake, reference: str) -> int:
    """Σ amount of the fake Paystack's refunds on ``reference`` that are ``processed`` right now."""
    return sum(int(r["amount"]) for r in fake.refunds.values()
               if r["transaction_reference"] == reference and r["status"] == "processed")


def paystack_live_kobo(fake, reference: str) -> int:
    """Σ amount of the fake Paystack's refunds on ``reference`` that are processed or could still become so (not failed)."""
    return sum(int(r["amount"]) for r in fake.refunds.values()
               if r["transaction_reference"] == reference and r["status"] in PAYSTACK_LIVE)


def wallet_credits_kobo(db, patient_id) -> int:
    """Σ the refund credits on the patient's Oakly wallet ledger."""
    from app.db.models.patient import PatientTransaction
    db.expire_all()
    return int(db.query(func.coalesce(func.sum(PatientTransaction.amount_kobo), 0))
               .filter(PatientTransaction.patient_id == patient_id, PatientTransaction.type == "refund",
                       PatientTransaction.refund_id.isnot(None)).scalar())


def refunds_of(db, booking) -> list:
    db.expire_all()
    return db.query(Refund).filter(Refund.booking_id == booking.id).order_by(Refund.created_at).all()


def is_flagged(refund) -> bool:
    return refund.status == "needs_review" or refund.review_flagged_at is not None


def money_moved_by(db, refund) -> tuple:
    """Everything the system has done with money for one refund: attempts sent, wallet credits, caregiver take-back rows and the
    amounts the refund itself records. Two equal snapshots mean no money moved between them."""
    from app.db.models.caregiver import CaregiverTransaction
    from app.db.models.patient import PatientTransaction
    from app.db.models.refund import RefundAttempt
    db.expire_all()
    r = db.get(Refund, refund.id)
    attempts = db.query(RefundAttempt).filter_by(refund_id=r.id).count()
    credits = db.query(PatientTransaction).filter_by(refund_id=r.id).count()
    take_backs = db.query(CaregiverTransaction).filter(CaregiverTransaction.type == "refund_take_back",
                                                       CaregiverTransaction.status_reason.contains(str(r.id))).count()
    return (attempts, credits, take_backs, r.amount_kobo, r.destination, r.processed_at, r.take_back_debited_kobo,
            r.take_back_owed_kobo)
