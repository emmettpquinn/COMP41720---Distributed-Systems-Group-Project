import os

from pydantic import BaseModel, Field
from typing import Literal


class Settings(BaseModel):
    mode: Literal["ok", "decline", "down"] = "ok"
    delay: float = Field(default=0, ge=0, le=10)
    error_rate: float = Field(default=0, ge=0, le=1)


def load_settings() -> Settings:
    return Settings(mode=os.getenv("PAYMENT_MODE", "ok"),
                    delay=os.getenv("PAYMENT_DELAY", "0"),
                    error_rate=os.getenv("PAYMENT_ERROR_RATE", "0"))
