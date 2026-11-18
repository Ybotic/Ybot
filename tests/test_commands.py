from types import SimpleNamespace

import discord
import pytest

from commands.basic import COMMAND_PREFIX, route_message
from commands.reminders import register_reminder_slash_commands
from config import Config
from database.db import Database


class FakeChannel:
    def __init__(self) -> None:
        self.id = 8000
        self.sent = []

    async def send(self, content: str) -> None:
        self.sent.append(content)


class FakeDatabase:
    def __init__(self) -> None:
        self.calls = 0

    async def ping(self) -> bool:
        self.calls += 1
        return True

    async def get_server_config(self, guild_id: int):
        self.calls += 1
        return {"setup_completed": 1}


def make_message(content, *, author_id=123, guild_id=456, bot=False):
    return SimpleNamespace(
        content=content,
        author=SimpleNamespace(id=author_id, bot=bot),
        guild=None if guild_id is None else SimpleNamespace(id=guild_id, name="YBOT SERVER"),
        channel=FakeChannel(),
    )


@pytest.mark.asyncio
async def test_command_prefix_filtering_ignores_unrelated_messages() -> None:
    config = Config("unused-in-test", owner_id=123, guild_id=456)
    database = FakeDatabase()
    message = make_message("hello there")

    handled = await route_message(message, config, database)

    assert COMMAND_PREFIX == "!ybot"
    assert not handled
    assert message.channel.sent == []
    assert database.calls == 0


@pytest.mark.asyncio
async def test_only_owner_can_execute_commands() -> None:
    config = Config("unused-in-test", owner_id=123, guild_id=456)
    database = FakeDatabase()
    message = make_message("!ybot ping", author_id=999)

    handled = await route_message(message, config, database)

    assert not handled
    assert message.channel.sent == []
    assert database.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message_kwargs",
    [
        {"guild_id": None},
        {"guild_id": 999},
        {"bot": True},
    ],
)
async def test_dms_other_guilds_and_bot_messages_are_ignored(message_kwargs) -> None:
    config = Config("unused-in-test", owner_id=123, guild_id=456)
    database = FakeDatabase()
    message = make_message("!ybot ping", **message_kwargs)

    handled = await route_message(message, config, database)

    assert not handled
    assert message.channel.sent == []
    assert database.calls == 0


@pytest.mark.asyncio
async def test_owner_command_returns_ping_response() -> None:
    config = Config("unused-in-test", owner_id=123, guild_id=456)
    message = make_message("!ybot ping")

    handled = await route_message(message, config, FakeDatabase())

    assert handled
    assert message.channel.sent == ["Pong! YBOT is online."]


@pytest.mark.asyncio
async def test_reminder_prefix_commands_create_list_cancel_and_delete(tmp_path) -> None:
    config = Config("unused-in-test", owner_id=123, guild_id=456)
    database = Database(tmp_path / "ybot.sqlite3")
    await database.initialize()
    await database.set_setup_completed(456, True)
    await database.upsert_resource(
        resource_id=8000,
        guild_id=456,
        resource_type="text_channel",
        resource_name="ybot",
        parent_category_id=7000,
    )

    create_message = make_message(
        "!ybot remind me in 30 minutes submit lab"
    )
    assert await route_message(create_message, config, database)
    assert "Reminder #1 created" in create_message.channel.sent[0]
    reminders = await database.list_pending_reminders()
    assert reminders[0]["description"] == "Submit lab"

    list_message = make_message("!ybot reminders")
    await route_message(list_message, config, database)
    assert "Submit lab" in list_message.channel.sent[0]

    cancel_message = make_message("!ybot cancel reminder 1")
    await route_message(cancel_message, config, database)
    assert (await database.get_reminder(1))["status"] == "cancelled"

    delete_message = make_message("!ybot delete reminder 1")
    await route_message(delete_message, config, database)
    assert await database.get_reminder(1) is None
    await database.close()


@pytest.mark.asyncio
async def test_reminder_prefix_commands_do_not_expose_data_outside_private_channel(
    tmp_path,
) -> None:
    config = Config("unused-in-test", owner_id=123, guild_id=456)
    database = Database(tmp_path / "ybot.sqlite3")
    await database.initialize()
    await database.set_setup_completed(456, True)
    await database.upsert_resource(
        resource_id=8000,
        guild_id=456,
        resource_type="text_channel",
        resource_name="ybot",
        parent_category_id=7000,
    )
    message = make_message("!ybot reminders")
    message.channel.id = 9000

    await route_message(message, config, database)

    assert message.channel.sent == [
        "For privacy, use reminder commands in the private #ybot channel."
    ]
    assert await database.list_pending_reminders() == []
    await database.close()


@pytest.mark.asyncio
async def test_slash_commands_are_guild_scoped_and_owner_authorized(tmp_path) -> None:
    config = Config("unused-in-test", owner_id=123, guild_id=456)
    database = Database(tmp_path / "ybot.sqlite3")
    await database.initialize()
    client = discord.Client(intents=discord.Intents.none())
    tree = discord.app_commands.CommandTree(client)
    register_reminder_slash_commands(tree, config, database)
    commands = tree.get_commands(guild=discord.Object(id=456))
    assert {command.name for command in commands} == {
        "remind",
        "reminders",
        "reminder-cancel",
    }

    class FakeResponse:
        def __init__(self) -> None:
            self.sent = []

        async def send_message(self, content, *, ephemeral=False):
            self.sent.append((content, ephemeral))

    remind_command = next(command for command in commands if command.name == "remind")
    assert [option["name"] for option in remind_command.to_dict(tree)["options"]] == [
        "time",
        "description",
        "class",
    ]
    unauthorized = SimpleNamespace(
        guild_id=456,
        user=SimpleNamespace(id=999),
        response=FakeResponse(),
    )
    await remind_command.callback(
        unauthorized, "in 30 minutes", "private reminder", None
    )
    assert unauthorized.response.sent[0][1] is True
    assert await database.list_pending_reminders() == []

    wrong_guild = SimpleNamespace(
        guild_id=999,
        user=SimpleNamespace(id=123),
        response=FakeResponse(),
    )
    reminders_command = next(command for command in commands if command.name == "reminders")
    await reminders_command.callback(wrong_guild)
    assert wrong_guild.response.sent[0][1] is True
    assert await database.list_pending_reminders() == []

    owner_interaction = SimpleNamespace(
        guild_id=456,
        user=SimpleNamespace(id=123),
        response=FakeResponse(),
    )
    await remind_command.callback(
        owner_interaction, "in 30 minutes", "submit lab", "CMSC216"
    )
    saved = await database.list_pending_reminders()
    assert saved[0]["description"] == "Submit lab"
    assert saved[0]["class_name"] == "CMSC216"
    assert owner_interaction.response.sent[0][1] is True
    await database.close()
