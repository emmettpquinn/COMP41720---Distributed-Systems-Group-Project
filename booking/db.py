"""All Postgres connection code for the Booking service."""
import time
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
