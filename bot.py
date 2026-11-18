"""YBOT Discord client and process entry point."""

from __future__ import annotations

import logging
import asyncio
import signal
import sys
from pathlib import Path

import discord
from discord import app_commands

from commands.basic import route_message
from commands.classes import ClassCommands
from commands.knowledge import KnowledgeCommands
from commands.reminders import register_reminder_slash_commands
from commands.todo_panel import TodoPanelManager
from commands.todos import register_todo_slash_commands
from config import Config, ConfigurationError
from database.db import Database
from services.reminder_scheduler import ReminderScheduler
from services.server_setup import bootstrap_guild
from services.todo_service import TodoService
from services.background import BackgroundServices
from services.assistant import AssistantService
from utils.logger import configure_logging

logger = logging.getLogger(__name__)
DATABASE_PATH = Path(__file__).resolve().parent / "data" / "ybot.sqlite3"


class YbotClient(discord.Client):
    def __init__(self, config: Config, database: Database) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.config = config
        self.database = database
        self.todo_service = TodoService(database, config)
        self.todo_panel = TodoPanelManager(self, config, database, self.todo_service)
        self.classes = ClassCommands(self, config, database)
        self.knowledge = KnowledgeCommands(self, config, database)
        self.reminder_scheduler = ReminderScheduler(self, config, database)
        self.background = BackgroundServices(self, database, config)
        self.assistant = AssistantService(self, config, database, self.background.digests)

    async def setup_hook(self) -> None:
        await self.database.initialize()
        register_reminder_slash_commands(self.tree, self.config, self.database)
        register_todo_slash_commands(
            self.tree, self.config, self.todo_service, self.todo_panel
        )
        self.classes.register_slash_commands(self.tree)
        self.knowledge.register_slash_commands(self.tree)
        try:
            await self.tree.sync(guild=discord.Object(id=self.config.guild_id))
            logger.info("Synchronized guild-scoped slash commands")
        except discord.HTTPException:
            logger.exception("Failed to synchronize guild-scoped slash commands")
        await self.reminder_scheduler.restore_persistent_views()
        await self.todo_panel.restore_persistent_view()
        await self.assistant.initialize()

    async def on_ready(self) -> None:
        if self.user is None:
            logger.error("YBOT connected without a resolved bot user")
            return
        logger.info("YBOT is online as %s (ID %s)", self.user.name, self.user.id)
        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            logger.error(
                "Configured guild ID %s is not available to YBOT",
                self.config.guild_id,
            )
            return
        bootstrap = await bootstrap_guild(guild, self, self.config, self.database)
        if bootstrap.completed:
            try:
                await self.classes.reconcile_channels(guild)
            except Exception:
                logger.exception("Could not reconcile class channel mappings")
            await self.todo_panel.ensure_panel(guild)
            await self.background.start()
        await self.reminder_scheduler.start()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id != self.config.guild_id:
            logger.info("Joined non-configured guild ID %s; ignoring it", guild.id)
            return
        logger.info("Joined configured guild ID %s", guild.id)
        bootstrap = await bootstrap_guild(guild, self, self.config, self.database)
        if bootstrap.completed:
            try:
                await self.classes.reconcile_channels(guild)
            except Exception:
                logger.exception("Could not reconcile class channel mappings")
            await self.todo_panel.ensure_panel(guild)
            await self.background.start()
        await self.reminder_scheduler.start()

    async def on_message(self, message: discord.Message) -> None:
        await route_message(
            message,
            self.config,
            self.database,
            self.todo_service,
            self.todo_panel,
            self.knowledge,
            self.classes,
            self.assistant,
            self.background.digests,
        )

    async def on_disconnect(self) -> None:
        logger.warning("Discord connection disconnected; discord.py will reconnect")

    async def on_resumed(self) -> None:
        logger.info("Discord connection resumed")

    async def on_error(self, event_method: str, *args: object, **kwargs: object) -> None:
        # Event arguments can contain private messages: log only the event name.
        error_type = sys.exc_info()[0]
        logger.error("Unexpected Discord event error in %s (%s)", event_method, error_type.__name__ if error_type else "unknown")

    async def close(self) -> None:
        logger.info("YBOT is shutting down")
        try:
            await self.assistant.close()
            await self.background.stop()
            await self.reminder_scheduler.stop()
            await super().close()
        finally:
            await self.database.close()


async def run_client(client: YbotClient, config: Config) -> None:
    loop = asyncio.get_running_loop()
    closing: asyncio.Task | None = None

    def shutdown() -> None:
        nonlocal closing
        if closing is None:
            closing = asyncio.create_task(client.close())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown)
    try:
        async with client:
            await client.start(config.discord_token)
    finally:
        if closing is not None:
            await closing
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main() -> None:
    configure_logging()
    try:
        config = Config.from_env()
    except ConfigurationError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc

    configure_logging(config.log_level)
    database = Database(Path(config.database_path).expanduser())
    client = YbotClient(config, database)
    logger.info("Starting YBOT")
    try:
        asyncio.run(run_client(client, config))
    except discord.LoginFailure:
        logger.error("Discord login failed; verify DISCORD_TOKEN in .env")
        raise SystemExit(1) from None
    except Exception as exc:
        logger.error("YBOT terminated unexpectedly (%s)", type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
