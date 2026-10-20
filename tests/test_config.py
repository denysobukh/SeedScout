from dataclasses import FrozenInstanceError

import pytest

from tg_torrent_downloader.config import ConfigurationError, Settings


@pytest.fixture
def env():
    return {
        "TELEGRAM_BOT_TOKEN": "123456789:abcdefghijklmnopqrstuvwxyz_123456789",
        "TELEGRAM_ALLOWED_USER_IDS": "123, 456,123",
        "RUTRACKER_USERNAME": "tracker-user",
        "RUTRACKER_PASSWORD": "secret-password",
        "TRANSMISSION_RPC_URL": "http://127.0.0.1:9091/transmission/rpc",
    }


def test_valid_settings_are_private_and_immutable(env):
    settings = Settings.load(env, env_file=None)
    assert settings.telegram_allowed_user_ids == frozenset({123, 456})
    assert settings.database_path.as_posix() == ".runtime/bot.db"
    assert settings.transmission_download_dir is None
    assert settings.transmission_username is None
    assert "secret-password" not in repr(settings)
    with pytest.raises(FrozenInstanceError):
        settings.log_level = "DEBUG"


@pytest.mark.parametrize(
    "key",
    [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USER_IDS",
        "RUTRACKER_USERNAME",
        "RUTRACKER_PASSWORD",
        "TRANSMISSION_RPC_URL",
    ],
)
@pytest.mark.parametrize("value", [None, "", "   "])
def test_required_settings(env, key, value):
    if value is None:
        del env[key]
    else:
        env[key] = value
    with pytest.raises(ConfigurationError, match=key):
        Settings.load(env, env_file=None)


@pytest.mark.parametrize("value", ["0", "-1", "abc", "123,", "1.5", str(2**52), "9" * 5000])
def test_bad_allowlists(env, value):
    env["TELEGRAM_ALLOWED_USER_IDS"] = value
    with pytest.raises(ConfigurationError, match="TELEGRAM_ALLOWED_USER_IDS"):
        Settings.load(env, env_file=None)


@pytest.mark.parametrize(
    "value",
    [
        "ftp://localhost/rpc",
        "http:///rpc",
        "http://user:password@host/rpc",
        "http://host:bad/rpc",
        "http://host:65536/rpc",
        "http://host:0/rpc",
        "http://host/rpc?token=secret",
        "http://host/rpc#fragment",
        "http://host/\nrpc",
        "http://host",
        "http://[invalid/rpc",
    ],
)
def test_bad_urls(env, value):
    env["TRANSMISSION_RPC_URL"] = value
    with pytest.raises(ConfigurationError, match="TRANSMISSION_RPC_URL") as exc:
        Settings.load(env, env_file=None)
    assert value not in str(exc.value)


@pytest.mark.parametrize("value", ["https://host/transmission/rpc", "http://[::1]:9091/rpc"])
def test_valid_urls(env, value):
    env["TRANSMISSION_RPC_URL"] = value
    assert Settings.load(env, env_file=None).transmission_rpc_url == value


@pytest.mark.parametrize("key", ["TRANSMISSION_USERNAME", "TRANSMISSION_PASSWORD"])
def test_rpc_credentials_must_be_paired(env, key):
    env[key] = "private-value"
    with pytest.raises(ConfigurationError, match="must be paired"):
        Settings.load(env, env_file=None)


def test_env_overrides_dotenv_without_interpolation_or_process_mutation(env, tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("RUTRACKER_PASSWORD=${SECRET}\nLOG_LEVEL=DEBUG\n")
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    values = env.copy()
    del values["RUTRACKER_PASSWORD"]
    settings = Settings.load(values, env_file=path)
    assert settings.rutracker_password == "${SECRET}"
    assert settings.log_level == "DEBUG"
    import os

    assert "LOG_LEVEL" not in os.environ
    values["RUTRACKER_PASSWORD"] = ""
    with pytest.raises(ConfigurationError, match="RUTRACKER_PASSWORD"):
        Settings.load(values, env_file=path)


@pytest.mark.parametrize(
    "key,value",
    [
        ("LOG_LEVEL", "verbose"),
        ("DATABASE_PATH", ""),
        ("DATABASE_PATH", ":memory:"),
        ("DATABASE_PATH", "\x00"),
        ("TELEGRAM_BOT_TOKEN", "invalid-private-token"),
    ],
)
def test_invalid_options_do_not_echo_values(env, key, value):
    env[key] = value
    with pytest.raises(ConfigurationError, match=key) as exc:
        Settings.load(env, env_file=None)
    if value:
        assert value not in str(exc.value)


def test_database_directory_rejected(env, tmp_path):
    env["DATABASE_PATH"] = str(tmp_path)
    with pytest.raises(ConfigurationError, match="DATABASE_PATH"):
        Settings.load(env, env_file=None)


def test_invalid_environment_file_is_reported_without_content(env, tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"RUTRACKER_PASSWORD=private\xff")
    with pytest.raises(ConfigurationError, match="Environment file") as exc:
        Settings.load(env, env_file=path)
    assert "private" not in str(exc.value)
