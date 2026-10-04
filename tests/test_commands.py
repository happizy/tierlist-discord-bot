from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tierbot.__main__ import TierBot
from tierbot.commands import DeleteConfirmation, TierCommands
from tierbot.models import UserError
from tierbot.render import MAX_UPLOAD_BYTES
from tierbot.sync import Synchronizer


def interaction(guild=123):
    response = MagicMock()
    response.is_done.return_value = False

    async def defer(**kwargs):
        response.is_done.return_value = True

    response.defer = AsyncMock(side_effect=defer)
    response.send_message = AsyncMock()
    response.edit_message = AsyncMock()
    return SimpleNamespace(
        guild_id=guild,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
        user=SimpleNamespace(id=555),
        namespace=SimpleNamespace(),
        edit_original_response=AsyncMock(),
    )


@pytest.fixture
def cog(store):
    sync = Synchronizer(store, MagicMock())
    return TierCommands(store, sync)


async def test_discord_command_tree_schema(store):
    bot = TierBot(store)
    try:
        await bot.add_cog(TierCommands(store, bot.sync), guild=discord.Object(id=123))
        groups = bot.tree.get_commands(guild=discord.Object(id=123))
        assert {g.name for g in groups} == {"tierlist", "tier", "item"}
        assert bot.tree.get_commands() == []  # No global registration.
        count = 0
        for group in groups:
            schema = group.to_dict(bot.tree)
            assert len(schema["description"]) <= 100
            for command in schema["options"]:
                count += 1
                assert len(command["description"]) <= 100
                for parameter in command["options"]:
                    if parameter["name"] in ("list", "tier", "item", "destination"):
                        assert parameter["autocomplete"] is True
        assert count == 18
        assert not bot.intents.message_content
        assert not bot.intents.messages
        assert not bot.intents.members
    finally:
        await bot.close()


async def test_create_and_mutate_are_private(cog):
    request = interaction()
    await cog.create.callback(cog, request, "Movies", "Excellent | Fine")
    request.response.defer.assert_called_once_with(ephemeral=True, thinking=True)
    assert request.followup.send.call_args.kwargs["ephemeral"] is True
    key = cog.store.resolve("Movies")
    request = interaction()
    await cog.item_add.callback(cog, request, "Movies", "Arrival", "Excellent")
    assert cog.store.item(key, "Arrival").name == "Arrival"
    assert request.followup.send.call_args.kwargs["ephemeral"] is True
    cog.sync.gateway.publish.assert_not_called()


async def test_autocomplete_filtering_scope_and_stable_ids(cog):
    key = cog.store.create("Movies")
    for index in range(40):
        cog.store.add_item(key, f"Movie {index}", "S")
    request = interaction()
    request.namespace.list = f"#{key}"
    assert len(await cog.complete_items(request, "")) == 25
    found = await cog.complete_items(request, "Movie 39")
    assert len(found) == 1
    entry = cog.store.item(key, found[0].value)
    cog.store.edit_item(key, found[0].value, name="Renamed")
    assert cog.store.item(key, found[0].value).id == entry.id
    foreign = interaction(guild=999)
    foreign.namespace.list = f"#{key}"
    assert await cog.complete_lists(foreign, "") == []
    assert await cog.complete_items(foreign, "") == []
    assert await cog.complete_tiers(foreign, "") == []


async def test_global_server_check_rejects_dms_and_other_servers(store):
    bot = TierBot(store)
    try:
        for guild in (None, 999):
            request = interaction(guild=guild)
            assert not await bot.tree.interaction_check(request)
            assert request.response.send_message.call_args.kwargs["ephemeral"] is True
        assert await bot.tree.interaction_check(interaction())
    finally:
        await bot.close()


async def test_upload_limit_checked_before_download(cog):
    image = SimpleNamespace(size=MAX_UPLOAD_BYTES + 1, read=AsyncMock())
    with pytest.raises(UserError, match="10 MiB"):
        await cog.image_data(image)
    image.read.assert_not_called()


async def test_delete_confirmation_cannot_delete_changed_list(cog):
    key = cog.store.create("Movies")
    board = cog.store.get(key)
    view = DeleteConfirmation(cog.sync, key, 555, board.revision)
    cog.store.add_item(key, "Added meanwhile", "S")
    request = interaction()
    await view.confirm.callback(request)
    assert "changed" in request.edit_original_response.call_args.kwargs["content"]
    assert len(cog.store.get(key).items) == 1
    cog.sync.gateway.delete.assert_not_called()


async def test_confirmed_delete_and_cancel(cog):
    key = cog.store.create("Movies")
    cog.sync.gateway.delete = AsyncMock()
    view = DeleteConfirmation(cog.sync, key, 555, cog.store.get(key).revision)
    outsider = interaction()
    outsider.user.id = 556
    assert not await view.interaction_check(outsider)
    request = interaction()
    await view.cancel.callback(request)
    assert cog.store.get(key)
    view = DeleteConfirmation(cog.sync, key, 555, cog.store.get(key).revision)
    assert await view.interaction_check(request)
    await view.confirm.callback(request)
    assert cog.store.all() == []
    cog.sync.gateway.delete.assert_awaited_once()


async def test_directory_pagination_is_private(cog):
    for index in range(12):
        cog.store.create(f"List {index:02}")
    request = interaction()
    await cog.directory.callback(cog, request, page=2)
    payload = request.response.send_message.call_args.kwargs
    assert payload["ephemeral"] is True
    assert len(payload["embed"].fields) == 2
    assert payload["embed"].footer.text == "Page 2/2"
