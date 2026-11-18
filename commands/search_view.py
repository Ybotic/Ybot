"""Private, paginated embeds for unified local search results."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from services.search_service import SearchPage, SearchResult
from services.reminder_format import format_local_due

if TYPE_CHECKING:
    from commands.knowledge import KnowledgeCommands

logger = logging.getLogger(__name__)


class SearchResultsView(discord.ui.View):
    def __init__(self, manager: KnowledgeCommands, page: SearchPage) -> None:
        super().__init__(timeout=300)
        self.manager = manager
        self.current_page = page
        previous = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            custom_id="ybot:find:previous",
            disabled=not page.has_previous,
        )
        next_page = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            custom_id="ybot:find:next",
            disabled=not page.has_next,
        )
        previous.callback = self._previous
        next_page.callback = self._next
        self.add_item(previous)
        self.add_item(next_page)
        link_results = [
            result for result in page.results if result.resource_type == "links"
        ]
        if link_results:
            self.add_item(SearchResultSelect(manager, link_results))

    async def _previous(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, -1)

    async def _next(self, interaction: discord.Interaction) -> None:
        await self._move(interaction, 1)

    async def _move(self, interaction: discord.Interaction, delta: int) -> None:
        if not await self.manager.authorize_interaction(interaction):
            return
        new_page = self.current_page.page + delta
        try:
            page = await self.manager.search.search(
                self.current_page.query,
                resource_type=self.current_page.resource_type,
                class_name=self.current_page.class_name,
                page=new_page,
            )
        except Exception:
            logger.exception("Unified search pagination failed")
            await interaction.response.send_message(
                "YBOT couldn't load that search page. Please try again.",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            embed=build_search_embed(page, self.manager.config.timezone_name),
            view=SearchResultsView(self.manager, page),
        )


def build_search_embed(page: SearchPage, timezone_name: str) -> discord.Embed:
    type_label = page.resource_type.title() if page.resource_type != "all" else "All resources"
    embed = discord.Embed(
        title=f"Search · {type_label}",
        description=(
            f"Query: **{discord.utils.escape_mentions(page.query)}**"
            + (f" · Class: **{discord.utils.escape_mentions(page.class_name)}**" if page.class_name else "")
        ),
        color=discord.Color.blurple(),
    )
    if not page.results:
        embed.add_field(name="No results", value="Try different terms or a different filter.", inline=False)
    for result in page.results:
        embed.add_field(
            name=_field_title(result),
            value=_field_value(result, timezone_name),
            inline=False,
        )
    embed.set_footer(text=f"Page {page.page + 1} · private owner search")
    return embed


def _field_title(result: SearchResult) -> str:
    title = discord.utils.escape_markdown(
        discord.utils.escape_mentions(result.title)
    )
    return f"{result.resource_type.title()} #{result.resource_id} · {title}"[:256]


def _field_value(result: SearchResult, timezone_name: str) -> str:
    excerpt = discord.utils.escape_markdown(
        discord.utils.escape_mentions(result.excerpt)
    )
    lines = [excerpt or "No text excerpt available."]
    if result.class_name:
        lines.append(f"Class: {discord.utils.escape_mentions(result.class_name)}")
    lines.append(f"Date: {format_local_due(result.timestamp, timezone_name)}")
    if result.resource_type == "notes":
        lines.append(f"Manage: `!ybot note edit {result.resource_id}`")
    elif result.resource_type == "links":
        lines.append(f"Open the original with the link selector above.")
        lines.append(f"Manage: `!ybot link edit {result.resource_id}`")
    elif result.resource_type == "todos":
        lines.append(f"Manage: `!ybot todo edit {result.resource_id}`")
    value = "\n".join(lines)
    return value[:1024]


class SearchResultSelect(discord.ui.Select):
    def __init__(self, manager: KnowledgeCommands, links: list[SearchResult]) -> None:
        options = [
            discord.SelectOption(
                label=f"#{link.resource_id} {link.title}"[:100],
                value=str(link.resource_id),
                description=(link.class_name or "Saved webpage")[:100],
            )
            for link in links[:25]
        ]
        super().__init__(
            custom_id="ybot:find:open-link",
            placeholder="Open a saved webpage…",
            min_values=1,
            max_values=1,
            options=options,
            row=1,
        )
        self.manager = manager

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.manager.authorize_interaction(interaction):
            return
        value = self.values[0]
        if not value.isascii() or not value.isdigit() or len(value) > 19 or int(value) <= 0:
            await interaction.response.send_message("Select a valid saved link.", ephemeral=True)
            return
        try:
            link = await self.manager.links.get_link(int(value))
        except Exception:
            logger.exception("Could not load saved link for search result")
            await interaction.response.send_message(
                "YBOT couldn't load that link.", ephemeral=True
            )
            return
        if link is None or link["is_archived"]:
            await interaction.response.send_message(
                "That saved link is no longer available.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title=discord.utils.escape_mentions(link["title"])[:256],
            url=link["url"],
            description=discord.utils.escape_mentions(
                link.get("description") or "Saved webpage"
            )[:2000],
            color=discord.Color.green(),
        )
        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
