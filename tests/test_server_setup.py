from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from config import Config
from database.db import Database
from services.server_setup import CATEGORY_CHANNELS, bootstrap_guild


@dataclass
class FakeOverwrite:
    view_channel: bool | None = None
    send_messages: bool | None = None
    read_message_history: bool | None = None


@dataclass
class FakePermissions:
    manage_channels: bool = True
    manage_roles: bool = True


class FakeTarget:
    def __init__(self, target_id: int, name: str = "") -> None:
        self.id = target_id
        self.name = name


class FakeMember(FakeTarget):
    def __init__(self, target_id: int, name: str, permissions=None) -> None:
        super().__init__(target_id, name)
        self.guild_permissions = permissions or FakePermissions()


class FakeCategory(FakeTarget):
    def __init__(self, target_id: int, name: str, overwrites=None) -> None:
        super().__init__(target_id, name)
        self.overwrites = overwrites or {}


class FakeChannel(FakeTarget):
    def __init__(self, target_id: int, name: str, category, overwrites=None) -> None:
        super().__init__(target_id, name)
        self.category_id = category.id
        self.category = category
        self.overwrites = overwrites or {}

    async def set_permissions(self, target, *, overwrite, reason=None) -> None:
        self.overwrites[target] = overwrite

    def overwrites_for(self, target):
        return self.overwrites.get(target, FakeOverwrite())


class FakeGuild:
    def __init__(self, permissions=None) -> None:
        self.id = 456
        self.owner_id = 123
        self.default_role = FakeTarget(1, "@everyone")
        self.roles = [self.default_role, FakeTarget(2, "members")]
        self.owner = FakeMember(123, "owner")
        self.bot_member = FakeMember(789, "ybot", permissions)
        self.categories = []
        self.text_channels = []
        self._members = {123: self.owner, 789: self.bot_member}
        self._next_id = 1000

    def get_member(self, member_id):
        return self._members.get(member_id)

    async def fetch_member(self, member_id):
        return self._members[member_id]

    async def create_category(self, name, **kwargs):
        category = FakeCategory(self._new_id(), name)
        self.categories.append(category)
        return category

    async def create_text_channel(self, name, *, category, overwrites=None, **kwargs):
        channel = FakeChannel(self._new_id(), name, category, overwrites)
        self.text_channels.append(channel)
        return channel

    def _new_id(self):
        self._next_id += 1
        return self._next_id


def overwrite_factory(**permissions):
    return FakeOverwrite(**permissions)


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent_and_persists_all_ids(tmp_path) -> None:
    database = Database(tmp_path / "ybot.sqlite3")
    await database.initialize()
    guild = FakeGuild()
    client = SimpleNamespace(user=SimpleNamespace(id=789))
    config = Config("unused-in-test", owner_id=123, guild_id=456)

    first = await bootstrap_guild(
        guild, client, config, database, overwrite_factory=overwrite_factory
    )
    initial_counts = (len(guild.categories), len(guild.text_channels))
    second = await bootstrap_guild(
        guild, client, config, database, overwrite_factory=overwrite_factory
    )

    assert first.completed and second.completed
    assert initial_counts == (5, 6)
    assert (len(guild.categories), len(guild.text_channels)) == initial_counts
    assert len(await database.list_resources(guild.id)) == 11
    assert (await database.get_server_config(guild.id))["setup_completed"] == 1
    assert set(category.name for category in guild.categories) == set(CATEGORY_CHANNELS)

    private_channel = next(channel for channel in guild.text_channels if channel.name == "ybot")
    assert private_channel.overwrites[guild.default_role].view_channel is False
    assert private_channel.overwrites[guild.owner].view_channel is True
    assert private_channel.overwrites[guild.bot_member].view_channel is True
    reminder_channel = next(
        channel for channel in guild.text_channels if channel.name == "reminders"
    )
    assert reminder_channel.overwrites[guild.default_role].view_channel is False
    assert reminder_channel.overwrites[guild.owner].view_channel is True
    assert reminder_channel.overwrites[guild.bot_member].view_channel is True
    await database.close()


@pytest.mark.asyncio
async def test_bootstrap_checks_all_permissions_before_creating_resources(tmp_path) -> None:
    database = Database(tmp_path / "ybot.sqlite3")
    await database.initialize()
    guild = FakeGuild(FakePermissions(manage_channels=False, manage_roles=False))
    client = SimpleNamespace(user=SimpleNamespace(id=789))
    config = Config("unused-in-test", owner_id=123, guild_id=456)

    result = await bootstrap_guild(
        guild, client, config, database, overwrite_factory=overwrite_factory
    )

    assert not result.completed
    assert "Manage Channels" in result.detail
    assert "Manage Roles" in result.detail
    assert guild.categories == []
    assert guild.text_channels == []
    assert await database.get_server_config(guild.id) is None
    await database.close()


@pytest.mark.asyncio
async def test_bootstrap_ignores_non_configured_guild(tmp_path) -> None:
    database = Database(tmp_path / "ybot.sqlite3")
    await database.initialize()
    guild = FakeGuild()
    guild.id = 999
    client = SimpleNamespace(user=SimpleNamespace(id=789))
    config = Config("unused-in-test", owner_id=123, guild_id=456)

    result = await bootstrap_guild(
        guild, client, config, database, overwrite_factory=overwrite_factory
    )

    assert not result.completed
    assert guild.categories == []
    await database.close()
