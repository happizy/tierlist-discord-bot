import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from tierbot.models import UserError
from tierbot.render import MAX_UPLOAD_BYTES, normalize_image
from tierbot.store import Store, name_key
from tierbot.sync import Synchronizer, attachments

log = logging.getLogger(__name__)


async def reply(interaction: discord.Interaction, text: str):
    if interaction.response.is_done():
        await interaction.followup.send(
            text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )
    else:
        await interaction.response.send_message(
            text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )


class DeleteConfirmation(discord.ui.View):
    def __init__(self, sync: Synchronizer, list_id: int, owner_id: int, revision: int):
        super().__init__(timeout=60)
        self.sync, self.list_id, self.owner_id, self.revision = sync, list_id, owner_id, revision

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id or interaction.guild_id != self.sync.store.guild_id:
            await reply(interaction, "Only the person who requested this deletion can confirm it.")
            return False
        return True

    @discord.ui.button(label="Delete list and all items", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            async with self.sync.locks[self.list_id]:
                board = self.sync.store.get(self.list_id)
                if board.revision != self.revision:
                    raise UserError(
                        "This list changed after you requested deletion. "
                        "Run /tierlist delete again."
                    )
                await self.sync.delete_locked(self.list_id)
        except UserError as exc:
            await interaction.edit_original_response(content=str(exc), view=None)
        except discord.HTTPException:
            await interaction.edit_original_response(
                content="Discord could not remove the board. Your list data is safe; try again.",
                view=None,
            )
        else:
            await interaction.edit_original_response(
                content="Deleted the list, its items, and its board.", view=None
            )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Deletion cancelled.", view=None)
        self.stop()

    async def on_error(self, interaction, error, item):
        log.error(
            "Deletion confirmation failed", exc_info=(type(error), error, error.__traceback__)
        )
        await reply(
            interaction, "Deletion failed unexpectedly. Check the bot logs and current list state."
        )


class TierCommands(commands.Cog):
    tierlist = app_commands.Group(
        name="tierlist", description="Create and publish shared tier lists", guild_only=True
    )
    tier = app_commands.Group(
        name="tier", description="Manage the ordered tiers in a list", guild_only=True
    )
    item = app_commands.Group(
        name="item", description="Manage and rank items in a tier list", guild_only=True
    )

    def __init__(self, store: Store, sync: Synchronizer):
        self.store, self.sync = store, sync
        self.image_slots = asyncio.Semaphore(1)
        for group in self.get_app_commands():
            for command in group.commands:
                names = {p.name for p in command.parameters}
                for parameter, callback in (
                    ("list", self.complete_lists),
                    ("tier", self.complete_tiers),
                    ("destination", self.complete_tiers),
                    ("item", self.complete_items),
                ):
                    if parameter in names:
                        command.autocomplete(parameter)(callback)

    async def complete_lists(self, interaction: discord.Interaction, current: str):
        if interaction.guild_id != self.store.guild_id:
            return []
        return [
            app_commands.Choice(name=f"{b.title[:85]} (#{b.id})"[:100], value=f"#{b.id}")
            for b in self.store.all()
            if name_key(current) in name_key(b.title) or current == f"#{b.id}"
        ][:25]

    def selected_list(self, interaction):
        if interaction.guild_id != self.store.guild_id:
            raise UserError("Use these commands in the configured server.")
        return self.store.resolve(getattr(interaction.namespace, "list", ""))

    async def complete_tiers(self, interaction: discord.Interaction, current: str):
        try:
            board = self.store.get(self.selected_list(interaction))
        except UserError:
            return []
        return [
            app_commands.Choice(name=t.name, value=f"#{t.id}")
            for t in board.tiers
            if name_key(current) in name_key(t.name) or current == f"#{t.id}"
        ][:25]

    async def complete_items(self, interaction: discord.Interaction, current: str):
        try:
            board = self.store.get(self.selected_list(interaction))
        except UserError:
            return []
        return [
            app_commands.Choice(name=f"{i.name} · {t.name}"[:100], value=f"#{i.id}")
            for t in board.tiers
            for i in t.items
            if name_key(current) in name_key(i.name) or current == f"#{i.id}"
        ][:25]

    async def image_data(self, image: discord.Attachment | None):
        if image is None:
            return None
        if image.size > MAX_UPLOAD_BYTES:
            raise UserError("Images must be no larger than 10 MiB.")
        async with self.image_slots:
            raw = await image.read()
            return await asyncio.to_thread(normalize_image, raw)

    async def mutate(self, interaction, list_value, operation):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        list_id = self.store.resolve(list_value)
        async with self.sync.locks[list_id]:
            operation(list_id)
            result = await self.sync.refresh_locked(list_id)
        await reply(interaction, result)

    @tierlist.command(name="create", description="Create a list; custom tiers are separated by |")
    @app_commands.describe(
        title="List title", tiers="Ordered tier names, e.g. Amazing | Good | Meh"
    )
    async def create(self, interaction: discord.Interaction, title: str, tiers: str | None = None):
        await interaction.response.defer(ephemeral=True, thinking=True)
        list_id = self.store.create(title, tiers.split("|") if tiers is not None else None)
        await reply(
            interaction,
            f"Created list #{list_id}. Add items with /item add, then publish with /tierlist hook.",
        )

    @tierlist.command(name="list", description="Browse lists and their hooked boards")
    async def directory(
        self, interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1
    ):
        boards = self.store.all()
        total_pages = max(1, (len(boards) + 9) // 10)
        if page > total_pages:
            raise UserError(f"Choose a page between 1 and {total_pages}.")
        embed = discord.Embed(
            title="Tier lists",
            description="No lists yet. Use /tierlist create." if not boards else None,
        )
        for board in boards[(page - 1) * 10 : page * 10]:
            status = f"[Open board]({board.url})" if board.url else "Not hooked"
            if board.sync_error:
                status += f" · Update problem: {board.sync_error}"
            elif board.message_id and board.synced_revision != board.revision:
                status += " · Update pending"
            embed.add_field(
                name=f"{board.title} (#{board.id})",
                value=f"{len(board.items)} items · {status}",
                inline=False,
            )
        embed.set_footer(text=f"Page {page}/{total_pages}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tierlist.command(name="show", description="Show a private preview of a list")
    async def show(self, interaction: discord.Interaction, list: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        list_id = self.store.resolve(list)
        async with self.sync.locks[list_id]:
            board = self.store.get(list_id)
            pages = await self.sync.render(board, interaction.filesize_limit)
        files, embeds = attachments(board, pages)
        try:
            await interaction.followup.send(
                files=files,
                embeds=embeds,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        finally:
            for file in files:
                file.close()

    @tierlist.command(name="rename", description="Change a list's title")
    async def rename(self, interaction: discord.Interaction, list: str, title: str):
        await self.mutate(interaction, list, lambda key: self.store.rename(key, title))

    @tierlist.command(name="delete", description="Delete a list and every item after confirmation")
    async def delete(self, interaction: discord.Interaction, list: str):
        board = self.store.get(self.store.resolve(list))
        view = DeleteConfirmation(self.sync, board.id, interaction.user.id, board.revision)
        title = discord.utils.escape_markdown(board.title)
        await interaction.response.send_message(
            f"Delete **{title}** and all {len(board.items)} items? "
            "This cannot be undone. Confirm within 60 seconds.",
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @tierlist.command(
        name="hook", description="Publish or move a list's single board to a text channel"
    )
    async def hook(self, interaction: discord.Interaction, list: str, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True, thinking=True)
        list_id = self.store.resolve(list)
        async with self.sync.locks[list_id]:
            result = await self.sync.hook_locked(list_id, channel.id)
        await reply(interaction, result)

    @tierlist.command(
        name="unhook", description="Remove the public board while keeping the list and items"
    )
    async def unhook(self, interaction: discord.Interaction, list: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        list_id = self.store.resolve(list)
        async with self.sync.locks[list_id]:
            await self.sync.unhook_locked(list_id)
        await reply(interaction, "Removed the board. The list and its items are saved.")

    @tierlist.command(name="refresh", description="Retry an update without creating a new message")
    async def refresh(self, interaction: discord.Interaction, list: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        list_id = self.store.resolve(list)
        async with self.sync.locks[list_id]:
            result = await self.sync.refresh_locked(list_id)
        await reply(interaction, result)

    @tier.command(name="add", description="Add a custom tier; position is one-based")
    async def tier_add(
        self,
        interaction: discord.Interaction,
        list: str,
        name: str,
        position: app_commands.Range[int, 1, 15] | None = None,
    ):
        await self.mutate(interaction, list, lambda key: self.store.add_tier(key, name, position))

    @tier.command(name="rename", description="Rename a tier")
    async def tier_rename(self, interaction: discord.Interaction, list: str, tier: str, name: str):
        await self.mutate(interaction, list, lambda key: self.store.edit_tier(key, tier, name=name))

    @tier.command(name="move", description="Move a tier to a one-based position")
    async def tier_move(
        self,
        interaction: discord.Interaction,
        list: str,
        tier: str,
        position: app_commands.Range[int, 1, 15],
    ):
        await self.mutate(
            interaction, list, lambda key: self.store.edit_tier(key, tier, position=position)
        )

    @tier.command(name="color", description="Set a tier's background color, such as #ff7f7f")
    async def tier_color(self, interaction: discord.Interaction, list: str, tier: str, color: str):
        await self.mutate(
            interaction, list, lambda key: self.store.edit_tier(key, tier, color=color)
        )

    @tier.command(
        name="delete",
        description="Delete a tier; populated tiers need a destination for their items",
    )
    async def tier_delete(
        self, interaction: discord.Interaction, list: str, tier: str, destination: str | None = None
    ):
        await self.mutate(
            interaction, list, lambda key: self.store.delete_tier(key, tier, destination)
        )

    @item.command(name="add", description="Add a named item with an optional uploaded image")
    async def item_add(
        self,
        interaction: discord.Interaction,
        list: str,
        name: str,
        tier: str,
        image: discord.Attachment | None = None,
        position: app_commands.Range[int, 1, 300] | None = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await self.image_data(image)
        await self.mutate(
            interaction, list, lambda key: self.store.add_item(key, name, tier, position, data)
        )

    @item.command(
        name="show", description="Show an item's full name, tier, position, and image privately"
    )
    async def item_show(self, interaction: discord.Interaction, list: str, item: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        list_id = self.store.resolve(list)
        async with self.sync.locks[list_id]:
            entry = self.store.item(list_id, item)
            tier = self.store.tier(list_id, f"#{entry.tier_id}")
            embed = discord.Embed(
                title=entry.name,
                description=f"Tier: {discord.utils.escape_markdown(tier.name)}\n"
                f"Position: {entry.position + 1}\nItem ID: #{entry.id}",
            )
            file = None
            if entry.image and (self.store.images / entry.image).is_file():
                file = discord.File(self.store.images / entry.image, filename="item.png")
                embed.set_image(url="attachment://item.png")
            try:
                await interaction.followup.send(
                    embed=embed,
                    files=[file] if file else [],
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            finally:
                if file:
                    file.close()

    @item.command(name="edit", description="Rename an item, replace its image, or remove its image")
    @app_commands.rename(remove_image="remove-image")
    async def item_edit(
        self,
        interaction: discord.Interaction,
        list: str,
        item: str,
        name: str | None = None,
        image: discord.Attachment | None = None,
        remove_image: bool = False,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if image is not None and remove_image:
            raise UserError("Choose either a replacement image or remove-image, not both.")
        data = await self.image_data(image)
        await self.mutate(
            interaction,
            list,
            lambda key: self.store.edit_item(
                key, item, name=name, image=data, remove_image=remove_image
            ),
        )

    @item.command(
        name="move", description="Move an item between tiers or reorder it within its tier"
    )
    async def item_move(
        self,
        interaction: discord.Interaction,
        list: str,
        item: str,
        tier: str,
        position: app_commands.Range[int, 1, 300] | None = None,
    ):
        await self.mutate(
            interaction, list, lambda key: self.store.move_item(key, item, tier, position)
        )

    @item.command(name="delete", description="Delete an item and its stored image")
    async def item_delete(self, interaction: discord.Interaction, list: str, item: str):
        await self.mutate(interaction, list, lambda key: self.store.delete_item(key, item))
