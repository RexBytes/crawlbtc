#!/usr/bin/env python3
import os
import math
import asyncio
import signal
import aiohttp
import datetime
import random
import time
import sys
import traceback
from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict
from dotenv import load_dotenv
from pathlib import Path
from psycopg_pool import AsyncConnectionPool
import psycopg
from io import StringIO
import structlog
import re
from structlog.stdlib import BoundLogger
from structlog.typing import EventDict
from structlog.processors import TimeStamper, JSONRenderer
from decimal import Decimal, ROUND_DOWN, getcontext

# High precision for value -> satoshi conversion
getcontext().prec = 28

# === Logging ===
class ProgressOnlyFilter:
    def __init__(self, allowed_event: str = "progress"):
        self.allowed_event = allowed_event
    def __call__(self, logger: BoundLogger, method_name: str, event_dict: EventDict) -> EventDict:
        log_level = os.getenv("LOG_LEVEL", "").lower()
        if log_level == "progress":
            if event_dict.get("event") == self.allowed_event:
                return event_dict
            raise structlog.DropEvent
        return event_dict

structlog.configure(
    processors=[TimeStamper(fmt="iso"), structlog.stdlib.add_log_level, ProgressOnlyFilter(), JSONRenderer()]
)
log = structlog.get_logger("vin-backfill")

TXID_RE = re.compile(r'^[0-9a-fA-F]{64}$')
SATS_PER_BTC = Decimal(100_000_000)

# ---- scaling helpers ----
def clamp(v, lo, hi): return max(lo, min(hi, v))

def derive_runtime_limits(num_workers: int) -> dict:
    rpc_conc = clamp(num_workers * 2, 10, 128)
    db_max_conn = clamp(math.ceil(num_workers * 0.60), 8, 128)
    db_write_conc = clamp(math.floor(db_max_conn * 0.50), 4, max(4, db_max_conn - 3))
    # keep default JOB_BATCH_SIZE small for your accurate progress style
    job_batch_size = int(os.getenv("JOB_BATCH_SIZE", "1"))
    return {
        "RPC_CONCURRENCY": int(os.getenv("RPC_CONCURRENCY", str(rpc_conc))),
        "DB_MAX_CONN": int(os.getenv("DB_MAX_CONN", str(db_max_conn))),
        "DB_WRITE_CONCURRENCY": int(os.getenv("DB_WRITE_CONCURRENCY", str(db_write_conc))),
        "JOB_BATCH_SIZE": job_batch_size,
    }

# === Configuration ===
@dataclass
class Config:
    rpc_url: str
    rpc_auth: aiohttp.BasicAuth
    db_conninfo: str
    num_workers: int
    job_batch_size: int

def load_config() -> Config:
    load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

    power = int(os.getenv("POWER", "100"))
    cores = os.cpu_count() or 1
    # Keep your typical worker sizing logic simple here
    auto_workers = max(8, min(int(round(cores * 2.0 * power / 100.0)), 96))
    num_workers = int(os.getenv("NUM_WORKERS", str(auto_workers)))

    db_conninfo = (
        f"host={os.getenv('PG_HOST')} port={os.getenv('PG_PORT')} "
        f"dbname={os.getenv('PG_DB')} user={os.getenv('PG_USER')} password={os.getenv('PG_PASSWORD')}"
    )
    auto = derive_runtime_limits(num_workers)

    os.environ.setdefault("RPC_CONCURRENCY", str(auto["RPC_CONCURRENCY"]))
    os.environ.setdefault("DB_MAX_CONN", str(auto["DB_MAX_CONN"]))
    os.environ.setdefault("DB_WRITE_CONCURRENCY", str(auto["DB_WRITE_CONCURRENCY"]))
    os.environ.setdefault("JOB_BATCH_SIZE", str(auto["JOB_BATCH_SIZE"]))

    job_batch_size = int(os.getenv("JOB_BATCH_SIZE"))
    log.info("vin_autosizing_plan",
             num_workers=num_workers,
             rpc_concurrency=os.environ["RPC_CONCURRENCY"],
             db_max_conn=os.environ["DB_MAX_CONN"],
             db_write_conc=os.environ["DB_WRITE_CONCURRENCY"],
             job_batch_size=job_batch_size)

    return Config(
        rpc_url=f"http://{os.getenv('RPC_HOST')}:{os.getenv('RPC_PORT')}",
        rpc_auth=aiohttp.BasicAuth(os.getenv("RPC_USER"), os.getenv("RPC_PASSWORD")),
        db_conninfo=db_conninfo,
        num_workers=num_workers,
        job_batch_size=job_batch_size
    )

config = load_config()

# Global concurrency knobs
RPC_CONCURRENCY = int(os.environ["RPC_CONCURRENCY"])
DB_MAX_CONN = int(os.environ["DB_MAX_CONN"])
DB_WRITE_CONCURRENCY = int(os.environ["DB_WRITE_CONCURRENCY"])

# Global throttles
rpc_semaphore = asyncio.BoundedSemaphore(RPC_CONCURRENCY)
db_write_sem = asyncio.BoundedSemaphore(DB_WRITE_CONCURRENCY)

# NEW: guard total concurrent DB users (reads + writes) so we don't exhaust the pool
DB_CONN_GUARD = max(1, DB_MAX_CONN - 2)  # leave a couple for maintenance/monitor
db_conn_sem = asyncio.BoundedSemaphore(DB_CONN_GUARD)

pool: Optional[AsyncConnectionPool] = None
shutdown_event = asyncio.Event()
metrics = {"blocks_processed": 0, "errors": 0, "vins_inserted": 0}

# === Utility Functions ===
def validate_txid(txid: str) -> bool:
    return isinstance(txid, str) and TXID_RE.match(txid) is not None

def to_sats(value) -> int:
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError("non-finite value")
    if d <= 0:
        return 0
    return int((d * SATS_PER_BTC).to_integral_value(rounding=ROUND_DOWN))

def get_address_type(address: Optional[str]) -> Optional[str]:
    if not address or not isinstance(address, str):
        return None
    if address.startswith("1"): return "p2pkh"
    if address.startswith("3"): return "p2sh"
    if address.startswith("bc1p"): return "taproot"
    if address.startswith("bc1q"): return "bech32"
    return "unknown"

def normalize_address_type(script_type: Optional[str], address: Optional[str]) -> str:
    mapping = {
        "pubkeyhash": "p2pkh",
        "scripthash": "p2sh",
        "witness_v0_keyhash": "bech32",
        "witness_v1_taproot": "taproot"
    }
    if script_type in mapping:
        return mapping[script_type]
    return get_address_type(address) or "unknown"

def _first_addr(spk: dict | None) -> Optional[str]:
    spk = spk or {}
    if isinstance(spk.get("address"), str):
        return spk["address"]
    addrs = spk.get("addresses")
    if isinstance(addrs, list) and addrs and isinstance(addrs[0], str):
        return addrs[0]
    return None

async def with_retry(
    coro_func, *args, retries=3, delay=1.0, backoff=2.0, jitter=0.1,
    exceptions=(aiohttp.ClientError, psycopg.Error, ValueError, RuntimeError),
    **kwargs,
):
    last_exception = None
    for attempt in range(retries):
        try:
            return await coro_func(*args, **kwargs)
        except exceptions as e:
            last_exception = e
            if attempt < retries - 1:
                sleep_time = delay * (backoff ** attempt) + random.uniform(0, jitter)
                log.warning("retry", func=getattr(coro_func, "__name__", str(coro_func)), attempt=attempt + 1, retries=retries, error=str(e), sleep=sleep_time)
                await asyncio.sleep(sleep_time)
            else:
                log.error("retry_exhausted", func=getattr(coro_func, "__name__", str(coro_func)), error=str(last_exception))
                metrics["errors"] += 1
                raise last_exception
        except asyncio.CancelledError:
            raise

# === RPC ===
async def rpc_call(session: aiohttp.ClientSession, method: str, params: Optional[List] = None, timeout: float = 30.0) -> dict:
    async with rpc_semaphore:
        async with session.post(
            config.rpc_url,
            json={"jsonrpc": "1.0", "id": "vin-backfill", "method": method, "params": params or []},
            auth=config.rpc_auth,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if data.get("error"):
                raise ValueError(f"RPC error: {data['error']}")
            return data["result"]

async def get_block_with_prevouts(session, block_hash):
    # Prefer verbosity=3 (vin.prevout included), fall back to 2
    try:
        return await with_retry(rpc_call, session, "getblock", [block_hash, 3], timeout=120.0)
    except Exception:
        return await with_retry(rpc_call, session, "getblock", [block_hash, 2], timeout=120.0)

async def check_rpc_health(session: aiohttp.ClientSession) -> bool:
    try:
        tip = await rpc_call(session, "getblockcount")
        log.info("rpc_healthy", block_count=tip)
        return True
    except Exception as e:
        log.error("rpc_unreachable", error=str(e))
        return False

# === DB COPY helpers ===
async def insert_spends_rows(conn, spends_rows):
    """
    spends_rows: List[Tuple[prev_txid, prev_vout, spending_txid, spending_vin, spent_height, spent_block, spent_time]]
    """
    if not spends_rows:
        return

    buf = StringIO()
    for prev_txid, prev_vout, spending_txid, spending_vin, height, block_hash, ts in spends_rows:
        # ts is a timezone-aware datetime (UTC)
        buf.write(f"{prev_txid}\t{prev_vout}\t{spending_txid}\t{spending_vin}\t{height}\t{block_hash}\t{ts.isoformat()}\n")
    payload = buf.getvalue()

    async with conn.cursor() as cur:
        await cur.execute("""
            CREATE TEMP TABLE IF NOT EXISTS _spends_stage (
              prev_txid     text,
              prev_vout     int,
              spending_txid text,
              spending_vin  int,
              spent_height  int,
              spent_block   text,
              spent_time    timestamp
            ) ON COMMIT DROP;
            TRUNCATE _spends_stage;
        """)
        async with cur.copy("""
            COPY _spends_stage (prev_txid, prev_vout, spending_txid, spending_vin, spent_height, spent_block, spent_time)
            FROM STDIN WITH (FORMAT text)
        """) as copy:
            await copy.write(payload)

        await cur.execute("""
            INSERT INTO blockchain.spends (
              prev_txid, prev_vout, spending_txid, spending_vin, spent_height, spent_block, spent_time
            )
            SELECT prev_txid, prev_vout, spending_txid, spending_vin, spent_height, spent_block, spent_time
            FROM _spends_stage
            ON CONFLICT (prev_txid, prev_vout) DO NOTHING;
        """)

def is_valid_txio_row(row: Tuple) -> bool:
    txid, address, address_type, amount, io_type, idx = row
    return (
        isinstance(txid, str) and TXID_RE.match(txid) is not None and
        isinstance(address, str) and
        isinstance(address_type, str) and
        isinstance(amount, int) and amount >= 0 and
        io_type in ("in", "out") and
        isinstance(idx, int)
    )

async def insert_txio_rows(conn, txio_rows: List[Tuple]) -> None:
    if not txio_rows:
        return
    txio_rows = [row for row in txio_rows if is_valid_txio_row(row)]
    if not txio_rows:
        return

    buf = StringIO()
    for txid, address, address_type, amount, io_type, idx in txio_rows:
        buf.write(f"{txid}\t{address}\t{address_type}\t{amount}\t{io_type}\t{idx}\n")
    payload = buf.getvalue()

    async with conn.cursor() as cur:
        await cur.execute("""
            CREATE TEMP TABLE IF NOT EXISTS _txio_stage
            (txid text, address text, address_type text, amount bigint, io_type text, idx int)
            ON COMMIT DROP;
            TRUNCATE _txio_stage;
        """)
        async with cur.copy("""
            COPY _txio_stage (txid, address, address_type, amount, io_type, idx)
            FROM STDIN WITH (FORMAT text)
        """) as copy:
            await copy.write(payload)
        await cur.execute("""
            INSERT INTO blockchain.transaction_io (txid, address, address_type, amount, io_type, idx)
            SELECT txid, address, address_type::blockchain.address_type, amount, io_type::blockchain.tx_io_type, idx
            FROM _txio_stage
            ON CONFLICT (txid, io_type, idx) DO NOTHING;
        """)
    log.info("inserted_txio_rows", count=len(txio_rows))

async def update_total_in(conn, per_tx_vin_totals: Dict[str, int]) -> None:
    if not per_tx_vin_totals:
        return
    buf = StringIO()
    for txid, total_in in per_tx_vin_totals.items():
        buf.write(f"{txid}\t{total_in}\n")
    payload = buf.getvalue()
    async with conn.cursor() as cur:
        await cur.execute("""
            CREATE TEMP TABLE IF NOT EXISTS _vin_totals (txid text primary key, total_in bigint)
            ON COMMIT DROP;
            TRUNCATE _vin_totals;
        """)
        async with cur.copy("""
            COPY _vin_totals (txid, total_in) FROM STDIN WITH (FORMAT text)
        """) as copy:
            await copy.write(payload)
        await cur.execute("""
            UPDATE blockchain.transactions t
               SET total_in = v.total_in
              FROM _vin_totals v
             WHERE t.txid = v.txid
               AND (t.total_in IS NULL OR t.total_in = 0);
        """)

# === VIN job control (uses vin_status) ===
async def reset_abandoned_vin_jobs():
    async with db_conn_sem:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE blockchain.block_jobs
                       SET vin_status = 'pending'::blockchain.block_job_status,
                           updated_at = now()
                     WHERE vin_status = 'in_progress'::blockchain.block_job_status
                       AND updated_at < now() - interval '15 minutes';
                """)
                await conn.commit()

async def get_next_vin_jobs(batch_size: int) -> List[int]:
    async with db_conn_sem:
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute("""
                        UPDATE blockchain.block_jobs
                           SET vin_status = 'in_progress'::blockchain.block_job_status,
                               updated_at = now()
                         WHERE height IN (
                           SELECT height
                             FROM blockchain.block_jobs
                            WHERE vin_status = 'pending'::blockchain.block_job_status
                              AND vout_status = ANY (ARRAY['done','skipped']::blockchain.block_job_status[])
                            ORDER BY height
                            LIMIT %s
                            FOR UPDATE SKIP LOCKED
                         )
                     RETURNING height;
                    """, (batch_size,))
                    rows = await cur.fetchall()
                    return [r[0] for r in rows]

async def mark_vin_done(conn, height: int):
    async with conn.cursor() as cur:
        await cur.execute("""
            UPDATE blockchain.block_jobs
               SET vin_status = 'done'::blockchain.block_job_status,
                   updated_at = now()
             WHERE height = %s;
        """, (height,))

async def mark_vin_failed(height: int):
    async with db_conn_sem:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE blockchain.block_jobs
                       SET vin_status = 'failed'::blockchain.block_job_status,
                           updated_at = now()
                     WHERE height = %s;
                """, (height,))
    metrics["errors"] += 1

async def get_vin_progress() -> Tuple[int, int, int, int, int]:
    async with db_conn_sem:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT
                      COUNT(*) FILTER (WHERE vin_status = 'done'::blockchain.block_job_status)        AS done,
                      COUNT(*) FILTER (WHERE vin_status = 'in_progress'::blockchain.block_job_status) AS in_progress,
                      COUNT(*) FILTER (WHERE vin_status = 'pending'::blockchain.block_job_status)     AS pending,
                      COUNT(*) FILTER (WHERE vin_status = 'failed'::blockchain.block_job_status)      AS failed,
                      COALESCE(MAX(height) FILTER (WHERE vin_status = 'done'::blockchain.block_job_status), 0)
                        AS latest_vin_done_height
                    FROM blockchain.block_jobs
                    WHERE vout_status = ANY (ARRAY['done','skipped']::blockchain.block_job_status[]);
                """)
                return await cur.fetchone()

# === Core VIN processor ===
async def backfill_vins_for_height(session: aiohttp.ClientSession, height: int) -> int:
    """Returns number of 'in' rows inserted for this block (0 is fine for coinbase-only)."""
    try:
        block_hash = await with_retry(rpc_call, session, "getblockhash", [height])
        # Before fetching whole block, quick DB check: if already complete, just mark done.
        async with db_conn_sem:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        WITH per_tx AS (
                          SELECT t.txid,
                                 EXISTS (SELECT 1 FROM blockchain.transaction_io i
                                         WHERE i.txid=t.txid AND i.io_type='in'::blockchain.tx_io_type) AS has_in
                          FROM blockchain.transactions t
                          WHERE t.block_hash = %s
                        ),
                        spend_ct AS (
                          SELECT COUNT(*) AS c FROM blockchain.spends WHERE spent_height = %s
                        )
                        SELECT
                          COUNT(*) FILTER (WHERE NOT has_in) AS missing_inputs,
                          (SELECT c FROM spend_ct)           AS spend_edges
                        FROM per_tx;
                    """, (block_hash, height))
                    missing_inputs, spend_edges = (await cur.fetchone())
                    if (missing_inputs or 0) <= 1 and (spend_edges or 0) > 0:
                        async with conn.transaction():
                            await mark_vin_done(conn, height)
                        return 0

        block = await get_block_with_prevouts(session, block_hash)

        block_time = block.get("time")
        if not isinstance(block_time, int):
            raise ValueError(f"Invalid block time for height {height}")
        spent_ts = datetime.datetime.fromtimestamp(block_time, datetime.UTC)

        txio_rows_in: List[Tuple] = []
        per_tx_vin_totals: Dict[str, int] = {}
        missing_prevouts: List[Tuple[str, int, str, int]] = []  # prev_txid, prev_idx, cur_txid, vin_idx
        spends_rows: List[Tuple[str, int, str, int, int, str, datetime.datetime]] = []  # <--- NEW

        for tx in block.get("tx", []):
            txid = tx.get("txid")
            if not validate_txid(txid):
                continue
            vin_total = 0
            for vin_idx, vin in enumerate(tx.get("vin", []) or []):
                if not isinstance(vin, dict) or "coinbase" in vin:
                    continue
                prev_txid, prev_idx = vin.get("txid"), vin.get("vout")
                if not (validate_txid(prev_txid) and isinstance(prev_idx, int)):
                    continue

                # NEW: record the spend edge immediately
                spends_rows.append((
                    prev_txid,
                    prev_idx,
                    txid,
                    vin_idx,
                    height,
                    block_hash,
                    spent_ts,
                ))

                prevout = vin.get("prevout") or {}
                if prevout:
                    spk = prevout.get("scriptPubKey", {}) or {}
                    address = _first_addr(spk)
                    value = prevout.get("value")
                    if isinstance(address, str) and isinstance(value, (int, float, str)):
                        try:
                            sat = to_sats(value)
                        except Exception:
                            sat = 0
                        if sat > 0:
                            addr_type = normalize_address_type(spk.get("type"), address)
                            vin_total += sat
                            txio_rows_in.append((txid, address, addr_type, sat, 'in', vin_idx))
                    else:
                        missing_prevouts.append((prev_txid, prev_idx, txid, vin_idx))
                else:
                    missing_prevouts.append((prev_txid, prev_idx, txid, vin_idx))
            per_tx_vin_totals[txid] = vin_total

        # DB phase
        async with db_write_sem:
            async with db_conn_sem:
                async with pool.connection() as conn:
                    async def _write():
                        async with conn.transaction():
                            # 1) Resolve missing prevouts using a CURSOR
                            if missing_prevouts:
                                async with conn.cursor() as cur:
                                    await cur.execute("""
                                        CREATE TEMP TABLE IF NOT EXISTS _needed_prevouts
                                        (prev_txid text, prev_idx int, cur_txid text, vin_idx int)
                                        ON COMMIT DROP;
                                    """)
                                    await cur.execute("TRUNCATE _needed_prevouts;")

                                    buf = StringIO()
                                    for prev_txid, prev_idx, cur_txid, vin_idx in missing_prevouts:
                                        buf.write(f"{prev_txid}\t{prev_idx}\t{cur_txid}\t{vin_idx}\n")
                                    payload = buf.getvalue()

                                    async with cur.copy("""
                                        COPY _needed_prevouts (prev_txid, prev_idx, cur_txid, vin_idx)
                                        FROM STDIN WITH (FORMAT text)
                                    """) as copy:
                                        await copy.write(payload)

                                    await cur.execute("""
                                        SELECT n.cur_txid, n.vin_idx, tio.address, tio.address_type, tio.amount
                                          FROM _needed_prevouts n
                                          JOIN blockchain.transaction_io tio
                                            ON tio.txid = n.prev_txid
                                           AND tio.idx  = n.prev_idx
                                           AND tio.io_type = 'out'::blockchain.tx_io_type;
                                    """)
                                    rows = await cur.fetchall()
                                    for cur_txid, vin_idx, address, addr_type, amt in rows:
                                        txio_rows_in.append((cur_txid, address, addr_type, int(amt), 'in', int(vin_idx)))
                                        per_tx_vin_totals[cur_txid] = per_tx_vin_totals.get(cur_txid, 0) + int(amt)

                            # 2) Bulk writes (these helpers open their own cursors)
                            await insert_txio_rows(conn, txio_rows_in)
                            await update_total_in(conn, per_tx_vin_totals)
                            await insert_spends_rows(conn, spends_rows)
                            await mark_vin_done(conn, height)

                    await asyncio.shield(_write())

        metrics["blocks_processed"] += 1
        metrics["vins_inserted"] += len(txio_rows_in)
        metrics["spends_inserted"] = metrics.get("spends_inserted", 0) + len(spends_rows)
        return len(txio_rows_in)

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error("vin_block_failed", height=height, error=str(e), traceback=traceback.format_exc())
        await mark_vin_failed(height)
        return 0

# === Workers ===
async def worker_loop(session: aiohttp.ClientSession):
    while not shutdown_event.is_set():
        heights = await get_next_vin_jobs(config.job_batch_size)
        if not heights:
            await asyncio.sleep(2)
            continue

        log.info("worker_starting_vin_jobs", heights=heights)
        tasks = [backfill_vins_for_height(session, h) for h in heights]
        await asyncio.gather(*tasks)

async def monitor_loop(session: aiohttp.ClientSession):
    start_done = None
    start_time = asyncio.get_event_loop().time()

    while not shutdown_event.is_set():
        try:
            done, in_progress, pending, failed, latest_vin_done_height = await get_vin_progress()
            now = datetime.datetime.now(datetime.UTC)
            total_seen = done + failed

            if start_done is None and total_seen > 0:
                start_done = total_seen
                start_time = asyncio.get_event_loop().time()

            eta_str = "calculating..."
            if start_done is not None and done > start_done:
                rate = (done - start_done) / (asyncio.get_event_loop().time() - start_time)
                eta = now + datetime.timedelta(seconds=(pending / rate)) if rate > 0 else now
                eta_str = eta.strftime("%Y-%m-%d %H:%M:%S")

            # Two compact lines for parity with your VOUT script
            log.info("progress", line=1,
                     done=done, in_progress=in_progress, pending=pending, failed=failed, eta=eta_str)
            log.info("progress", line=2,
                     latest_processed_block=latest_vin_done_height,
                     vins_inserted=metrics["vins_inserted"],
                     spends_inserted=metrics.get("spends_inserted", 0),
                     blocks_processed=metrics["blocks_processed"],
                     errors=metrics["errors"])

            if pending == 0 and in_progress == 0:
                log.info("vin_all_blocks_processed", metrics=metrics)
                shutdown_event.set()

        except Exception as e:
            log.error("vin_monitor_error", error=str(e))
            metrics["errors"] += 1

        await asyncio.sleep(5)

# === Main Entrypoint ===
def handle_shutdown():
    asyncio.get_running_loop().call_soon_threadsafe(shutdown_event.set)

async def main():
    global pool
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, lambda s, f: handle_shutdown())

    connector = aiohttp.TCPConnector(limit=RPC_CONCURRENCY, limit_per_host=RPC_CONCURRENCY)

    async with AsyncConnectionPool(
        config.db_conninfo,
        min_size=max(1, min(DB_MAX_CONN // 2, config.num_workers)),
        max_size=DB_MAX_CONN,
        timeout=float(os.getenv("DB_POOL_TIMEOUT", "60")),
    ) as p:
        pool = p

        async with aiohttp.ClientSession(connector=connector) as session:
            if not await check_rpc_health(session):
                log.critical("bitcoin_core_unavailable", message="Bitcoin Core RPC is not responding. Exiting.")
                print("⚠️  Bitcoin Core is not responding. Exiting now.")
                sys.exit(1)

            await reset_abandoned_vin_jobs()
            log.info("starting_vin_workers",
                     num_workers=config.num_workers,
                     rpc_concurrency=RPC_CONCURRENCY,
                     db_max_conn=DB_MAX_CONN,
                     db_write_conc=DB_WRITE_CONCURRENCY,
                     job_batch_size=config.job_batch_size)

            start_delay = float(os.getenv("START_DELAY", "0"))
            if start_delay > 0:
                log.info("starting_in", seconds=start_delay)
                await asyncio.sleep(start_delay)

            tasks = [worker_loop(session) for _ in range(config.num_workers)]
            tasks.append(monitor_loop(session))
            await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())

