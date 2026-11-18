"""The HTTP endpoints of the Booking service."""
from typing import Annotated

import psycopg2
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, StringConstraints

import db
import metrics

router = APIRouter()

# Text fields are trimmed first, then must be non-empty and within the
# contract's limits (section 1): IDs 64, customer_email and customer_name 255.
# Anything else is a 422.
IdStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
TextStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class HoldRequest(BaseModel):
    event_id: IdStr
    seat_id: IdStr
    customer_id: IdStr
    # Optional (contract section 5). Absent (or null) -> db.hold_seat stores
    # '<customer_id>@example.test' and customer_id. No email format check.
    customer_email: TextStr | None = None
    customer_name: TextStr | None = None


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/ready")
def ready():
    try:
        conn = db.connect_once()
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
        finally:
            conn.close()
    except psycopg2.Error as error:
        raise HTTPException(503, "Booking database is unavailable") from error
    return {"status": "ready", "capabilities": ["holds"]}


@router.get("/metrics")
def get_metrics():
    """Prometheus scrapes this. Text format, not JSON."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.post("/holds")
def create_hold(req: HoldRequest):
    try:
        outcome, booking_ref, booking_status = db.hold_seat(
            req.event_id, req.seat_id, req.customer_id, req.customer_email, req.customer_name)
    except psycopg2.Error as error:
        raise HTTPException(503, "Booking database is unavailable") from error

    metrics.HOLDS.labels(metrics.HOLD_LABELS[outcome]).inc()

    if outcome in ("held", "already_yours"):
        body = {
            "booking_ref": booking_ref,
            "event_id": req.event_id,
            "seat_id": req.seat_id,
            # "status" keeps meaning "this customer holds the seat" (the UI checks it).
            "status": "held",
            # Contract section 5 additions: booking_id is the same value as booking_ref.
            "booking_id": booking_ref,
            "booking_status": booking_status,
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

    if outcome == "event_cancelled":
        return JSONResponse(
            {"error": "event_cancelled", "message": "This event has been cancelled."},
            status_code=409,
        )

    return JSONResponse(
        {"error": "seat_not_found", "message": "No such seat for this event."},
        status_code=404,
    )


@router.get("/bookings/{booking_id}")
def get_booking(booking_id: IdStr, customer_id: Annotated[IdStr, Query()]):
    """Contract section 6: the booking's state, for its owner only."""
    try:
        booking = db.get_booking(booking_id, customer_id)
    except psycopg2.Error as error:
        raise HTTPException(503, "Booking database is unavailable") from error
    if booking is None:
        # Unknown booking and wrong customer get the SAME reply, so a guessed
        # booking_id reveals nothing.
        return JSONResponse(
            {"error": "booking_not_found", "message": "No such booking."},
            status_code=404,
        )
    return booking
