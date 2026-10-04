"""One lock per list, durable revisions, and edit-only background reconciliation."""

import asyncio
import io
import logging
from collections import defaultdict
from contextlib import asynccontextmanager

import discord

from tierbot.models import Board, UserError
from tierbot.render import Page, render_board
from tierbot.store import Store

log = logging.getLogger(__name__)


class MissingBoard(UserError):
    pass


class BoardForbidden(UserError):
    pass


def attachments(board: Board, pages: list[Page]):
    files, embeds = [], []
    for index, page in enumerate(pages):
        files.append(discord.File(io.BytesIO(page.data), filename=page.filename))
        embed = discord.Embed(title=board.title if index == 0 else f"{board.title} (continued)")
        embed.set_image(url=f"attachment://{page.filename}")
        embed.set_footer(text=f"tierlist:{board.id} • revision:{board.revision} • page:{index + 1}")
        embeds.append(embed)
    return files, embeds


class DiscordGateway:
    def __init__(self, client: discord.Client, guild_id: int):
        self.client, self.guild_id = client, guild_id

    async def channel(self, channel_id: int) -> discord.TextChannel:
        try:
            channel = self.client.get_channel(channel_id) or await self.client.fetch_channel(
                channel_id
            )
        except discord.NotFound as exc:
            raise MissingBoard("The hooked channel no longer exists. Use /tierlist hook.") from exc
        except discord.Forbidden as exc:
            raise BoardForbidden(
                "I cannot access the hooked channel. Check my channel permissions."
            ) from exc
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.guild_id:
            raise UserError("Choose a standard text channel in the configured server.")
        if channel.type != discord.ChannelType.text:
            raise UserError("Choose a standard text channel, not an announcement channel.")
        return channel

    async def validate(self, channel_id: int) -> int:
        channel = await self.channel(channel_id)
        if channel.guild.me is None:
            raise BoardForbidden("My server membership is unavailable; try again shortly.")
        permissions = channel.permissions_for(channel.guild.me)
        needed = (
            "view_channel",
            "send_messages",
            "embed_links",
            "attach_files",
            "read_message_history",
        )
        missing = [p.replace("_", " ") for p in needed if not getattr(permissions, p)]
        if missing:
            raise BoardForbidden("Missing channel permissions: " + ", ".join(missing) + ".")
        return min(channel.guild.filesize_limit, 8 * 1024 * 1024)

    @asynccontextmanager
    async def translate_errors(self):
        try:
            yield
        except discord.NotFound as exc:
            raise MissingBoard(
                "The board message is missing. Use /tierlist hook to recreate it."
            ) from exc
        except discord.Forbidden as exc:
            raise BoardForbidden(
                "I cannot update this board. Check my channel permissions."
            ) from exc

    async def edit(self, board: Board, pages: list[Page]):
        async with self.translate_errors():
            channel = await self.channel(board.channel_id)
            message = channel.get_partial_message(board.message_id)
            files, embeds = attachments(board, pages)
            try:
                # Supplying only the new files explicitly removes ALL old attachments.
                await message.edit(
                    content=None,
                    embeds=embeds,
                    attachments=files,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            finally:
                for file in files:
                    file.close()

    async def publish(self, channel_id: int, board: Board, pages: list[Page]) -> int:
        async with self.translate_errors():
            channel = await self.channel(channel_id)
            files, embeds = attachments(board, pages)
            try:
                message = await channel.send(
                    embeds=embeds, files=files, allowed_mentions=discord.AllowedMentions.none()
                )
                return message.id
            finally:
                for file in files:
                    file.close()

    async def delete(self, board: Board):
        if board.message_id is None:
            return
        try:
            async with self.translate_errors():
                channel = await self.channel(board.channel_id)
                await channel.get_partial_message(board.message_id).delete()
        except MissingBoard:
            pass  # Deleting an already missing board/channel is successful cleanup.


class Synchronizer:
    def __init__(self, store: Store, gateway: DiscordGateway):
        self.store, self.gateway = store, gateway
        self.locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.render_slots = asyncio.Semaphore(2)

    async def render(self, board: Board, limit: int = 8 * 1024 * 1024) -> list[Page]:
        async with self.render_slots:
            return await asyncio.to_thread(render_board, board, self.store.images, limit)

    async def refresh_locked(self, list_id: int) -> str:
        """Caller holds locks[list_id]. Failures leave the committed revision pending."""
        board = self.store.get(list_id)
        if board.message_id is None:
            return "Saved. This list is not hooked; use /tierlist hook to publish it."
        try:
            limit = await self.gateway.validate(board.channel_id)
            pages = await self.render(board, limit)
            await self.gateway.edit(board, pages)
            self.store.synced(list_id, board.revision)
            return f"Saved and updated the [board]({board.url})."
        except MissingBoard as exc:
            self.store.sync_error(list_id, "missing")
            return f"Saved, but {exc} No new message was posted."
        except BoardForbidden as exc:
            self.store.sync_error(list_id, "forbidden")
            return f"Saved, but {exc} Then run /tierlist refresh."
        except UserError as exc:
            self.store.sync_error(list_id, "render")
            return f"Saved, but the board could not be rendered: {exc}"
        except Exception:
            self.store.sync_error(list_id, "transient")
            log.exception("Board update pending for list %s", list_id)
            return "Saved. The board update failed; it will be retried automatically."

    async def hook_locked(self, list_id: int, channel_id: int) -> str:
        board = self.store.get(list_id)
        limit = await self.gateway.validate(channel_id)
        if board.channel_id == channel_id:
            await self.refresh_locked(list_id)
            current = self.store.get(list_id)
            if current.sync_error != "missing":
                if current.sync_error:
                    raise UserError(
                        "The existing board could not be refreshed. Check permissions and retry."
                    )
                return f"Updated the existing [board]({current.url})."
        # Prepare images BEFORE deleting an old board, so render failures preserve it.
        pages = await self.render(board, limit)
        if board.message_id:
            await self.gateway.delete(board)
            self.store.bind(list_id, None, None)
        try:
            message_id = await self.gateway.publish(channel_id, board, pages)
        except Exception as exc:
            # Sends are intentionally never retried automatically: Discord might have accepted one.
            raise UserError(
                "Publishing failed; your list data is safe and unhooked. Check the destination "
                "for a possible posted board before retrying /tierlist hook."
            ) from exc
        self.store.bind(list_id, channel_id, message_id)
        self.store.synced(list_id, board.revision)
        return f"Hooked the [board]({self.store.get(list_id).url})."

    async def unhook_locked(self, list_id: int):
        board = self.store.get(list_id)
        await self.gateway.delete(board)
        self.store.bind(list_id, None, None)

    async def delete_locked(self, list_id: int):
        board = self.store.get(list_id)
        await self.gateway.delete(board)
        self.store.delete(list_id)

    async def reconcile(self):
        for board in self.store.all():
            if not board.message_id or board.revision == board.synced_revision:
                continue
            if board.sync_error in ("missing", "forbidden", "render"):
                continue
            async with self.locks[board.id]:
                try:
                    current = self.store.get(board.id)
                except UserError:
                    continue  # Deleted while waiting for its lock.
                if current.message_id and current.revision != current.synced_revision:
                    await self.refresh_locked(current.id)
