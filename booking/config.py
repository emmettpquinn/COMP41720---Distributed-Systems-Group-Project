"""Settings for the Booking service, read from environment variables.

The defaults below work on a laptop with docker-compose.dev.yml running.
The password here is a DEVELOPMENT-ONLY value, not a real credential.
"""
import os

# Postgres address (user, password, host, port, database name).
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://booking_dev:dev_password_only@localhost:5432/booking",
)

# RabbitMQ address and port.
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))

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

# Where the dev/test scripts find a running Booking (used only by the scripts).
BOOKING_URL = os.getenv("BOOKING_URL", f"http://localhost:{PORT}")
