"""Caregiver bank accounts for payouts (A3 slice 2).

A caregiver never types the account holder's name: the app asks for the bank and the account number, we resolve the name
with Paystack (``/bank/resolve``), register the account as a Paystack transfer recipient (``/transferrecipient``) and store
the resolved name and the ``recipient_code``. A payout goes only to a stored account that has a recipient code.

Changing to a *different* account starts a 24 hour cool-down in which no payout is made (a stolen session cannot redirect
money the same day), and writes a ``caregiver_bank_events`` row. Saving the first account, or saving the same account again,
does not.

A holder name that does not look like the caregiver's profile name does not refuse the save: the account is stored with
``name_review_status = "pending"`` and no payout is made to it until Finance approves it (``review_name``). The lookup shows such
a name masked, so the endpoint is never a free account-number -> name search.

Paystack is called with no database lock held. The save takes the wallet lock first and then the bank row, the same order the
payout creation uses (wallet, then the bank row it reads), so a payout is created either before the change or after it.
"""

import hashlib
import hmac
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
from sqlalchemy.orm.attributes import flag_modified

from app.core.client_ip import client_ip
from app.core.config import settings
from app.db.models.caregiver import Caregiver, CaregiverBankAccount
from app.db.models.payout import CaregiverBankEvent
from app.services import admin_notify, earnings
from app.services.audit import AuditService
from app.services.kyc_matching import name_tokens
from app.services.paystack import PaystackService

logger = logging.getLogger(__name__)

COOLDOWN = timedelta(hours=24)
REVIEW_PENDING, REVIEW_APPROVED, REVIEW_REJECTED = "pending", "approved", "rejected"
SHORT_FORM_MIN = 4  # a word shorter than this is never accepted as the short form of a longer one ("Ada" is not "Adaeze")
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


class ReviewNotFound(Exception):
    """The caregiver has no bank account (HTTP 404)."""


class ReviewConflict(Exception):
    """The account is not in a state that allows this decision; ``str(exc)`` is safe to show an admin (HTTP 409)."""


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


def _is_short_form(word: str, other: str) -> bool:
    """``word`` is the beginning or the end of the longer ``other`` ("tosin" in "oluwatosin", "tunde" in "babatunde")."""
    short, long_ = sorted((word, other), key=len)
    return len(short) >= SHORT_FORM_MIN and len(short) < len(long_) and (long_.startswith(short) or long_.endswith(short))


def account_name_matches_profile(profile_name: Optional[str], account_name: Optional[str]) -> bool:
    """Is the account held by the person on the profile? Tolerant of case, punctuation, word order, a middle name, initials
    and a short form of a name. One-letter initials are ignored; the profile's first and last words must each be a word of the
    account name, or the start or end of one (at least 4 letters: Tosin / Oluwatosin, Tunde / Babatunde), and at least one of
    the two must be a whole word, so "Adebayo Tosin" matches "VICTOR OLUWATOSIN ADEBAYO" and "ADA OKAFOR NURSE" matches "Ada
    Nurse". A one-word profile needs that exact word. No words -> no match."""
    profile = [t for t in name_tokens(profile_name) if len(t) > 1]
    account = name_tokens(account_name)
    if not profile or not account:
        return False
    ends = (profile[0], profile[-1]) if len(profile) > 1 else (profile[0],)
    whole = [w in account for w in ends]
    if len(ends) == 1:
        return whole[0]
    near = [w in account or any(_is_short_form(w, a) for a in account) for w in ends]
    return all(near) and any(whole)


def mask_name(name: Optional[str]) -> str:
    """The first letter of each word and a fixed run of stars: "V**** O**** A****". The word lengths are not kept, so the mask
    gives away as little as possible. Shown to a caregiver in place of a holder name that is not theirs."""
    return " ".join(t[0].upper() + "****" for t in name_tokens(name))


def account_review_token(account: CaregiverBankAccount) -> str:
    """Identifies the exact account a reviewer is looking at: it changes when the number, the bank or the holder name changes.
    It is an HMAC under the server secret (a plain hash of a 10 digit number could be brute-forced back to the full number by someone
    who only sees the masked one). The queue returns it and a decision must send it back, so a decision never carries over to a different account."""
    raw = f"bank-review|{account.caregiver_id}|{account.account_number}|{account.bank_code}|{account.account_name}"
    return hmac.new(settings.SECRET_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]


@dataclass(frozen=True)
class Resolved:
    account_number: str
    bank_code: str
    bank_name: str
    account_name: str
    needs_review: bool = False  # the holder's name does not look like the profile name: the account is held once saved


def resolve(caregiver_id, account_number: str, bank_code: str, profile_name: Optional[str],
            paystack: Optional[PaystackService] = None) -> Resolved:
    """Look the account up. ``profile_name`` is the name on the caregiver's Oakly profile: a holder that does not look like it
    comes back with ``needs_review`` (the names are not logged or returned here; the caller masks the name)."""
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
    needs_review = not account_name_matches_profile(profile_name, result.get("account_name"))
    if needs_review:
        logger.warning("bank account needs a name review: caregiver %s, account ****%s", caregiver_id, account_number[-4:])
    return Resolved(account_number=account_number, bank_code=bank_code, bank_name=bank["name"],
                    account_name=result["account_name"], needs_review=needs_review)


# --- saving the account --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SaveResult:
    account: CaregiverBankAccount
    changed: bool  # a different account replaced an earlier one: payouts are blocked for 24 hours
    needs_review: bool = False  # the holder's name did not match the profile: held until Finance approves it


def save(db: Session, caregiver: Caregiver, account_number: str, bank_code: str, *, request: Optional[Request] = None,
         paystack: Optional[PaystackService] = None, now: Optional[datetime] = None) -> SaveResult:
    """Resolve, register with Paystack, then store. A holder name that does not look like the caregiver's is stored too, with
    ``name_review_status = "pending"`` (no payout until it is approved). Commits."""
    paystack = paystack or PaystackService()
    resolved = resolve(caregiver.id, account_number, bank_code, caregiver.full_name, paystack)
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
            name_review_status=REVIEW_PENDING if resolved.needs_review else None,
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
            # A later save restarts the review: a matching account has nothing to review, a flagged one waits again.
            account.name_review_status = REVIEW_PENDING if resolved.needs_review else None
            account.name_review_at = None
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
    if resolved.needs_review:
        admin_notify.notify("bank_name_review", caregiver_id=caregiver.id)  # best effort, after the commit
    return SaveResult(account=account, changed=different, needs_review=resolved.needs_review)


def _describe(account: CaregiverBankAccount) -> Dict[str, Optional[str]]:
    return {"bank_name": account.bank_name, "bank_code": account.bank_code, "account_number": mask(account.account_number),
            "account_name": account.account_name, "name_review": account.name_review_status}


def is_cooling_down(account: Optional[CaregiverBankAccount], now: Optional[datetime] = None) -> bool:
    return bool(account and account.payout_blocked_until and account.payout_blocked_until > (now or datetime.utcnow()))


def is_on_name_hold(account: Optional[CaregiverBankAccount]) -> bool:
    """True while the holder-name review is open or was rejected: no payout is made to the account."""
    return bool(account and account.name_review_status in (REVIEW_PENDING, REVIEW_REJECTED))


# --- Finance decides (admin) ---------------------------------------------------------------------------------------


def review_name(db: Session, caregiver_id, *, approve: bool, admin_id, reason: str, review_token: str,
                request: Optional[Request] = None, now: Optional[datetime] = None) -> CaregiverBankAccount:
    """Approve or reject a held account. ``review_token`` identifies the account the reviewer looked at: if the caregiver has saved
    another account since (any change of number, bank or holder name), the decision is refused (409) rather than carried over. Writes an ``admin_audit_log`` row (masked
    numbers only). Commits."""
    now = now or datetime.utcnow()
    try:
        account = (db.query(CaregiverBankAccount).filter(CaregiverBankAccount.caregiver_id == caregiver_id)
                   .with_for_update().populate_existing().first())
        if account is None:
            raise ReviewNotFound()
        if account.name_review_status != REVIEW_PENDING:
            raise ReviewConflict("This account is not waiting for a name review")
        if not hmac.compare_digest(account_review_token(account), review_token):
            raise ReviewConflict("The caregiver changed their account since you opened it. Refresh and look again.")
        before = _describe(account)
        account.name_review_status = REVIEW_APPROVED if approve else REVIEW_REJECTED
        account.name_review_at = now
        flag_modified(account, "updated_at")  # keep the save time: the queue reports it as "submitted_at", a decision is not a new save
        AuditService(db).record(admin_user_id=admin_id, action="bank_account.name_approve" if approve else "bank_account.name_reject",
                                entity_type="caregiver_bank_account", entity_id=caregiver_id, before=before,
                                after=_describe(account), reason=reason, request=request)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return account
