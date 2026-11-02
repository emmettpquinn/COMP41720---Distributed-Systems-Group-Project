"""The HTTP endpoints of the Booking service."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok"}
