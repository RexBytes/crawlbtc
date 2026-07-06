-- Table: blockchain.transaction_io_fail

-- DROP TABLE IF EXISTS blockchain.transaction_io_fail;

CREATE TABLE IF NOT EXISTS blockchain.transaction_io_fail
(
    txid text COLLATE pg_catalog."default" NOT NULL,
    last_attempt_at timestamp without time zone NOT NULL DEFAULT now(),
    error_message text COLLATE pg_catalog."default",
    CONSTRAINT transaction_io_fail_pkey PRIMARY KEY (txid),
    CONSTRAINT error_message_max_length CHECK (char_length(error_message) <= 1000)
)

TABLESPACE pg_default;

ALTER TABLE IF EXISTS blockchain.transaction_io_fail
    OWNER to pgadmin;

REVOKE ALL ON TABLE blockchain.transaction_io_fail FROM blockchain;

GRANT INSERT, DELETE, SELECT, UPDATE ON TABLE blockchain.transaction_io_fail TO blockchain;

GRANT ALL ON TABLE blockchain.transaction_io_fail TO pgadmin;
-- Index: idx_transaction_io_fail_last_attempt

-- DROP INDEX IF EXISTS blockchain.idx_transaction_io_fail_last_attempt;

CREATE INDEX IF NOT EXISTS idx_transaction_io_fail_last_attempt
    ON blockchain.transaction_io_fail USING btree
    (last_attempt_at DESC NULLS FIRST)
    TABLESPACE pg_default;
