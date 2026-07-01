-- Payment status cache — run once before deploying payment features.
-- psql $DATABASE_URL -f backend/db/migrations/add_invoice_cache.sql

CREATE TABLE IF NOT EXISTS invoice_cache (
    invoice_id      VARCHAR(255) PRIMARY KEY,
    customer_name   VARCHAR(255) NOT NULL,
    status          VARCHAR(50)  NOT NULL,
    due_date        DATE,
    balance         NUMERIC(12, 2) NOT NULL DEFAULT 0,
    total           NUMERIC(12, 2) NOT NULL DEFAULT 0,
    currency_code   VARCHAR(10)  NOT NULL DEFAULT 'INR',
    zoho_view_url   TEXT,
    last_synced     BIGINT       NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_invoice_cache_status
    ON invoice_cache (status);

CREATE INDEX IF NOT EXISTS idx_invoice_cache_customer_name_lower
    ON invoice_cache (LOWER(customer_name));
