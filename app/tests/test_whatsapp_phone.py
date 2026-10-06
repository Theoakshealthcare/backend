"""W1: E.164 normalisation, lookup variants and log masking."""

import pytest

from app.core.phone import contact_ref, mask_phone, normalize_e164, phone_variants


@pytest.mark.parametrize("raw,expected", [
    ("+2348012345678", "+2348012345678"),
    ("2348012345678", "+2348012345678"),          # a WhatsApp wa_id: digits, no plus
    ("08012345678", "+2348012345678"),            # Nigerian national format
    ("8012345678", "+2348012345678"),             # national, trunk zero dropped
    ("0801 234 5678", "+2348012345678"),
    ("+234 (801) 234-5678", "+2348012345678"),
    ("002348012345678", "+2348012345678"),        # 00 international prefix
    ("+14155550123", "+14155550123"),             # other countries pass through
    ("14155550123", "+14155550123"),
    (" +2348012345678\n", "+2348012345678"),
])
def test_normalize_accepts(raw, expected):
    assert normalize_e164(raw) == expected


@pytest.mark.parametrize("raw", [
    "", None, "abc", "+234801234567",      # one digit short for Nigeria
    "+23480123456789",                      # one too many
    "+0123456789", "123", "+" + "1" * 16,   # leading zero country code, too short, too long
    "080-ABC-45678", 12345678901,           # letters; not a string
])
def test_normalize_rejects(raw):
    assert normalize_e164(raw) is None


def test_variants_cover_the_formats_an_old_account_may_have_stored():
    assert phone_variants("+2348012345678") == {"+2348012345678", "2348012345678", "08012345678"}
    assert phone_variants("+14155550123") == {"+14155550123", "14155550123"}


def test_mask_phone_keeps_only_the_edges():
    assert mask_phone("+2348012345678") == "+234****5678"
    assert mask_phone("2348012345678") == "234****5678"
    assert mask_phone(None) == "-"


def test_contact_ref_is_stable_and_not_the_number():
    ref = contact_ref("2348012345678")
    assert ref == contact_ref("2348012345678") and len(ref) == 12
    assert "2348012345678" not in ref


def test_contact_ref_is_keyed_so_a_number_cannot_be_recovered_by_hashing_candidates():
    import hashlib

    ref = contact_ref("2348012345678")
    assert ref != hashlib.sha256(b"2348012345678").hexdigest()[:12]
    assert ref == contact_ref("2348012345678") and ref != contact_ref("2348012345679")


def test_contact_ref_depends_on_the_server_secret(monkeypatch):
    from app.core.config import settings

    before = contact_ref("2348012345678")
    monkeypatch.setattr(settings, "SECRET_KEY", settings.SECRET_KEY + "-rotated")
    assert contact_ref("2348012345678") != before
