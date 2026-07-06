-- Drops duplicate/redundant indexes accumulated on the legacy schema.
-- Every one of these is either an exact duplicate of another index or a
-- prefix of the unique constraint (txid, io_type, idx); each removed index
-- was pure write amplification on the highest-volume tables.
-- Safe to run repeatedly.

-- transaction_io: keep the unique constraint + ONE (address, io_type) index.
DROP INDEX IF EXISTS blockchain.idx_transaction_io_txid;             -- prefix of unique constraint
DROP INDEX IF EXISTS blockchain.idx_transaction_io_txid_idx;         -- covered by unique constraint
DROP INDEX IF EXISTS blockchain.idx_transaction_io_txid_io_idx;      -- exact duplicate of unique constraint
DROP INDEX IF EXISTS blockchain.idx_transaction_io_address;          -- prefix of (address, io_type)
DROP INDEX IF EXISTS blockchain.idx_transaction_io_address_and_type; -- (address, address_type): unused
DROP INDEX IF EXISTS blockchain.idx_transaction_io_address_type;     -- duplicate of (address, io_type)
DROP INDEX IF EXISTS blockchain.txio_address_io_idx;                 -- duplicate of (address, io_type)
DROP INDEX IF EXISTS blockchain.idx_transaction_io_io_type;          -- 2-value column, useless alone
DROP INDEX IF EXISTS blockchain.idx_transaction_io_type_io;          -- (address_type, io_type): unused

-- transactions
DROP INDEX IF EXISTS blockchain.transactions_block_hash_idx;         -- duplicate of idx_transactions_block_hash

-- spends
DROP INDEX IF EXISTS blockchain.spends_spending_idx;                 -- duplicate of unique constraint

-- block_jobs: legacy 'status' column indexes + duplicate updated_at index
DROP INDEX IF EXISTS blockchain.idx_block_jobs_status;
DROP INDEX IF EXISTS blockchain.idx_block_jobs_status_features;
DROP INDEX IF EXISTS blockchain.idx_block_jobs_height_status;
DROP INDEX IF EXISTS blockchain.idx_block_jobs_updated;              -- duplicate of idx_block_jobs_updated_at
DROP INDEX IF EXISTS blockchain.block_jobs_address_status_idx;       -- superseded by partial sched index

-- Partial scheduling indexes for the claim queries (cheap: only pending rows).
CREATE INDEX IF NOT EXISTS block_jobs_vout_sched_idx ON blockchain.block_jobs (height)
    WHERE vout_status = 'pending';
CREATE INDEX IF NOT EXISTS block_jobs_vin_sched_idx ON blockchain.block_jobs (height)
    WHERE vin_status = 'pending'
      AND vout_status IN ('done', 'skipped');
