-- Table: blockchain.watch_addresses

-- DROP TABLE IF EXISTS blockchain.watch_addresses;

CREATE TABLE IF NOT EXISTS blockchain.watch_addresses
(
    address text COLLATE pg_catalog."default" NOT NULL,
    address_type blockchain.address_type,
    public_key_hex text COLLATE pg_catalog."default",
    private_key_wif text COLLATE pg_catalog."default",
    private_key_hex text COLLATE pg_catalog."default",
    derivation_path text COLLATE pg_catalog."default",
    label text COLLATE pg_catalog."default",
    tags jsonb NOT NULL DEFAULT '[]'::jsonb,
    first_seen timestamp with time zone,
    last_seen timestamp with time zone,
    tx_count integer NOT NULL DEFAULT 0,
    utxo_count integer NOT NULL DEFAULT 0,
    balance_sats bigint NOT NULL DEFAULT 0,
    last_scanned_height integer,
    last_scanned_time timestamp with time zone,
    last_scanned_txid text COLLATE pg_catalog."default",
    balance_status blockchain.balance_job_status NOT NULL DEFAULT 'pending'::blockchain.balance_job_status,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CONSTRAINT watch_addresses_pkey PRIMARY KEY (address),
    CONSTRAINT watch_addresses_tx_count_check CHECK (tx_count >= 0),
    CONSTRAINT watch_addresses_utxo_count_check CHECK (utxo_count >= 0),
    CONSTRAINT watch_addresses_balance_sats_check CHECK (balance_sats >= 0),
    CONSTRAINT watch_addresses_tags_check CHECK (jsonb_typeof(tags) = 'array'::text)
)

TABLESPACE pg_default;

ALTER TABLE IF EXISTS blockchain.watch_addresses
    OWNER to pgadmin;

REVOKE ALL ON TABLE blockchain.watch_addresses FROM blockchain;

GRANT INSERT, DELETE, SELECT, UPDATE ON TABLE blockchain.watch_addresses TO blockchain;

GRANT ALL ON TABLE blockchain.watch_addresses TO pgadmin;
-- Index: watch_addresses_addr_type_idx

-- DROP INDEX IF EXISTS blockchain.watch_addresses_addr_type_idx;

CREATE INDEX IF NOT EXISTS watch_addresses_addr_type_idx
    ON blockchain.watch_addresses USING btree
    (address_type ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: watch_addresses_last_seen_idx

-- DROP INDEX IF EXISTS blockchain.watch_addresses_last_seen_idx;

CREATE INDEX IF NOT EXISTS watch_addresses_last_seen_idx
    ON blockchain.watch_addresses USING btree
    (last_seen DESC NULLS FIRST)
    TABLESPACE pg_default;
-- Index: watch_addresses_status_idx

-- DROP INDEX IF EXISTS blockchain.watch_addresses_status_idx;

CREATE INDEX IF NOT EXISTS watch_addresses_status_idx
    ON blockchain.watch_addresses USING btree
    (balance_status ASC NULLS LAST, last_scanned_height ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: watch_addresses_tags_gin

-- DROP INDEX IF EXISTS blockchain.watch_addresses_tags_gin;

CREATE INDEX IF NOT EXISTS watch_addresses_tags_gin
    ON blockchain.watch_addresses USING gin
    (tags)
    TABLESPACE pg_default;

-- Trigger: watch_addresses_touch

-- DROP TRIGGER IF EXISTS watch_addresses_touch ON blockchain.watch_addresses;

CREATE OR REPLACE TRIGGER watch_addresses_touch
    BEFORE UPDATE 
    ON blockchain.watch_addresses
    FOR EACH ROW
    EXECUTE FUNCTION blockchain.touch_updated_at();
