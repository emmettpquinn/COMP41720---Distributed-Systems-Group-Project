CREATE TABLE outbox (
    message_id UUID PRIMARY KEY,
    event_id VARCHAR(64) NOT NULL REFERENCES events (event_id),
    event_version BIGINT NOT NULL CHECK (event_version > 0),
    envelope JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    published_at TIMESTAMPTZ,
    UNIQUE (event_id, event_version)
);

CREATE INDEX outbox_pending ON outbox (occurred_at, event_id, event_version)
    WHERE published_at IS NULL;
