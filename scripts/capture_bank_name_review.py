"""Capture the 2026-11-25 bank-account holder-name review shapes against a RUNNING Oakly API.

A payout account whose holder name does not look like the caregiver's profile name is saved but held (``name_review_status``),
and Finance approves or rejects it. This records, into docs/fe-captures/caregiver/970+ and docs/fe-captures/admin/970+:

* caregiver: the lookup for a short-form match (Tosin / Oluwatosin), the lookup and save for a different holder, the account as
  the caregiver sees it while pending, after approval, and after a rejection;
* admin (Finance): the review queue, a decision on a stale account (409), an approval, a second decision (409), a rejection.

Same local stack and fake Paystack as ``capture_arrival_window.py`` (see its docstring); run it on a FRESH database. Re-running
replaces only 970 to 979.

    OAKLY_ENV_FILE=/path/scratch.env poetry run python -W ignore -m scripts.capture_bank_name_review \\
        --base-url http://127.0.0.1:8001 --db-url postgresql://oakly:oakly@127.0.0.1:5433/oakly --env-file /path/scratch.env

Real HTTP calls; people registered through the endpoints (the Finance admin is seeded, then signs in through the real login and
MFA endpoints). **Paystack is a local fake**: the account holder names are what the fake returns for these placeholder accounts, so
nothing here proves a live Paystack account answers the same way. Placeholder data only.
"""

import argparse
import re
import uuid
from pathlib import Path

from scripts.capture_caregiver_flow import (  # noqa: E402
    DB, Ctx, FakePaystack, Recorder, approve_caregiver, onboard_caregiver, read_env, seed_admin_session, serve_fake_paystack,
)

FIRST = 970
CG = "/api/v1/caregiver"
REVIEWS = "/api/v1/admin/payouts/bank-reviews"
FAKE = "FAKE PAYSTACK: the holder name is what the local fake returns for this placeholder account."


def _clear(out: Path) -> None:
    for f in out.glob("*.json"):
        if re.match(r"\d{3}-", f.name) and FIRST <= int(f.name[:3]) < FIRST + 10:
            f.unlink()


def run(args) -> None:
    cg_out, admin_out = Path(args.caregiver_out), Path(args.admin_out)
    _clear(cg_out)
    _clear(admin_out)
    env = read_env(args.env_file)
    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    quiet = Recorder(args.base_url, scratch)
    cg, adm = Recorder(args.base_url, cg_out), Recorder(args.base_url, admin_out)
    cg.n = adm.n = FIRST - 1
    db = DB(args.db_url)
    tag = uuid.uuid4().hex[:6]
    paystack = FakePaystack()
    # profile "Placeholder Tosin": an exact word (placeholder) and a short form (tosin inside oluwatosin) -> passes without review
    paystack.accounts[("0000000010", "058")] = "SAMPLE OLUWATOSIN PLACEHOLDER"
    paystack.accounts[("0000000011", "058")] = "SAMPLE OTHERPERSON"      # nobody on this profile
    paystack.accounts[("0000000012", "058")] = "ANOTHER STRANGER NAME"   # a second one, for the rejection
    server = serve_fake_paystack(paystack, port=args.paystack_port)
    ctx = Ctx(quiet, db, env, None, tag, paystack)  # type: ignore[arg-type]
    mail = lambda who: f"{who}-{tag}@example.com"  # noqa: E731

    quiet.quiet = True
    finance = seed_admin_session(quiet, db, env, mail("finance"), role="finance")
    a = onboard_caregiver(ctx, "a", mail("caregiver-a"), "Placeholder Tosin", capture=False)
    approve_caregiver(ctx, seed_admin_session(quiet, db, env, mail("admin")), a, capture=False)
    ta = a["token"]
    quiet.quiet = False
    cid = a["id"]

    cg.call("a-bank-resolve-short-form", "POST", f"{CG}/bank-account/resolve", token=ta,
            json_body={"account_number": "0000000010", "bank_code": "058"},
            note=f"{FAKE} The profile is 'Placeholder Tosin'; 'tosin' is the end of 'OLUWATOSIN', so it passes with needs_review false and the "
                 "real name is shown.")
    cg.call("a-bank-resolve-needs-review", "POST", f"{CG}/bank-account/resolve", token=ta,
            json_body={"account_number": "0000000011", "bank_code": "058"},
            note=f"{FAKE} A name that does not look like the profile: 200, needs_review true, and account_name is MASKED (first letter of "
                 "each word). The real name is never in this response.")
    cg.call("a-bank-save-needs-review", "PUT", f"{CG}/bank-account", token=ta,
            json_body={"account_number": "0000000011", "bank_code": "058"},
            note=f"{FAKE} The save is accepted, but name_review_status is 'pending' and account_name is MASKED (the real name is not shown to "
                 "the caregiver until Finance approves the account). Payouts to it wait for Finance.")
    adm.call("finance-bank-reviews", "GET", REVIEWS, token=finance,
             note="Finance's queue (pending by default): both names side by side, the account number masked, and the caregiver's "
                  "available balance. The wallet was seeded empty here, so balance_kobo is 0.")
    adm.call("finance-bank-review-stale", "POST", f"{REVIEWS}/{cid}/approve", token=finance, expect=409,
             json_body={"reason": "Placeholder: checked the wrong account", "account_last4": "9999"},
             note="account_last4 must be the last four digits of the account the reviewer looked at. Anything else is refused, so a "
                  "decision never carries over to an account the caregiver saved afterwards.")
    adm.call("finance-bank-review-approve", "POST", f"{REVIEWS}/{cid}/approve", token=finance,
             json_body={"reason": "Placeholder: confirmed with the caregiver by phone", "account_last4": "0011"})
    adm.call("finance-bank-review-again", "POST", f"{REVIEWS}/{cid}/approve", token=finance, expect=409,
             json_body={"reason": "Placeholder: second click", "account_last4": "0011"},
             note="A decision on an account that is not pending is refused.")
    cg.call("a-bank-get-approved", "GET", f"{CG}/bank-account", token=ta,
            note="After approval name_review_status is 'approved', the real holder name is shown, and payouts to the account resume.")
    cg.call("a-bank-save-second-needs-review", "PUT", f"{CG}/bank-account", token=ta,
            json_body={"account_number": "0000000012", "bank_code": "058"},
            note=f"{FAKE} Saving another flagged account replaces the first and starts a new review (pending again). A change of "
                 "account also starts the 24 hour pause (payouts_blocked_until).")
    adm.call("finance-bank-review-reject", "POST", f"{REVIEWS}/{cid}/reject", token=finance,
             json_body={"reason": "Placeholder: the caregiver could not confirm this account", "account_last4": "0012"})
    cg.call("a-bank-get-rejected", "GET", f"{CG}/bank-account", token=ta,
            note="After a rejection name_review_status is 'rejected' and the name stays masked: no payout is made to it. The caregiver must save a different account.")
    server.shutdown()
    print(f"wrote {len(cg.index)} caregiver and {len(adm.index)} admin captures")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://127.0.0.1:8001")
    p.add_argument("--db-url", default="postgresql://oakly:oakly@127.0.0.1:5433/oakly")
    p.add_argument("--env-file", required=True)
    p.add_argument("--caregiver-out", default="docs/fe-captures/caregiver")
    p.add_argument("--admin-out", default="docs/fe-captures/admin")
    p.add_argument("--scratch", default="/tmp/oakly-bank-name-review-setup")
    p.add_argument("--paystack-port", type=int, default=9912)
    run(p.parse_args())


if __name__ == "__main__":
    main()
