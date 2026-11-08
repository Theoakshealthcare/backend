"""Caregiver bank accounts for payouts: the bank list, resolving the account name, saving with a Paystack recipient, and the
24 hour cool-down after a change of account."""

from datetime import datetime, timedelta

import pytest

from app.db.models.caregiver import CaregiverBankAccount
from app.db.models.payout import CaregiverBankEvent
from app.services import bank_accounts
from app.tests.factories import auth_headers, make_caregiver, make_patient

BANKS, RESOLVE, ACCOUNT = "/api/v1/caregiver/banks", "/api/v1/caregiver/bank-account/resolve", "/api/v1/caregiver/bank-account"


@pytest.fixture(autouse=True)
def _fresh_state(fake_paystack):
    bank_accounts.clear_bank_cache()
    bank_accounts.clear_throttle()
    yield
    bank_accounts.clear_bank_cache()
    bank_accounts.clear_throttle()


def test_bank_list_needs_a_caregiver(db, client):
    assert client.get(BANKS).status_code == 401
    assert client.get(BANKS, headers=auth_headers(make_patient(db).user)).status_code == 403


def test_bank_list_comes_from_paystack_and_is_cached(db, client, fake_paystack):
    cg = make_caregiver(db)
    r = client.get(BANKS, headers=auth_headers(cg.user))
    assert r.status_code == 200
    assert r.json() == [{"name": "Placeholder Bank", "code": "058"}, {"name": "Sample Microfinance Bank", "code": "090"}]
    assert fake_paystack.requests[0][2]["country"] == "nigeria"
    client.get(BANKS, headers=auth_headers(cg.user))
    assert len([r for r in fake_paystack.requests if r[1].endswith("/bank")]) == 1  # the second answer came from the cache


def test_a_stale_bank_list_is_served_when_paystack_is_down_and_503_when_there_is_none(db, client, fake_paystack, monkeypatch):
    cg = make_caregiver(db)
    fake_paystack.offline = True
    r = client.get(BANKS, headers=auth_headers(cg.user))
    assert r.status_code == 503 and "list of banks" in r.json()["detail"]
    fake_paystack.offline = False
    assert client.get(BANKS, headers=auth_headers(cg.user)).status_code == 200
    fake_paystack.offline = True
    monkeypatch.setitem(bank_accounts._banks_cache, "at", 0.0)  # expired
    assert client.get(BANKS, headers=auth_headers(cg.user)).json()[0]["code"] == "058"  # stale, but better than nothing


def test_resolve_returns_the_name_the_bank_holds(db, client, fake_paystack):
    cg = make_caregiver(db, full_name="Ada Obi")
    fake_paystack.accounts[("0123456789", "058")] = "ADA OBI"
    r = client.post(RESOLVE, json={"account_number": "0123456789", "bank_code": "058"}, headers=auth_headers(cg.user))
    assert r.status_code == 200, r.text
    assert r.json() == {"account_name": "ADA OBI", "account_number": "0123456789", "bank_code": "058",
                        "bank_name": "Placeholder Bank"}


@pytest.mark.parametrize("body, detail", [
    ({"account_number": "123", "bank_code": "058"}, "10 digits"),
    ({"account_number": "01234567AB", "bank_code": "058"}, "10 digits"),
    ({"account_number": "0123456789", "bank_code": "999"}, "Choose a bank"),
])
def test_resolve_rejects_bad_input_before_calling_paystack(db, client, fake_paystack, body, detail):
    cg = make_caregiver(db)
    r = client.post(RESOLVE, json=body, headers=auth_headers(cg.user))
    assert r.status_code == 400 and detail in r.json()["detail"]
    assert not [x for x in fake_paystack.requests if x[1].endswith("/bank/resolve")]


def test_resolve_of_an_account_the_bank_does_not_know_is_a_400_with_a_plain_message(db, client, fake_paystack):
    cg = make_caregiver(db)
    fake_paystack.unresolvable.add(("0123456789", "058"))
    r = client.post(RESOLVE, json={"account_number": "0123456789", "bank_code": "058"}, headers=auth_headers(cg.user))
    assert r.status_code == 400 and "could not find that account" in r.json()["detail"]


def test_resolve_when_paystack_does_not_answer_is_a_503_not_a_wrong_account(db, client, fake_paystack):
    cg = make_caregiver(db)
    client.get(BANKS, headers=auth_headers(cg.user))  # the bank list is cached
    fake_paystack.offline = True
    r = client.post(RESOLVE, json={"account_number": "0123456789", "bank_code": "058"}, headers=auth_headers(cg.user))
    assert r.status_code == 503


def test_resolve_is_rate_limited_per_caregiver(db, client, fake_paystack):
    cg, other = make_caregiver(db), make_caregiver(db)
    body = {"account_number": "0123456789", "bank_code": "058"}
    codes = [client.post(RESOLVE, json=body, headers=auth_headers(cg.user)).status_code
             for _ in range(bank_accounts.RESOLVE_LIMIT + 1)]
    assert codes[:-1] == [200] * bank_accounts.RESOLVE_LIMIT and codes[-1] == 429
    assert client.post(RESOLVE, json=body, headers=auth_headers(other.user)).status_code == 200  # someone else is unaffected


def test_saving_the_first_account_stores_the_resolved_name_and_recipient_and_ignores_a_client_name(db, client, fake_paystack):
    cg = make_caregiver(db, full_name="Ada Obi")
    fake_paystack.accounts[("0123456789", "058")] = "ADA OBI"
    r = client.put(ACCOUNT, json={"account_number": "0123456789", "bank_code": "058", "account_name": "SOMEONE ELSE",
                                  "bank_name": "Made Up Bank"}, headers=auth_headers(cg.user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["account_name"] == "ADA OBI" and body["bank_name"] == "Placeholder Bank" and body["bank_code"] == "058"
    assert body["verified"] is True and body["payouts_blocked_until"] is None  # a first account is not a "change"
    db.expire_all()
    row = db.query(CaregiverBankAccount).one()
    assert row.recipient_code.startswith("RCP_") and row.verified_at is not None and row.payout_blocked_until is None
    recipient = fake_paystack.recipients[row.recipient_code]
    assert recipient["name"] == "ADA OBI" and recipient["type"] == "nuban" and recipient["currency"] == "NGN"
    (event,) = db.query(CaregiverBankEvent).all()
    assert event.action == "bank_account.set" and event.before is None and event.after["account_number"] == "******6789"


def test_saving_needs_a_bank_code(db, client):
    cg = make_caregiver(db)
    r = client.put(ACCOUNT, json={"account_number": "0123456789", "bank_name": "Placeholder Bank",
                                  "account_name": "ADA OBI"}, headers=auth_headers(cg.user))
    assert r.status_code == 422


def test_an_unresolvable_account_saves_nothing(db, client, fake_paystack):
    cg = make_caregiver(db)
    fake_paystack.unresolvable.add(("0123456789", "058"))
    r = client.put(ACCOUNT, json={"account_number": "0123456789", "bank_code": "058"}, headers=auth_headers(cg.user))
    assert r.status_code == 400
    assert db.query(CaregiverBankAccount).count() == 0 and db.query(CaregiverBankEvent).count() == 0


def test_saving_when_paystack_cannot_register_the_recipient_saves_nothing(db, client, fake_paystack, monkeypatch):
    cg = make_caregiver(db)
    monkeypatch.setattr(fake_paystack, "_recipient", lambda payload: (503, {"status": False, "message": "down"}))
    r = client.put(ACCOUNT, json={"account_number": "0123456789", "bank_code": "058"}, headers=auth_headers(cg.user))
    assert r.status_code == 503
    assert db.query(CaregiverBankAccount).count() == 0


def test_changing_to_a_different_account_blocks_payouts_for_24_hours_and_leaves_a_masked_history_row(db, client, fake_paystack):
    cg = make_caregiver(db)
    h = auth_headers(cg.user)
    assert client.put(ACCOUNT, json={"account_number": "0123456789", "bank_code": "058"}, headers=h).status_code == 200
    before = datetime.utcnow()
    r = client.put(ACCOUNT, json={"account_number": "0987654321", "bank_code": "090"}, headers=h)
    assert r.status_code == 200
    blocked = datetime.fromisoformat(r.json()["payouts_blocked_until"])
    assert timedelta(hours=23, minutes=59) < blocked - before < timedelta(hours=24, minutes=1)
    assert client.get(ACCOUNT, headers=h).json()["payouts_blocked_until"] == r.json()["payouts_blocked_until"]
    db.expire_all()
    change = db.query(CaregiverBankEvent).filter_by(action="bank_account.change").one()
    assert change.before["account_number"] == "******6789" and change.after["account_number"] == "******4321"
    assert change.payout_blocked_until == db.query(CaregiverBankAccount).one().payout_blocked_until
    assert "0987654321" not in str(change.after) and "0123456789" not in str(change.before)  # never the full number


def test_saving_the_same_account_again_does_not_start_a_cool_down(db, client, fake_paystack):
    cg = make_caregiver(db)
    h = auth_headers(cg.user)
    body = {"account_number": "0123456789", "bank_code": "058"}
    client.put(ACCOUNT, json=body, headers=h)
    r = client.put(ACCOUNT, json=body, headers=h)
    assert r.status_code == 200 and r.json()["payouts_blocked_until"] is None


def test_an_account_saved_before_verified_payouts_is_verified_by_saving_it_again_without_a_cool_down(db, client, fake_paystack):
    cg = make_caregiver(db)
    db.add(CaregiverBankAccount(caregiver_id=cg.id, bank_name="Typed Bank", account_number="0123456789",
                                account_name="Typed By The Client"))
    db.commit()
    h = auth_headers(cg.user)
    assert client.get(ACCOUNT, headers=h).json()["verified"] is False
    r = client.put(ACCOUNT, json={"account_number": "0123456789", "bank_code": "058"}, headers=h)
    assert r.status_code == 200 and r.json()["verified"] is True and r.json()["payouts_blocked_until"] is None
    assert r.json()["account_name"] != "Typed By The Client"


def test_the_cool_down_ends_by_itself(db, client, fake_paystack):
    cg = make_caregiver(db)
    h = auth_headers(cg.user)
    client.put(ACCOUNT, json={"account_number": "0123456789", "bank_code": "058"}, headers=h)
    client.put(ACCOUNT, json={"account_number": "0987654321", "bank_code": "058"}, headers=h)
    row = db.query(CaregiverBankAccount).one()
    row.payout_blocked_until = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    assert client.get(ACCOUNT, headers=h).json()["payouts_blocked_until"] is None


def test_only_a_caregiver_can_use_the_bank_endpoints(db, client):
    patient = make_patient(db)
    for method, url in (("get", ACCOUNT), ("put", ACCOUNT), ("post", RESOLVE)):
        r = getattr(client, method)(url, headers=auth_headers(patient.user), **({} if method == "get" else
                                    {"json": {"account_number": "0123456789", "bank_code": "058"}}))
        assert r.status_code == 403, (method, url)


# --- the account holder must be the caregiver (DP2-05) -----------------------------------------------------------------

MISMATCH = {"code": "bank_account_name_mismatch",
            "message": "The account name must match the name on your Oakly profile. If your name is different, contact support."}


@pytest.mark.parametrize("profile, holder", [
    ("Ada Okafor", "ADA OKAFOR"),                    # match, case-insensitive
    ("Ada Okafor", "OKAFOR, ADA"),                   # reordered, punctuation
    ("Ada Okafor", "ADA CHINWE OKAFOR"),             # the bank holds a middle name the profile lacks
    ("Ada Chinwe Okafor", "ADA OKAFOR"),             # the profile has a middle name the bank lacks
    ("Ada C. Okafor", "OKAFOR ADA C"),               # initials are ignored
    ("Ada Nurse", "ADA OKAFOR NURSE"),               # a third word on the account is fine
    ("Ada Okafor", "ADA O'KAFOR-X OKAFOR"),          # extra words and punctuation
    ("Ada", "ADA OKAFOR"),                           # single-name profile: that name must be on the account
])
def test_the_account_holder_matching_the_profile_name_is_accepted(db, client, fake_paystack, profile, holder):
    cg = make_caregiver(db, full_name=profile)
    fake_paystack.accounts[("0123456789", "058")] = holder
    h = auth_headers(cg.user)
    body = {"account_number": "0123456789", "bank_code": "058"}
    assert client.post(RESOLVE, json=body, headers=h).status_code == 200
    r = client.put(ACCOUNT, json=body, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["account_name"] == holder


@pytest.mark.parametrize("profile, holder", [
    ("Ada Okafor", "CHIDI OBI"),                     # someone else
    ("Ada Okafor", "ADA OBI"),                       # the first name only
    ("Ada Okafor", "OKAFOR CHIDI"),                  # the last name only
    ("Ada Okafor", "A OKAFOR"),                      # an initial does not stand in for the first name
    ("Ada", "CHIDI OBI"),                            # single-name profile, no match
    ("Ada", "ADAEZE OKAFOR"),                        # a prefix is not a match
    ("A B", "A B OKAFOR"),                           # a profile of initials only cannot match anyone
])
def test_a_different_account_holder_is_refused_with_a_code_and_no_names(db, client, fake_paystack, caplog, profile, holder):
    cg = make_caregiver(db, full_name=profile)
    fake_paystack.accounts[("0123456789", "058")] = holder
    h = auth_headers(cg.user)
    body = {"account_number": "0123456789", "bank_code": "058"}
    with caplog.at_level("INFO"):
        put = client.put(ACCOUNT, json=body, headers=h)
        resolve = client.post(RESOLVE, json=body, headers=h)
    for r in (put, resolve):
        assert r.status_code == 422 and r.json() == {"detail": MISMATCH}, r.text
        assert holder not in r.text and profile not in r.text
    assert "does not match the profile name" in caplog.text and "****6789" in caplog.text
    assert holder not in caplog.text and profile not in caplog.text
    assert db.query(CaregiverBankAccount).count() == 0 and db.query(CaregiverBankEvent).count() == 0
    assert not [x for x in fake_paystack.requests if x[1].endswith("/transferrecipient")]  # nothing registered with Paystack


def test_a_mismatch_leaves_an_existing_account_and_its_cool_down_untouched(db, client, fake_paystack):
    cg = make_caregiver(db, full_name="Ada Okafor")
    h = auth_headers(cg.user)
    fake_paystack.accounts[("0123456789", "058")] = "ADA OKAFOR"
    assert client.put(ACCOUNT, json={"account_number": "0123456789", "bank_code": "058"}, headers=h).status_code == 200
    fake_paystack.accounts[("0987654321", "090")] = "SOMEONE ELSE"
    r = client.put(ACCOUNT, json={"account_number": "0987654321", "bank_code": "090"}, headers=h)
    assert r.status_code == 422
    got = client.get(ACCOUNT, headers=h).json()
    assert got["account_number"] == "0123456789" and got["payouts_blocked_until"] is None  # no change, no 24h pause


def test_a_matching_change_of_account_still_starts_the_24_hour_pause(db, client, fake_paystack):
    cg = make_caregiver(db, full_name="Ada Okafor")
    h = auth_headers(cg.user)
    fake_paystack.accounts[("0123456789", "058")] = "ADA OKAFOR"
    fake_paystack.accounts[("0987654321", "090")] = "OKAFOR ADA CHINWE"
    client.put(ACCOUNT, json={"account_number": "0123456789", "bank_code": "058"}, headers=h)
    r = client.put(ACCOUNT, json={"account_number": "0987654321", "bank_code": "090"}, headers=h)
    assert r.status_code == 200 and r.json()["payouts_blocked_until"] is not None


@pytest.mark.parametrize("profile, holder, expected", [
    ("Ada Okafor", "Ada Okafor", True),
    ("Åda Okafor", "ADA OKAFOR", True),              # accents fold
    ("", "ADA OKAFOR", False),
    (None, "ADA OKAFOR", False),
    ("Ada Okafor", "", False),
    ("Ada Okafor", None, False),
])
def test_account_name_matches_profile(profile, holder, expected):
    assert bank_accounts.account_name_matches_profile(profile, holder) is expected
