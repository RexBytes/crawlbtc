-- Historical BTC price series for fiat valuation at time-of-transaction.
-- Daily granularity (the usual evidentiary standard); multi-currency.
-- Populated by `crawlbtc import-prices`. Safe to run repeatedly.
CREATE TABLE IF NOT EXISTS blockchain.btc_prices (
    ts        date NOT NULL,
    currency  text NOT NULL,
    price     numeric NOT NULL,
    source    text,
    added_at  timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT btc_prices_pkey PRIMARY KEY (ts, currency)
);
CREATE INDEX IF NOT EXISTS btc_prices_currency_idx ON blockchain.btc_prices (currency, ts);
