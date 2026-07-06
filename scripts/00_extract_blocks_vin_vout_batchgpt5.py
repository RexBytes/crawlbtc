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
from typing import Optional, List, Tuple
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
            else:
                # Drop all non-progress events in progress mode
                raise structlog.DropEvent
        return event_dict

structlog.configure(
    processors=[
        TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        ProgressOnlyFilter(),
        JSONRenderer()
    ]
)

log = structlog.get_logger("block-extractor")

TXID_RE = re.compile(r'^[0-9a-fA-F]{64}$')
HEX64_RE = TXID_RE  # alias for clarity when used for block hashes
SATS_PER_BTC = Decimal(100_000_000)  # correct multiplier

# ---- scaling helpers ----
def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def derive_runtime_limits(num_workers: int) -> dict:
    rpc_conc = clamp(num_workers * 2, 10, 128)
    # was 0.30x; bump to ~0.6x to match your observed pressure
    db_max_conn = clamp(math.ceil(num_workers * 0.60), 8, 128)
    # write concurrency ~50% of pool, leave at least a few read slots
    db_write_conc = clamp(math.floor(db_max_conn * 0.50), 4, max(4, db_max_conn - 3))
    job_batch_size = clamp(math.ceil(200 / max(1, num_workers)), 3, 12)
    return {
        "RPC_CONCURRENCY": rpc_conc,
        "DB_MAX_CONN": db_max_conn,
        "DB_WRITE_CONCURRENCY": db_write_conc,
        "JOB_BATCH_SIZE": 1,
    }

def probe_server_capacity(conninfo: str) -> tuple[int, int]:
    # short, synchronous probe before the async pool exists
    try:
        with psycopg.connect(conninfo, autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute("SHOW max_connections;")
                max_conn = int(cur.fetchone()[0])
                cur.execute("SHOW superuser_reserved_connections;")
                reserved = int(cur.fetchone()[0])
        return max_conn, reserved
    except Exception as e:
        log.warning("server_capacity_probe_failed", error=str(e))
        return 100, 3  # safe-ish fallback



# === Configuration ===
@dataclass
class Config:
    rpc_url: str
    rpc_auth: aiohttp.BasicAuth
    db_conninfo: str
    num_workers: int
    job_batch_size: int  # derived per NUM_WORKERS

# --- Auto sizing helpers ---
def _read_total_mem_gb() -> int:
    """Best-effort total RAM in GiB without extra deps."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return max(1, kb // (1024 * 1024))
    except Exception:
        pass
    return 0  # unknown

def auto_num_workers(power_pct: int = 100) -> int:
    cores = os.cpu_count() or 1
    ram_gb = _read_total_mem_gb()
    # Baseline: ~2.0× logical cores, cap higher
    base = max(8, min(int(round(cores * 2.0)), 96))
    if ram_gb:
        # keep a RAM cap; you’ve got 128 GiB so this won’t bite
        ram_cap = max(8, min(96, (ram_gb * 5) // 2))  # ≈ 2 GB per 5 workers heuristic
        base = min(base, ram_cap)
    power_pct = max(10, min(200, power_pct))
    return max(1, (base * power_pct) // 100)



def load_config() -> Config:
    load_dotenv(dotenv_path=Path(__file__).resolve().parent.parent / ".env")

    power = int(os.getenv("POWER", "100"))
    auto_workers = auto_num_workers(power)
    num_workers = int(os.getenv("NUM_WORKERS", str(auto_workers)))

    # Build conninfo early so we can probe the server
    db_conninfo = (
        f"host={os.getenv('PG_HOST')} port={os.getenv('PG_PORT')} "
        f"dbname={os.getenv('PG_DB')} user={os.getenv('PG_USER')} password={os.getenv('PG_PASSWORD')}"
    )

    # Derive defaults from workers
    auto = derive_runtime_limits(num_workers)

    # Probe server caps
    svr_max, svr_reserved = probe_server_capacity(db_conninfo)
    usable_server = max(1, svr_max - svr_reserved)

    # If envs are not set, autosize pool within a % of server capacity
    # Aim to consume at most 70% of usable_server (leave headroom for psql, autovac, other apps)
    target_budget = max(8, int(usable_server * 0.70))

    if "DB_MAX_CONN" in os.environ:
        db_max_conn = int(os.getenv("DB_MAX_CONN"))
    else:
        db_max_conn = min(auto["DB_MAX_CONN"], target_budget)

    # write concurrency <= pool-3 and not above half of pool
    if "DB_WRITE_CONCURRENCY" in os.environ:
        db_write_conc = int(os.getenv("DB_WRITE_CONCURRENCY"))
    else:
        db_write_conc = clamp(math.floor(db_max_conn * 0.50), 4, max(4, db_max_conn - 3))

    # rpc + batch size (env can still override)
    rpc_conc = int(os.getenv("RPC_CONCURRENCY", str(auto["RPC_CONCURRENCY"])))

    # Export for the rest of the module (only set if not already)
    os.environ.setdefault("RPC_CONCURRENCY", str(rpc_conc))
    os.environ.setdefault("DB_MAX_CONN", str(db_max_conn))
    os.environ.setdefault("DB_WRITE_CONCURRENCY", str(db_write_conc))
    job_batch_size = int(os.getenv("JOB_BATCH_SIZE", "1"))  # default 1

    # Warn if we’re being too aggressive
    if db_max_conn > target_budget:
        log.warning("pool_may_overcrowd_server",
                    db_max_conn=db_max_conn,
                    target_budget=target_budget,
                    server_max=svr_max, reserved=svr_reserved)

    log.info("autosizing_plan",
             cores=os.cpu_count() or 1,
             ram_gb=_read_total_mem_gb(),
             power_pct=power,
             chosen_workers=num_workers,
             rpc_concurrency=rpc_conc,
             db_max_conn=db_max_conn,
             db_write_conc=db_write_conc,
             job_batch_size=job_batch_size,
             server_max_connections=svr_max,
             server_reserved_connections=svr_reserved)

    return Config(
        rpc_url=f"http://{os.getenv('RPC_HOST')}:{os.getenv('RPC_PORT')}",
        rpc_auth=aiohttp.BasicAuth(os.getenv("RPC_USER"), os.getenv("RPC_PASSWORD")),
        db_conninfo=db_conninfo,
        num_workers=num_workers,
        job_batch_size=job_batch_size
    )


config = load_config()

# Global concurrency knobs (derived from NUM_WORKERS unless overridden)
RPC_CONCURRENCY = int(os.environ["RPC_CONCURRENCY"])
DB_MAX_CONN = int(os.environ["DB_MAX_CONN"])
DB_WRITE_CONCURRENCY = int(os.environ["DB_WRITE_CONCURRENCY"])

# Global throttles
rpc_semaphore = asyncio.BoundedSemaphore(RPC_CONCURRENCY)
db_write_sem = asyncio.BoundedSemaphore(DB_WRITE_CONCURRENCY)

pool: Optional[AsyncConnectionPool] = None
shutdown_event = asyncio.Event()

metrics = {"blocks_processed": 0, "errors": 0, "skipped": 0}

# === Utility Functions ===
def get_address_type(address: Optional[str]) -> Optional[str]:
    if not address or not isinstance(address, str):
        return None
    if address.startswith("1"):
        return "p2pkh"
    elif address.startswith("3"):
        return "p2sh"
    elif address.startswith("bc1p"):
        return "taproot"
    elif address.startswith("bc1q"):
        return "bech32"
    return "unknown"

def validate_txid(txid: str) -> bool:
    return isinstance(txid, str) and TXID_RE.match(txid) is not None

def to_sats(value) -> int:
    """
    Robust BTC -> sats conversion:
    - accepts int/float/str
    - floors (ROUND_DOWN)
    - rejects non-finite values (NaN/Inf)
    """
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError("non-finite value")
    if d <= 0:
        return 0
    return int((d * SATS_PER_BTC).to_integral_value(rounding=ROUND_DOWN))

async def rpc_call(session: aiohttp.ClientSession, method: str, params: Optional[List] = None, timeout: float = 30.0) -> dict:
    async with rpc_semaphore:  # global throttle for all RPC calls
        try:
            async with session.post(
                config.rpc_url,
                json={"jsonrpc": "1.0", "id": "extractor", "method": method, "params": params or []},
                auth=config.rpc_auth,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                if data.get("error"):
                    raise ValueError(f"RPC error: {data['error']}")
                return data["result"]
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise RuntimeError(f"RPC call '{method}' failed: {str(e)}")

async def fetch_blocks(session: aiohttp.ClientSession, heights: List[int]) -> List[Tuple[int, Optional[dict]]]:
    async def fetch_single_block(height: int) -> Tuple[int, Optional[dict]]:
        try:
            block_hash = await with_retry(rpc_call, session, "getblockhash", [height])
            block = await with_retry(rpc_call, session, "getblock", [block_hash, 2], timeout=90.0)
            return height, block
        except Exception as e:
            log.error("fetch_block_failed", height=height, error=str(e))
            return height, None

    tasks = [fetch_single_block(h) for h in heights]
    return await asyncio.gather(*tasks, return_exceptions=False)

async def with_retry(
    coro_func, *args, retries=3, delay=1.0, backoff=2.0, jitter=0.1,
    exceptions=(aiohttp.ClientError, psycopg.Error, ValueError, RuntimeError),
    **kwargs,  # <-- add this
):
    last_exception = None
    for attempt in range(retries):
        try:
            return await coro_func(*args, **kwargs)  # <-- forward kwargs
        except exceptions as e:
            last_exception = e
            if attempt < retries - 1:
                sleep_time = delay * (backoff ** attempt) + random.uniform(0, jitter)
                log.warning("retry", func=coro_func.__name__, attempt=attempt + 1, retries=retries, error=str(e), sleep=sleep_time)
                await asyncio.sleep(sleep_time)
            else:
                log.error("retry_exhausted", func=coro_func.__name__, error=str(last_exception))
                metrics["errors"] += 1
                raise last_exception
        except asyncio.CancelledError:
            raise


async def check_rpc_health(session: aiohttp.ClientSession) -> bool:
    try:
        block_count = await rpc_call(session, "getblockcount")
        log.info("rpc_healthy", block_count=block_count)
        return True
    except Exception as e:
        log.error("rpc_unreachable", error=str(e))
        return False

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

def classify_block_features(txio_rows: List[Tuple], transactions: List[dict]) -> str:
    has_vout = bool(txio_rows)
    has_vin = any(
        isinstance(tx.get("vin"), list) and any("txid" in vin for vin in tx["vin"])
        for tx in transactions
    )
    is_coinbase_only = (
        all(
            isinstance(tx.get("vin"), list)
            and len(tx["vin"]) == 1
            and "coinbase" in tx["vin"][0]
            for tx in transactions
        ) if transactions else False
    )

    def any_spendable_output():
        for tx in transactions:
            for vout in tx.get("vout", []):
                spk = vout.get("scriptPubKey", {})
                asm = spk.get("asm", "")
                if isinstance(asm, str) and asm.startswith("OP_RETURN"):
                    continue
                if vout.get("value", 0) > 0:
                    return True
        return False

    has_spendable_output = any_spendable_output()
    has_only_opreturn = has_spendable_output and not has_vout

    if has_vin and has_vout:
        return "both"
    elif has_vin:
        return "vin"
    elif has_vout:
        return "vout"
    elif has_only_opreturn:
        return "op_return_only"
    elif is_coinbase_only and not has_vout:
        return "coinbase_only"
    else:
        return "none"

# === Database Functions ===
async def reset_abandoned_jobs():
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                UPDATE blockchain.block_jobs
                   SET vout_status = 'pending'::blockchain.block_job_status,
                       updated_at  = now()
                 WHERE vout_status = 'in_progress'::blockchain.block_job_status;
            """)
            await conn.commit()


async def get_next_jobs(batch_size: int) -> List[int]:
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE blockchain.block_jobs
                       SET vout_status = 'in_progress'::blockchain.block_job_status,
                           updated_at  = now()
                     WHERE height IN (
                         SELECT height
                           FROM blockchain.block_jobs
                          WHERE vout_status = 'pending'::blockchain.block_job_status
                          ORDER BY height
                          LIMIT %s
                          FOR UPDATE SKIP LOCKED
                     )
                 RETURNING height;
                """, (batch_size,))
                rows = await cur.fetchall()
                return [row[0] for row in rows]


async def mark_job_done(conn, height: int, features: str):
    async with conn.cursor() as cur:
        await cur.execute("""
            UPDATE blockchain.block_jobs
               SET vout_status = 'done'::blockchain.block_job_status,
                   features    = %s::blockchain.block_feature,
                   updated_at  = now()
             WHERE height = %s;
        """, (features, height))

async def mark_block_skipped(height: int):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                UPDATE blockchain.block_jobs
                   SET vout_status = 'skipped'::blockchain.block_job_status,
                       updated_at  = now()
                 WHERE height = %s;
            """, (height,))
    metrics["skipped"] += 1

async def mark_block_failed(height: int):
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                UPDATE blockchain.block_jobs
                   SET vout_status = 'failed'::blockchain.block_job_status,
                       updated_at  = now()
                 WHERE height = %s;
            """, (height,))
    metrics["errors"] += 1


async def get_progress() -> Tuple[int, int, int, int, int]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE vout_status = 'done'::blockchain.block_job_status)        AS done,
                    COUNT(*) FILTER (WHERE vout_status = 'in_progress'::blockchain.block_job_status) AS in_progress,
                    COUNT(*) FILTER (WHERE vout_status = 'pending'::blockchain.block_job_status)     AS pending,
                    COUNT(*) FILTER (WHERE vout_status = 'skipped'::blockchain.block_job_status)     AS skipped,
                    COALESCE(
                      MAX(height) FILTER (
                        WHERE vout_status IN ('done'::blockchain.block_job_status,
                                              'skipped'::blockchain.block_job_status)
                      ), 0
                    ) AS latest_processed_block
                FROM blockchain.block_jobs;
            """)
            return await cur.fetchone()


# ---- COPY-based fast ingestion (idempotent via staging, cast text->enum on insert)
async def insert_tx_rows(conn, tx_rows: List[Tuple]) -> None:
    if not tx_rows:
        return

    # Build TEXT COPY payload (tab-delimited, newline-terminated)
    buf = StringIO()
    for txid, block_hash, received_time, total_in, total_out in tx_rows:
        # Use timezone-aware UTC (py3.12 deprecation fix)
        ts = datetime.datetime.fromtimestamp(received_time, datetime.UTC).isoformat()
        buf.write(f"{txid}\t{block_hash}\t{ts}\t{total_in}\t{total_out}\n")
    payload = buf.getvalue()

    
    try:
        async with conn.cursor() as cur:
            # temp staging table lives for the transaction; TRUNCATE to be safe if reused
            await cur.execute("""
                CREATE TEMP TABLE IF NOT EXISTS _tx_stage
                (txid text, block_hash text, received_time timestamp, total_in bigint, total_out bigint)
                ON COMMIT DROP;
                TRUNCATE _tx_stage;
            """)
             # COPY FROM STDIN (text) via async context manager
            async with cur.copy("""
                COPY _tx_stage (txid, block_hash, received_time, total_in, total_out)
                FROM STDIN WITH (FORMAT text)
            """) as copy:
                await copy.write(payload)
             # Upsert into real table
            await cur.execute("""
                INSERT INTO blockchain.transactions (txid, block_hash, received_time, total_in, total_out)
                SELECT txid, block_hash, received_time, total_in, total_out
                FROM _tx_stage
                ON CONFLICT (txid) DO NOTHING;
            """)
        log.info("inserted_tx_rows", count=len(tx_rows))
    except Exception as e:
        log.error("insert_tx_rows_failed", error=str(e))
        raise


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

    # Build TEXT COPY payload
    buf = StringIO()
    for txid, address, address_type, amount, io_type, idx in txio_rows:
        buf.write(f"{txid}\t{address}\t{address_type}\t{amount}\t{io_type}\t{idx}\n")
    payload = buf.getvalue()


    try:
        async with conn.cursor() as cur:
            await cur.execute("""
                CREATE TEMP TABLE IF NOT EXISTS _txio_stage
                (
                    txid text,
                    address text,
                    address_type text,
                    amount bigint,
                    io_type text,
                    idx int
                )
                ON COMMIT DROP;
                TRUNCATE _txio_stage;
            """)

            async with cur.copy("""
                COPY _txio_stage (txid, address, address_type, amount, io_type, idx)
                FROM STDIN WITH (FORMAT text)
            """) as copy:
                await copy.write(payload)

            # Cast to enums on insert
            await cur.execute("""
                INSERT INTO blockchain.transaction_io (txid, address, address_type, amount, io_type, idx)
                SELECT
                    txid,
                    address,
                    address_type::blockchain.address_type,
                    amount,
                    io_type::blockchain.tx_io_type,
                    idx
                FROM _txio_stage
                ON CONFLICT (txid, io_type, idx) DO NOTHING;
            """)
        log.info("inserted_txio_rows", count=len(txio_rows))
    except Exception as e:
        log.error("insert_txio_rows_failed", error=str(e))
        raise


# === Block Processor ===
async def process_block(session: aiohttp.ClientSession, height: int, block: Optional[dict] = None) -> None:
    try:
        start = time.time()

        # --- RPC + parsing phase (no DB connection held) ---
        if block is None:
            block_hash = await with_retry(rpc_call, session, "getblockhash", [height])
            block = await with_retry(rpc_call, session, "getblock", [block_hash, 2], timeout=90.0)
        else:
            block_hash = block.get("hash")
            if not isinstance(block_hash, str) or HEX64_RE.match(block_hash) is None:
                block_hash = await with_retry(rpc_call, session, "getblockhash", [height])

        block_time = block.get("time")
        if not isinstance(block_time, int):
            raise ValueError(f"Invalid block time for height {height}")

        transactions = block.get("tx", [])
        tx_rows: List[Tuple] = []
        txio_rows: List[Tuple] = []

        for tx in transactions:
            txid = tx.get("txid")
            if not validate_txid(txid):
                log.warning("invalid_txid", height=height, txid=txid)
                continue

            vout_total = 0
            for idx, vout in enumerate(tx.get("vout", [])):
                value = vout.get("value")
                if not isinstance(value, (int, float, str)):
                    continue

                spk = vout.get("scriptPubKey", {})
                asm = spk.get("asm", "")
                if isinstance(asm, str) and asm.startswith("OP_RETURN"):
                    continue

                address = spk.get("address") or (spk.get("addresses") or [None])[0]
                if not isinstance(address, str):
                    continue

                address_type = normalize_address_type(spk.get("type"), address)
                try:
                    satoshis = to_sats(value)
                except Exception as conv_err:
                    log.warning("bad_vout_value", height=height, txid=txid, value=value, error=str(conv_err))
                    continue
                if satoshis <= 0:
                    continue

                vout_total += satoshis
                txio_rows.append((txid, address, address_type, satoshis, 'out', idx))

            tx_rows.append((txid, block_hash, block_time, 0, vout_total))

        features = classify_block_features(txio_rows, transactions)
        if features == "none":
            log.warning("no_valid_txio", height=height)
            await mark_block_skipped(height)
            return

        # --- DB phase (short, with connection held) ---
        async with db_write_sem:
            async with pool.connection() as conn:
                async def _write():
                    async with conn.transaction():
                        await insert_tx_rows(conn, tx_rows)
                        await insert_txio_rows(conn, txio_rows)
                        await mark_job_done(conn, height, features)

                # Ensure this DB work isn't cancelled mid-flight
                await asyncio.shield(_write())

        metrics["blocks_processed"] += 1
        log.info("block_processed", height=height, tx_count=len(tx_rows), duration=time.time() - start)

    except asyncio.CancelledError:
        # let shutdown cancel without flipping the job to failed
        raise

    except Exception as e:
        log.error("block_failed", height=height, error=str(e), traceback=traceback.format_exc())
        await mark_block_failed(height)
        raise


# === Workers ===
async def worker_loop(session: aiohttp.ClientSession):
    while not shutdown_event.is_set():
        heights = await get_next_jobs(config.job_batch_size)
        if not heights:
            await asyncio.sleep(2)
            continue

        log.info("worker_starting_jobs", heights=heights)

        # No batch semaphore here; RPC throttle happens inside rpc_call
        results = await fetch_blocks(session, heights)

        for height, block in results:
            if block is None:
                await with_retry(mark_block_failed, height)
                continue
            await process_block(session, height, block)


async def monitor_loop(session: aiohttp.ClientSession):
    start_done = None
    start_time = asyncio.get_event_loop().time()
    cached_block_count = None
    cache_expiry = 0

    while not shutdown_event.is_set():
        try:
            done, in_progress, pending, skipped, latest_processed_block = await get_progress()
            now = datetime.datetime.now()
            total_complete = done + skipped

            if start_done is None and total_complete > 0:
                start_done = total_complete
                start_time = asyncio.get_event_loop().time()

            eta_str = "calculating..."
            if start_done is not None and total_complete > start_done:
                rate = (total_complete - start_done) / (asyncio.get_event_loop().time() - start_time)
                eta = now + datetime.timedelta(seconds=(pending / rate)) if rate > 0 else now
                eta_str = eta.strftime("%Y-%m-%d %H:%M:%S")

            #log.info("progress", done=done, in_progress=in_progress, pending=pending, skipped=skipped,
            #         eta=eta_str,latest_processed_block=latest_processed_block, blocks_processed=metrics["blocks_processed"], errors=metrics["errors"])
            tip_height = cached_block_count or latest_processed_block
            lag = max(0, (tip_height or 0) - (latest_processed_block or 0))

            log.info(
                "progress",
                line=1,
                done=done,
                in_progress=in_progress,
                pending=pending,
                skipped=skipped,
                eta=eta_str,
            )
            log.info(
                "progress",
                line=2,
                latest_processed_block=latest_processed_block,
                tip_height=tip_height,
                lag=lag,
                blocks_processed=metrics["blocks_processed"],
                errors=metrics["errors"],
            )

            if asyncio.get_event_loop().time() > cache_expiry:
                cached_block_count = await with_retry(rpc_call, session, "getblockcount", timeout=20.0)
                cache_expiry = asyncio.get_event_loop().time() + 60

            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT MAX(height) FROM blockchain.block_jobs;")
                    row = await cur.fetchone()
                    latest_job = row[0] if row and row[0] else 0
                    if cached_block_count and cached_block_count > latest_job:
                        await cur.execute("""
                            INSERT INTO blockchain.block_jobs (height, vout_status, vin_status)
                            SELECT g, 'pending'::blockchain.block_job_status, 'pending'::blockchain.block_job_status
                            FROM generate_series(%s, %s) g
                            ON CONFLICT (height) DO NOTHING;
                        """, (latest_job + 1, cached_block_count))
                        await conn.commit()

            if pending == 0 and in_progress == 0:
                log.info("all_blocks_processed", metrics=metrics)
                shutdown_event.set()

        except Exception as e:
            log.error("monitor_error", error=str(e))
            metrics["errors"] += 1

        await asyncio.sleep(5)

# === Main Entrypoint ===
def handle_shutdown():
    asyncio.get_running_loop().call_soon_threadsafe(shutdown_event.set)

async def main():
    global pool
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, lambda s, f: handle_shutdown())

    # Align HTTP connector capacity with RPC throttle
    connector = aiohttp.TCPConnector(
    limit=RPC_CONCURRENCY,
    limit_per_host=RPC_CONCURRENCY 
    )


    async with AsyncConnectionPool(
        config.db_conninfo,
        min_size=max(1, min(DB_MAX_CONN // 2, config.num_workers)),
        max_size=DB_MAX_CONN,
    timeout=float(os.getenv("DB_POOL_TIMEOUT", "60")),  # was implicit 30s
    ) as p:
        pool = p

        async with aiohttp.ClientSession(connector=connector) as session:
            # ✅ Check Bitcoin Core is available
            if not await check_rpc_health(session):
                log.critical("bitcoin_core_unavailable", message="Bitcoin Core RPC is not responding. Exiting.")
                print("⚠️  Bitcoin Core is not responding. Exiting now.")
                sys.exit(1)

            # Genesis block only mode (ensure a job exists if relying on jobs table)
            if len(os.sys.argv) > 1 and os.sys.argv[1] == "genesis":
                await process_block(session, 0)
                return


            await reset_abandoned_jobs()
            log.info("starting_workers",
                     num_workers=config.num_workers,
                     rpc_concurrency=RPC_CONCURRENCY,
                     db_max_conn=DB_MAX_CONN,
                     db_write_conc=DB_WRITE_CONCURRENCY,
                     job_batch_size=config.job_batch_size)

            # ⏳ Optional pause before launching workers
            start_delay = float(os.getenv("START_DELAY", "0"))
            if start_delay > 0:
                log.info("starting_in", seconds=start_delay)
                await asyncio.sleep(start_delay)

            tasks = [worker_loop(session) for _ in range(config.num_workers)]
            tasks.append(monitor_loop(session))
            await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())

