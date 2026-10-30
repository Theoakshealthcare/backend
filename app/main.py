import os
os.makedirs("uploads/profile_images", exist_ok=True)
from typing import Any
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.staticfiles import StaticFiles

from app.api.v1.api import api_router
from app.core.config import settings
from app.core.log_filters import install_access_log_redaction

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    description="Oaklycare Healthcare Provider API",
    version="1.0.0",
)

# uvicorn's access log prints query strings; keep the WhatsApp webhook's hub.verify_token out of it
install_access_log_redaction()

# FastAPI's 422 body echoes each rejected value back as ``input``. For these fields that would put a bank account number
# into a response body (and the logs and traces that capture it), so the value is dropped from the error. A missing
# field's error carries the whole body as ``input``, so naming a password field here also covers the other one.
NEVER_ECHO_FIELDS = {"account_number", "current_password", "new_password"}


@app.exception_handler(RequestValidationError)
async def validation_error_without_secrets(_request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = []
    for error in exc.errors():
        if NEVER_ECHO_FIELDS.intersection(str(part) for part in error.get("loc", ())):
            error = {k: v for k, v in error.items() if k not in ("input", "ctx")}
        errors.append(error)
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


# Set up CORS
if settings.BACKEND_CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin) for origin in settings.BACKEND_CORS_ORIGINS],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # the console reads Retry-After from the 423 (locked) and 429 (rate limited) admin auth answers
        expose_headers=["Retry-After"],
    )

# Include API router
app.include_router(api_router, prefix=settings.API_V1_STR)

# Only public profile images are served statically. Private files (caregiver documents) live in
# PRIVATE_UPLOAD_DIR and are served through authenticated endpoints.
app.mount("/uploads/profile_images", StaticFiles(directory="uploads/profile_images"), name="profile_images")

@app.get("/", include_in_schema=False)
async def root():
    return {"message": "Welcome to Oaklycare API. See /docs for documentation."}

@app.get("/health")
def health_check() -> Any:
    """
    Health check endpoint
    """
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)