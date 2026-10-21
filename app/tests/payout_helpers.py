"""Test helpers for caregiver payouts: a caregiver who can be paid, and the money invariant."""

import hashlib
import hmac
import json
import uuid
from datetime import datetime
from typing import Optional

from app.core.config import settings
from app.db.models.caregiver import CaregiverBankAccount, CaregiverTransaction, CaregiverWallet
from app.db.models.payout import Payout
from app.tests.factories import make_caregiver

WEBHOOK = "/api/v1/payment/paystack/webhook"
RESERVED = ("pending_approval", "processing", "success")


def payable(db, balance_kobo: int = 1_000_000, *, fake=None, bank: bool = True, number: Optional[str] = None,
            blocked_until: Optional[datetime] = None, **caregiver_fields):
    """A caregiver with an available balance and a verified bank account (a Paystack recipient). With ``fake`` the
    recipient also exists on the fake Paystack, so a transfer to it is accepted."""
    cg = make_caregiver(db, **caregiver_fields)
    db.add(CaregiverWallet(caregiver_id=cg.id, balance=balance_kobo))
    if bank:
        number = number or f"{uuid.uuid4().int % 10**10:010d}"
        code = "RCP_" + uuid.uuid4().hex[:12]
        db.add(CaregiverBankAccount(
            caregiver_id=cg.id, bank_name="Placeholder Bank", bank_code="058", account_number=number,
            account_name="PLACEHOLDER ACCOUNT", recipient_code=code, verified_at=datetime.utcnow(),
            payout_blocked_until=blocked_until))
        if fake is not None:
            fake.recipients[code] = {"account_number": number, "bank_code": "058"}
    db.commit()
    return cg


def wallet_balance(db, cg) -> int:
    db.expire_all()
    return db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).one().balance


def payouts_of(db, cg=None, **filters):
    db.expire_all()
    q = db.query(Payout)
    if cg is not None:
        q = q.filter_by(caregiver_id=cg.id)
    return q.filter_by(**filters).order_by(Payout.created_at).all()


def ledger(db, cg, type="payout"):
    db.expire_all()
    return db.query(CaregiverTransaction).filter_by(caregiver_id=cg.id, type=type).order_by(CaregiverTransaction.created_at).all()


def assert_conserved(db, cg, initial_kobo: int) -> None:
    """The money invariant: the available balance plus everything reserved or paid out is exactly what the caregiver had."""
    reserved = sum(p.amount_kobo for p in payouts_of(db, cg) if p.status in RESERVED)
    assert wallet_balance(db, cg) + reserved == initial_kobo, "money was created or lost"


def signed(body: dict):
    raw = json.dumps(body).encode()
    sig = hmac.new(settings.PAYSTACK_SECRET_KEY.encode(), raw, hashlib.sha512).hexdigest()
    return {"content": raw, "headers": {"x-paystack-signature": sig, "Content-Type": "application/json"}}


def money_state(db, cg) -> dict:
    """Where the caregiver's money is, read from the database. ``paid`` counts every Paystack transfer that succeeded (one per
    successful attempt); ``reserved`` is money held for a payout that has no successful attempt; ``payable`` is what automatic
    code could still send them: the available balance plus payouts waiting to be approved or sent; ``unsent`` maps the payouts
    that have not been sent to Paystack to their amounts."""
    from app.db.models.payout import PayoutAttempt
    db.expire_all()
    paid = reserved = shortfall = payable = 0
    unsent = {}
    for p in db.query(Payout).filter_by(caregiver_id=cg.id).all():
        succeeded = db.query(PayoutAttempt).filter_by(payout_id=p.id, status="success").count()
        tx = db.get(CaregiverTransaction, p.transaction_id)
        paid += succeeded * p.amount_kobo
        if succeeded == 0 and tx.status in ("pending_approval", "pending"):
            reserved += p.amount_kobo
        shortfall += p.shortfall_kobo
        if p.status in ("pending_approval", "processing"):
            payable += p.amount_kobo
            if p.status == "pending_approval" or p.initiated_at is None:
                unsent[p.id] = p.amount_kobo
    wallet = db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).one()
    return {"available": wallet.balance, "paid": paid, "reserved": reserved, "shortfall": shortfall,
            "payable": payable + wallet.balance, "frozen": wallet.payouts_frozen_at is not None, "unsent": unsent}


def cancelled_unsent_kobo(db, cg, before: dict) -> int:
    """How much of the payouts that were not yet sent in ``before`` (a money_state) has since been cancelled."""
    db.expire_all()
    return sum(amount for payout_id, amount in before["unsent"].items() if db.get(Payout, payout_id).status == "cancelled")


def taken_back_kobo(db, cg) -> int:
    """What refund take-backs have already debited from the caregiver (the ``refund_take_back`` ledger rows)."""
    from sqlalchemy import func
    db.expire_all()
    return int(db.query(func.coalesce(func.sum(CaregiverTransaction.amount), 0))
               .filter_by(caregiver_id=cg.id, type="refund_take_back", status="successful").scalar())


def assert_never_over(db, cg, earned_kobo: int) -> dict:
    """THE INVARIANT: what Paystack has paid (less any shortfall that is on record), plus the available balance, plus what is
    reserved, never exceeds what the caregiver has earned less what refunds have already taken back. What a take-back could not
    take yet is ``owed_kobo``: it is not yet recovered, so it does not lower ``earned`` (it is recovered from later earnings)."""
    state = money_state(db, cg)
    total = state["paid"] - state["shortfall"] + state["available"] + state["reserved"]
    entitled = earned_kobo - taken_back_kobo(db, cg)
    assert state["available"] >= 0, f"negative balance: {state}"
    assert total <= entitled, f"more money than the caregiver earned ({total} > {entitled}): {state}"
    return state


def paystack_paid_kobo(db, fake, cg) -> int:
    """What the FAKE Paystack says it has paid this caregiver: every transfer to their recipient that is ``success`` there
    right now (a reversed or failed one is not). Read from Paystack's own state, not from our attempt rows, so a bug that makes
    our books agree with themselves cannot hide an overpayment."""
    db.expire_all()
    recipient = db.query(CaregiverBankAccount.recipient_code).filter_by(caregiver_id=cg.id).scalar()
    return sum(t["amount"] for t in fake.transfers.values() if t["recipient"] == recipient and t["status"] == "success")


def assert_overpayment_on_record(db, fake, cg, earned_kobo: int, written_off_kobo: int = 0) -> dict:
    """Paid (by Paystack's own account) + available + reserved <= earned + the shortfall that is outstanding or was written off.
    The shortfall is ADDED to what may exist, never subtracted from what was paid, so an overpayment is only allowed when it is
    on record: written off by an admin on purpose, or still outstanding."""
    state = money_state(db, cg)
    paid = paystack_paid_kobo(db, fake, cg)
    total = paid + state["available"] + state["reserved"]
    allowed = earned_kobo + state["shortfall"] + written_off_kobo
    assert total <= allowed, (f"an overpayment that is not on record ({total} > {earned_kobo} earned + {state['shortfall']} "
                              f"outstanding + {written_off_kobo} written off): paid {paid}, {state}")
    return state
