"""Single-transaction forensic analysis (`crawlbtc analyze-tx <txid>`).

Pure, testable heuristics over one transaction's inputs and outputs, plus a DB
wrapper that fetches the data. Bundles four roadmap items that are all pure
computation over data already stored (no re-extract, no reindex):

  #4 change-output detection   - which output is the sender keeping
  #5 coin days destroyed       - dormant-coin-moved signal (old coins cashing out)
  #6 transaction entropy       - Boltzmann-style linkability / interpretation count
  #8 peel-chain / CoinJoin      - laundering-shape and mixing flags

The heuristics are intentionally transparent (each returns its reasons), because
in an evidence context the *why* matters as much as the verdict.
"""

import math
import sys
from itertools import combinations

import psycopg

SATS = 100_000_000


def _connect(cfg):
    return psycopg.connect(cfg.db_conninfo, autocommit=True)


# --------------------------------------------------------------------------- #
#  #5  Coin age / Coin Days Destroyed
# --------------------------------------------------------------------------- #
def coin_days_destroyed(inputs):
    """inputs: [{'value': sats, 'age_days': float}]. Returns CDD + dormancy.

    CDD = sum(value_btc * age_days). A large CDD, or a very old input moving,
    is the classic 'dormant coins suddenly spent' signal (e.g. old theft or
    early-miner coins cashing out).
    """
    cdd = 0.0
    max_age = 0.0
    for i in inputs:
        age = max(0.0, float(i.get("age_days") or 0.0))
        cdd += (i["value"] / SATS) * age
        max_age = max(max_age, age)
    return {
        "coin_days_destroyed": round(cdd, 2),
        "oldest_input_days": round(max_age, 1),
        "oldest_input_years": round(max_age / 365.25, 2),
        "dormant": max_age >= 365.25 * 2,   # untouched 2y+ then moved
    }


# --------------------------------------------------------------------------- #
#  #4  Change-output detection
# --------------------------------------------------------------------------- #
def _trailing_zero_sats(v):
    """Roundness proxy: trailing zero digits of the sats amount."""
    if v <= 0:
        return 0
    z = 0
    while v % 10 == 0:
        v //= 10
        z += 1
    return z


def detect_change_output(inputs, outputs):
    """Which output is the sender's change. Heuristic, with reasons.

    inputs/outputs: [{'address', 'value' (sats), 'script_type', 'is_fresh'}].
    outputs additionally may carry 'is_fresh' (address unseen before this tx).
    Returns {'index', 'address', 'confidence', 'reasons'} or None when it can't
    reasonably tell (coinbase, single output, or a mixing shape).
    """
    if not inputs or len(outputs) < 2:
        return None
    in_addrs = {i["address"] for i in inputs if i.get("address")}
    in_types = [i.get("script_type") for i in inputs if i.get("script_type")]
    dominant_in_type = max(set(in_types), key=in_types.count) if in_types else None
    min_in = min(i["value"] for i in inputs)

    # A repeated equal output value (CoinJoin denomination) makes change
    # undecidable - bail rather than guess.
    vals = [o["value"] for o in outputs]
    if any(vals.count(v) >= 3 for v in set(vals)):
        return None

    scores = []
    for idx, o in enumerate(outputs):
        s, why = 0.0, []
        if o.get("address") and o["address"] in in_addrs:
            s += 3.0; why.append("output pays back an input address (self-change)")
        if o.get("is_fresh"):
            s += 1.0; why.append("fresh (never-before-seen) address")
        if dominant_in_type and o.get("script_type") == dominant_in_type:
            s += 0.5; why.append(f"script type matches inputs ({dominant_in_type})")
        tz = _trailing_zero_sats(o["value"])
        if tz <= 3:
            s += 1.0; why.append("non-round amount (payments are usually round)")
        else:
            s -= 1.0; why.append("round amount (looks like the payment)")
        if o["value"] < min_in:
            # An output smaller than the smallest input can be change; a payment
            # this small would not have needed that input (weak signal).
            s += 0.3
        scores.append((s, idx, why))

    scores.sort(reverse=True)
    best_s, best_i, best_why = scores[0]
    second_s = scores[1][0]
    if best_s <= 0:
        return None
    margin = best_s - second_s
    confidence = max(0.3, min(0.95, 0.5 + 0.15 * margin + (0.3 if margin >= 2 else 0)))
    return {
        "index": best_i,
        "address": outputs[best_i].get("address"),
        "value_btc": outputs[best_i]["value"] / SATS,
        "confidence": round(confidence, 2),
        "reasons": best_why,
    }


# --------------------------------------------------------------------------- #
#  #6  Transaction entropy (Boltzmann-lite linkability)
# --------------------------------------------------------------------------- #
def transaction_entropy(input_sats, output_sats, cap=14):
    """Interpretation ambiguity of a tx, from equal-sum subset 'cuts'.

    A transaction can be split into independent sub-transactions wherever a
    proper subset of inputs sums to a proper subset of outputs. If no such cut
    exists the tx is *atomic* - every input funds every output group, so the
    linkage is a fact (entropy 0). More cuts => more ways to read who paid whom
    => higher entropy (more privacy / less certainty).

    Bounded: with more than `cap` inputs or outputs the subset enumeration is
    skipped and the tx is reported as high-entropy/unknown.
    """
    n, m = len(input_sats), len(output_sats)
    if n == 0 or m == 0:
        return {"atomic": False, "cuts": 0, "entropy_bits": 0.0, "note": "no in/out"}
    if n == 1 or m == 1:
        return {"atomic": True, "cuts": 0, "entropy_bits": 0.0,
                "note": "single input or output - fully linked (deterministic)"}
    if n > cap or m > cap:
        return {"atomic": False, "cuts": None, "entropy_bits": None,
                "note": f"too large to enumerate (>{cap} in/out); treat as ambiguous"}

    # proper non-empty input subset sums
    in_sums = {}
    for r in range(1, n):
        for combo in combinations(range(n), r):
            s = sum(input_sats[i] for i in combo)
            in_sums.setdefault(s, 0)
            in_sums[s] += 1
    cuts = 0
    for r in range(1, m):
        for combo in combinations(range(m), r):
            s = sum(output_sats[i] for i in combo)
            if s in in_sums:
                cuts += in_sums[s]
    # each cut is counted with its complement too; halve for distinct splits
    distinct = cuts // 2
    entropy = round(math.log2(1 + distinct), 3)
    # Fee-unaware: a fee makes total inputs exceed total outputs, so an exact
    # input-subset == output-subset match is only a heuristic for a sub-tx
    # boundary. With many inputs AND many outputs, "no cut found" means the
    # inputs *likely* jointly fund the outputs - suggestive, not proven.
    return {
        "atomic": distinct == 0,
        "cuts": distinct,
        "entropy_bits": entropy,
        "note": ("no equal-sum sub-transaction found (fee-unaware); inputs likely "
                 "jointly fund the outputs, but not proven" if distinct == 0
                 else f"{distinct} equal-sum split(s); linkage is ambiguous"),
    }


# --------------------------------------------------------------------------- #
#  #8  Peel-chain / CoinJoin shape flags
# --------------------------------------------------------------------------- #
def classify_shape(input_sats, output_sats):
    """Flag laundering-relevant transaction shapes. Returns a list of flags."""
    flags = []
    n, m = len(input_sats), len(output_sats)
    if n == 0:
        return flags
    vals = list(output_sats)
    # CoinJoin: a denomination value repeated across many outputs, with enough
    # inputs to be a joint spend.
    for v in set(vals):
        c = vals.count(v)
        if c >= 3 and n >= c:
            flags.append({
                "flag": "coinjoin",
                "detail": f"{c} equal outputs of {v / SATS:.8f} BTC across {n} inputs",
                "confidence": 0.6 + min(0.3, 0.03 * c),
            })
            break
    # Peel chain: (typically) one small payment plus one large 'peel' remainder
    # that carries most of the value onward.
    if m == 2 and n >= 1:
        big, small = max(vals), min(vals)
        if small > 0 and big >= small * 4 and big / max(1, sum(vals)) >= 0.7:
            flags.append({
                "flag": "peel_chain_step",
                "detail": f"one large remainder ({big / SATS:.8f} BTC) + one small "
                          f"payment ({small / SATS:.8f} BTC)",
                "confidence": 0.5,
            })
    # Consolidation / sweep: many inputs into one output.
    if n >= 10 and m == 1:
        flags.append({
            "flag": "consolidation",
            "detail": f"{n} inputs swept into a single output",
            "confidence": 0.7,
        })
    # Fan-out: one/few inputs to many outputs (distribution/payout batch).
    if m >= 10 and n <= 2:
        flags.append({
            "flag": "fan_out",
            "detail": f"{m} outputs from {n} input(s) (batch payout / distribution)",
            "confidence": 0.6,
        })
    return flags


# --------------------------------------------------------------------------- #
#  DB wrapper: fetch one tx and run all of the above
# --------------------------------------------------------------------------- #
def _fetch_tx(cur, txid):
    cur.execute("SELECT received_time, block_hash FROM blockchain.transactions WHERE txid = %s;",
                (txid,))
    row = cur.fetchone()
    if not row:
        return None
    tx_time, block_hash = row

    cur.execute("""
        SELECT address, amount, address_type::text, idx
          FROM blockchain.transaction_io
         WHERE txid = %s AND io_type = 'out'
         ORDER BY idx;
    """, (txid,))
    outputs = [{"address": a, "value": int(v or 0), "script_type": t, "idx": i}
               for a, v, t, i in cur.fetchall()]

    cur.execute("""
        SELECT address, amount, address_type::text, idx
          FROM blockchain.transaction_io
         WHERE txid = %s AND io_type = 'in'
         ORDER BY idx;
    """, (txid,))
    inputs = [{"address": a, "value": int(v or 0), "script_type": t, "idx": i}
              for a, v, t, i in cur.fetchall()]

    # spent flags for outputs
    cur.execute("""
        SELECT prev_vout FROM blockchain.spends WHERE prev_txid = %s;
    """, (txid,))
    spent_vouts = {r[0] for r in cur.fetchall()}
    for o in outputs:
        o["spent"] = o["idx"] in spent_vouts

    # coin age per input: creation time of the consumed prevout
    if tx_time is not None:
        # spending_vin is this tx's input index, so each input aligns to the
        # exact prevout it spends - its value and age stay correctly paired.
        cur.execute("""
            SELECT s.spending_vin, pt.received_time
              FROM blockchain.spends s
              JOIN blockchain.transactions pt ON pt.txid = s.prev_txid
             WHERE s.spending_txid = %s;
        """, (txid,))
        age_by_vin = {}
        for vin, rt in cur.fetchall():
            if rt is not None:
                age_by_vin[vin] = (tx_time - rt).total_seconds() / 86400.0
        for inp in inputs:
            inp["age_days"] = age_by_vin.get(inp["idx"], 0.0)

    # freshness of output addresses (unseen as an output before this tx).
    # Needs a reference time; without one, "< NULL" is always false and would
    # mark every address fresh, so leave freshness unset instead.
    for o in outputs:
        if not o.get("address") or tx_time is None:
            o["is_fresh"] = False
            continue
        cur.execute("""
            SELECT EXISTS(
                SELECT 1 FROM blockchain.transaction_io io
                  JOIN blockchain.transactions t ON t.txid = io.txid
                 WHERE io.address = %s AND io.io_type = 'out'
                   AND t.received_time < %s LIMIT 1);
        """, (o["address"], tx_time))
        o["is_fresh"] = not cur.fetchone()[0]

    return {"txid": txid, "time": tx_time, "block_hash": block_hash,
            "inputs": inputs, "outputs": outputs}


def analyze_tx(cur, txid):
    tx = _fetch_tx(cur, txid)
    if tx is None:
        return None
    in_sats = [i["value"] for i in tx["inputs"]]
    out_sats = [o["value"] for o in tx["outputs"]]
    is_coinbase = len(tx["inputs"]) == 0
    return {
        "txid": txid,
        "time": str(tx["time"]) if tx["time"] else None,
        "coinbase": is_coinbase,
        "n_inputs": len(in_sats),
        "n_outputs": len(out_sats),
        "total_in_btc": sum(in_sats) / SATS,
        "total_out_btc": sum(out_sats) / SATS,
        "coin_age": coin_days_destroyed(tx["inputs"]) if not is_coinbase else None,
        "change": detect_change_output(tx["inputs"], tx["outputs"]),
        "entropy": transaction_entropy(in_sats, out_sats),
        "shape_flags": classify_shape(in_sats, out_sats),
    }


def cmd_analyze_tx(args, cfg):
    with _connect(cfg) as conn:
        cur = conn.cursor()
        cur.execute(f"SET statement_timeout = '{int(args.timeout)}s';")
        try:
            res = analyze_tx(cur, args.txid)
        except psycopg.errors.QueryCanceled:
            print("analysis timed out (busy address in a freshness check); "
                  "raise --timeout and retry.", file=sys.stderr)
            sys.exit(1)
    if res is None:
        print(f"transaction {args.txid} not found in the database.", file=sys.stderr)
        sys.exit(1)
    if args.json:
        import json
        print(json.dumps(res, indent=2))
        return
    _print_human(res)


def _print_human(r):
    print(f"transaction {r['txid']}")
    print(f"  time        : {r['time']}")
    print(f"  in / out    : {r['n_inputs']} inputs ({r['total_in_btc']:.8f} BTC) -> "
          f"{r['n_outputs']} outputs ({r['total_out_btc']:.8f} BTC)")
    if r["coinbase"]:
        print("  coinbase    : yes (newly minted coins, no prior owner)")
    ca = r.get("coin_age")
    if ca:
        print(f"  coin age    : {ca['coin_days_destroyed']:,} coin-days destroyed; "
              f"oldest input {ca['oldest_input_years']} yr"
              + ("  [DORMANT COINS MOVED]" if ca["dormant"] else ""))
    ch = r.get("change")
    if ch:
        print(f"  change      : output #{ch['index']} {ch['address']} "
              f"({ch['value_btc']:.8f} BTC), confidence {ch['confidence']}")
        for why in ch["reasons"]:
            print(f"                - {why}")
    else:
        print("  change      : undetermined (single output, coinbase, or mixing shape)")
    en = r["entropy"]
    print(f"  entropy     : {en['note']}"
          + (f" ({en['entropy_bits']} bits)" if en.get("entropy_bits") is not None else ""))
    if r["shape_flags"]:
        print("  shape flags :")
        for f in r["shape_flags"]:
            print(f"                - {f['flag']}: {f['detail']} (conf {f['confidence']:.2f})")
    else:
        print("  shape flags : none")
