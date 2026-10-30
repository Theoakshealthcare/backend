"""Read a webhook's raw body with a size cap (shared by the Meta and Dojah webhooks).

A signature is checked over the exact bytes received, so webhooks read the body themselves instead of letting FastAPI parse it.
"""
from fastapi import HTTPException, Request, status


async def read_capped_body(request: Request, max_bytes: int) -> bytes:
    """The raw body, or a 413 as soon as it passes ``max_bytes``: a declared Content-Length is checked up front and the stream is
    counted as it arrives, so a chunked body (no Content-Length) cannot push the whole thing into memory before anyone looks at
    the size."""
    too_large = HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Payload too large")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise too_large
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)
