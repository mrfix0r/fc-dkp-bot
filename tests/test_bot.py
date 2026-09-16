"""Offline Discord handler checks, enabled when requirements.txt is installed."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading
import itertools
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from store import RuleError, Store

try:
    import discord
    from bot import DKPBot, DKPCommands, PanelView, AuctionView, ConfirmView, report_error
except ModuleNotFoundError as error:
    if error.name not in {"discord", "dotenv"}:
        raise
    discord = None


class FakeInteraction:
    """Enforce one initial response and capture private/public messages."""
    sequence = itertools.count(987654321)
    def __init__(self, *, command="balance", custom_id=None, channel_id=456):
        self.guild_id, self.channel_id = 123, channel_id
        self.id = next(self.sequence)
        self.extras = {}
        self.type = discord.InteractionType.component if custom_id else discord.InteractionType.application_command
        self.guild = SimpleNamespace(owner_id=99)
        self.user = Mock(spec=discord.Member)
        self.user.id, self.user.bot, self.user.roles = 1, False, []
        self.user.guild_permissions = SimpleNamespace(administrator=True)
        self.command = SimpleNamespace(name=command)
        self.data = {"custom_id": custom_id} if custom_id else {}
        self.done = False
        self.messages = []
        self.response = SimpleNamespace(
            type=None,
            is_done=lambda: self.done,
            defer=AsyncMock(side_effect=self.initial),
            send_message=AsyncMock(side_effect=self.initial),
            send_modal=AsyncMock(side_effect=self.initial),
        )
        self.followup = SimpleNamespace(send=AsyncMock(side_effect=self.followup_send))
        self.edit_original_response = AsyncMock(side_effect=self.edit_send)
        self.channel = SimpleNamespace(send=AsyncMock())

    async def initial(self, *args, **kwargs):
        if self.done:
            raise AssertionError("Interaction acknowledged twice")
        self.done = True
        if args:
            self.response.type = discord.InteractionResponseType.modal
        elif self.type == discord.InteractionType.component and not kwargs.get("thinking"):
            self.response.type = discord.InteractionResponseType.deferred_message_update
        else:
            self.response.type = discord.InteractionResponseType.deferred_channel_message

    async def edit_send(self, *, content=None, **kwargs):
        if not self.done:
            raise AssertionError("Edit before acknowledgement")
        self.messages.append((content, {"ephemeral": True, **kwargs}))
        return SimpleNamespace(id=self.id, content=content)

    async def followup_send(self, text=None, **kwargs):
        if not self.done:
            raise AssertionError("Followup before acknowledgement")
        self.messages.append((text, kwargs))


@contextmanager
def delayed_call(target, name):
    """Hold a real operation until the async test releases it; fail if loop stalls."""
    original = getattr(target, name)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()

    def delayed(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):
            raise AssertionError("Database work blocked the event loop")
        return original(*args, **kwargs)

    with patch.object(target, name, side_effect=delayed):
        try:
            yield entered, release
        finally:
            release.set()


@unittest.skipIf(discord is None, "Install requirements.txt for Discord handler tests")
class DiscordResponseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.sqlite3", 123)
        self.store.configure(456, 789, 0, 99)
        self.store.register(1, "Fix0r")
        self.store.adjust(1, 100, "Старт", 99, "seed")
        self.bot = DKPBot(123, self.store)
        self.commands = self.bot.tree.get_command("dkp", guild=discord.Object(id=123))

    async def asyncTearDown(self):
        await self.bot.close()

    async def invoke(self, name, i, **kwargs):
        command = self.commands.get_command(name)
        i.command = command
        try:
            await command._invoke_with_namespace(i, SimpleNamespace(**kwargs))
        except discord.app_commands.AppCommandError as error:
            await self.bot.tree.on_error(i, error)

    async def test_slash_balance_acknowledges_before_slow_guard_and_read(self):
        i = FakeInteraction()
        with delayed_call(self.store, "settings") as (entered, release):
            task = asyncio.create_task(self.invoke("balance", i))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                i.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
                self.assertFalse(task.done())
            finally:
                release.set()
                await task
        self.assertIn("100", i.messages[0][0])

        i = FakeInteraction()
        with delayed_call(self.store, "balance") as (entered, release):
            task = asyncio.create_task(self.invoke("balance", i))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                # Discord's initial response deadline passes while the DB is still busy.
                await asyncio.sleep(3.1)
                self.assertTrue(i.done)
                self.assertFalse(task.done())
                self.assertEqual(i.messages, [])
                # Another interaction can finish while this balance read is waiting.
                help_i = FakeInteraction(command="help")
                await asyncio.wait_for(self.invoke("help", help_i), 1)
                self.assertTrue(help_i.messages)
            finally:
                release.set()
                await task
        i.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.assertTrue(i.messages[0][1]["ephemeral"])
        self.assertIn("Fix0r", i.messages[0][0])

    async def test_balance_button_acknowledges_before_guard(self):
        i = FakeInteraction(custom_id="fc:balance:v1")
        panel = PanelView(self.bot)
        async def click():
            if await panel.interaction_check(i):
                await panel.balance.callback(i)
        with delayed_call(self.store, "settings") as (entered, release):
            task = asyncio.create_task(click())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                i.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
            finally:
                release.set()
                await task
        self.assertIn("100", i.messages[0][0])

    async def test_errors_are_delivered_after_acknowledgement(self):
        for channel_id, uid, expected in ((999, 1, "ДКП-канал"), (456, 2, "привязал ник")):
            with self.subTest(channel_id=channel_id, uid=uid):
                i = FakeInteraction(channel_id=channel_id)
                i.user.id = uid
                await self.invoke("balance", i)
                self.assertIn(expected, i.messages[0][0])
                i.response.send_message.assert_not_awaited()

    async def test_modal_openers_keep_the_initial_response_available(self):
        i = FakeInteraction(custom_id="fc:register:v1")
        view = PanelView(self.bot)
        self.assertTrue(await view.interaction_check(i))
        await view.register.callback(i)
        i.response.defer.assert_not_awaited()
        i.response.send_modal.assert_awaited_once()

        aid = self.store.create_auction("Щит", 10, 1, 5, 99)
        i = FakeInteraction(custom_id=f"fc:auction:{aid}:bid")
        view = AuctionView(self.bot, aid)
        self.assertTrue(await view.interaction_check(i))
        await view.bid.callback(i)
        i.response.defer.assert_not_awaited()
        modal = i.response.send_modal.call_args.args[0]
        self.assertEqual(modal.aid, aid)

    async def test_panel_stays_public_after_private_acknowledgement(self):
        i = FakeInteraction(command="panel")
        await self.invoke("panel", i)
        i.channel.send.assert_awaited_once()
        self.assertIsInstance(i.channel.send.call_args.kwargs["view"], PanelView)
        self.assertTrue(i.messages[0][1]["ephemeral"])

    async def test_background_database_work_does_not_stall_commands(self):
        with delayed_call(self.store, "close_due") as (entered, release):
            task = asyncio.create_task(self.bot.settlements.coro(self.bot))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                i = FakeInteraction()
                await asyncio.wait_for(self.invoke("balance", i), 1)
                self.assertIn("100", i.messages[0][0])
            finally:
                release.set()
                # Skip Discord publication after the operation finishes.
                with patch.object(self.store, "settings", return_value=None):
                    await task

    async def test_confirmation_cannot_be_repeated_or_cancelled_while_running(self):
        def apply():
            self.store.adjust(1, 10, "Проверка", 99, "confirm")
            return "Начислено"
        view = ConfirmView(self.bot, 1, apply)
        i = FakeInteraction()
        self.assertTrue(await view.interaction_check(i))
        i.response.defer.assert_awaited_once_with()
        with delayed_call(view, "action") as (entered, release):
            task = asyncio.create_task(view.confirm.callback(i))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                for button in (view.confirm, view.decline):
                    second = FakeInteraction()
                    self.assertFalse(await view.interaction_check(second))
                    # Also cover callbacks already queued before the first click claimed it.
                    with self.assertRaises(RuleError):
                        await button.callback(second)
            finally:
                release.set()
                await task
        self.assertEqual(self.store.balance(1)["total"], 110)
        i.edit_original_response.assert_awaited_once()
