"""Caregiver bank accounts for payouts (A3 slice 2).

A caregiver never types the account holder's name: the app asks for the bank and the account number, we resolve the name
with Paystack (``/bank/resolve``), register the account as a Paystack transfer recipient (``/transferrecipient``) and store
the resolved name and the ``recipient_code``. A payout goes only to a stored account that has a recipient code.

Changing to a *different* account starts a 24 hour cool-down in which no payout is made (a stolen session cannot redirect
money the same day), and writes a ``caregiver_bank_events`` row. Saving the first account, or saving the same account again,
does not.

Paystack is called with no database lock held. The save takes the wallet lock first and then the bank row, the same order the
payout creation uses (wallet, then the bank row it reads), so a payout is created either before the change or after it.
"""

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Deque, Dict, List, Optional

from fastapi import Request
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.client_ip import client_ip
from app.db.models.caregiver import Caregiver, CaregiverBankAccount
from app.db.models.payout import CaregiverBankEvent
from app.services import earnings
from app.services.paystack import PaystackService

logger = logging.getLogger(__name__)

COOLDOWN = timedelta(hours=24)
BANKS_TTL_SECONDS = 24 * 3600
RESOLVE_LIMIT = 10          # account-name lookups per caregiver ...
RESOLVE_WINDOW_SECONDS = 60  # ... per this many seconds
ACCOUNT_NUMBER = re.compile(r"^\d{10}$")  # a NUBAN


class BankError(Exception):
    """Base: ``str(exc)`` is safe to show the caregiver."""


class BankInvalid(BankError):
    """The bank or account the caregiver gave us is not usable (HTTP 400)."""


class BankUnavailable(BankError):
    """Paystack could not be reached, or refused for a reason that is not the caregiver's (HTTP 503)."""


class BankThrottled(BankError):
    """Too many lookups (HTTP 429)."""


def mask(account_number: Optional[str]) -> Optional[str]:
    return None if not account_number else "*" * 6 + account_number[-4:]


# --- the bank list (cached) ----------------------------------------------------------------------------------------

_banks_lock = threading.Lock()
_banks_cache: Dict[str, object] = {"at": 0.0, "banks": None}


def clear_bank_cache() -> None:
    with _banks_lock:
        _banks_cache.update(at=0.0, banks=None)


def list_banks(paystack: Optional[PaystackService] = None) -> List[Dict[str, str]]:
    """Nigerian banks Paystack can pay, cached for a day in this process. When Paystack is down the last list we had is
    served; with none, ``BankUnavailable``."""
    with _banks_lock:
        banks = _banks_cache["banks"]
        if banks is not None and time.monotonic() - float(_banks_cache["at"]) < BANKS_TTL_SECONDS:
            return list(banks)  # type: ignore[arg-type]
    result = (paystack or PaystackService()).list_banks()
    with _banks_lock:
        if result.get("status"):
            fresh = sorted(({"name": b["name"], "code": b["code"]} for b in result["banks"]), key=lambda b: b["name"])
            _banks_cache.update(at=time.monotonic(), banks=fresh)
            return list(fresh)
        stale = _banks_cache["banks"]
    logger.warning("bank list unavailable from Paystack: %s", result.get("message"))
    if stale is not None:
        return list(stale)  # type: ignore[arg-type]
    raise BankUnavailable("We cannot load the list of banks right now. Please try again in a moment.")


# --- resolving an account ------------------------------------------------------------------------------------------

_throttle_lock = threading.Lock()
_lookups: Dict[str, Deque[float]] = {}


def clear_throttle() -> None:
    with _throttle_lock:
        _lookups.clear()


def _throttle(caregiver_id) -> None:
    """The resolve endpoint turns an account number into a person's name: it must not be a free lookup. Per caregiver, per
    process (a shared limiter is a follow-up)."""
    now = time.monotonic()
    with _throttle_lock:
        window = _lookups.setdefault(str(caregiver_id), deque())
        while window and now - window[0] > RESOLVE_WINDOW_SECONDS:
            window.popleft()
        if len(window) >= RESOLVE_LIMIT:
            raise BankThrottled("Too many account checks. Please wait a minute and try again.")
        window.append(now)


@dataclass(frozen=True)
class Resolved:
    account_number: str
    bank_code: str
    bank_name: str
    account_name: str


def resolve(caregiver_id, account_number: str, bank_code: str, paystack: Optional[PaystackService] = None) -> Resolved:
    paystack = paystack or PaystackService()
    account_number, bank_code = (account_number or "").strip(), (bank_code or "").strip()
    if not ACCOUNT_NUMBER.match(account_number):
        raise BankInvalid("An account number has 10 digits.")
    bank = next((b for b in list_banks(paystack) if b["code"] == bank_code), None)
    if bank is None:
        raise BankInvalid("Choose a bank from the list.")
    _throttle(caregiver_id)
    result = paystack.resolve_account(account_number, bank_code)
    if not result.get("status"):
        if result.get("ambiguous"):
            logger.warning("account resolve failed (%s) for ****%s", result.get("message"), account_number[-4:])
            raise BankUnavailable("We cannot check the account right now. Please try again in a moment.")
        raise BankInvalid("We could not find that account. Check the account number and the bank.")
    return Resolved(account_number=account_number, bank_code=bank_code, bank_name=bank["name"],
                    account_name=result["account_name"])


# --- saving the account --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SaveResult:
    account: CaregiverBankAccount
    changed: bool  # a different account replaced an earlier one: payouts are blocked for 24 hours


def save(db: Session, caregiver: Caregiver, account_number: str, bank_code: str, *, request: Optional[Request] = None,
         paystack: Optional[PaystackService] = None, now: Optional[datetime] = None) -> SaveResult:
    """Resolve, register with Paystack, then store. Commits."""
    paystack = paystack or PaystackService()
    resolved = resolve(caregiver.id, account_number, bank_code, paystack)
    recipient = paystack.create_transfer_recipient(name=resolved.account_name, account_number=resolved.account_number,
                                                   bank_code=resolved.bank_code)
    if not recipient.get("status"):
        logger.warning("transfer recipient not created (%s) for ****%s", recipient.get("message"),
                       resolved.account_number[-4:])
        if recipient.get("ambiguous"):
            raise BankUnavailable("We cannot save the account right now. Please try again in a moment.")
        raise BankInvalid("This account cannot receive payouts. Check the account number and the bank.")

    now = now or datetime.utcnow()
    try:
        wallet = earnings.locked_wallet(db, caregiver.id)  # wallet first, then the bank row (see the module docstring)
        # A first account saved while money is already in the wallet is as risky as a changed one (whoever holds the login
        # can name an account and take the balance at the next payout), so it starts the same cool-down.
        first_with_balance = int(wallet.balance or 0) > 0
        inserted = db.execute(pg_insert(CaregiverBankAccount).values(
            caregiver_id=caregiver.id, bank_name=resolved.bank_name, account_number=resolved.account_number,
            account_name=resolved.account_name, bank_code=resolved.bank_code, recipient_code=recipient["recipient_code"],
            verified_at=now, created_at=now, updated_at=now,
            payout_blocked_until=now + COOLDOWN if first_with_balance else None,
        ).on_conflict_do_nothing(index_elements=["caregiver_id"]).returning(CaregiverBankAccount.id)).first() is not None
        account = (db.query(CaregiverBankAccount).filter(CaregiverBankAccount.caregiver_id == caregiver.id)
                   .with_for_update().populate_existing().one())
        before = None if inserted else _describe(account)
        # A row from before verified payouts has no bank code or recipient: saving it again verifies it, which is not a
        # change of account when the number is the same.
        different = (not inserted) and (account.account_number != resolved.account_number
                                        or (account.bank_code is not None and account.bank_code != resolved.bank_code))
        if not inserted:
            account.bank_name = resolved.bank_name
            account.account_number = resolved.account_number
            account.account_name = resolved.account_name
            account.bank_code = resolved.bank_code
            account.recipient_code = recipient["recipient_code"]
            account.verified_at = now
            if different:
                account.payout_blocked_until = now + COOLDOWN
            account.updated_at = now
        db.add(CaregiverBankEvent(
            caregiver_id=caregiver.id, action="bank_account.change" if different else "bank_account.set",
            before=before, after=_describe(account),
            payout_blocked_until=account.payout_blocked_until if (different or (inserted and first_with_balance)) else None,
            ip=client_ip(request) if request is not None and request.client else None,
            user_agent=((request.headers.get("user-agent") or "")[:500] or None) if request is not None else None))
        db.commit()
    except Exception:
        db.rollback()
        raise
    if different:
        logger.info("caregiver %s changed bank account: payouts blocked until %s", caregiver.id, account.payout_blocked_until)
    return SaveResult(account=account, changed=different)


def _describe(account: CaregiverBankAccount) -> Dict[str, Optional[str]]:
    return {"bank_name": account.bank_name, "bank_code": account.bank_code, "account_number": mask(account.account_number),
            "account_name": account.account_name}


def is_cooling_down(account: Optional[CaregiverBankAccount], now: Optional[datetime] = None) -> bool:
    return bool(account and account.payout_blocked_until and account.payout_blocked_until > (now or datetime.utcnow()))
