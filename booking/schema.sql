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
