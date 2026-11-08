"""First-run helper: publish the ONE-TIME pricing row (`booking_settings`) on a new environment.

    python3 -m scripts.staging_set_pricing https://api.<domain> --admin-email you@x.com \\
        --base-fee <naira per hour> --platform-fee-percent <0-100> \\
        --multiplier nurse=<x> --multiplier doctor=<x> ... --reason "Opening staging price list"

WHY IT EXISTS: a fresh database has no pricing row, and until one exists every booking preview and create answers
503 `booking_settings_not_configured`. `POST /api/v1/booking-settings/` creates the first version and needs an admin
with `pricing.write` (a Super Admin has it). This script signs that admin in (password and authenticator code are
typed at a hidden prompt, never passed on the command line) and sends the request.

IT NEVER INVENTS PRICES: --base-fee, --platform-fee-percent and --reason are required with no default; the owner
decides the numbers. Multipliers are optional (a service type without one is quoted at the base fee).

First sign-in of a brand-new admin enrols MFA: the script prints the authenticator key ONCE to your terminal; add it
to an authenticator app (Google Authenticator, 1Password, ...: "enter setup key"), then type the 6-digit code.
If pricing already exists the script changes nothing and shows what is live (edit it with PUT, which is a new version).

Environment alternatives to the prompts: OAKLY_ADMIN_PASSWORD, OAKLY_ADMIN_TOTP (a TOTP code is valid for 30 s).
"""

import argparse
import getpass
import os
import sys
from typing import Dict, List, Optional

from scripts.staging_smoke import Smoke

SERVICE_TYPES = ("nurse", "doctor", "physiotherapist", "caregiver", "nutritionist")


def _parse_multipliers(items: List[str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or key not in SERVICE_TYPES:
            raise SystemExit(f"--multiplier must look like nurse=1.0 with a key from {', '.join(SERVICE_TYPES)} (got {item!r})")
        try:
            out[key] = float(value)
        except ValueError:
            raise SystemExit(f"--multiplier {item!r}: {value!r} is not a number")
    return out


def _detail(body) -> str:
    return str(body.get("detail") if isinstance(body, dict) else body)[:200]


def sign_in(api: Smoke, email: str) -> bool:
    password = os.environ.get("OAKLY_ADMIN_PASSWORD") or getpass.getpass(f"Password for {email} (hidden): ")
    status, body = api.call("POST", "/api/v1/admin/auth/login", body={"email": email, "password": password})
    if status != 200 or not isinstance(body, dict) or not body.get("mfa_token"):
        print(f"Sign-in refused: HTTP {status}: {_detail(body)}", file=sys.stderr)
        return False
    if body.get("mfa_enrollment_required"):
        print("\nFirst sign-in: add this admin to an authenticator app now.")
        print(f"  Setup key: {body.get('secret')}")
        print(f"  (or open this link/QR: {body.get('otpauth_uri')})\n")
    code = os.environ.get("OAKLY_ADMIN_TOTP") or input("6-digit code from the authenticator app: ").strip()
    status, body = api.call("POST", "/api/v1/admin/auth/mfa/verify", body={"mfa_token": body["mfa_token"], "code": code})
    if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        print(f"Code refused: HTTP {status}: {_detail(body)}", file=sys.stderr)
        return False
    api.token = body["access_token"]
    return True


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Publish the first pricing version on a new environment.")
    parser.add_argument("base_url", help="https://api.<domain>")
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--base-fee", type=float, required=True, help="naira per hour (e.g. a number the owner chose)")
    parser.add_argument("--platform-fee-percent", type=float, required=True, help="0-100")
    parser.add_argument("--multiplier", action="append", default=[], metavar="SERVICE=X", help="repeatable, e.g. nurse=1.0")
    parser.add_argument("--surge-pricing", action="store_true", help="switch surge on (default off)")
    parser.add_argument("--surge-multiplier", type=float, default=1.0)
    parser.add_argument("--discount", type=float, default=0.0, help="percent, default 0")
    parser.add_argument("--reason", required=True, help="kept in the audit log")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = parser.parse_args(argv)

    payload = {
        "base_fee": args.base_fee, "platform_fee_percent": args.platform_fee_percent,
        "multipliers": _parse_multipliers(args.multiplier) or None, "surge_pricing": args.surge_pricing,
        "surge_multiplier": args.surge_multiplier, "discount": args.discount, "reason": args.reason,
    }
    api = Smoke(args.base_url)
    if not sign_in(api, args.admin_email):
        return 1

    status, latest = api.call("GET", "/api/v1/booking-settings/latest", auth=True)
    if status == 200:
        print(f"Pricing already exists (version {latest.get('id')}, base fee {latest.get('base_fee')}, platform fee "
              f"{latest.get('platform_fee_percent')}%). Nothing changed. Edit it with PUT /booking-settings/<id> (a new version).")
        return 0
    if status != 404:
        print(f"Could not read the current pricing: HTTP {status}: {_detail(latest)}", file=sys.stderr)
        return 1

    print("About to publish the FIRST price list:")
    for key, value in payload.items():
        print(f"  {key}: {value}")
    if not args.yes and input("Publish these prices? [y/N] ").strip().lower() != "y":
        print("Cancelled; nothing was sent.")
        return 1
    status, body = api.call("POST", "/api/v1/booking-settings/", body=payload, auth=True)
    if status != 200:
        print(f"Refused: HTTP {status}: {_detail(body)}", file=sys.stderr)
        return 1
    print(f"Published pricing version {body.get('id')}. Re-run the smoke script: 'Pricing row exists' should now PASS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
