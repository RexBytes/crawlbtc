# Reclaiming disk space (do these LATER, in order)

The database is the archive; once it is verifiably complete, most of the
remaining disk usage is either redundant or compressible. Approximate
sizes from a full-chain database (July 2026: ~4.5 TB Postgres + ~1 TB node).

## Preconditions - do NOT start until ALL of these hold

1. `crawlbtc status` shows no pending/failed blocks for vout and vin.
2. `crawlbtc diagnose` is clean (genesis coinbase present as p2pk,
   no addressless-feature blocks, tip lag 0).
3. The late-spends vin repair has been run (see session notes / README).
4. `crawlbtc build-balances` has completed and spot-checks look right.

## 1. Drop the unused `id` primary keys (~250 GB, minutes)

`transaction_io` and `transactions` carry bigint `id` columns whose PK
indexes nothing in the pipeline uses (rows are identified by the txid
unique constraints). FIRST confirm no external tooling queries by `id`.

```sql
-- run as the schema owner (pgadmin)
ALTER TABLE blockchain.transaction_io DROP CONSTRAINT transaction_io_pkey;
ALTER TABLE blockchain.transactions  DROP CONSTRAINT transactions_pkey;
-- The id columns and sequences remain (harmless, 8 bytes/row);
-- removing the columns entirely requires a full table rewrite - skip.
```

The `spends` PK is (prev_txid, prev_vout) and IS used - leave it.

## 2. Prune the Bitcoin node (~600-700 GB, requires acceptance of trade-offs)

Once the database is complete, the node is only a feed for new blocks.
A pruned node serves tip-following extraction fine.

> BEFORE pruning, read `extraction-completeness.md`. Pruning permanently
> discards any block data crawlbtc has not already extracted (OP_RETURN
> payloads, tx metadata, raw scripts, etc.). If you may ever want those,
> extend extraction and do a full re-extract pass FIRST - it cannot be
> done after the blocks are gone without a full chain re-sync.

Trade-offs (accept before doing this):
- `txindex` must be disabled (incompatible with pruning).
- `crawlbtc requeue` + re-extract of OLD blocks becomes impossible without
  re-downloading the whole chain (days). The database becomes the only
  copy of extracted history - keep Postgres backups accordingly.

```
# bitcoin.conf
prune=5000        # keep ~5 GB of recent blocks
# remove/comment: txindex=1
```

Then restart bitcoind. It deletes old block files gradually.

## 3. Store txids as bytea instead of hex text (~1.5-2 TB, a project)

A txid is 32 bytes; it is currently stored as 64-char text in every row
of `transaction_io`, `spends`, `transactions` and every index on those
columns. Converting roughly halves the largest tables and their indexes.

This is a full rewrite of the biggest tables (needs large temp space and
hours of exclusive access) and CHANGES THE CONTACT POINTS: every external
query comparing `txid = '<hex>'` becomes `txid = decode('<hex>', 'hex')`.
Plan it as a versioned migration with a compatibility view if needed.
Do not attempt while low on disk - the rewrite needs headroom comparable
to the table being rewritten.

## 4. Ongoing growth expectations

- Chain growth adds roughly 300-400 GB/year to the database at current
  block sizes (tables + indexes).
- `blockchain.address_balances` is rebuilt in place by `crawlbtc
  build-balances`; it does not accumulate.
- After measures 1 + 2, expect ~2 TB free on a 7.3 TB disk - several
  years of runway before measure 3 becomes necessary.
