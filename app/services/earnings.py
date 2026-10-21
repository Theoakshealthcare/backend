"""Caregiver earnings (AD-FR-38): credited when a visit is completed, held for a dispute window, then released.

Money flow, all integer kobo:

1. **Credit.** Completing a visit creates ONE ``earning`` transaction for the assigned caregiver: the booking's
   ``service_fee_kobo`` x the visit share percent, rounded half up. The base is the **service fee** (what the caregiver's
   hours are priced at), never ``total_amount_kobo`` and never the platform fee. The row is ``pending_clearance`` with
   ``available_at = completed_at + hold``. The wallet balance does not move.
2. **Late-cancel compensation.** A family cancellation in the late tier that was compensable (the caregiver set out in
   good time and really moved) creates ONE ``late_cancel_compensation`` for the fee actually kept x the late-fee share
   percent, pending with the same hold.
3. **Release.** ``release_due`` (the ARQ job) moves ``pending_clearance`` rows whose ``available_at`` has passed to
   ``successful`` and adds the amount to ``caregiver_wallets.balance``, the withdrawable (available) balance.
4. **Admin.** A pending row can be put on ``held`` (the release job skips it), taken off hold, or ``voided`` (never paid).
   A released row cannot be changed here: taking money back is a ledger adjustment (AD-FR-42).

The percent and hold come from the booking's own ``earnings_terms`` snapshot (taken when the booking was created, like the
cancellation terms), else the live policy. Idempotency has two layers: the credit functions look for the row first (they
run under the booking row lock), and ``uq_caregiver_transactions_booking_type`` refuses a second one whatever the caller.

Lock order: booking, then transaction row, then wallet. The credit functions take no wallet lock (nothing moves).
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple
from uuid import UUID

from fastapi import Request
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db.models.booking import Booking
from app.db.models.caregiver import (
    EARNING_TYPES,
    TX_EARNING,
    TX_HELD,
    TX_LATE_CANCEL,
    TX_PENDING_CLEARANCE,
    TX_REFUND_TAKE_BACK,
    TX_SUCCESSFUL,
    TX_VOIDED,
    CaregiverTransaction,
    CaregiverWallet,
)
from app.services import booking_policy
from app.services.audit import AuditService

logger = logging.getLogger(__name__)

RELEASE_BATCH = 200


class EarningNotFound(Exception):
    pass


class EarningConflict(Exception):
    """The earning is not in a state that allows the action; the message says why."""


class EarningForbidden(Exception):
    """The caller lacks the permission this particular change needs; the message says which."""


NO_SHOW_HOLD_REASON = "No-show flagged on this booking"


@dataclass(frozen=True)
class Terms:
    visit_share_percent: int
    hold_hours: int
    late_fee_share_percent: int


def terms_for(db: Session, booking: Booking) -> Terms:
    """The booking's own snapshot, else (an old booking, or a damaged copy) the live policy."""
    snap = booking.earnings_terms
    try:
        return Terms(**{key: int(snap[key]) for key in ("visit_share_percent", "hold_hours", "late_fee_share_percent")})  # type: ignore[index]
    except (TypeError, KeyError, ValueError):
        pass
    return Terms(**booking_policy.earnings_snapshot(booking_policy.current(db)))


def share(base_kobo: int, percent: int) -> int:
    """``percent`` of ``base_kobo``, integer kobo, half up."""
    return (int(base_kobo) * int(percent) + 50) // 100


def _existing(db: Session, booking_id: UUID, tx_type: str) -> Optional[CaregiverTransaction]:
    return (db.query(CaregiverTransaction)
            .filter(CaregiverTransaction.booking_id == booking_id, CaregiverTransaction.type == tx_type).first())


def _credit(db: Session, booking: Booking, *, tx_type: str, base_kobo: int, percent: int, hold_hours: int, now: datetime,
            narration: str, held_reason: Optional[str] = None,
            held_by: Optional[UUID] = None) -> Optional[CaregiverTransaction]:
    if booking.caregiver_id is None:
        return None
    existing = _existing(db, booking.id, tx_type)
    if existing is not None:
        return existing
    tx = CaregiverTransaction(
        caregiver_id=booking.caregiver_id, booking_id=booking.id, type=tx_type, amount=share(base_kobo, percent),
        status=TX_HELD if held_reason else TX_PENDING_CLEARANCE, base_amount_kobo=int(base_kobo), share_percent=percent,
        available_at=now + timedelta(hours=hold_hours), reference=booking.booking_code, narration=narration,
        status_reason=held_reason, status_changed_by=held_by if held_reason else None,
        status_changed_at=now if held_reason else None)
    db.add(tx)
    db.flush()  # the unique constraint speaks here, inside the caller's transaction
    if held_reason:
        _audit_created_held(db, tx, held_reason, held_by)
    logger.info("earning credited: booking %s, %s %s kobo, available at %s", booking.booking_code, tx_type, tx.amount,
                tx.available_at)
    return tx


def _no_show_hold(booking: Booking) -> Dict[str, Any]:
    """The held_reason / held_by to credit with when an admin has flagged the booking as a no-show, else nothing."""
    if booking.no_show_flagged_at is None:
        return {}
    return {"held_reason": NO_SHOW_HOLD_REASON, "held_by": booking.no_show_flagged_by}


def _audit_created_held(db: Session, tx: CaregiverTransaction, reason: str, admin_id: Optional[UUID]) -> None:
    """An earning born held leaves the same audit row the admin hold endpoint writes, attributed to the admin who flagged."""
    if admin_id is None:
        logger.warning("earning %s created held (%s) but the flagging admin is unknown: no audit row", tx.id, reason)
        return
    AuditService(db).record(admin_user_id=admin_id, action="earning.hold", entity_type="caregiver_transaction",
                            entity_id=tx.id, before=None, after=_snapshot(tx), reason=reason)


def credit_visit(db: Session, booking: Booking, now: Optional[datetime] = None) -> Optional[CaregiverTransaction]:
    """Credit the assigned caregiver for a completed visit. The caller holds the booking lock and owns the transaction;
    nothing is committed here. Calling it again for the same booking returns the first row. A booking an admin flagged as
    a no-show (and did not clear by reassigning) is credited already ``held``: the money is recorded but cannot clear
    until Finance looks at it."""
    now = now or booking.actual_end_time or datetime.utcnow()
    terms = terms_for(db, booking)
    return _credit(db, booking, tx_type=TX_EARNING, base_kobo=booking.service_fee_kobo, percent=terms.visit_share_percent,
                   hold_hours=terms.hold_hours, now=now, narration=f"Visit {booking.booking_code}",
                   **_no_show_hold(booking))


def credit_late_cancel(db: Session, booking: Booking, fee_kobo: int,
                       now: Optional[datetime] = None) -> Optional[CaregiverTransaction]:
    """Compensate the caregiver of a compensable late cancellation: a share of the fee the family was charged. Only when
    a fee was actually kept. Same locking and idempotency as ``credit_visit``, and the same rule for a no-show-flagged
    booking: credited already ``held``."""
    if fee_kobo <= 0:
        return None
    terms = terms_for(db, booking)
    return _credit(db, booking, tx_type=TX_LATE_CANCEL, base_kobo=fee_kobo, percent=terms.late_fee_share_percent,
                   hold_hours=terms.hold_hours, now=now or datetime.utcnow(),
                   narration=f"Late cancellation {booking.booking_code}", **_no_show_hold(booking))


# --- balances ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Balances:
    available_kobo: int
    pending_kobo: int
    held_kobo: int
    next_available_at: Optional[datetime]


def balances(db: Session, caregiver_id: UUID, wallet: Optional[CaregiverWallet] = None) -> Balances:
    if wallet is None:
        wallet = db.query(CaregiverWallet).filter(CaregiverWallet.caregiver_id == caregiver_id).first()
    rows = dict(
        db.query(CaregiverTransaction.status, func.coalesce(func.sum(CaregiverTransaction.amount), 0))
        .filter(CaregiverTransaction.caregiver_id == caregiver_id, CaregiverTransaction.type.in_(EARNING_TYPES),
                CaregiverTransaction.status.in_((TX_PENDING_CLEARANCE, TX_HELD)))
        .group_by(CaregiverTransaction.status).all())
    next_at = (db.query(func.min(CaregiverTransaction.available_at))
               .filter(CaregiverTransaction.caregiver_id == caregiver_id, CaregiverTransaction.type.in_(EARNING_TYPES),
                       CaregiverTransaction.status == TX_PENDING_CLEARANCE).scalar())
    return Balances(available_kobo=int(wallet.balance or 0) if wallet else 0,
                    pending_kobo=int(rows.get(TX_PENDING_CLEARANCE, 0)), held_kobo=int(rows.get(TX_HELD, 0)),
                    next_available_at=next_at)


# --- release (the worker job) --------------------------------------------------------------------------------------


def locked_wallet(db: Session, caregiver_id: UUID) -> CaregiverWallet:
    """The caregiver's wallet row, created if missing, locked FOR UPDATE until the transaction ends. Never ``.refresh()`` it
    afterwards (that would be a fresh, non-locking SELECT)."""
    db.execute(pg_insert(CaregiverWallet).values(caregiver_id=caregiver_id, balance=0).on_conflict_do_nothing(
        index_elements=["caregiver_id"]))
    return (db.query(CaregiverWallet).filter(CaregiverWallet.caregiver_id == caregiver_id)
            .with_for_update().populate_existing().one())


_locked_wallet = locked_wallet


def release_due(db: Session, now: Optional[datetime] = None, limit: int = RELEASE_BATCH) -> int:
    """Move earnings whose hold has passed to the available balance. Safe to run in parallel and to repeat: a row another
    worker or an admin holds locked is skipped (SKIP LOCKED), only ``pending_clearance`` rows qualify (held, voided and
    already released ones never do), and each row is released once. Commits per batch."""
    now = now or datetime.utcnow()
    released = 0
    while True:
        rows = (db.query(CaregiverTransaction)
                .filter(CaregiverTransaction.status == TX_PENDING_CLEARANCE, CaregiverTransaction.type.in_(EARNING_TYPES),
                        CaregiverTransaction.available_at <= now)
                .order_by(CaregiverTransaction.available_at, CaregiverTransaction.id)
                .with_for_update(skip_locked=True).populate_existing().limit(limit).all())
        if not rows:
            db.rollback()
            return released
        try:
            by_caregiver: Dict[UUID, list] = {}
            for tx in rows:
                tx.status = TX_SUCCESSFUL
                tx.released_at = now
                by_caregiver.setdefault(tx.caregiver_id, []).append(tx)
            db.flush()
            # Wallet rows in a fixed order (two workers never wait on each other in a cycle), each credited once under its
            # row lock. What a refund take-back still owes is netted off each released earning first, so the caregiver is
            # only ever credited LESS than the earning (never more), and the part kept is written as a take-back row.
            for caregiver_id in sorted(by_caregiver, key=str):
                wallet = _locked_wallet(db, caregiver_id)
                credit = 0
                for tx in by_caregiver[caregiver_id]:
                    amount = int(tx.amount)
                    taken = recover_owed(db, wallet, amount, why=f"recovered from release of {tx.id}", now=now,
                                         reference=tx.reference)
                    credit += amount - taken
                db.execute(update(CaregiverWallet).where(CaregiverWallet.caregiver_id == caregiver_id)
                           .values(balance=func.coalesce(CaregiverWallet.balance, 0) + credit, owed_kobo=wallet.owed_kobo)
                           .execution_options(synchronize_session=False))
            db.commit()
        except Exception:
            db.rollback()
            raise
        released += len(rows)
        if len(rows) < limit:
            return released


def recover_owed(db: Session, wallet: CaregiverWallet, available_kobo: int, *, why: str, now: datetime,
                 reference: Optional[str] = None) -> int:
    """Net a caregiver's ``owed_kobo`` off ``available_kobo`` that is about to reach (or already is in) their balance and
    return what was taken. Writes the ``refund_take_back`` recovery row and lowers ``owed_kobo`` on the (locked) wallet
    object; the caller applies the balance change. Decrease-only: it can only take, never give."""
    owed = int(wallet.owed_kobo or 0)
    taken = min(max(int(available_kobo), 0), owed)
    if taken <= 0:
        return 0
    wallet.owed_kobo = owed - taken
    db.add(CaregiverTransaction(
        caregiver_id=wallet.caregiver_id, type=TX_REFUND_TAKE_BACK, amount=taken, status=TX_SUCCESSFUL, booking_id=None,
        released_at=now, reference=reference, narration="Refund recovered from earnings", status_reason=why))
    return taken


def apply_take_back(db: Session, *, caregiver_id: Optional[UUID] = None, booking_id: UUID, refund_id: UUID, amount_kobo: int,
                    now: datetime) -> Tuple[int, int]:
    """Take a refunded visit's share back from what the caregiver actually received. Returns ``(debited_kobo,
    owed_added_kobo)``. It re-reads the booking's ``earning`` row under its lock at success time (ledger rows before
    wallets, the lock order) and follows it:

    * missing or ``voided``: the caregiver never got the money, nothing is taken;
    * ``pending_clearance`` or ``held`` (not yet released): the EARNING is lowered by the take-back (decrease-only; it then
      releases or voids at the lower amount). No wallet debit and nothing owed. A ``voided`` ``refund_take_back`` row records
      the reduction (it moves no money, so it is neither a debit nor an earning and ``balances()`` ignores it);
    * ``successful`` (released): the available balance is DEBITED up to the take-back and the rest is ``owed_kobo``,
      recovered by ``release_due`` and at payout reservation.

    The earning row's caregiver is used, not the booking's. It never credits, never takes the balance below zero, never
    touches a payout (a reservation already left the balance) and never freezes payouts. Nothing is committed; the caller
    holds the booking and refund locks."""
    amount = int(amount_kobo)
    if amount <= 0:
        return 0, 0
    earning = (db.query(CaregiverTransaction)
               .filter(CaregiverTransaction.booking_id == booking_id, CaregiverTransaction.type == TX_EARNING)
               .with_for_update().populate_existing().first())
    if earning is None or earning.status == TX_VOIDED:
        logger.warning("refund %s take-back of %s kobo skipped: the visit earning is %s", refund_id, amount,
                       "missing" if earning is None else "voided")
        return 0, 0
    if earning.status in (TX_PENDING_CLEARANCE, TX_HELD):
        reduced = min(amount, int(earning.amount))
        earning.amount = int(earning.amount) - reduced
        if reduced > 0:
            db.add(CaregiverTransaction(
                caregiver_id=earning.caregiver_id, type=TX_REFUND_TAKE_BACK, amount=reduced, status=TX_VOIDED,
                booking_id=booking_id, narration="Refund reduced a visit's earning before it was released",
                status_reason=f"reduced unreleased earning {earning.id} for refund {refund_id}"))
        db.flush()
        logger.info("refund %s take-back: unreleased earning %s lowered by %s kobo", refund_id, earning.id, reduced)
        return 0, 0
    wallet = locked_wallet(db, earning.caregiver_id)
    debited = min(max(int(wallet.balance or 0), 0), amount)
    owed_added = amount - debited
    if debited > 0:
        db.add(CaregiverTransaction(
            caregiver_id=earning.caregiver_id, type=TX_REFUND_TAKE_BACK, amount=debited, status=TX_SUCCESSFUL,
            booking_id=booking_id, released_at=now, narration="Refund taken back from a visit's earning",
            status_reason=f"refund {refund_id}"))
    db.execute(update(CaregiverWallet).where(CaregiverWallet.caregiver_id == earning.caregiver_id)
               .values(balance=func.coalesce(CaregiverWallet.balance, 0) - debited,
                       owed_kobo=func.coalesce(CaregiverWallet.owed_kobo, 0) + owed_added)
               .execution_options(synchronize_session=False))
    db.flush()
    logger.info("refund %s take-back: %s kobo debited, %s kobo owed", refund_id, debited, owed_added)
    return debited, owed_added


# --- admin: hold, release the hold, void ---------------------------------------------------------------------------


def _lock_tx(db: Session, tx_id: UUID) -> CaregiverTransaction:
    tx = (db.query(CaregiverTransaction)
          .filter(CaregiverTransaction.id == tx_id, CaregiverTransaction.type.in_(EARNING_TYPES))
          .with_for_update().populate_existing().first())
    if tx is None:
        db.rollback()
        raise EarningNotFound()
    return tx


def _snapshot(tx: CaregiverTransaction) -> Dict[str, Any]:
    return {"status": tx.status, "amount": tx.amount, "available_at": tx.available_at, "booking_id": tx.booking_id,
            "caregiver_id": tx.caregiver_id, "type": tx.type}


def _change(db: Session, tx: CaregiverTransaction, *, allowed_from: tuple, to: str, action: str, admin_id: UUID,
            reason: str, request: Optional[Request], now: Optional[datetime] = None) -> CaregiverTransaction:
    if tx.status not in allowed_from:
        db.rollback()
        raise EarningConflict(_why(tx.status, action))
    before = _snapshot(tx)
    tx.status = to
    tx.status_reason = reason
    tx.status_changed_by = admin_id
    tx.status_changed_at = now or datetime.utcnow()
    AuditService(db).record(admin_user_id=admin_id, action=action, entity_type="caregiver_transaction", entity_id=tx.id,
                            before=before, after=_snapshot(tx), reason=reason, request=request)
    return tx


def _why(status: str, action: str) -> str:
    if status == TX_SUCCESSFUL:
        return "This earning has already been released to the caregiver's balance; correct it with a ledger adjustment"
    if status == TX_VOIDED:
        return "This earning has been voided"
    if action == "earning.hold" and status == TX_HELD:
        return "This earning is already on hold"
    if action == "earning.release_hold":
        return "This earning is not on hold"
    return f"This earning cannot be changed while it is {status}"


def hold(db: Session, tx_id: UUID, *, admin_id: UUID, reason: str, request: Optional[Request] = None) -> CaregiverTransaction:
    tx = _lock_tx(db, tx_id)
    try:
        _change(db, tx, allowed_from=(TX_PENDING_CLEARANCE,), to=TX_HELD, action="earning.hold", admin_id=admin_id,
                reason=reason, request=request)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return tx


def release_hold(db: Session, tx_id: UUID, *, admin_id: UUID, reason: str, request: Optional[Request] = None,
                 can_clear_no_show: bool = False) -> CaregiverTransaction:
    """Take a hold off: the earning is pending again and the release job pays it as soon as its time has passed. While
    the booking is still flagged as a no-show, only a caller with ``can_clear_no_show`` (``ledger.adjust``) may do it:
    ``ledger.adjust_request`` is enough for an ordinary hold but not to pay out money a no-show flag stopped."""
    tx = _lock_tx(db, tx_id)
    try:
        if not can_clear_no_show and tx.booking_id is not None:
            flagged = (db.query(Booking.no_show_flagged_at).filter(Booking.id == tx.booking_id).scalar()) is not None
            if flagged:
                db.rollback()
                raise EarningForbidden("This booking is flagged as a no-show; releasing its hold needs the ledger.adjust "
                                       "permission")
        _change(db, tx, allowed_from=(TX_HELD,), to=TX_PENDING_CLEARANCE, action="earning.release_hold",
                admin_id=admin_id, reason=reason, request=request)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return tx


def void(db: Session, tx_id: UUID, *, admin_id: UUID, reason: str, request: Optional[Request] = None) -> CaregiverTransaction:
    tx = _lock_tx(db, tx_id)
    try:
        _change(db, tx, allowed_from=(TX_PENDING_CLEARANCE, TX_HELD), to=TX_VOIDED, action="earning.void",
                admin_id=admin_id, reason=reason, request=request)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return tx


def hold_pending_for_booking(db: Session, booking: Booking, *, admin_id: UUID, reason: str,
                             request: Optional[Request] = None) -> int:
    """A no-show flag holds the booking's pending earning so it cannot clear while support looks into it. Runs in the
    caller's transaction (which holds the booking lock): nothing is committed. Returns how many rows were held; an
    earning that is already released or held is left alone. Today the flag is only offered before a visit has started, so
    there is normally no earning yet; ``credit_visit`` then credits a flagged booking already held."""
    held = 0
    rows = (db.query(CaregiverTransaction)
            .filter(CaregiverTransaction.booking_id == booking.id, CaregiverTransaction.type.in_(EARNING_TYPES),
                    CaregiverTransaction.status == TX_PENDING_CLEARANCE)
            .with_for_update().populate_existing().all())
    for tx in rows:
        _change(db, tx, allowed_from=(TX_PENDING_CLEARANCE,), to=TX_HELD, action="earning.hold", admin_id=admin_id,
                reason=f"No-show flagged: {reason}", request=request)
        held += 1
    return held
