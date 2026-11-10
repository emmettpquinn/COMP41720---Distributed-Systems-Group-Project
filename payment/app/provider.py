"""A fake provider. Payment's database is separate from this test double."""

import random
import time

from . import metrics


class RefundRejected(ValueError):
    """A terminal refusal, different from a temporary outage."""


def simulate(mode: str, delay: float, error_rate: float) -> None:
    time.sleep(delay)
    if mode == "down" or random.random() < error_rate:
        raise ConnectionError("Provider is unavailable")


def pay(mode: str, delay: float, error_rate: float = 0) -> str:
    try:
        simulate(mode, delay, error_rate)
    except ConnectionError:
        metrics.PROVIDER.labels("payment", "error").inc()
        raise
    if mode == "decline":
        metrics.PROVIDER.labels("payment", "declined").inc()
        return "failed"
    metrics.PROVIDER.labels("payment", "success").inc()
    return "paid"


def refund(mode: str, delay: float, error_rate: float = 0) -> None:
    try:
        simulate(mode, delay, error_rate)
    except ConnectionError:
        metrics.PROVIDER.labels("refund", "error").inc()
        raise
    if mode == "decline":
        metrics.PROVIDER.labels("refund", "declined").inc()
        raise RefundRejected("Provider rejected the refund")
    metrics.PROVIDER.labels("refund", "success").inc()
