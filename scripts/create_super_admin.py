"""Create the first Super Admin (AD-FR-84). Run once per environment:

    OAKLY_BOOTSTRAP_PASSWORD='...' poetry run python -m scripts.create_super_admin --email you@example.com
    poetry run python -m scripts.create_super_admin --email you@example.com      # prompts for the password

In Docker:  docker compose exec api python -m scripts.create_super_admin --email you@example.com

The password comes from the OAKLY_BOOTSTRAP_PASSWORD environment variable or a hidden prompt, never
from a command-line argument (arguments show up in `ps` and shell history). The account is created
active with MFA *not* enrolled, so the first sign-in goes through MFA enrolment. The script refuses
to touch an email that already exists, and it never prints the password.

Exit codes: 0 created, 1 the email already exists, 2 bad input (weak password, missing roles table...).
"""

import argparse
import getpass
import os
import sys
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.admin_permissions import SUPER_ADMIN
from app.core.security import get_password_hash
from app.db.models.admin import AdminRole, AdminUser
from app.services.admin_auth import normalize_email, validate_password

PASSWORD_ENV = "OAKLY_BOOTSTRAP_PASSWORD"


class BootstrapError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def create_super_admin(db: Session, email: str, password: str, full_name: str) -> AdminUser:
    email = normalize_email(email)
    if "@" not in email:
        raise BootstrapError("That does not look like an email address.", 2)
    try:
        validate_password(password, email)
    except ValueError as exc:
        raise BootstrapError(str(exc), 2)
    if db.scalar(select(AdminUser.id).where(AdminUser.email == email)) is not None:
        raise BootstrapError(f"An admin with the email {email} already exists. Nothing was changed.", 1)
    role = db.scalar(select(AdminRole).where(AdminRole.key == SUPER_ADMIN))
    if role is None:
        raise BootstrapError("The super_admin role is missing. Run `alembic upgrade head` first.", 2)
    admin = AdminUser(
        email=email, full_name=full_name.strip() or "Super Admin", password_hash=get_password_hash(password),
        status="active", mfa_enabled=False, roles=[role],
    )
    db.add(admin)
    db.commit()
    return admin


def _read_password() -> str:
    from_env = os.environ.get(PASSWORD_ENV)
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        raise BootstrapError(f"Set {PASSWORD_ENV}, or run this in a terminal to be prompted.", 2)
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise BootstrapError("The passwords do not match.", 2)
    return first


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="Super Admin", help="display name (default: Super Admin)")
    args = parser.parse_args(argv)

    from app.db.session import SessionLocal

    try:
        password = _read_password()
        with SessionLocal() as db:
            admin = create_super_admin(db, args.email, password, args.name)
            print(f"Created Super Admin {admin.email}. Sign in at the console; MFA enrolment is required on first login.")
    except BootstrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
