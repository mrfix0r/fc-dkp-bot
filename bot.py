"""FC DKP Discord interface. See README.md for Windows setup."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import sqlite3

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from store import RuleError, Store
from policy import authorize
from database_io import DatabaseIO
from responses import (acknowledge, reply, edit_response, open_modal, handled, record,
                       AcknowledgementFailed, recover_ack_failure)

ROOT = Path(__file__).resolve().parent
GREEN = 0x66CC88
LOG = logging.getLogger("fc_dkp")
VERSION = "1.0.5"


def safe(value):
    return discord.utils.escape_markdown(discord.utils.escape_mentions(str(value)))


def clip(value, limit):
    # Stay within Discord text limits even for astral Unicode and escaped Markdown.
    return value.encode("utf-16-le")[:limit * 2].decode("utf-16-le", errors="ignore")


def who(uid):
    return f"<@{uid}>" if uid else "Автоматически"


async def report_error(i, error):
    if i.extras.get("dkp_error_reported"):
        return
    i.extras["dkp_error_reported"] = True
    error = getattr(error, "original", error)
    record(i, "operation_failed", error)
    if isinstance(error, AcknowledgementFailed):
        await recover_ack_failure(i)
        return
    if isinstance(error, RuleError):
        message = str(error)
    elif isinstance(error, app_commands.CheckFailure):
        message = "Действие недоступно. Проверьте роль и выбранный ДКП-канал."
    elif isinstance(error, app_commands.TransformerError):
        message = "Не удалось найти выбранного участника или канал, либо параметр неверен. Выберите значение заново в команде."
    elif isinstance(error, (app_commands.CommandNotFound, app_commands.CommandSignatureMismatch)):
        message = "Список команд обновился. Закройте выбор команды и выберите /dkp заново."
    elif isinstance(error, sqlite3.OperationalError):
        message = "База временно недоступна. Проверьте баланс и журнал перед повтором операции."
    elif isinstance(error, (TimeoutError, aiohttp.ClientError, OSError)):
        message = "Связь с Discord прервалась или ответ задержался. Проверьте баланс и журнал перед повтором операции."
    elif isinstance(error, (discord.Forbidden, discord.NotFound)):
        message = "Не хватает доступа к каналу/сообщению или оно удалено. Проверьте права бота."
    else:
        message = "Операция не завершилась штатно. Проверьте баланс и журнал перед повтором. Тип ошибки записан в logs/bot.log."
    try:
        await reply(i, message)
    except (discord.HTTPException, aiohttp.ClientError, OSError, TimeoutError, AcknowledgementFailed) as delivery_error:
        record(i, "error_delivery_failed", delivery_error)


async def discord_call(awaitable):
    """Bound ordinary REST calls after the interaction has been acknowledged."""
    async with asyncio.timeout(15):
        return await awaitable


class DKPCommandTree(app_commands.CommandTree):
    async def interaction_check(self, i):
        handled(i)
        try:
            await acknowledge(i)
            return True
        except Exception as error:
            await report_error(i, error)
            return False


def journal_text(row):
    d = json.loads(row["data"])
    kind = row["kind"]
    if kind == "settings":
        body = f"Настройки: офицеры <@&{d['officer_role_id']}>, роль участников: " + (f"<@&{d['member_role_id']}>" if d["member_role_id"] else "все участники сервера")
    elif kind == "nickname":
        body = f"{who(d['user_id'])}: ник **{safe(d['nickname'])}**"
        if d.get("previous"):
            body += f" (был {safe(d['previous'])})"
    elif kind == "adjust":
        body = f"{who(d['user_id'])}: **{d['amount']:+} ДКП** · {safe(d['reason'])}"
    elif kind == "attendance":
        body = f"Событие #{d['event_id']}: {'добавлено' if d['present'] else 'удалено'} участников: {len(d['user_ids'])} · "
        body += ", ".join(who(uid) for uid in d["user_ids"][:25])
        if len(d["user_ids"]) > 25:
            body += " … полный состав: /dkp roster"
    elif kind == "event_created":
        body = f"Событие #{d['event_id']}: {safe(d['title'])} · +{d['points']} ДКП"
    elif kind == "event_awarded":
        body = f"Событие #{d['event_id']}: **+{d['points']} ДКП** каждому из **{len(d['user_ids'])}** участников. Состав: /dkp roster"
    elif kind == "event_cancelled":
        body = f"Событие #{d['event_id']} отменено без начисления."
    elif kind == "auction_created":
        body = f"Аукцион #{d['auction_id']}: {safe(d['item'])}"
    elif kind == "bid":
        body = f"Аукцион #{d['auction_id']}: ставка **{d['amount']} ДКП**"
    elif kind == "auction_closed":
        body = f"Аукцион #{d['auction_id']}: **{safe(d['item'])}**. "
        body += f"Победитель {who(d['user_id'])}, списано **{d['amount']} ДКП**. Предмет передаёт офицер в игре." if d["user_id"] else "Завершён без ставок."
    elif kind == "auction_cancelled":
        body = f"Аукцион #{d['auction_id']} отменён, резерв освобождён. Причина: {safe(d['reason'])}"
    else:
        body = kind
    return f"**Запись #{row['id']}** · <t:{row['created_at']}:f> · {who(row['actor_id'])}\n{body}"


def text_file(text, name):
    return discord.File(io.BytesIO(text.encode("utf-8-sig")), filename=name)


def roster_file(event, people):
    lines = [f"Событие #{event['id']}: {event['title']}", f"Статус: {event['status']}; награда: {event['points']}; участников: {len(people)}", "", "Discord ID\tИгровой ник"]
    lines += [f"{p['user_id']}\t{p['nickname']}" for p in people]
    return text_file("\n".join(lines)+"\n", f"event-{event['id']}-roster.txt")


class SafeView(discord.ui.View):
    defer_update = False

    def __init__(self, bot, *, timeout=None):
        super().__init__(timeout=timeout)
        self.bot = bot

    async def interaction_check(self, i):
        handled(i)
        try:
            if self.opens_modal(i):
                self.bot.guard_modal(i)
            else:
                await acknowledge(i, update=self.defer_update)
                await self.bot.guard(i)
            return True
        except Exception as error:
            await report_error(i, error)
            return False

    def opens_modal(self, i):
        return False

    async def on_error(self, i, error, item):
        await report_error(i, error)


class SafeModal(discord.ui.Modal):
    async def interaction_check(self, i):
        handled(i)
        try:
            await acknowledge(i)
            return True
        except Exception as error:
            await report_error(i, error)
            return False

    async def on_error(self, i, error):
        await report_error(i, error)


class RegisterModal(SafeModal, title="Привязать игровой ник"):
    nickname = discord.ui.TextInput(label="Ник в RF Online", min_length=1, max_length=32)

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, i):
        await acknowledge(i)
        await self.bot.guard(i)
        await self.bot.db.register(i.user.id, self.nickname.value)
        await reply(i, f"Ник **{safe(self.nickname.value)}** привязан.")


class BidModal(SafeModal, title="Ставка ДКП"):
    amount = discord.ui.TextInput(label="Полная сумма ставки (целое число)", min_length=1, max_length=10)

    def __init__(self, bot, aid, minimum=None):
        super().__init__()
        self.bot, self.aid = bot, aid
        self.amount.placeholder = str(minimum) if minimum is not None else "Например, 25"

    async def on_submit(self, i):
        await acknowledge(i)
        await self.bot.guard(i)
        try:
            amount = int(self.amount.value.strip())
        except ValueError:
            raise RuleError("Введите целое число, например 25.")
        await self.bot.db.bid(self.aid, i.user.id, amount)
        await reply(i, f"Ставка **{amount} ДКП** принята на аукционе #{self.aid}. Очки зарезервированы.")


class ConfirmView(SafeView):
    defer_update = True

    def __init__(self, bot, owner_id, action):
        super().__init__(bot, timeout=180)
        self.owner_id, self.action, self.used = owner_id, action, False
        self.expired = False

    async def interaction_check(self, i):
        handled(i)
        if i.user.id != self.owner_id:
            await reply(i, "Это подтверждение другого офицера.")
            return False
        if self.used or self.expired:
            await reply(i, "Это подтверждение уже обработано. Проверьте журнал операции.")
            return False
        return await super().interaction_check(i)

    @discord.ui.button(label="Подтвердить", style=discord.ButtonStyle.success)
    async def confirm(self, i, button):
        await self.bot.guard(i, officer=True)
        if self.used or self.expired:
            raise RuleError("Подтверждение уже использовано.")
        # Claim before yielding: a second click/cancel must not race the transaction.
        self.used = True
        try:
            result = await self.bot.db.call(self.action, write=True)
        except RuleError:
            self.used = False
            raise
        for child in self.children:
            child.disabled = True
        self.stop()
        await edit_response(i, content=result, view=self, embeds=[], attachments=[])

    @discord.ui.button(label="Не применять", style=discord.ButtonStyle.secondary)
    async def decline(self, i, button):
        if self.used or self.expired:
            raise RuleError("Это подтверждение уже обработано.")
        self.used = True
        self.stop()
        await edit_response(i, content="Изменения не применены.", view=None, embeds=[], attachments=[])

    async def on_timeout(self):
        if self.used:
            return
        self.expired = True
        for child in self.children:
            child.disabled = True
        origin = getattr(self, "response_interaction", None)
        if origin:
            try:
                await edit_response(origin, content="Время подтверждения истекло. Изменения не применены. Выполните команду заново.", view=self, embeds=[], attachments=[])
            except (discord.HTTPException, aiohttp.ClientError, OSError, TimeoutError):
                pass  # Old clicks still receive a response from on_interaction.


class PanelView(SafeView):
    def opens_modal(self, i):
        return (i.data or {}).get("custom_id") == "fc:register:v1"

    @discord.ui.button(label="Привязать ник", style=discord.ButtonStyle.success, custom_id="fc:register:v1")
    async def register(self, i, button):
        await open_modal(i, RegisterModal(self.bot))

    @discord.ui.button(label="Мой баланс", style=discord.ButtonStyle.primary, custom_id="fc:balance:v1")
    async def balance(self, i, button):
        await self.bot.show_balance(i, i.user.id)

    @discord.ui.button(label="Таблица ДКП", custom_id="fc:top:v1")
    async def top(self, i, button):
        await self.bot.show_top(i, 1)

    @discord.ui.button(label="Моя история", custom_id="fc:history:v1")
    async def history(self, i, button):
        await self.bot.show_history(i, i.user.id, 1)


class EventView(SafeView):
    def __init__(self, bot, eid):
        super().__init__(bot)
        self.eid = eid
        for child in self.children:
            child.custom_id = f"fc:event:{eid}:{child.custom_id}"

    @discord.ui.button(label="Я участвую", style=discord.ButtonStyle.success, custom_id="join")
    async def join(self, i, button):
        await self.bot.db.attend(self.eid, [i.user.id], True, i.user.id)
        await reply(i, "Ты в списке. ДКП начислятся после проверки офицером.")

    @discord.ui.button(label="Убрать меня", custom_id="leave")
    async def leave(self, i, button):
        await self.bot.db.attend(self.eid, [i.user.id], False, i.user.id)
        await reply(i, "Ты убран из списка.")

    @discord.ui.button(label="Состав", custom_id="roster")
    async def roster(self, i, button):
        await self.bot.show_roster(i, self.eid)

    @discord.ui.button(label="Проверить и начислить", style=discord.ButtonStyle.primary, custom_id="award")
    async def award(self, i, button):
        await self.bot.confirm_award(i, self.eid)


class AuctionView(SafeView):
    def opens_modal(self, i):
        return True

    def __init__(self, bot, aid):
        super().__init__(bot)
        self.aid = aid
        self.children[0].custom_id = f"fc:auction:{aid}:bid"

    @discord.ui.button(label="Сделать ставку", style=discord.ButtonStyle.success, custom_id="bid")
    async def bid(self, i, button):
        # A modal is the initial response. Validate the current auction at submission.
        await open_modal(i, BidModal(self.bot, self.aid))


class DKPBot(discord.Client):
    def __init__(self, guild_id, store):
        intents = discord.Intents.none()
        intents.guilds, intents.voice_states = True, True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.guild_id, self.store = guild_id, store
        self.db = DatabaseIO(store)
        self._modal_settings = store.settings()
        self.tree = DKPCommandTree(self)
        self.tree.add_command(DKPCommands(self), guild=discord.Object(id=guild_id))
        self.tree.on_error = report_error
        self.last_backup_day = None

    async def setup_hook(self):
        self.add_view(PanelView(self))
        for table, view in (("events", EventView), ("auctions", AuctionView)):
            for row in await self.db.rows(f"SELECT id,message_id FROM {table} WHERE status='open'"):
                self.add_view(view(self, row["id"]), message_id=row["message_id"])
        await self.tree.sync(guild=discord.Object(id=self.guild_id))
        self.worker.start()
        self.settlements.start()
        self.backups.start()

    async def on_ready(self):
        print(f"FC DKP {VERSION} online. Guild ID: {self.guild_id}. Use /dkp setup in Discord.", flush=True)
        LOG.info("gateway_ready version=%s guild=%s", VERSION, self.guild_id)

    async def on_disconnect(self):
        LOG.warning("gateway_disconnected")

    async def on_resumed(self):
        LOG.info("gateway_resumed")

    async def on_interaction(self, i):
        if i.type not in (discord.InteractionType.component, discord.InteractionType.modal_submit):
            return
        # discord.py schedules normal views/modals before this event. Give those tasks
        # one turn to claim the request; discarded/expired controls have no handler.
        await asyncio.sleep(0)
        if "dkp_started" in i.extras:
            return
        handled(i)
        try:
            await reply(i, "Эта кнопка или форма уже неактивна. Проверьте /dkp history и /dkp journal, если подтверждали операцию. Для нового действия вызовите команду заново.")
        except Exception as error:
            await report_error(i, error)

    async def close(self):
        jobs = [self.worker, self.settlements, self.backups]
        running = [job.get_task() for job in jobs if job.get_task() is not None]
        for job in jobs:
            job.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        await self.db.close()
        await super().close()

    def guard_modal(self, i):
        # Configuration is refreshed by /dkp setup; interaction roles are always current.
        # Never read disk or fetch members before answering with a modal.
        self.authorize_interaction(i, self._modal_settings)

    def authorize_interaction(self, i, settings, **options):
        is_member = isinstance(i.user, discord.Member)
        is_admin = is_member and (i.user.guild_permissions.administrator or (i.guild and i.user.id == i.guild.owner_id))
        authorize(guild_id=i.guild_id, expected_guild_id=self.guild_id,
                  is_human_member=is_member and not i.user.bot, is_admin=bool(is_admin),
                  role_ids={role.id for role in i.user.roles} if is_member else set(),
                  channel_id=i.channel_id, settings=settings, **options)

    async def guard(self, i, *, officer=False, admin=False, require_setup=True):
        self.authorize_interaction(i, await self.db.settings(), officer=officer, admin=admin, require_setup=require_setup)

    async def eligible(self, member):
        s = await self.db.settings()
        roles = {r.id for r in member.roles}
        return not member.bot and (not s["member_role_id"] or s["member_role_id"] in roles or member.guild_permissions.administrator or s["officer_role_id"] in roles)

    async def show_balance(self, i, uid):
        await acknowledge(i)
        d = await self.db.balance(uid)
        await reply(i, f"**{safe(d['nickname'])}** · {who(uid)}\nБаланс: **{d['total']} ДКП**\nВ ставках: **{d['reserved']}**\nДоступно: **{d['available']}**")

    async def show_top(self, i, page):
        rows = await self.db.leaderboard(page)
        lines = [f"{(page-1)*20+n}. **{safe(r['nickname'])}** — {r['total']} ДКП · свободно {r['total']-r['reserved']}" for n, r in enumerate(rows, 1)]
        embed = discord.Embed(title=f"FC · Таблица ДКП · страница {page}", description="\n".join(lines) or "Пока пусто.", color=GREEN)
        embed.set_footer(text="Другие страницы: /dkp top page:2")
        await reply(i, embed=embed)

    async def show_history(self, i, uid, page):
        rows = await self.db.history(uid, page)
        lines = [f"**#{r['id']} · {r['amount']:+} ДКП** · <t:{r['created_at']}:f>\n{safe(r['reason'])} · {who(r['actor_id'])}" for r in rows]
        embed = discord.Embed(title=f"История ДКП · страница {page}", description=clip("\n\n".join(lines) or "Начислений и списаний пока нет.", 4096), color=GREEN)
        embed.set_footer(text="Другие страницы: /dkp history page:2")
        await reply(i, embed=embed)

    async def show_roster(self, i, eid):
        event, people = await self.db.event_snapshot(eid)
        await reply(i, f"**#{eid} · {safe(event['title'])}**\nУчастников: **{len(people)}**, по **{event['points']} ДКП**.", file=roster_file(event, people))

    async def confirm_award(self, i, eid):
        await self.guard(i, officer=True)
        event, people = await self.db.event_snapshot(eid)
        if event["status"] != "open" or not people:
            raise RuleError("Нужно открытое событие хотя бы с одним участником.")
        def apply():
            count = self.store.award_event(eid, event["revision"], i.user.id)
            return f"Начислено по **{event['points']} ДКП** каждому из **{count}** участников события #{eid}."
        await reply(i, f"Проверьте приложенный полный состав **#{eid} · {safe(event['title'])}**.\nНачисление: **+{event['points']} ДКП × {len(people)} участников**.\nУдалить лишнего: /dkp event_remove. Подтверждение действует 3 минуты.", file=roster_file(event, people), view=ConfirmView(self, i.user.id, apply))

    def event_embed(self, event, people):
        status = {"open":"Сбор участников", "awarded":"ДКП начислены", "cancelled":"Отменено"}[event["status"]]
        embed = discord.Embed(title=clip(f"Событие #{event['id']} · {safe(event['title'])}", 256), description=f"**{status}**\nНаграда: **{event['points']} ДКП** каждому\nУчастников: **{len(people)}**", color=GREEN)
        names = ", ".join(safe(p["nickname"]) for p in people[:15]) or "Пока никто не отметился."
        embed.add_field(name="Состав", value=clip(names, 1000)+(" …" if len(people)>15 else ""), inline=False)
        embed.set_footer(text=f"ДКП после проверки офицером · полный состав: /dkp roster event_id:{event['id']}")
        return embed

    def auction_embed(self, a):
        status = {"open":"Приём ставок", "closed":"Завершён", "cancelled":"Отменён"}[a["status"]]
        leader = f"{who(a['highest_user'])} · **{a['highest_bid']} ДКП**" if a["highest_user"] else "Ставок пока нет"
        required = a["minimum"] if a["highest_user"] is None else a["highest_bid"]+a["step"]
        body = f"**{status}**\n{leader}\n"
        if a["status"] == "open":
            body += f"Следующая ставка: **от {required} ДКП**\nШаг: **{a['step']}** · конец <t:{a['ends_at']}:F> (<t:{a['ends_at']}:R>)\nСтавка в последние 30 секунд оставляет ещё 30 секунд на ответ."
        elif a["status"] == "closed":
            body += "ДКП победителя списаны. Предмет передаёт офицер в игре." if a["highest_user"] else "Списания ДКП не было."
        else:
            body += "Резерв освобождён, списания нет."
        return discord.Embed(title=clip(f"Аукцион #{a['id']} · {safe(a['item'])}", 256), description=body, color=GREEN)

    @tasks.loop(seconds=5)
    async def worker(self):
        try:
            settings = await self.db.settings()
            if not settings:
                return
            channel = self.get_channel(settings["channel_id"]) or await discord_call(self.fetch_channel(settings["channel_id"]))
            for table, view_cls in (("events", EventView), ("auctions", AuctionView)):
                for row in await self.db.dirty(table):
                    people = await self.db.attendees(row["id"]) if table == "events" else []
                    embed = self.event_embed(row, people) if table == "events" else self.auction_embed(row)
                    view = view_cls(self, row["id"]) if row["status"] == "open" else None
                    message = channel.get_partial_message(row["message_id"]) if row["message_id"] else None
                    try:
                        if message:
                            await discord_call(message.edit(embed=embed, view=view))
                        else:
                            message = await discord_call(channel.send(embed=embed, view=view))
                    except discord.NotFound:
                        message = await discord_call(channel.send(embed=embed, view=view))
                    await self.db.rendered(table, row["id"], row["revision"], message.id)
            for row in await self.db.pending_journal():
                message = await discord_call(channel.send(clip(journal_text(row), 2000)))
                await self.db.delivered(row["id"], message.id)
        except (discord.HTTPException, OSError) as error:
            LOG.warning("Background publication pending: %s", type(error).__name__)
        except Exception as error:
            LOG.error("Background failure: %s", type(error).__name__)

    @worker.before_loop
    async def before_worker(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=1)
    async def settlements(self):
        try:
            await self.db.close_due()
        except Exception as error:
            LOG.warning("settlement_failed error=%s", type(error).__name__)

    @tasks.loop(minutes=1)
    async def backups(self):
        day = datetime.now(timezone.utc).date().isoformat()
        if self.last_backup_day != day:
            try:
                await self.db.backup(self.store.path.parent / "backups" / f"dkp-{day}.sqlite3")
                self.last_backup_day = day
            except Exception as error:
                LOG.warning("backup_failed error=%s", type(error).__name__)


@app_commands.guild_only()
class DKPCommands(app_commands.Group):
    def __init__(self, bot):
        super().__init__(name="dkp", description="ДКП гильдии FC · RF Online")
        self.bot = bot

    async def interaction_check(self, i):
        handled(i)
        try:
            await acknowledge(i)
            is_setup = i.command and i.command.name == "setup"
            await self.bot.guard(i, require_setup=not is_setup)
            return True
        except Exception as error:
            # Group checks are outside Command._do_call: raw RuleError/OSError
            # would otherwise escape discord.py's AppCommandError handler.
            await report_error(i, error)
            return False

    @app_commands.command(description="Настроить ДКП-канал и роли (администратор)")
    async def setup(self, i: discord.Interaction, channel: discord.TextChannel, officer_role: discord.Role, member_role: discord.Role | None = None):
        await self.bot.guard(i, admin=True, require_setup=False)
        if officer_role.is_default() or officer_role.managed:
            raise RuleError("Выберите отдельную роль людей-офицеров, не @everyone и не роль бота.")
        permissions = channel.permissions_for(i.guild.me)
        missing = [name for name in ("view_channel","send_messages","embed_links","attach_files","read_message_history") if not getattr(permissions, name)]
        if missing:
            raise RuleError("В выбранном канале боту нужны права: " + ", ".join(missing))
        await self.bot.db.configure(channel.id, officer_role.id, member_role.id if member_role else 0, i.user.id)
        self.bot._modal_settings = await self.bot.db.settings()
        await reply(i, f"Готово. ДКП-канал: {channel.mention}. Выполните там **/dkp panel** для кнопок участников.")

    @app_commands.command(description="Опубликовать панель с кнопками (офицер)")
    async def panel(self, i: discord.Interaction):
        await self.bot.guard(i, officer=True)
        embed = discord.Embed(title="FC · Sleeping Forest · ДКП", description="Привяжи игровой ник, отмечайся на гильдийных сборах и участвуй в аукционах.\n\nОчки начисляет офицер за подтверждённое участие. Все операции публикуются в этом канале.", color=GREEN)
        await discord_call(i.channel.send(embed=embed, view=PanelView(self.bot)))
        await reply(i, "Панель с кнопками опубликована. Её можно закрепить в канале.")

    @app_commands.command(description="Привязать свой игровой ник")
    async def register(self, i: discord.Interaction, nickname: app_commands.Range[str,1,32]):
        await self.bot.db.register(i.user.id, nickname)
        await reply(i, f"Ник **{safe(nickname)}** привязан.")

    @app_commands.command(description="Изменить игровой ник участника (офицер)")
    async def rename(self, i: discord.Interaction, member: discord.Member, nickname: app_commands.Range[str,1,32]):
        await self.bot.guard(i, officer=True)
        await self.bot.db.rename(member.id, nickname, i.user.id)
        await reply(i, "Ник изменён. Баланс и история сохранены.")

    @app_commands.command(description="Свой баланс или баланс участника")
    async def balance(self, i: discord.Interaction, member: discord.Member | None = None):
        await self.bot.show_balance(i, (member or i.user).id)

    @app_commands.command(description="Таблица ДКП гильдии")
    async def top(self, i: discord.Interaction, page: app_commands.Range[int,1,10000] = 1):
        await self.bot.show_top(i, page)

    @app_commands.command(description="История начислений и списаний участника")
    async def history(self, i: discord.Interaction, member: discord.Member | None = None, page: app_commands.Range[int,1,10000] = 1):
        await self.bot.show_history(i, (member or i.user).id, page)

    @app_commands.command(description="Полный журнал гильдии, включая ставки и действия офицеров")
    async def journal(self, i: discord.Interaction, page: app_commands.Range[int,1,10000] = 1):
        rows = await self.bot.db.journal(page)
        body = "\n\n".join(journal_text(r) + "\nПолные данные записи:\n" + json.dumps(json.loads(r["data"]), ensure_ascii=False, indent=2) for r in rows) or "Журнал пока пуст."
        await reply(i, f"Журнал · страница {page}", file=text_file(body, f"journal-{page}.txt"))

    @app_commands.command(description="Начислить/списать ДКП с причиной и подтверждением (офицер)")
    async def adjust(self, i: discord.Interaction, member: discord.Member, amount: app_commands.Range[int,-1000000,1000000], reason: app_commands.Range[str,1,200]):
        await self.bot.guard(i, officer=True)
        state = await self.bot.db.balance(member.id)
        if amount == 0:
            raise RuleError("Укажите ненулевое изменение.")
        def apply():
            self.bot.store.adjust(member.id, amount, reason, i.user.id, i.id)
            return f"Изменение **{amount:+} ДКП** для {who(member.id)} проведено."
        await reply(i, f"{who(member.id)} · сейчас **{state['total']} ДКП**, свободно **{state['available']}**.\nИзменение: **{amount:+} ДКП**\nПричина: {safe(reason)}", view=ConfirmView(self.bot, i.user.id, apply))

    @app_commands.command(description="Создать сбор на ЧВ, ПБ или другое событие (офицер)")
    async def event(self, i: discord.Interaction, title: app_commands.Range[str,1,100], points: app_commands.Range[int,1,100000]):
        await self.bot.guard(i, officer=True)
        eid = await self.bot.db.create_event(title, points, i.user.id)
        await reply(i, f"Событие **#{eid}** сохранено. Карточка появится здесь через несколько секунд.")

    @app_commands.command(description="Добавить участника в событие (офицер)")
    async def event_add(self, i: discord.Interaction, event_id: int, member: discord.Member):
        await self.bot.guard(i, officer=True)
        if not await self.bot.eligible(member):
            raise RuleError("Участнику нужна роль гильдии; ботов добавлять нельзя.")
        count = await self.bot.db.attend(event_id, [member.id], True, i.user.id)
        await reply(i, "Участник добавлен." if count else "Участник уже в списке.")

    @app_commands.command(description="Удалить участника перед начислением (офицер)")
    async def event_remove(self, i: discord.Interaction, event_id: int, member: discord.Member):
        await self.bot.guard(i, officer=True)
        count = await self.bot.db.attend(event_id, [member.id], False, i.user.id)
        await reply(i, "Участник удалён." if count else "Участника нет в списке.")

    @app_commands.command(description="Добавить зарегистрированных участников голосового канала (офицер)")
    async def voice(self, i: discord.Interaction, event_id: int, channel: discord.VoiceChannel):
        await self.bot.guard(i, officer=True)
        await self.bot.db.entity("events", event_id)
        if not channel.permissions_for(i.user).view_channel or not channel.permissions_for(i.guild.me).view_channel:
            raise RuleError("Офицеру и боту нужен доступ к этому голосовому каналу.")
        await acknowledge(i)
        user_ids, skipped = [], []
        registered = {r["user_id"] for r in await self.bot.db.rows("SELECT user_id FROM members")}
        slots = asyncio.Semaphore(4)
        async def fetch(uid):
            async with slots:
                try:
                    return await discord_call(i.guild.fetch_member(uid))
                except discord.NotFound:
                    return None
        # Only network reads are cancelled on timeout: no partial roster is written.
        try:
            async with asyncio.timeout(60):
                async with asyncio.TaskGroup() as group:
                    pending = [group.create_task(fetch(uid)) for uid in list(channel.voice_states)]
                members = [task.result() for task in pending]
                i.user = await discord_call(i.guild.fetch_member(i.user.id))
        except (TimeoutError, ExceptionGroup, discord.HTTPException, aiohttp.ClientError) as error:
            record(i, "voice_fetch_failed", error)
            raise RuleError("Не удалось получить полный состав голосового канала. Список не изменён; повторите позже.") from None
        for member in members:
            if member is None or member.bot:
                continue
            if member.id not in registered or not await self.bot.eligible(member):
                skipped.append(member.id)
            else:
                user_ids.append(member.id)
        await self.bot.guard(i, officer=True)
        count = await self.bot.db.attend(event_id, user_ids, True, i.user.id) if user_ids else 0
        skipped_text = ", ".join(who(uid) for uid in skipped[:30]) or "нет"
        await reply(i, f"Добавлено: **{count}**. Пропущены (нет ника/роли): {skipped_text}.\nЭто снимок голосового канала; проверьте участие в игре перед начислением.")

    @app_commands.command(description="Полный список участников события")
    async def roster(self, i: discord.Interaction, event_id: int):
        await self.bot.show_roster(i, event_id)

    @app_commands.command(description="Проверить состав и подтвердить начисление (офицер)")
    async def award(self, i: discord.Interaction, event_id: int):
        await self.bot.confirm_award(i, event_id)

    @app_commands.command(description="Отменить открытое событие без начисления (офицер)")
    async def event_cancel(self, i: discord.Interaction, event_id: int):
        await self.bot.guard(i, officer=True)
        def apply():
            self.bot.store.cancel_event(event_id, i.user.id)
            return f"Событие #{event_id} отменено."
        event = await self.bot.db.entity("events", event_id)
        await reply(i, f"Отменить событие **#{event_id} · {safe(event['title'])}** без начисления?", view=ConfirmView(self.bot, i.user.id, apply))

    @app_commands.command(description="Выставить предмет на аукцион за ДКП (офицер)")
    async def auction(self, i: discord.Interaction, item: app_commands.Range[str,1,150], minimum: app_commands.Range[int,1,1000000] = 10, step: app_commands.Range[int,1,1000000] = 1, minutes: app_commands.Range[int,1,10080] = 10):
        await self.bot.guard(i, officer=True)
        aid = await self.bot.db.create_auction(item, minimum, step, minutes, i.user.id)
        await reply(i, f"Аукцион **#{aid}** сохранён. Карточка появится через несколько секунд. Время отсчитывается с создания.")

    @app_commands.command(description="Сделать ставку на аукцион (полная сумма)")
    async def bid(self, i: discord.Interaction, auction_id: int, amount: app_commands.Range[int,1,1000000000]):
        await self.bot.db.bid(auction_id, i.user.id, amount)
        await reply(i, f"Ставка **{amount} ДКП** принята на аукционе #{auction_id}.")

    @app_commands.command(description="Открытые аукционы")
    async def auctions(self, i: discord.Interaction, page: app_commands.Range[int,1,10000] = 1):
        rows = await self.bot.db.rows("SELECT * FROM auctions WHERE status='open' ORDER BY ends_at LIMIT 10 OFFSET ?", ((page-1)*10,))
        body = "\n".join(f"**#{r['id']}** · {safe(r['item'])} · {r['highest_bid']} ДКП · <t:{r['ends_at']}:R>" for r in rows) or "Открытых аукционов нет."
        await reply(i, embed=discord.Embed(title=f"Аукционы · страница {page}", description=clip(body, 4096), color=GREEN))

    @app_commands.command(description="Отменить открытый аукцион с причиной (офицер)")
    async def cancel(self, i: discord.Interaction, auction_id: int, reason: app_commands.Range[str,1,200]):
        await self.bot.guard(i, officer=True)
        def apply():
            self.bot.store.cancel_auction(auction_id, i.user.id, reason)
            return f"Аукцион #{auction_id} отменён. Резерв освобождён."
        auction = await self.bot.db.entity("auctions", auction_id)
        await reply(i, f"Отменить **#{auction_id} · {safe(auction['item'])}**?\nПричина: {safe(reason)}", view=ConfirmView(self.bot, i.user.id, apply))

    @app_commands.command(description="Резервная копия базы на компьютере бота (офицер)")
    async def backup(self, i: discord.Interaction):
        await self.bot.guard(i, officer=True)
        await acknowledge(i)
        name = f"manual-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.sqlite3"
        await self.bot.db.backup(self.bot.store.path.parent / "backups" / name)
        await reply(i, f"Копия сохранена на компьютере бота: **data/backups/{name}** (или DATA_DIR, если он изменён).")

    @app_commands.command(description="Как пользоваться ДКП")
    async def help(self, i: discord.Interaction):
        await reply(i, "**Участнику:** /dkp register → «Я участвую» в событии → «Сделать ставку» в аукционе.\n**Офицеру:** /dkp event → проверить /dkp roster → /dkp award → /dkp auction.\n**Учёт:** /dkp balance, /dkp top, /dkp history, /dkp journal.\n**Состав:** /dkp voice, /dkp event_add, /dkp event_remove.\n**Исправления:** /dkp adjust, /dkp rename, /dkp event_cancel, /dkp cancel.\n**Настройки:** /dkp setup, /dkp panel, /dkp backup.\n\nСвободные ДКП = баланс − лидирующие ставки. Списываются только при победе. Ставку снять нельзя. При равной сумме новая ставка отклоняется. Поздняя ставка оставляет 30 секунд на ответ.\nПри выключенном компьютере бот недоступен. Просроченные аукционы рассчитываются после запуска по последней принятой ставке.")

    @app_commands.command(description="Версия бота и состояние соединения")
    async def status(self, i: discord.Interaction):
        latency = round(self.bot.latency * 1000) if self.bot.latency < float("inf") else "нет данных"
        await reply(i, f"**FC DKP {VERSION}**\nПодтверждение запроса: **{i.extras.get('dkp_ack_ms', '—')} мс**\nЗадержка Gateway: **{latency} мс**\nПроверка сбоев: `logs/bot.log` на компьютере бота.")


def load_config():
    load_dotenv(ROOT / ".env")
    token = os.getenv("DISCORD_TOKEN", "").strip()
    guild = os.getenv("GUILD_ID", "").strip()
    if not token or token == "PASTE_BOT_TOKEN_HERE" or not guild.isdecimal() or int(guild) <= 0:
        raise RuleError("Заполните DISCORD_TOKEN и GUILD_ID в .env. Можно запустить configure.bat.")
    directory = Path(os.getenv("DATA_DIR", "data"))
    if not directory.is_absolute():
        directory = ROOT / directory
    return token, int(guild), directory.resolve()


def main():
    from runtime import InstanceLock
    try:
        token, guild_id, data_dir = load_config()
        data_dir.mkdir(parents=True, exist_ok=True)
        with InstanceLock(data_dir / "bot.lock"):
            logs = ROOT / "logs"
            logs.mkdir(exist_ok=True)
            handler = RotatingFileHandler(logs / "bot.log", maxBytes=1000000, backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
            LOG.setLevel(logging.INFO)
            LOG.addHandler(handler)
            store = Store(data_dir / "dkp.sqlite3", guild_id)
            store.backup(data_dir / "backups" / f"startup-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.sqlite3")
            DKPBot(guild_id, store).run(token, log_handler=None)
    except (RuleError, RuntimeError) as error:
        print(str(error), flush=True)
        return 1
    except discord.LoginFailure:
        print("Discord rejected the bot token. Run configure.bat and enter the bot token again.", flush=True)
        return 1
    except discord.Forbidden:
        print("Discord denied access. Check GUILD_ID and Guild Install with bot + applications.commands.", flush=True)
        return 1
    except Exception as error:
        print(f"Startup failed ({type(error).__name__}). Check Discord connectivity and README.md.", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
