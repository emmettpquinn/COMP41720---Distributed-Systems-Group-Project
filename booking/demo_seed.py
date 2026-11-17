"""Opt-in synthetic inventory, written only through Booking's own database code."""

import json
from pathlib import Path

import db


def seed():
    data = json.loads((Path(__file__).parent / "demo-data.json").read_text())
    connection = db.connect_once()
    try:
        with connection, connection.cursor() as cursor:
            for event in data["events"]:
                for seat in event["seats"]:
                    cursor.execute(
                        "INSERT INTO seats (event_id, seat_id, section) VALUES (%s, %s, %s) "
                        "ON CONFLICT (event_id, seat_id) DO NOTHING",
                        (event["event_id"], seat["seat_id"], seat["section"]),
                    )
    finally:
        connection.close()
