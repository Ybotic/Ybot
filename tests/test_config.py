import pytest

from config import Config, ConfigurationError


def test_config_loads_required_values() -> None:
    config = Config.from_env(
        {
            "DISCORD_TOKEN": "secret-token",
            "OWNER_ID": "123",
            "GUILD_ID": "456",
        }
    )

    assert config.discord_token == "secret-token"
    assert config.owner_id == 123
    assert config.guild_id == 456


def test_config_reports_missing_and_invalid_values_without_echoing_token() -> None:
    with pytest.raises(ConfigurationError) as error:
        Config.from_env(
            {
                "DISCORD_TOKEN": "secret-token",
                "OWNER_ID": "not-an-id",
                "GUILD_ID": "0",
            }
        )

    assert "OWNER_ID" in str(error.value)
    assert "GUILD_ID" in str(error.value)
    assert "secret-token" not in str(error.value)


def test_config_requires_token_and_ids() -> None:
    with pytest.raises(ConfigurationError) as error:
        Config.from_env({})

    assert "DISCORD_TOKEN" in str(error.value)
    assert "OWNER_ID" in str(error.value)
    assert "GUILD_ID" in str(error.value)


def test_config_validates_optional_timezone() -> None:
    with pytest.raises(ConfigurationError, match="TIMEZONE"):
        Config.from_env(
            {
                "DISCORD_TOKEN": "secret-token",
                "OWNER_ID": "123",
                "GUILD_ID": "456",
                "TIMEZONE": "Not/A_Real_Zone",
            }
        )
