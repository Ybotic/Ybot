"""Idempotent Discord server structure bootstrap."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from config import Config
from database.db import Database

logger = logging.getLogger(__name__)

CATEGORY_CHANNELS: dict[str, tuple[str, ...]] = {
    "INBOX": ("ybot",),
    "PLANNER": ("reminders", "todo", "digest"),
    "CLASSES": (),
    "SAVED": ("links",),
    "SYSTEM": ("bot-log",),
}
# Owner-only reminder and task contents are private channel data.
PRIVATE_CHANNELS = frozenset({"ybot", "bot-log", "reminders", "todo", "digest"})


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    completed: bool
    detail: str


class BootstrapPermissionError(RuntimeError):
    """Raised when the bot cannot safely configure the server structure."""


async def bootstrap_guild(
    guild: Any,
    client: Any,
    config: Config,
    database: Database,
    *,
    overwrite_factory: Callable[..., Any] | None = None,
) -> BootstrapResult:
    """Create or reuse YBOT resources in the configured guild only."""
    if guild.id != config.guild_id:
        logger.info("Skipping bootstrap for non-configured guild ID %s", guild.id)
        return BootstrapResult(False, "Guild is not configured for YBOT")

    try:
        bot_member = await _resolve_bot_member(guild, client)
        owner_member = await _resolve_member(guild, config.owner_id)
        guild_owner = await _resolve_member(guild, guild.owner_id)
        _check_permissions(bot_member)
    except BootstrapPermissionError as exc:
        logger.error("Server bootstrap permission check failed: %s", exc)
        return BootstrapResult(False, str(exc))
    except Exception:
        logger.exception("Unable to inspect guild permissions or members")
        return BootstrapResult(False, "Could not verify bot and owner access")

    make_overwrite = overwrite_factory or _default_overwrite
    logger.info("Starting server bootstrap for guild ID %s", guild.id)
    try:
        await database.set_setup_completed(guild.id, False)
        categories: dict[str, Any] = {}
        for category_name in CATEGORY_CHANNELS:
            category = _find_category(guild, category_name)
            if category is None:
                category = await guild.create_category(
                    category_name, reason="YBOT server structure bootstrap"
                )
                logger.info("Created category %s in guild ID %s", category_name, guild.id)
            else:
                logger.info("Reusing category %s in guild ID %s", category_name, guild.id)
            categories[category_name] = category
            await database.upsert_resource(
                resource_id=category.id,
                guild_id=guild.id,
                resource_type="category",
                resource_name=category_name,
                parent_category_id=None,
            )

        for category_name, channel_names in CATEGORY_CHANNELS.items():
            category = categories[category_name]
            for channel_name in channel_names:
                channel = _find_text_channel(guild, channel_name, category.id)
                if channel is None:
                    overwrites = None
                    if channel_name in PRIVATE_CHANNELS:
                        overwrites = _private_overwrites(
                            guild,
                            category,
                            owner_member,
                            guild_owner,
                            bot_member,
                            make_overwrite,
                        )
                    channel = await guild.create_text_channel(
                        channel_name,
                        category=category,
                        overwrites=overwrites,
                        reason="YBOT server structure bootstrap",
                    )
                    logger.info(
                        "Created channel #%s in category %s (guild ID %s)",
                        channel_name,
                        category_name,
                        guild.id,
                    )
                else:
                    logger.info(
                        "Reusing channel #%s in category %s (guild ID %s)",
                        channel_name,
                        category_name,
                        guild.id,
                    )
                    if channel_name in PRIVATE_CHANNELS:
                        await _secure_existing_channel(
                            channel,
                            category,
                            guild,
                            owner_member,
                            guild_owner,
                            bot_member,
                            make_overwrite,
                        )
                await database.upsert_resource(
                    resource_id=channel.id,
                    guild_id=guild.id,
                    resource_type="text_channel",
                    resource_name=channel_name,
                    parent_category_id=category.id,
                )

        _verify_structure(guild, categories)
        _verify_private_channels(
            guild, categories, owner_member, guild_owner, bot_member
        )
        await database.set_setup_completed(guild.id, True)
        logger.info("Server bootstrap completed for guild ID %s", guild.id)
        return BootstrapResult(True, "Server structure verified")
    except Exception:
        logger.exception("Server bootstrap failed for guild ID %s", guild.id)
        return BootstrapResult(False, "Server bootstrap failed; see bot logs")


def _check_permissions(bot_member: Any) -> None:
    permissions = getattr(bot_member, "guild_permissions", None)
    missing: list[str] = []
    if permissions is None or not getattr(permissions, "manage_channels", False):
        missing.append("Manage Channels")
    if permissions is None or not getattr(permissions, "manage_roles", False):
        missing.append("Manage Roles")
    if missing:
        raise BootstrapPermissionError(
            "Missing bot permission(s): "
            + ", ".join(missing)
            + ". Grant these permissions and restart YBOT. Administrator is not required."
        )


async def _resolve_bot_member(guild: Any, client: Any) -> Any:
    bot_id = getattr(getattr(client, "user", None), "id", None)
    member = guild.get_member(bot_id) if bot_id is not None else None
    if member is None:
        member = getattr(guild, "me", None)
    if member is None:
        raise BootstrapPermissionError("Could not resolve YBOT's guild member permissions")
    return member


async def _resolve_member(guild: Any, member_id: int) -> Any:
    member = guild.get_member(member_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(member_id)
    except Exception as exc:
        raise BootstrapPermissionError(
            "Could not resolve the configured owner or Discord server owner in this guild"
        ) from exc


def _find_category(guild: Any, name: str) -> Any | None:
    matches = [category for category in guild.categories if category.name == name]
    if len(matches) > 1:
        logger.warning(
            "Multiple categories named %s exist in guild ID %s; reusing the first",
            name,
            guild.id,
        )
    return matches[0] if matches else None


def _find_text_channel(guild: Any, name: str, category_id: int) -> Any | None:
    matches = [
        channel
        for channel in guild.text_channels
        if channel.name == name and channel.category_id == category_id
    ]
    if len(matches) > 1:
        logger.warning(
            "Multiple #%s channels exist in category ID %s; reusing the first",
            name,
            category_id,
        )
    return matches[0] if matches else None


def _private_overwrites(
    guild: Any,
    category: Any,
    owner_member: Any,
    guild_owner: Any,
    bot_member: Any,
    make_overwrite: Callable[..., Any],
) -> dict[Any, Any]:
    privileged_ids = {owner_member.id, guild_owner.id, bot_member.id}
    targets: dict[int, Any] = {}
    for target in getattr(guild, "roles", ()):
        if target.id != guild.default_role.id:
            targets[target.id] = target
    for target in list(getattr(category, "overwrites", {})):
        if target.id != guild.default_role.id and target.id not in privileged_ids:
            targets[target.id] = target

    overwrites = {
        guild.default_role: make_overwrite(
            view_channel=False,
            send_messages=False,
            read_message_history=False,
        )
    }
    for target in targets.values():
        overwrites[target] = make_overwrite(
            view_channel=False,
            send_messages=False,
            read_message_history=False,
        )
    allowed = make_overwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
    )
    overwrites[owner_member] = allowed
    overwrites[guild_owner] = allowed
    overwrites[bot_member] = allowed
    return overwrites


async def _secure_existing_channel(
    channel: Any,
    category: Any,
    guild: Any,
    owner_member: Any,
    guild_owner: Any,
    bot_member: Any,
    make_overwrite: Callable[..., Any],
) -> None:
    allowed_ids = {owner_member.id, guild_owner.id, bot_member.id}
    targets: dict[int, Any] = {}
    for target in getattr(guild, "roles", ()):
        if target.id != guild.default_role.id:
            targets[target.id] = target
    for source in (getattr(category, "overwrites", {}), getattr(channel, "overwrites", {})):
        for target in source:
            if target.id != guild.default_role.id and target.id not in allowed_ids:
                targets[target.id] = target

    denied = make_overwrite(
        view_channel=False,
        send_messages=False,
        read_message_history=False,
    )
    allowed = make_overwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
    )
    await channel.set_permissions(guild.default_role, overwrite=denied)
    for target in targets.values():
        await channel.set_permissions(target, overwrite=denied)
    for member in {owner_member.id: owner_member, guild_owner.id: guild_owner, bot_member.id: bot_member}.values():
        await channel.set_permissions(member, overwrite=allowed)


def _verify_structure(guild: Any, categories: dict[str, Any]) -> None:
    for category_name, channel_names in CATEGORY_CHANNELS.items():
        category = categories[category_name]
        if category not in guild.categories:
            raise RuntimeError(f"Category {category_name} is missing after bootstrap")
        for channel_name in channel_names:
            if _find_text_channel(guild, channel_name, category.id) is None:
                raise RuntimeError(
                    f"Channel #{channel_name} is missing from category {category_name}"
                )


def _verify_private_channels(
    guild: Any,
    categories: dict[str, Any],
    owner_member: Any,
    guild_owner: Any,
    bot_member: Any,
) -> None:
    for channel_name in PRIVATE_CHANNELS:
        channel = _find_text_channel(guild, channel_name, _parent_id_for(channel_name, categories))
        if channel is None:
            raise RuntimeError(f"Private channel #{channel_name} is missing")
        for target in [guild.default_role, *getattr(guild, "roles", ())]:
            overwrite = channel.overwrites_for(target)
            if (
                overwrite.view_channel is not False
                or overwrite.send_messages is not False
                or overwrite.read_message_history is not False
            ):
                raise RuntimeError(
                    f"Private channel #{channel_name} has an unsafe role permission overwrite"
                )
        for member in {
            owner_member.id: owner_member,
            guild_owner.id: guild_owner,
            bot_member.id: bot_member,
        }.values():
            overwrite = channel.overwrites_for(member)
            if (
                overwrite.view_channel is not True
                or overwrite.send_messages is not True
                or overwrite.read_message_history is not True
            ):
                raise RuntimeError(
                    f"Private channel #{channel_name} is not accessible to its owner and YBOT"
                )


def _parent_id_for(channel_name: str, categories: dict[str, Any]) -> int:
    for category_name, channel_names in CATEGORY_CHANNELS.items():
        if channel_name in channel_names:
            return categories[category_name].id
    raise RuntimeError(f"No parent category configured for #{channel_name}")


def _default_overwrite(**permissions: bool) -> Any:
    import discord

    return discord.PermissionOverwrite(**permissions)
