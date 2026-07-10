"""Incremental maintenance of blockchain.address_balances.

`build-balances` does the exact full rebuild (all addresses). This module adds
`update-balances`, which recomputes only the addresses whose UTXO set changed
since the last run - seconds/minutes per new batch of blocks instead of the
multi-hour full pass. The two share the same per-address balance definition, so
an incremental update lands on the identical row a full rebuild would.

Correctness rests on the watermark being the *contiguous fully-processed
frontier*: the highest height H such that every block up to H has finished the
vin phase (so its spends are known). Balances are exact up to H. On each run we
touch every address that either received an output above H, or had an output
spent above H, and recompute those addresses from scratch.
"""

import sys

import psycopg

WATERMARK_NAME = "address_balances"


def _connect(cfg):
    return psycopg.connect(cfg.db_conninfo, autocommit=True)


def fully_processed_frontier(cur):
    """Highest height H with every block <= H past the vin phase.

    One below the first block whose vin_status is not done/skipped; if there is
    no such gap, the max height present. Spends for blocks <= H are complete, so
    address balances can be trusted up to H.
    """
    cur.execute("""
        SELECT COALESCE(
                 (SELECT MIN(height) - 1 FROM blockchain.block_jobs
                   WHERE vin_status NOT IN ('done', 'skipped')),
                 (SELECT MAX(height) FROM blockchain.block_jobs),
                 -1);
    """)
    return cur.fetchone()[0]


def read_watermark(cur, name=WATERMARK_NAME):
    cur.execute("SELECT height FROM blockchain.balance_watermark WHERE name = %s;", (name,))
    row = cur.fetchone()
    return row[0] if row else None


def write_watermark(cur, height, name=WATERMARK_NAME):
    cur.execute("""
        INSERT INTO blockchain.balance_watermark (name, height, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (name) DO UPDATE
          SET height = EXCLUDED.height, updated_at = now();
    """, (name, height))


# Recompute exactly the addresses touched in (wm, frontier]. `wm` is the low
# bound (already accounted for) and `frontier` the new high-water height.
_UPDATE_SQL = """
WITH new_created AS (
  SELECT DISTINCT i.address
    FROM blockchain.blocks b
    JOIN blockchain.transactions t ON t.block_hash = b.block_hash
    JOIN blockchain.transaction_io i ON i.txid = t.txid AND i.io_type = 'out'
   WHERE b.height > %(wm)s AND b.height <= %(hi)s AND i.address IS NOT NULL
),
new_spent AS (
  SELECT DISTINCT i.address
    FROM blockchain.spends s
    JOIN blockchain.transaction_io i
      ON i.txid = s.prev_txid AND i.idx = s.prev_vout AND i.io_type = 'out'
   WHERE s.spent_height > %(wm)s AND s.spent_height <= %(hi)s AND i.address IS NOT NULL
),
touched AS (
  SELECT address FROM new_created
  UNION
  SELECT address FROM new_spent
),
recomputed AS (
  SELECT i.address,
         MAX(i.address_type)                                          AS address_type,
         COALESCE(SUM(i.amount) FILTER (WHERE s.prev_txid IS NULL), 0) AS balance_sats,
         COUNT(*) FILTER (WHERE s.prev_txid IS NULL)                  AS utxo_count,
         COALESCE(SUM(i.amount), 0)                                   AS total_received_sats,
         COALESCE(SUM(i.amount) FILTER (WHERE s.prev_txid IS NOT NULL), 0) AS total_spent_sats
    FROM touched tc
    JOIN blockchain.transaction_io i ON i.address = tc.address AND i.io_type = 'out'
    LEFT JOIN blockchain.spends s ON s.prev_txid = i.txid AND s.prev_vout = i.idx
   GROUP BY i.address
)
INSERT INTO blockchain.address_balances
    (address, address_type, balance_sats, utxo_count,
     total_received_sats, total_spent_sats, updated_at)
SELECT address, address_type, balance_sats, utxo_count,
       total_received_sats, total_spent_sats, now()
  FROM recomputed
ON CONFLICT (address) DO UPDATE
  SET address_type        = EXCLUDED.address_type,
      balance_sats        = EXCLUDED.balance_sats,
      utxo_count          = EXCLUDED.utxo_count,
      total_received_sats = EXCLUDED.total_received_sats,
      total_spent_sats    = EXCLUDED.total_spent_sats,
      updated_at          = now();
"""


def cmd_update_balances(args, cfg):
    with _connect(cfg) as conn:
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('blockchain.address_balances');")
        if cur.fetchone()[0] is None:
            print("blockchain.address_balances does not exist - run `crawlbtc build-balances` "
                  "once first (as the schema owner).", file=sys.stderr)
            sys.exit(1)

        wm = read_watermark(cur)
        if wm is None:
            print("no balance watermark found - run `crawlbtc build-balances` once to seed "
                  "the full table and watermark, then `update-balances` keeps it current.",
                  file=sys.stderr)
            sys.exit(1)

        frontier = fully_processed_frontier(cur)
        if frontier <= wm:
            print(f"already current: watermark at height {wm:,}, "
                  f"fully-processed frontier at {frontier:,} - nothing to do.")
            return

        print(f"updating address_balances for blocks {wm + 1:,}..{frontier:,} ...")
        try:
            cur.execute("SET statement_timeout = 0;")
            cur.execute("SELECT set_config('work_mem', %s, false);", (args.work_mem,))
            cur.execute(_UPDATE_SQL, {"wm": wm, "hi": frontier})
            touched = cur.rowcount
            write_watermark(cur, frontier)
        except psycopg.errors.InsufficientPrivilege as e:
            print(f"\npermission denied: {e}", file=sys.stderr)
            print("update-balances writes blockchain.address_balances, which requires the "
                  "schema OWNER (usually pgadmin), not the app role.", file=sys.stderr)
            print("  PG_USER=pgadmin PG_PASSWORD=... crawlbtc update-balances", file=sys.stderr)
            sys.exit(1)
        print(f"recomputed {touched:,} address(es); watermark advanced to height {frontier:,}")
