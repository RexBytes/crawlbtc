-- Table: blockchain.known_addresses

-- DROP TABLE IF EXISTS blockchain.known_addresses;

CREATE TABLE IF NOT EXISTS blockchain.known_addresses
(
    address text COLLATE pg_catalog."default" NOT NULL,
    public_key text COLLATE pg_catalog."default",
    private_key text COLLATE pg_catalog."default",
    label text COLLATE pg_catalog."default",
    note text COLLATE pg_catalog."default",
    created_at timestamp without time zone DEFAULT now(),
    CONSTRAINT known_addresses_pkey PRIMARY KEY (address)
)

TABLESPACE pg_default;

ALTER TABLE IF EXISTS blockchain.known_addresses
    OWNER to pgadmin;

REVOKE ALL ON TABLE blockchain.known_addresses FROM blockchain;

GRANT INSERT, DELETE, SELECT, UPDATE ON TABLE blockchain.known_addresses TO blockchain;

GRANT ALL ON TABLE blockchain.known_addresses TO pgadmin;
-- Index: idx_known_addresses_created_at

-- DROP INDEX IF EXISTS blockchain.idx_known_addresses_created_at;

CREATE INDEX IF NOT EXISTS idx_known_addresses_created_at
    ON blockchain.known_addresses USING btree
    (created_at ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_known_addresses_label

-- DROP INDEX IF EXISTS blockchain.idx_known_addresses_label;

CREATE INDEX IF NOT EXISTS idx_known_addresses_label
    ON blockchain.known_addresses USING btree
    (lower(label) COLLATE pg_catalog."default" ASC NULLS LAST)
    TABLESPACE pg_default;
