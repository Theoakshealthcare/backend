"""K3: in production the app refuses to boot with an empty Dojah webhook secret, secret key or fingerprint key (design 9.3).

An empty webhook secret makes every signature forgeable (HMAC with an empty key is something anyone can compute), an empty secret key
means no webhook can ever be confirmed by the pull, and an empty fingerprint key turns the duplicate-identity hash into a plain
hash of a NIN/BVN. Other environments may leave them empty (the suite and local development do)."""
import pytest

from app.core.config import Settings
from app.tests.fake_dojah import PRODUCTION_KYC_KEYS

BASE = dict(ENVIRONMENT="production", PAYSTACK_SECRET_KEY="sk_live_placeholder", TRUSTED_PROXIES=["10.0.0.0/8"])
KEYS = tuple(PRODUCTION_KYC_KEYS)


def test_production_boots_with_all_three_keys():
    assert Settings(**BASE, **PRODUCTION_KYC_KEYS).ENVIRONMENT == "production"


@pytest.mark.parametrize("missing", KEYS)
def test_production_refuses_to_boot_without_each_key(missing):
    with pytest.raises(ValueError, match=missing):
        Settings(**BASE, **{**PRODUCTION_KYC_KEYS, missing: ""})


def test_the_refusal_names_every_missing_key():
    with pytest.raises(ValueError) as excinfo:
        Settings(**BASE)
    assert all(name in str(excinfo.value) for name in KEYS)


@pytest.mark.parametrize("environment", ["development", "test", "staging"])
def test_other_environments_may_leave_the_keys_empty(environment):
    s = Settings(**{**BASE, "ENVIRONMENT": environment})
    assert (s.DOJAH_WEBHOOK_SECRET, s.DOJAH_SECRET_KEY, s.KYC_FINGERPRINT_KEY) == ("", "", "")


def test_the_environment_name_is_case_insensitive():
    with pytest.raises(ValueError):
        Settings(**{**BASE, "ENVIRONMENT": "Production"})
