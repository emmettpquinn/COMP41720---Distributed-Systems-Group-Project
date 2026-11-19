-- One row per seat per event. Safe to run on every start (IF NOT EXISTS).
CREATE TABLE IF NOT EXISTS seats (
    event_id    TEXT NOT NULL,
    seat_id     TEXT NOT NULL,          -- e.g. 'B12'
    section     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'free'
                CHECK (status IN ('free', 'held', 'booked')),
    booking_ref TEXT,                   -- empty while the seat is free
    customer_id TEXT,                   -- empty while the seat is free
    held_until  TIMESTAMPTZ,            -- same value as the booking's held_until (set by a hold)
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

-- Availability sequence per event (contract section 11). 0 = no snapshot written
-- yet; the first snapshot is 1. Only ever incremented, never reset. Safe on an
-- existing database: existing events get 0 and are backfilled once on startup.
ALTER TABLE events ADD COLUMN IF NOT EXISTS sequence BIGINT NOT NULL DEFAULT 0 CHECK (sequence >= 0);

-- Transactional outbox (contract section 3). A row is written in the same
-- transaction as the state change; the relay (outbox.py) publishes it later and
-- only then sets published_at. The envelope text is stored exactly as written
-- and is never changed, so a republish sends the same message_id and body.
CREATE TABLE IF NOT EXISTS outbox (
    message_id   TEXT PRIMARY KEY,      -- a UUID, as text
    type         TEXT NOT NULL,         -- e.g. 'seat.availability-changed' (= routing key)
    envelope     TEXT NOT NULL,         -- the full envelope JSON
    created_at   TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    published_at TIMESTAMPTZ            -- NULL until the broker confirmed it
);
-- The relay only ever looks for unpublished rows, oldest first.
CREATE INDEX IF NOT EXISTS outbox_unpublished ON outbox (created_at) WHERE published_at IS NULL;
-- Permanent publish failures (contract section 3): a row the broker returned as
-- unroutable is "set aside": failed_at and error are set, published_at stays
-- NULL, and the relay never tries it again. Pending = published_at IS NULL AND
-- failed_at IS NULL. Safe on an existing database: nullable, so existing rows
-- keep failed_at NULL (not set aside).
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS failed_at TIMESTAMPTZ;   -- NULL = not set aside
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS error TEXT;              -- why it was set aside (max 500 characters)

-- One row per booking (contract sections 4 and 5). Created by a successful hold,
-- in the same transaction as the seat update (section 4a, row 1). booking_id is
-- the same value as the seat's booking_ref. Safe on an existing database: a new
-- table; held seats that have no row here are freed on startup (section 4b,
-- db.free_holds_without_booking). No foreign key to seats or events: seats are
-- never deleted, and the tests rebuild those tables.
CREATE TABLE IF NOT EXISTS bookings (
    booking_id          TEXT PRIMARY KEY,   -- a UUID, as text
    event_id            TEXT NOT NULL,
    seat_id             TEXT NOT NULL,
    customer_id         TEXT NOT NULL,
    customer_email      TEXT NOT NULL,
    customer_name       TEXT NOT NULL,
    price_minor         INTEGER NOT NULL,   -- copied from config at hold time, never recalculated (section 8)
    currency            TEXT NOT NULL,
    status              TEXT NOT NULL
                        CHECK (status IN ('held', 'payment_pending', 'confirmed', 'released',
                                          'refund_required', 'refund_pending', 'refunded')),
    held_until          TIMESTAMPTZ NOT NULL,   -- database clock: now() + HOLD_MINUTES
    checkout_started_at TIMESTAMPTZ,            -- set by checkout (a later step)
    payment_id          TEXT,                   -- set when Payment answers (a later step)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- The expiry sweeper (contract section 4, row 4) looks for held bookings whose
-- held_until has passed; releases (rows 4 and 13) work on one event's held bookings.
CREATE INDEX IF NOT EXISTS bookings_status_held_until ON bookings (status, held_until);
CREATE INDEX IF NOT EXISTS bookings_event_status ON bookings (event_id, status);
