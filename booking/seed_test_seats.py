"""DEV ONLY: put a few free seats in the database for testing.

Run from the booking folder:  python seed_test_seats.py
Safe to run again: it resets these seats back to free.
"""
import db

EVENT_ID = "test-event"
SEATS = [("A1", "A"), ("A2", "A"), ("B12", "B"), ("B13", "B")]

conn = db.connect()
with conn, conn.cursor() as cur:
    for seat_id, section in SEATS:
        cur.execute(
            """INSERT INTO seats (event_id, seat_id, section) VALUES (%s, %s, %s)
               ON CONFLICT (event_id, seat_id) DO UPDATE
               SET status = 'free', booking_ref = NULL, customer_id = NULL, held_until = NULL""",
            (EVENT_ID, seat_id, section),
        )
conn.close()
print(f"Seeded {len(SEATS)} free seats for event '{EVENT_ID}'.")
