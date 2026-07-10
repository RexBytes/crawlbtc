# Feature roadmap & database impact

Implementation effort is *my* coding time (S = hours, M = ~half–1 day,
L = multi-day). "Run cost" is what YOUR machine spends once, separately.

## The key question: what touches the 4.5 TB tables?

Almost nothing needs a reindex. Categories, cheapest → most expensive:

- **None** — pure code/SQL over existing data. No DB change.
- **New table** — a fresh table with its own (small/medium) indexes. Never
  touches `transaction_io`/`spends`. Cheap.
- **New column on a big table** — `ALTER TABLE ADD COLUMN` is instant
  (nullable). Populating it is a one-time SQL `UPDATE` over the table (hours,
  heavy but no node needed). A new index on that column is a big
  `CREATE INDEX` (hours) — but NOT a reindex of existing indexes.
- **Re-extract** — data not currently stored (tx metadata, OP_RETURN, raw
  scripts) must be re-read from the Bitcoin node: a full chain pass (many
  hours–days of run). This is the expensive one, and the reason to decide
  the "maximal extraction" scope *before* pruning the node.
- **Full rewrite/reindex** — only the txid→bytea space optimization
  (`reclaiming-space.md`). Rewrites the biggest tables end to end.

## Done this session

Packaging + CLI; merged single-pass extract; Satoshi-era P2PK; diagnose;
migrate; requeue; recompute-balances; **build-balances** (all-address);
**backup/verify** + provenance manifest; **config** show/backup/restore;
**tags** (OFAC + exchanges + custom); **trace** (7-tab HTML + xlsx + json,
direction, confidence, level, risk, entities, collapse, explorer links,
print/PDF, related-wallet clustering, direct-vs-co-mingled attribution,
progress, bounded/safe); unbranded reports + `--report-title`.

## Done: the no-reindex batch (items #1–#9, #13, #14)

Every roadmap item that needs no re-extract and no reindex is now shipped:

- **#1 Fiat valuation** — `import-prices` + `trace --fiat CUR`; per-flow value
  at the transaction date, in the xlsx/HTML/JSON.
- **#2 Incremental balances** — `update-balances`; watermark-driven refresh of
  only the touched addresses (seconds/minutes, not the multi-hour rebuild).
- **#3 Global clusters** — `build-clusters`; common-input union-find via
  iterative SQL label propagation, resumable, with `--stats`/`--lookup`.
- **#4 Change detection**, **#5 coin-days-destroyed**, **#6 tx entropy /
  Boltzmann-lite**, **#8 peel-chain / CoinJoin flags** — `analyze-tx <txid>`
  (human or `--json`), pure transparent heuristics with reasons.
- **#7 Amount+timing correlation** — `correlate`; time-index-bounded candidate
  leads across a mixer/exchange break.
- **#9 Topological service detection** — trace auto-labels exchange-like /
  collector / distributor / hub nodes by degree shape, no tag needed.
- **#13 Reorg handling** — `detect-reorg` (dry-run/`--apply`) repairs orphaned
  blocks near the tip against the node's canonical hashes.
- **#14 Encrypt watch_addresses keys** — `encrypt-keys` / `decrypt-keys`;
  stdlib-only authenticated encryption (scrypt + HMAC-CTR + encrypt-then-MAC).

Remaining (need a chain re-read or a full reindex — deferred): #10
fingerprinting, #11 OP_RETURN/token layers, #12 raw scripts, #15 txid→bytea.

## Pending

| # | Feature | What it adds | Impl | DB / run impact | Reindex? |
|---|---------|--------------|------|-----------------|----------|
| 1 | **Fiat valuation** (`import-prices`) | BTC→GBP/USD at each tx's time; loss/quantum figures | M | New `btc_prices` table + CSV/API import. Optional `value_fiat` column on `transactions` (populate via SQL UPDATE, no node). | No |
| 2 | **Incremental balances** (`update-balances`) | keep `address_balances` current per new block in seconds, not the multi-hour full rebuild | M | Small watermark; delta SQL. | No |
| 3 | **Global clusters** (`build-clusters`) | union-find over all co-inputs → a cluster id per address; instant wallet lookup | L | New `address_clusters` table (~1B rows) + index build; heavy one-time compute + cheap incremental. | No |
| 4 | **Change-detection scoring** | fresh-address / script-match / round-number / unnecessary-input → which output is change | M | None (SQL over existing data). | No |
| 5 | **Coin age / Coin Days Destroyed** | dormant-coin-moved signal (old theft cashing out) | S | None (from `spends.spent_time`). | No |
| 6 | **Transaction entropy / Boltzmann + subset-sum** | quantify intra-tx linkability; resolve some multi-in/out attributions to *fact* | L | Optional cached column on `transactions` (populate via SQL, no node). | No |
| 7 | **Amount+timing gap correlation** | bridge mixer/exchange/chain-hop breaks probabilistically | M | Optional materialized candidate-pairs table. | No |
| 8 | **Peel-chain / CoinJoin auto-flags** | detect + flag laundering shapes and mix boundaries | M | Optional flag column on `transactions` (populate via SQL). | No |
| 9 | **Topological hub/service detection** | auto-label exchanges/mixers by graph shape (no tag needed) | M | None beyond clusters (#3). | No |
| 10 | **Wallet fingerprinting** | tx version / locktime / RBF / sequence → link txs to one wallet app; strengthen change guesses | L | New columns on `transactions` + **RE-EXTRACT** to populate (data not stored today). | Re-extract |
| 11 | **OP_RETURN / token layers** (USDT-Omni, Counterparty) | trace stablecoin/token movements, embedded data | L | New `op_returns` table + **RE-EXTRACT**. | Re-extract |
| 12 | **Raw scripts / bare-multisig / P2PK pubkeys** | capture value + scripts we currently drop | L | New columns/table + **RE-EXTRACT**. | Re-extract |
| 13 | **Reorg handling** | detect + repair orphaned-block rows near the tip | M | None (job requeue logic). | No |
| 14 | **Encrypt watch_addresses private keys** | remove plaintext keys from the DB | S | None (app-level). | No |
| 15 | **txid → bytea** (space) | ~halve the DB + backups | L | Full table **REWRITE + REINDEX**; contact-point change. | Full reindex |

## Suggested order

1. **Incremental balances** (#2) — finishes the "hardest first" list; removes the multi-hour rebuild pain.
2. **Fiat valuation** (#1) — counsel always wants £/$ at time of tx; small, high value, no re-extract.
3. **Change-detection scoring** (#4) + **coin age** (#5) — pure-code tracing rigor, no DB cost.
4. **Global clusters** (#3) — bigger, powerful; enables #9.
5. **Maximal-extraction pass** (#10–#12 together) — decide scope, do ONE re-extract while the node is still unpruned, then prune.
6. **txid→bytea** (#15) — last, when space demands it.

Items #1–#9, #13, #14 need **no re-extract and no reindex** — they're
code, new tables, or one-time SQL over data you already have. Only the
fingerprinting/OP_RETURN/scripts group (#10–#12) needs a chain re-read,
and only #15 is a true reindex.
