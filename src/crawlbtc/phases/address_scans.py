"""Phase: scan-addresses.

Applies per-block balance/utxo/tx-count deltas to blockchain.watch_addresses.
The delta computation is unchanged from the legacy 02_address_scans.py -
it runs entirely in SQL inside one transaction per height.
"""

import asyncio
import datetime
import traceback
from typing import List

from ..core import jobs
from ..core.logging import get_logger
from ..core.rpc import with_retry
from ..core.runner import PhaseContext

log = get_logger("addr-updater")

_READY_WHERE = """
    AND vout_status = ANY (ARRAY['done','skipped']::blockchain.block_job_status[])
    AND vin_status = 'done'::blockchain.block_job_status
"""

_DELTA_SQL = """
    WITH block_txs AS (
      SELECT txid
        FROM blockchain.transactions
       WHERE block_hash = %s
    ),
    credits AS (
      SELECT i.address,
             SUM(i.amount)::bigint AS credit_sats,
             COUNT(*)::int         AS credit_utxos,
             ARRAY_AGG(DISTINCT i.txid) AS credit_txids
        FROM blockchain.transaction_io i
        JOIN block_txs bt ON bt.txid = i.txid
        JOIN blockchain.watch_addresses wa ON wa.address = i.address
       WHERE i.io_type = 'out'::blockchain.tx_io_type
       GROUP BY i.address
    ),
    debits AS (
      SELECT tio.address,
             SUM(tio.amount)::bigint AS debit_sats,
             COUNT(*)::int           AS debit_utxos,
             ARRAY_AGG(DISTINCT s.spending_txid) AS debit_txids
        FROM blockchain.spends s
        JOIN blockchain.transaction_io tio
          ON tio.txid = s.prev_txid
         AND tio.idx  = s.prev_vout
         AND tio.io_type = 'out'::blockchain.tx_io_type
        JOIN blockchain.watch_addresses wa ON wa.address = tio.address
       WHERE s.spent_height = %s
       GROUP BY tio.address
    ),
    touched AS (
      SELECT address FROM credits
      UNION
      SELECT address FROM debits
    ),
    per_addr AS (
      SELECT
        a.address,
        (COALESCE(c.credit_sats,0) - COALESCE(d.debit_sats,0)) AS delta_sats,
        (COALESCE(c.credit_utxos,0) - COALESCE(d.debit_utxos,0)) AS delta_utxos,
        (
          SELECT COUNT(*) FROM (
            SELECT UNNEST(COALESCE(c.credit_txids, ARRAY[]::text[]))
            UNION
            SELECT UNNEST(COALESCE(d.debit_txids, ARRAY[]::text[]))
          ) u(x)
        )::int AS delta_txs,
        (
          SELECT MIN(x) FROM (
            SELECT UNNEST(COALESCE(c.credit_txids, ARRAY[]::text[])) x
            UNION
            SELECT UNNEST(COALESCE(d.debit_txids, ARRAY[]::text[]))
          ) u
        ) AS rep_txid
      FROM touched a
      LEFT JOIN credits c ON c.address = a.address
      LEFT JOIN debits  d ON d.address = a.address
    ),
    locked AS (
      SELECT wa.address
        FROM blockchain.watch_addresses wa
        JOIN per_addr p ON p.address = wa.address
       ORDER BY wa.address
       FOR UPDATE
    )
    UPDATE blockchain.watch_addresses wa
       SET balance_sats        = GREATEST(0, wa.balance_sats + p.delta_sats),
           utxo_count          = GREATEST(0, wa.utxo_count + p.delta_utxos),
           tx_count            = GREATEST(0, wa.tx_count + p.delta_txs),
           first_seen          = CASE WHEN wa.first_seen IS NULL THEN %s
                                      ELSE LEAST(wa.first_seen, %s) END,
           last_seen           = CASE WHEN wa.last_seen IS NULL THEN %s
                                      ELSE GREATEST(wa.last_seen, %s) END,
           last_scanned_height = GREATEST(COALESCE(wa.last_scanned_height, 0), %s),
           last_scanned_time   = CASE WHEN wa.last_scanned_time IS NULL THEN %s
                                      ELSE GREATEST(wa.last_scanned_time, %s) END,
           last_scanned_txid   = COALESCE(p.rep_txid, wa.last_scanned_txid),
           updated_at          = now()
      FROM per_addr p
     WHERE wa.address = p.address
 RETURNING wa.address;
"""


class AddressScanPhase:
    name = "scan-addresses"

    async def setup(self, ctx: PhaseContext):
        ctx.metrics.update({"heights_done": 0, "errors": 0, "touched_addresses": 0})
        if ctx.is_primary:
            await jobs.reset_abandoned(ctx.pool, "address_status")

    async def claim(self, ctx: PhaseContext) -> List[int]:
        return await jobs.claim_jobs(ctx.pool, "address_status", ctx.config.job_batch_size,
                                     extra_where=_READY_WHERE)

    async def progress(self, ctx: PhaseContext):
        return await jobs.get_phase_progress(ctx.pool, "address_status", extra_where=_READY_WHERE)

    def extra_metrics(self, ctx: PhaseContext) -> dict:
        return {
            "touched_addresses": ctx.metrics.get("touched_addresses", 0),
            "heights_done": ctx.metrics.get("heights_done", 0),
            "errors": ctx.metrics.get("errors", 0),
        }

    async def process(self, ctx: PhaseContext, height: int) -> None:
        try:
            block_hash = await with_retry(ctx.rpc.call, "getblockhash", [height])
            block = await with_retry(ctx.rpc.call, "getblock", [block_hash, 1], timeout=60.0)
            block_time = block.get("time")
            if not isinstance(block_time, int):
                raise ValueError(f"Invalid block time for height {height}")
            block_ts = datetime.datetime.fromtimestamp(block_time, datetime.UTC)

            async with ctx.db_write_sem:
                async with ctx.pool.connection() as conn:
                    async def _write():
                        async with conn.transaction():
                            async with conn.cursor() as cur:
                                await cur.execute(_DELTA_SQL, (
                                    block_hash, height,
                                    block_ts, block_ts,   # first_seen
                                    block_ts, block_ts,   # last_seen
                                    height,               # last_scanned_height
                                    block_ts, block_ts,   # last_scanned_time
                                ))
                                touched_count = len(await cur.fetchall())
                            await jobs.mark_status(conn, "address_status", height, "done")
                            return touched_count

                    touched = await asyncio.shield(_write())

            ctx.metrics["heights_done"] += 1
            ctx.metrics["touched_addresses"] += touched
            log.info("addr_height_done", height=height, touched_addresses=touched,
                     block_hash=block_hash)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("addr_height_failed", height=height, error=str(e),
                      traceback=traceback.format_exc())
            ctx.metrics["errors"] += 1
            try:
                await jobs.mark_status_standalone(ctx.pool, "address_status", height, "failed")
            except Exception:
                pass
