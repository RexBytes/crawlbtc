-- Table: blockchain.block_jobs

-- DROP TABLE IF EXISTS blockchain.block_jobs;

CREATE TABLE IF NOT EXISTS blockchain.block_jobs
(
    height integer NOT NULL,
    status text COLLATE pg_catalog."default" NOT NULL,
    updated_at timestamp without time zone DEFAULT now(),
    CONSTRAINT block_jobs_pkey PRIMARY KEY (height),
    CONSTRAINT block_jobs_status_check CHECK (status = ANY (ARRAY['pending'::text, 'in_progress'::text, 'done'::text]))
)

TABLESPACE pg_default;

ALTER TABLE IF EXISTS blockchain.block_jobs
    OWNER to pgadmin;

REVOKE ALL ON TABLE blockchain.block_jobs FROM blockchain;

GRANT INSERT, DELETE, SELECT, UPDATE ON TABLE blockchain.block_jobs TO blockchain;

GRANT ALL ON TABLE blockchain.block_jobs TO pgadmin;
-- Index: idx_block_jobs_status

-- DROP INDEX IF EXISTS blockchain.idx_block_jobs_status;

CREATE INDEX IF NOT EXISTS idx_block_jobs_status
    ON blockchain.block_jobs USING btree
    (status COLLATE pg_catalog."default" ASC NULLS LAST)
    TABLESPACE pg_default;
