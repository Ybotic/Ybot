"""Prefix/slash command coordination for notes, links, and unified search."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import discord
from discord import app_commands

from commands.knowledge_ui import LinkEditLauncherView, NoteEditLauncherView, NoteModal
from commands.search_view import SearchResultsView, build_search_embed
from config import Config
from database.db import Database
from services.link_extractor import LinkMetadataExtractor
from services.link_service import LinkService, LinkServiceError
from services.note_service import NoteService, NoteValidationError
from services.reminder_format import parse_utc_timestamp
from services.class_service import infer_class_name
from services.search_service import (
    SEARCH_TYPES,
    SearchService,
    SearchValidationError,
)

logger = logging.getLogger(__name__)
_RESOURCE_ID_RE = re.compile(r"^[1-9]\d{0,18}$")


@dataclass(slots=True)
class KnowledgePrefixResponse:
    content: str | None = None
    embed: discord.Embed | None = None
    view: discord.ui.View | None = None
    destination: Any | None = None
    react_source: bool = False


class KnowledgeCommands:
    def __init__(
        self, client: Any, config: Config, database: Database
    ) -> None:
        self.client = client
        self.config = config
        self.database = database
        self.notes = NoteService(database)
        self.link_extractor = LinkMetadataExtractor()
        self.links = LinkService(database, self.link_extractor)
        self.search = SearchService(database)

    async def authorize_interaction(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.guild_id == self.config.guild_id
            and interaction.user.id == self.config.owner_id
        ):
            return True
        await interaction.response.send_message(
            "Only the configured owner can manage knowledge in the configured server.",
            ephemeral=True,
        )
        return False

    async def handle_prefix(
        self, command_name: str, arguments: str, message: Any
    ) -> KnowledgePrefixResponse:
        private_channel = await self._resolve_channel(
            message.guild, "ybot", resource_name="ybot"
        )
        in_private_channel = (
            private_channel is not None and message.channel.id == private_channel.id
        )
        is_class_channel = await infer_class_name(self.database, message.channel) is not None
        is_save_command = self._is_save_command(command_name, arguments)
        if is_class_channel and is_save_command and private_channel is None:
            return KnowledgePrefixResponse(
                "Private #ybot is unavailable; run server bootstrap before saving knowledge.",
                destination=message.channel,
            )
        if not in_private_channel and not (is_class_channel and is_save_command):
            return KnowledgePrefixResponse(
                "For privacy, use notes, links, and search in the private #ybot channel.",
                destination=message.channel,
            )

        response_destination = private_channel
        if command_name == "note":
            return await self._prefix_note(arguments, message, response_destination)
        if command_name == "notes":
            return await self._prefix_notes(arguments, response_destination)
        if command_name == "link":
            return await self._prefix_link(arguments, message, response_destination)
        if command_name == "links":
            return await self._prefix_links(arguments, response_destination)
        if command_name == "find":
            return await self._prefix_find(arguments, response_destination)
        return KnowledgePrefixResponse(
            "Use `!ybot note`, `!ybot link`, `!ybot notes`, `!ybot links`, or `!ybot find`.",
            destination=response_destination or message.channel,
        )

    @staticmethod
    def _is_save_command(command_name: str, arguments: str) -> bool:
        text = arguments.strip()
        if not text:
            return False
        first, _, remainder = text.partition(" ")
        is_management = (
            first.casefold() in {"edit", "delete"}
            and _parse_id(remainder) is not None
        )
        return command_name in {"note", "link"} and not is_management

    async def _prefix_note(
        self, arguments: str, message: Any, destination: Any | None
    ) -> KnowledgePrefixResponse:
        text = arguments.strip()
        command, _, remainder = text.partition(" ")
        note_id = _parse_id(remainder)
        if command.casefold() == "edit" and note_id is not None:
            note = await self.notes.get_note(note_id)
            if note is None or note["is_archived"]:
                return KnowledgePrefixResponse(
                    f"Note #{note_id} was not found or is archived.", destination=destination
                )
            return KnowledgePrefixResponse(
                f"Edit private note #{note_id} using the button below.",
                view=NoteEditLauncherView(self, note_id),
                destination=destination,
            )
        if command.casefold() == "delete" and note_id is not None:
            archived = await self.notes.archive_note(note_id)
            message_text = (
                f"Note #{note_id} archived."
                if archived
                else f"Note #{note_id} was not found or is already archived."
            )
            return KnowledgePrefixResponse(message_text, destination=destination)

        try:
            title, content, class_name, tags = _parse_note_input(text)
            note_id = await self.notes.create_note(
                content,
                title=title,
                class_name=class_name,
                tags=tags,
                channel=message.channel,
            )
        except NoteValidationError as exc:
            return KnowledgePrefixResponse(str(exc), destination=destination)
        return KnowledgePrefixResponse(
            f"Private note #{note_id} saved.",
            destination=destination,
            react_source=True,
        )

    async def _prefix_notes(
        self, arguments: str, destination: Any | None
    ) -> KnowledgePrefixResponse:
        mode = arguments.strip().casefold()
        if mode not in {"", "recent"}:
            return KnowledgePrefixResponse(
                "Use `!ybot notes` or `!ybot notes recent`.", destination=destination
            )
        notes = await self.notes.list_notes(limit=10)
        return KnowledgePrefixResponse(
            embed=_notes_embed(notes, "Recent private notes"),
            destination=destination,
        )

    async def _prefix_link(
        self, arguments: str, message: Any, destination: Any | None
    ) -> KnowledgePrefixResponse:
        text = arguments.strip()
        command, _, remainder = text.partition(" ")
        if command.casefold() == "delete":
            link_id = _parse_id(remainder)
            if link_id is None:
                return KnowledgePrefixResponse("Use `!ybot link delete <id>`.", destination=destination)
            archived = await self.links.archive_link(link_id)
            if archived:
                archived_link = await self.links.get_link(link_id)
                if archived_link is not None:
                    await self.sync_link_feed(archived_link)
            response = (
                f"Saved link #{link_id} archived."
                if archived
                else f"Saved link #{link_id} was not found or is already archived."
            )
            return KnowledgePrefixResponse(response, destination=destination)
        if command.casefold() == "edit":
            link_id = _parse_id(remainder)
            if link_id is None:
                return KnowledgePrefixResponse("Use `!ybot link edit <id>`.", destination=destination)
            link = await self.links.get_link(link_id)
            if link is None or link["is_archived"]:
                return KnowledgePrefixResponse(
                    f"Saved link #{link_id} was not found or is archived.",
                    destination=destination,
                )
            return KnowledgePrefixResponse(
                f"Edit saved link #{link_id} using the button below.",
                view=LinkEditLauncherView(self, link_id),
                destination=destination,
            )

        url_text, fields, error = _parse_link_input(text)
        if error:
            return KnowledgePrefixResponse(error, destination=destination)
        try:
            link = await self.links.save_link(
                url_text,
                class_name=fields.get("class"),
                tags=fields.get("tags"),
                channel=message.channel,
            )
        except LinkServiceError as exc:
            return KnowledgePrefixResponse(str(exc), destination=destination)
        posted = await self.sync_link_feed(link)
        response = f"Saved link #{link['id']}: {discord.utils.escape_mentions(link['title'])}."
        if link["extraction_status"] == "failed":
            response += " Metadata could not be retrieved; the URL is saved."
        if not posted:
            response += " The link is saved, but its #links post could not be created."
        return KnowledgePrefixResponse(
            response,
            destination=destination,
            react_source=True,
        )

    async def _prefix_links(
        self, arguments: str, destination: Any | None
    ) -> KnowledgePrefixResponse:
        mode = arguments.strip().casefold()
        if mode not in {"", "recent"}:
            return KnowledgePrefixResponse(
                "Use `!ybot links` or `!ybot links recent`.", destination=destination
            )
        links = await self.links.list_links(limit=10)
        return KnowledgePrefixResponse(
            embed=_links_embed(links, "Recent saved links"),
            destination=destination,
        )

    async def _prefix_find(
        self, arguments: str, destination: Any | None
    ) -> KnowledgePrefixResponse:
        query, resource_type, class_name, error = _parse_search_input(arguments)
        if error:
            return KnowledgePrefixResponse(error, destination=destination)
        try:
            page = await self.search.search(
                query, resource_type=resource_type, class_name=class_name
            )
        except SearchValidationError as exc:
            return KnowledgePrefixResponse(str(exc), destination=destination)
        return KnowledgePrefixResponse(
            embed=build_search_embed(page, self.config.timezone_name),
            view=SearchResultsView(self, page),
            destination=destination,
        )

    async def sync_link_feed(self, link: dict[str, Any]) -> bool:
        guild = self.client.get_guild(self.config.guild_id)
        if guild is None:
            logger.error("Cannot publish saved link: configured guild is unavailable")
            return False
        try:
            channel = await self._resolve_channel(
                guild, "links", resource_name="links"
            )
        except Exception:
            logger.exception("Could not resolve #links for saved link ID %s", link["id"])
            return False
        if channel is None:
            logger.error(
                "Cannot publish saved link: #links is missing. Run YBOT server bootstrap."
            )
            return False
        embed = _link_feed_embed(link)
        message_id = link.get("discord_message_id")
        if message_id is not None:
            try:
                message = await channel.fetch_message(message_id)
                await message.edit(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return True
            except discord.NotFound:
                message_id = None
            except discord.HTTPException:
                logger.exception("Could not synchronize link feed message for ID %s", link["id"])
                return False
        try:
            message = await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.database.set_link_discord_message_id(link["id"], message.id)
            return True
        except Exception:
            logger.exception("Could not post saved link ID %s to #links", link["id"])
            return False

    async def _resolve_channel(
        self, guild: Any, channel_name: str, *, resource_name: str
    ) -> Any | None:
        resource = await self.database.get_resource(
            self.config.guild_id, "text_channel", resource_name
        )
        if resource is not None:
            channel = guild.get_channel(resource["resource_id"])
            if channel is not None:
                return channel
        return next(
            (
                channel
                for channel in getattr(guild, "text_channels", ())
                if channel.name == channel_name
            ),
            None,
        )

    def register_slash_commands(self, tree: app_commands.CommandTree) -> None:
        target_guild = discord.Object(id=self.config.guild_id)
        search_types = ["all", "notes", "links", "todos"]

        @app_commands.command(name="note", description="Save a private note.")
        async def note_command(interaction: discord.Interaction) -> None:
            if not await self.authorize_interaction(interaction):
                return
            await interaction.response.send_modal(NoteModal(self, interaction.channel))

        @app_commands.command(name="notes", description="List recent private notes.")
        async def notes_command(interaction: discord.Interaction) -> None:
            if not await self.authorize_interaction(interaction):
                return
            try:
                notes = await self.notes.list_notes(limit=10)
                await interaction.response.send_message(
                    embed=_notes_embed(notes, "Recent private notes"), ephemeral=True
                )
            except Exception:
                logger.exception("Slash notes listing failed")
                await interaction.response.send_message(
                    "YBOT couldn't load notes. Please try again.", ephemeral=True
                )

        @app_commands.command(name="note-edit", description="Edit a private note.")
        @app_commands.describe(note_id="Select a note to edit")
        async def note_edit_command(
            interaction: discord.Interaction, note_id: int
        ) -> None:
            if not await self.authorize_interaction(interaction):
                return
            try:
                note = await self.notes.get_note(note_id)
            except Exception:
                logger.exception("Could not load note ID %s for slash editing", note_id)
                await interaction.response.send_message(
                    "YBOT couldn't load that note.", ephemeral=True
                )
                return
            if note is None or note["is_archived"]:
                await interaction.response.send_message(
                    "That note was not found or is archived.", ephemeral=True
                )
                return
            await interaction.response.send_modal(
                NoteModal(self, interaction.channel, note)
            )

        @note_edit_command.autocomplete("note_id")
        async def note_edit_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[int]]:
            return await self._note_choices(interaction, current)

        @app_commands.command(name="note-delete", description="Archive a private note.")
        @app_commands.describe(note_id="Select a note to archive")
        async def note_delete_command(
            interaction: discord.Interaction, note_id: int
        ) -> None:
            if not await self.authorize_interaction(interaction):
                return
            try:
                changed = await self.notes.archive_note(note_id)
                response = (
                    f"Note #{note_id} archived."
                    if changed
                    else "That note was not found or is already archived."
                )
                await interaction.response.send_message(response, ephemeral=True)
            except Exception:
                logger.exception("Slash note archive failed for ID %s", note_id)
                await interaction.response.send_message(
                    "YBOT couldn't archive that note.", ephemeral=True
                )

        @note_delete_command.autocomplete("note_id")
        async def note_delete_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[int]]:
            return await self._note_choices(interaction, current)

        @app_commands.command(name="link", description="Save a webpage link.")
        @app_commands.describe(
            url="HTTP or HTTPS webpage URL",
            class_name="Optional class label",
            tags="Optional comma-separated tags",
        )
        @app_commands.rename(class_name="class")
        async def link_command(
            interaction: discord.Interaction,
            url: str,
            class_name: str | None = None,
            tags: str | None = None,
        ) -> None:
            if not await self.authorize_interaction(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                link = await self.links.save_link(
                    url,
                    class_name=class_name,
                    tags=tags,
                    channel=interaction.channel,
                )
                posted = await self.sync_link_feed(link)
                response = f"Saved link #{link['id']}: {discord.utils.escape_mentions(link['title'])}."
                if link["extraction_status"] == "failed":
                    response += " Metadata could not be retrieved; the URL is saved."
                if not posted:
                    response += " The link is saved, but its #links post could not be created."
                await interaction.followup.send(response, ephemeral=True)
            except LinkServiceError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
            except Exception:
                logger.exception("Slash link save failed")
                await interaction.followup.send(
                    "YBOT couldn't save that link. Please try again.", ephemeral=True
                )

        @app_commands.command(name="links", description="List recent saved links.")
        async def links_command(interaction: discord.Interaction) -> None:
            if not await self.authorize_interaction(interaction):
                return
            try:
                links = await self.links.list_links(limit=10)
                await interaction.response.send_message(
                    embed=_links_embed(links, "Recent saved links"), ephemeral=True
                )
            except Exception:
                logger.exception("Slash links listing failed")
                await interaction.response.send_message(
                    "YBOT couldn't load saved links.", ephemeral=True
                )

        @app_commands.command(name="link-delete", description="Archive a saved link.")
        @app_commands.describe(link_id="Select a saved link to archive")
        async def link_delete_command(
            interaction: discord.Interaction, link_id: int
        ) -> None:
            if not await self.authorize_interaction(interaction):
                return
            try:
                changed = await self.links.archive_link(link_id)
                if changed:
                    archived_link = await self.links.get_link(link_id)
                    if archived_link is not None:
                        await self.sync_link_feed(archived_link)
                response = (
                    f"Saved link #{link_id} archived."
                    if changed
                    else "That link was not found or is already archived."
                )
                await interaction.response.send_message(response, ephemeral=True)
            except Exception:
                logger.exception("Slash link archive failed for ID %s", link_id)
                await interaction.response.send_message(
                    "YBOT couldn't archive that link.", ephemeral=True
                )

        @link_delete_command.autocomplete("link_id")
        async def link_delete_autocomplete(
            interaction: discord.Interaction, current: str
        ) -> list[app_commands.Choice[int]]:
            return await self._link_choices(interaction, current)

        @app_commands.command(name="find", description="Search notes, links, and tasks.")
        @app_commands.describe(
            query="Words to search for",
            resource_type="Limit search to notes, links, or tasks",
            class_name="Optional class filter",
        )
        @app_commands.rename(resource_type="type", class_name="class")
        @app_commands.choices(
            resource_type=[
                app_commands.Choice(name=value.title(), value=value)
                for value in search_types
            ]
        )
        async def find_command(
            interaction: discord.Interaction,
            query: str,
            resource_type: str = "all",
            class_name: str | None = None,
        ) -> None:
            if not await self.authorize_interaction(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                page = await self.search.search(
                    query, resource_type=resource_type, class_name=class_name
                )
                await interaction.followup.send(
                    embed=build_search_embed(page, self.config.timezone_name),
                    view=SearchResultsView(self, page),
                    ephemeral=True,
                )
            except SearchValidationError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
            except Exception:
                logger.exception("Slash unified search failed")
                await interaction.followup.send(
                    "YBOT couldn't search saved knowledge. Please try again.",
                    ephemeral=True,
                )

        for command in (
            note_command,
            notes_command,
            note_edit_command,
            note_delete_command,
            link_command,
            links_command,
            link_delete_command,
            find_command,
        ):
            tree.add_command(command, guild=target_guild)

    async def _note_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        if not self._interaction_authorized(interaction):
            return []
        try:
            notes = await self.notes.list_notes(limit=100)
        except Exception:
            logger.exception("Note autocomplete query failed")
            return []
        return [
            app_commands.Choice(
                name=f"#{note['id']} {note['title']}"[:100],
                value=note["id"],
            )
            for note in notes
            if current.casefold() in str(note["id"])
            or current.casefold() in (note.get("title") or "").casefold()
        ][:25]

    async def _link_choices(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        if not self._interaction_authorized(interaction):
            return []
        try:
            links = await self.links.list_links(limit=100)
        except Exception:
            logger.exception("Link autocomplete query failed")
            return []
        return [
            app_commands.Choice(
                name=f"#{link['id']} {link['title']}"[:100],
                value=link["id"],
            )
            for link in links
            if current.casefold() in str(link["id"])
            or current.casefold() in link["title"].casefold()
        ][:25]

    def _interaction_authorized(self, interaction: discord.Interaction) -> bool:
        return (
            interaction.guild_id == self.config.guild_id
            and interaction.user.id == self.config.owner_id
        )


def _parse_note_input(
    raw: str,
) -> tuple[str | None, str, str | None, str | None]:
    text = raw.strip()
    if not text:
        raise NoteValidationError("Provide note content after `!ybot note`.")
    if not text.casefold().startswith("title:"):
        return None, text, None, None
    fields: dict[str, str] = {}
    for part in text.split("|"):
        key, separator, value = part.partition(":")
        normalized_key = key.strip().casefold()
        if not separator or normalized_key not in {"title", "content", "class", "tags"}:
            raise NoteValidationError(
                "Use `title: ... | content: ... | class: ... | tags: ...`."
            )
        fields[normalized_key] = value.strip()
    if "content" not in fields:
        raise NoteValidationError("Explicit note syntax requires a `content:` field.")
    return (
        fields.get("title"),
        fields["content"],
        fields.get("class"),
        fields.get("tags"),
    )


def _parse_link_input(
    raw: str,
) -> tuple[str, dict[str, str], str | None]:
    parts = [part.strip() for part in raw.split("|")]
    url = parts[0] if parts else ""
    if not url:
        return "", {}, "Provide a URL after `!ybot link`."
    fields: dict[str, str] = {}
    for part in parts[1:]:
        key, separator, value = part.partition(":")
        normalized_key = key.strip().casefold()
        if not separator or normalized_key not in {"class", "tags"}:
            return url, fields, "Optional link fields use `| class: ... | tags: ...`."
        fields[normalized_key] = value.strip()
    return url, fields, None


def _parse_search_input(
    raw: str,
) -> tuple[str, str, str | None, str | None]:
    parts = [part.strip() for part in raw.split("|")]
    query = parts[0] if parts else ""
    resource_type = "all"
    class_name: str | None = None
    for part in parts[1:]:
        key, separator, value = part.partition(":")
        normalized_key = key.strip().casefold()
        if not separator or normalized_key not in {"type", "class"}:
            return query, resource_type, class_name, (
                "Optional search filters use `| type: notes | class: COURSE`."
            )
        if normalized_key == "type":
            resource_type = value.strip().casefold()
        else:
            class_name = value.strip() or None
    if resource_type not in SEARCH_TYPES:
        return query, resource_type, class_name, "Search type must be all, notes, links, or todos."
    return query, resource_type, class_name, None


def _parse_id(value: str) -> int | None:
    stripped = value.strip()
    if not _RESOURCE_ID_RE.fullmatch(stripped):
        return None
    return int(stripped)


def _notes_embed(notes: list[dict[str, Any]], title: str) -> discord.Embed:
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    if not notes:
        embed.description = "No saved notes."
        return embed
    for note in notes:
        note_title = discord.utils.escape_mentions(note.get("title") or "Untitled note")
        content = discord.utils.escape_mentions(" ".join(note["content"].split()))
        if len(content) > 350:
            content = content[:347] + "…"
        details = [content]
        if note.get("class_name"):
            details.append(f"Class: {discord.utils.escape_mentions(note['class_name'])}")
        if note.get("tags"):
            details.append(f"Tags: {discord.utils.escape_mentions(note['tags'])}")
        details.append(f"Manage: `!ybot note edit {note['id']}`")
        embed.add_field(
            name=f"#{note['id']} · {note_title}"[:256],
            value="\n".join(details)[:1024],
            inline=False,
        )
    return embed


def _links_embed(links: list[dict[str, Any]], title: str) -> discord.Embed:
    embed = discord.Embed(title=title, color=discord.Color.green())
    if not links:
        embed.description = "No saved links."
        return embed
    for link in links:
        label = discord.utils.escape_markdown(
            discord.utils.escape_mentions(link["title"])
        )
        description = discord.utils.escape_markdown(
            discord.utils.escape_mentions(link.get("description") or link["url"])
        )
        if len(description) > 350:
            description = description[:347] + "…"
        details = [description, link["url"]]
        if link.get("class_name"):
            details.append(f"Class: {discord.utils.escape_mentions(link['class_name'])}")
        if link.get("tags"):
            details.append(f"Tags: {discord.utils.escape_mentions(link['tags'])}")
        details.append(f"Manage: `!ybot link edit {link['id']}`")
        embed.add_field(
            name=f"#{link['id']} · [{label}]({link['url']})"[:256],
            value="\n".join(details)[:1024],
            inline=False,
        )
    return embed


def _link_feed_embed(link: dict[str, Any]) -> discord.Embed:
    title = discord.utils.escape_markdown(
        discord.utils.escape_mentions(link["title"])
    )
    description = discord.utils.escape_markdown(
        discord.utils.escape_mentions(
            link.get("description") or "No page description was available."
        )
    )
    archived = bool(link.get("is_archived"))
    embed = discord.Embed(
        title=title[:256],
        url=link["url"],
        description=description[:2000],
        color=discord.Color.dark_grey() if archived else discord.Color.green(),
        timestamp=parse_utc_timestamp(link["saved_at"]),
    )
    if link.get("class_name"):
        embed.add_field(
            name="Class",
            value=discord.utils.escape_mentions(link["class_name"]),
            inline=True,
        )
    label = "Archived saved link" if archived else "Saved link"
    embed.set_footer(text=f"{label} #{link['id']}")
    return embed
