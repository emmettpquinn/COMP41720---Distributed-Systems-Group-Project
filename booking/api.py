"""The HTTP endpoints of the Booking service."""
from typing import Annotated

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StringConstraints

import db

router = APIRouter()

# A text field that must not be empty (spaces around it are trimmed first).
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class HoldRequest(BaseModel):
    event_id: NonEmptyStr
    seat_id: NonEmptyStr
    customer_id: NonEmptyStr


@router.get("/health")
def health():
    return {"status": "ok"}


@router.post("/holds")
def create_hold(req: HoldRequest):
    outcome, booking_ref = db.hold_seat(req.event_id, req.seat_id, req.customer_id)

    if outcome in ("held", "already_yours"):
        body = {
            "booking_ref": booking_ref,
            "event_id": req.event_id,
            "seat_id": req.seat_id,
            "status": "held",
        }
        return JSONResponse(body, status_code=201 if outcome == "held" else 200)

    if outcome == "taken":
        return JSONResponse(
            {
                "error": "seat_taken",
                "message": "That seat was just taken. We've refreshed the map, pick another.",
            },
            status_code=409,
        )

    return JSONResponse(
        {"error": "seat_not_found", "message": "No such seat for this event."},
        status_code=404,
    )
