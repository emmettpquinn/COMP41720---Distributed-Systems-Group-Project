"""All Postgres connection code for the Booking service."""
import time
import uuid
from pathlib import Path

import psycopg2

import config

RETRIES = 10          # how many times to try connecting
WAIT_SECONDS = 2      # pause between tries


def connect():
    """Open a connection to Postgres, retrying if it isn't ready yet."""
    for attempt in range(1, RETRIES + 1):
        try:
            return psycopg2.connect(config.DATABASE_URL)
        except psycopg2.OperationalError as error:
            if attempt == RETRIES:
                raise
            print(f"Postgres not ready (try {attempt}/{RETRIES}): {error}")
            time.sleep(WAIT_SECONDS)


def init_schema():
    """Run schema.sql. Safe to call on every start."""
    sql = (Path(__file__).parent / "schema.sql").read_text()
    conn = connect()
    try:
        with conn, conn.cursor() as cur:   # 'with conn' commits on success
            cur.execute(sql)
    finally:
        conn.close()


def hold_seat(event_id, seat_id, customer_id):
    """Try to hold a seat. Returns (outcome, booking_ref).

    outcome is one of: 'held', 'already_yours', 'taken', 'not_found'.
    """
    new_ref = str(uuid.uuid4())
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            # The ONE conditional update: it only succeeds if the seat is free right now.
            cur.execute(
                """UPDATE seats
                   SET status = 'held', booking_ref = %s, customer_id = %s
                   WHERE event_id = %s AND seat_id = %s AND status = 'free'""",
                (new_ref, customer_id, event_id, seat_id),
            )
            if cur.rowcount == 1:
                return "held", new_ref

            # The update changed nothing. Look the seat up ONLY to choose the reply.
            cur.execute(
                "SELECT status, customer_id, booking_ref FROM seats WHERE event_id = %s AND seat_id = %s",
                (event_id, seat_id),
            )
            row = cur.fetchone()
            if row is None:
                return "not_found", None
            status, holder, ref = row
            if status == "held" and holder == customer_id:
                return "already_yours", ref
            return "taken", None
    finally:
        conn.close()
