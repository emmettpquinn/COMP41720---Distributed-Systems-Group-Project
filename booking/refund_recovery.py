"""Refund recovery command (contract section 4 row 12, section 10).

For a booking whose refund Payment REJECTED (refund.failed -> refund_pending),
send a NEW refund.requested. Run it inside the running booking container:

    docker compose exec booking python refund_recovery.py <booking_id>
    docker compose exec booking python refund_recovery.py --all

It only writes Booking's outbox (one transaction per booking, db.request_refund_again);
the running Booking's outbox relay publishes the message. The booking stays
refund_pending; refund.completed later moves it to refunded (row 10).

Not covered here: a booking stuck in refund_required because Payment had a
TEMPORARY error. Payment parked that request in its dead-letter queue, and the
fix is to replay that queue (contract section 10, case 1).

Exit code: 0 = every booking asked was requested (or nothing to do),
1 = at least one booking was refused, 2 = bad arguments or database unavailable.
"""
import argparse
import sys

import psycopg2

import db

DLQ_REPLAY = "docker compose exec payment python -m messaging.replay payment.refund-requests"


def describe(booking_id, outcome, details):
    """One line of output for one booking."""
    if outcome == db.REFUND_REQUESTED_AGAIN:
        return (f"{booking_id}: refund requested again (attempt {details['attempt']}, "
                f"payment {details['payment_id']}, message {details['message_id']}). "
                "Status stays refund_pending; it becomes refunded when Payment sends refund.completed. "
                "If Payment rejects again it sends no message (contract section 10).")
    if outcome == "unknown_booking":
        return f"{booking_id}: refused - no such booking."
    if outcome == "no_payment_id":
        return f"{booking_id}: refused - no payment_id stored, so there is nothing to refund."
    status = details.get("status")
    if status == "refund_required":
        return (f"{booking_id}: refused - status is refund_required (Payment has not answered). "
                "If it has been stuck for a while, Payment hit a temporary error: "
                f"replay its dead-letter queue with: {DLQ_REPLAY}")
    return f"{booking_id}: refused - status is {status}, not refund_pending."


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="refund_recovery.py",
        description="Send a new refund.requested for bookings in refund_pending (contract row 12).")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("booking_id", nargs="?", help="one booking to retry")
    target.add_argument("--all", action="store_true", help="every booking in refund_pending")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return 0 if exit_.code == 0 else 2      # --help -> 0; bad arguments -> 2

    try:
        if args.all:
            booking_ids = db.refund_pending_ids()
            if not booking_ids:
                print("Nothing to do: no booking is in refund_pending.")
                return 0
            print(f"Found {len(booking_ids)} booking(s) in refund_pending.")
        else:
            booking_ids = [args.booking_id.strip()]
        refused = 0
        for booking_id in booking_ids:
            outcome, details = db.request_refund_again(booking_id)   # one transaction each
            print(describe(booking_id, outcome, details))
            if outcome != db.REFUND_REQUESTED_AGAIN:
                refused += 1
    except psycopg2.Error as error:
        print(f"Booking database unavailable, nothing (more) was done: {error}", file=sys.stderr)
        return 2
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
