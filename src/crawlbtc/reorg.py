"""`crawlbtc detect-reorg` - find and repair orphaned blocks near the tip.

A chain reorganisation replaces the block(s) at the top of the chain with a
different branch. Any rows we extracted for the losing branch are now orphaned:
they describe transactions that are no longer in the canonical chain. This
command compares the block hash we stored at each recent height against the
node's canonical hash for that height, reports divergences, and (with --apply)
repairs them by deleting the orphaned rows and requeueing those heights so
`extract` rebuilds them from the canonical branch.

Needs the Bitcoin node reachable (it asks for getblockhash). Dry-run by default;
repair only happens with --apply.
"""

import asyncio
import sys

import psycopg

from .core.rpc import RpcClient


def _connect(cfg):
    return psycopg.connect(cfg.db_conninfo, autocommit=True)


async def _canonical_hashes(cfg, heights):
    import aiohttp
    rpc = RpcClient(cfg.rpc_url, cfg.rpc_user, cfg.rpc_password, concurrency=8)
    async with aiohttp.ClientSession(connector=rpc.make_connector()) as sess:
        rpc.session = sess
        tip = await rpc.call("getblockcount")
        out = {}
        for h in heights:
            if h <= tip:
                out[h] = await rpc.call("getblockhash", [h])
        return tip, out


# Delete every row owned by an orphaned block hash, then requeue its height.
# Kept as separate statements: psycopg's parameterized (extended-protocol)
# execute allows only one statement per call.
_REPAIR_STEPS = (
    "DELETE FROM blockchain.spends WHERE spent_block = %(h)s;",
    "DELETE FROM blockchain.transaction_io "
    " WHERE txid IN (SELECT txid FROM blockchain.transactions WHERE block_hash = %(h)s);",
    "DELETE FROM blockchain.transactions WHERE block_hash = %(h)s;",
    "DELETE FROM blockchain.blocks WHERE block_hash = %(h)s;",
    "UPDATE blockchain.block_jobs "
    "   SET vout_status = 'pending', vin_status = 'pending', address_status = 'pending', "
    "       features = 'none', updated_at = now() "
    " WHERE height = %(height)s;",
)


def cmd_detect_reorg(args, cfg):
    with _connect(cfg) as conn:
        cur = conn.cursor()
        cur.execute("SELECT MAX(height) FROM blockchain.blocks;")
        row = cur.fetchone()
        max_h = row[0] if row else None
        if max_h is None:
            print("no blocks stored yet.", file=sys.stderr)
            return
        lo = max(0, max_h - args.depth + 1)
        cur.execute("""
            SELECT height, block_hash FROM blockchain.blocks
             WHERE height BETWEEN %s AND %s ORDER BY height;
        """, (lo, max_h))
        stored = dict(cur.fetchall())

    heights = sorted(stored)
    print(f"checking {len(heights)} stored heights {lo}..{max_h} against the node ...",
          file=sys.stderr)
    try:
        tip, canonical = asyncio.run(_canonical_hashes(cfg, heights))
    except Exception as e:
        print(f"could not reach the node: {e}", file=sys.stderr)
        print("start Bitcoin Core (RPC) and retry; nothing was changed.", file=sys.stderr)
        sys.exit(1)

    divergent = [h for h in heights if h in canonical and stored[h] != canonical[h]]
    print(f"node tip height: {tip:,}; stored tip: {max_h:,}")
    if not divergent:
        print("no reorg detected - every stored block matches the canonical chain.")
        if tip > max_h:
            print(f"note: node is {tip - max_h:,} block(s) ahead; run `extract` to catch up.")
        return

    print(f"\nREORG: {len(divergent)} orphaned block(s):")
    for h in divergent:
        print(f"  height {h}: stored {stored[h][:16]}… -> canonical {canonical[h][:16]}…")

    if not args.apply:
        print("\ndry-run. Re-run with --apply to delete the orphaned rows and requeue "
              "these heights for re-extraction.")
        return

    with _connect(cfg) as conn:
        cur = conn.cursor()
        for h in divergent:
            params = {"h": stored[h], "height": h}
            cur.execute("BEGIN;")
            for stmt in _REPAIR_STEPS:
                cur.execute(stmt, params)
            cur.execute("COMMIT;")
            print(f"  repaired height {h} (orphaned rows deleted, requeued)")
    print(f"\nrepaired {len(divergent)} height(s). Run `crawlbtc extract` to rebuild them, "
          f"then `update-balances` if watched addresses may be affected.")
