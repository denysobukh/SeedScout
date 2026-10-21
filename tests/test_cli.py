import os
import subprocess
import sys


def run_cli(tmp_path, *args, configured=False, overrides=None, mock_polling=False):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("TELEGRAM_", "RUTRACKER_", "TRANSMISSION_", "DATABASE_", "LOG_"))
    }
    if configured:
        env.update(
            {
                "TELEGRAM_BOT_TOKEN": "123456789:abcdefghijklmnopqrstuvwxyz_123456789",
                "TELEGRAM_ALLOWED_USER_IDS": "123",
                "TRANSMISSION_RPC_URL": "http://127.0.0.1:1/transmission/rpc",
            }
        )
    env.update(overrides or {})
    invocation = [sys.executable, "-m", "tg_torrent_downloader", *args]
    if mock_polling:
        invocation = [
            sys.executable,
            "-c",
            (
                "import importlib, logging\n"
                "module = importlib.import_module('tg_torrent_downloader.__main__')\n"
                "async def fake_polling(settings):\n"
                "    logging.getLogger('test').info('Polling simulated')\n"
                "module.run_bot = fake_polling\n"
                "raise SystemExit(module.main())\n"
            ),
        ]
    return subprocess.run(
        invocation,
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


def test_default_command_runs_polling(tmp_path):
    result = run_cli(tmp_path, configured=True, mock_polling=True)
    assert result.returncode == 0
    assert "Polling simulated" in result.stderr


def test_startup_creates_diagnostic_file(tmp_path):
    result = run_cli(tmp_path, configured=True, mock_polling=True)
    path = tmp_path / ".runtime" / "bot.log"
    assert result.returncode == 0
    assert "Polling simulated" in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600


def test_logging_setup_failure_is_safe_and_actionable(tmp_path):
    blocker = tmp_path / "file-instead-of-directory"
    blocker.write_text("existing data")
    result = run_cli(tmp_path, configured=True, overrides={"LOG_FILE": str(blocker / "bot.log")})
    assert result.returncode == 2
    assert "LOG_FILE could not be opened" in result.stderr
    assert "Traceback" not in result.stderr
    assert blocker.read_text() == "existing data"
