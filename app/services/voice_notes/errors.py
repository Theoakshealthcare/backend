from typing import Dict, Optional

from fastapi import HTTPException

from app.services.voice_notes.constants import REQUEST_ERRORS


def refuse(status_code: int, code: str, headers: Optional[Dict[str, str]] = None) -> HTTPException:
    """An error of the voice note routes: ``{"detail": {"code", "message"}}`` with the sentence for the code."""
    # An error carries no health text, but it is still never cached (the dependency that sets the header on a success does not run
    # for a raised error).
    return HTTPException(status_code=status_code, detail={"code": code, "message": REQUEST_ERRORS[code]},
                         headers={"Cache-Control": "no-store", **(headers or {})})


def not_found() -> HTTPException:
    """One answer for every kind of no-access: not your visit, no such note, the window is over, an inactive patient."""
    return refuse(404, "not_found")
