import pytest

from database.db import Database


@pytest.mark.asyncio
async def test_database_initializes_schema_and_persists_server_resources(tmp_path) -> None:
    database = Database(tmp_path / "ybot.sqlite3")
    await database.initialize()

    assert await database.ping()
    await database.set_setup_completed(456, True)
    await database.upsert_resource(
        resource_id=1001,
        guild_id=456,
        resource_type="category",
        resource_name="INBOX",
        parent_category_id=None,
    )

    config = await database.get_server_config(456)
    resources = await database.list_resources(456)
    assert config is not None
    assert config["setup_completed"] == 1
    assert len(resources) == 1
    assert resources[0]["resource_id"] == 1001
    assert resources[0]["parent_category_id"] is None

    await database.close()
    assert not database.is_connected
