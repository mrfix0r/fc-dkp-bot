"""Bounded Discord replies. Retry message edits, never replay a DKP operation."""
import asyncio
import io
import logging
import time
import traceback
from datetime import datetime, timezone

import aiohttp
import discord

LOG = logging.getLogger("fc_dkp")
ACK_TIMEOUT = 2.5
SEND_TIMEOUT = 8.0
SEND_ATTEMPTS = 2


class AcknowledgementFailed(Exception):
    """The command must not mutate data without a confirmed initial response."""


def handled(i):
    if "dkp_started" not in i.extras:
        i.extras["dkp_started"] = time.monotonic()
        created = getattr(i, "created_at", None)
        if created is not None:
            i.extras["dkp_arrival_ms"] = round((datetime.now(timezone.utc) - created).total_seconds() * 1000)


def record(i, phase, error=None):
    data = i.data or {}
    label = data.get("custom_id") or data.get("name", "interaction")
    options = data.get("options", [])
    while options and options[0].get("type") in (1, 2):
        label += "/" + options[0]["name"]
        options = options[0].get("options", [])
    elapsed = time.monotonic() - i.extras.get("dkp_started", time.monotonic())
    fields = [f"request={i.id}", f"operation={label}", f"phase={phase}", f"elapsed={elapsed:.3f}s",
              f"arrival_ms={i.extras.get('dkp_arrival_ms', '-')}" ]
    if error is not None:
        fields += [f"error={type(error).__name__}", f"http={getattr(error, 'status', '-')}",
                   f"discord_code={getattr(error, 'code', '-')}"]
        frames = traceback.extract_tb(error.__traceback__)
        if frames:
            frame = frames[-1]
            fields.append(f"at={frame.name}:{frame.lineno}")
    # No tokens, URLs, HTTP bodies, nicknames or exception messages.
    LOG.log(logging.WARNING if error else logging.INFO, " ".join(fields))


async def acknowledge(i, *, update=False):
    handled(i)
    lock = i.extras.setdefault("dkp_response_lock", asyncio.Lock())
    async with lock:
        if i.response.is_done():
            return
        if i.extras.get("dkp_ack_failed"):
            raise AcknowledgementFailed()
        started = time.monotonic()
        try:
            async with asyncio.timeout(ACK_TIMEOUT):
                if update:
                    await i.response.defer()
                else:
                    await i.response.defer(ephemeral=True, thinking=True)
        except (discord.HTTPException, aiohttp.ClientError, OSError, TimeoutError) as error:
            i.extras["dkp_ack_failed"] = True
            record(i, "ack_failed", error)
            raise AcknowledgementFailed() from error
        i.extras["dkp_ack_ms"] = round((time.monotonic() - started) * 1000)
        record(i, "acknowledged")


async def open_modal(i, modal):
    handled(i)
    started = time.monotonic()
    try:
        async with asyncio.timeout(ACK_TIMEOUT):
            await i.response.send_modal(modal)
    except (discord.HTTPException, aiohttp.ClientError, OSError, TimeoutError) as error:
        i.extras["dkp_ack_failed"] = True
        record(i, "modal_failed", error)
        raise AcknowledgementFailed() from error
    i.extras["dkp_ack_ms"] = round((time.monotonic() - started) * 1000)
    record(i, "modal_opened")


def transient(error):
    if isinstance(error, discord.HTTPException):
        return error.status == 429 or error.status >= 500
    return isinstance(error, (aiohttp.ClientError, OSError, TimeoutError))


async def edit_response(i, **kwargs):
    # discord.py closes File objects after an HTTP attempt: recreate uploads on retry.
    uploads = []
    for attachment in kwargs.get("attachments", []):
        if isinstance(attachment, discord.File):
            attachment.fp.seek(0)
            uploads.append((attachment.fp.read(), attachment.filename))
        else:
            uploads.append(attachment)
    for attempt in range(SEND_ATTEMPTS):
        files = []
        request = dict(kwargs)
        if "attachments" in request:
            request["attachments"] = []
            for attachment in uploads:
                if isinstance(attachment, tuple):
                    upload = discord.File(io.BytesIO(attachment[0]), filename=attachment[1])
                    files.append(upload)
                    request["attachments"].append(upload)
                else:
                    request["attachments"].append(attachment)
        try:
            async with asyncio.timeout(SEND_TIMEOUT):
                result = await i.edit_original_response(**request)
            record(i, "response_delivered")
            return result
        except (discord.HTTPException, aiohttp.ClientError, OSError, TimeoutError) as error:
            record(i, "response_failed", error)
            if not transient(error) or attempt + 1 == SEND_ATTEMPTS:
                raise
            await asyncio.sleep(0.25)
        finally:
            for upload in files:
                upload.close()
                upload.fp.close()


async def reply(i, text=None, **kwargs):
    handled(i)
    kwargs.setdefault("ephemeral", True)
    if not i.response.is_done():
        await acknowledge(i)
    if i.response.type == discord.InteractionResponseType.deferred_message_update:
        # Errors on confirmation buttons must not replace a public source message.
        async with asyncio.timeout(SEND_TIMEOUT):
            result = await i.followup.send(text, **kwargs)
        record(i, "response_delivered")
        return result
    kwargs.pop("ephemeral", None)
    files = kwargs.pop("files", [])
    if "file" in kwargs:
        files.append(kwargs.pop("file"))
    if files:
        kwargs["attachments"] = files
    view = kwargs.get("view")
    if view is not None:
        view.response_interaction = i
    try:
        return await edit_response(i, content=text, **kwargs)
    finally:
        for upload in files:
            upload.close()


async def recover_ack_failure(i):
    # The ACK may have reached Discord even if its HTTP response was lost.
    # An idempotent edit can resolve that state; the business operation was not run.
    try:
        await edit_response(i, content="Не удалось вовремя подтвердить запрос. Действие не запускалось. Повторите команду.")
    except (discord.HTTPException, aiohttp.ClientError, OSError, TimeoutError):
        pass
