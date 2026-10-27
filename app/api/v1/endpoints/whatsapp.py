"""Meta WhatsApp webhook (verification handshake, signed event delivery) and the booking Flow data endpoint."""

import json
import logging
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.channels.whatsapp.flows.crypto import FlowCryptoError, decrypt_request, encrypt_response, load_private_key
from app.channels.whatsapp.flows.service import FlowService
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
    too_large = HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Payload too large")
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


# Meta's status codes for the Flows data endpoint (they change what the Flow tells the person):
# https://developers.facebook.com/docs/whatsapp/flows/guides/implementingyourflowendpoint#error_codes
STATUS_DECRYPTION_FAILED = 421  # Meta re-fetches the public key and retries
STATUS_SIGNATURE_FAILED = 432


@lru_cache(maxsize=4)
def _flow_private_key(pem: str, passphrase: str):
    return load_private_key(pem, passphrase or None)


@router.post("/flows/data", include_in_schema=False)
async def flow_data(request: Request, db: Session = Depends(get_db)) -> Response:
    """The booking Flow's data endpoint (WA-FR-16). The request is signed (``X-Hub-Signature-256``, the same
    app-secret HMAC as the webhook, checked over the raw body first) and encrypted (RSA-OAEP key, AES-128-GCM
    payload); the reply is encrypted with the flipped IV. Spec:
    https://developers.facebook.com/docs/whatsapp/flows/guides/implementingyourflowendpoint"""
    raw = await _read_capped_body(request)

    if not settings.WHATSAPP_APP_SECRET:
        logger.error("whatsapp flow request rejected: WHATSAPP_APP_SECRET is not configured")
    if not verify_signature(raw, request.headers.get("x-hub-signature-256"), settings.WHATSAPP_APP_SECRET):
        logger.warning("whatsapp flow request rejected: bad or missing signature")
        return Response(status_code=STATUS_SIGNATURE_FAILED)

    try:
        key = _flow_private_key(settings.WHATSAPP_FLOW_PRIVATE_KEY, settings.WHATSAPP_FLOW_PRIVATE_KEY_PASSPHRASE)
    except FlowCryptoError:
        logger.error("whatsapp flow request refused: WHATSAPP_FLOW_PRIVATE_KEY is missing or unreadable")
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    try:
        decrypted = decrypt_request(json.loads(raw), key)
    except (FlowCryptoError, ValueError):
        logger.warning("whatsapp flow request could not be decrypted")
        return Response(status_code=STATUS_DECRYPTION_FAILED)

    result = await run_in_threadpool(lambda: FlowService(db).handle(decrypted.payload))
    if result.body is None:
        return Response(status_code=result.status)
    return Response(content=encrypt_response(result.body, decrypted), media_type="text/plain")
