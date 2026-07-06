-- Table: blockchain.address_transactions

-- DROP TABLE IF EXISTS blockchain.address_transactions;

CREATE TABLE IF NOT EXISTS blockchain.address_transactions
(
    txid text COLLATE pg_catalog."default" NOT NULL,
    address text COLLATE pg_catalog."default" NOT NULL,
    amount bigint NOT NULL,
    io_type text COLLATE pg_catalog."default" NOT NULL,
    block_hash text COLLATE pg_catalog."default",
    "timestamp" timestamp without time zone,
    CONSTRAINT address_transactions_pkey PRIMARY KEY (txid, address, io_type),
    CONSTRAINT address_transactions_io_type_check CHECK (io_type = ANY (ARRAY['in'::text, 'out'::text]))
)

TABLESPACE pg_default;

ALTER TABLE IF EXISTS blockchain.address_transactions
    OWNER to pgadmin;

REVOKE ALL ON TABLE blockchain.address_transactions FROM blockchain;

GRANT INSERT, DELETE, SELECT, UPDATE ON TABLE blockchain.address_transactions TO blockchain;

GRANT ALL ON TABLE blockchain.address_transactions TO pgadmin;
-- Index: idx_address_transactions_address

-- DROP INDEX IF EXISTS blockchain.idx_address_transactions_address;

CREATE INDEX IF NOT EXISTS idx_address_transactions_address
    ON blockchain.address_transactions USING btree
    (address COLLATE pg_catalog."default" ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_address_transactions_timestamp

-- DROP INDEX IF EXISTS blockchain.idx_address_transactions_timestamp;

CREATE INDEX IF NOT EXISTS idx_address_transactions_timestamp
    ON blockchain.address_transactions USING btree
    ("timestamp" ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_address_transactions_txid

-- DROP INDEX IF EXISTS blockchain.idx_address_transactions_txid;

CREATE INDEX IF NOT EXISTS idx_address_transactions_txid
    ON blockchain.address_transactions USING btree
    (txid COLLATE pg_catalog."default" ASC NULLS LAST)
    TABLESPACE pg_default;
