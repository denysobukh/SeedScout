import asyncio
import plistlib
from unittest.mock import AsyncMock, Mock

import pytest

from tg_torrent_downloader import transmission_app as module
from tg_torrent_downloader.transmission_app import (
    TransmissionLaunchError,
    ensure_transmission_running,
)


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "darwin")
    path = tmp_path / "Transmission Test.app"
    (path / "Contents" / "MacOS").mkdir(parents=True)
    (path / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "org.m0k.transmission",
                "CFBundleExecutable": "Transmission",
            }
        )
    )
    binary = path / "Contents" / "MacOS" / "Transmission"
    binary.touch(mode=0o700)
    return path


def test_existing_process_is_not_launched_again(bundle, monkeypatch):
    command = AsyncMock(return_value=0)
    monkeypatch.setattr(module, "_command", command)
    assert asyncio.run(ensure_transmission_running(bundle)) is False
    assert command.await_count == 1
    assert command.await_args.args[0] == "/usr/bin/pgrep"


def test_missing_process_launches_exact_bundle_without_shell_and_waits(bundle, monkeypatch):
    command = AsyncMock(side_effect=[1, 0, 1, 0])
    monkeypatch.setattr(module, "_command", command)
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    assert asyncio.run(ensure_transmission_running(bundle)) is True
    assert command.await_args_list[1].args == ("/usr/bin/open", "-g", "-a", str(bundle))
    assert command.await_count == 4


@pytest.mark.parametrize("codes", [[2], [1, 1]])
def test_probe_and_launch_errors_are_reportable(bundle, monkeypatch, codes):
    monkeypatch.setattr(module, "_command", AsyncMock(side_effect=codes))
    with pytest.raises(TransmissionLaunchError):
        asyncio.run(ensure_transmission_running(bundle))


def test_blank_setting_disables_launch_on_any_platform(monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "linux")
    command = AsyncMock()
    monkeypatch.setattr(module, "_command", command)
    assert asyncio.run(ensure_transmission_running(None)) is False
    command.assert_not_awaited()


def test_configured_launch_requires_macos(bundle, monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "linux")
    with pytest.raises(TransmissionLaunchError, match="macOS"):
        asyncio.run(ensure_transmission_running(bundle))


@pytest.mark.parametrize(
    "case", ["missing", "different-app", "no-executable", "malformed", "malformed-xml"]
)
def test_invalid_bundle_does_not_launch_anything(bundle, monkeypatch, case):
    path = bundle
    if case == "missing":
        path = bundle.parent / "Missing.app"
    elif case == "different-app":
        (path / "Contents" / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": "other"})
        )
    elif case == "no-executable":
        (path / "Contents" / "MacOS" / "Transmission").chmod(0o600)
    elif case == "malformed-xml":
        (path / "Contents" / "Info.plist").write_bytes(b'<?xml version="1.0"?><plist><')
    else:
        (path / "Contents" / "Info.plist").write_bytes(b"not a plist")
    command = AsyncMock()
    monkeypatch.setattr(module, "_command", command)
    with pytest.raises(TransmissionLaunchError) as caught:
        asyncio.run(ensure_transmission_running(path))
    assert str(path) not in str(caught.value)
    command.assert_not_awaited()


def test_launch_wait_has_a_deadline(bundle, monkeypatch):
    monkeypatch.setattr(module, "_command", AsyncMock(return_value=0))
    monkeypatch.setattr(module, "_running", AsyncMock(return_value=False))

    async def timeout(action, *, timeout):
        action.close()
        assert timeout == 10
        raise TimeoutError

    monkeypatch.setattr(module.asyncio, "wait_for", timeout)
    with pytest.raises(TransmissionLaunchError, match="did not start in time"):
        asyncio.run(ensure_transmission_running(bundle))


@pytest.mark.parametrize("error", [TimeoutError, asyncio.CancelledError])
def test_subprocess_timeout_or_cancellation_reaps_only_its_own_command(monkeypatch, error):
    process = Mock(returncode=None)
    process.wait = AsyncMock(side_effect=[error(), 0])
    factory = AsyncMock(return_value=process)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", factory)
    expected = TransmissionLaunchError if error is TimeoutError else asyncio.CancelledError
    with pytest.raises(expected):
        asyncio.run(module._command("/usr/bin/pgrep", "-x", "Transmission"))
    process.kill.assert_called_once()
    assert process.wait.await_count == 2


def test_command_creation_errors_have_no_raw_path(monkeypatch):
    monkeypatch.setattr(
        module.asyncio, "create_subprocess_exec", AsyncMock(side_effect=OSError("private-path"))
    )
    with pytest.raises(TransmissionLaunchError) as caught:
        asyncio.run(module._command("private-path"))
    assert "private-path" not in str(caught.value)
