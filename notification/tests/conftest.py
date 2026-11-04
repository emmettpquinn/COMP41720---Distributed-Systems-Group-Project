import sys
from pathlib import Path

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "shared"), str(ROOT / "notification")]

from app.db import create_schema, make_engine, make_sessionmaker  # noqa: E402
from app.handlers import TicketingService  # noqa: E402
from app.mailer import MailDeliveryError  # noqa: E402


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.down = False

    async def send(self, recipient, subject, body, message_key):
        if self.down:
            raise MailDeliveryError("connection refused (MailHog stopped)")
        self.sent.append({"to": recipient, "subject": subject, "body": body, "key": message_key})


@pytest.fixture
def mailer():
    return FakeMailer()


@pytest_asyncio.fixture
async def sessionmaker(tmp_path):
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'ticketing.db'}")
    await create_schema(engine)
    yield make_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture
def service(sessionmaker, mailer):
    return TicketingService(sessionmaker, mailer)
