CREATE TABLE venues (
    venue_id VARCHAR(64) PRIMARY KEY
        CHECK (venue_id <> '' AND venue_id !~ '^[[:space:]]|[[:space:]]$'),
    name TEXT NOT NULL CHECK (name ~ '[^[:space:]]'),
    city TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE events (
    event_id VARCHAR(64) PRIMARY KEY
        CHECK (event_id <> '' AND event_id !~ '^[[:space:]]|[[:space:]]$'),
    venue_id VARCHAR(64) NOT NULL REFERENCES venues (venue_id),
    event_version BIGINT NOT NULL DEFAULT 1 CHECK (event_version > 0),
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled', 'cancelled')),
    title TEXT NOT NULL CHECK (title ~ '[^[:space:]]'),
    description TEXT NOT NULL DEFAULT '',
    starts_at TIMESTAMPTZ NOT NULL,
    timezone TEXT NOT NULL CHECK (timezone ~ '[^[:space:]]'),
    tags TEXT[] NOT NULL DEFAULT '{}' CHECK (array_position(tags, NULL) IS NULL),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX events_venue_id ON events (venue_id);
CREATE INDEX events_starts_at ON events (starts_at, event_id);

-- This is a map definition, not Booking's inventory or hold state.
CREATE TABLE event_seats (
    event_id VARCHAR(64) NOT NULL REFERENCES events (event_id) ON DELETE CASCADE,
    seat_id VARCHAR(64) NOT NULL
        CHECK (seat_id <> '' AND seat_id !~ '^[[:space:]]|[[:space:]]$'),
    section TEXT NOT NULL CHECK (section ~ '[^[:space:]]'),
    label TEXT NOT NULL CHECK (label ~ '[^[:space:]]'),
    PRIMARY KEY (event_id, seat_id)
);
