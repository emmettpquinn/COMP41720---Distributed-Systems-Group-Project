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

# Port Booking listens on. TEMPORARY: the team is still deciding which port each service gets.
PORT = int(os.getenv("PORT", "8002"))

# Where the dev/test scripts find a running Booking (used only by the scripts).
BOOKING_URL = os.getenv("BOOKING_URL", f"http://localhost:{PORT}")
