"""`crawlbtc correlate` - probabilistic bridging across a value break.

When coins pass through a mixer, exchange, or chain-hop, the on-chain edge is
broken: value goes in at one address and comes out later at an unrelated one.
This command proposes candidate re-links by matching *amount* and *timing*: given
value V leaving a break point at time T, it finds outputs of similar value that
appear within a following time window, ranked by how closely amount and time
match. These are leads, not proof - the score says how suggestive, never how
certain.

The search is bounded by the transactions time index (a required window), so it
stays safe on the full-chain database; the per-query statement_timeout caps it
further.
"""

import datetime
import sys

import psycopg

SATS = 100_000_000


def _connect(cfg):
    return psycopg.connect(cfg.db_conninfo, autocommit=True)


def _seed_from_txid(cur, txid, vout):
    cur.execute("""
        SELECT i.amount, t.received_time
          FROM blockchain.transaction_io i
          JOIN blockchain.transactions t ON t.txid = i.txid
         WHERE i.txid = %s AND i.io_type = 'out' AND i.idx = %s;
    """, (txid, vout))
    row = cur.fetchone()
    if not row:
        return None
    return int(row[0] or 0), row[1]


_SEARCH_SQL = """
WITH win AS (
    SELECT txid, received_time
      FROM blockchain.transactions
     WHERE received_time > %(t0)s AND received_time <= %(t1)s
)
SELECT i.txid, i.address, i.amount, w.received_time
  FROM win w
  JOIN blockchain.transaction_io i ON i.txid = w.txid AND i.io_type = 'out'
 WHERE i.amount BETWEEN %(lo)s AND %(hi)s
   AND i.address IS NOT NULL
 ORDER BY abs(i.amount - %(v)s), w.received_time
 LIMIT %(limit)s;
"""


def correlate(cur, value_sats, t0, window_hours, tolerance_pct, limit):
    lo = int(value_sats * (1 - tolerance_pct / 100.0))
    hi = int(value_sats * (1 + tolerance_pct / 100.0))
    t1 = t0 + datetime.timedelta(hours=window_hours)
    cur.execute(_SEARCH_SQL, {"t0": t0, "t1": t1, "lo": lo, "hi": hi,
                              "v": value_sats, "limit": limit})
    out = []
    span = max(1.0, window_hours * 3600.0)
    for txid, address, amount, rt in cur.fetchall():
        amount = int(amount or 0)
        amt_err = abs(amount - value_sats) / value_sats if value_sats else 1.0
        gap_s = (rt - t0).total_seconds()
        # score: closeness in amount (weighted) and recency within the window
        amt_score = max(0.0, 1.0 - amt_err / (tolerance_pct / 100.0 + 1e-9))
        time_score = max(0.0, 1.0 - gap_s / span)
        score = round(0.7 * amt_score + 0.3 * time_score, 3)
        out.append({
            "txid": txid, "address": address,
            "value_btc": amount / SATS,
            "amount_diff_pct": round(amt_err * 100, 3),
            "time_gap_hours": round(gap_s / 3600.0, 2),
            "score": score,
        })
    out.sort(key=lambda x: -x["score"])
    return out


def cmd_correlate(args, cfg):
    if not ((args.txid and args.vout is not None) or (args.amount and args.after)):
        print("give either --txid X --vout N (seed from a specific output), or "
              "--amount BTC --after YYYY-MM-DD[THH:MM:SS]", file=sys.stderr)
        sys.exit(2)

    with _connect(cfg) as conn:
        cur = conn.cursor()
        cur.execute(f"SET statement_timeout = '{int(args.timeout)}s';")

        if args.txid:
            seed = _seed_from_txid(cur, args.txid, args.vout)
            if seed is None:
                print(f"output {args.txid}:{args.vout} not found.", file=sys.stderr)
                sys.exit(1)
            value_sats, t0 = seed
        else:
            value_sats = int(round(float(args.amount) * SATS))
            t0 = datetime.datetime.fromisoformat(args.after)

        print(f"seed: {value_sats / SATS:.8f} BTC at {t0}; searching +{args.window_hours}h, "
              f"±{args.tolerance_pct}% ...", file=sys.stderr)
        try:
            res = correlate(cur, value_sats, t0, args.window_hours,
                            args.tolerance_pct, args.limit)
        except psycopg.errors.QueryCanceled:
            print(f"search timed out ({args.timeout}s) - narrow --window-hours or raise "
                  f"--timeout.", file=sys.stderr)
            sys.exit(1)

    if args.json:
        import json
        print(json.dumps(res, indent=2))
        return
    if not res:
        print("no candidate matches in the window.")
        return
    print(f"{'score':>6}  {'value_btc':>14}  {'Δamt%':>7}  {'gap_h':>8}  address / txid")
    for r in res:
        print(f"{r['score']:>6.3f}  {r['value_btc']:>14.8f}  {r['amount_diff_pct']:>7.3f}  "
              f"{r['time_gap_hours']:>8.2f}  {r['address']}  {r['txid']}")
    print("\ncandidates only - amount+timing coincidence, not proof of a link.", file=sys.stderr)
