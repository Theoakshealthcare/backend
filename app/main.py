import os
os.makedirs("uploads/profile_images", exist_ok=True)
from typing import Any
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.staticfiles import StaticFiles

from app.api.v1.api import api_router
from app.core.config import settings

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    description="Oaklycare Healthcare Provider API",
    version="1.0.0",
)

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