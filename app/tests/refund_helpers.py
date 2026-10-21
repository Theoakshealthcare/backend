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
