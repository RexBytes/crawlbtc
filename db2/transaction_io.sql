-- Table: blockchain.transaction_io

-- DROP TABLE IF EXISTS blockchain.transaction_io;

CREATE TABLE IF NOT EXISTS blockchain.transaction_io
(
    id bigint NOT NULL DEFAULT nextval('blockchain.transaction_io_id_seq'::regclass),
    txid text COLLATE pg_catalog."default" NOT NULL,
    address text COLLATE pg_catalog."default",
    amount bigint,
    io_type blockchain.tx_io_type NOT NULL,
    idx integer NOT NULL,
    address_type blockchain.address_type DEFAULT 'unknown'::blockchain.address_type,
    CONSTRAINT transaction_io_pkey PRIMARY KEY (id),
    CONSTRAINT transaction_io_unique_txid_type_idx UNIQUE (txid, io_type, idx)
)

TABLESPACE pg_default;

ALTER TABLE IF EXISTS blockchain.transaction_io
    OWNER to pgadmin;

REVOKE ALL ON TABLE blockchain.transaction_io FROM blockchain;

GRANT INSERT, DELETE, SELECT, UPDATE ON TABLE blockchain.transaction_io TO blockchain;

GRANT ALL ON TABLE blockchain.transaction_io TO pgadmin;
-- Index: idx_transaction_io_address

-- DROP INDEX IF EXISTS blockchain.idx_transaction_io_address;

CREATE INDEX IF NOT EXISTS idx_transaction_io_address
    ON blockchain.transaction_io USING btree
    (address COLLATE pg_catalog."default" ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_transaction_io_address_and_type

-- DROP INDEX IF EXISTS blockchain.idx_transaction_io_address_and_type;

CREATE INDEX IF NOT EXISTS idx_transaction_io_address_and_type
    ON blockchain.transaction_io USING btree
    (address COLLATE pg_catalog."default" ASC NULLS LAST, address_type ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_transaction_io_address_type

-- DROP INDEX IF EXISTS blockchain.idx_transaction_io_address_type;

CREATE INDEX IF NOT EXISTS idx_transaction_io_address_type
    ON blockchain.transaction_io USING btree
    (address COLLATE pg_catalog."default" ASC NULLS LAST, io_type ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_transaction_io_io_type

-- DROP INDEX IF EXISTS blockchain.idx_transaction_io_io_type;

CREATE INDEX IF NOT EXISTS idx_transaction_io_io_type
    ON blockchain.transaction_io USING btree
    (io_type ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_transaction_io_txid

-- DROP INDEX IF EXISTS blockchain.idx_transaction_io_txid;

CREATE INDEX IF NOT EXISTS idx_transaction_io_txid
    ON blockchain.transaction_io USING btree
    (txid COLLATE pg_catalog."default" ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_transaction_io_txid_idx

-- DROP INDEX IF EXISTS blockchain.idx_transaction_io_txid_idx;

CREATE INDEX IF NOT EXISTS idx_transaction_io_txid_idx
    ON blockchain.transaction_io USING btree
    (txid COLLATE pg_catalog."default" ASC NULLS LAST, idx ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_transaction_io_txid_io_idx

-- DROP INDEX IF EXISTS blockchain.idx_transaction_io_txid_io_idx;

CREATE INDEX IF NOT EXISTS idx_transaction_io_txid_io_idx
    ON blockchain.transaction_io USING btree
    (txid COLLATE pg_catalog."default" ASC NULLS LAST, io_type ASC NULLS LAST, idx ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_transaction_io_type_io

-- DROP INDEX IF EXISTS blockchain.idx_transaction_io_type_io;

CREATE INDEX IF NOT EXISTS idx_transaction_io_type_io
    ON blockchain.transaction_io USING btree
    (address_type ASC NULLS LAST, io_type ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_txio_address_io

-- DROP INDEX IF EXISTS blockchain.idx_txio_address_io;

CREATE INDEX IF NOT EXISTS idx_txio_address_io
    ON blockchain.transaction_io USING btree
    (address COLLATE pg_catalog."default" ASC NULLS LAST, io_type ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: txio_address_io_idx

-- DROP INDEX IF EXISTS blockchain.txio_address_io_idx;

CREATE INDEX IF NOT EXISTS txio_address_io_idx
    ON blockchain.transaction_io USING btree
    (address COLLATE pg_catalog."default" ASC NULLS LAST, io_type ASC NULLS LAST)
    TABLESPACE pg_default;
