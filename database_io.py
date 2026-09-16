"""Keep SQLite and backups out of both the event loop and DNS executor."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from store import RuleError


class DatabaseIO:
    WRITES = {"configure", "register", "rename", "adjust", "create_event", "attend", "award_event",
              "cancel_event", "create_auction", "bid", "close_auction", "close_due", "cancel_auction",
              "rendered", "delivered"}

    def __init__(self, store):
        self.store = store
        self.pools = {
            "read": ThreadPoolExecutor(3, thread_name_prefix="dkp-read"),
            "write": ThreadPoolExecutor(1, thread_name_prefix="dkp-write"),
            "backup": ThreadPoolExecutor(1, thread_name_prefix="dkp-backup"),
        }
        self.slots = {name: asyncio.Semaphore(3 if name == "read" else 1) for name in self.pools}

    async def call(self, function, *args, write=False, **kwargs):
        name = getattr(function, "__name__", "")
        lane = "backup" if name == "backup" else "write" if write or name in self.WRITES else "read"
        slot = self.slots[lane]
        try:
            await asyncio.wait_for(slot.acquire(), 10)
        except TimeoutError:
            raise RuleError("База занята другими операциями. Этот запрос не выполнялся; повторите чуть позже.") from None
        try:
            future = asyncio.get_running_loop().run_in_executor(self.pools[lane], partial(function, *args, **kwargs))
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                # Do not abandon a transaction that may already be committing.
                await future
                raise
        finally:
            slot.release()

    def __getattr__(self, name):
        return partial(self.call, getattr(self.store, name))

    async def close(self):
        for pool in self.pools.values():
            await asyncio.to_thread(pool.shutdown, wait=True)
