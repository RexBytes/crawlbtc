"""crawlbtc diagnose - inspect the database and node, report health.

Produces a plain-text report designed to be pasted back into a review:
schema/enum state, per-phase job progress, table and index sizes, duplicate
indexes, Satoshi-era (P2PK) coverage sampling, spot consistency checks, and
node capabilities (verbosity-3 support, tip lag).

Read-only: runs no writes against the database.
"""

import asyncio
import datetime
from typing import List, Optional

import aiohttp
import orjson
import psycopg

from .core.config import Config

# Well-known Satoshi-era coinbase payout addresses (P2PK outputs whose
# canonical P2PKH form is shown by every explorer).
KNOWN_EARLY_ADDRESSES = [
    ("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "genesis coinbase (block 0)"),
    ("12c6DSiU4Rq3P4ZxziKxzrL5LmMBrzjrJX", "block 1 coinbase"),
    ("1HLoD9E4SDFFPDiYfNYnkBLQ85Y51J3Zb1", "block 2 coinbase"),
]

SAMPLE_EARLY_HEIGHTS = [1, 9, 170, 1000, 10000, 50000, 100000, 150000]


def _section(lines: List[str], title: str):
    lines.append("")
    lines.append(f"== {title} ==")


async def _rpc(session, cfg: Config, method: str, params=None, timeout=30.0):
    auth = aiohttp.BasicAuth(cfg.rpc_user, cfg.rpc_password)
    async with session.post(
        cfg.rpc_url,
        data=orjson.dumps({"jsonrpc": "1.0", "id": "diagnose", "method": method, "params": params or []}),
        headers={"Content-Type": "application/json"},
        auth=auth,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        resp.raise_for_status()
        data = orjson.loads(await resp.read())
        if data.get("error"):
            raise ValueError(f"RPC error: {data['error']}")
        return data["result"]


async def _probe_node(cfg: Config, lines: List[str], db_max_height: Optional[int]) -> None:
    try:
        async with aiohttp.ClientSession() as session:
            tip = await _rpc(session, cfg, "getblockcount")
            lines.append(f"node reachable: yes ({cfg.rpc_url})")
            lines.append(f"node tip height: {tip}")
            if db_max_height is not None:
                lines.append(f"db max job height: {db_max_height} (lag: {max(0, tip - db_max_height)})")
            try:
                h1 = await _rpc(session, cfg, "getblockhash", [1])
                await _rpc(session, cfg, "getblock", [h1, 3], timeout=60.0)
                lines.append("getblock verbosity 3 (inline prevouts): supported")
            except Exception as e:
                lines.append(f"getblock verbosity 3: NOT supported ({e}) - merged extract will "
                             "fall back to verbosity 2 + backfill-vins")
    except Exception as e:
        lines.append(f"node reachable: NO ({e})")
        lines.append("(node-dependent checks skipped)")


async def _sample_early_blocks(cfg: Config, conn, lines: List[str]):
    """Fetch early block hashes from the node, check row counts in the DB."""
    try:
        async with aiohttp.ClientSession() as session:
            tip = await _rpc(session, cfg, "getblockcount")
            heights = [h for h in SAMPLE_EARLY_HEIGHTS if h <= tip]
            hashes = {}
            for h in heights:
                hashes[h] = await _rpc(session, cfg, "getblockhash", [h])
    except Exception as e:
        lines.append(f"(early-block sampling needs the node: {e})")
        return

    lines.append(f"{'height':>8}  {'txs_in_db':>9}  {'out_rows':>8}  {'in_rows':>7}  job(vout/vin)")
    with conn.cursor() as cur:
        for h, bh in hashes.items():
            cur.execute("""
                SELECT COUNT(*),
                       COALESCE(SUM((SELECT COUNT(*) FROM blockchain.transaction_io i
                                     WHERE i.txid = t.txid AND i.io_type = 'out')), 0),
                       COALESCE(SUM((SELECT COUNT(*) FROM blockchain.transaction_io i
                                     WHERE i.txid = t.txid AND i.io_type = 'in')), 0)
                FROM blockchain.transactions t WHERE t.block_hash = %s;
            """, (bh,))
            txs, outs, ins = cur.fetchone()
            cur.execute("SELECT vout_status, vin_status FROM blockchain.block_jobs WHERE height = %s;", (h,))
            row = cur.fetchone()
            job = f"{row[0]}/{row[1]}" if row else "no job row"
            lines.append(f"{h:>8}  {txs:>9}  {outs:>8}  {ins:>7}  {job}")


def run_diagnose(cfg: Config) -> str:
    lines: List[str] = []
    lines.append("crawlbtc diagnose report")
    lines.append(f"generated: {datetime.datetime.now(datetime.UTC).isoformat()}")

    try:
        conn = psycopg.connect(cfg.db_conninfo, autocommit=True, connect_timeout=15)
    except Exception as e:
        lines.append(f"DATABASE UNREACHABLE: {e}")
        return "\n".join(lines)

    with conn:
        cur = conn.cursor()

        _section(lines, "DATABASE")
        cur.execute("SELECT version();")
        lines.append(cur.fetchone()[0])
        for setting in ("max_connections", "idle_in_transaction_session_timeout",
                        "shared_buffers", "work_mem", "maintenance_work_mem"):
            cur.execute(f"SHOW {setting};")
            lines.append(f"{setting}: {cur.fetchone()[0]}")

        _section(lines, "SCHEMA")
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'blockchain' ORDER BY table_name;
        """)
        tables = [r[0] for r in cur.fetchall()]
        lines.append(f"tables: {', '.join(tables) if tables else 'NONE - run crawlbtc init-db'}")
        if not tables:
            return "\n".join(lines)

        cur.execute("""
            SELECT e.enumlabel FROM pg_enum e
            JOIN pg_type t ON t.oid = e.enumtypid
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname = 'blockchain' AND t.typname = 'address_type'
            ORDER BY e.enumsortorder;
        """)
        enum_vals = [r[0] for r in cur.fetchall()]
        lines.append(f"address_type enum: {enum_vals}")
        lines.append("p2pk enum value: " + ("present" if "p2pk" in enum_vals
                                            else "MISSING - run `crawlbtc migrate` before extracting"))

        _section(lines, "TABLE SIZES")
        cur.execute("""
            SELECT c.relname,
                   pg_size_pretty(pg_table_size(c.oid)),
                   pg_size_pretty(pg_indexes_size(c.oid)),
                   c.reltuples::bigint
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'blockchain' AND c.relkind = 'r'
            ORDER BY pg_total_relation_size(c.oid) DESC;
        """)
        lines.append(f"{'table':<20} {'data':>10} {'indexes':>10} {'~rows':>15}")
        for name, data_sz, idx_sz, rows in cur.fetchall():
            lines.append(f"{name:<20} {data_sz:>10} {idx_sz:>10} {rows:>15,}")

        _section(lines, "DUPLICATE / REDUNDANT INDEXES")
        cur.execute("""
            SELECT array_agg(idx.indexrelid::regclass::text ORDER BY idx.indexrelid) AS dupes,
                   pg_size_pretty(SUM(pg_relation_size(idx.indexrelid))::bigint) AS wasted
            FROM pg_index idx
            JOIN pg_class c ON c.oid = idx.indrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'blockchain'
            GROUP BY idx.indrelid, idx.indkey, pg_get_expr(idx.indpred, idx.indrelid)
            HAVING COUNT(*) > 1;
        """)
        dupes = cur.fetchall()
        if dupes:
            for group, wasted in dupes:
                lines.append(f"identical column sets: {group} (combined size {wasted})")
            lines.append("-> run `crawlbtc migrate` to drop redundant indexes")
        else:
            lines.append("none found")

        _section(lines, "JOB PROGRESS (block_jobs)")
        cur.execute("SELECT COUNT(*), MIN(height), MAX(height) FROM blockchain.block_jobs;")
        total, min_h, max_h = cur.fetchone()
        lines.append(f"job rows: {total:,} (heights {min_h}..{max_h})")
        db_max_height = max_h
        for col in ("vout_status", "vin_status", "address_status"):
            cur.execute(f"""
                SELECT {col}::text, COUNT(*) FROM blockchain.block_jobs
                GROUP BY 1 ORDER BY 2 DESC;
            """)
            parts = ", ".join(f"{status}={count:,}" for status, count in cur.fetchall())
            lines.append(f"{col:<15} {parts}")
        cur.execute("""
            SELECT features::text, COUNT(*) FROM blockchain.block_jobs
            GROUP BY 1 ORDER BY 2 DESC;
        """)
        lines.append("features        " + ", ".join(f"{f}={c:,}" for f, c in cur.fetchall()))

        _section(lines, "SATOSHI-ERA (P2PK) COVERAGE")
        # Coinbases were paid to raw pubkeys (P2PK) by default until Bitcoin
        # Core 0.8.2 (mid-2013, ~height 240k). The legacy extractor dropped
        # address-less outputs, so P2PK-only blocks were filed as
        # op_return_only/none/vin, and mixed early blocks are missing their
        # coinbase rows even though they look 'done'.
        cur.execute("""
            SELECT COUNT(*) FILTER (WHERE features IN ('op_return_only', 'none', 'vin')),
                   COUNT(*) FILTER (WHERE vout_status IN ('skipped', 'failed'))
            FROM blockchain.block_jobs;
        """)
        p2pk_suspect, skipped_failed = cur.fetchone()
        cur.execute("""
            SELECT COUNT(*) FROM blockchain.block_jobs
            WHERE height < 260000 AND features IN ('both', 'vout');
        """)
        early_done = cur.fetchone()[0]
        lines.append(f"blocks with addressless-output features (op_return_only/none/vin): {p2pk_suspect:,}")
        lines.append(f"blocks skipped or failed: {skipped_failed:,}")
        lines.append(f"early blocks (<260k) marked done that may still miss P2PK coinbase rows: {early_done:,}")
        if p2pk_suspect or early_done:
            lines.append("-> to pick up Satoshi-era coins:")
            lines.append("   crawlbtc requeue --phase vout --from 0 --to 260000")
            lines.append("   crawlbtc requeue --phase vout --features op_return_only,none,vin --failed --skipped")
            lines.append("   crawlbtc extract   (idempotent: only missing rows are added)")
        if "p2pk" in enum_vals:
            # Existence probe; time-boxed since address_type has no index.
            try:
                cur.execute("SET statement_timeout = '15s';")
                cur.execute("""
                    SELECT EXISTS (SELECT 1 FROM blockchain.transaction_io
                                   WHERE address_type = 'p2pk');
                """)
                lines.append("rows labeled address_type='p2pk': "
                             + ("present" if cur.fetchone()[0] else "none yet"))
            except psycopg.errors.QueryCanceled:
                lines.append("rows labeled address_type='p2pk': unknown (probe timed out)")
            finally:
                cur.execute("SET statement_timeout = DEFAULT;")

        lines.append("")
        lines.append("known early addresses in transaction_io:")
        for addr, desc in KNOWN_EARLY_ADDRESSES:
            cur.execute("""
                SELECT COUNT(*), COALESCE(SUM(amount), 0)
                FROM blockchain.transaction_io
                WHERE address = %s AND io_type = 'out';
            """, (addr,))
            count, sats = cur.fetchone()
            status = f"{count} out-rows, {sats:,} sats" if count else "ABSENT"
            lines.append(f"  {addr}  ({desc}): {status}")
        cur.execute("""
            SELECT COUNT(*) FROM blockchain.watch_addresses
            WHERE first_seen IS NOT NULL AND first_seen < '2011-01-01';
        """)
        lines.append(f"watch_addresses first seen before 2011: {cur.fetchone()[0]}")

        _section(lines, "SPOT CONSISTENCY CHECKS")
        cur.execute("""
            SELECT height FROM blockchain.block_jobs
            WHERE vin_status = 'done' AND height > 0
            ORDER BY random() LIMIT 5;
        """)
        sample_heights = [r[0] for r in cur.fetchall()]
        for h in sample_heights:
            cur.execute("SELECT COUNT(*) FROM blockchain.spends WHERE spent_height = %s;", (h,))
            lines.append(f"height {h}: spends rows = {cur.fetchone()[0]}")
        cur.execute("""
            SELECT COUNT(*) FROM blockchain.block_jobs
            WHERE vout_status = 'failed' OR vin_status = 'failed' OR address_status = 'failed';
        """)
        failed = cur.fetchone()[0]
        lines.append(f"blocks with any failed phase: {failed:,}"
                     + ("  -> requeue with: crawlbtc requeue --failed" if failed else ""))

        _section(lines, "WATCH ADDRESSES")
        cur.execute("""
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE balance_sats > 0),
                   COUNT(*) FILTER (WHERE private_key_wif IS NOT NULL OR private_key_hex IS NOT NULL)
            FROM blockchain.watch_addresses;
        """)
        total_wa, funded, with_keys = cur.fetchone()
        lines.append(f"total: {total_wa:,}, with balance: {funded:,}")
        if with_keys:
            lines.append(f"WARNING: {with_keys:,} rows store PLAINTEXT PRIVATE KEYS - "
                         "consider moving keys out of the crawler database")

        _section(lines, "EARLY BLOCK SAMPLE (db rows per block)")
        asyncio.run(_sample_early_blocks(cfg, conn, lines))

    _section(lines, "BITCOIN CORE NODE")
    asyncio.run(_probe_node(cfg, lines, db_max_height))

    lines.append("")
    lines.append("(paste this whole report back for analysis)")
    return "\n".join(lines)
