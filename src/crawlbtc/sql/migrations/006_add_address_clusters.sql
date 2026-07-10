-- 006: global wallet clusters (common-input-ownership).
-- One row per address that has ever been spent as a transaction input, mapping
-- it to the canonical (lexicographically smallest) address of its cluster.
-- Populated by `crawlbtc build-clusters` (iterative label propagation). Heavy
-- one-time compute; the table itself is additive and touches nothing else.
CREATE TABLE IF NOT EXISTS blockchain.address_clusters (
    address    text PRIMARY KEY,
    cluster_id text NOT NULL
);
CREATE INDEX IF NOT EXISTS address_clusters_cid_idx
    ON blockchain.address_clusters (cluster_id);
