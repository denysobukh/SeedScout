"""Installer tests use temporary homes/checkouts and fake launchctl; no real service changes."""

import importlib.util
import plistlib
from pathlib import Path
from unittest.mock import Mock

import pytest
from dotenv import dotenv_values

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install.py"
spec = importlib.util.spec_from_file_location("seedscout_install", SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
TOKEN = "123456789:abcdefghijklmnopqrstuvwxyz_123456789"
OTHER_TOKEN = "987654321:abcdefghijklmnopqrstuvwxyz_987654321"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    project = tmp_path / "My Bot & Worker"
    home = tmp_path / "User Home"
    project.mkdir()
    home.mkdir()
    monkeypatch.setattr(installer, "PROJECT", project)
    monkeypatch.chdir(project)
    monkeypatch.setattr(
        installer, "transmission_path", lambda values, home: Path("/Applications/Transmission.app")
    )
    monkeypatch.setattr(
        installer,
        "collect",
        lambda values, mode: (
            mode,
            {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ALLOWED_USER_IDS": "1,2"},
        ),
    )
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USER_IDS", "TRANSMISSION_APP_PATH"):
        monkeypatch.delenv(key, raising=False)
    commands = []

    def run(command, *, check=True):
        commands.append(command)
        return "print" not in command

    monkeypatch.setattr(installer, "run", run)
    return project, home, commands


@pytest.mark.parametrize("mode", ["agent", "daemon"])
def test_plist_uses_absolute_venv_arguments_and_working_directory(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(installer.pwd, "getpwuid", lambda uid: Mock(pw_name="installer-user"))
    project = tmp_path / "My Bot & Worker"
    value = plistlib.loads(installer.launchd_plist(project, mode, tmp_path))
    assert value["ProgramArguments"] == [
        str(project / ".venv/bin/python"),
        "-u",
        "-m",
        "tg_torrent_downloader",
    ]
    assert value["WorkingDirectory"] == str(project)
    assert value["RunAtLoad"] and value["ThrottleInterval"] == 30
    assert "TELEGRAM_BOT_TOKEN" not in value.get("EnvironmentVariables", {})
    if mode == "agent":
        assert value["LimitLoadToSessionType"] == "Aqua" and "UserName" not in value
    else:
        assert value["UserName"] != "root"


@pytest.mark.parametrize("restart_at", range(4))
def test_restart_at_every_prompt_reuses_original_defaults(monkeypatch, restart_at):
    answers = ["agent", TOKEN, "1,2", "yes"]
    supplied = iter(answers[:restart_at] + [":restart"] + ["", "", "", "yes"])
    prompts = []

    def reader(label, default="", *, secret=False):
        prompts.append((label, default))
        value = next(supplied)
        if value == ":restart":
            raise installer.RestartInput
        return default if not value else value

    monkeypatch.setattr(installer, "prompt", reader)
    mode, values = installer.collect(
        {"TELEGRAM_BOT_TOKEN": OTHER_TOKEN, "TELEGRAM_ALLOWED_USER_IDS": "99"}, "agent"
    )
    assert mode == "agent"
    assert values == {"TELEGRAM_BOT_TOKEN": OTHER_TOKEN, "TELEGRAM_ALLOWED_USER_IDS": "99"}
    assert len(prompts) == restart_at + 5


def test_secret_default_is_masked_and_enter_preserves_it(monkeypatch):
    seen = []
    monkeypatch.setattr(installer.getpass, "getpass", lambda label: seen.append(label) or "")
    assert installer.prompt("Bot token", TOKEN, secret=True) == TOKEN
    assert TOKEN not in seen[0] and "Enter keeps it" in seen[0]


def test_install_preserves_other_settings_comments_and_quoted_values(setup):
    project, home, commands = setup
    env = project / ".env"
    env.write_text(
        f"# keep this comment\nTELEGRAM_BOT_TOKEN='{OTHER_TOKEN}'\n"
        "TELEGRAM_ALLOWED_USER_IDS=99\nTRANSMISSION_APP_PATH=\n"
        "TRANSMISSION_USERNAME='user #name'\nTRANSMISSION_PASSWORD='literal$pass'\n"
        "TRANSMISSION_DOWNLOAD_DIR='/media/Movie & TV'\n"
    )
    installer.install(project, home, "agent")
    values = dotenv_values(env, interpolate=False)
    assert values["TELEGRAM_BOT_TOKEN"] == TOKEN
    assert values["TRANSMISSION_APP_PATH"] == ""
    assert values["TRANSMISSION_PASSWORD"] == "literal$pass"
    assert values["TRANSMISSION_DOWNLOAD_DIR"] == "/media/Movie & TV"
    assert "# keep this comment" in env.read_text()
    assert env.stat().st_mode & 0o777 == 0o600
    plist = installer.service_path("agent", home)
    assert plist.exists() and commands[-1] == [
        "/bin/launchctl",
        "bootstrap",
        installer.domain("agent"),
        str(plist),
    ]
    assert all(TOKEN not in str(command) for command in commands)


def test_new_install_and_uninstall_keeps_all_application_data(setup):
    project, home, commands = setup
    installer.install(project, home, "agent")
    env = (project / ".env").read_bytes()
    database = project / ".runtime/bot.db"
    database.write_bytes(b"existing database")
    installer.uninstall(project, home, None)
    assert not installer.service_path("agent", home).exists()
    assert (project / ".env").read_bytes() == env
    assert database.read_bytes() == b"existing database"
    assert (project / ".runtime/launchd.stderr.log").exists()
    assert not any("bootstrap" in command for command in commands[2:])


def test_bootstrap_failure_restores_existing_config_and_plist(setup, monkeypatch):
    project, home, commands = setup
    original = (
        f"# original\nTELEGRAM_BOT_TOKEN={OTHER_TOKEN}\nTELEGRAM_ALLOWED_USER_IDS=99\n".encode()
    )
    (project / ".env").write_bytes(original)
    path = installer.service_path("agent", home)
    path.parent.mkdir(parents=True)
    old_plist = installer.launchd_plist(project, "agent", home)
    path.write_bytes(old_plist)

    def fail(command, *, check=True):
        commands.append(command)
        if "bootstrap" in command:
            raise installer.InstallError("fake failure")
        return "print" not in command

    monkeypatch.setattr(installer, "run", fail)
    with pytest.raises(installer.InstallError, match="fake failure"):
        installer.install(project, home, "agent")
    assert (project / ".env").read_bytes() == original
    assert path.read_bytes() == old_plist


def test_external_env_edit_during_prompts_is_not_overwritten(setup, monkeypatch):
    project, home, _ = setup
    path = project / ".env"
    path.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_ALLOWED_USER_IDS=1\n")

    def collect(values, mode):
        path.write_text("# changed by someone else\n")
        return mode, {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_ALLOWED_USER_IDS": "1"}

    monkeypatch.setattr(installer, "collect", collect)
    with pytest.raises(installer.InstallError, match="changed while input"):
        installer.install(project, home, "agent")
    assert path.read_text() == "# changed by someone else\n"
    assert not installer.service_path("agent", home).exists()


def test_live_manual_worker_prevents_config_or_service_changes(setup):
    project, home, _ = setup
    runtime = project / ".runtime"
    runtime.mkdir()
    lock = runtime / "bot.db.runtime.lock"
    import fcntl

    with lock.open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(installer.InstallError, match="already running"):
            installer.install(project, home, "agent")
    assert not (project / ".env").exists()
    assert not installer.service_path("agent", home).exists()


def test_uninstall_rejects_another_checkouts_service(setup):
    project, home, commands = setup
    path = installer.service_path("agent", home)
    path.parent.mkdir(parents=True)
    original = installer.launchd_plist(project.parent / "Other checkout", "agent", home)
    path.write_bytes(original)
    with pytest.raises(installer.InstallError, match="another checkout"):
        installer.uninstall(project, home, "agent")
    assert path.read_bytes() == original and not commands


def test_uninstall_does_not_check_transmission_or_read_env(setup, monkeypatch):
    project, home, _ = setup
    path = installer.service_path("agent", home)
    path.parent.mkdir(parents=True)
    path.write_bytes(installer.launchd_plist(project, "agent", home))
    (project / ".env").write_bytes(b"invalid environment data")
    dependency = Mock(side_effect=AssertionError("must not inspect dependencies"))
    monkeypatch.setattr(installer, "transmission_path", dependency)
    installer.uninstall(project, home, "agent")
    dependency.assert_not_called()
    assert (project / ".env").read_bytes() == b"invalid environment data"


def test_transmission_detection_requires_a_real_executable_bundle(tmp_path):
    home = tmp_path / "home"
    app = home / "Applications/Transmission.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "org.m0k.transmission"})
    )
    executable = app / "Contents/MacOS/Transmission"
    executable.touch(mode=0o700)
    assert installer.transmission_path({"TRANSMISSION_APP_PATH": str(app)}, home) == app


def test_help_runs_without_bootstrapping_dependencies():
    import subprocess

    result = subprocess.run(
        ["/bin/bash", str(SCRIPT.parents[1] / "install.sh"), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--uninstall" in result.stdout and "--daemon" in result.stdout
    assert "Resolved" not in result.stderr


def test_loaded_service_reinstall_stops_then_reloads_without_losing_data(setup, monkeypatch):
    project, home, commands = setup
    installer.install(project, home, "agent")
    original = (project / ".env").read_bytes()
    commands.clear()

    def run(command, *, check=True):
        commands.append(command)
        return True

    monkeypatch.setattr(installer, "run", run)
    installer.install(project, home, "agent")
    assert [command[1] for command in commands] == ["print", "bootout", "bootstrap"]
    assert (project / ".env").read_bytes() == original
    commands.clear()
    installer.uninstall(project, home, "agent")
    assert [command[1] for command in commands] == ["print", "bootout"]
    assert (project / ".env").read_bytes() == original


def test_cancelled_input_leaves_existing_environment_and_service_unchanged(setup, monkeypatch):
    project, home, commands = setup
    original = b"# retain this file\n"
    (project / ".env").write_bytes(original)
    monkeypatch.setattr(installer, "collect", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        installer.install(project, home, "agent")
    assert (project / ".env").read_bytes() == original
    assert not installer.service_path("agent", home).exists() and not commands


def test_agent_failure_on_first_install_rolls_back_new_env_and_plist(setup, monkeypatch):
    project, home, commands = setup

    def fail(command, *, check=True):
        commands.append(command)
        if "bootstrap" in command:
            raise installer.InstallError("fake failure")
        return False

    monkeypatch.setattr(installer, "run", fail)
    with pytest.raises(installer.InstallError, match="fake failure"):
        installer.install(project, home, "agent")
    assert not (project / ".env").exists()
    assert not installer.service_path("agent", home).exists()
    assert (project / ".runtime").exists()


def test_invalid_configured_transmission_path_is_not_silently_replaced(tmp_path):
    configured = tmp_path / "Missing Transmission.app"
    with pytest.raises(installer.InstallError, match="Correct that setting") as caught:
        installer.transmission_path({"TRANSMISSION_APP_PATH": str(configured)}, tmp_path)
    assert str(configured) not in str(caught.value)
