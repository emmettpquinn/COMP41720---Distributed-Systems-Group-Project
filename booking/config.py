"""Settings for the Booking service, read from environment variables.

docker-compose.yml sets DATABASE_URL, AMQP_URL and BOOKING_DEMO_SEED for the
booking container. The DATABASE_URL default below is the same value compose
gives Booking (host booking-db). The password is a DEVELOPMENT-ONLY value,
not a real credential.
"""
import os
import re

# Postgres address (user, password, host, port, database name).
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://booking:booking@booking-db:5432/booking",
)

# RabbitMQ URL for the event.changed consumer (events.py). Set it to an empty
# value to run without messaging (holds still work).
AMQP_URL = os.getenv("AMQP_URL", "amqp://guest:guest@localhost:5672/")

# Bookings (contract sections 4 and 8). The price is a flat synthetic price per
# seat, in minor units (4500 = 45.00), copied onto each booking when it is held.
BOOKING_PRICE_MINOR = int(os.getenv("BOOKING_PRICE_MINOR", "4500"))
BOOKING_CURRENCY = os.getenv("BOOKING_CURRENCY", "EUR")
# How long a hold lasts. held_until is computed with the database clock.
HOLD_MINUTES = int(os.getenv("HOLD_MINUTES", "10"))

# Port Booking listens on. TEMPORARY: the team is still deciding which port each service gets.
PORT = int(os.getenv("PORT", "8002"))

# Opt-in synthetic inventory on startup (demo_seed.py). Off unless set to "true";
# it must stay off while the event.changed consumer runs (contract section 7).
BOOKING_DEMO_SEED = os.getenv("BOOKING_DEMO_SEED", "false").lower() == "true"

# Payment's address for checkout's background call (contract sections 8 and 9).
# The default is the compose service name and its internal port.
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment:8000")

# Safety net (contract section 4 row 8, section 9): a booking still payment_pending
# this many seconds after checkout_started_at gets ONE more Payment call, then is
# released. It must be above the background call's longest run (about 6.75 s).
BOOKING_SAFETY_NET_SECONDS = float(os.getenv("BOOKING_SAFETY_NET_SECONDS", "60"))
SAFETY_NET_MIN_SECONDS = 7


def validate():
    """Refuse to start with a price or currency Payment would reject (422) on every charge,
    or with a safety-net delay that could overlap the background Payment call.

    Called by main.py's lifespan. Reads the values at call time, so tests can patch them.
    """
    if not re.fullmatch(r"[A-Z]{3}", BOOKING_CURRENCY):
        raise ValueError(f"BOOKING_CURRENCY must be three capital letters (e.g. EUR), got {BOOKING_CURRENCY!r}")
    if BOOKING_PRICE_MINOR <= 0:
        raise ValueError(f"BOOKING_PRICE_MINOR must be above 0, got {BOOKING_PRICE_MINOR}")
    # The safety net must never fire while the background call (at most ~6.75 s) may still run.
    if not BOOKING_SAFETY_NET_SECONDS >= SAFETY_NET_MIN_SECONDS:     # also refuses NaN
        raise ValueError(f"BOOKING_SAFETY_NET_SECONDS must be at least {SAFETY_NET_MIN_SECONDS}, "
                         f"got {BOOKING_SAFETY_NET_SECONDS}")
