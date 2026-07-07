# crawlbtc

A Bitcoin blockchain data extraction pipeline, packaged as an installable
CLI. It connects to a local Bitcoin Core node via RPC, extracts structured
data (blocks, transactions, inputs/outputs, spend edges), and stores it in
PostgreSQL for indexing, analysis, or research.

Satoshi-era coins are fully tracked: raw-pubkey (P2PK) outputs, which modern
Bitcoin Core reports without an `address` field, are resolved by deriving
their canonical P2PKH form (`1...`) directly from the public key — the same
address every block explorer shows for those coins.

---

## 🧰 Requirements

- Python 3.11+
- PostgreSQL 13+ (native install; tested with 16)
- A synced Bitcoin Core node with RPC enabled
  (Core ≥ 25 recommended — `getblock` verbosity 3 lets the extractor pull
  vouts *and* vins in a single pass; older nodes fall back automatically)

## 📦 Install

```bash
pipx install .        # recommended: isolated install with the crawlbtc command
# or
pip install .
```

## 🔧 Configuration

Same environment variables as always. Put them in a `.env` in the directory
you run from, **or** point at a file anywhere:

```bash
crawlbtc --env-file ~/myenvfiles/crawlbtc.env <command>
# or
export CRAWLBTC_ENV_FILE=~/myenvfiles/crawlbtc.env
```

```
RPC_HOST=127.0.0.1
RPC_PORT=8332
RPC_USER=bitcoin
RPC_PASSWORD=yourpassword

PG_HOST=localhost
PG_PORT=5432
PG_DB=crawlbtc
PG_USER=blockchain
PG_PASSWORD=yourdbpassword

LOG_LEVEL=progress          # same two-line JSON progress stream as before
```

Tuning (all optional, all auto-sized from CPU/RAM/postgres capacity):
`POWER`, `NUM_WORKERS`, `PROCESSES`, `RPC_CONCURRENCY`, `DB_MAX_CONN`,
`DB_WRITE_CONCURRENCY`, `JOB_BATCH_SIZE`, `DB_POOL_TIMEOUT`, `START_DELAY`.

## 🗄️ Database setup

Fresh install:

```bash
createdb -U pgadmin crawlbtc
crawlbtc init-db            # or: crawlbtc init-db --show-sql | psql -U pgadmin -d crawlbtc
```

Existing database (crawled with the legacy scripts):

```bash
crawlbtc migrate
```

`migrate` is additive and safe to re-run: it adds the `p2pk` address type
and drops ~14 duplicate/redundant indexes (a large write-speed win on
`transaction_io`).

## 🚀 Usage

```bash
crawlbtc extract            # blocks -> transactions, vouts, vins, spends (single pass)
crawlbtc scan-addresses     # apply per-block balance deltas to watch_addresses
crawlbtc run-all            # extract -> backfill-vins -> scan-addresses

crawlbtc status             # per-phase job counts
crawlbtc diagnose           # full health report (db, node, P2PK coverage, index audit)

crawlbtc config             # show every config source (env file, bitcoin.conf, postgres)
crawlbtc config backup      # snapshot those configs to a timestamped dir
crawlbtc config restore DIR # restore env/bitcoin.conf (dry-run; add --force to write)

crawlbtc backfill-vins      # repair pass (only needed for verbosity-2 nodes / legacy data)
crawlbtc requeue --phase vout --skipped     # reset blocks for reprocessing
crawlbtc recompute-balances                 # exact watch_addresses rebuild from io/spends
crawlbtc build-balances     # materialize EVERY address's balance into blockchain.address_balances
```

`-P/--processes`, `-w/--workers`, `-b/--batch-size` override the auto-sizing
per run. Multiple processes parallelize the CPU-bound JSON parsing of block
payloads; the `FOR UPDATE SKIP LOCKED` job queue keeps them coordinated.

## ⚡ What changed vs. the legacy scripts

Contact points are identical — same tables/columns, same env vars, same
JSON progress log — but the engine is faster:

1. **One pass instead of two.** `extract` fetches each block once at
   verbosity 3 and writes vouts, vins, totals and spend edges together.
   The legacy pipeline fetched and parsed the entire chain twice.
2. **Multi-core parsing.** N worker processes (`-P`) each run their own
   event loop; block JSON is decoded with orjson. The legacy scripts parsed
   everything on one core regardless of worker count.
3. **Leaner indexes.** `crawlbtc migrate` removes duplicate indexes that
   multiplied every insert's write cost.
4. **P2PK (Satoshi-era) coverage.** Previously those outputs were dropped
   and P2PK-only blocks were marked `skipped`.

### Picking up Satoshi-era coins on an existing database

```bash
crawlbtc migrate                            # adds the p2pk enum value
crawlbtc requeue --phase vout --skipped     # re-queue blocks dropped as P2PK-only
crawlbtc requeue --phase vout --from 0 --to 300000   # optional: rescan the early era fully
crawlbtc extract                            # reprocess (idempotent; only missing rows are added)
crawlbtc recompute-balances                 # rebuild watch_addresses exactly
```

Run `crawlbtc diagnose` first — it samples early blocks, checks for the
known genesis-era addresses, and tells you exactly what needs requeueing.

### Address balances

Two models, both derived purely from `transaction_io` + `spends` (no node):

- `watch_addresses` — a curated set you actively track; `scan-addresses`
  keeps it current incrementally, `recompute-balances` rebuilds it exactly.
- `blockchain.address_balances` — every address on the chain, materialized
  by `crawlbtc build-balances` (full rebuild; a large batch job on a
  full-chain database). Includes balance, UTXO count, total received/spent.

## 💾 Disk space

A full-chain database is several TB. When space gets tight, see
[docs/reclaiming-space.md](docs/reclaiming-space.md) for ordered,
tested-tradeoff measures (unused PK indexes ~250 GB, node pruning
~700 GB, txid bytea conversion ~1.5-2 TB) and the preconditions for each.

## 🧪 Tests

```bash
pip install pytest && pytest tests/
```

## 📜 Legacy scripts

The original standalone scripts remain in `scripts/` and still work, but
are superseded by the CLI. `main.py` is the legacy launcher for them.
