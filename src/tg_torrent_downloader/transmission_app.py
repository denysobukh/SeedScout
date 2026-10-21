"""Check and launch the configured macOS Transmission app without using a shell."""

import asyncio
import os
import plistlib
import sys
from pathlib import Path
from xml.parsers.expat import ExpatError

from tg_torrent_downloader.logging import diagnostic_event


class TransmissionLaunchError(Exception):
    """Safe startup diagnostic; never includes subprocess output or supplied paths."""


def _process_name(path: Path) -> str:
    try:
        with (path / "Contents" / "Info.plist").open("rb") as stream:
            bundle = plistlib.load(stream)
        executable = path / "Contents" / "MacOS" / "Transmission"
        if (
            not executable.is_file()
            or bundle.get("CFBundleIdentifier") != "org.m0k.transmission"
            or bundle.get("CFBundleExecutable") != "Transmission"
            or not os.access(executable, os.X_OK)
        ):
            raise TransmissionLaunchError("The configured app is not a usable Transmission bundle.")
    except OSError, ValueError, plistlib.InvalidFileException, AttributeError, ExpatError:
        raise TransmissionLaunchError(
            "The configured Transmission app could not be read."
        ) from None
    return "Transmission"


async def _command(*args: str) -> int:
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError:
        raise TransmissionLaunchError(
            "The Transmission startup command could not be run."
        ) from None
    try:
        return await asyncio.wait_for(process.wait(), timeout=5)
    except (TimeoutError, asyncio.CancelledError) as error:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        if isinstance(error, asyncio.CancelledError):
            raise
        raise TransmissionLaunchError("The Transmission startup command timed out.") from None


async def _running(name: str) -> bool:
    result = await _command("/usr/bin/pgrep", "-u", str(os.getuid()), "-x", name)
    if result not in {0, 1}:
        raise TransmissionLaunchError("Could not check whether Transmission is running.")
    return result == 0


async def ensure_transmission_running(path: Path | None) -> bool:
    """Return True only when this startup launched the app; blank config disables launch.

    Checks the current user's process, never stops Transmission, and makes no RPC changes.
    The GUI app must already have remote access enabled for the configured RPC endpoint.
    """
    if path is None:
        return False
    if sys.platform != "darwin":
        raise TransmissionLaunchError("Automatic Transmission app startup requires macOS.")
    name = await asyncio.to_thread(_process_name, path)
    if await _running(name):
        diagnostic_event("transmission.app.running")
        return False
    if await _command("/usr/bin/open", "-g", "-a", str(path)) != 0:
        raise TransmissionLaunchError(
            "Transmission could not be launched. Try starting it manually."
        )

    async def wait_running():
        while not await _running(name):
            await asyncio.sleep(0.25)

    try:
        await asyncio.wait_for(wait_running(), timeout=10)
    except TimeoutError:
        raise TransmissionLaunchError(
            "Transmission did not start in time. Try starting it manually."
        ) from None
    diagnostic_event("transmission.app.started")
    return True
