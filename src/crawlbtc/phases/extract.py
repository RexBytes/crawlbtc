"""Phase: extract (merged vout + vin single pass).

Fetches each block once at getblock verbosity 3, which includes every
input's prevout inline - so one pass produces the transactions, both 'out'
and 'in' transaction_io rows, spends edges, and total_in/total_out. The
legacy pipeline fetched and parsed the whole chain twice (verbosity 2 for
vouts, then verbosity 3 again for vins); this halves that work.

On nodes without verbosity 3 the block is fetched at verbosity 2, only the
vout side is written, and vin_status stays pending for the backfill-vins
phase - identical to the legacy behavior.
"""

import asyncio
import datetime
import time
import traceback
from typing import List, Optional, Tuple

from ..core import jobs
from ..core.addresses import extract_output_address, to_sats, validate_txid
from ..core.ingest import insert_spends_rows, insert_tx_rows, insert_txio_rows
from ..core.logging import get_logger
from ..core.rpc import with_retry
from ..core.runner import PhaseContext

log = get_logger("block-extractor")


def classify_block_features(has_out_rows: bool, has_in_rows: bool, transactions: List[dict]) -> str:
    has_vin = has_in_rows or any(
        isinstance(tx.get("vin"), list) and any("txid" in vin for vin in tx["vin"])
        for tx in transactions
    )
    is_coinbase_only = bool(transactions) and all(
        isinstance(tx.get("vin"), list) and len(tx["vin"]) == 1 and "coinbase" in tx["vin"][0]
        for tx in transactions
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

    if has_vin and has_out_rows:
        return "both"
    if has_vin:
        return "vin"
    if has_out_rows:
        return "vout"
    if any_spendable_output():
        return "op_return_only"
    if is_coinbase_only:
        return "coinbase_only"
    return "none"


def parse_block(block: dict, block_hash: str, height: int, has_prevouts: bool, p2pk_label: str):
    """Pure CPU part of block processing; returns all row sets."""
    block_time = block.get("time")
    if not isinstance(block_time, int):
        raise ValueError("invalid block time")
    spent_ts = datetime.datetime.fromtimestamp(block_time, datetime.UTC)

    transactions = block.get("tx", [])
    tx_rows: List[Tuple] = []
    out_rows: List[Tuple] = []
    in_rows: List[Tuple] = []
    spends_rows: List[Tuple] = []

    for tx in transactions:
        txid = tx.get("txid")
        if not validate_txid(txid):
            log.warning("invalid_txid", txid=txid)
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
            address, address_type = extract_output_address(spk, p2pk_label)
            if address is None:
                continue
            try:
                satoshis = to_sats(value)
            except Exception as conv_err:
                log.warning("bad_vout_value", txid=txid, value=value, error=str(conv_err))
                continue
            if satoshis <= 0:
                continue
            vout_total += satoshis
            out_rows.append((txid, address, address_type, satoshis, "out", idx))

        vin_total = 0
        for vin_idx, vin in enumerate(tx.get("vin", []) or []):
            if not isinstance(vin, dict) or "coinbase" in vin:
                continue
            prev_txid, prev_idx = vin.get("txid"), vin.get("vout")
            if not (validate_txid(prev_txid) and isinstance(prev_idx, int)):
                continue

            spends_rows.append((prev_txid, prev_idx, txid, vin_idx, height, block_hash, spent_ts))

            if has_prevouts:
                prevout = vin.get("prevout") or {}
                spk = prevout.get("scriptPubKey", {}) or {}
                address, address_type = extract_output_address(spk, p2pk_label)
                value = prevout.get("value")
                if address is not None and isinstance(value, (int, float, str)):
                    try:
                        sat = to_sats(value)
                    except Exception:
                        sat = 0
                    if sat > 0:
                        vin_total += sat
                        in_rows.append((txid, address, address_type, sat, "in", vin_idx))

        tx_rows.append((txid, block_hash, block_time, vin_total if has_prevouts else 0, vout_total))

    features = classify_block_features(bool(out_rows), bool(in_rows), transactions)
    return tx_rows, out_rows, in_rows, spends_rows, features


class ExtractPhase:
    name = "extract"

    def __init__(self):
        self._cached_tip: Optional[int] = None
        self._tip_expiry = 0.0
        self.p2pk_label = "p2pk"

    async def setup(self, ctx: PhaseContext):
        ctx.metrics.update({"blocks_processed": 0, "errors": 0, "skipped": 0})
        # Fall back to labeling derived P2PK addresses as p2pkh if the
        # database enum predates the 'p2pk' value (run `crawlbtc migrate`).
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
                        log.warning("p2pk_enum_missing",
                                    message="address_type enum lacks 'p2pk'; labeling derived "
                                            "P2PK addresses as 'p2pkh'. Run `crawlbtc migrate`.")
        if ctx.is_primary:
            await jobs.reset_abandoned(ctx.pool, "vout_status")

    async def claim(self, ctx: PhaseContext) -> List[int]:
        return await jobs.claim_jobs(ctx.pool, "vout_status", ctx.config.job_batch_size)

    async def progress(self, ctx: PhaseContext):
        return await jobs.get_phase_progress(ctx.pool, "vout_status")

    def extra_metrics(self, ctx: PhaseContext) -> dict:
        return {
            "blocks_processed": ctx.metrics.get("blocks_processed", 0),
            "errors": ctx.metrics.get("errors", 0),
        }

    async def top_up(self, ctx: PhaseContext):
        now = asyncio.get_event_loop().time()
        if now > self._tip_expiry:
            self._cached_tip = await with_retry(ctx.rpc.call, "getblockcount", timeout=20.0)
            self._tip_expiry = now + 60
        if self._cached_tip is not None:
            await jobs.top_up_jobs(ctx.pool, self._cached_tip)

    async def process(self, ctx: PhaseContext, height: int) -> None:
        try:
            start = time.time()
            block_hash = await with_retry(ctx.rpc.call, "getblockhash", [height])
            block, has_prevouts = await ctx.rpc.get_block_with_prevouts(block_hash)

            tx_rows, out_rows, in_rows, spends_rows, features = parse_block(
                block, block_hash, height, has_prevouts, self.p2pk_label
            )

            if features == "none":
                log.warning("no_valid_txio", height=height)
                await jobs.mark_status_standalone(ctx.pool, "vout_status", height, "skipped")
                if has_prevouts:
                    await jobs.mark_status_standalone(ctx.pool, "vin_status", height, "done")
                ctx.metrics["skipped"] = ctx.metrics.get("skipped", 0) + 1
                return

            async with ctx.db_write_sem:
                async with ctx.pool.connection() as conn:
                    async def _write():
                        async with conn.transaction():
                            await insert_tx_rows(conn, tx_rows)
                            await insert_txio_rows(conn, out_rows + in_rows)
                            await insert_spends_rows(conn, spends_rows)
                            await jobs.mark_status(conn, "vout_status", height, "done", features)
                            if has_prevouts:
                                await jobs.mark_status(conn, "vin_status", height, "done")

                    await asyncio.shield(_write())

            ctx.metrics["blocks_processed"] = ctx.metrics.get("blocks_processed", 0) + 1
            log.info("block_processed", height=height, tx_count=len(tx_rows),
                     vins=len(in_rows), duration=time.time() - start)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("block_failed", height=height, error=str(e),
                      traceback=traceback.format_exc())
            ctx.metrics["errors"] = ctx.metrics.get("errors", 0) + 1
            try:
                await jobs.mark_status_standalone(ctx.pool, "vout_status", height, "failed")
            except Exception:
                pass
