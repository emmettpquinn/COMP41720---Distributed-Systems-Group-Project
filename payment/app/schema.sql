CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    booking_id TEXT UNIQUE NOT NULL,
    amount BIGINT NOT NULL CHECK (amount > 0),
    currency TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('paid', 'failed', 'refunded')),
    refund_id TEXT,
    customer_email TEXT,
    refund_failed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Upgrade existing five-table ledgers without losing their records.
ALTER TABLE payments ADD COLUMN IF NOT EXISTS customer_email TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS refund_failed BOOLEAN NOT NULL DEFAULT FALSE;

DO $$
BEGIN
    IF to_regclass(format('%I.contacts', current_schema())) IS NOT NULL THEN
        UPDATE payments AS p SET customer_email = c.email
        FROM contacts AS c
        WHERE p.payment_id = c.payment_id AND p.customer_email IS NULL;
        DROP TABLE contacts;
    END IF;
    IF to_regclass(format('%I.refund_failures', current_schema())) IS NOT NULL THEN
        UPDATE payments AS p SET refund_failed = TRUE
        FROM refund_failures AS f WHERE p.payment_id = f.payment_id;
        DROP TABLE refund_failures;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    payment_id TEXT NOT NULL REFERENCES payments(payment_id)
);

CREATE TABLE IF NOT EXISTS outbox (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    message_id TEXT UNIQUE NOT NULL,
    body TEXT NOT NULL,
    sent BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS outbox_pending ON outbox (id) WHERE NOT sent;
