# Backup strategy

Two large things exist on this machine; they need **opposite** treatment.
Sizes as of July 2026: Bitcoin Core ~1 TB, PostgreSQL ~4.5 TB live.

## The one-line summary

- **Do NOT back up the Bitcoin Core block data.** It is public and
  re-downloadable. Backing up 1 TB of chain is wasted storage.
- **Do back up the database - but a logical dump is ~5-8x smaller than
  the live 4.5 TB**, because dumps exclude indexes (rebuilt on restore).
- **Back up the small curated/irreproducible data religiously**; treat the
  giant derived graph as "reproducible in days, archived occasionally".

---

## Tier 0 - do NOT back up: Bitcoin Core (~1 TB)

`blocks/` and `chainstate/` are a copy of the public chain. If lost,
`bitcoind` re-downloads and re-validates them. Zero backup value.

The ONLY node files worth saving are tiny and belong with your configs:
- `bitcoin.conf`  (captured by `crawlbtc config backup`)
- there is no wallet on a crawler node; if you ever add one, `wallet.dat`
  becomes Tier 1 and must be backed up + encrypted.

## Tier 1 - back up always, small, precious (MB to low GB)

The human/curated layer that CANNOT be re-derived from the chain:
- `blockchain.watch_addresses` - labels, tags, and **private keys** if any
- entity tag data, case annotations
- imported price series (`blockchain.btc_prices`, once it exists)
- configs: env file, bitcoin.conf, schema, role settings
  (`crawlbtc config backup`)

```bash
# Consistent, compressed, custom-format dump of just the curated tables.
pg_dump "host=127.0.0.1 dbname=postgres user=blockchain" \
  -Fc -Z9 \
  -t blockchain.watch_addresses \
  -t blockchain.btc_prices \
  -f watch_$(date +%F).dump

# watch_addresses may hold private keys -> ENCRYPT before it leaves the box
gpg --symmetric --cipher-algo AES256 watch_$(date +%F).dump
```
Store **off-machine**, encrypted, keep several dated copies. This is the
data whose loss you could never recover from. Automate it (cron/daily).

## Tier 2 - the extracted graph, large but reproducible (the 4.5 TB)

`transactions`, `transaction_io`, `spends`, `block_jobs`,
`address_balances`. Deterministically rebuildable by re-running crawlbtc
against a node - but that costs days. So: archive occasionally, accept a
multi-day rebuild as the fallback.

**Key size fact:** a logical dump contains table DATA + schema, NOT
indexes (those ~2.5 TB rebuild on restore). The dumpable table data is
~2 TB, and it is highly compressible (hex txids, repeated addresses):
expect a **~500-800 GB compressed dump** from a 4.5 TB live database.

```bash
# Directory-format dump, parallel across tables, compressed.
# Excludes address_balances (cheap to rebuild with `crawlbtc build-balances`).
pg_dump "host=127.0.0.1 dbname=postgres user=blockchain" \
  -Fd -j 4 -Z6 \
  --exclude-table=blockchain.address_balances \
  -f /mnt/external/crawlbtc_$(date +%F).dumpdir

# Restore (rebuilds indexes - hours):
pg_restore -d postgres -j 4 /mnt/external/crawlbtc_2026-07-09.dumpdir
```
Caveats:
- Dump time on 6.3 B rows is long (bottlenecked on the biggest table,
  which is one job) - plan for many hours. Restore + index build is
  comparable. This is a "monthly / after-milestone" job, not nightly.
- **Needs external storage** - the primary NVMe is ~80% full. A network
  NAS is the natural target (e.g. the QNAP at /mnt/qnap here, ~50 TB
  free). A ~500-800 GB compressed dump writes over the network in a
  couple of hours on gigabit; both Tier 1 and Tier 2 backups should land
  there, NOT on the primary disk (a same-disk backup protects nothing).
- **A NAS backup is good but not sufficient alone** for evidence-grade
  data: the NAS should itself be redundant (RAID), and at least one copy
  of Tier 1 (the irreplaceable curated/keys data) should also go OFFLINE
  / off-site (ransomware, fire, NAS failure). Test-restore periodically.

### Alternative: physical base backup + WAL archiving (PITR)
The workload is almost append-only (new blocks added, old rarely
changed), which is the ideal case for WAL archiving:
- one physical `pg_basebackup` (size ~= live DB, so needs big external
  storage), then
- continuously archive WAL segments (small, only cover new blocks).
Gives point-in-time recovery and fast restore (start Postgres on the
copy). Heavier to operate than periodic dumps; worth it only if fast RTO
matters. If the data dir sits on ZFS/LVM/btrfs, filesystem snapshots +
incremental send is the least-effort version of this.

## Recovery time objective (be honest about it)

| Scenario | Recovery |
|---|---|
| Lose Tier 1 backup | **Unrecoverable** - curation/keys gone forever. Never let this happen. |
| Lose DB, have Tier 2 dump | `pg_restore` + index rebuild: hours to ~1 day |
| Lose DB, no Tier 2 dump | Re-sync node (if also lost) + `crawlbtc run-all`: several days, fully automated |
| Lose Bitcoin Core only | `bitcoind` re-downloads: hours, no action needed |

## Forensic / legal integrity (this is an evidence service)

- **Encrypt** every backup that leaves the machine (private keys, and
  client-linked analysis are sensitive; GDPR / chain-of-custody).
- **Record provenance with each backup**: chain tip height + block hash,
  row counts per table, `crawlbtc.__version__`, and a checksum of the
  dump. Store alongside the backup so a restored dataset can be proven
  identical to what a report was based on.
- **Immutability**: keep dated, write-once copies so a backup cannot be
  silently altered - matters if a dataset underpins filed evidence.
- Test-restore periodically: an untested backup is not a backup.

## Interaction with disk reclamation

The txid->bytea conversion in `reclaiming-space.md` roughly halves the
live database AND the dump size - do that first and every backup gets
cheaper. Node pruning (also in that doc) does not affect backups here,
since we do not back the node up anyway.
