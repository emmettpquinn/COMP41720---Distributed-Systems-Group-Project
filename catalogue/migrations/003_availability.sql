CREATE TABLE availability (
    event_id VARCHAR(64) PRIMARY KEY REFERENCES events (event_id) ON DELETE CASCADE,
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    seats_remaining INTEGER NOT NULL CHECK (seats_remaining >= 0),
    available_seat_ids VARCHAR(64)[] NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (cardinality(available_seat_ids) = seats_remaining),
    CHECK (array_position(available_seat_ids, NULL) IS NULL)
);

CREATE TABLE processed_messages (
    message_id UUID PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL REFERENCES events (event_id),
    sequence BIGINT NOT NULL CHECK (sequence > 0),
    processed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
