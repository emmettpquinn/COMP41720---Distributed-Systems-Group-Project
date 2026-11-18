-- One row per seat per event. Safe to run on every start (IF NOT EXISTS).
CREATE TABLE IF NOT EXISTS seats (
    event_id    TEXT NOT NULL,
    seat_id     TEXT NOT NULL,          -- e.g. 'B12'
    section     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'free'
                CHECK (status IN ('free', 'held', 'booked')),
    booking_ref TEXT,                   -- empty while the seat is free
    customer_id TEXT,                   -- empty while the seat is free
    held_until  TIMESTAMPTZ,            -- unused for now (seat expiry comes later)
    PRIMARY KEY (event_id, seat_id),    -- a seat appears once per event
    CHECK (status <> 'free' OR (booking_ref IS NULL AND customer_id IS NULL AND held_until IS NULL))
);

-- Upgrade for databases created before event.changed was consumed (contract section 7).
-- Nullable, so existing rows (demo seed, held seats) are kept as they are.
ALTER TABLE seats ADD COLUMN IF NOT EXISTS label TEXT;

-- What Booking knows about each event, from Catalogue's event.changed (contract section 7).
-- A seat may have no row here (demo seed); it then keeps the hold-only behaviour.
CREATE TABLE IF NOT EXISTS events (
    event_id      TEXT PRIMARY KEY,
    event_version BIGINT NOT NULL CHECK (event_version > 0),
    status        TEXT NOT NULL CHECK (status IN ('scheduled', 'cancelled')),
    title         TEXT NOT NULL,
    starts_at     TEXT NOT NULL,        -- kept exactly as Catalogue sends it (contract section 1)
    timezone      TEXT NOT NULL
);

-- Every consumed message_id, written in the same transaction as its effect (contract section 3).
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id   TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
