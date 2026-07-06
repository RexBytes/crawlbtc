# Bitcoin tracing techniques

Reference for building tracing on top of the crawlbtc database, written for
a service supporting legal work (asset recovery, litigation, disclosure).
Ordered from deterministic facts to weaker heuristics. For evidentiary use,
every reported trace should state which techniques produced it and with
what confidence - courts and opposing experts will probe exactly this.

---

## 0. What is fact and what is inference

**Fact (no probability involved):**
- Every transaction input consumes exactly one identified previous output.
  The `blockchain.spends` table is this graph, complete: coin-level
  history is a deterministic graph traversal.
- Amounts, addresses, timestamps, block inclusion and ordering.
- Fees: `total_in - total_out` per transaction (miner reward txs aside).

**Inference (heuristic, must carry confidence):**
- Which *person/entity* controls an address.
- Within one transaction, which input "became" which output (value is
  pooled; there is no ground truth).
- Whether two addresses belong to the same wallet.

A defensible report keeps these layers visibly separate.

---

## 0a. "Did they move the coins, and where to?" - split the question

The most common client question ("the target held X BTC here - where did
it go?") is really two questions of very different difficulty. Blurring
them is the main way tracing gets misrepresented, in both directions.

### Question A - did the coins move, and into which transaction?
**Deterministic. Trivial.** An output either has a row in
`blockchain.spends` (moved - and you see exactly which transaction
consumed it, at which height and time) or it does not (still unspent).
No heuristic. Every departure from an address:

```sql
SELECT s.spending_txid, s.spent_height, s.spent_time
  FROM blockchain.spends s
  JOIN blockchain.transaction_io i
    ON i.txid = s.prev_txid AND i.idx = s.prev_vout
 WHERE i.address = %(address)s AND i.io_type = 'out';
```

Movement itself is provable to an evidentiary standard. You can follow
every hop with certainty *as far as the coins travel* - the certainty is
in the edges; the only question is which edge to follow at a fork.

### Question B - which output is *their* onward coin vs a payment to someone else?
This is the hard part, and it is narrower than "we can't tell if they
moved funds." It is exactly the change-identification problem (section
2.2). The movement is certain; the ambiguity is only branch selection at
each multi-output hop. In practice it is usually resolvable, because the
common cases are simple:

- **Whole-balance sweep** (flee/consolidate to a new wallet): often one
  input, one output -> **zero ambiguity**. Very common.
- **Self-transfer / consolidation**: several of their own addresses into
  one new address -> common-input heuristic ties it, near-certain.
- **Ordinary payment with change**: two outputs; the change signals in
  2.2 usually pick the onward (change) branch with high confidence.

Genuinely hard only in deliberate-obfuscation cases (CoinJoin, custodial
mixer, chain-hop through an exchange - section 5). There the honest
result is "trail reaches <service/mix>; here are the candidate successors
and the legal process to pierce it." That is the true state of knowledge,
not a gap in the data.

### Framing for clients / counsel
- *Movement* is provable.
- *Destination through ordinary wallets* is traceable, with stated
  per-hop confidence.
- *Destination through a mixer/exchange* terminates at the service and
  converts into a legal-process question (subpoena / disclosure order).

Difficulty scales with how hard the target worked to hide. A target who
simply moved coins to a fresh address - the majority - is fully
traceable. Overstating certainty through a mixer, or understating it for
a plain sweep, are both failures; match the claim to the case.

---

## 1. Deterministic graph traversal

The foundation. Forward ("where did the coins go") and backward ("where
did they come from") walks over `spends`, bounded by hops/time/value.

```sql
WITH RECURSIVE trail AS (
  SELECT prev_txid, prev_vout, spending_txid, spending_vin, spent_height, 1 AS hop
    FROM blockchain.spends
   WHERE prev_txid = %(txid)s AND prev_vout = %(vout)s
  UNION ALL
  SELECT s.prev_txid, s.prev_vout, s.spending_txid, s.spending_vin, s.spent_height, t.hop + 1
    FROM blockchain.spends s
    JOIN trail t ON s.prev_txid = t.spending_txid
   WHERE t.hop < %(max_hops)s
)
SELECT * FROM trail;
```

Caveat: an unbounded forward walk explodes combinatorially once value
passes through high-traffic services. Traversal must be pruned by the
value-attribution model (section 3).

## 2. Address clustering (who owns what)

### 2.1 Common-input-ownership (multi-input) heuristic
All inputs of a transaction are usually signed by the same entity
(Meiklejohn et al., 2013). Union-find over every transaction's input
addresses collapses hundreds of millions of addresses into wallet
clusters. **The highest-value heuristic in existence.**
- Breaks on: CoinJoin, PayJoin, exchange batched withdrawals from pooled
  wallets (still one entity, but the entity is the exchange, not the user).
- Mitigation: detect CoinJoin-shaped transactions first (section 5) and
  exclude them from clustering.

### 2.2 Change-output identification
One output of a typical payment returns change to the sender. Signals
(individually weak, combined strong; all computable from this schema):
- **Fresh address**: the change address has never appeared before; the
  payment address often has history.
- **Script-type match**: change usually matches the input address type
  (inputs bech32 -> change bech32; the odd-one-out is the payment).
- **Round numbers**: payments tend to be round (in BTC or in fiat at the
  block's exchange rate); change is a ragged remainder.
- **Unnecessary-input test**: if some input was unnecessary to cover
  output A, then A is not the payment the sender sized inputs for.
- **No self-transition**: an output paying an input address directly is
  near-certain change (address reuse).
- **Position bias**: older wallets placed change at a fixed index
  (weak; modern wallets randomize; BIP69 wallets sort).

### 2.3 Wallet fingerprinting
Wallet software leaves stable tells: transaction version, nLockTime
behavior (Core sets it to tip height; many wallets leave 0), RBF
signaling, input ordering (BIP69 vs not), script/address types, fee-rate
patterns, output count habits. Useful to (a) link transactions to one
wallet app and (b) strengthen or veto change guesses.
> Schema note: nLockTime/version/RBF are not currently extracted -
> requires adding columns to `transactions` and re-extracting (cheap to
> add to the single-pass extractor).

### 2.4 Behavioral/temporal clustering
Spending-hour histograms (time zone inference), periodicity (payroll,
scheduled sweeps), co-spending across time. Corroborating evidence, not
primary.

## 3. Value attribution within a transaction (taint models)

Where the "probability analysis" lives. All are *conventions* - state
which one a report uses; serious work computes several and reports ranges.

| Model | Rule | Character |
|---|---|---|
| **Haircut** | value flows pro-rata from all inputs to all outputs | smooth dilution; taint never disappears, becomes homeopathic |
| **FIFO** | first-in value maps to first-out value | deterministic, order-dependent; has been argued in legal contexts by analogy to Clayton's Rule for account tracing |
| **LIFO** | last-in maps to first-out | as FIFO, different convention |
| **Poison** | any tainted input taints all outputs 100% | maximal, over-inclusive; useful for screening, not attribution |
| **Change-aware flow** | attribute payment vs change first (2.2), then apply a model only across the payment path | closest to how funds "really" move; inherits change-detection confidence |

Practical guidance for legal work: run poison for candidate discovery,
then FIFO + haircut + change-aware on the candidate paths, and report
where they agree/diverge. Agreement across models is a strong statement;
divergence is honest uncertainty.

## 4. Entity identification (turning clusters into names)

- **Tag database**: label known addresses/clusters - exchange deposit and
  hot wallets, payment processors, gambling sites, darknet markets,
  ransomware campaigns, sanctioned entities (OFAC publishes addresses).
  `watch_addresses.tags` (jsonb) is the natural store.
- **Exchange deposit pattern**: a fresh address that receives once and is
  swept into a known exchange hot wallet is a *deposit address of a
  customer of that exchange*. For lawyers this is the endgame: the trace
  terminates at a VASP that can be subpoenaed / served a disclosure order
  (Norwich Pharmacal / Bankers Trust orders in UK practice; 1782
  discovery in the US) for KYC records.
- **OSINT**: reused addresses posted publicly (forums, invoices, court
  records, breach dumps), vanity addresses, dust-attack responses.
- **Counterparty inference**: repeated interaction with a labeled cluster.

## 5. Obfuscation recognition (know when confidence collapses)

Do not trace *through* these naively; detect, flag, and report them.

- **CoinJoin** (Wasabi, Whirlpool, JoinMarket): many equal-value outputs,
  many inputs, characteristic denominations. Detectable by shape.
  Post-mix, per-output attribution drops to ~1/N; report "entered mix,
  N candidate successors" - anyone claiming better is overselling.
  (Subset-sum analysis can sometimes partition sloppy CoinJoins.)
- **PayJoin (P2EP)**: receiver contributes an input - silently breaks the
  common-input heuristic. Hard to detect; a reason clustering is never
  100%.
- **Peel chains**: a large UTXO repeatedly "peels" small payments, change
  rolling forward through fresh addresses. Classic theft/laundering
  pattern and *good* news for tracing: the chain is followable
  (change-detection at every hop) and characteristic. Automate its
  recognition: long chains of 2-output txs where one output continues.
- **Mixers/tumblers (custodial)**: value disappears into a pool and
  re-emerges unlinked; on-chain linkage is genuinely broken - timing and
  amount correlation sometimes helps; legal process against the operator
  (where possible) works better.
- **Chain-hopping**: swap to another chain via an exchange or bridge and
  back. On-chain trace terminates at the service; correlate timing and
  amounts across chains, then use legal process on the service.
- **Lightning**: opens/closes are on-chain (visible channel points);
  everything between is off-chain and out of scope of this dataset.
- **CoinSwap / atomic swaps, Taproot-based protocols**: designed to look
  like ordinary payments; treat "we can always trace" claims skeptically.

## 6. Reporting standards for legal use

- **Reproducibility**: every trace should be re-derivable from the
  database: record txids/vouts of the full path, heuristics applied,
  model parameters, and the code version (`crawlbtc.__version__`).
- **Separate fact from inference** in the report structure, per section 0.
- **Confidence**: attach per-hop confidence (deterministic hop = 1.0;
  change-guess hop = its combined signal score; mixer = 1/N), and
  multiply along paths so long chains honestly decay.
- **Data provenance**: the database derives from a local Bitcoin Core
  node's validated chain; note node version and block hash at report
  time (chain tip pin) so findings anchor to an immutable state.
- **Limitations section**: always disclose the failure modes in section 5.
  Credibility with a court survives on candor about what cannot be known.

## 7. Implementation roadmap on this schema

| Piece | Needs | Status |
|---|---|---|
| Graph traversal / `trace` command | `spends` | data ready; CLI command to build |
| `trace` branch-selection (Question B) | change signals in 2.2 + per-hop confidence output | data ready; ranks onward path with scores, flags mixers |
| Cluster table (`build-clusters`) | union-find over input co-occurrence | data ready; batch job to build |
| Change-detection scoring | `transaction_io` (idx, address_type, amounts), address first-seen | data ready |
| CoinJoin/peel-chain flags | per-tx output shape | data ready; flag column or view |
| Wallet fingerprinting | tx version/locktime/RBF | **schema addition + re-extract** |
| Entity tags | `watch_addresses.tags` | store exists; needs curation/import |
| Fiat valuation at time of tx | external price series table | external data to import |

## References

- Meiklejohn et al., *A Fistful of Bitcoins: Characterizing Payments Among
  Men with No Names* (2013) - clustering heuristics foundation.
- Androulaki et al., *Evaluating User Privacy in Bitcoin* (2013).
- Ron & Shamir, *Quantitative Analysis of the Full Bitcoin Transaction
  Graph* (2013).
- Kalodner et al., *BlockSci: Design and applications of a blockchain
  analysis platform* (2020).
- Möser & Böhme, work on CoinJoin/mixer effectiveness.
- FATF, *Updated Guidance for a Risk-Based Approach to Virtual Assets and
  VASPs* - the regulatory frame counterparties operate under.
