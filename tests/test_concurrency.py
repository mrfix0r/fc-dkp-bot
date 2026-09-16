"""Mixed traffic, blocked SQLite writers and independent auction settlement."""
import asyncio
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_bot as support

discord = support.discord
if discord is not None:
    from bot import EventView


@unittest.skipIf(discord is None, "Install requirements.txt for concurrency tests")
class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.DiscordResponseTests.asyncSetUp
    asyncTearDown = support.DiscordResponseTests.asyncTearDown
    invoke = support.DiscordResponseTests.invoke

    async def test_one_hundred_simultaneous_requests_all_receive_results(self):
        eid = self.store.create_event("ЧВ", 10, 1)
        aid = self.store.create_auction("Щит", 10, 1, 5, 1)
        event = EventView(self.bot, eid)
        interactions, jobs = [], []
        for index in range(100):
            i = support.FakeInteraction(command="balance")
            interactions.append(i)
            if index < 60:
                jobs.append(self.invoke("balance", i))
            elif index < 80:
                jobs.append(self.invoke("bid", i, auction_id=aid, amount=index - 50))
            else:
                i.type = discord.InteractionType.component
                jobs.append(event._scheduled_task(event.join, i))
        await asyncio.wait_for(asyncio.gather(*jobs), 10)
        for i in interactions:
            self.assertEqual(i.response.defer.await_count, 1)
            self.assertEqual(len(i.messages), 1)
        self.assertEqual(self.store.balance(1)["reserved"], 29)
        self.assertEqual(self.store.balance(1)["total"], 100)
        self.assertEqual(len(self.store.attendees(eid)), 1)

    async def test_busy_writer_does_not_block_balance_or_initial_responses(self):
        i = support.FakeInteraction(command="rename")
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        original = self.store.rename
        def rename(*args):
            loop.call_soon_threadsafe(entered.set)
            return original(*args)
        # A real SQLite lock, held on a separate connection until reads finish.
        writer = None
        try:
            with self.store.transaction():
                with patch.object(self.store, "rename", rename):
                    writer = asyncio.create_task(self.invoke("rename", i, member=i.user, nickname="Renamed"))
                    await asyncio.wait_for(entered.wait(), 1)
                    balance = support.FakeInteraction()
                    await asyncio.wait_for(self.invoke("balance", balance), 1)
                    self.assertIn("Fix0r", balance.messages[0][0])
                    self.assertTrue(i.done)
                    self.assertFalse(writer.done())
        finally:
            if writer is not None:
                await asyncio.wait_for(writer, 2)
        self.assertEqual(self.store.balance(1)["nickname"], "Renamed")

    async def test_slow_publication_does_not_delay_settlement_or_commands(self):
        now = [1000000]
        self.store.clock = lambda: now[0]
        aid = self.store.create_auction("Щит", 10, 1, 1, 1)
        self.store.bid(aid, 1, 30)
        started, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            started.set()
            await release.wait()
            return SimpleNamespace(id=444)
        channel = SimpleNamespace(send=AsyncMock(side_effect=send))
        with patch.object(self.bot, "get_channel", return_value=channel):
            publication = asyncio.create_task(self.bot.worker.coro(self.bot))
            try:
                await asyncio.wait_for(started.wait(), 1)
                now[0] += 61
                await asyncio.wait_for(self.bot.settlements.coro(self.bot), 1)
                i = support.FakeInteraction()
                await asyncio.wait_for(self.invoke("balance", i), 1)
                self.assertIn("70", i.messages[0][0])
                self.assertEqual(self.store.entity("auctions", aid)["status"], "closed")
                self.assertFalse(publication.done())
            finally:
                release.set()
                await publication

    async def test_cancelled_waiter_does_not_abandon_an_inflight_write(self):
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        def write():
            loop.call_soon_threadsafe(started.set)
            if not release.wait(3):
                raise AssertionError("Writer was not released")
            self.store.adjust(1, 10, "Test", 1, "cancelled-task")
        task = asyncio.create_task(self.bot.db.call(write, write=True))
        try:
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertTrue(self.bot.db.slots["write"].locked())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.store.balance(1)["total"], 110)
        self.assertFalse(self.bot.db.slots["write"].locked())
