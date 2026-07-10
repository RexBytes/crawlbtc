# Extraction completeness & the pre-prune checklist

Pruning Bitcoin Core deletes the raw blocks. Anything crawlbtc did not
extract into PostgreSQL **before** pruning is gone for good (short of a
full chain re-sync from the network). So decide, deliberately, what is
worth keeping BEFORE you prune.

The current pipeline extracts the **value-flow graph** and nothing else.
That is the correct minimal set for balances and tracing - but it is a
subset of what a block contains.

## What crawlbtc extracts today

| Stored | Table |
|---|---|
| txid, block hash, block time | `transactions` |
| total in / total out (=> fee is derivable for non-coinbase) | `transactions` |
| each output: address, address_type, amount, index | `transaction_io` (io_type='out') |
| each input: resolved address, amount, index | `transaction_io` (io_type='in') |
| spend edges: prev output -> spending tx/input, height, time | `spends` |
| per-block/phase job + feature state | `block_jobs` |

This answers: balances, who-paid-whom, when, and the full spend graph.

## What a block contains that we DROP

Ordered roughly by likely value to a tracing service.

1. **OP_RETURN payloads** - currently skipped entirely. These carry token
   layers and embedded data: **Omni / Tether (USDT)**, Counterparty
   assets, timestamping, ordinals/inscription metadata, messages. For
   asset tracing this is increasingly relevant and is **the most likely
   regret** if pruned without capturing it.
2. **Transaction metadata**: version, locktime, input `sequence` (=> RBF
   signaling), vsize/weight (=> fee *rate*). Listed in
   `tracing-techniques.md` as required for **wallet fingerprinting**.
   Tiny to store; impossible to recover after pruning.
3. **Bare multisig & nonstandard outputs** - have no single address, so
   today their value is attributed to no one and the output is dropped.
   Niche, but it is real un-attributed value (some early/coinbase and
   exotic scripts). Capturing the raw `scriptPubKey` hex preserves them.
4. **Coinbase scriptSig message** - miner/pool tags and arbitrary data;
   useful for mining-pool attribution. Tiny.
5. **Raw scriptPubKey / P2PK pubkeys** - we derive the address; the raw
   pubkey/script is discarded. Occasionally useful (pubkey recovery).
6. **Block header fields** - version, merkle root, bits/difficulty, nonce,
   size, weight, prev-hash. The `blocks` table exists but is unused/empty.
   Low forensic value; trivial to store if wanted.
7. **Witness / scriptSig signature data** - signatures, pubkeys, redeem
   scripts. **Deliberately NOT recommended for storage**: it is enormous
   (would roughly double the database) and rarely needed. Capture only if
   a specific need exists.

## Recommendation: extract-max, verify, THEN prune

To honour the condition "prune only once we've extracted all we could":

1. **Decide scope.** Recommended keep-set for a tracing service:
   OP_RETURN payloads (1), tx metadata (2), raw script hex for otherwise-
   dropped outputs (3), coinbase message (4). Skip full witness data (7)
   unless justified - the size cost is not worth it.
2. **Extend the schema + extractor** to capture the chosen fields
   (additive columns/tables; the single-pass extractor already has the
   full verbosity-3 block in hand, so most fields are a parse-and-store
   away, no extra RPC cost).
3. **Full re-extract pass** over the whole chain against the STILL-UNPRUNED
   node (`crawlbtc requeue --phase vout --from 0 --to <tip>` then
   `extract`). Idempotent; adds only the new columns' data.
4. **Verify** with `diagnose` + spot checks that the new fields are
   populated across eras (early, segwit, taproot, recent).
5. **Back up** (see `backup-strategy.md`) - a Tier 2 dump at known tip.
6. **Only now prune.** After this, the database is a genuine superset of
   what pruning removes, and `prune=5000` (per `reclaiming-space.md`) is
   safe: the node becomes a pure tip-follower and the DB is the archive.

## If you prune WITHOUT extending extraction

Acceptable only if you are certain the value-flow graph is all the service
will ever need. You permanently lose the ability to: trace token layers
(USDT/Counterparty via OP_RETURN), fingerprint historical wallets, and
recover bare-multisig/nonstandard value - unless you re-sync the full
chain from the network (days) to re-extract.

## One-way-door summary

- Fields we ALREADY store: safe to prune anytime.
- Fields we DROP: pruning is irreversible for them. Extend extraction
  first if there is any chance you will want them.
