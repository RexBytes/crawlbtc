"""COPY-based bulk ingestion helpers (staging temp table -> upsert).

All writes are idempotent so any block can be reprocessed safely - that is
what makes `crawlbtc requeue` (e.g. to pick up Satoshi-era P2PK coins on
already-crawled blocks) a plain re-run instead of a migration.
"""

import datetime
from io import StringIO
from typing import Dict, List, Tuple

from .addresses import TXID_RE
from .logging import get_logger

log = get_logger("ingest")


async def _copy_stage(cur, create_sql: str, copy_sql: str, payload: str) -> None:
    await cur.execute(create_sql)
    async with cur.copy(copy_sql) as copy:
        await copy.write(payload)


async def insert_tx_rows(conn, tx_rows: List[Tuple]) -> None:
    """tx_rows: (txid, block_hash, block_time_epoch, total_in, total_out).

    On conflict the totals are refreshed (not ignored) so reprocessing a
    block corrects totals computed before P2PK support existed. total_in=0
    means "not computed here" and never clobbers an existing value.
    """
    if not tx_rows:
        return
    buf = StringIO()
    for txid, block_hash, received_time, total_in, total_out in tx_rows:
        ts = datetime.datetime.fromtimestamp(received_time, datetime.UTC).isoformat()
        buf.write(f"{txid}\t{block_hash}\t{ts}\t{total_in}\t{total_out}\n")

    async with conn.cursor() as cur:
        await _copy_stage(
            cur,
            """
            CREATE TEMP TABLE IF NOT EXISTS _tx_stage
            (txid text, block_hash text, received_time timestamp, total_in bigint, total_out bigint)
            ON COMMIT DROP;
            TRUNCATE _tx_stage;
            """,
            "COPY _tx_stage (txid, block_hash, received_time, total_in, total_out) FROM STDIN WITH (FORMAT text)",
            buf.getvalue(),
        )
        await cur.execute("""
            INSERT INTO blockchain.transactions AS t (txid, block_hash, received_time, total_in, total_out)
            SELECT txid, block_hash, received_time, total_in, total_out
            FROM _tx_stage
            ON CONFLICT (txid) DO UPDATE
               SET total_out = EXCLUDED.total_out,
                   total_in  = CASE WHEN COALESCE(EXCLUDED.total_in, 0) > 0
                                    THEN EXCLUDED.total_in
                                    ELSE t.total_in END
             WHERE t.total_out IS DISTINCT FROM EXCLUDED.total_out
                OR (COALESCE(EXCLUDED.total_in, 0) > 0
                    AND t.total_in IS DISTINCT FROM EXCLUDED.total_in);
        """)
    log.info("inserted_tx_rows", count=len(tx_rows))


def is_valid_txio_row(row: Tuple) -> bool:
    txid, address, address_type, amount, io_type, idx = row
    return (
        isinstance(txid, str) and TXID_RE.match(txid) is not None
        and isinstance(address, str)
        and isinstance(address_type, str)
        and isinstance(amount, int) and amount >= 0
        and io_type in ("in", "out")
        and isinstance(idx, int)
    )


async def insert_txio_rows(conn, txio_rows: List[Tuple]) -> None:
    """txio_rows: (txid, address, address_type, amount, io_type, idx)."""
    txio_rows = [row for row in txio_rows if is_valid_txio_row(row)]
    if not txio_rows:
        return
    buf = StringIO()
    for txid, address, address_type, amount, io_type, idx in txio_rows:
        buf.write(f"{txid}\t{address}\t{address_type}\t{amount}\t{io_type}\t{idx}\n")

    async with conn.cursor() as cur:
        await _copy_stage(
            cur,
            """
            CREATE TEMP TABLE IF NOT EXISTS _txio_stage
            (txid text, address text, address_type text, amount bigint, io_type text, idx int)
            ON COMMIT DROP;
            TRUNCATE _txio_stage;
            """,
            "COPY _txio_stage (txid, address, address_type, amount, io_type, idx) FROM STDIN WITH (FORMAT text)",
            buf.getvalue(),
        )
        await cur.execute("""
            INSERT INTO blockchain.transaction_io (txid, address, address_type, amount, io_type, idx)
            SELECT txid, address, address_type::blockchain.address_type, amount,
                   io_type::blockchain.tx_io_type, idx
            FROM _txio_stage
            ON CONFLICT (txid, io_type, idx) DO NOTHING;
        """)
    log.info("inserted_txio_rows", count=len(txio_rows))


async def insert_spends_rows(conn, spends_rows: List[Tuple]) -> None:
    """spends_rows: (prev_txid, prev_vout, spending_txid, spending_vin, height, block_hash, ts)."""
    if not spends_rows:
        return
    buf = StringIO()
    for prev_txid, prev_vout, spending_txid, spending_vin, height, block_hash, ts in spends_rows:
        buf.write(f"{prev_txid}\t{prev_vout}\t{spending_txid}\t{spending_vin}\t{height}\t{block_hash}\t{ts.isoformat()}\n")

    async with conn.cursor() as cur:
        await _copy_stage(
            cur,
            """
            CREATE TEMP TABLE IF NOT EXISTS _spends_stage
            (prev_txid text, prev_vout int, spending_txid text, spending_vin int,
             spent_height int, spent_block text, spent_time timestamptz)
            ON COMMIT DROP;
            TRUNCATE _spends_stage;
            """,
            "COPY _spends_stage (prev_txid, prev_vout, spending_txid, spending_vin, spent_height, spent_block, spent_time) FROM STDIN WITH (FORMAT text)",
            buf.getvalue(),
        )
        await cur.execute("""
            INSERT INTO blockchain.spends
              (prev_txid, prev_vout, spending_txid, spending_vin, spent_height, spent_block, spent_time)
            SELECT prev_txid, prev_vout, spending_txid, spending_vin, spent_height, spent_block, spent_time
            FROM _spends_stage
            ON CONFLICT (prev_txid, prev_vout) DO NOTHING;
        """)


async def update_total_in(conn, per_tx_vin_totals: Dict[str, int]) -> None:
    if not per_tx_vin_totals:
        return
    buf = StringIO()
    for txid, total_in in per_tx_vin_totals.items():
        buf.write(f"{txid}\t{total_in}\n")

    async with conn.cursor() as cur:
        await _copy_stage(
            cur,
            """
            CREATE TEMP TABLE IF NOT EXISTS _vin_totals (txid text primary key, total_in bigint)
            ON COMMIT DROP;
            TRUNCATE _vin_totals;
            """,
            "COPY _vin_totals (txid, total_in) FROM STDIN WITH (FORMAT text)",
            buf.getvalue(),
        )
        await cur.execute("""
            UPDATE blockchain.transactions t
               SET total_in = v.total_in
              FROM _vin_totals v
             WHERE t.txid = v.txid
               AND (t.total_in IS NULL OR t.total_in = 0);
        """)
