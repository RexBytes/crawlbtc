"""crawlbtc command-line interface.

Same contact points as the legacy scripts: identical PostgreSQL tables,
identical .env variables, identical JSON progress log stream.
"""

import argparse
import sys
from importlib import resources

import psycopg

from .core.config import load_config
from .core.logging import get_logger

log = get_logger("cli")

_PHASE_COLUMNS = {"vout": "vout_status", "vin": "vin_status", "address": "address_status"}


def _read_sql(name: str) -> str:
    return (resources.files("crawlbtc") / "sql" / name).read_text()


def _connect(cfg):
    return psycopg.connect(cfg.db_conninfo, autocommit=True)


# --- commands ---

def cmd_init_db(args, cfg):
    sql = _read_sql("schema.sql")
    if args.show_sql:
        print(sql)
        return
    with _connect(cfg) as conn:
        conn.execute(sql)
    print("schema initialized (schema 'blockchain')")


def cmd_migrate(args, cfg):
    migration_files = sorted(
        p for p in (resources.files("crawlbtc") / "sql" / "migrations").iterdir()
        if p.name.endswith(".sql")
    )
    if args.show_sql:
        for p in migration_files:
            print(f"-- {p.name}")
            print(p.read_text())
        return
    try:
        with _connect(cfg) as conn:
            for p in migration_files:
                print(f"applying {p.name} ...")
                conn.execute(p.read_text())
    except psycopg.errors.InsufficientPrivilege as e:
        print(f"\npermission denied: {e}", file=sys.stderr)
        print("migrations alter types/indexes, which requires their OWNER "
              "(usually your admin role, not the app role).", file=sys.stderr)
        print("run once as admin, either:", file=sys.stderr)
        print("  PG_USER=pgadmin PG_PASSWORD=... crawlbtc migrate", file=sys.stderr)
        print("  crawlbtc migrate --show-sql | psql -h <host> -U pgadmin -d <db>", file=sys.stderr)
        sys.exit(1)
    print("migrations applied")


def cmd_extract(args, cfg):
    from .core.runner import run_phase
    from .phases.extract import ExtractPhase
    run_phase(ExtractPhase, cfg)


def cmd_backfill_vins(args, cfg):
    from .core.runner import run_phase
    from .phases.backfill_vins import BackfillVinsPhase
    run_phase(BackfillVinsPhase, cfg)


def cmd_scan_addresses(args, cfg):
    from .core.runner import run_phase
    from .phases.address_scans import AddressScanPhase
    run_phase(AddressScanPhase, cfg)


def cmd_run_all(args, cfg):
    from .core.runner import run_phase
    from .phases.address_scans import AddressScanPhase
    from .phases.backfill_vins import BackfillVinsPhase
    from .phases.extract import ExtractPhase
    for factory in (ExtractPhase, BackfillVinsPhase, AddressScanPhase):
        run_phase(factory, cfg)


def cmd_status(args, cfg):
    with _connect(cfg) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), MIN(height), MAX(height) FROM blockchain.block_jobs;")
        total, min_h, max_h = cur.fetchone()
        print(f"block_jobs: {total:,} rows (heights {min_h}..{max_h})")
        for col in _PHASE_COLUMNS.values():
            cur.execute(
                f"SELECT {col}::text, COUNT(*) FROM blockchain.block_jobs GROUP BY 1 ORDER BY 2 DESC;"
            )
            parts = ", ".join(f"{s}={c:,}" for s, c in cur.fetchall())
            print(f"{col:<15} {parts}")


def cmd_diagnose(args, cfg):
    from .diagnose import run_diagnose
    print(run_diagnose(cfg))


def cmd_config(args, cfg):
    from .configtool import cmd_config as _cmd_config
    _cmd_config(args, cfg)


def cmd_backup(args, cfg):
    from .backup import cmd_backup as _cmd_backup
    _cmd_backup(args, cfg)


def cmd_trace(args, cfg):
    from .trace import cmd_trace as _cmd_trace
    _cmd_trace(args, cfg)


def cmd_tags(args, cfg):
    from .tags import cmd_tags as _cmd_tags
    _cmd_tags(args, cfg)


def cmd_import_prices(args, cfg):
    from .prices import cmd_import_prices as _cmd
    _cmd(args, cfg)


_BLOCK_FEATURES = {"vin", "vout", "both", "none", "op_return_only", "coinbase_only"}


def cmd_requeue(args, cfg):
    if args.phase == "all":
        columns = list(_PHASE_COLUMNS.values())
    else:
        columns = [_PHASE_COLUMNS[args.phase]]

    features = [f.strip() for f in args.features.split(",")] if args.features else []
    bad = set(features) - _BLOCK_FEATURES
    if bad:
        print(f"unknown features: {', '.join(sorted(bad))} "
              f"(valid: {', '.join(sorted(_BLOCK_FEATURES))})", file=sys.stderr)
        sys.exit(2)

    if not (args.skipped or args.failed or features
            or args.from_height is not None or args.to_height is not None):
        print("refusing to requeue everything: give --skipped, --failed, --features, "
              "and/or --from/--to to select blocks", file=sys.stderr)
        sys.exit(2)

    with _connect(cfg) as conn:
        cur = conn.cursor()
        for col in columns:
            conds, params = [], []
            if args.from_height is not None:
                conds.append("height >= %s")
                params.append(args.from_height)
            if args.to_height is not None:
                conds.append("height <= %s")
                params.append(args.to_height)
            selector_conds = []
            if args.skipped:
                selector_conds.append(f"{col} = 'skipped'")
            if args.failed:
                selector_conds.append(f"{col} = 'failed'")
            if features:
                selector_conds.append("features = ANY(%s::blockchain.block_feature[])")
                params.append(features)
            if selector_conds:
                conds.append("(" + " OR ".join(selector_conds) + ")")
            else:
                # Height-range-only requeue: reset any terminal state.
                conds.append(f"{col} IN ('done', 'skipped', 'failed')")
            cur.execute(
                f"""
                UPDATE blockchain.block_jobs
                   SET {col} = 'pending', updated_at = now()
                 WHERE {' AND '.join(conds)};
                """,
                params,
            )
            print(f"{col}: {cur.rowcount:,} blocks requeued")
    print("now run the matching phase (e.g. `crawlbtc extract`).")
    print("if watched-address balances may be affected, finish with "
          "`crawlbtc recompute-balances`.")


_RECOMPUTE_SQL = """
WITH outs AS (
  SELECT i.address, i.amount, i.txid,
         s.prev_txid IS NOT NULL AS spent,
         s.spending_txid
    FROM blockchain.transaction_io i
    JOIN blockchain.watch_addresses wa ON wa.address = i.address
    LEFT JOIN blockchain.spends s
      ON s.prev_txid = i.txid AND s.prev_vout = i.idx
   WHERE i.io_type = 'out'::blockchain.tx_io_type
),
touched AS (
  SELECT address, txid AS tx FROM outs
  UNION
  SELECT address, spending_txid FROM outs WHERE spending_txid IS NOT NULL
),
tx_times AS (
  SELECT tc.address,
         MIN(t.received_time) AS first_ts,
         MAX(t.received_time) AS last_ts,
         COUNT(DISTINCT tc.tx) AS tx_count
    FROM touched tc
    LEFT JOIN blockchain.transactions t ON t.txid = tc.tx
   GROUP BY tc.address
),
agg AS (
  SELECT o.address,
         COALESCE(SUM(o.amount) FILTER (WHERE NOT o.spent), 0) AS balance_sats,
         COUNT(*) FILTER (WHERE NOT o.spent) AS utxo_count
    FROM outs o
   GROUP BY o.address
)
UPDATE blockchain.watch_addresses wa
   SET balance_sats = a.balance_sats,
       utxo_count   = a.utxo_count,
       tx_count     = COALESCE(tt.tx_count, 0),
       first_seen   = COALESCE(tt.first_ts AT TIME ZONE 'UTC', wa.first_seen),
       last_seen    = COALESCE(tt.last_ts AT TIME ZONE 'UTC', wa.last_seen),
       updated_at   = now()
  FROM agg a
  LEFT JOIN tx_times tt ON tt.address = a.address
 WHERE wa.address = a.address;
"""


_BUILD_BALANCES_SQL = """
INSERT INTO blockchain.address_balances
    (address, address_type, balance_sats, utxo_count,
     total_received_sats, total_spent_sats, updated_at)
SELECT i.address,
       MAX(i.address_type)                                        AS address_type,
       COALESCE(SUM(i.amount) FILTER (WHERE s.prev_txid IS NULL), 0) AS balance_sats,
       COUNT(*) FILTER (WHERE s.prev_txid IS NULL)                AS utxo_count,
       COALESCE(SUM(i.amount), 0)                                 AS total_received_sats,
       COALESCE(SUM(i.amount) FILTER (WHERE s.prev_txid IS NOT NULL), 0) AS total_spent_sats,
       now()
  FROM blockchain.transaction_io i
  LEFT JOIN blockchain.spends s
    ON s.prev_txid = i.txid AND s.prev_vout = i.idx
 WHERE i.io_type = 'out'::blockchain.tx_io_type
   AND i.address IS NOT NULL
 GROUP BY i.address;
"""


def cmd_build_balances(args, cfg):
    """Materialize the balance of EVERY address into blockchain.address_balances.

    Pure SQL over transaction_io + spends; no node needed. Full rebuild each
    run (truncate + insert) so it is always exact and idempotent. On a
    full-chain database this is a big batch job - expect hours, and make
    sure there is temp disk headroom for the aggregation spill.
    """
    try:
        _run_build_balances(cfg, args)
    except psycopg.errors.InsufficientPrivilege as e:
        print(f"\npermission denied: {e}", file=sys.stderr)
        print("build-balances creates and truncates blockchain.address_balances, which "
              "requires the schema OWNER (usually pgadmin), not the app role.", file=sys.stderr)
        print("run it as the owner, e.g.:", file=sys.stderr)
        print("  PG_USER=pgadmin PG_PASSWORD=... crawlbtc build-balances --work-mem 8GB",
              file=sys.stderr)
        sys.exit(1)


def _run_build_balances(cfg, args):
    with _connect(cfg) as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS blockchain.address_balances (
                address text PRIMARY KEY,
                address_type blockchain.address_type,
                balance_sats bigint NOT NULL,
                utxo_count integer NOT NULL,
                total_received_sats bigint NOT NULL,
                total_spent_sats bigint NOT NULL,
                updated_at timestamp with time zone DEFAULT now() NOT NULL
            );
        """)
        cur.execute("SET statement_timeout = 0;")
        cur.execute("SELECT set_config('work_mem', %s, false);", (args.work_mem,))
        print("rebuilding blockchain.address_balances (full pass over transaction_io)...")
        cur.execute("BEGIN;")
        cur.execute("TRUNCATE blockchain.address_balances;")
        cur.execute(_BUILD_BALANCES_SQL)
        count = cur.rowcount
        cur.execute("COMMIT;")
        print(f"built balances for {count:,} addresses")
        cur.execute("""
            SELECT COUNT(*) FILTER (WHERE balance_sats > 0),
                   COALESCE(SUM(balance_sats), 0)
            FROM blockchain.address_balances;
        """)
        funded, total = cur.fetchone()
        print(f"addresses with balance > 0: {funded:,}; total tracked: {total:,} sats "
              f"({total / 100_000_000:,.2f} BTC)")


def cmd_recompute_balances(args, cfg):
    """Exact rebuild of watch_addresses balances from transaction_io + spends.

    Unlike the per-block delta scan, this is idempotent - run it any time,
    especially after requeueing old blocks (e.g. for P2PK coverage).
    """
    with _connect(cfg) as conn:
        cur = conn.cursor()
        cur.execute("SET statement_timeout = 0;")
        cur.execute(_RECOMPUTE_SQL)
        print(f"recomputed balances for {cur.rowcount:,} watch addresses")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawlbtc",
        description="Bitcoin Core -> PostgreSQL blockchain crawler and data processor",
    )
    parser.add_argument("-e", "--env-file", default=None,
                        help="path to env file (default: CRAWLBTC_ENV_FILE or nearest .env)")
    parser.add_argument("-P", "--processes", type=int, default=None,
                        help="OS processes per phase (default: auto, env PROCESSES)")
    parser.add_argument("-w", "--workers", type=int, default=None,
                        help="total async workers (default: auto, env NUM_WORKERS)")
    parser.add_argument("-b", "--batch-size", type=int, default=None,
                        help="job claim batch size (default: 1, env JOB_BATCH_SIZE)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="create the blockchain schema and tables")
    p.add_argument("--show-sql", action="store_true", help="print the SQL instead of executing")
    p.set_defaults(func=cmd_init_db, needs_probe=False)

    p = sub.add_parser("migrate", help="apply additive migrations (p2pk enum, index cleanup)")
    p.add_argument("--show-sql", action="store_true", help="print the SQL instead of executing")
    p.set_defaults(func=cmd_migrate, needs_probe=False)

    p = sub.add_parser("extract", help="extract blocks: transactions, vouts, vins, spends (single pass)")
    p.set_defaults(func=cmd_extract, needs_probe=True)

    p = sub.add_parser("backfill-vins", help="repair pass: fill vins for blocks missing them")
    p.set_defaults(func=cmd_backfill_vins, needs_probe=True)

    p = sub.add_parser("scan-addresses", help="apply per-block balance deltas to watch_addresses")
    p.set_defaults(func=cmd_scan_addresses, needs_probe=True)

    p = sub.add_parser("run-all", help="run extract, backfill-vins, scan-addresses in sequence")
    p.set_defaults(func=cmd_run_all, needs_probe=True)

    p = sub.add_parser("status", help="print per-phase job progress")
    p.set_defaults(func=cmd_status, needs_probe=False)

    p = sub.add_parser("diagnose", help="database/node health report (paste back for analysis)")
    p.set_defaults(func=cmd_diagnose, needs_probe=False)

    p = sub.add_parser("config",
                       help="show / back up / restore the configs crawlbtc depends on")
    p.add_argument("action", nargs="?", choices=["show", "backup", "restore"], default="show",
                   help="show (default), backup, or restore")
    p.add_argument("path", nargs="?", default=None,
                   help="backup: output dir (default: ./crawlbtc-config-backup-<ts>); "
                        "restore: backup dir to read")
    p.add_argument("--bitcoin-conf", default=None, help="explicit path to bitcoin.conf")
    p.add_argument("--force", action="store_true", help="restore: actually write files (default dry-run)")
    p.set_defaults(func=cmd_config, needs_probe=False)

    p = sub.add_parser("requeue", help="reset job statuses so blocks get reprocessed")
    p.add_argument("--phase", choices=["vout", "vin", "address", "all"], default="vout")
    p.add_argument("--from", dest="from_height", type=int, default=None, metavar="HEIGHT")
    p.add_argument("--to", dest="to_height", type=int, default=None, metavar="HEIGHT")
    p.add_argument("--skipped", action="store_true", help="requeue blocks marked skipped")
    p.add_argument("--failed", action="store_true", help="requeue blocks marked failed")
    p.add_argument("--features", default=None, metavar="F1,F2",
                   help="requeue blocks whose features match (e.g. op_return_only,none,vin)")
    p.set_defaults(func=cmd_requeue, needs_probe=False)

    p = sub.add_parser("recompute-balances",
                       help="exact rebuild of watch_addresses balances from io/spends data")
    p.set_defaults(func=cmd_recompute_balances, needs_probe=False)

    p = sub.add_parser("trace",
                       help="follow value outward from an address -> interactive HTML + Excel")
    p.add_argument("address", help="starting bitcoin address")
    p.add_argument("--direction", choices=["out", "in", "both"], default="out",
                   help="follow value outgoing (default), incoming (provenance), or both")
    p.add_argument("--depth", type=int, default=3, help="hops to expand (default 3)")
    p.add_argument("--fanout", type=int, default=10,
                   help="max counterparties kept per address, by value (default 10)")
    p.add_argument("--max-nodes", type=int, default=750,
                   help="hard cap on addresses in the graph (default 750)")
    p.add_argument("--no-cluster", action="store_true",
                   help="skip common-input related-wallet detection")
    p.add_argument("--timeout", type=int, default=60,
                   help="per-query timeout in seconds; slow nodes skip, not abort (default 60)")
    p.add_argument("--max-utxos", type=int, default=2000,
                   help="follow only the N largest outputs per address (bounds busy "
                        "high-UTXO addresses; default 2000)")
    p.add_argument("--fiat", default=None, metavar="CUR",
                   help="value each flow in this currency at the tx date (needs import-prices)")
    p.add_argument("--report-title", default=None,
                   help="title shown on the report (default: neutral, no tool branding)")
    p.add_argument("--out", default=None, help="output directory (default: current dir)")
    p.set_defaults(func=cmd_trace, needs_probe=False)

    p = sub.add_parser("tags", help="manage the known-entity reference table (OFAC, exchanges, custom)")
    p.add_argument("action", choices=["import-ofac", "load-builtin", "import", "add",
                                      "remove", "list", "count"])
    p.add_argument("rest", nargs="*", help="positional args for add/remove")
    p.add_argument("--file", default=None, help="input file (import / import-ofac)")
    p.add_argument("--url", default=None, help="OFAC SDN url override (import-ofac)")
    p.add_argument("--source", default=None, help="source label for import/add")
    p.add_argument("--category", default=None, help="category for import")
    p.add_argument("--confidence", type=float, default=0.8, help="confidence for import/add")
    p.add_argument("--all", action="store_true", help="import-ofac: all chains, not just Bitcoin")
    p.add_argument("--search", default=None, help="list: filter by address/name substring")
    p.add_argument("--limit", type=int, default=100, help="list: max rows")
    p.set_defaults(func=cmd_tags, needs_probe=False)

    p = sub.add_parser("import-prices", help="load a historical BTC price CSV for fiat valuation")
    p.add_argument("--csv", required=True, help="CSV file with date,price columns")
    p.add_argument("--currency", default="USD", help="currency label for these prices (default USD)")
    p.add_argument("--source", default=None, help="provenance label (default: filename)")
    p.set_defaults(func=cmd_import_prices, needs_probe=False)

    p = sub.add_parser("backup",
                       help="consistent evidence-grade dump of the blockchain schema + manifest")
    p.add_argument("action", nargs="?", default="create",
                   help="create (default) or verify; or just give a path to create there")
    p.add_argument("path", nargs="?", default=None,
                   help="create: output dir (a blockchain_<timestamp> subdir is made); "
                        "verify: the dump dir to check")
    p.add_argument("--jobs", type=int, default=4, help="parallel pg_dump/pg_restore jobs (default 4)")
    p.add_argument("--compress", type=int, default=6, help="dump compression level 0-9 (default 6)")
    p.add_argument("--include-derived", action="store_true",
                   help="also dump address_balances (default: excluded, rebuildable)")
    p.add_argument("--no-checksum", action="store_true",
                   help="skip SHA-256 of dump files (faster, but not verifiable)")
    p.add_argument("--pg-user", default=None, help="override PG role for the dump (e.g. pgadmin)")
    p.set_defaults(func=cmd_backup, needs_probe=False)

    p = sub.add_parser("build-balances",
                       help="materialize balances for EVERY address into blockchain.address_balances")
    p.add_argument("--work-mem", default="1GB", metavar="SIZE",
                   help="session work_mem for the aggregation (default 1GB)")
    p.set_defaults(func=cmd_build_balances, needs_probe=False)

    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    cfg = load_config(
        processes=args.processes,
        workers=args.workers,
        batch_size=args.batch_size,
        probe_db=getattr(args, "needs_probe", False),
        env_file=args.env_file,
    )
    try:
        args.func(args, cfg)
    except BrokenPipeError:
        # stdout piped into head/less that exited early
        sys.exit(0)


if __name__ == "__main__":
    main()
