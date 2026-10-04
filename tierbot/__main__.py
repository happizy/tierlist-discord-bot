import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from tierbot.commands import TierCommands, reply
from tierbot.models import UserError
from tierbot.store import Store
from tierbot.sync import DiscordGateway, Synchronizer

log = logging.getLogger(__name__)


class CommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id != self.client.store.guild_id:
            await reply(interaction, "Use this bot in its configured Discord server.")
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        cause = getattr(error, "original", error)
        if isinstance(cause, UserError):
            await reply(interaction, str(cause))
        elif isinstance(cause, discord.HTTPException):
            log.warning("Discord command request failed: %s", type(cause).__name__)
            await reply(
                interaction,
                "Discord could not complete the request. Check channel permissions and try again.",
            )
        else:
            log.error("Command failed", exc_info=(type(cause), cause, cause.__traceback__))
            await reply(
                interaction,
                "An unexpected error occurred. Check the bot logs and the list's current state.",
            )


class TierBot(commands.Bot):
    def __init__(self, store: Store):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            tree_cls=CommandTree,
            allowed_mentions=discord.AllowedMentions.none(),
            help_command=None,
        )
        self.store = store
        self.sync = Synchronizer(store, DiscordGateway(self, store.guild_id))
        self.worker: asyncio.Task | None = None

    async def setup_hook(self):
        self.store.collect_images()
        guild = discord.Object(id=self.store.guild_id)
        await self.add_cog(TierCommands(self.store, self.sync), guild=guild)
        await self.tree.sync(guild=guild)
        self.worker = asyncio.create_task(self.reconcile_loop(), name="board-reconciliation")
        log.info("Registered slash commands for server %s", self.store.guild_id)

    async def on_ready(self):
        log.info("Connected as %s; serving server %s", self.user, self.store.guild_id)

    async def reconcile_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.sync.reconcile()
            except Exception:
                log.exception("Board reconciliation failed; retrying in 30 seconds")
            await asyncio.sleep(30)

    async def close(self):
        if self.worker is not None:
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
            self.worker = None
        await super().close()


async def run(token: str, guild_id: int, directory: Path):
    # Refuse a second local process sharing the same data volume.
    import fcntl

    # This initialization runs before the client or any background task exists.
    directory.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    with (directory / "bot.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another bot instance is using this data directory.") from exc
        store = Store(directory, guild_id)
        try:
            async with TierBot(store) as bot:
                loop = asyncio.get_running_loop()
                loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(bot.close()))
                await bot.start(token)
        finally:
            store.close()


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = os.getenv("DISCORD_TOKEN", "").strip()
    raw_guild = os.getenv("DISCORD_GUILD_ID", "")
    if (
        not token
        or token == "replace-with-your-bot-token"
        or not raw_guild.isdigit()
        or int(raw_guild) <= 0
    ):
        raise SystemExit(
            "Set DISCORD_TOKEN and a numeric DISCORD_GUILD_ID. See .env.example and README.md."
        )
    directory = Path(os.getenv("DATA_DIR", "data"))
    try:
        asyncio.run(run(token, int(raw_guild), directory))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
