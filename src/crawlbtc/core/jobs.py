"""block_jobs queue operations, parameterized by phase status column.

The FOR UPDATE SKIP LOCKED claim makes multiple async workers AND multiple
OS processes safe against double-claiming - this is what lets `--processes N`
scale without coordination.
"""

from typing import List, Optional, Tuple

from psycopg import sql

_STATUS_COLUMNS = ("vout_status", "vin_status", "address_status")


def _col(column: str) -> sql.Identifier:
    if column not in _STATUS_COLUMNS:
        raise ValueError(f"unknown status column: {column}")
    return sql.Identifier(column)


async def reset_abandoned(pool, column: str, stale_minutes: int = 15) -> int:
    """Reset stale in_progress jobs to pending.

    Only jobs untouched for `stale_minutes` are reset, so concurrently
    running instances/processes don't clobber each other's in-flight work.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql.SQL("""
                    UPDATE blockchain.block_jobs
                       SET {col} = 'pending'::blockchain.block_job_status,
                           updated_at = now()
                     WHERE {col} = 'in_progress'::blockchain.block_job_status
                       AND updated_at < now() - make_interval(mins => %s);
                """).format(col=_col(column)),
                (stale_minutes,),
            )
            count = cur.rowcount
            await conn.commit()
            return count


async def claim_jobs(pool, column: str, batch_size: int, extra_where: str = "") -> List[int]:
    """Atomically claim up to batch_size pending heights for this phase."""
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    sql.SQL("""
                        UPDATE blockchain.block_jobs
                           SET {col} = 'in_progress'::blockchain.block_job_status,
                               updated_at = now()
                         WHERE height IN (
                             SELECT height
                               FROM blockchain.block_jobs
                              WHERE {col} = 'pending'::blockchain.block_job_status
                                    {extra}
                              ORDER BY height
                              LIMIT %s
                              FOR UPDATE SKIP LOCKED
                         )
                     RETURNING height;
                    """).format(col=_col(column), extra=sql.SQL(extra_where)),
                    (batch_size,),
                )
                rows = await cur.fetchall()
                return [r[0] for r in rows]


async def mark_status(conn, column: str, height: int, status: str, features: Optional[str] = None) -> None:
    """Set a phase status inside the caller's transaction/connection."""
    async with conn.cursor() as cur:
        if features is not None:
            await cur.execute(
                sql.SQL("""
                    UPDATE blockchain.block_jobs
                       SET {col} = %s::blockchain.block_job_status,
                           features = %s::blockchain.block_feature,
                           updated_at = now()
                     WHERE height = %s;
                """).format(col=_col(column)),
                (status, features, height),
            )
        else:
            await cur.execute(
                sql.SQL("""
                    UPDATE blockchain.block_jobs
                       SET {col} = %s::blockchain.block_job_status,
                           updated_at = now()
                     WHERE height = %s;
                """).format(col=_col(column)),
                (status, height),
            )


async def mark_status_standalone(pool, column: str, height: int, status: str) -> None:
    """Same as mark_status but grabs its own connection (for failure paths)."""
    async with pool.connection() as conn:
        await mark_status(conn, column, height, status)


async def get_phase_progress(pool, column: str, extra_where: str = "") -> Tuple[int, int, int, int, int]:
    """Returns (done, in_progress, pending, skipped_or_failed, latest_done_height)."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql.SQL("""
                    SELECT
                      COUNT(*) FILTER (WHERE {col} = 'done'::blockchain.block_job_status)        AS done,
                      COUNT(*) FILTER (WHERE {col} = 'in_progress'::blockchain.block_job_status) AS in_progress,
                      COUNT(*) FILTER (WHERE {col} = 'pending'::blockchain.block_job_status)     AS pending,
                      COUNT(*) FILTER (WHERE {col} IN ('skipped'::blockchain.block_job_status,
                                                       'failed'::blockchain.block_job_status))   AS skipped_failed,
                      COALESCE(MAX(height) FILTER (WHERE {col} IN ('done'::blockchain.block_job_status,
                                                                   'skipped'::blockchain.block_job_status)), 0)
                        AS latest_done
                    FROM blockchain.block_jobs
                    {where}
                """).format(
                    col=_col(column),
                    where=sql.SQL(f"WHERE TRUE {extra_where}" if extra_where else ""),
                )
            )
            return await cur.fetchone()


async def top_up_jobs(pool, tip_height: int) -> int:
    """Insert pending jobs for any new blocks up to the node's tip."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COALESCE(MAX(height), -1) FROM blockchain.block_jobs;")
            latest_job = (await cur.fetchone())[0]
            if tip_height <= latest_job:
                return 0
            await cur.execute("""
                INSERT INTO blockchain.block_jobs (height, vout_status, vin_status, address_status)
                SELECT g, 'pending', 'pending', 'pending'
                FROM generate_series(%s::int, %s::int) g
                ON CONFLICT (height) DO NOTHING;
            """, (latest_job + 1, tip_height))
            await conn.commit()
            return tip_height - latest_job
