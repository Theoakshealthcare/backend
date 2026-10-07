"""Test helpers for the admin-editable booking policy (cancellation tiers, drop-out timings)."""

from app.db.models.booking_policy import BookingPolicy


def set_policy(db, **fields) -> None:
    """Write the policy row directly (the tests truncate the table, so the row may not exist yet)."""
    row = db.get(BookingPolicy, 1)
    if row is None:
        row = BookingPolicy(id=1)
        db.add(row)
    for key, value in fields.items():
        setattr(row, key, value)
    db.commit()
