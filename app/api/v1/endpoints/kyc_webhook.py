"""The public Dojah webhook (caregiver KYC, design section 8.1). Verified over the raw body first, recorded and queued fast.

Order: capped raw body (413) -> signature (401) -> JSON (400) -> a reference that is not one of ours (200, nothing recorded) -> ledger
insert (a duplicate of a processed event: 200; of an unprocessed one: enqueue again) -> enqueue (a failure undoes a fresh insert
and answers 500) -> 200. No Dojah call and no decision here: the worker pulls the verification and records what Dojah says
(app/services/kyc_events.py). NEVER log the body, the headers or the reference.
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.core.http_body import read_capped_body
from app.db.session import get_db
from app.services import kyc_seam as seam
from app.services.kyc_queue import KycEventQueue, QueueRefused, get_kyc_event_queue
from app.services.kyc_webhook import Ingested, compensate, ingest

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/webhook", include_in_schema=False)
async def receive_webhook(request: Request, db: Session = Depends(get_db),
                          queue: KycEventQueue = Depends(get_kyc_event_queue)) -> dict:
    raw = await read_capped_body(request, seam.MAX_BODY_BYTES)

    secret = seam.webhook_secret()
    if not secret:
        logger.error("kyc webhook rejected: DOJAH_WEBHOOK_SECRET is not configured")
    if not seam.verify_signature(raw, request.headers.get(seam.SIGNATURE_HEADER), secret):
        logger.warning("kyc webhook rejected: bad or missing signature")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    try:
        event = seam.parse_event(json.loads(raw))
    except (ValueError, RecursionError):
        event = None
    if event is None:
        logger.warning("kyc webhook rejected: not a JSON object with a usable reference_id")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload")
    if not seam.is_our_reference(event.reference_id):
        # Signed, but not an attempt this server minted (the Dojah account may serve other widgets): acknowledge, record nothing.
        logger.info("kyc webhook ignored: reference is not one of ours")
        return {"status": "ok"}
    if event.verification_status is None:
        logger.info("kyc webhook: status word missing or unknown; handled like any other (the pull decides)")

    ingested = await run_in_threadpool(lambda: ingest(db, raw_body=raw, event=event))
    if ingested is None:
        return {"status": "ok"}  # a duplicate delivery or a replay that was already processed

    try:
        await queue.enqueue(ingested.job)
    except QueueRefused:
        # A job for this ledger row is already queued (the row id is the job id): a concurrent twin delivery got there first, or a
        # re-delivery's job is still running. Nothing to do, and the row must NOT be forgotten: that job needs it.
        return {"status": "ok"}
    except Exception as exc:
        logger.error("kyc webhook enqueue failed (%s); asking Dojah to retry", type(exc).__name__)  # the class only
        await _fail(db, ingested)
    return {"status": "ok"}


async def _fail(db: Session, ingested: Ingested) -> None:
    """Answer 500 (a retry, if Dojah sends one). A row this request created is forgotten first, so the retry is processed instead of being
    deduped away; a row that already existed (a re-delivery) is left alone: the next retry still owns it."""
    if ingested.is_new:
        await run_in_threadpool(compensate, db, ingested.job["event_id"])
    raise HTTPException(status_code=seam.ENQUEUE_FAILURE_STATUS, detail="Queue unavailable")
