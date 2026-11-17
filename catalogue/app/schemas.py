"""Catalogue input validation; message payloads follow the proposed C01 contract."""

from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints,
    field_validator, model_validator,
)

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
EventTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
Description = Annotated[str, StringConstraints(max_length=8000)]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Tags = Annotated[list[Tag], Field(max_length=50)]
PositiveInt = Annotated[int, Field(strict=True, gt=0, le=9223372036854775807)]


def valid_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Use a valid IANA timezone") from exc
    return value


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IndexableText(InputModel):
    title: EventTitle
    description: Description
    tags: Tags


class VenueCreate(InputModel):
    venue_id: Identifier
    name: NonBlank
    city: str = ""


class Seat(InputModel):
    seat_id: Identifier
    section: NonBlank
    label: NonBlank


class EventCreate(InputModel):
    event_id: Identifier
    venue_id: Identifier
    title: EventTitle
    description: Description = ""
    starts_at: AwareDatetime
    timezone: NonBlank
    tags: Tags = Field(default_factory=list)
    seats: list[Seat] = Field(min_length=1)

    _timezone = field_validator("timezone")(valid_timezone)

    @model_validator(mode="after")
    def unique_seats(self):
        if len({seat.seat_id for seat in self.seats}) != len(self.seats):
            raise ValueError("Seat IDs must be unique within the event")
        return self


class EventPatch(InputModel):
    expected_version: PositiveInt
    title: EventTitle | None = None
    description: Description | None = None
    starts_at: AwareDatetime | None = None
    timezone: NonBlank | None = None
    tags: Tags | None = None
    status: Literal["cancelled"] | None = None

    @model_validator(mode="before")
    @classmethod
    def changes_required(cls, values):
        if isinstance(values, dict):
            changes = {key: value for key, value in values.items() if key != "expected_version"}
            if not changes or any(value is None for value in changes.values()):
                raise ValueError("Supply at least one non-null update field")
        return values

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value):
        return valid_timezone(value) if value is not None else value


class AvailabilitySnapshot(InputModel):
    event_id: Identifier
    sequence: PositiveInt
    seats_remaining: Annotated[int, Field(strict=True, ge=0)]
    available_seat_ids: list[Identifier]

    @model_validator(mode="after")
    def consistent_count(self):
        ids = self.available_seat_ids
        if len(set(ids)) != len(ids) or self.seats_remaining != len(ids):
            raise ValueError("Availability must contain unique IDs matching the count")
        return self


class AvailabilityMessage(InputModel):
    message_id: UUID
    type: Literal["seat.availability-changed"]
    occurred_at: AwareDatetime
    version: Annotated[int, Field(strict=True, ge=1, le=1)]
    producer: Literal["booking"]
    payload: AvailabilitySnapshot
