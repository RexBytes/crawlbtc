"""Bitcoin Core JSON-RPC client with orjson parsing, throttling and retry."""

import asyncio
import random
from typing import List, Optional

import aiohttp
import orjson
import psycopg

from .logging import get_logger

log = get_logger("rpc")


async def with_retry(
    coro_func, *args, retries=3, delay=1.0, backoff=2.0, jitter=0.1,
    exceptions=(aiohttp.ClientError, psycopg.Error, ValueError, RuntimeError),
    metrics: Optional[dict] = None,
    **kwargs,
):
    last_exception = None
    for attempt in range(retries):
        try:
            return await coro_func(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except exceptions as e:
            last_exception = e
            if attempt < retries - 1:
                sleep_time = delay * (backoff ** attempt) + random.uniform(0, jitter)
                log.warning("retry", func=getattr(coro_func, "__name__", str(coro_func)),
                            attempt=attempt + 1, retries=retries, error=str(e), sleep=sleep_time)
                await asyncio.sleep(sleep_time)
            else:
                log.error("retry_exhausted", func=getattr(coro_func, "__name__", str(coro_func)),
                          error=str(last_exception))
                if metrics is not None:
                    metrics["errors"] = metrics.get("errors", 0) + 1
                raise last_exception


class RpcClient:
    """One instance per process; caller provides the aiohttp session."""

    def __init__(self, url: str, user: str, password: str, concurrency: int, client_id: str = "crawlbtc"):
        self.url = url
        self.auth = aiohttp.BasicAuth(user, password)
        self.semaphore = asyncio.BoundedSemaphore(concurrency)
        self.client_id = client_id
        self.session: Optional[aiohttp.ClientSession] = None

    def make_connector(self) -> aiohttp.TCPConnector:
        limit = self.semaphore._value  # initial capacity
        return aiohttp.TCPConnector(limit=limit, limit_per_host=limit)

    async def call(self, method: str, params: Optional[List] = None, timeout: float = 30.0):
        async with self.semaphore:
            try:
                async with self.session.post(
                    self.url,
                    data=orjson.dumps({"jsonrpc": "1.0", "id": self.client_id,
                                       "method": method, "params": params or []}),
                    headers={"Content-Type": "application/json"},
                    auth=self.auth,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    resp.raise_for_status()
                    # orjson is several times faster than stdlib json on the
                    # multi-megabyte verbosity-2/3 block payloads, which is
                    # what the event loop otherwise chokes on.
                    data = orjson.loads(await resp.read())
                    if data.get("error"):
                        raise ValueError(f"RPC error: {data['error']}")
                    return data["result"]
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise RuntimeError(f"RPC call '{method}' failed: {e}") from e

    async def get_block_with_prevouts(self, block_hash: str) -> tuple[dict, bool]:
        """Fetch a block, preferring verbosity 3 (inline prevouts).

        Returns (block, has_prevouts). Falls back to verbosity 2 on nodes
        older than Core 25.
        """
        try:
            block = await with_retry(self.call, "getblock", [block_hash, 3], timeout=120.0)
            return block, True
        except Exception:
            block = await with_retry(self.call, "getblock", [block_hash, 2], timeout=120.0)
            return block, False

    async def healthy(self) -> bool:
        try:
            block_count = await self.call("getblockcount")
            log.info("rpc_healthy", block_count=block_count)
            return True
        except Exception as e:
            log.error("rpc_unreachable", error=str(e))
            return False
