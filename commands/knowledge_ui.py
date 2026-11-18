"""Discord modals and temporary launcher views for notes and links."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

import discord

from services.link_service import LinkServiceError
from services.note_service import NoteValidationError

if TYPE_CHECKING:
    from commands.knowledge import KnowledgeCommands

logger = logging.getLogger(__name__)


class NoteModal(discord.ui.Modal):
    def __init__(
        self,
        manager: KnowledgeCommands,
        channel: Any | None,
        note: dict[str, Any] | None = None,
    ) -> None:
        title = f"Edit note #{note['id']}" if note else "Save a note"
        custom_id = f"ybot:note:edit-modal:{note['id']}" if note else "ybot:note:add-modal"
        super().__init__(title=title, custom_id=custom_id)
        self.manager = manager
        self.channel = channel
        self.note_id = note["id"] if note else None
        self.title_field = discord.ui.TextInput(
            label="Title (optional)",
            max_length=200,
            required=False,
            default=(note.get("title") or "") if note else "",
        )
        self.content_field = discord.ui.TextInput(
            label="Note content",
            max_length=4000,
            required=True,
            style=discord.TextStyle.paragraph,
            default=note["content"] if note else "",
        )
        self.class_field = discord.ui.TextInput(
            label=(
                "Class (enter clear to remove)"
                if note is not None
                else "Class (optional)"
            ),
            max_length=100,
            required=False,
            default=(note.get("class_name") or "") if note else "",
        )
        self.tags_field = discord.ui.TextInput(
            label="Tags, comma-separated (optional)",
            max_length=500,
            required=False,
            default=(note.get("tags") or "") if note else "",
        )
        for field in (
            self.title_field,
            self.content_field,
            self.class_field,
            self.tags_field,
        ):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.manager.authorize_interaction(interaction):
            return
        try:
            if self.note_id is None:
                note_id = await self.manager.notes.create_note(
                    self.content_field.value,
                    title=self.title_field.value,
                    class_name=self.class_field.value,
                    tags=self.tags_field.value,
                    channel=self.channel or interaction.channel,
                )
                response = f"Private note #{note_id} saved."
            else:
                clear_class = self.class_field.value.casefold() == "clear"
                changed = await self.manager.notes.edit_note(
                    self.note_id,
                    title=self.title_field.value,
                    content=self.content_field.value,
                    class_name=None if clear_class else self.class_field.value,
                    tags=self.tags_field.value,
                    channel=self.channel or interaction.channel,
                    clear_class=clear_class,
                )
                if not changed:
                    await interaction.response.send_message(
                        "That note is no longer available for editing.", ephemeral=True
                    )
                    return
                response = f"Private note #{self.note_id} updated."
        except NoteValidationError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except Exception:
            logger.exception("Could not save note through modal")
            await interaction.response.send_message(
                "YBOT couldn't save that note. Please try again.", ephemeral=True
            )
            return
        await interaction.response.send_message(response, ephemeral=True)


class NoteEditLauncherView(discord.ui.View):
    def __init__(self, manager: KnowledgeCommands, note_id: int) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        self.note_id = note_id
        button = discord.ui.Button(
            label="Open note editor",
            style=discord.ButtonStyle.primary,
            custom_id=f"ybot:note:edit-launch:{note_id}",
        )
        button.callback = self._open_editor
        self.add_item(button)

    async def _open_editor(self, interaction: discord.Interaction) -> None:
        if not await self.manager.authorize_interaction(interaction):
            return
        try:
            note = await self.manager.notes.get_note(self.note_id)
        except Exception:
            logger.exception("Could not load note ID %s", self.note_id)
            await interaction.response.send_message(
                "YBOT couldn't load that note. Please try again later.", ephemeral=True
            )
            return
        if note is None or note["is_archived"]:
            await interaction.response.send_message(
                "That note was not found or has been archived.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            NoteModal(self.manager, interaction.channel, note)
        )


class LinkEditLauncherView(discord.ui.View):
    def __init__(self, manager: KnowledgeCommands, link_id: int) -> None:
        super().__init__(timeout=180)
        self.manager = manager
        self.link_id = link_id
        button = discord.ui.Button(
            label="Open link editor",
            style=discord.ButtonStyle.primary,
            custom_id=f"ybot:link:edit-launch:{link_id}",
        )
        button.callback = self._open_editor
        self.add_item(button)

    async def _open_editor(self, interaction: discord.Interaction) -> None:
        if not await self.manager.authorize_interaction(interaction):
            return
        try:
            link = await self.manager.links.get_link(self.link_id)
        except Exception:
            logger.exception("Could not load saved link ID %s", self.link_id)
            await interaction.response.send_message(
                "YBOT couldn't load that link. Please try again later.", ephemeral=True
            )
            return
        if link is None or link["is_archived"]:
            await interaction.response.send_message(
                "That link was not found or has been archived.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            LinkEditModal(self.manager, interaction.channel, link)
        )


class LinkEditModal(discord.ui.Modal):
    def __init__(
        self,
        manager: KnowledgeCommands,
        channel: Any | None,
        link: dict[str, Any],
    ) -> None:
        super().__init__(
            title=f"Edit link #{link['id']}",
            custom_id=f"ybot:link:edit-modal:{link['id']}",
        )
        self.manager = manager
        self.channel = channel
        self.link_id = link["id"]
        self.url_field = discord.ui.TextInput(
            label="URL",
            max_length=2048,
            required=True,
            default=link["url"],
        )
        self.class_field = discord.ui.TextInput(
            label="Class (optional; enter clear to remove)",
            max_length=100,
            required=False,
            default=link.get("class_name") or "",
        )
        self.tags_field = discord.ui.TextInput(
            label="Tags, comma-separated (optional)",
            max_length=500,
            required=False,
            default=link.get("tags") or "",
        )
        self.add_item(self.url_field)
        self.add_item(self.class_field)
        self.add_item(self.tags_field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.manager.authorize_interaction(interaction):
            return
        try:
            link = await self.manager.links.edit_link(
                self.link_id,
                url=self.url_field.value,
                class_name=self.class_field.value,
                tags=self.tags_field.value,
                channel=self.channel or interaction.channel,
            )
        except LinkServiceError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        except Exception:
            logger.exception("Could not edit saved link ID %s", self.link_id)
            await interaction.response.send_message(
                "YBOT couldn't update that link. Please try again.", ephemeral=True
            )
            return
        posted = await self.manager.sync_link_feed(link)
        response = f"Saved link #{self.link_id} updated."
        if not posted:
            response += " The link is saved; its #links post could not be synchronized."
        await interaction.response.send_message(response, ephemeral=True)
