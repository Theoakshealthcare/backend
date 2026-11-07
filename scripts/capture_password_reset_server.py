"""Run the real API (uvicorn) with ONE thing replaced: the SMTP transport. Every email the app tries to send is written to
a file in $CAPTURE_MAILBOX_DIR instead of leaving the machine, so `scripts/capture_password_reset.py` can read the code
out of the email the way a person does. HTTP, the database, the code hashing and the throttles are all real.

    DATABASE_URL=... SECRET_KEY=... VITALS_ENCRYPTION_KEY=... CAPTURE_MAILBOX_DIR=/tmp/mbox \
        poetry run python -m scripts.capture_password_reset_server --port 8022

Capture tooling only; never run this against anything but a scratch database.
"""
import argparse
import itertools
import os
import smtplib
from pathlib import Path

_counter = itertools.count(1)


def main(argv=None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8022)
    args = p.parse_args(argv)
    mailbox = Path(os.environ["CAPTURE_MAILBOX_DIR"])
    mailbox.mkdir(parents=True, exist_ok=True)

    class RecordingSMTP:
        def __init__(self, host, port, context=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            pass

        def sendmail(self, sender, recipient, raw):
            (mailbox / f"{next(_counter):04d}.eml").write_text(f"X-Capture-Recipient: {recipient}\n{raw}")

    smtplib.SMTP_SSL = RecordingSMTP
    from app.core.config import settings

    settings.SMTP_HOST, settings.SMTP_PORT = "capture.invalid", 465
    settings.SMTP_USER, settings.SMTP_PASSWORD = "capture", "capture"
    settings.EMAILS_FROM_EMAIL, settings.EMAILS_FROM_NAME = "noreply@example.com", "Oakly"

    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
