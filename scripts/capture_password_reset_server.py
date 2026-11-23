"""Run the real API (uvicorn) with ONE thing replaced: the Resend HTTP transport. Every email the app tries to send is written to
a file in $CAPTURE_MAILBOX_DIR instead of leaving the machine, so `scripts/capture_password_reset.py` can read the code
out of the email the way a person does. HTTP, the database, the code hashing and the throttles are all real.

    DATABASE_URL=... SECRET_KEY=... VITALS_ENCRYPTION_KEY=... CAPTURE_MAILBOX_DIR=/tmp/mbox \
        poetry run python -m scripts.capture_password_reset_server --port 8022

Capture tooling only; never run this against anything but a scratch database.
"""
import argparse
import itertools
import json
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import httpx

_counter = itertools.count(1)


def main(argv=None) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8022)
    args = p.parse_args(argv)
    mailbox = Path(os.environ["CAPTURE_MAILBOX_DIR"])
    mailbox.mkdir(parents=True, exist_ok=True)

    def record(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        message = MIMEMultipart("alternative")
        message["Subject"], message["From"], message["To"] = body["subject"], body["from"], ", ".join(body["to"])
        message.attach(MIMEText(body["html"], "html"))
        (mailbox / f"{next(_counter):04d}.eml").write_text(f"X-Capture-Recipient: {body['to'][0]}\n{message.as_string()}")
        return httpx.Response(200, json={"id": "capture"})

    from app.core.config import settings
    from app.services import email as email_service

    email_service._transport = httpx.MockTransport(record)
    settings.RESEND_KEY = "capture"
    settings.EMAILS_FROM_EMAIL, settings.EMAILS_FROM_NAME = "noreply@example.com", "Oakly"

    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
