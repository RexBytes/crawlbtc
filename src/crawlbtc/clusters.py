"""`crawlbtc build-clusters` - global common-input-ownership wallet clusters.

Two or more addresses spent together as inputs of the same transaction are
almost certainly controlled by one wallet (you need every input's key to sign).
Taking the transitive closure of that relation over the whole chain groups
addresses into wallets. We compute it with iterative label propagation, which
converges to the connected-component minimum-address label:

  init   : every input address is its own cluster (label = itself)
  repeat : each address takes the smallest label among all addresses it has
           ever shared a transaction's inputs with; stop when nothing changes

This is a heavy one-time pass (each iteration scans the input rows), so it is
built to be observed and resumed: state lives entirely in the
blockchain.address_clusters table, progress prints per iteration, --max-iterations
bounds the run, and re-running continues converging (use --reset to start over).

Only direct co-input grouping is used; no CoinJoin/PayJoin unmixing is attempted
(that would over-merge). Addresses never spent are singletons - absent from the
table, and treated as their own cluster on lookup.
"""

import sys

import psycopg


def _connect(cfg):
    return psycopg.connect(cfg.db_conninfo, autocommit=True)


def _require_table(cur):
    cur.execute("SELECT to_regclass('blockchain.address_clusters');")
    if cur.fetchone()[0] is None:
        print("blockchain.address_clusters does not exist - run `crawlbtc migrate` first "
              "(as the schema owner).", file=sys.stderr)
        sys.exit(1)


# Seed: every address ever used as a transaction input starts in its own cluster.
_INIT_SQL = """
INSERT INTO blockchain.address_clusters (address, cluster_id)
SELECT DISTINCT address, address
  FROM blockchain.transaction_io
 WHERE io_type = 'in' AND address IS NOT NULL
ON CONFLICT (address) DO NOTHING;
"""

# One propagation pass: relabel every co-input address to the smallest current
# label seen across the transactions it participates in. Only rows that would
# strictly decrease are written, so rowcount == 0 means converged.
_STEP_SQL = """
WITH tx_min AS (
    SELECT i.txid, MIN(ac.cluster_id) AS m
      FROM blockchain.transaction_io i
      JOIN blockchain.address_clusters ac ON ac.address = i.address
     WHERE i.io_type = 'in' AND i.address IS NOT NULL
     GROUP BY i.txid
),
newlabel AS (
    SELECT i.address, MIN(tm.m) AS label
      FROM blockchain.transaction_io i
      JOIN tx_min tm ON tm.txid = i.txid
     WHERE i.io_type = 'in' AND i.address IS NOT NULL
     GROUP BY i.address
)
UPDATE blockchain.address_clusters ac
   SET cluster_id = nl.label
  FROM newlabel nl
 WHERE ac.address = nl.address
   AND ac.cluster_id > nl.label;
"""


def cmd_build_clusters(args, cfg):
    if args.lookup:
        _lookup(cfg, args.lookup)
        return
    if args.stats:
        _stats(cfg)
        return
    try:
        _build(cfg, args)
    except psycopg.errors.InsufficientPrivilege as e:
        print(f"\npermission denied: {e}", file=sys.stderr)
        print("build-clusters writes blockchain.address_clusters, which requires the schema "
              "OWNER (usually pgadmin), not the app role.", file=sys.stderr)
        print("  PG_USER=pgadmin PG_PASSWORD=... crawlbtc build-clusters", file=sys.stderr)
        sys.exit(1)


def _build(cfg, args):
    with _connect(cfg) as conn:
        cur = conn.cursor()
        _require_table(cur)
        cur.execute("SET statement_timeout = 0;")
        cur.execute("SELECT set_config('work_mem', %s, false);", (args.work_mem,))

        if args.reset:
            print("resetting blockchain.address_clusters ...")
            cur.execute("TRUNCATE blockchain.address_clusters;")

        cur.execute("SELECT COUNT(*) FROM blockchain.address_clusters;")
        if cur.fetchone()[0] == 0:
            print("seeding clusters (one per input address) ...")
            cur.execute(_INIT_SQL)
            print(f"  seeded {cur.rowcount:,} addresses")

        print(f"propagating labels (max {args.max_iterations} iterations) ...")
        for it in range(1, args.max_iterations + 1):
            cur.execute(_STEP_SQL)
            changed = cur.rowcount
            print(f"  iteration {it}: {changed:,} labels updated", flush=True)
            if changed == 0:
                print("converged.")
                break
        else:
            print(f"reached max-iterations ({args.max_iterations}); clustering is partial - "
                  "re-run `build-clusters` to continue converging.")

        cur.execute("""
            SELECT COUNT(*) AS addresses, COUNT(DISTINCT cluster_id) AS clusters
              FROM blockchain.address_clusters;
        """)
        addrs, cl = cur.fetchone()
        print(f"clustered {addrs:,} addresses into {cl:,} wallets")


def _stats(cfg):
    with _connect(cfg) as conn:
        cur = conn.cursor()
        _require_table(cur)
        cur.execute("""
            SELECT COUNT(*), COUNT(DISTINCT cluster_id) FROM blockchain.address_clusters;
        """)
        addrs, cl = cur.fetchone()
        if not addrs:
            print("no clusters built yet - run `crawlbtc build-clusters`.")
            return
        print(f"addresses clustered : {addrs:,}")
        print(f"distinct wallets    : {cl:,}")
        print(f"avg addresses/wallet: {addrs / cl:.2f}")
        print("largest wallets:")
        cur.execute("""
            SELECT cluster_id, COUNT(*) n FROM blockchain.address_clusters
            GROUP BY cluster_id ORDER BY n DESC LIMIT 10;
        """)
        for cid, n in cur.fetchall():
            print(f"  {n:>12,}  {cid}")


def _lookup(cfg, address):
    with _connect(cfg) as conn:
        cur = conn.cursor()
        _require_table(cur)
        cur.execute("SELECT cluster_id FROM blockchain.address_clusters WHERE address = %s;",
                    (address,))
        row = cur.fetchone()
        if not row:
            print(f"{address}: not in any cluster (never spent, or clusters not built)")
            return
        cid = row[0]
        cur.execute("SELECT COUNT(*) FROM blockchain.address_clusters WHERE cluster_id = %s;",
                    (cid,))
        n = cur.fetchone()[0]
        print(f"{address}")
        print(f"  cluster id : {cid}")
        print(f"  wallet size: {n:,} address(es) probably one owner")
        cur.execute("""
            SELECT address FROM blockchain.address_clusters
            WHERE cluster_id = %s ORDER BY address LIMIT 20;
        """, (cid,))
        members = [r[0] for r in cur.fetchall()]
        print("  sample:")
        for m in members:
            print(f"    {m}")
        if n > len(members):
            print(f"    ... and {n - len(members):,} more")
