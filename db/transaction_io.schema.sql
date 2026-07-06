-- Table: blockchain.transaction_io

-- DROP TABLE IF EXISTS blockchain.transaction_io;

CREATE TABLE IF NOT EXISTS blockchain.transaction_io
(
    id bigint NOT NULL DEFAULT nextval('blockchain.transaction_io_id_seq'::regclass),
    txid text COLLATE pg_catalog."default" NOT NULL,
    address text COLLATE pg_catalog."default",
    amount bigint,
    io_type text COLLATE pg_catalog."default" NOT NULL,
    idx integer NOT NULL,
    CONSTRAINT transaction_io_pkey PRIMARY KEY (id),
    CONSTRAINT transaction_io_unique_txid_type_idx UNIQUE (txid, io_type, idx),
    CONSTRAINT transaction_io_io_type_check CHECK (io_type = ANY (ARRAY['in'::text, 'out'::text]))
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
-- Index: idx_transaction_io_address_type

-- DROP INDEX IF EXISTS blockchain.idx_transaction_io_address_type;

CREATE INDEX IF NOT EXISTS idx_transaction_io_address_type
    ON blockchain.transaction_io USING btree
    (address COLLATE pg_catalog."default" ASC NULLS LAST, io_type COLLATE pg_catalog."default" ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_transaction_io_txid

-- DROP INDEX IF EXISTS blockchain.idx_transaction_io_txid;

CREATE INDEX IF NOT EXISTS idx_transaction_io_txid
    ON blockchain.transaction_io USING btree
    (txid COLLATE pg_catalog."default" ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_transaction_io_txid_io_idx

-- DROP INDEX IF EXISTS blockchain.idx_transaction_io_txid_io_idx;

CREATE INDEX IF NOT EXISTS idx_transaction_io_txid_io_idx
    ON blockchain.transaction_io USING btree
    (txid COLLATE pg_catalog."default" ASC NULLS LAST, io_type COLLATE pg_catalog."default" ASC NULLS LAST, idx ASC NULLS LAST)
    TABLESPACE pg_default;
