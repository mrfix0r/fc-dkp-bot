"""Discord library smoke check, without token, internet, or a real guild."""
import asyncio
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock

from bot import DKPBot, EventView, AuctionView, PanelView, BidModal, RegisterModal, ConfirmView
from store import Store


async def main():
    with tempfile.TemporaryDirectory() as temp:
        store = Store(Path(temp) / "test.sqlite3", 123456789)
        bot = DKPBot(123456789, store)
        commands = bot.tree.get_commands(guild=__import__("discord").Object(id=bot.guild_id))
        payload = commands[0].to_dict(bot.tree)
        assert payload["name"] == "dkp" and 1 <= len(payload["options"]) <= 25
        assert all(len(c["description"]) <= 100 for c in payload["options"])
        eid = store.create_event("Тест ЧВ", 10, 1)
        aid = store.create_auction("Щит", 10, 1, 5, 1)
        for view in (PanelView(bot), EventView(bot, eid), AuctionView(bot, aid)):
            assert view.is_persistent()
            assert all(len(child.custom_id) <= 100 for child in view.children)
            bot.add_view(view)
        RegisterModal(bot)
        BidModal(bot, aid)
        ConfirmView(bot, 1, lambda: "OK")
        assert len(bot.event_embed(store.entity("events", eid), store.attendees(eid))) < 6000
        assert len(bot.auction_embed(store.entity("auctions", aid))) < 6000
        # Restore persistent views and sync the guild-scoped command schema offline.
        bot.tree.sync = AsyncMock(return_value=[])
        bot.worker.start = lambda: None
        bot.settlements.start = lambda: None
        bot.backups.start = lambda: None
        await bot.setup_hook()
        assert bot.tree.sync.await_count == 1
        await bot.close()
        print(f"Discord interface OK: {len(payload['options'])} commands, persistent buttons, modals, restart setup.")


if __name__ == "__main__":
    asyncio.run(main())
