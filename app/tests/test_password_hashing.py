"""pwdlib must verify every hash the old passlib stack produced, and keep bcrypt's 72-byte rule."""

import bcrypt
import pytest

from app.core.security import get_password_hash, verify_password

# Hashes produced by passlib 1.7.4 + bcrypt 4.0.1 (CryptContext(schemes=["bcrypt"])) before the swap.
PASSLIB_HASHES = [
    ("Correct-Horse-9", "$2b$12$iZSjLtmGh4N5CqJMiydwd.FOfKKCYttWzCLeAwJ2OU4CCDQv/qvOi"),
    ("é" * 36, "$2b$12$rPqETZ18Q16xSI09bchYxOuvC1jDl.2AovDcmmf8rc4QwU2N0cZbK"),  # exactly 72 bytes
    ("a" * 80, "$2b$12$LpCf2GhyO3FJs1H49X23webDI5z//VVlX4P2EH9UYyqqoLVINPyJ2"),  # passlib cut at 72
]


@pytest.mark.parametrize("password,hashed", PASSLIB_HASHES)
def test_passlib_hashes_still_verify(password, hashed):
    assert verify_password(password, hashed)


def test_wrong_password_is_rejected():
    assert not verify_password("wrong", PASSLIB_HASHES[0][1])


def test_new_hashes_round_trip_and_are_plain_bcrypt():
    h = get_password_hash("Another-Pass-1")
    assert h.startswith("$2b$12$") and verify_password("Another-Pass-1", h)
    assert bcrypt.checkpw(b"Another-Pass-1", h.encode())
    assert get_password_hash("Another-Pass-1") != h  # salted


def test_only_the_first_72_bytes_count_and_overlong_input_does_not_raise():
    h = get_password_hash("a" * 100)
    assert verify_password("a" * 72, h)
    assert verify_password("a" * 100 + "tail", h)
    assert not verify_password("a" * 71, h)


def test_multibyte_password_is_cut_on_bytes_not_characters():
    h = get_password_hash("é" * 50)  # 100 bytes
    assert verify_password("é" * 36 + "different", h)
    assert not verify_password("é" * 35, h)
