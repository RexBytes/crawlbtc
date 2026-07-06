"""Shared phase runner: worker loops, monitor loop, and multi-process launcher.

A "phase" object supplies:
    name            - str, used in logs
    claim(pool)     - coroutine returning List[int] heights to work on
    process(ctx, height) - coroutine handling one height
    progress(pool)  - coroutine returning the 5-tuple for the progress log
    extra_metrics() - dict merged into progress line 2
    top_up(ctx)     - optional coroutine run by the primary process's monitor

Multiple OS processes run this same loop concurrently; the SKIP LOCKED job
queue arbitrates. Only process 0 logs progress and tops up new blocks.
"""

import asyncio
import datetime
import multiprocessing
import os
import signal
import sys
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from psycopg_pool import AsyncConnectionPool

from .config import Config
from .logging import get_logger
from .rpc import RpcClient

log = get_logger("runner")


@dataclass
class PhaseContext:
    config: Config
    pool: AsyncConnectionPool
    rpc: RpcClient
    db_write_sem: asyncio.Semaphore
    shutdown_event: asyncio.Event
    is_primary: bool
    metrics: dict = field(default_factory=dict)


async def _worker_loop(phase, ctx: PhaseContext):
    while not ctx.shutdown_event.is_set():
        heights = await phase.claim(ctx)
        if not heights:
            await asyncio.sleep(2)
            continue
        log.info("worker_starting_jobs", phase=phase.name, heights=heights)
        for height in heights:
            if ctx.shutdown_event.is_set():
                break
            await phase.process(ctx, height)


async def _monitor_loop(phase, ctx: PhaseContext):
    start_done = None
    start_time = asyncio.get_event_loop().time()

    while not ctx.shutdown_event.is_set():
        try:
            # Top up before reading progress so newly discovered blocks are
            # counted as pending and can't trigger a premature shutdown.
            if ctx.is_primary:
                top_up = getattr(phase, "top_up", None)
                if top_up is not None:
                    await top_up(ctx)

            done, in_progress, pending, skipped, latest = await phase.progress(ctx)
            total_complete = done + skipped

            if start_done is None and total_complete > 0:
                start_done = total_complete
                start_time = asyncio.get_event_loop().time()

            eta_str = "calculating..."
            if start_done is not None and total_complete > start_done:
                rate = (total_complete - start_done) / (asyncio.get_event_loop().time() - start_time)
                if rate > 0:
                    eta = datetime.datetime.now() + datetime.timedelta(seconds=pending / rate)
                    eta_str = eta.strftime("%Y-%m-%d %H:%M:%S")

            if ctx.is_primary:
                log.info("progress", line=1, done=done, in_progress=in_progress,
                         pending=pending, skipped=skipped, eta=eta_str)
                log.info("progress", line=2, latest_processed_block=latest,
                         **phase.extra_metrics(ctx))

            if pending == 0 and in_progress == 0 and (done + skipped) > 0:
                if ctx.is_primary:
                    log.info("all_blocks_processed", phase=phase.name, metrics=ctx.metrics)
                ctx.shutdown_event.set()

        except Exception as e:
            log.error("monitor_error", phase=phase.name, error=str(e))
            ctx.metrics["errors"] = ctx.metrics.get("errors", 0) + 1

        await asyncio.sleep(5)


async def run_phase_async(phase_factory, config: Config, is_primary: bool = True) -> None:
    phase = phase_factory()
    shutdown_event = asyncio.Event()

    if hasattr(signal, "SIGINT"):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, shutdown_event.set)
            except NotImplementedError:
                pass

    rpc = RpcClient(config.rpc_url, config.rpc_user, config.rpc_password,
                    config.rpc_concurrency, client_id=f"crawlbtc-{phase.name}")

    async with AsyncConnectionPool(
        config.db_conninfo,
        min_size=max(1, min(config.db_max_conn // 2, config.num_workers)),
        max_size=config.db_max_conn,
        timeout=config.db_pool_timeout,
    ) as pool:
        async with aiohttp.ClientSession(connector=rpc.make_connector()) as session:
            rpc.session = session

            if not await rpc.healthy():
                log.critical("bitcoin_core_unavailable",
                             message="Bitcoin Core RPC is not responding. Exiting.")
                print("⚠️  Bitcoin Core is not responding. Exiting now.", file=sys.stderr)
                sys.exit(1)

            ctx = PhaseContext(
                config=config,
                pool=pool,
                rpc=rpc,
                db_write_sem=asyncio.BoundedSemaphore(config.db_write_concurrency),
                shutdown_event=shutdown_event,
                is_primary=is_primary,
            )

            setup = getattr(phase, "setup", None)
            if setup is not None:
                await setup(ctx)

            if is_primary:
                log.info("starting_workers", phase=phase.name,
                         num_workers=config.num_workers,
                         processes=config.processes,
                         rpc_concurrency=config.rpc_concurrency,
                         db_max_conn=config.db_max_conn,
                         db_write_conc=config.db_write_concurrency,
                         job_batch_size=config.job_batch_size)

            if config.start_delay > 0:
                log.info("starting_in", seconds=config.start_delay)
                await asyncio.sleep(config.start_delay)

            tasks = [asyncio.create_task(_worker_loop(phase, ctx))
                     for _ in range(config.num_workers)]
            tasks.append(asyncio.create_task(_monitor_loop(phase, ctx)))

            await shutdown_event.wait()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def _child_entry(phase_module: str, phase_attr: str, config: Config, index: int):
    import importlib
    mod = importlib.import_module(phase_module)
    factory = getattr(mod, phase_attr)
    asyncio.run(run_phase_async(factory, config, is_primary=(index == 0)))


def run_phase(phase_factory, config: Config) -> None:
    """Run a phase across config.processes OS processes.

    Multiple processes exist to parallelize JSON parsing of block payloads,
    which is CPU-bound and would otherwise pin a single core no matter how
    many async workers are configured.
    """
    if config.processes <= 1:
        asyncio.run(run_phase_async(phase_factory, config, is_primary=True))
        return

    mp = multiprocessing.get_context("spawn")
    procs = []
    for i in range(1, config.processes):
        p = mp.Process(
            target=_child_entry,
            args=(phase_factory.__module__, phase_factory.__qualname__, config, i),
            daemon=False,
        )
        p.start()
        procs.append(p)

    try:
        asyncio.run(run_phase_async(phase_factory, config, is_primary=True))
    finally:
        for p in procs:
            p.join(timeout=30)
            if p.is_alive():
                p.terminate()
                p.join(timeout=10)
