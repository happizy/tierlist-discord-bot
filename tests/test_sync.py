import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tierbot.models import UserError
from tierbot.render import Page
from tierbot.sync import BoardForbidden, DiscordGateway, MissingBoard, Synchronizer


class Gateway:
    def __init__(self):
        self.messages = {}
        self.edits = []
        self.sent = 0
        self.error = None
        self.delete_error = None
        self.publish_error = None
        self.validate_error = None

    async def validate(self, channel):
        if self.validate_error:
            raise self.validate_error
        return 8 * 1024 * 1024

    async def publish(self, channel, board, pages):
        if self.publish_error:
            raise self.publish_error
        self.sent += 1
        message_id = 1000 + self.sent
        self.messages[message_id] = (channel, board, pages)
        return message_id

    async def edit(self, board, pages):
        if self.error:
            raise self.error
        if board.message_id not in self.messages:
            raise MissingBoard("The board message is missing.")
        await asyncio.sleep(0)
        self.edits.append((board.message_id, board.revision))
        self.messages[board.message_id] = (board.channel_id, board, pages)

    async def delete(self, board):
        if self.delete_error:
            raise self.delete_error
        self.messages.pop(board.message_id, None)


@pytest.fixture
def setup(store):
    gateway = Gateway()
    sync = Synchronizer(store, gateway)
    sync.render = AsyncMock(return_value=[Page("board.jpg", b"rendered")])
    key = store.create("Board")
    return store, gateway, sync, key


async def test_hook_updates_and_repeat_hook_preserve_message_id(setup):
    store, gateway, sync, key = setup
    async with sync.locks[key]:
        await sync.hook_locked(key, 100)
        message_id = store.get(key).message_id
        store.add_item(key, "New", "S")
        await sync.refresh_locked(key)
        await sync.hook_locked(key, 100)
    assert gateway.sent == 1
    assert store.get(key).message_id == message_id
    assert all(message == message_id for message, _ in gateway.edits)


async def test_missing_board_only_recreated_by_explicit_hook(setup):
    store, gateway, sync, key = setup
    await sync.hook_locked(key, 100)
    old = store.get(key).message_id
    del gateway.messages[old]
    store.rename(key, "Changed")
    result = await sync.refresh_locked(key)
    assert "No new message" in result
    await sync.reconcile()
    assert gateway.sent == 1
    assert store.get(key).sync_error == "missing"
    await sync.hook_locked(key, 100)
    assert gateway.sent == 2
    assert store.get(key).message_id != old
    assert store.get(key).sync_error is None


async def test_transient_failure_survives_restart_and_reconciles(setup):
    store, gateway, sync, key = setup
    await sync.hook_locked(key, 100)
    store.rename(key, "Changed")
    gateway.error = RuntimeError("network failure")
    await sync.refresh_locked(key)
    assert store.get(key).sync_error == "transient"
    # New worker reads the durable state; it does not need the previous worker's memory.
    restarted = Synchronizer(store, gateway)
    restarted.render = sync.render
    gateway.error = None
    await restarted.reconcile()
    board = store.get(key)
    assert board.revision == board.synced_revision
    assert board.sync_error is None
    assert gateway.sent == 1


async def test_forbidden_requires_refresh_and_preserves_edits(setup):
    store, gateway, sync, key = setup
    await sync.hook_locked(key, 100)
    gateway.error = BoardForbidden("Missing permissions.")
    store.rename(key, "Still saved")
    await sync.refresh_locked(key)
    assert store.get(key).title == "Still saved"
    assert store.get(key).sync_error == "forbidden"
    gateway.error = None
    await sync.reconcile()
    assert not gateway.edits
    await sync.refresh_locked(key)
    assert len(gateway.edits) == 1


async def test_concurrent_mutations_finish_in_revision_order(setup):
    store, gateway, sync, key = setup
    await sync.hook_locked(key, 100)

    async def change(name):
        async with sync.locks[key]:
            store.add_item(key, name, "S")
            await sync.refresh_locked(key)

    await asyncio.gather(*(change(str(i)) for i in range(10)))
    board = store.get(key)
    assert len(board.items) == 10
    assert gateway.messages[board.message_id][1].revision == board.revision
    revisions = [revision for _, revision in gateway.edits]
    assert revisions == sorted(set(revisions))
    assert gateway.sent == 1


async def test_move_unhook_and_delete(setup):
    store, gateway, sync, key = setup
    await sync.hook_locked(key, 100)
    old = store.get(key).message_id
    await sync.hook_locked(key, 200)
    assert old not in gateway.messages
    assert len(gateway.messages) == 1
    assert store.get(key).channel_id == 200
    await sync.unhook_locked(key)
    assert not gateway.messages
    assert store.get(key).message_id is None
    await sync.hook_locked(key, 200)
    await sync.delete_locked(key)
    assert not gateway.messages
    assert not store.all()


@pytest.mark.parametrize("operation", ["delete", "unhook", "move"])
async def test_cleanup_failure_does_not_lose_data_or_binding(setup, operation):
    store, gateway, sync, key = setup
    await sync.hook_locked(key, 100)
    before = store.get(key)
    gateway.delete_error = BoardForbidden("Forbidden")
    with pytest.raises(BoardForbidden):
        if operation == "delete":
            await sync.delete_locked(key)
        elif operation == "unhook":
            await sync.unhook_locked(key)
        else:
            await sync.hook_locked(key, 200)
    assert store.get(key) == before
    assert gateway.sent == 1


async def test_failed_publish_after_move_leaves_data_unhooked(setup):
    store, gateway, sync, key = setup
    await sync.hook_locked(key, 100)
    gateway.publish_error = RuntimeError("network failure")
    with pytest.raises(UserError, match="safe and unhooked"):
        await sync.hook_locked(key, 200)
    assert store.get(key).message_id is None
    assert store.get(key).title == "Board"
    await sync.reconcile()
    assert gateway.sent == 1


async def test_bad_destination_leaves_old_board_intact(setup):
    store, gateway, sync, key = setup
    await sync.hook_locked(key, 100)
    before = store.get(key)
    gateway.validate_error = BoardForbidden("Forbidden")
    with pytest.raises(BoardForbidden):
        await sync.hook_locked(key, 200)
    assert store.get(key) == before
    assert len(gateway.messages) == 1


async def test_unhooked_mutations_never_publish(setup):
    store, gateway, sync, key = setup
    store.rename(key, "Changed")
    assert "not hooked" in await sync.refresh_locked(key)
    await sync.reconcile()
    assert gateway.sent == 0


async def test_gateway_edit_replaces_all_attachments_when_board_shrinks(store):
    key = store.create("Board")
    store.bind(key, 100, 200)
    board = store.get(key)
    message = MagicMock()
    message.edit = AsyncMock()
    channel = MagicMock()
    channel.get_partial_message.return_value = message
    gateway = DiscordGateway(MagicMock(), store.guild_id)
    gateway.channel = AsyncMock(return_value=channel)
    await gateway.edit(board, [Page("page1.jpg", b"one"), Page("page2.jpg", b"two")])
    await gateway.edit(board, [Page("new.jpg", b"new")])
    channel.get_partial_message.assert_called_with(200)
    payload = message.edit.call_args.kwargs
    assert len(payload["attachments"]) == 1
    assert payload["attachments"][0].filename == "new.jpg"
    assert len(payload["embeds"]) == 1
    assert payload["embeds"][0].image.url == "attachment://new.jpg"
