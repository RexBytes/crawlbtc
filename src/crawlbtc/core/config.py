"""Configuration: same environment variables as the legacy scripts.

RPC_HOST/RPC_PORT/RPC_USER/RPC_PASSWORD, PG_HOST/PG_PORT/PG_DB/PG_USER/
PG_PASSWORD, plus the tuning knobs POWER, NUM_WORKERS, RPC_CONCURRENCY,
DB_MAX_CONN, DB_WRITE_CONCURRENCY, JOB_BATCH_SIZE, DB_POOL_TIMEOUT,
START_DELAY, LOG_LEVEL. New: PROCESSES (parallel OS processes per phase).

No import-time side effects: load_config() is called explicitly by the CLI.
"""

import math
import os
from dataclasses import dataclass
from typing import Optional

import psycopg
from dotenv import find_dotenv, load_dotenv


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _read_total_mem_gb() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return max(1, kb // (1024 * 1024))
    except Exception:
        pass
    return 0


def auto_num_workers(power_pct: int = 100) -> int:
    cores = os.cpu_count() or 1
    ram_gb = _read_total_mem_gb()
    base = max(8, min(int(round(cores * 2.0)), 96))
    if ram_gb:
        ram_cap = max(8, min(96, (ram_gb * 5) // 2))
        base = min(base, ram_cap)
    power_pct = clamp(power_pct, 10, 200)
    return max(1, (base * power_pct) // 100)


@dataclass
class Config:
    rpc_url: str
    rpc_user: str
    rpc_password: str
    db_conninfo: str
    num_workers: int          # async workers per process
    processes: int            # OS processes (JSON parsing parallelism)
    rpc_concurrency: int      # per process
    db_max_conn: int          # per process
    db_write_concurrency: int  # per process
    job_batch_size: int
    db_pool_timeout: float
    start_delay: float
    synchronous_commit: str = "off"


def probe_server_capacity(conninfo: str) -> tuple[int, int]:
    try:
        with psycopg.connect(conninfo, autocommit=True, connect_timeout=10) as c:
            with c.cursor() as cur:
                cur.execute("SHOW max_connections;")
                max_conn = int(cur.fetchone()[0])
                cur.execute("SHOW superuser_reserved_connections;")
                reserved = int(cur.fetchone()[0])
        return max_conn, reserved
    except Exception:
        return 100, 3  # safe-ish fallback


def resolve_env_file(env_file: Optional[str] = None) -> Optional[str]:
    """Return the absolute path of the env file load_config() would use,
    or None if it would fall back to the process environment only."""
    env_file = env_file or os.getenv("CRAWLBTC_ENV_FILE")
    if env_file:
        return os.path.abspath(os.path.expanduser(env_file))
    found = find_dotenv(usecwd=True)
    return found or None


def load_config(
    processes: Optional[int] = None,
    workers: Optional[int] = None,
    batch_size: Optional[int] = None,
    probe_db: bool = True,
    env_file: Optional[str] = None,
) -> Config:
    # Precedence: --env-file flag, then CRAWLBTC_ENV_FILE, then the nearest
    # .env found from the current directory upward. Real environment
    # variables always win over file values.
    env_file = env_file or os.getenv("CRAWLBTC_ENV_FILE")
    if env_file:
        path = os.path.expanduser(env_file)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"env file not found: {path}")
        load_dotenv(path)
    else:
        load_dotenv(find_dotenv(usecwd=True))

    power = int(os.getenv("POWER", "100"))
    total_workers = workers if workers is not None else int(os.getenv("NUM_WORKERS", str(auto_num_workers(power))))

    if processes is None:
        processes = int(os.getenv("PROCESSES", "0"))
    if processes <= 0:
        # Default: enough processes that JSON parsing isn't single-core bound,
        # without swamping the machine.
        processes = clamp((os.cpu_count() or 2) // 2, 1, 8)

    num_workers = max(1, math.ceil(total_workers / processes))

    db_conninfo = (
        f"host={os.getenv('PG_HOST')} port={os.getenv('PG_PORT')} "
        f"dbname={os.getenv('PG_DB')} user={os.getenv('PG_USER')} password={os.getenv('PG_PASSWORD')}"
    )

    # Derive per-process limits from the total worker budget, then split.
    total_rpc = clamp(total_workers * 2, 10, 128)
    total_db = clamp(math.ceil(total_workers * 0.60), 8, 128)

    if probe_db:
        svr_max, svr_reserved = probe_server_capacity(db_conninfo)
        usable = max(1, svr_max - svr_reserved)
        total_db = min(total_db, max(8, int(usable * 0.70)))

    rpc_concurrency = int(os.getenv("RPC_CONCURRENCY", str(total_rpc)))
    db_max_conn = int(os.getenv("DB_MAX_CONN", str(total_db)))

    per_proc_rpc = max(2, math.ceil(rpc_concurrency / processes))
    per_proc_db = max(2, math.ceil(db_max_conn / processes))
    if "DB_WRITE_CONCURRENCY" in os.environ:
        per_proc_db_write = max(1, math.ceil(int(os.environ["DB_WRITE_CONCURRENCY"]) / processes))
    else:
        per_proc_db_write = clamp(math.floor(per_proc_db * 0.50), 1, max(1, per_proc_db - 1))

    # The pool must hold every writer PLUS a spare connection for the
    # monitor loop (progress reads + block top-up); otherwise workers grab
    # all connections for slow writes and the monitor starves - it stops
    # printing and stops discovering new blocks. Grow the pool to guarantee
    # that headroom regardless of how the env knobs were split.
    per_proc_db = max(per_proc_db, per_proc_db_write + 2)

    return Config(
        rpc_url=f"http://{os.getenv('RPC_HOST')}:{os.getenv('RPC_PORT')}",
        rpc_user=os.getenv("RPC_USER", ""),
        rpc_password=os.getenv("RPC_PASSWORD", ""),
        db_conninfo=db_conninfo,
        num_workers=num_workers,
        processes=processes,
        rpc_concurrency=per_proc_rpc,
        db_max_conn=per_proc_db,
        db_write_concurrency=max(1, per_proc_db_write),
        job_batch_size=batch_size if batch_size is not None else int(os.getenv("JOB_BATCH_SIZE", "1")),
        db_pool_timeout=float(os.getenv("DB_POOL_TIMEOUT", "60")),
        start_delay=float(os.getenv("START_DELAY", "0")),
        # Safe for this pipeline: job status commits atomically with the
        # data, so a crash that loses the tail commits just leaves those
        # blocks pending for reprocessing. Set to "on" to restore full
        # durability waits.
        synchronous_commit=os.getenv("PG_SYNCHRONOUS_COMMIT", "off"),
    )
