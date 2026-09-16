"""Every command and UI operation through discord.py's checks/argument dispatch."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_bot as support
from store import RuleError

discord = support.discord
if discord is not None:
    from bot import PanelView, EventView, AuctionView, ConfirmView, RegisterModal, BidModal
    import responses


CASES = {
    "setup", "panel", "register", "rename", "balance", "top", "history", "journal", "adjust",
    "event", "event_add", "event_remove", "voice", "roster", "award", "event_cancel", "auction",
    "bid", "auctions", "cancel", "backup", "help", "status",
}


@unittest.skipIf(discord is None, "Install requirements.txt for Discord operation tests")
class OperationsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.DiscordResponseTests.asyncSetUp
    asyncTearDown = support.DiscordResponseTests.asyncTearDown
    invoke = support.DiscordResponseTests.invoke

    def member(self, uid=1, *, admin=True):
        member = Mock(spec=discord.Member)
        member.id, member.bot, member.roles = uid, False, []
        member.guild_permissions = SimpleNamespace(administrator=admin)
        return member

    async def click(self, view, button, i=None):
        i = i or support.FakeInteraction(custom_id=button.custom_id)
        await view._scheduled_task(button, i)
        return i

    async def submit(self, modal, field, value):
        i = support.FakeInteraction(custom_id=modal.custom_id)
        i.type = discord.InteractionType.modal_submit
        components = [{"type": 1, "components": [{"type": 4, "custom_id": field.custom_id, "value": value}]}]
        await modal._scheduled_task(i, components, {})
        return i

    async def exercise_command(self, name):
        self.store.register(2, "Player2")
        eid = self.store.create_event("ЧВ", 10, 1)
        self.store.attend(eid, [1], True, 1)
        aid = self.store.create_auction("Щит", 10, 1, 5, 1)
        i = support.FakeInteraction(command=name)
        member = self.member()
        text = Mock(spec=discord.TextChannel)
        text.id, text.mention = 456, "<#456>"
        text.permissions_for.return_value = SimpleNamespace(**{p: True for p in
            ("view_channel", "send_messages", "embed_links", "attach_files", "read_message_history")})
        role = Mock(spec=discord.Role)
        role.id, role.managed = 789, False
        role.is_default.return_value = False
        i.guild.me = self.member(99)
        voice = Mock(spec=discord.VoiceChannel)
        voice.id, voice.voice_states = 777, {1: None, 2: None}
        voice.permissions_for.return_value = SimpleNamespace(view_channel=True)
        i.guild.fetch_member = AsyncMock(side_effect=lambda uid: self.member(uid))
        args = {
            "setup": dict(channel=SimpleNamespace(resolve=lambda: text), officer_role=role),
            "register": dict(nickname="NewPlayer"),
            "rename": dict(member=member, nickname="Renamed"),
            "balance": dict(member=member),
            "adjust": dict(member=member, amount=10, reason="Участие"),
            "event": dict(title="ПБ", points=5),
            "event_add": dict(event_id=eid, member=self.member(2)),
            "event_remove": dict(event_id=eid, member=member),
            "voice": dict(event_id=eid, channel=SimpleNamespace(resolve=lambda: voice)),
            "roster": dict(event_id=eid), "award": dict(event_id=eid),
            "event_cancel": dict(event_id=eid),
            "auction": dict(item="Копьё", minimum=5, step=1, minutes=5),
            "bid": dict(auction_id=aid, amount=10),
            "cancel": dict(auction_id=aid, reason="Ошибка предмета"),
        }
        if name == "register":
            i.user.id = 3
        await self.invoke(name, i, **args.get(name, {}))
        self.assertFalse(i.extras.get("dkp_error_reported"), (name, i.messages))
        self.assertTrue(i.messages, name)
        self.assertTrue(i.done)
        view = i.messages[-1][1].get("view")
        if isinstance(view, ConfirmView):
            clicked = await self.click(view, view.confirm)
            self.assertFalse(clicked.extras.get("dkp_error_reported"), clicked.messages)
            self.assertTrue(clicked.messages)
        if name == "register":
            self.assertEqual(self.store.balance(3)["nickname"], "NewPlayer")
        elif name == "rename":
            self.assertEqual(self.store.balance(1)["nickname"], "Renamed")
        elif name in ("award", "adjust"):
            self.assertEqual(self.store.balance(1)["total"], 110)
        elif name in ("event_add", "voice"):
            self.assertEqual(len(self.store.attendees(eid)), 2)
        elif name == "event_remove":
            self.assertEqual(self.store.attendees(eid), [])
        elif name == "event_cancel":
            self.assertEqual(self.store.entity("events", eid)["status"], "cancelled")
        elif name == "cancel":
            self.assertEqual(self.store.entity("auctions", aid)["status"], "cancelled")
        elif name == "bid":
            self.assertEqual(self.store.balance(1)["reserved"], 10)
        elif name == "backup":
            self.assertTrue(list((self.store.path.parent / "backups").glob("manual-*.sqlite3")))
        elif name == "event":
            self.assertEqual(len(self.store.rows("SELECT id FROM events")), 2)
        elif name == "auction":
            self.assertEqual(len(self.store.rows("SELECT id FROM auctions")), 2)
        elif name == "setup":
            self.assertEqual(self.bot._modal_settings, self.store.settings())
        elif name == "panel":
            i.channel.send.assert_awaited_once()

    async def test_coverage_contains_every_registered_command(self):
        self.assertEqual({c.name for c in self.commands.commands}, CASES)

    async def test_all_panel_buttons(self):
        panel = PanelView(self.bot)
        for button in (panel.balance, panel.top, panel.history):
            i = await self.click(panel, button)
            self.assertTrue(i.messages)
            self.assertFalse(i.extras.get("dkp_error_reported"))
        # Neither configuration nor auction reads may delay opening a form.
        with patch.object(self.store, "settings", side_effect=AssertionError("DB before modal")):
            i = await self.click(panel, panel.register)
        i.response.send_modal.assert_awaited_once()

    async def test_all_event_buttons_and_decline(self):
        eid = self.store.create_event("ЧВ", 10, 1)
        view = EventView(self.bot, eid)
        await self.click(view, view.join)
        self.assertEqual(len(self.store.attendees(eid)), 1)
        await self.click(view, view.leave)
        self.assertEqual(self.store.attendees(eid), [])
        await self.click(view, view.join)
        self.assertTrue((await self.click(view, view.roster)).messages)
        i = await self.click(view, view.award)
        confirmation = i.messages[-1][1]["view"]
        await self.click(confirmation, confirmation.decline)
        self.assertEqual(self.store.balance(1)["total"], 100)
        i = await self.click(view, view.award)
        confirmation = i.messages[-1][1]["view"]
        await self.click(confirmation, confirmation.confirm)
        self.assertEqual(self.store.balance(1)["total"], 110)

    async def test_registration_and_bid_modal_submissions(self):
        modal = RegisterModal(self.bot)
        i = support.FakeInteraction(custom_id=modal.custom_id)
        i.type, i.user.id = discord.InteractionType.modal_submit, 2
        await modal._scheduled_task(i, [{"type": 1, "components": [
            {"type": 4, "custom_id": modal.nickname.custom_id, "value": "Player2"}]}], {})
        self.assertEqual(self.store.balance(2)["nickname"], "Player2")
        self.assertTrue(i.messages)
        aid = self.store.create_auction("Щит", 10, 1, 5, 1)
        view = AuctionView(self.bot, aid)
        with patch.object(self.store, "entity", side_effect=AssertionError("DB before modal")):
            i = await self.click(view, view.bid)
        modal = i.response.send_modal.call_args.args[0]
        i = await self.submit(modal, modal.amount, "15")
        self.assertEqual(self.store.balance(1)["reserved"], 15)
        self.assertTrue(i.messages)

    async def test_invalid_modal_and_insufficient_points_return_errors(self):
        aid = self.store.create_auction("Щит", 10, 1, 5, 1)
        for value in ("abc", "1000"):
            modal = BidModal(self.bot, aid)
            i = await self.submit(modal, modal.amount, value)
            self.assertTrue(i.messages)
            self.assertTrue(i.extras.get("dkp_error_reported"))
        self.assertEqual(self.store.balance(1)["reserved"], 0)

    async def test_expired_unknown_and_restarted_controls_receive_a_reply(self):
        view = ConfirmView(self.bot, 1, lambda: self.fail("Expired action ran"))
        await view.on_timeout()
        i = await self.click(view, view.confirm)
        self.assertTrue(i.messages)
        for kind in (discord.InteractionType.component, discord.InteractionType.modal_submit):
            i = support.FakeInteraction(custom_id="old-control")
            i.type = kind
            await self.bot.on_interaction(i)
            self.assertIn("неактивна", i.messages[0][0])

    async def test_retrying_reply_does_not_repeat_award(self):
        eid = self.store.create_event("ЧВ", 10, 1)
        self.store.attend(eid, [1], True, 1)
        i = support.FakeInteraction(command="award")
        await self.invoke("award", i, event_id=eid)
        view = i.messages[-1][1]["view"]
        click = support.FakeInteraction(custom_id=view.confirm.custom_id)
        attempts = 0
        async def send(**kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("simulated dropped HTTP connection")
            return await click.edit_send(**kwargs)
        click.edit_original_response.side_effect = send
        await self.click(view, view.confirm, click)
        self.assertEqual(attempts, 2)
        self.assertEqual(self.store.balance(1)["total"], 110)
        self.assertEqual(len(self.store.history(1, 1)), 2)

    async def test_slow_or_rejected_ack_prevents_mutations(self):
        for mode in ("timeout", "expired"):
            i = support.FakeInteraction(command="event")
            async def ack(**kwargs):
                if mode == "timeout":
                    await asyncio.sleep(10)
                raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"code": 10062, "message": "Unknown interaction"})
            i.response.defer.side_effect = ack
            i.edit_original_response.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"code": 10015, "message": "Unknown webhook"})
            with patch.object(responses, "ACK_TIMEOUT", 0.02):
                await self.invoke("event", i, title="ЧВ", points=10)
            self.assertEqual(self.store.rows("SELECT id FROM events"), [])

    async def test_voice_failure_does_not_save_partial_roster(self):
        eid = self.store.create_event("ЧВ", 10, 1)
        i = support.FakeInteraction(command="voice")
        i.guild.me = self.member(99)
        i.guild.fetch_member = AsyncMock(side_effect=TimeoutError())
        voice = Mock(spec=discord.VoiceChannel)
        voice.voice_states = {1: None, 2: None}
        voice.permissions_for.return_value = SimpleNamespace(view_channel=True)
        await self.invoke("voice", i, event_id=eid, channel=SimpleNamespace(resolve=lambda: voice))
        self.assertEqual(self.store.attendees(eid), [])
        self.assertTrue(i.messages)
        self.assertIn("Список не изменён", i.messages[0][0])

    async def test_modal_delivery_timeout_finishes_handler_without_changes(self):
        panel = PanelView(self.bot)
        i = support.FakeInteraction(custom_id=panel.register.custom_id)
        async def stalled(modal):
            await asyncio.sleep(10)
        i.response.send_modal.side_effect = stalled
        i.edit_original_response.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), {"code": 10015, "message": "Unknown webhook"})
        with patch.object(responses, "ACK_TIMEOUT", 0.02):
            await asyncio.wait_for(self.click(panel, panel.register, i), 1)
        self.assertTrue(i.extras["dkp_ack_failed"])
        self.assertEqual(len(self.store.rows("SELECT user_id FROM members")), 1)

    async def test_long_unicode_text_stays_within_message_limits(self):
        aid = self.store.create_auction("😀" * 150, 10, 1, 5, 1)
        embed = self.bot.auction_embed(self.store.entity("auctions", aid))
        self.assertLessEqual(len(embed.title.encode("utf-16-le")) // 2, 256)
        for n in range(20):
            self.store.adjust(1, 1, "😀" * 200, 1, f"unicode-{n}")
        i = support.FakeInteraction(command="history")
        await self.invoke("history", i)
        embed = i.messages[0][1]["embed"]
        self.assertLessEqual(len(embed.description.encode("utf-16-le")) // 2, 4096)


def command_test(name):
    async def test(self):
        await self.exercise_command(name)
    return test


for command_name in sorted(CASES):
    setattr(OperationsTests, "test_command_" + command_name, command_test(command_name))
