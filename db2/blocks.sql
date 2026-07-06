-- Table: blockchain.blocks

-- DROP TABLE IF EXISTS blockchain.blocks;

CREATE TABLE IF NOT EXISTS blockchain.blocks
(
    id bigint NOT NULL DEFAULT nextval('blockchain.blocks_id_seq'::regclass),
    block_hash text COLLATE pg_catalog."default" NOT NULL,
    height integer NOT NULL,
    "timestamp" timestamp without time zone NOT NULL,
    processed_at timestamp without time zone DEFAULT now(),
    CONSTRAINT blocks_pkey PRIMARY KEY (id),
    CONSTRAINT blocks_block_hash_key UNIQUE (block_hash),
    CONSTRAINT blocks_height_key UNIQUE (height)
)

TABLESPACE pg_default;

ALTER TABLE IF EXISTS blockchain.blocks
    OWNER to pgadmin;

REVOKE ALL ON TABLE blockchain.blocks FROM blockchain;

GRANT INSERT, DELETE, SELECT, UPDATE ON TABLE blockchain.blocks TO blockchain;

GRANT ALL ON TABLE blockchain.blocks TO pgadmin;
