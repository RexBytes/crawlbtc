-- Table: blockchain.transactions

-- DROP TABLE IF EXISTS blockchain.transactions;

CREATE TABLE IF NOT EXISTS blockchain.transactions
(
    id bigint NOT NULL DEFAULT nextval('blockchain.transactions_id_seq'::regclass),
    txid text COLLATE pg_catalog."default" NOT NULL,
    block_hash text COLLATE pg_catalog."default",
    received_time timestamp without time zone NOT NULL,
    total_in bigint,
    total_out bigint,
    CONSTRAINT transactions_pkey PRIMARY KEY (id),
    CONSTRAINT transactions_txid_key UNIQUE (txid),
    CONSTRAINT transactions_block_hash_fkey FOREIGN KEY (block_hash)
        REFERENCES blockchain.blocks (block_hash) MATCH SIMPLE
        ON UPDATE NO ACTION
        ON DELETE NO ACTION
)

TABLESPACE pg_default;

ALTER TABLE IF EXISTS blockchain.transactions
    OWNER to pgadmin;

REVOKE ALL ON TABLE blockchain.transactions FROM blockchain;

GRANT INSERT, DELETE, SELECT, UPDATE ON TABLE blockchain.transactions TO blockchain;

GRANT ALL ON TABLE blockchain.transactions TO pgadmin;
-- Index: idx_transactions_block_hash

-- DROP INDEX IF EXISTS blockchain.idx_transactions_block_hash;

CREATE INDEX IF NOT EXISTS idx_transactions_block_hash
    ON blockchain.transactions USING btree
    (block_hash COLLATE pg_catalog."default" ASC NULLS LAST)
    TABLESPACE pg_default;
