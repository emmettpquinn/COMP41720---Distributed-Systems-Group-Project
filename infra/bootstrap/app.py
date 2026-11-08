"""Health-only development shell, not a business service implementation."""

import logging
import os

from fastapi import FastAPI

SERVICE_NAME = os.getenv("SERVICE_NAME", "")
if SERVICE_NAME not in {"booking", "discovery", "payment"}:
    raise ValueError("SERVICE_NAME must be booking, discovery or payment")

logging.getLogger("uvicorn.error").warning(
    "%s is a health-only development bootstrap; business endpoints are unavailable",
    SERVICE_NAME,
)

app = FastAPI(
    title=f"{SERVICE_NAME} development bootstrap",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health")
async def health() -> dict:
    return {"service": SERVICE_NAME, "mode": "bootstrap", "status": "ok"}
