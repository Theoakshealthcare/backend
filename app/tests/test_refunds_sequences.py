"""Refunds v2 task 9: random event sequences and forced races over the whole refund system.

The invariant under test: NO ORDER OF EVENTS refunds more than was paid. It is read from ground truth, never from the refund
rows: what the fake Paystack says it processed, plus what the patient's wallet ledger says was credited, plus the caregiver's
money. A real double payment that the test itself causes (a refund made on the Paystack dashboard, or Paystack paying a refund
it had failed) is something the system can only DETECT, so once the test has done that the invariant becomes: the system
flagged it for review, and it paid nothing more by itself while the total was exceeded."""
import random
import threading
import time
from datetime import datetime, timedelta


from app.db.models.admin import AdminUser
from app.db.models.booking import Booking, BookingStatus, PaymentStatus
from app.db.models.caregiver import CaregiverBankAccount, CaregiverTransaction, CaregiverWallet
from app.db.models.patient import PatientWallet
from app.db.models.refund import REFUND_OPEN_STATUSES, Refund, RefundAttempt
from app.db.models.whatsapp_booking import BookingEvent
from app.db.session import SessionLocal
from app.services import earnings, patient_wallet, payouts, refunds
from app.tests.factories import make_admin, make_booking, make_patient
from app.tests.fake_meta import fake_paystack  # noqa: F401  (fixture)
from app.tests.payout_helpers import assert_never_over, payable, payouts_of
from app.tests.refund_helpers import (
    assert_refund_conserved, is_flagged, money_moved_by, paystack_live_kobo, paystack_processed_kobo, refunds_of,
    wallet_credits_kobo,
)
from app.tests.test_payouts import MONDAY

TOTAL = 4_400_000
EARNING = 3_200_000
ITERATIONS, MAX_STEPS = 100, 14
ORIGINAL, WALLET = "original_method", "wallet"
REASONS = ("cancelled_by_booker", "caregiver_cancelled", "no_caregiver_found", "admin_cancelled", "paid_after_expiry",
           "complaint", "other")
STATUSES = ("pending", "processing", "needs-attention", "processed", "failed")
# What Paystack may do next to a refund it holds. A refund it called failed can come back as processed: that is the case the
# system must DETECT (it cannot stop it), so it counts as an adversarial event below.
NEXT = {"pending": ("processing", "needs-attention", "processed", "failed"),
        "processing": ("needs-attention", "processed", "failed"),
        "needs-attention": ("processing", "processed", "failed")}
AUTOMATIC = ("send", "flaky_send", "replay", "reconcile", "reconcile_missing", "reconcile_down")
HEADS_UP = (refunds.RefundError, payouts.PayoutConflict, payouts.PayoutNotFound)  # an admin action refused, as it should be


def far():
    return datetime.utcnow() + timedelta(hours=2)


def admins(db):
    return {"f1": make_admin(db, ("finance",), password=None), "f2": make_admin(db, ("finance",), password=None),
            "sa1": make_admin(db, ("super_admin",), password=None), "sa2": make_admin(db, ("super_admin",), password=None)}


def app_booking(db, fake, *, total=TOTAL, released=True, with_earning=True, channel="app"):
    """A completed app booking paid on the fake Paystack, with its caregiver's visit earning (EARNING) either already in their
    available balance (``released``) or still pending clearance and due."""
    cg = payable(db, EARNING if (released and with_earning) else 0, fake=fake)
    booking = make_booking(db, make_patient(db), cg, status=BookingStatus.COMPLETED, total_amount_kobo=total, channel=channel)
    fake.initialize_transaction(None, "placeholder@example.com", total, booking.paystack_reference)
    fake.paid.add(booking.paystack_reference)
    if with_earning:
        db.add(CaregiverTransaction(caregiver_id=cg.id, type="earning", amount=EARNING, booking_id=booking.id,
                                    status="successful" if released else "pending_clearance",
                                    available_at=None if released else datetime.utcnow() - timedelta(hours=1)))
        db.commit()
    return cg, booking


def sent_refund(db, fake, booking, admin, *, amount=TOTAL, take_back=False):
    """A pending refund, approved to the original method and sent: processing, attempt 1 pending at Paystack."""
    refund = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                    suggested_amount_kobo=amount, status="pending_approval")
    db.add(refund)
    db.commit()
    refunds.approve_refund(db, refund.id, admin=admin, amount_kobo=amount, take_back_from_caregiver=take_back,
                           take_back_reason="Caregiver at fault" if take_back else None)
    assert refunds.dispatch_due(db) == 1
    db.expire_all()
    attempt = db.query(RefundAttempt).filter_by(refund_id=refund.id).one()
    return refund, attempt.paystack_refund_id


def body_of(fake, pid):
    """The webhook body for a refund as it stands on the fake Paystack, as (status, reference, id, amount)."""
    record = fake.refunds[pid]
    return {"status": record["status"], "transaction_reference": record["transaction_reference"],
            "paystack_refund_id": str(record["id"]), "amount_kobo": int(record["amount"])}


def deliver(db, **event):
    return refunds.apply_refund_event(db, **event)


# --- the random sequences -------------------------------------------------------------------------------------------
class Sequence:
    """One seeded run: the actions available now are the ones the state allows, so a sequence reaches deep states instead of
    being refused at the first step. The invariants run after EVERY step."""

    def __init__(self, db, fake, seed, iteration):
        self.db, self.fake, self.rng, self.iteration = db, fake, random.Random(seed), iteration
        self.released = self.rng.random() < 0.6
        self.cg, self.booking = app_booking(db, fake, released=self.released)
        self.reference = self.booking.paystack_reference
        self.people = admins(db)
        self.history, self.events = [], []   # the actions taken; the webhook bodies Paystack has sent (for replays)
        self.adversarial = False             # the test itself made Paystack pay what the books say it did not
        self.flag_seen = False               # a refund was flagged for review since then
        self.snapshots = {}                  # refund id -> (when it was flagged, money moved at that time)
        self.injected = 0
        self.next_dashboard_id = 9000 + iteration * 10
        self.stats = {}

    # -- state ------------------------------------------------------------------------------------------------------
    def open(self):
        self.db.expire_all()
        return self.db.query(Refund).filter(Refund.booking_id == self.booking.id, Refund.status.in_(REFUND_OPEN_STATUSES)).first()

    def flagged_refunds(self):
        return [r for r in refunds_of(self.db, self.booking) if is_flagged(r)]

    def moving(self):
        return [pid for pid, r in self.fake.refunds.items()
                if r["transaction_reference"] == self.reference and r["status"] in NEXT]

    def failed_at_paystack(self):
        return [pid for pid, r in self.fake.refunds.items()
                if r["transaction_reference"] == self.reference and r["status"] == "failed"]

    def enabled(self):
        r, acts = self.open(), ["release", "payout"]
        booking = self.db.get(Booking, self.booking.id)
        if r is None:
            if booking.payment_status in (PaymentStatus.PAID, PaymentStatus.PARTIALLY_REFUNDED):
                acts += ["create_system", "create_staff"] * 2
        elif r.status == "pending_approval":
            acts += ["approve"] * 5 + ["reject"]
        elif r.status == "approved":
            acts += ["send"] * 3 + ["flaky_send"]
        elif r.status in ("processing", "needs_attention"):
            acts += ["reconcile", "reconcile_missing", "reconcile_down"]
            if r.status == "needs_attention":
                acts += ["bank"] * 3
        elif r.status == "failed":
            acts += ["retry"] * 2 + ["switch"] * 3 + ["close"] * 2
        if self.flagged_refunds():
            acts += ["resolve"] * 3
        if self.moving():
            acts += ["moves"] * 4
        if self.failed_at_paystack():
            acts += ["late_processed"]
        if self.events:
            acts += ["replay"] * 2
        if r is not None and not self.injected:
            acts += ["inject"]
        return acts

    # -- one step ---------------------------------------------------------------------------------------------------
    def person(self, *names):
        return self.people[self.rng.choice(names or tuple(self.people))]

    def step(self, action, step):
        db, fake, rng = self.db, self.fake, self.rng
        r = self.open()
        if action == "create_system":
            reason = rng.choice(REASONS)
            refunds.open_system_refund(db, db.get(Booking, self.booking.id), reason, rng.choice([None, 1_000_000, TOTAL]))
            db.commit()
        elif action == "create_staff":
            refunds.request_refund(db, booking_id=self.booking.id, amount_kobo=rng.choice([1_000_000, 2_200_000, TOTAL, TOTAL + 1]),
                                   reason_code=rng.choice(REASONS), note=None, admin=self.person("f1", "sa1"))
        elif action == "approve":
            take_back = rng.random() < 0.45
            refunds.approve_refund(
                db, r.id, admin=self.person(), amount_kobo=rng.choice([None, TOTAL, TOTAL // 2, 1_000_000, rng.randint(1, TOTAL), TOTAL + 1]),
                destination=rng.choice([ORIGINAL, ORIGINAL, WALLET]), take_back_from_caregiver=take_back,
                take_back_reason="r" if take_back else None)
        elif action == "reject":
            refunds.reject_refund(db, r.id, admin=self.person(), reason="r")
        elif action == "send":
            refunds.dispatch_due(db)
        elif action == "flaky_send":
            fake.refund_error = rng.choice(["timeout", "500", "422"])
            try:
                refunds.dispatch_due(db)
            finally:
                fake.refund_error = None
        elif action == "moves":
            pid = rng.choice(sorted(self.moving()))
            status = rng.choice(NEXT[fake.refunds[pid]["status"]])
            fake.settle_refund(pid, status)
            event = body_of(fake, pid)
            self.events.append(event)
            if rng.random() < 0.7:  # else Paystack's word is not heard yet, and reconcile has something to find
                deliver(db, **event)
        elif action == "late_processed":
            pid = rng.choice(sorted(self.failed_at_paystack()))
            fake.settle_refund(pid, "processed")
            self.adversarial = True
            event = body_of(fake, pid)
            self.events.append(event)
            deliver(db, **event)  # Paystack always tells us: a refund it pays sends a webhook
        elif action == "replay":
            deliver(db, **rng.choice(self.events))  # a duplicate, a stale or an out-of-order webhook
        elif action == "inject":
            self.dashboard_refund()
        elif action == "reconcile":
            refunds.reconcile(db, now=far())
        elif action == "reconcile_missing":
            gone = {pid: fake.refunds.pop(pid) for pid in [p for p, x in fake.refunds.items() if x["transaction_reference"] == self.reference]}
            try:
                refunds.reconcile(db, now=far())
            finally:
                fake.refunds.update(gone)  # Paystack may know it after all
        elif action == "reconcile_down":
            fake.refund_error = "timeout"
            try:
                refunds.reconcile(db, now=far())
            finally:
                fake.refund_error = None
        elif action == "bank":
            refunds.submit_bank_details(db, r.id, admin=self.person(), bank_code="058", account_number="0123456789")
        elif action == "retry":
            refunds.retry_refund(db, r.id, admin=self.person())
        elif action == "switch":
            refunds.switch_to_wallet(db, r.id, admin=self.person())
            self.stats["switched"] = self.stats.get("switched", 0) + 1
        elif action == "close":
            refunds.close_refund(db, r.id, admin=self.person(), reason="r")
        elif action == "resolve":
            target = rng.choice(self.flagged_refunds())
            last = db.query(RefundAttempt).filter_by(refund_id=target.id).order_by(RefundAttempt.attempt.desc()).first()
            confirmed = int(fake.refunds[last.paystack_refund_id]["amount"]) if last and last.paystack_refund_id in fake.refunds else None
            refunds.resolve_refund(db, target.id, admin=self.person("sa1", "sa2", "f1"), outcome=rng.choice(["succeeded", "failed"]),
                                   note="checked the dashboard", amount_kobo=rng.choice([None, confirmed]))
        elif action == "release":
            earnings.release_due(db)
        elif action == "payout":
            self.payout_cycle(step)

    def dashboard_refund(self):
        """A refund made on the Paystack dashboard, which Paystack announces with a webhook like any other. It is on the same
        payment as our open refund, so the system has something to match it against: it must flag the contradiction."""
        self.next_dashboard_id += 1
        pid = str(self.next_dashboard_id)
        amount = self.rng.choice([1_000_000, 2_200_000, TOTAL])
        self.fake.refunds[pid] = {"id": pid, "transaction_reference": self.reference, "amount": amount, "status": "pending"}
        status = self.rng.choice(["pending", "processing", "processed"])
        self.fake.settle_refund(pid, status)
        self.injected += 1
        self.adversarial = True
        event = body_of(self.fake, pid)
        self.events.append(event)
        deliver(self.db, **event)

    def payout_cycle(self, step):
        db, fake = self.db, self.fake
        batch = payouts.create_batch(db, period_key=f"s{self.iteration}-{step}", now=MONDAY)
        if batch is not None:
            payouts.approve_batch(db, batch.id, admin_id=self.people["sa1"].id, reason="r", now=MONDAY)
        payouts.dispatch_due(db)
        recipient = db.query(CaregiverBankAccount.recipient_code).filter_by(caregiver_id=self.cg.id).scalar()
        for reference, transfer in sorted(fake.transfers.items()):
            if transfer["recipient"] == recipient and transfer["status"] == "pending" and self.rng.random() < 0.8:
                body = fake.settle_transfer(reference, "success")
                payouts.apply_transfer_event(db, reference, "success", amount_kobo=body["data"]["amount"], now=MONDAY)

    # -- the invariants, after every step ----------------------------------------------------------------------------
    def check(self, action, live_before, trail):
        db, fake = self.db, self.fake
        open_ones = [r for r in refunds_of(db, self.booking) if r.status in REFUND_OPEN_STATUSES]
        assert len(open_ones) <= 1, f"two open refunds on one booking. {trail}"                                       # 3
        flagged = bool(self.flagged_refunds())
        self.flag_seen = self.flag_seen or (self.adversarial and flagged)
        processed = paystack_processed_kobo(fake, self.reference)
        credits = wallet_credits_kobo(db, self.booking.patient_id)
        live = paystack_live_kobo(fake, self.reference) + credits
        if not self.adversarial:
            assert_refund_conserved(db, self.booking)                                                                   # 1
            assert processed + credits <= TOTAL, f"paid {processed} at Paystack + {credits} to the wallet > {TOTAL}. {trail}"  # 2
            assert live <= TOTAL, f"{live} is on its way out of a {TOTAL} payment. {trail}"
        elif processed + credits > TOTAL or live > TOTAL:
            assert flagged or self.flag_seen, f"more than was paid left and nothing was flagged. {trail}"
            if action in AUTOMATIC and live_before is not None:
                assert live <= live_before, f"the system sent more by itself while the total was exceeded. {trail}"
        assert_never_over(db, self.cg, EARNING)                                                                         # 4
        now_flagged = {}
        for refund in self.flagged_refunds():                                                                           # 5
            episode, moved = refund.review_flagged_at, money_moved_by(db, refund)
            now_flagged[refund.id] = (episode, moved)
            if refund.id in self.snapshots and self.snapshots[refund.id][0] == episode:
                assert self.snapshots[refund.id][1] == moved, f"money moved on a refund in review. {trail}"
                now_flagged[refund.id] = self.snapshots[refund.id]
        self.snapshots = now_flagged

    def live(self):
        return paystack_live_kobo(self.fake, self.reference) + wallet_credits_kobo(self.db, self.booking.patient_id)

    def run(self):
        for step in range(MAX_STEPS):
            action = self.rng.choice(self.enabled())
            self.history.append(action)
            live_before = self.live()
            try:
                self.step(action, step)
            except HEADS_UP:
                self.db.rollback()
                self.stats["refused"] = self.stats.get("refused", 0) + 1
            self.check(action, live_before, f"seed {1000 + self.iteration}, step {step}: {self.history}")
        self.finish()

    def finish(self):
        """Leave nothing open for the next iteration's send and reconcile jobs, and keep this caregiver out of its batches."""
        self.db.expire_all()
        self.db.query(Refund).filter(Refund.booking_id == self.booking.id, Refund.status.in_(REFUND_OPEN_STATUSES)).update(
            {"status": "closed"}, synchronize_session=False)
        self.cg.profile_status = "suspended"
        self.db.commit()


def test_no_event_sequence_refunds_more_than_was_paid(db, fake_paystack):
    """100 seeded sequences of up to 14 steps over one booking: create (system and staff), approve (any admin, amount and
    destination, with and without a take-back), send (and a send that times out or is refused), every webhook status for any
    attempt including duplicates and out-of-order ones, reconcile (Paystack knows it, has no record, or is down), retry, bank
    details, switch to wallet, close, resolve, a refund made on the Paystack dashboard, a failed refund Paystack then pays, and
    the caregiver's earning release and payout. After every step: refunds committed <= paid; Paystack-processed + wallet credits
    <= paid (from the fake's own state); one open refund at most; the caregiver is never paid more than they earned; and a
    refund in review moves no money."""
    totals = {"reviews": 0, "switches": 0, "succeeded": 0, "wallet_approved": 0, "injected": 0, "late_processed": 0,
              "take_backs": 0, "refused": 0, "events": 0}
    for iteration in range(ITERATIONS):
        seq = Sequence(db, fake_paystack, 1000 + iteration, iteration)
        seq.run()
        db.expire_all()
        mine = refunds_of(db, seq.booking)
        totals["reviews"] += int(any(r.review_flagged_at is not None or r.review_reason for r in mine) or "needs_review" in [r.status for r in mine])
        totals["switches"] += seq.stats.get("switched", 0)
        totals["refused"] += seq.stats.get("refused", 0)
        totals["succeeded"] += sum(r.status == "succeeded" for r in mine)
        totals["wallet_approved"] += sum(r.destination == WALLET and r.status == "succeeded" for r in mine)
        totals["take_backs"] += sum(int(r.take_back_kobo or 0) > 0 and r.status == "succeeded" for r in mine)
        totals["injected"] += seq.injected
        totals["late_processed"] += seq.history.count("late_processed")
        totals["events"] += len(seq.events)
    print("sequence coverage:", totals)
    assert totals["reviews"] >= 1 and totals["switches"] >= 1, f"the sequences never reached a review or a switch: {totals}"
    assert totals["succeeded"] > ITERATIONS // 4 and totals["wallet_approved"] and totals["take_backs"], totals
    assert totals["injected"] and totals["events"] > ITERATIONS and totals["refused"], totals


# --- forced races: two sessions and an Event, so the interleaving is the one under test, not luck ---------------------
def pause_first_call(monkeypatch, owner, name):
    """Make the first call to ``owner.name`` stop (holding whatever locks its caller holds) until ``go`` is set. Returns
    (reached, go, restore): ``reached`` is set when the call is stopped."""
    real, reached, go, state = getattr(owner, name), threading.Event(), threading.Event(), {"paused": False}

    def wrapper(*a, **kw):
        if not state["paused"]:
            state["paused"] = True
            reached.set()
            assert go.wait(60), "the test never released the paused call"
        return real(*a, **kw)

    monkeypatch.setattr(owner, name, wrapper)
    return reached, go, lambda: monkeypatch.setattr(owner, name, real)


def in_thread(fn, outcome, key):
    def run():
        session = SessionLocal()
        try:
            result = fn(session)
            outcome[key] = result.status if isinstance(result, Refund) else result  # not the row: its session is closed
        except Exception as error:  # noqa: BLE001  (the test inspects what the call raised)
            outcome[key] = error
        finally:
            session.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def blocked(thread, why):
    time.sleep(1.0)
    assert thread.is_alive(), why


def test_two_approvals_cannot_exceed_total(db, fake_paystack, monkeypatch):
    """(a) Two sessions approve the same refund at once: the second waits for the booking lock and is refused, so one
    approval stands. (b) The cumulative case: a closed refund that Paystack later paid is confirmed by a Super Admin at the
    same moment the refund opened after it is approved for the full total. The approval is decided under the booking lock,
    after the confirmation committed, so it sees what is left to refund and is refused."""
    people = admins(db)
    # (a)
    _cg, booking = app_booking(db, fake_paystack)
    refund = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                    suggested_amount_kobo=3_000_000, status="pending_approval")
    db.add(refund)
    db.commit()
    reached, go, restore = pause_first_call(monkeypatch, refunds.AuditService, "record")
    out = {}
    first = in_thread(lambda s: refunds.approve_refund(s, refund.id, admin=s.get(AdminUser, people["sa1"].id), amount_kobo=3_000_000,
                                                       destination=WALLET), out, "first")
    assert reached.wait(30)
    second = in_thread(lambda s: refunds.approve_refund(s, refund.id, admin=s.get(AdminUser, people["sa2"].id), amount_kobo=TOTAL), out, "second")
    blocked(second, "the second approval did not wait for the booking lock")
    go.set()
    first.join(60)
    second.join(60)
    assert isinstance(out["second"], refunds.RefundConflict), out
    db.expire_all()
    assert out["first"] == "succeeded" and db.get(Refund, refund.id).amount_kobo == 3_000_000
    assert wallet_credits_kobo(db, booking.patient_id) == 3_000_000
    assert_refund_conserved(db, booking)
    restore()

    # (b)
    _cg, booking = app_booking(db, fake_paystack, with_earning=False)
    first_refund, pid = sent_refund(db, fake_paystack, booking, people["f1"], amount=3_000_000)
    fake_paystack.settle_refund(pid, "failed")
    assert deliver(db, status="failed", transaction_reference=booking.paystack_reference, paystack_refund_id=pid,
                   amount_kobo=3_000_000) == "applied"
    refunds.close_refund(db, first_refund.id, admin=people["f1"], reason="giving up")
    second_refund = refunds.request_refund(db, booking_id=booking.id, amount_kobo=TOTAL, reason_code="other", note=None, admin=people["f1"])
    fake_paystack.settle_refund(pid, "processed")  # Paystack paid the one we gave up on
    assert deliver(db, status="processed", transaction_reference=booking.paystack_reference, paystack_refund_id=pid,
                   amount_kobo=3_000_000) == "review"
    db.expire_all()
    assert db.get(Refund, first_refund.id).status == "closed" and db.get(Refund, first_refund.id).review_flagged_at is not None
    reached, go, restore = pause_first_call(monkeypatch, refunds.AuditService, "record")
    out = {}
    resolve = in_thread(lambda s: refunds.resolve_refund(s, first_refund.id, admin=s.get(AdminUser, people["sa1"].id), outcome="succeeded",
                                                         note="Paystack shows 30,000 processed"), out, "resolve")
    assert reached.wait(30)
    approve = in_thread(lambda s: refunds.approve_refund(s, second_refund.id, admin=s.get(AdminUser, people["sa2"].id), amount_kobo=TOTAL),
                        out, "approve")
    blocked(approve, "the approval did not wait for the booking lock")
    go.set()
    resolve.join(60)
    approve.join(60)
    assert out["resolve"] == "succeeded", out
    assert isinstance(out["approve"], refunds.RefundInvalid), f"a refund of the whole total was approved on top of {out}"
    db.expire_all()
    assert db.get(Refund, second_refund.id).status == "pending_approval"
    assert_refund_conserved(db, booking)
    assert paystack_live_kobo(fake_paystack, booking.paystack_reference) <= TOTAL


def test_webhook_and_reconcile_settle_once(db, fake_paystack, monkeypatch):
    """A processed webhook is holding the locks, inside the success routine, when reconcile finds the same refund processed at
    Paystack. Reconcile waits, then finds it settled: the success routine, the take-back and the family's notice run once."""
    people = admins(db)
    cg, booking = app_booking(db, fake_paystack, channel="whatsapp")  # a WhatsApp booking is the one that queues the family's notice
    refund, pid = sent_refund(db, fake_paystack, booking, people["f1"], amount=2_200_000, take_back=True)
    db.query(RefundAttempt).filter_by(refund_id=refund.id).update({"created_at": datetime.utcnow() - timedelta(hours=3)})
    db.commit()
    fake_paystack.settle_refund(pid, "processed")
    reached, go, restore = pause_first_call(monkeypatch, refunds, "_mark_succeeded")
    out = {}
    webhook = in_thread(lambda s: deliver(s, status="processed", transaction_reference=booking.paystack_reference, paystack_refund_id=pid,
                                          amount_kobo=2_200_000), out, "webhook")
    assert reached.wait(30)
    sweep = in_thread(lambda s: refunds.reconcile(s, now=far()), out, "reconcile")
    blocked(sweep, "reconcile did not wait for the booking lock")
    go.set()
    webhook.join(60)
    sweep.join(60)
    assert out["webhook"] == "applied" and out["reconcile"] == 0, out
    db.expire_all()
    assert db.get(Refund, refund.id).status == "succeeded"
    rows = db.query(CaregiverTransaction).filter_by(caregiver_id=cg.id, type="refund_take_back").all()
    assert [(t.amount, t.status) for t in rows] == [(1_600_000, "successful")], "the take-back ran more than once"
    assert db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).one().balance == EARNING - 1_600_000
    assert db.query(BookingEvent).filter_by(booking_id=booking.id, dedupe_key=f"{refund.id}:complete").count() == 1
    assert db.get(Booking, booking.id).payment_status == PaymentStatus.PARTIALLY_REFUNDED
    assert_never_over(db, cg, EARNING)
    assert_refund_conserved(db, booking)


def failed_refund(db, fake, booking, admin, amount=TOTAL):
    refund, pid = sent_refund(db, fake, booking, admin, amount=amount)
    fake.settle_refund(pid, "failed")
    assert deliver(db, status="failed", transaction_reference=booking.paystack_reference, paystack_refund_id=pid, amount_kobo=amount) == "applied"
    return refund, pid


def test_switch_to_wallet_racing_a_processed_webhook(db, fake_paystack, monkeypatch):
    """(a) Paystack processes a refund between the moment the switch asked it (failed) and the moment the switch locks: the
    webhook has put the refund in review by then, the switch is refused and the wallet is NOT credited on top. (b) The switch is
    holding the locks, about to credit the wallet, when the processed webhook arrives: the credit stands, nothing is reversed, and
    the refund is flagged for a person, who is told money may have left twice."""
    people = admins(db)
    _cg, booking = app_booking(db, fake_paystack, with_earning=False)
    refund, pid = failed_refund(db, fake_paystack, booking, people["f1"])
    asked, proceed, real = threading.Event(), threading.Event(), refunds.PaystackService.fetch_refund
    state = {"stopped": False}

    def fetch(self, paystack_refund_id):
        answer = real(self, paystack_refund_id)  # Paystack still says failed
        if not state["stopped"]:
            state["stopped"] = True
            asked.set()
            assert proceed.wait(60)
        return answer

    monkeypatch.setattr(refunds.PaystackService, "fetch_refund", fetch)
    out = {}
    switch = in_thread(lambda s: refunds.switch_to_wallet(s, refund.id, admin=s.get(AdminUser, people["sa1"].id)), out, "switch")
    assert asked.wait(30)
    fake_paystack.settle_refund(pid, "processed")
    session = SessionLocal()
    try:
        assert deliver(session, status="processed", transaction_reference=booking.paystack_reference, paystack_refund_id=pid,
                       amount_kobo=TOTAL) == "review"
    finally:
        session.close()
    proceed.set()
    switch.join(60)
    assert isinstance(out["switch"], refunds.RefundConflict), out
    db.expire_all()
    assert db.get(Refund, refund.id).status == "needs_review" and wallet_credits_kobo(db, booking.patient_id) == 0
    assert paystack_processed_kobo(fake_paystack, booking.paystack_reference) == TOTAL
    monkeypatch.setattr(refunds.PaystackService, "fetch_refund", real)

    # (b)
    _cg, booking = app_booking(db, fake_paystack, with_earning=False)
    refund, pid = failed_refund(db, fake_paystack, booking, people["f1"])
    reached, go, restore = pause_first_call(monkeypatch, patient_wallet, "credit_refund_locked")
    out = {}
    switch = in_thread(lambda s: refunds.switch_to_wallet(s, refund.id, admin=s.get(AdminUser, people["sa1"].id)), out, "switch")
    assert reached.wait(30)
    fake_paystack.settle_refund(pid, "processed")
    webhook = in_thread(lambda s: deliver(s, status="processed", transaction_reference=booking.paystack_reference, paystack_refund_id=pid,
                                          amount_kobo=TOTAL), out, "webhook")
    blocked(webhook, "the webhook did not wait for the booking lock")
    go.set()
    switch.join(60)
    webhook.join(60)
    assert out["switch"] == "succeeded" and out["webhook"] == "review", out
    db.expire_all()
    after = db.get(Refund, refund.id)
    assert after.status == "needs_review" and after.review_reason and after.processed_at is not None
    assert wallet_credits_kobo(db, booking.patient_id) == TOTAL
    assert db.query(PatientWallet).filter_by(patient_id=booking.patient_id).one().balance_kobo == TOTAL, "nothing is reversed by itself"
    flagged_at = money_moved_by(db, after)
    assert deliver(db, status="processed", transaction_reference=booking.paystack_reference, paystack_refund_id=pid, amount_kobo=TOTAL) == "review"
    assert money_moved_by(db, after) == flagged_at, "money moved on a refund in review"


def test_take_back_racing_release_due_and_a_payout_batch(db, fake_paystack, monkeypatch):
    """A refund's take-back holds the caregiver's wallet when the release job and a payout batch both want it. They wait, then
    run against the balance AFTER the take-back, so the caregiver is paid and keeps exactly what they earned less the take-back:
    the batch never reserves the part that was taken back, and the release never credits more than the lowered balance."""
    people = admins(db)
    cg, booking = app_booking(db, fake_paystack)  # EARNING (3,200,000) already in the wallet
    due = make_booking(db, make_patient(db), cg, status=BookingStatus.COMPLETED, total_amount_kobo=TOTAL)
    db.add(CaregiverTransaction(caregiver_id=cg.id, type="earning", amount=1_000_000, booking_id=due.id, status="pending_clearance",
                                available_at=datetime.utcnow() - timedelta(hours=1)))
    refund = Refund(booking_id=booking.id, patient_id=booking.patient_id, reason_code="other", origin="system",
                    suggested_amount_kobo=2_200_000, status="pending_approval")
    db.add(refund)
    db.commit()
    earned = EARNING + 1_000_000
    reached, go, restore = pause_first_call(monkeypatch, patient_wallet, "credit_refund_locked")  # after the take-back, before the commit
    out = {}
    approve = in_thread(lambda s: refunds.approve_refund(
        s, refund.id, admin=s.get(AdminUser, people["sa1"].id), amount_kobo=2_200_000, destination=WALLET,
        take_back_from_caregiver=True, take_back_reason="Caregiver at fault"), out, "approve")
    assert reached.wait(30)
    release = in_thread(lambda s: earnings.release_due(s), out, "release")
    batch = in_thread(lambda s: payouts.create_batch(s, period_key="race", now=MONDAY), out, "batch")
    blocked(release, "the release did not wait for the caregiver's wallet")
    blocked(batch, "the batch did not wait for the caregiver's wallet")
    go.set()
    for thread in (approve, release, batch):
        thread.join(60)
    assert out["approve"] == "succeeded" and out["release"] == 1, out
    db.expire_all()
    wallet = db.query(CaregiverWallet).filter_by(caregiver_id=cg.id).one()
    reserved = sum(p.amount_kobo for p in payouts_of(db, cg))
    assert wallet.owed_kobo == 0 and wallet.balance + reserved == earned - 1_600_000, (wallet.balance, reserved)
    assert reserved in (0, EARNING - 1_600_000, earned - 1_600_000), "the batch reserved money that was taken back"
    rows = db.query(CaregiverTransaction).filter_by(caregiver_id=cg.id, type="refund_take_back").all()
    assert [(t.amount, t.status) for t in rows] == [(1_600_000, "successful")]
    assert_never_over(db, cg, earned)
    assert_refund_conserved(db, booking)
