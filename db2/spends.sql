-- Table: blockchain.spends

-- DROP TABLE IF EXISTS blockchain.spends;

CREATE TABLE IF NOT EXISTS blockchain.spends
(
    prev_txid text COLLATE pg_catalog."default" NOT NULL,
    prev_vout integer NOT NULL,
    spending_txid text COLLATE pg_catalog."default" NOT NULL,
    spending_vin integer NOT NULL,
    spent_height integer,
    spent_block text COLLATE pg_catalog."default",
    spent_time timestamp with time zone,
    CONSTRAINT spends_pkey PRIMARY KEY (prev_txid, prev_vout),
    CONSTRAINT spends_spending_txid_spending_vin_key UNIQUE (spending_txid, spending_vin)
)

TABLESPACE pg_default;

ALTER TABLE IF EXISTS blockchain.spends
    OWNER to pgadmin;

REVOKE ALL ON TABLE blockchain.spends FROM blockchain;

GRANT INSERT, DELETE, SELECT, UPDATE ON TABLE blockchain.spends TO blockchain;

GRANT ALL ON TABLE blockchain.spends TO pgadmin;
-- Index: spends_spending_idx

-- DROP INDEX IF EXISTS blockchain.spends_spending_idx;

CREATE INDEX IF NOT EXISTS spends_spending_idx
    ON blockchain.spends USING btree
    (spending_txid COLLATE pg_catalog."default" ASC NULLS LAST, spending_vin ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: spends_spent_height_idx

-- DROP INDEX IF EXISTS blockchain.spends_spent_height_idx;

CREATE INDEX IF NOT EXISTS spends_spent_height_idx
    ON blockchain.spends USING btree
    (spent_height ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: spends_spent_time_idx

-- DROP INDEX IF EXISTS blockchain.spends_spent_time_idx;

CREATE INDEX IF NOT EXISTS spends_spent_time_idx
    ON blockchain.spends USING btree
    (spent_time ASC NULLS LAST)
    TABLESPACE pg_default;
