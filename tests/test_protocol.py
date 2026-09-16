"""Real discord.Interaction, CommandTree, ViewStore and HTTP payload serialization."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_bot as support

discord = support.discord
if discord is not None:
    from discord.webhook.async_ import async_context
    from bot import PanelView, EventView, BidModal
    import responses


class OfflineAdapter:
    def __init__(self):
        self.initial = []
        self.edits = []
        self.files = []
        self.fail_edits = 0

    async def create_interaction_response(self, iid, token, *, params, **kwargs):
        self.initial.append(params.payload)
        return {"interaction": {"id": str(iid), "response_message_id": str(iid + 1)}}

    async def edit_original_interaction_response(self, application_id, token, *, payload, multipart, files, **kwargs):
        self.files.append([f.fp.read() for f in files or []])
        if self.fail_edits:
            self.fail_edits -= 1
            raise OSError("simulated connection reset")
        self.edits.append(payload or multipart)
        return {"id": "9876543210", "channel_id": "456", "type": 0,
                "author": {"id": "99", "username": "FC DKP", "discriminator": "0000", "avatar": None, "bot": True},
                "content": (payload or {}).get("content", ""), "attachments": [], "embeds": [],
                "timestamp": datetime.now(timezone.utc).isoformat(), "edited_timestamp": None,
                "tts": False, "mention_everyone": False, "mentions": [], "mention_roles": [], "flags": 64}


@unittest.skipIf(discord is None, "Install requirements.txt for protocol tests")
class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await support.DiscordResponseTests.asyncSetUp(self)
        await self.bot._async_setup_hook()
        self.bot._connection.user = discord.ClientUser(state=self.bot._connection,
            data={"id": "99", "username": "FC DKP", "discriminator": "0000", "avatar": None, "bot": True})
        guild = discord.Guild(state=self.bot._connection, data={"id": "123", "name": "Test", "owner_id": "1",
            "roles": [{"id": "123", "name": "@everyone", "permissions": "8", "position": 0}],
            "channels": [{"id": "456", "type": 0, "name": "dkp", "position": 0}]})
        self.bot._connection._add_guild(guild)
        self.adapter = OfflineAdapter()
        self.context = async_context.set(self.adapter)

    async def asyncTearDown(self):
        async_context.reset(self.context)
        await support.DiscordResponseTests.asyncTearDown(self)

    def interaction(self, *, command="balance", kind=2, custom_id=None, channel_id=456, options=None, message_id=None):
        iid = discord.utils.time_snowflake(datetime.now(timezone.utc))
        data = {"id": "333", "name": "dkp", "guild_id": "123", "type": 1,
                "options": [{"name": command, "type": 1, "options": options or []}]}
        if custom_id:
            data = {"custom_id": custom_id, "component_type": 2}
        payload = {"id": str(iid), "type": kind, "application_id": "99", "token": "offline-test-token",
            "version": 1, "guild_id": "123", "channel": {"id": str(channel_id), "type": 0, "name": "dkp", "position": 0},
            "data": data, "attachment_size_limit": 10485760,
            "member": {"user": {"id": "1", "username": "Fix0r", "discriminator": "0000", "avatar": None},
                       "roles": [], "flags": 0, "permissions": "8", "joined_at": None, "deaf": False, "mute": False},
            "locale": "ru", "guild_locale": "ru", "app_permissions": "8"}
        if message_id:
            payload["message"] = {"id": str(message_id), "channel_id": str(channel_id), "type": 0,
                "author": {"id": "99", "username": "FC DKP", "discriminator": "0000", "avatar": None},
                "content": "", "attachments": [], "embeds": [], "timestamp": None, "edited_timestamp": None,
                "tts": False, "mention_everyone": False, "mentions": [], "mention_roles": []}
        return discord.Interaction(data=payload, state=self.bot._connection)

    async def test_real_command_dispatch_acknowledges_and_edits_private_original(self):
        i = self.interaction()
        await self.bot.tree._call(i)
        self.assertEqual(self.adapter.initial, [{"type": 5, "data": {"flags": 64}}])
        self.assertIn("100", self.adapter.edits[0]["content"])
        self.assertEqual(len(self.adapter.edits), 1)

    async def test_real_group_error_finishes_response_instead_of_escaping(self):
        i = self.interaction(channel_id=999)
        await self.bot.tree._call(i)
        self.assertEqual(len(self.adapter.initial), 1)
        self.assertEqual(len(self.adapter.edits), 1)
        self.assertIn("ДКП-канал", self.adapter.edits[0]["content"])

    async def test_real_file_upload_survives_failed_first_attempt(self):
        eid = self.store.create_event("ЧВ", 10, 1)
        self.store.attend(eid, [1], True, 1)
        i = self.interaction(command="roster", options=[{"name": "event_id", "type": 4, "value": eid}])
        self.adapter.fail_edits = 1
        await self.bot.tree._call(i)
        self.assertEqual(len(self.adapter.files), 2)
        self.assertEqual(self.adapter.files[0], self.adapter.files[1])
        self.assertIn(b"Fix0r", self.adapter.files[1][0])
        self.assertEqual(len(self.adapter.edits), 1)

    async def test_real_view_dispatch_and_fallback_do_not_double_acknowledge(self):
        self.bot.add_view(PanelView(self.bot))
        i = self.interaction(kind=3, custom_id="fc:balance:v1", message_id=55)
        view_store = self.bot._connection._view_store
        view_store.dispatch_view(2, "fc:balance:v1", i)
        await self.bot.on_interaction(i)
        for _ in range(100):
            if self.adapter.edits:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(self.adapter.initial), 1)
        self.assertIn("100", self.adapter.edits[0]["content"])

    async def test_real_missing_view_dispatch_gets_fallback_response(self):
        i = self.interaction(kind=3, custom_id="expired-control", message_id=55)
        self.bot._connection._view_store.dispatch_view(2, "expired-control", i)
        await self.bot.on_interaction(i)
        self.assertEqual(len(self.adapter.initial), 1)
        self.assertIn("неактивна", self.adapter.edits[0]["content"])

    async def test_real_modal_opener_responds_with_type_nine_without_disk(self):
        panel = PanelView(self.bot)
        i = self.interaction(kind=3, custom_id="fc:register:v1", message_id=55)
        with patch.object(self.store, "settings", side_effect=AssertionError("Unexpected disk access")):
            await panel._scheduled_task(panel.register, i)
        self.assertEqual(self.adapter.initial[0]["type"], 9)
        self.assertEqual(len(self.adapter.initial), 1)


if __name__ == "__main__":
    unittest.main()
