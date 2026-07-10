-- 005: incremental-balance watermark.
-- Lets `crawlbtc update-balances` refresh blockchain.address_balances for only
-- the addresses touched since the last run, instead of the multi-hour full
-- rebuild. One row per materialized table, holding the fully-processed block
-- height that the table is exact up to.
CREATE TABLE IF NOT EXISTS blockchain.balance_watermark (
    name       text PRIMARY KEY,
    height     integer NOT NULL,
    updated_at timestamptz DEFAULT now() NOT NULL
);
