"""Owner-only class/channel management and class overviews."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiosqlite
import discord
from discord import app_commands

from commands.class_ui import ClassOverviewView, ClassRecreateView
from config import Config
from database.db import Database
from services.class_service import (
    ClassService,
    ClassValidationError,
    class_name_key,
    normalize_class_name,
)
from services.reminder_format import format_local_due, format_remaining

logger = logging.getLogger(__name__)
CLASS_TOPIC_PREFIX = "YBOT_CLASS:"


@dataclass(slots=True)
class ClassCommandResponse:
    content: str | None = None
    embed: discord.Embed | None = None
    view: discord.ui.View | None = None


class ClassCommands:
    def __init__(self, client: Any, config: Config, database: Database) -> None:
        self.client = client
        self.config = config
        self.database = database
        self.service = ClassService(database)
        self._operation_lock = asyncio.Lock()

    async def reconcile_channels(self, guild: Any) -> None:
        """Register pre-Phase-5 channels already placed under CLASSES."""
        if guild.id != self.config.guild_id:
            return
        category = await self._classes_category(guild)
        if category is None:
            logger.warning("Cannot reconcile class channels: CLASSES category is missing")
            return
        for channel in getattr(guild, "text_channels", ()):
            if channel.category_id != category.id:
                continue
            marker_key, marker_status = _read_class_topic(
                getattr(channel, "topic", None)
            )
            source_channel_name = channel.name
            if marker_status == "archived" and source_channel_name.startswith("archived-"):
                source_channel_name = source_channel_name[len("archived-") :]
            source_name = source_channel_name.replace("-", " ").replace("_", " ").upper()
            try:
                display_name, normalized_name, _ = normalize_class_name(source_name)
            except ClassValidationError:
                logger.warning("Skipping unsupported channel under CLASSES")
                continue
            if marker_key is not None:
                if class_name_key(marker_key) != marker_key:
                    logger.warning("Skipping class channel with an invalid YBOT topic marker")
                    continue
                normalized_name = marker_key
            record = await self.database.get_class_by_name(
                guild.id, normalized_name
            )
            desired_status = marker_status or "active"
            if record is None:
                try:
                    class_id = await self.database.create_class(
                        guild_id=guild.id,
                        class_name=display_name,
                        normalized_name=normalized_name,
                        category_id=category.id,
                    )
                except aiosqlite.IntegrityError:
                    record = await self.database.get_class_by_name(
                        guild.id, normalized_name
                    )
                    if record is None:
                        logger.exception("Could not register legacy class channel")
                        continue
                    class_id = record["id"]
                else:
                    record = await self.database.get_class_by_id(guild.id, class_id)
                await self.database.update_class_channel(
                    guild.id,
                    class_id,
                    channel_id=channel.id,
                    category_id=category.id,
                )
                if desired_status == "archived":
                    await self.database.set_class_status(guild.id, class_id, "archived")
                record = await self.database.get_class_by_id(guild.id, class_id)

            if record is None or record["status"] == "archived":
                continue
            if record.get("channel_id") not in {None, channel.id}:
                existing_channel = guild.get_channel(record["channel_id"])
                if existing_channel is not None:
                    logger.warning(
                        "Multiple channels appear to map to class %s; leaving the extra channel unregistered",
                        record["class_name"],
                    )
                    continue
            if record.get("channel_id") != channel.id:
                await self.database.update_class_channel(
                    guild.id,
                    record["id"],
                    channel_id=channel.id,
                    category_id=category.id,
                )
            if marker_key != normalized_name or marker_status != "active":
                try:
                    await channel.edit(
                        topic=_class_topic(normalized_name, "active"),
                        reason="YBOT registered an existing class channel",
                    )
                except discord.HTTPException:
                    logger.exception(
                        "Could not set class marker topic for channel ID %s", channel.id
                    )
            logger.info(
                "Registered active class channel #%s to class %s",
                channel.name,
                record["class_name"],
            )

    async def authorize_interaction(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.guild_id == self.config.guild_id
            and interaction.user.id == self.config.owner_id
        ):
            return True
        await interaction.response.send_message(
            "Only the configured owner can manage classes in the configured server.",
            ephemeral=True,
        )
        return False

    async def handle_prefix(
        self, command_name: str, arguments: str
    ) -> ClassCommandResponse:
        if command_name == "channel":
            action, _, value = arguments.strip().partition(" ")
            if action.casefold() == "new":
                return await self.create_class(value)
            if action.casefold() == "archive":
                return ClassCommandResponse(await self.archive_class(value))
            if action.casefold() == "restore":
                return ClassCommandResponse(await self.restore_class(value))
            return ClassCommandResponse(
                "Use `!ybot channel new <class>`, `archive <class>`, or `restore <class>`."
            )
        if command_name == "class":
            if not arguments.strip():
                return ClassCommandResponse("Use `!ybot class <name>`.")
            return await self.class_overview(arguments.strip())
        if command_name == "classes":
            status = arguments.strip().casefold() or "active"
            if status not in {"active", "archived", "all"}:
                return ClassCommandResponse(
                    "Use `!ybot classes [active|archived|all]`."
                )
            return await self.class_list(status)
        return ClassCommandResponse(
            "Use `!ybot channel new`, `!ybot channel archive`, `!ybot channel restore`, "
            "`!ybot class`, or `!ybot classes`."
        )

    async def create_class(self, name: str) -> ClassCommandResponse:
        try:
            class_name, normalized_name, channel_name = normalize_class_name(name)
        except ClassValidationError as exc:
            return ClassCommandResponse(str(exc))

        async with self._operation_lock:
            guild = self.client.get_guild(self.config.guild_id)
            if guild is None:
                return ClassCommandResponse("The configured server is unavailable.")
            existing = await self.service.get_by_name(
                self.config.guild_id, class_name
            )
            if existing is not None:
                if existing["status"] == "archived":
                    return ClassCommandResponse(
                        f"{existing['class_name']} is archived. Restore it instead of creating a duplicate.",
                        view=ClassRecreateView(self, existing["id"], restore=True),
                    )
                channel = await self._locate_class_channel(guild, existing)
                if channel is not None:
                    return ClassCommandResponse(
                        f"Class {existing['class_name']} already exists in {channel.mention}."
                    )
                return ClassCommandResponse(
                    f"Class {existing['class_name']} is registered, but its channel is missing. "
                    "Use the button to recreate it.",
                    view=ClassRecreateView(self, existing["id"]),
                )

            category = await self._classes_category(guild)
            if category is None:
                return ClassCommandResponse(
                    "The CLASSES category is missing. Restart YBOT to run server bootstrap."
                )
            orphan = self._find_topic_channel(
                guild, normalized_name, category.id
            )
            if orphan is not None:
                collision = self._channel_with_name(
                    guild, category.id, channel_name, exclude_id=orphan.id
                )
                if collision is not None:
                    return ClassCommandResponse(
                        f"A different channel named `#{channel_name}` already exists under CLASSES. "
                        "Rename it before registering this class."
                    )
            if orphan is None:
                collision = self._channel_with_name(guild, category.id, channel_name)
                if collision is not None:
                    return ClassCommandResponse(
                        f"A channel named `#{channel_name}` already exists under CLASSES "
                        "and is not registered to this class. Rename it before creating the class."
                    )
            try:
                class_id = await self.database.create_class(
                    guild_id=self.config.guild_id,
                    class_name=class_name,
                    normalized_name=normalized_name,
                    category_id=category.id,
                )
            except aiosqlite.IntegrityError:
                existing = await self.service.get_by_name(
                    self.config.guild_id, class_name
                )
                return ClassCommandResponse(
                    f"Class {existing['class_name'] if existing else class_name} already exists."
                )

            if orphan is not None:
                await self.database.update_class_channel(
                    self.config.guild_id,
                    class_id,
                    channel_id=orphan.id,
                    category_id=category.id,
                )
                try:
                    await orphan.edit(
                        name=channel_name,
                        topic=_class_topic(normalized_name, "active"),
                        reason="YBOT class channel mapping recovery",
                    )
                except discord.HTTPException:
                    logger.exception("Could not reconcile orphaned class channel")
                return ClassCommandResponse(
                    f"Class {class_name} was registered to the existing channel {orphan.mention}."
                )

            try:
                channel = await guild.create_text_channel(
                    channel_name,
                    category=category,
                    topic=_class_topic(normalized_name, "active"),
                    reason=f"YBOT class channel for {class_name}",
                )
            except discord.HTTPException:
                logger.exception("Could not create channel for class ID %s", class_id)
                return ClassCommandResponse(
                    f"Class {class_name} is registered, but Discord could not create its channel. "
                    "Use the button to retry.",
                    view=ClassRecreateView(self, class_id),
                )
            try:
                await self.database.update_class_channel(
                    self.config.guild_id,
                    class_id,
                    channel_id=channel.id,
                    category_id=category.id,
                )
            except Exception:
                logger.exception("Could not persist channel for class ID %s", class_id)
                return ClassCommandResponse(
                    f"Channel {channel.mention} was created, but its mapping could not be saved. "
                    "Use the retry button to reconcile it.",
                    view=ClassRecreateView(self, class_id),
                )
            return ClassCommandResponse(
                f"Class {class_name} created in {channel.mention}.",
                view=ClassOverviewView(self, await self.service.get(self.config.guild_id, class_id), channel),
            )

    async def recreate_channel(self, class_id: int) -> str:
        async with self._operation_lock:
            record = await self.service.get(self.config.guild_id, class_id)
            if record is None:
                return "That class record was not found."
            if record["status"] == "archived":
                return "That class is archived; use its Restore Class action."
            channel, error = await self._ensure_class_channel(record, activate=False)
            if error:
                return error
            return f"Class channel restored: {channel.mention}."

    async def archive_class(self, name: str) -> str:
        try:
            _, normalized_name, _ = normalize_class_name(name)
        except ClassValidationError as exc:
            return str(exc)
        async with self._operation_lock:
            record = await self.database.get_class_by_name(
                self.config.guild_id, normalized_name
            )
            if record is None:
                return f"Class {name} was not found."
            if record["status"] == "archived":
                return f"Class {record['class_name']} is already archived."
            await self.database.set_class_status(
                self.config.guild_id, record["id"], "archived"
            )
            guild = self.client.get_guild(self.config.guild_id)
            channel = await self._locate_class_channel(guild, record) if guild else None
            if channel is None:
                return f"Class {record['class_name']} archived. Its channel was already missing."
            _, _, slug = normalize_class_name(record["class_name"])
            archived_slug = f"archived-{slug}"
            collision = self._channel_with_name(
                guild, channel.category_id, archived_slug, exclude_id=channel.id
            )
            if collision is not None:
                return (
                    f"Class {record['class_name']} is archived in YBOT, but another channel "
                    f"already uses `#{archived_slug}`. Rename that channel to finish visual archiving."
                )
            try:
                await channel.edit(
                    name=archived_slug,
                    topic=_class_topic(record["normalized_name"], "archived"),
                    reason=f"YBOT archived class {record['class_name']}",
                )
            except discord.HTTPException:
                logger.exception("Could not mark archived channel for class ID %s", record["id"])
                return (
                    f"Class {record['class_name']} is archived in YBOT, but Discord could not "
                    "rename its channel. The class will no longer be inferred automatically."
                )
            return f"Class {record['class_name']} archived. Its channel and content are preserved."

    async def restore_class(self, name: str) -> str:
        try:
            _, normalized_name, _ = normalize_class_name(name)
        except ClassValidationError as exc:
            return str(exc)
        record = await self.database.get_class_by_name(
            self.config.guild_id, normalized_name
        )
        if record is None:
            return f"Class {name} was not found."
        return await self.restore_class_by_id(record["id"])

    async def restore_class_by_id(self, class_id: int) -> str:
        async with self._operation_lock:
            record = await self.service.get(self.config.guild_id, class_id)
            if record is None:
                return "That class record was not found."
            if record["status"] == "active":
                channel, error = await self._ensure_class_channel(record, activate=False)
                return error or f"Class {record['class_name']} is already active in {channel.mention}."
            channel, error = await self._ensure_class_channel(record, activate=True)
            if error:
                return error
            return f"Class {record['class_name']} restored in {channel.mention}."

    async def class_overview(self, name: str) -> ClassCommandResponse:
        try:
            _, normalized_name, _ = normalize_class_name(name)
        except ClassValidationError as exc:
            return ClassCommandResponse(str(exc))
        record = await self.database.get_class_by_name(
            self.config.guild_id, normalized_name
        )
        if record is None:
            return ClassCommandResponse(f"Class {name} was not found.")
        guild = self.client.get_guild(self.config.guild_id)
        channel = await self._locate_class_channel(guild, record) if guild else None
        counts = await self.database.class_counts(record["class_name"])
        notes = await self.database.list_notes(
            limit=3, class_name=record["class_name"]
        )
        links = await self.database.list_links(
            limit=3, class_name=record["class_name"]
        )
        upcoming = await self.database.list_pending_reminders(
            class_name=record["class_name"],
            due_from=datetime.now(timezone.utc),
            limit=1,
        )

        status = record["status"].title()
        channel_value = (
            f"{channel.mention} ([open channel]({channel.jump_url}))"
            if channel is not None
            else "Channel missing. Use `!ybot channel new <name>` to recreate it."
        )
        embed = discord.Embed(
            title=f"Class · {discord.utils.escape_markdown(record['class_name'])}",
            description=(
                f"Status: **{status}**\n"
                f"Channel: {channel_value}\n"
                f"Created: {format_local_due(record['created_at'], self.config.timezone_name)}"
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Tasks",
            value=(
                f"Open: **{counts['open_todos']}**\n"
                f"Completed: **{counts['completed_todos']}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Knowledge",
            value=(
                f"Saved notes: **{counts['notes']}**\n"
                f"Saved links: **{counts['links']}**"
            ),
            inline=True,
        )
        reminder_lines = [f"Pending: **{counts['reminders']}**"]
        if upcoming:
            reminder = upcoming[0]
            reminder_lines.append(
                f"Next: {format_local_due(reminder['due_at'], self.config.timezone_name)}"
            )
            reminder_lines.append(discord.utils.escape_mentions(reminder["description"])[:120])
        else:
            reminder_lines.append("Next: none scheduled")
        embed.add_field(name="Reminders", value="\n".join(reminder_lines), inline=False)
        recent_notes = [
            f"`#{note['id']}` {discord.utils.escape_mentions(note['title'] or 'Untitled note')}"
            for note in notes
        ]
        embed.add_field(
            name="Recent notes",
            value="\n".join(recent_notes) if recent_notes else "None",
            inline=False,
        )
        recent_links = [
            f"`#{link['id']}` [{discord.utils.escape_mentions(link['title'])}]({link['url']})"
            for link in links
        ]
        embed.add_field(
            name="Recent links",
            value="\n".join(recent_links) if recent_links else "None",
            inline=False,
        )
        return ClassCommandResponse(
            embed=embed,
            view=ClassOverviewView(self, record, channel),
        )

    async def class_list(self, status: str = "active") -> ClassCommandResponse:
        if status not in {"active", "archived", "all"}:
            return ClassCommandResponse("Class list status must be active, archived, or all.")
        records = await self.service.list(self.config.guild_id, status=status)
        embed = discord.Embed(
            title=f"{status.title()} classes",
            color=discord.Color.blurple(),
        )
        if not records:
            embed.description = "No classes found."
            return ClassCommandResponse(embed=embed)
        for record in records[:25]:
            channel = await self._locate_class_channel(
                self.client.get_guild(self.config.guild_id), record
            )
            channel_text = (
                f"Channel: {channel.mention}"
                if channel is not None
                else "Channel: missing"
            )
            embed.add_field(
                name=f"#{record['id']} · {discord.utils.escape_markdown(record['class_name'])}",
                value=(
                    f"{channel_text}\nStatus: {record['status'].title()}\n"
                    f"Overview: `!ybot class {record['class_name']}`"
                ),
                inline=False,
            )
        if len(records) > 25:
            embed.set_footer(text=f"Showing 25 of {len(records)} classes")
        return ClassCommandResponse(embed=embed)

    async def _ensure_class_channel(
        self, record: dict[str, Any], *, activate: bool
    ) -> tuple[Any | None, str | None]:
        guild = self.client.get_guild(self.config.guild_id)
        if guild is None:
            return None, "The configured server is unavailable."
        category = await self._classes_category(guild)
        if category is None:
            return None, "The CLASSES category is missing. Restart YBOT to run bootstrap."
        _, normalized_name, channel_name = normalize_class_name(record["class_name"])
        channel = await self._locate_class_channel(guild, record)
        if channel is not None:
            collision = self._channel_with_name(
                guild, category.id, channel_name, exclude_id=channel.id
            )
            if collision is not None:
                return None, (
                    f"A different channel named `#{channel_name}` already exists under CLASSES. "
                    "Rename it before restoring this class."
                )
            try:
                await channel.edit(
                    name=channel_name,
                    category=category,
                    topic=_class_topic(normalized_name, "active"),
                    reason=f"YBOT restored class {record['class_name']}",
                )
            except discord.HTTPException:
                logger.exception("Could not restore Discord channel for class ID %s", record["id"])
                return None, "Discord could not update the class channel. Check Manage Channels."
            await self.database.update_class_channel(
                self.config.guild_id,
                record["id"],
                channel_id=channel.id,
                category_id=category.id,
            )
            if activate:
                await self.database.set_class_status(
                    self.config.guild_id, record["id"], "active"
                )
            return channel, None

        collision = self._channel_with_name(guild, category.id, channel_name)
        if collision is not None:
            return None, (
                f"A different channel named `#{channel_name}` already exists under CLASSES. "
                "Rename it before YBOT recreates the class channel."
            )
        try:
            channel = await guild.create_text_channel(
                channel_name,
                category=category,
                topic=_class_topic(normalized_name, "active"),
                reason=f"YBOT recreated class channel for {record['class_name']}",
            )
        except discord.HTTPException:
            logger.exception("Could not create Discord channel for class ID %s", record["id"])
            return None, "Discord could not create the class channel. Check Manage Channels."
        await self.database.update_class_channel(
            self.config.guild_id,
            record["id"],
            channel_id=channel.id,
            category_id=category.id,
        )
        if activate:
            await self.database.set_class_status(
                self.config.guild_id, record["id"], "active"
            )
        return channel, None

    async def _locate_class_channel(
        self, guild: Any, record: dict[str, Any]
    ) -> Any | None:
        if guild is None:
            return None
        if record.get("channel_id") is not None:
            channel = guild.get_channel(record["channel_id"])
            if channel is not None:
                return channel
        channel = self._find_topic_channel(
            guild, record["normalized_name"], record.get("category_id")
        )
        if channel is not None and channel.id != record.get("channel_id"):
            await self.database.update_class_channel(
                self.config.guild_id,
                record["id"],
                channel_id=channel.id,
                category_id=channel.category_id or record["category_id"],
            )
        return channel

    async def _classes_category(self, guild: Any) -> Any | None:
        resource = await self.database.get_resource(
            self.config.guild_id, "category", "CLASSES"
        )
        if resource is not None:
            category = guild.get_channel(resource["resource_id"])
            if category is not None:
                return category
        return next(
            (category for category in guild.categories if category.name.casefold() == "classes"),
            None,
        )

    @staticmethod
    def _find_topic_channel(
        guild: Any, normalized_name: str, category_id: int | None
    ) -> Any | None:
        return next(
            (
                channel
                for channel in getattr(guild, "text_channels", ())
                if (category_id is None or channel.category_id == category_id)
                and _read_class_topic(getattr(channel, "topic", None))[0]
                == normalized_name
            ),
            None,
        )

    @staticmethod
    def _channel_with_name(
        guild: Any,
        category_id: int,
        channel_name: str,
        *,
        exclude_id: int | None = None,
    ) -> Any | None:
        return next(
            (
                channel
                for channel in getattr(guild, "text_channels", ())
                if channel.category_id == category_id
                and channel.name.casefold() == channel_name.casefold()
                and channel.id != exclude_id
            ),
            None,
        )

    def register_slash_commands(self, tree: app_commands.CommandTree) -> None:
        target_guild = discord.Object(id=self.config.guild_id)
        status_choices = [
            app_commands.Choice(name="Active", value="active"),
            app_commands.Choice(name="Archived", value="archived"),
            app_commands.Choice(name="All", value="all"),
        ]

        @app_commands.command(name="class-create", description="Create a class channel.")
        @app_commands.describe(name="Class name, e.g. CMSC216")
        async def class_create_command(
            interaction: discord.Interaction, name: str
        ) -> None:
            if not await self.authorize_interaction(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                response = await self.create_class(name)
            except Exception:
                logger.exception("Slash class creation failed")
                response = ClassCommandResponse(
                    "YBOT couldn't create that class. Please try again."
                )
            await _send_followup(interaction, response)

        @app_commands.command(name="class", description="Show a class overview.")
        @app_commands.describe(name="Class name")
        async def class_overview_command(
            interaction: discord.Interaction, name: str
        ) -> None:
            if not await self.authorize_interaction(interaction):
                return
            try:
                response = await self.class_overview(name)
                await _send_response(interaction, response, ephemeral=True)
            except Exception:
                logger.exception("Slash class overview failed")
                await interaction.response.send_message(
                    "YBOT couldn't load that class overview.", ephemeral=True
                )

        @app_commands.command(name="classes", description="List registered classes.")
        @app_commands.choices(status=status_choices)
        async def classes_command(
            interaction: discord.Interaction, status: str = "active"
        ) -> None:
            if not await self.authorize_interaction(interaction):
                return
            try:
                response = await self.class_list(status)
                await _send_response(interaction, response, ephemeral=True)
            except Exception:
                logger.exception("Slash class listing failed")
                await interaction.response.send_message(
                    "YBOT couldn't load the class list.", ephemeral=True
                )

        @app_commands.command(name="class-archive", description="Archive a class channel.")
        @app_commands.describe(name="Class name")
        async def class_archive_command(
            interaction: discord.Interaction, name: str
        ) -> None:
            if not await self.authorize_interaction(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                response = await self.archive_class(name)
            except Exception:
                logger.exception("Slash class archive failed")
                response = "YBOT couldn't archive that class. Please try again."
            await interaction.followup.send(response, ephemeral=True)

        @app_commands.command(name="class-restore", description="Restore an archived class.")
        @app_commands.describe(name="Class name")
        async def class_restore_command(
            interaction: discord.Interaction, name: str
        ) -> None:
            if not await self.authorize_interaction(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                response = await self.restore_class(name)
            except Exception:
                logger.exception("Slash class restoration failed")
                response = "YBOT couldn't restore that class. Please try again."
            await interaction.followup.send(response, ephemeral=True)

        for command in (
            class_create_command,
            class_overview_command,
            classes_command,
            class_archive_command,
            class_restore_command,
        ):
            tree.add_command(command, guild=target_guild)


def _class_topic(normalized_name: str, status: str) -> str:
    return f"{_class_topic_marker(normalized_name)} | STATUS:{status.upper()}"


def _class_topic_marker(normalized_name: str) -> str:
    return f"YBOT_CLASS:{normalized_name}"


def _read_class_topic(topic: str | None) -> tuple[str | None, str | None]:
    if not topic:
        return None, None
    parts = [part.strip() for part in topic.split("|")]
    marker = parts[0]
    if not marker.casefold().startswith(CLASS_TOPIC_PREFIX.casefold()):
        return None, None
    normalized_name = marker[len(CLASS_TOPIC_PREFIX) :].strip().casefold()
    status = "active"
    for part in parts[1:]:
        if part.casefold().startswith("status:"):
            candidate = part.partition(":")[2].strip().casefold()
            if candidate in {"active", "archived"}:
                status = candidate
    return normalized_name, status


def _archived_channel_name(channel_name: str) -> str:
    return f"archived-{channel_name}"[:100]


async def _send_response(
    interaction: discord.Interaction,
    response: ClassCommandResponse,
    *,
    ephemeral: bool,
) -> None:
    await interaction.response.send_message(
        content=response.content,
        embed=response.embed,
        view=response.view,
        ephemeral=ephemeral,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def _send_followup(
    interaction: discord.Interaction,
    response: ClassCommandResponse,
) -> None:
    await interaction.followup.send(
        content=response.content,
        embed=response.embed,
        view=response.view,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
