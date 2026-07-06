"""Phase: backfill-vins (repair tool).

After the merged extract phase, vin_status is already 'done' for every
block fetched at verbosity 3, so this phase finds nothing to do. It remains
for two cases: nodes that only support verbosity 2, and repairing legacy
databases crawled with the old two-pass scripts.

The legacy "quick DB check" shortcut (missing_inputs <= 1 AND spends > 0
=> assume complete) was removed: it could mark a block done while a
non-coinbase transaction was still missing its inputs. Reprocessing is
idempotent, so correctness wins here.
"""

import asyncio
import datetime
import traceback
from io import StringIO
from typing import Dict, List, Tuple

from ..core import jobs
from ..core.addresses import extract_output_address, to_sats, validate_txid
from ..core.ingest import insert_spends_rows, insert_txio_rows, update_total_in
from ..core.logging import get_logger
from ..core.rpc import with_retry
from ..core.runner import PhaseContext

log = get_logger("vin-backfill")

_READY_WHERE = """
    AND vout_status = ANY (ARRAY['done','skipped']::blockchain.block_job_status[])
"""


class BackfillVinsPhase:
    name = "backfill-vins"

    def __init__(self):
        self.p2pk_label = "p2pk"

    async def setup(self, ctx: PhaseContext):
        ctx.metrics.update({"blocks_processed": 0, "errors": 0,
                            "vins_inserted": 0, "spends_inserted": 0})
        async with ctx.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT 1 FROM pg_enum e
                    JOIN pg_type t ON t.oid = e.enumtypid
                    WHERE t.typname = 'address_type' AND e.enumlabel = 'p2pk';
                """)
                if await cur.fetchone() is None:
                    self.p2pk_label = "p2pkh"
        if ctx.is_primary:
            await jobs.reset_abandoned(ctx.pool, "vin_status")

    async def claim(self, ctx: PhaseContext) -> List[int]:
        return await jobs.claim_jobs(ctx.pool, "vin_status", ctx.config.job_batch_size,
                                     extra_where=_READY_WHERE)

    async def progress(self, ctx: PhaseContext):
        return await jobs.get_phase_progress(ctx.pool, "vin_status", extra_where=_READY_WHERE)

    def extra_metrics(self, ctx: PhaseContext) -> dict:
        return {
            "vins_inserted": ctx.metrics.get("vins_inserted", 0),
            "spends_inserted": ctx.metrics.get("spends_inserted", 0),
            "blocks_processed": ctx.metrics.get("blocks_processed", 0),
            "errors": ctx.metrics.get("errors", 0),
        }

    async def process(self, ctx: PhaseContext, height: int) -> None:
        try:
            block_hash = await with_retry(ctx.rpc.call, "getblockhash", [height])
            block, has_prevouts = await ctx.rpc.get_block_with_prevouts(block_hash)

            block_time = block.get("time")
            if not isinstance(block_time, int):
                raise ValueError(f"Invalid block time for height {height}")
            spent_ts = datetime.datetime.fromtimestamp(block_time, datetime.UTC)

            in_rows: List[Tuple] = []
            per_tx_vin_totals: Dict[str, int] = {}
            missing_prevouts: List[Tuple[str, int, str, int]] = []
            spends_rows: List[Tuple] = []

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

                    spends_rows.append((prev_txid, prev_idx, txid, vin_idx,
                                        height, block_hash, spent_ts))

                    prevout = vin.get("prevout") or {}
                    if has_prevouts and prevout:
                        spk = prevout.get("scriptPubKey", {}) or {}
                        address, addr_type = extract_output_address(spk, self.p2pk_label)
                        value = prevout.get("value")
                        if address is not None and isinstance(value, (int, float, str)):
                            try:
                                sat = to_sats(value)
                            except Exception:
                                sat = 0
                            if sat > 0:
                                vin_total += sat
                                in_rows.append((txid, address, addr_type, sat, "in", vin_idx))
                                continue
                        missing_prevouts.append((prev_txid, prev_idx, txid, vin_idx))
                    else:
                        missing_prevouts.append((prev_txid, prev_idx, txid, vin_idx))
                per_tx_vin_totals[txid] = vin_total

            async with ctx.db_write_sem:
                async with ctx.pool.connection() as conn:
                    async def _write():
                        async with conn.transaction():
                            # Resolve prevouts missing from the RPC response
                            # against outputs already in the database.
                            if missing_prevouts:
                                async with conn.cursor() as cur:
                                    await cur.execute("""
                                        CREATE TEMP TABLE IF NOT EXISTS _needed_prevouts
                                        (prev_txid text, prev_idx int, cur_txid text, vin_idx int)
                                        ON COMMIT DROP;
                                        TRUNCATE _needed_prevouts;
                                    """)
                                    buf = StringIO()
                                    for prev_txid, prev_idx, cur_txid, vin_idx in missing_prevouts:
                                        buf.write(f"{prev_txid}\t{prev_idx}\t{cur_txid}\t{vin_idx}\n")
                                    async with cur.copy("""
                                        COPY _needed_prevouts (prev_txid, prev_idx, cur_txid, vin_idx)
                                        FROM STDIN WITH (FORMAT text)
                                    """) as copy:
                                        await copy.write(buf.getvalue())
                                    await cur.execute("""
                                        SELECT n.cur_txid, n.vin_idx, tio.address, tio.address_type, tio.amount
                                          FROM _needed_prevouts n
                                          JOIN blockchain.transaction_io tio
                                            ON tio.txid = n.prev_txid
                                           AND tio.io_type = 'out'::blockchain.tx_io_type
                                           AND tio.idx = n.prev_idx;
                                    """)
                                    for cur_txid, vin_idx, address, addr_type, amt in await cur.fetchall():
                                        in_rows.append((cur_txid, address, addr_type, int(amt), "in", int(vin_idx)))
                                        per_tx_vin_totals[cur_txid] = per_tx_vin_totals.get(cur_txid, 0) + int(amt)

                            await insert_txio_rows(conn, in_rows)
                            await update_total_in(conn, per_tx_vin_totals)
                            await insert_spends_rows(conn, spends_rows)
                            await jobs.mark_status(conn, "vin_status", height, "done")

                    await asyncio.shield(_write())

            ctx.metrics["blocks_processed"] += 1
            ctx.metrics["vins_inserted"] += len(in_rows)
            ctx.metrics["spends_inserted"] += len(spends_rows)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("vin_block_failed", height=height, error=str(e),
                      traceback=traceback.format_exc())
            ctx.metrics["errors"] += 1
            try:
                await jobs.mark_status_standalone(ctx.pool, "vin_status", height, "failed")
            except Exception:
                pass
