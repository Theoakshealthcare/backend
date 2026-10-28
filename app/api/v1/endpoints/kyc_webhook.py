"""The public Prembly webhook (caregiver KYC, design section 8.1). Verified over the raw body first, recorded and queued fast.

Order: capped raw body (413) -> signature (401) -> ``token`` (400) -> JSON (400) -> ledger insert (a duplicate of a processed event:
200; of an unprocessed one: enqueue again) -> enqueue (a failure undoes a fresh insert and answers 500, which Prembly documents as
retried) -> 200. No Prembly call and no decision here: the
worker pulls the session and records what Prembly says (app/services/kyc_events.py). NEVER log the body or the headers.
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

    key = seam.webhook_signing_key()
    if not key:
        logger.error("kyc webhook rejected: PREMBLY_WEBHOOK_KEY is not configured")
    if not seam.verify_signature(raw, request.headers.get(seam.SIGNATURE_HEADER), key):
        logger.warning("kyc webhook rejected: bad or missing signature")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    token = request.headers.get(seam.TOKEN_HEADER)
    if not seam.valid_token(token):
        logger.warning("kyc webhook rejected: missing or malformed token header")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload")
    try:
        event = seam.parse_event(json.loads(raw))
    except (ValueError, RecursionError):
        event = None
    if event is None:
        logger.warning("kyc webhook rejected: not a JSON object with a usable session id")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload")
    if event.event_type is None:
        logger.info("kyc webhook: event type missing or unusual; handled like any other (the pull decides)")

    ingested = await run_in_threadpool(lambda: ingest(db, token=token, raw_body=raw, event=event))
    if ingested is None:
        return {"status": "ok"}  # a duplicate delivery or a replay that was already processed

    try:
        await queue.enqueue(ingested.job)
    except QueueRefused:
        if not ingested.is_new:
            return {"status": "ok"}  # a re-delivery whose job is still queued or running: nothing to do
        logger.error("kyc webhook enqueue refused; asking Prembly to retry")
        await _fail(db, ingested)
    except Exception as exc:
        logger.error("kyc webhook enqueue failed (%s); asking Prembly to retry", type(exc).__name__)  # the class only
        await _fail(db, ingested)
    return {"status": "ok"}


async def _fail(db: Session, ingested: Ingested) -> None:
    """Answer 500 (Prembly retries). A row this request created is forgotten first, so the retry is processed instead of being
    deduped away; a row that already existed (a re-delivery) is left alone: the next retry still owns it."""
    if ingested.is_new:
        await run_in_threadpool(compensate, db, ingested.job["event_id"])
    raise HTTPException(status_code=seam.ENQUEUE_FAILURE_STATUS, detail="Queue unavailable")
