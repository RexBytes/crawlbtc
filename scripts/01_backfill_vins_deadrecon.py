#!/usr/bin/env python3
import os
import math
import asyncio
import signal
import aiohttp
import datetime
import random
import sys
import traceback
from dataclasses import dataclass
from typing import Optional, List, Tuple
from dotenv import load_dotenv
from pathlib import Path
from psycopg_pool import AsyncConnectionPool
import psycopg
from io import StringIO
import structlog
from structlog.stdlib import BoundLogger
from structlog.typing import EventDict
from structlog.processors import TimeStamper, JSONRenderer

# === Logging (same style as your other scripts) ===
class ProgressOnlyFilter:
    def __init__(self, allowed_event: str = "progress"):
        self.allowed_event = allowed_event
    def __call__(self, logger: BoundLogger, method_name: str, event_dict: EventDict):
        log_level = os.getenv("LOG_LEVEL", "").lower()
        if log_level == "progress":
            if event_dict.get("event") == self.allowed_event:
                return event_dict
            raise structlog.DropEvent
        return event_dict

structlog.configure(
    processors=[TimeStamper(fmt="iso"), structlog.stdlib.add_log_level, ProgressOnlyFilter(), JSONRenderer()]
)
log = structlog.get_logger("addr-updater")

# === Config / autosizing helpers ===
def clamp(v, lo, hi): return max(lo, min(hi, v))

def derive_runtime_limits(num_workers: int) -> dict:
    rpc_conc = clamp(num_workers * 2, 10, 128)
    db_max_conn = clamp(math.ceil(num_workers * 0.60), 8, 128)
    db_write_conc = clamp(math.floor(db_max_conn * 0.50), 4, max(4, db_max_conn - 3))
    return {
        "RPC_CONCURRENCY": int(os.getenv("RPC_CONCURRENCY", str(rpc_conc))),
        "DB_MAX_CONN": int(os.getenv("DB_MAX_CONN", str(db_max_conn))),
        "DB_WRITE_CONCURRENCY": int(os.getenv("DB_WRITE_CONCURRENCY", str(db_write_conc))),
        # Slightly higher default to keep DB lanes full now that each height is a single TX
        "JOB_BATCH_SIZE": int(os.getenv("JOB_BATCH_SIZE", "2")),
    }

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
    log.info("addr_autosizing_plan",
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

RPC_CONCURRENCY = int(os.environ["RPC_CONCURRENCY"])
DB_MAX_CONN = int(os.environ["DB_MAX_CONN"])
DB_WRITE_CONCURRENCY = int(os.environ["DB_WRITE_CONCURRENCY"])

rpc_semaphore = asyncio.BoundedSemaphore(RPC_CONCURRENCY)
db_write_sem = asyncio.BoundedSemaphore(DB_WRITE_CONCURRENCY)

# Optional: guard total concurrent DB users (reads + writes) so we don't exhaust the pool
DB_CONN_GUARD = max(1, DB_MAX_CONN - 2)  # leave a couple for maintenance/monitor
db_conn_sem = asyncio.BoundedSemaphore(DB_CONN_GUARD)

pool: Optional[AsyncConnectionPool] = None
shutdown_event = asyncio.Event()

# === Dead-reckoning baseline + local counters ===
@dataclass
class Baseline:
    total_jobs: int
    done0: int
    failed0: int
    latest_done0: int

baseline: Optional[Baseline] = None

local = {
    "claimed": 0,             # pending -> in_progress since process start
    "done": 0,                # finished successfully since start
    "failed": 0,              # finished failed since start
    "in_progress": 0,         # currently executing in this process
    "latest_done_height": 0,  # running max since start
    "touched_addresses": 0,
    "heights_done": 0,
    "errors": 0,
}

metrics = local  # keep original name used in logs

async def load_baseline() -> Baseline:
    async with db_conn_sem:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT
                      (SELECT COUNT(*) FROM blockchain.block_jobs
                        WHERE vout_status IN ('done','skipped')
                          AND vin_status  = 'done') AS total_jobs,
                      (SELECT COUNT(*) FROM blockchain.block_jobs
                        WHERE address_status='done'
                          AND vout_status IN ('done','skipped')
                          AND vin_status  = 'done') AS done0,
                      (SELECT COUNT(*) FROM blockchain.block_jobs
                        WHERE address_status='failed'
                          AND vout_status IN ('done','skipped')
                          AND vin_status  = 'done') AS failed0,
                      (SELECT COALESCE(MAX(height), 0) FROM blockchain.block_jobs
                        WHERE address_status='done'
                          AND vout_status IN ('done','skipped')
                          AND vin_status  = 'done') AS latest_done0
                """)
                total_jobs, done0, failed0, latest_done0 = await cur.fetchone()
                return Baseline(total_jobs, done0, failed0, latest_done0)

def current_progress():
    # Combine baseline with process-local deltas
    done = (baseline.done0 if baseline else 0) + local["done"]
    failed = (baseline.failed0 if baseline else 0) + local["failed"]
    in_prog = local["in_progress"]
    total_jobs = (baseline.total_jobs if baseline else 0)
    pending = max(total_jobs - (done + failed + in_prog), 0)
    latest_done_height = max((baseline.latest_done0 if baseline else 0), local["latest_done_height"])
    processed_since_start = local["done"] + local["failed"]
    return done, in_prog, pending, failed, latest_done_height, processed_since_start

# === Retry + RPC ===
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
                local["errors"] += 1
                raise last_exception
        except asyncio.CancelledError:
            raise

async def rpc_call(session: aiohttp.ClientSession, method: str, params: Optional[List] = None, timeout: float = 30.0) -> dict:
    async with rpc_semaphore:
        async with session.post(
            config.rpc_url,
            json={"jsonrpc": "1.0", "id": "addr-updater", "method": method, "params": params or []},
            auth=config.rpc_auth,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            if data.get("error"):
                raise ValueError(f"RPC error: {data['error']}")
            return data["result"]

async def get_block_meta(session, height: int) -> Tuple[str, datetime.datetime]:
    # Use lightweight header instead of full block; still two calls (hash + header)
    block_hash = await with_retry(rpc_call, session, "getblockhash", [height])
    header = await with_retry(rpc_call, session, "getblockheader", [block_hash, True], timeout=15.0)
    block_time = header.get("time")
    if not isinstance(block_time, int):
        raise ValueError(f"Invalid block time for height {height}")
    return block_hash, datetime.datetime.fromtimestamp(block_time, datetime.UTC)

async def check_rpc_health(session: aiohttp.ClientSession) -> bool:
    try:
        tip = await rpc_call(session, "getblockcount")
        log.info("rpc_healthy", block_count=tip)
        return True
    except Exception as e:
        log.error("rpc_unreachable", error=str(e))
        return False

# === Address job control (uses address_status) ===
async def reset_abandoned_addr_jobs():
    async with db_conn_sem:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE blockchain.block_jobs
                       SET address_status = 'pending'::blockchain.block_job_status,
                           updated_at     = now()
                     WHERE address_status = 'in_progress'::blockchain.block_job_status
                       AND updated_at < now() - interval '15 minutes';
                """)
                await conn.commit()

async def get_next_address_jobs(batch_size: int) -> List[int]:
    async with db_conn_sem:
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute("""
                        UPDATE blockchain.block_jobs
                           SET address_status = 'in_progress'::blockchain.block_job_status,
                               updated_at     = now()
                         WHERE height IN (
                           SELECT height
                             FROM blockchain.block_jobs
                            WHERE address_status = 'pending'::blockchain.block_job_status
                              AND vout_status = ANY (ARRAY['done','skipped']::blockchain.block_job_status[])
                              AND vin_status  = 'done'::blockchain.block_job_status
                            ORDER BY height
                            LIMIT %s
                            FOR UPDATE SKIP LOCKED
                         )
                     RETURNING height;
                    """, (batch_size,))
                    rows = await cur.fetchall()
                    heights = [r[0] for r in rows]
                    # process-local bookkeeping
                    if heights:
                        local["claimed"] += len(heights)
                        local["in_progress"] += len(heights)
                    return heights

async def mark_address_done(conn, height: int):
    # Only flip status in DB; local counters are bumped by the caller AFTER commit returns
    async with conn.cursor() as cur:
        await cur.execute("""
            UPDATE blockchain.block_jobs
               SET address_status = 'done'::blockchain.block_job_status,
                   updated_at     = now()
             WHERE height = %s;
        """, (height,))

async def mark_address_failed(height: int):
    async with db_conn_sem:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE blockchain.block_jobs
                       SET address_status = 'failed'::blockchain.block_job_status,
                           updated_at     = now()
                     WHERE height = %s;
                """, (height,))
    local["failed"] += 1
    local["in_progress"] = max(0, local["in_progress"] - 1)
    local["errors"] += 1

# === Core per-height processor (idempotent) ===
async def apply_address_deltas_for_height(session: aiohttp.ClientSession, height: int) -> int:
    """
    Returns the number of watch_addresses rows touched for this height.
    All updates + status flip happen in a single transaction for idempotency.
    If anything fails before we flip to 'done', the transaction is rolled back
    and we later mark the job 'failed' (separate UPDATE).
    """
    try:
        block_hash, block_ts = await get_block_meta(session, height)

        async with db_write_sem:
            async with db_conn_sem:
                async with pool.connection() as conn:
                    async def _write() -> int:
                        async with conn.transaction():
                            # Backfill-friendly settings, scoped to this TX only
                            async with conn.cursor() as cur:
                                await cur.execute("SET LOCAL synchronous_commit = off;")
                                await cur.execute("SET LOCAL work_mem = '128MB';")
                                await cur.execute("SET LOCAL jit = off;")  # optional

                                # 1) Materialize txids for this block and index them (reused below)
                                await cur.execute("""
                                    CREATE TEMP TABLE IF NOT EXISTS _block_txs (txid text primary key) ON COMMIT DROP;
                                    TRUNCATE _block_txs;
                                    INSERT INTO _block_txs(txid)
                                    SELECT t.txid
                                      FROM blockchain.transactions t
                                     WHERE t.block_hash = %s;
                                    CREATE INDEX IF NOT EXISTS _ix_block_txs_txid ON _block_txs(txid);
                                """, (block_hash,))

                                # Optional fast-path: if no watched address is touched, mark done and bail
                                await cur.execute("""
                                    WITH touched AS (
                                      SELECT 1
                                        FROM blockchain.transaction_io i
                                        JOIN _block_txs bt ON bt.txid = i.txid
                                       WHERE i.io_type = 'out'::blockchain.tx_io_type
                                         AND EXISTS (SELECT 1 FROM blockchain.watch_addresses wa WHERE wa.address = i.address)
                                       LIMIT 1
                                      UNION ALL
                                      SELECT 1
                                        FROM blockchain.spends s
                                        JOIN blockchain.transaction_io tio
                                          ON tio.txid = s.prev_txid AND tio.idx = s.prev_vout
                                         AND tio.io_type = 'out'::blockchain.tx_io_type
                                       WHERE s.spent_height = %s
                                         AND EXISTS (SELECT 1 FROM blockchain.watch_addresses wa WHERE wa.address = tio.address)
                                       LIMIT 1
                                    )
                                    SELECT COUNT(*) FROM touched;
                                """, (height,))
                                if (await cur.fetchone())[0] == 0:
                                    await mark_address_done(conn, height)
                                    return 0

                                # 2) Single-pass aggregation and deterministic row locking
                                await cur.execute("""
                                    WITH credits AS (
                                      SELECT i.address,
                                             SUM(i.amount)::bigint AS credit_sats,
                                             COUNT(*)::int         AS credit_utxos
                                        FROM blockchain.transaction_io i
                                        JOIN _block_txs bt ON bt.txid = i.txid
                                       WHERE i.io_type = 'out'::blockchain.tx_io_type
                                         AND EXISTS (SELECT 1 FROM blockchain.watch_addresses wa WHERE wa.address = i.address)
                                       GROUP BY i.address
                                    ),
                                    debits AS (
                                      SELECT tio.address,
                                             SUM(tio.amount)::bigint AS debit_sats,
                                             COUNT(*)::int           AS debit_utxos
                                        FROM blockchain.spends s
                                        JOIN blockchain.transaction_io tio
                                          ON tio.txid = s.prev_txid
                                         AND tio.idx  = s.prev_vout
                                         AND tio.io_type = 'out'::blockchain.tx_io_type
                                       WHERE s.spent_height = %s
                                         AND EXISTS (SELECT 1 FROM blockchain.watch_addresses wa WHERE wa.address = tio.address)
                                       GROUP BY tio.address
                                    ),
                                    addr_events AS (
                                      SELECT i.address, i.txid
                                        FROM blockchain.transaction_io i
                                        JOIN _block_txs bt ON bt.txid = i.txid
                                       WHERE i.io_type = 'out'::blockchain.tx_io_type
                                         AND EXISTS (SELECT 1 FROM blockchain.watch_addresses wa WHERE wa.address = i.address)
                                      UNION ALL
                                      SELECT tio.address, s.spending_txid AS txid
                                        FROM blockchain.spends s
                                        JOIN blockchain.transaction_io tio
                                          ON tio.txid = s.prev_txid
                                         AND tio.idx  = s.prev_vout
                                         AND tio.io_type = 'out'::blockchain.tx_io_type
                                       WHERE s.spent_height = %s
                                         AND EXISTS (SELECT 1 FROM blockchain.watch_addresses wa WHERE wa.address = tio.address)
                                    ),
                                    per_addr AS (
                                      SELECT
                                        a.address,
                                        COALESCE(c.credit_sats,0)  AS credit_sats,
                                        COALESCE(d.debit_sats,0)   AS debit_sats,
                                        COALESCE(c.credit_utxos,0) AS credit_utxos,
                                        COALESCE(d.debit_utxos,0)  AS debit_utxos,
                                        COUNT(DISTINCT e.txid)::int AS delta_txs,
                                        MIN(e.txid)                 AS rep_txid
                                      FROM (SELECT address FROM credits
                                            UNION
                                            SELECT address FROM debits) a
                                      LEFT JOIN credits c ON c.address = a.address
                                      LEFT JOIN debits  d ON d.address = a.address
                                      LEFT JOIN addr_events e ON e.address = a.address
                                      GROUP BY a.address, c.credit_sats, d.debit_sats, c.credit_utxos, d.debit_utxos
                                    ),
                                    locked AS (
                                      SELECT wa.address
                                        FROM blockchain.watch_addresses wa
                                        JOIN per_addr p ON p.address = wa.address
                                       ORDER BY wa.address
                                       FOR UPDATE
                                    )
                                    UPDATE blockchain.watch_addresses wa
                                       SET balance_sats        = GREATEST(0, wa.balance_sats + (p.credit_sats - p.debit_sats)),
                                           utxo_count          = GREATEST(0, wa.utxo_count   + (p.credit_utxos - p.debit_utxos)),
                                           tx_count            = wa.tx_count + p.delta_txs,
                                           first_seen          = CASE
                                                                   WHEN wa.first_seen IS NULL THEN %s
                                                                   ELSE LEAST(wa.first_seen, %s)
                                                                 END,
                                           last_seen           = CASE
                                                                   WHEN wa.last_seen IS NULL THEN %s
                                                                   ELSE GREATEST(wa.last_seen, %s)
                                                                 END,
                                           last_scanned_height = GREATEST(COALESCE(wa.last_scanned_height,0), %s),
                                           last_scanned_time   = CASE
                                                                   WHEN wa.last_scanned_time IS NULL THEN %s
                                                                   ELSE GREATEST(wa.last_scanned_time, %s)
                                                                 END,
                                           last_scanned_txid   = COALESCE(p.rep_txid, wa.last_scanned_txid),
                                           updated_at          = now()
                                      FROM per_addr p
                                      JOIN locked l ON l.address = p.address
                                     WHERE wa.address = p.address;
                                """, (
                                    height,         # debits.s.spent_height
                                    height,         # addr_events.s.spent_height
                                    block_ts, block_ts,  # first_seen
                                    block_ts, block_ts,  # last_seen
                                    height,              # last_scanned_height
                                    block_ts, block_ts   # last_scanned_time
                                ))
                                touched = cur.rowcount  # number of rows updated

                                # status flip to 'done' is part of same transaction for idempotency
                                await mark_address_done(conn, height)

                                return touched

                    touched = await asyncio.shield(_write())

        # Only after successful commit, update process-local counters/metrics
        local["done"] += 1
        local["in_progress"] = max(0, local["in_progress"] - 1)
        local["heights_done"] += 1
        if height > local["latest_done_height"]:
            local["latest_done_height"] = height
        local["touched_addresses"] += touched

        log.info("addr_height_done", height=height, touched_addresses=touched, block_hash=block_hash, block_time=str(block_ts))
        return touched

    except asyncio.CancelledError:
        # make sure in_progress accounting is corrected by caller if needed
        raise
    except Exception as e:
        log.error("addr_height_failed", height=height, error=str(e), traceback=traceback.format_exc())
        # No partial effects from the write TX: it rolled back on exception.
        await mark_address_failed(height)
        return 0

# === Workers / Monitor ===
async def worker_loop(session: aiohttp.ClientSession):
    while not shutdown_event.is_set():
        heights = await get_next_address_jobs(config.job_batch_size)
        if not heights:
            await asyncio.sleep(2)
            continue
        log.info("addr_worker_starting_jobs", heights=heights)
        tasks = [apply_address_deltas_for_height(session, h) for h in heights]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            # Graceful shutdown: stop starting new work
            break

async def monitor_loop(session: aiohttp.ClientSession):
    start_time = asyncio.get_event_loop().time()
    base_processed = local["done"] + local["failed"]
    while not shutdown_event.is_set():
        try:
            done, in_progress, pending, failed, latest_done_height, processed = current_progress()

            elapsed = asyncio.get_event_loop().time() - start_time
            delta_processed = processed - base_processed
            rate = (delta_processed / elapsed) if elapsed > 2 else 0.0
            if rate > 0 and pending > 0:
                eta = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=(pending / rate))
                eta_str = eta.strftime("%Y-%m-%d %H:%M:%S")
            else:
                eta_str = "calculating..."

            log.info("progress", line=1,
                     done=done, in_progress=in_progress, pending=pending, failed=failed, eta=eta_str)
            log.info("progress", line=2,
                     latest_processed_block=latest_done_height,
                     touched_addresses=local["touched_addresses"],
                     heights_done=local["heights_done"],
                     errors=local["errors"])

            # stop when we've drained the queue visible to this process
            if pending == 0 and in_progress == 0:
                log.info("addr_all_blocks_processed", metrics=local)
                shutdown_event.set()
        except Exception as e:
            log.error("addr_monitor_error", error=str(e))
            local["errors"] += 1
        await asyncio.sleep(10)  # cheaper monitor now

# === Main ===
def handle_shutdown():
    asyncio.get_running_loop().call_soon_threadsafe(shutdown_event.set)

async def main():
    global pool, baseline
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, lambda s, f: handle_shutdown())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda s, f: handle_shutdown())

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

            await reset_abandoned_addr_jobs()
            baseline = await load_baseline()
            local["latest_done_height"] = baseline.latest_done0

            log.info("starting_addr_workers",
                     num_workers=config.num_workers,
                     rpc_concurrency=RPC_CONCURRENCY,
                     db_max_conn=DB_MAX_CONN,
                     db_write_conc=DB_WRITE_CONCURRENCY,
                     job_batch_size=config.job_batch_size,
                     baseline_total=baseline.total_jobs,
                     baseline_done=baseline.done0,
                     baseline_failed=baseline.failed0,
                     baseline_latest_done=baseline.latest_done0)

            start_delay = float(os.getenv("START_DELAY", "0"))
            if start_delay > 0:
                log.info("starting_in", seconds=start_delay)
                await asyncio.sleep(start_delay)

            tasks = [worker_loop(session) for _ in range(config.num_workers)]
            tasks.append(monitor_loop(session))
            await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


