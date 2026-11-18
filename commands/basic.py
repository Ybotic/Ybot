"""Owner-only basic command router."""

from __future__ import annotations

import logging
from typing import Any

import discord

from config import Config
from database.db import Database
from commands.classes import ClassCommands
from commands.knowledge import KnowledgeCommands
from commands.todo_panel import TodoPanelManager
from commands.reminders import handle_reminder_prefix
from commands.todos import handle_todo_prefix
from services.todo_service import TodoService
from services.class_service import infer_class_name
from services.assistant import AssistantService
from services.digest_service import DigestService

logger = logging.getLogger(__name__)

COMMAND_PREFIX = "!ybot"
HELP_TEXT = (
    "YBOT commands:\n"
    "`!ybot ping` — check that YBOT is responding.\n"
    "`!ybot help` — show this command list.\n"
    "`!ybot status` — show bot, server, database, and setup status.\n"
    "`!ybot remind me <time> <description>` — create a reminder.\n"
    "`!ybot reminders [today]` — list pending reminders.\n"
    "`!ybot complete|cancel|delete reminder <id>` — manage a reminder.\n"
    "`!ybot snooze reminder <id> <10m|30m|1h|tomorrow>` — snooze a reminder.\n"
    "`!ybot todo [task]` — open the task panel or quick-add a task.\n"
    "`!ybot note <content>` / `!ybot notes` — save or list private notes.\n"
    "`!ybot note edit|delete <id>` — edit or archive a note.\n"
    "`!ybot link <url>` / `!ybot links` — save or list links.\n"
    "`!ybot link edit|delete <id>` — edit or archive a link.\n"
    "`!ybot find <query>` — search notes, links, and tasks.\n"
    "`!ybot channel new|archive|restore <class>` — manage class channels.\n"
    "`!ybot class <name>` / `!ybot classes` — show class details or list classes.\n"
    "`!ybot digest` / `!ybot review` — generate a local daily/weekly overview.\n"
    "Other prefixed messages in #ybot use the optional conversational assistant."
)


async def route_message(
    message: Any,
    config: Config,
    database: Database,
    todo_service: TodoService | None = None,
    todo_panel: TodoPanelManager | None = None,
    knowledge: KnowledgeCommands | None = None,
    classes: ClassCommands | None = None,
    assistant: AssistantService | None = None,
    digests: DigestService | None = None,
) -> bool:
    """Handle an authorized command and return whether it was dispatched.

    Bot messages, DMs, other guilds, and messages without the command prefix
    are discarded before command parsing or database access.
    """
    author = message.author
    guild = message.guild
    if author.bot or guild is None or guild.id != config.guild_id:
        return False
    if not message.content.startswith(COMMAND_PREFIX):
        return False
    suffix = message.content[len(COMMAND_PREFIX):]
    if suffix and not suffix[0].isspace():
        return False
    if author.id != config.owner_id:
        return False

    command_text = message.content[len(COMMAND_PREFIX) :].strip()
    command_parts = command_text.split(maxsplit=1)
    command_name = command_parts[0].lower() if command_parts else ""
    arguments = command_parts[1] if len(command_parts) > 1 else ""

    fixed_commands = {
        "", "ping", "help", "status", "remind", "reminders", "cancel", "complete", "delete", "snooze",
        "todo", "note", "notes", "link", "links", "find", "channel", "class", "classes", "digest", "review",
    }
    if command_name not in fixed_commands:
        if assistant is not None:
            await assistant.respond(message, command_text)
        else:
            resource = await database.get_resource(config.guild_id, "text_channel", "ybot")
            if resource and message.channel.id == resource["resource_id"]:
                await message.channel.send("Conversational AI is disabled. Configure AI_ENABLED, OPENAI_API_KEY, and AI_MODEL in .env and restart.")
        return True

    if command_name in {"digest", "review"}:
        resource = await database.get_resource(config.guild_id, "text_channel", "ybot")
        if resource is None or message.channel.id != resource["resource_id"]:
            return True
        if digests is None:
            await message.channel.send("Digest generation is not available yet.")
            return True
        try:
            overview = await digests.generate("weekly" if command_name == "review" else "daily")
            await message.channel.send(embeds=overview.embeds(), allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            logger.error("Manual overview failed (%s)", type(exc).__name__)
            await message.channel.send("YBOT couldn't generate that overview. Please try again.")
        return True

    if command_name in {"channel", "class", "classes"}:
        try:
            private_channel = await database.get_resource(
                config.guild_id, "text_channel", "ybot"
            )
        except Exception:
            logger.exception("Could not verify private class command channel")
            return True
        if private_channel is None or message.channel.id != private_channel["resource_id"]:
            await message.channel.send(
                "For privacy, use class management commands in the private #ybot channel."
            )
            return True
        if classes is None:
            await message.channel.send("The class manager is not available yet.")
            return True
        try:
            response = await classes.handle_prefix(command_name, arguments)
            await message.channel.send(
                content=response.content,
                embed=response.embed,
                view=response.view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.exception("Class prefix command failed")
            await message.channel.send(
                "YBOT couldn't complete that class command. Please try again."
            )
        return True

    if command_name in {"note", "notes", "link", "links", "find"}:
        if knowledge is None:
            await message.channel.send("The knowledge manager is not available yet.")
            return True
        try:
            response = await knowledge.handle_prefix(command_name, arguments, message)
            if response.react_source:
                try:
                    await message.add_reaction("✅")
                except Exception:
                    logger.info("Could not add save confirmation reaction")
            destination = response.destination or message.channel
            await destination.send(
                content=response.content,
                embed=response.embed,
                view=response.view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.exception("Knowledge prefix command failed")
            await message.channel.send(
                "YBOT couldn't complete that knowledge command. Please try again."
            )
        return True

    if command_name == "todo":
        try:
            private_channel = await database.get_resource(
                config.guild_id, "text_channel", "todo"
            )
            registered_class = await infer_class_name(database, message.channel)
        except Exception:
            logger.exception("Could not verify private task command channel")
            return True
        in_private_channel = (
            private_channel is not None
            and message.channel.id == private_channel["resource_id"]
        )
        is_class_channel_add = (
            registered_class is not None
            and _is_task_creation(arguments)
        )
        if not in_private_channel and not is_class_channel_add:
            try:
                await message.channel.send(
                    "For privacy, use to-do commands in the private #todo channel."
                )
            except Exception:
                logger.exception("Failed to send to-do privacy guidance")
            return True
        if todo_service is None or todo_panel is None:
            await message.channel.send("The to-do manager is not available yet.")
            return True
        try:
            response = await handle_todo_prefix(
                arguments, message, config, todo_service, todo_panel
            )
            destination = message.channel
            if is_class_channel_add and not in_private_channel:
                destination = message.guild.get_channel(private_channel["resource_id"])
            if destination is None:
                logger.error("Private #todo response channel is unavailable")
                return True
            await destination.send(response.content, view=response.view)
        except Exception:
            logger.exception("To-do prefix command failed")
            await message.channel.send(
                "YBOT couldn't complete that task command. Please try again."
            )
        return True

    reminder_response_channel = message.channel
    if command_name in {"remind", "reminders", "cancel", "complete", "delete", "snooze"}:
        try:
            private_channel = await database.get_resource(
                config.guild_id, "text_channel", "ybot"
            )
            registered_class = (
                await infer_class_name(database, message.channel)
                if command_name == "remind"
                else None
            )
        except Exception:
            logger.exception("Could not verify private reminder command channel")
            return True
        in_private_channel = (
            private_channel is not None
            and message.channel.id == private_channel["resource_id"]
        )
        if not in_private_channel and registered_class is None:
            try:
                await message.channel.send(
                    "For privacy, use reminder commands in the private #ybot channel."
                )
            except Exception:
                logger.exception("Failed to send reminder privacy guidance")
            return True
        if registered_class is not None and not in_private_channel:
            reminder_response_channel = (
                message.guild.get_channel(private_channel["resource_id"])
                if private_channel is not None
                else None
            )
            if reminder_response_channel is None:
                await message.channel.send(
                    "Private #ybot is unavailable; run server bootstrap before saving reminders."
                )
                return True

    try:
        response = await _get_response(
            command_name, arguments, guild, config, database, message.channel
        )
    except Exception:
        logger.exception("Command handler failed for command %r", command_name)
        response = "YBOT couldn't complete that command. Please try again later."

    try:
        await reminder_response_channel.send(response)
    except Exception:
        logger.exception("Failed to send response for command %r", command_name)
    return True


async def _get_response(
    command_name: str,
    arguments: str,
    guild: Any,
    config: Config,
    database: Database,
    channel: Any | None = None,
) -> str:
    if command_name == "ping":
        return "Pong! YBOT is online."
    if command_name == "help":
        return HELP_TEXT
    if command_name == "status":
        db_connected = await database.ping()
        if db_connected:
            settings = await database.get_server_config(guild.id)
            bootstrap_status = (
                "Complete"
                if settings is not None and settings["setup_completed"]
                else "Incomplete"
            )
            database_status = "Connected"
        else:
            bootstrap_status = "Unknown"
            database_status = "Unavailable"
        return (
            "YBOT status:\n"
            "Bot: Online\n"
            f"Server: {guild.name}\n"
            f"Database: {database_status}\n"
            f"Server bootstrap: {bootstrap_status}"
        )
    reminder_response = await handle_reminder_prefix(
        command_name, arguments, config, database, channel=channel
    )
    if reminder_response is not None:
        return reminder_response
    if not command_name:
        return "Please choose a command. Use `!ybot help` to see available commands."
    return f"Unknown command. Use `!ybot help` to see available commands."


def _is_task_creation(arguments: str) -> bool:
    text = arguments.strip()
    if not text:
        return False
    first = text.split(maxsplit=1)[0].casefold()
    if first == "add":
        return True
    return first not in {"list", "done", "remove", "edit", "completed", "clear"}
