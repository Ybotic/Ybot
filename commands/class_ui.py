"""Temporary owner-only controls for class overview and recovery actions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from commands.classes import ClassCommands


class ClassOverviewView(discord.ui.View):
    def __init__(
        self,
        manager: ClassCommands,
        class_record: dict[str, Any],
        channel: Any | None,
    ) -> None:
        super().__init__(timeout=300)
        self.manager = manager
        self.class_id = class_record["id"]
        self.action = (
            "restore"
            if class_record["status"] == "archived"
            else "recreate"
            if channel is None
            else None
        )
        if channel is not None:
            self.add_item(
                discord.ui.Button(
                    label="Open class channel",
                    style=discord.ButtonStyle.link,
                    url=channel.jump_url,
                )
            )
        if self.action == "restore":
            self._add_action_button("Restore Class", "restore")
        elif self.action == "recreate":
            self._add_action_button("Recreate Channel", "recreate")

    def _add_action_button(self, label: str, action: str) -> None:
        button = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"ybot:class:{action}:{self.class_id}",
        )
        button.callback = self._action
        self.add_item(button)

    async def _action(self, interaction: discord.Interaction) -> None:
        if not await self.manager.authorize_interaction(interaction):
            return
        try:
            if self.action == "restore":
                response = await self.manager.restore_class_by_id(self.class_id)
            else:
                response = await self.manager.recreate_channel(self.class_id)
        except Exception:
            response = "YBOT couldn't complete that class action. Please try again."
        await interaction.response.send_message(response, ephemeral=True)


class ClassRecreateView(discord.ui.View):
    def __init__(
        self,
        manager: ClassCommands,
        class_id: int,
        *,
        restore: bool = False,
    ) -> None:
        super().__init__(timeout=300)
        self.manager = manager
        self.class_id = class_id
        self.restore = restore
        label = "Restore class and channel" if restore else "Recreate class channel"
        action = "restore" if restore else "recreate"
        button = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.primary,
            custom_id=f"ybot:class:{action}:{class_id}",
        )
        button.callback = self._run
        self.add_item(button)

    async def _run(self, interaction: discord.Interaction) -> None:
        if not await self.manager.authorize_interaction(interaction):
            return
        try:
            if self.restore:
                response = await self.manager.restore_class_by_id(self.class_id)
            else:
                response = await self.manager.recreate_channel(self.class_id)
        except Exception:
            response = "YBOT couldn't complete that class action. Please try again."
        await interaction.response.send_message(response, ephemeral=True)
