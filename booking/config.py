"""Settings for the Booking service, read from environment variables.

docker-compose.yml sets DATABASE_URL, AMQP_URL and BOOKING_DEMO_SEED for the
booking container. The DATABASE_URL default below is the same value compose
gives Booking (host booking-db). The password is a DEVELOPMENT-ONLY value,
not a real credential.
"""
import os

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
