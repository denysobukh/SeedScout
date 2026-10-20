import os
import subprocess
import sys


def run_cli(tmp_path, *args, configured=False):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("TELEGRAM_", "RUTRACKER_", "TRANSMISSION_", "DATABASE_", "LOG_LEVEL")
        )
    }
    if configured:
        env.update(
            {
                "TELEGRAM_BOT_TOKEN": "123456789:abcdefghijklmnopqrstuvwxyz_123456789",
                "TELEGRAM_ALLOWED_USER_IDS": "123",
                "RUTRACKER_USERNAME": "test-user",
                "RUTRACKER_PASSWORD": "test-password",
                "TRANSMISSION_RPC_URL": "http://127.0.0.1:1/transmission/rpc",
            }
        )
    return subprocess.run(
        [sys.executable, "-m", "tg_torrent_downloader", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_help_requires_no_configuration(tmp_path):
    result = run_cli(tmp_path, "--help")
    assert result.returncode == 0
    assert "--check-config" in result.stdout


def test_configuration_error_is_actionable(tmp_path):
    result = run_cli(tmp_path, "--check-config")
    assert result.returncode == 2
    assert "TELEGRAM_BOT_TOKEN is required" in result.stderr
    assert "Traceback" not in result.stderr


def test_check_configuration_does_not_contact_services_or_create_state(tmp_path):
    result = run_cli(tmp_path, "--check-config", configured=True)
    assert result.returncode == 0
    assert "Configuration is valid" in result.stderr
    assert list(tmp_path.iterdir()) == []
    assert "test-password" not in result.stderr


def test_default_command_is_honest_about_unimplemented_polling(tmp_path):
    result = run_cli(tmp_path, configured=True)
    assert result.returncode == 1
    assert "not implemented yet" in result.stderr
