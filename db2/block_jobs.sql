-- Table: blockchain.block_jobs

-- DROP TABLE IF EXISTS blockchain.block_jobs;

CREATE TABLE IF NOT EXISTS blockchain.block_jobs
(
    height integer NOT NULL,
    status blockchain.block_job_status NOT NULL DEFAULT 'pending'::blockchain.block_job_status,
    updated_at timestamp without time zone DEFAULT now(),
    features blockchain.block_feature NOT NULL DEFAULT 'none'::blockchain.block_feature,
    vout_status blockchain.block_job_status NOT NULL DEFAULT 'pending'::blockchain.block_job_status,
    vin_status blockchain.block_job_status NOT NULL DEFAULT 'pending'::blockchain.block_job_status,
    address_status blockchain.block_job_status NOT NULL DEFAULT 'pending'::blockchain.block_job_status,
    CONSTRAINT block_jobs_pkey PRIMARY KEY (height)
)

TABLESPACE pg_default;

ALTER TABLE IF EXISTS blockchain.block_jobs
    OWNER to pgadmin;

REVOKE ALL ON TABLE blockchain.block_jobs FROM blockchain;

GRANT INSERT, DELETE, SELECT, UPDATE ON TABLE blockchain.block_jobs TO blockchain;

GRANT ALL ON TABLE blockchain.block_jobs TO pgadmin;
-- Index: block_jobs_addr_sched_idx

-- DROP INDEX IF EXISTS blockchain.block_jobs_addr_sched_idx;

CREATE INDEX IF NOT EXISTS block_jobs_addr_sched_idx
    ON blockchain.block_jobs USING btree
    (height ASC NULLS LAST)
    TABLESPACE pg_default
    WHERE address_status = 'pending'::blockchain.block_job_status AND (vout_status = ANY (ARRAY['done'::blockchain.block_job_status, 'skipped'::blockchain.block_job_status])) AND vin_status = 'done'::blockchain.block_job_status;
-- Index: block_jobs_address_status_idx

-- DROP INDEX IF EXISTS blockchain.block_jobs_address_status_idx;

CREATE INDEX IF NOT EXISTS block_jobs_address_status_idx
    ON blockchain.block_jobs USING btree
    (address_status ASC NULLS LAST, height ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_block_jobs_features

-- DROP INDEX IF EXISTS blockchain.idx_block_jobs_features;

CREATE INDEX IF NOT EXISTS idx_block_jobs_features
    ON blockchain.block_jobs USING btree
    (features ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_block_jobs_height_status

-- DROP INDEX IF EXISTS blockchain.idx_block_jobs_height_status;

CREATE INDEX IF NOT EXISTS idx_block_jobs_height_status
    ON blockchain.block_jobs USING btree
    (height ASC NULLS LAST, status ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_block_jobs_status

-- DROP INDEX IF EXISTS blockchain.idx_block_jobs_status;

CREATE INDEX IF NOT EXISTS idx_block_jobs_status
    ON blockchain.block_jobs USING btree
    (status ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_block_jobs_status_features

-- DROP INDEX IF EXISTS blockchain.idx_block_jobs_status_features;

CREATE INDEX IF NOT EXISTS idx_block_jobs_status_features
    ON blockchain.block_jobs USING btree
    (status ASC NULLS LAST, features ASC NULLS LAST)
    TABLESPACE pg_default;
-- Index: idx_block_jobs_updated

-- DROP INDEX IF EXISTS blockchain.idx_block_jobs_updated;

CREATE INDEX IF NOT EXISTS idx_block_jobs_updated
    ON blockchain.block_jobs USING btree
    (updated_at DESC NULLS FIRST)
    TABLESPACE pg_default;
-- Index: idx_block_jobs_updated_at

-- DROP INDEX IF EXISTS blockchain.idx_block_jobs_updated_at;

CREATE INDEX IF NOT EXISTS idx_block_jobs_updated_at
    ON blockchain.block_jobs USING btree
    (updated_at ASC NULLS LAST)
    TABLESPACE pg_default;
