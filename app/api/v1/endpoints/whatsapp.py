"""Meta WhatsApp webhook: verification handshake and signed event delivery."""

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.channels.whatsapp.ingest import compensate, ingest, parse_webhook
from app.channels.whatsapp.queue import InboundQueue, get_inbound_queue
from app.channels.whatsapp.signature import verify_signature, verify_token_matches
from app.core.config import settings
from app.db.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_BODY_BYTES = 1_000_000  # Meta's payloads are a few KB; refuse anything absurd before hashing it


async def _read_capped_body(request: Request) -> bytes:
    """The raw body, or a 413 as soon as it passes the cap: a declared Content-Length is checked up front
    and the stream is counted as it arrives, so a chunked body (no Content-Length) cannot push the whole
    thing into memory before anyone looks at the size."""
    too_large = HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Payload too large")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise too_large
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/webhook", include_in_schema=False)
def verify_webhook(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
) -> Response:
    """Meta's subscription handshake: echo the challenge when the verify token matches."""
    if hub_mode != "subscribe" or not verify_token_matches(hub_verify_token, settings.WHATSAPP_VERIFY_TOKEN):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification failed")
    return Response(content=hub_challenge, media_type="text/plain")


@router.post("/webhook", include_in_schema=False)
async def receive_webhook(
    request: Request,
    db: Session = Depends(get_db),
    queue: InboundQueue = Depends(get_inbound_queue),
) -> dict:
    """Messages and delivery statuses. Verified over the raw body first, recorded and queued fast."""
    raw = await _read_capped_body(request)

    if not settings.WHATSAPP_APP_SECRET:
        logger.error("whatsapp webhook rejected: WHATSAPP_APP_SECRET is not configured")
    if not verify_signature(raw, request.headers.get("x-hub-signature-256"), settings.WHATSAPP_APP_SECRET):
        logger.warning("whatsapp webhook rejected: bad or missing signature")
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")

    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON")

    result = await run_in_threadpool(lambda: ingest(db, parse_webhook(payload)))
    for index, job in enumerate(result.jobs):
        try:
            await queue.enqueue(job)
        except Exception:
            # Forget the rows that never reached the queue (not the ones that did), so Meta's retry of
            # this batch is processed instead of being deduped away.
            logger.exception("whatsapp enqueue failed; asking Meta to retry")
            await run_in_threadpool(compensate, db, result.jobs[index:])
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Queue unavailable")
    return {"status": "ok"}
