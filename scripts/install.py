"""Interactive launchd installer, invoked by install.sh; never run the bot as root."""

import argparse
import fcntl
import getpass
import os
import plistlib
import pwd
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LABEL = "org.seedscout.bot"
PROJECT = Path(__file__).resolve().parent.parent


class InstallError(Exception):
    """A diagnostic that can be displayed without exposing configuration secrets."""


class RestartInput(Exception):
    pass


def run(command, *, check=True):
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check and result.returncode:
        raise InstallError(f"Command failed: {Path(command[0]).name}. Configuration is retained.")
    return result.returncode == 0


def service_path(mode, home):
    directory = home / "Library/LaunchAgents" if mode == "agent" else Path("/Library/LaunchDaemons")
    return directory / f"{LABEL}.plist"


def service_command(mode, *args):
    command = ["/bin/launchctl", *args]
    return ["/usr/bin/sudo", *command] if mode == "daemon" else command


def domain(mode):
    return f"gui/{os.getuid()}" if mode == "agent" else "system"


def owned_service(path, project):
    if path.is_symlink():
        raise InstallError("Refusing a symlink at the service plist path.")
    if not path.exists():
        return False
    try:
        value = plistlib.loads(path.read_bytes())
    except Exception:
        raise InstallError(
            "Existing service plist could not be read; leaving it untouched."
        ) from None
    if (
        not isinstance(value, dict)
        or value.get("Label") != LABEL
        or value.get("WorkingDirectory") != str(project)
    ):
        raise InstallError("This service belongs to another checkout; leaving it untouched.")
    return True


def launchd_plist(project, mode, home):
    value = {
        "Label": LABEL,
        "ProgramArguments": [
            str(project / ".venv/bin/python"),
            "-u",
            "-m",
            "tg_torrent_downloader",
        ],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ExitTimeOut": 30,
        "Umask": 0o077,
        "StandardOutPath": str(project / ".runtime/launchd.stdout.log"),
        "StandardErrorPath": str(project / ".runtime/launchd.stderr.log"),
        "EnvironmentVariables": {
            "HOME": str(home),
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }
    if mode == "daemon":
        value["UserName"] = pwd.getpwuid(os.getuid()).pw_name
    else:
        value["LimitLoadToSessionType"] = "Aqua"
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=False)


def prompt(label, default="", *, secret=False):
    shown = "configured; Enter keeps it" if secret and default else default
    suffix = f" [{shown}]" if shown else ""
    reader = getpass.getpass if secret else input
    value = reader(f"{label}{suffix}: ")
    if value == ":restart":
        raise RestartInput
    if value == ":quit":
        raise KeyboardInterrupt
    return default if value == "" else value


def valid_ids(value):
    parts = value.split(",")
    return bool(parts) and all(
        re.fullmatch(r"[1-9][0-9]{0,15}", part.strip()) and int(part.strip()) < 2**52
        for part in parts
    )


def collect(values, default_mode):
    """No files are written during prompts, including when the user starts over."""
    while True:
        try:
            print(
                "\nEnter keeps the suggested value. :restart starts over; "
                ":quit exits without saving."
            )
            while True:
                mode = prompt("Service type (agent or daemon)", default_mode).strip().lower()
                if mode in {"agent", "daemon"}:
                    break
                print("Choose agent or daemon.")
            if mode == "daemon":
                print(
                    "LaunchDaemons cannot open visible Chrome. "
                    "RuTracker login needs a GUI solution; agent is recommended."
                )
            while True:
                token = prompt(
                    "TELEGRAM_BOT_TOKEN (from BotFather)",
                    values.get("TELEGRAM_BOT_TOKEN") or "",
                    secret=True,
                )
                if re.fullmatch(r"[1-9][0-9]*:[A-Za-z0-9_-]{20,}", token):
                    break
                print("Enter a valid Telegram bot token; it will not be displayed.")
            while True:
                users = prompt(
                    "TELEGRAM_ALLOWED_USER_IDS (comma separated)",
                    values.get("TELEGRAM_ALLOWED_USER_IDS") or "",
                )
                if valid_ids(users):
                    break
                print("Enter one or more positive Telegram user IDs separated by commas.")
            print(
                f"\nReview: service={mode}; TELEGRAM_BOT_TOKEN=[configured]; "
                f"TELEGRAM_ALLOWED_USER_IDS={users}"
            )
            answer = prompt("Save configuration and install service? (yes/no/:restart)", "no")
            if answer.lower() == "yes":
                return mode, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_ALLOWED_USER_IDS": users}
            if answer.lower() == "no":
                raise KeyboardInterrupt
            print("No changes saved. Starting input again.")
        except RestartInput:
            print("Starting over; original configuration is unchanged.")


def transmission_path(values, home):
    configured = values.get("TRANSMISSION_APP_PATH")
    candidates = (
        [configured]
        if configured
        else ["/Applications/Transmission.app", str(home / "Applications/Transmission.app")]
    )
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        try:
            bundle = plistlib.loads((path / "Contents/Info.plist").read_bytes())
            executable = path / "Contents/MacOS/Transmission"
            if (
                bundle.get("CFBundleIdentifier") == "org.m0k.transmission"
                and executable.is_file()
                and os.access(executable, os.X_OK)
            ):
                return path.resolve()
        except Exception:
            continue
    if configured:
        raise InstallError(
            "TRANSMISSION_APP_PATH does not point to a usable Transmission app. "
            "Correct that setting before installing; its value was kept."
        )
    raise InstallError(
        "Transmission.app is not installed. Install it in /Applications and try again."
    )


def temporary_file(directory, data, mode=0o600):
    descriptor, name = tempfile.mkstemp(prefix=".seedscout-install-", dir=directory)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def atomic_write(path, data, mode):
    temporary = temporary_file(path.parent, data, mode)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_env(project, original, updates):
    from dotenv import set_key

    from tg_torrent_downloader.config import ConfigurationError, Settings

    temporary = temporary_file(project, original or b"")
    try:
        for key, value in updates.items():
            set_key(temporary, key, value, quote_mode="always")
        try:
            settings = Settings.load(env={}, env_file=temporary)
        except ConfigurationError as error:
            raise InstallError(f"Configuration was not saved: {error}") from None
        return temporary.read_bytes(), settings.database_path
    finally:
        temporary.unlink(missing_ok=True)


def check_worker(database, project, *, wait=False):
    database = database if database.is_absolute() else project / database
    lock = database.with_name(database.name + ".runtime.lock")
    deadline = time.monotonic() + (10 if wait else 0)
    while lock.exists():
        try:
            descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            finally:
                os.close(descriptor)
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise InstallError(
                    "A bot is already running outside this service. "
                    "Stop it and retry; nothing was saved."
                ) from None
            time.sleep(0.2)


def put_plist(path, data, mode):
    if mode == "agent":
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, data, 0o644)
    else:
        temporary = temporary_file(PROJECT, data, 0o644)
        try:
            run(
                [
                    "/usr/bin/sudo",
                    "/usr/bin/install",
                    "-o",
                    "root",
                    "-g",
                    "wheel",
                    "-m",
                    "644",
                    str(temporary),
                    str(path),
                ]
            )
        finally:
            temporary.unlink(missing_ok=True)


def remove_plist(path, mode):
    if mode == "agent":
        path.unlink(missing_ok=True)
    else:
        run(["/usr/bin/sudo", "/bin/rm", "-f", str(path)])


def install(project, home, default_mode):
    from dotenv import dotenv_values

    env_path = project / ".env"
    if env_path.is_symlink():
        raise InstallError("Refusing a symlink at .env; nothing was changed.")
    original = env_path.read_bytes() if env_path.exists() else None
    values = dict(dotenv_values(env_path, interpolate=False)) if original is not None else {}
    defaults = {**values, **os.environ}
    app = transmission_path(defaults, home)
    print(f"Python {sys.version_info.major}.{sys.version_info.minor}: OK. Transmission: {app}")
    mode, updates = collect(defaults, default_mode)
    # Existing values, including explicit blanks, are preserved unless deliberately edited.
    if "TRANSMISSION_APP_PATH" not in values:
        updates["TRANSMISSION_APP_PATH"] = defaults.get("TRANSMISSION_APP_PATH", str(app))
    data, database = prepare_env(project, original, updates)
    path = service_path(mode, home)
    owned_service(path, project)
    other = service_path("daemon" if mode == "agent" else "agent", home)
    if other.exists() and owned_service(other, project):
        raise InstallError(
            "The other service type is installed. Uninstall it before switching types."
        )
    old_plist = path.read_bytes() if path.exists() else None
    loaded = run(service_command(mode, "print", f"{domain(mode)}/{LABEL}"), check=False)
    if loaded and old_plist is None:
        raise InstallError(
            "A service with this label is already loaded without this checkout’s plist; "
            "leaving it untouched."
        )
    if not loaded:
        check_worker(database, project)
    if (env_path.read_bytes() if env_path.exists() else None) != original:
        raise InstallError(
            ".env changed while input was being collected. Retry; nothing was overwritten."
        )
    runtime = project / ".runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    os.chmod(runtime, 0o700)
    # Create private launchd log files before launchd opens them.
    for name in ("launchd.stdout.log", "launchd.stderr.log"):
        descriptor = os.open(
            runtime / name, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600
        )
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
    if loaded:
        run(service_command(mode, "bootout", f"{domain(mode)}/{LABEL}"))
    env_written = plist_written = bootstrap_attempted = False
    try:
        check_worker(database, project, wait=loaded)
        if (env_path.read_bytes() if env_path.exists() else None) != original:
            raise InstallError(".env changed before saving. Retry; nothing was overwritten.")
        atomic_write(env_path, data, 0o600)
        env_written = True
        put_plist(path, launchd_plist(project, mode, home), mode)
        plist_written = True
        bootstrap_attempted = True
        run(service_command(mode, "bootstrap", domain(mode), str(path)))
    except BaseException:
        if bootstrap_attempted:
            run(service_command(mode, "bootout", f"{domain(mode)}/{LABEL}"), check=False)
        if env_written and env_path.exists() and env_path.read_bytes() == data:
            if original is not None:
                atomic_write(env_path, original, 0o600)
            else:
                env_path.unlink()
        if plist_written:
            if old_plist is not None:
                put_plist(path, old_plist, mode)
            else:
                remove_plist(path, mode)
        if old_plist is not None and loaded:
            run(service_command(mode, "bootstrap", domain(mode), str(path)), check=False)
        raise
    print(f"Installed {path}. Logs: {runtime / 'launchd.stderr.log'}")
    print(
        "Keep this checkout and its .venv in place. "
        "Run ./install.sh --uninstall to remove only the service."
    )


def uninstall(project, home, mode):
    if mode is None:
        existing = [kind for kind in ("agent", "daemon") if service_path(kind, home).exists()]
        if not existing:
            print("No installed service found. All application data was left in place.")
            return
        if len(existing) != 1:
            raise InstallError(
                "Specify --agent or --daemon when uninstalling; both service files exist."
            )
        mode = existing[0]
    path = service_path(mode, home)
    if not owned_service(path, project):
        print("Service not installed. All application data was left in place.")
        return
    target = f"{domain(mode)}/{LABEL}"
    if run(service_command(mode, "print", target), check=False):
        run(service_command(mode, "bootout", target))
    remove_plist(path, mode)
    print(
        "Service removed. Configuration, database, logs, source, "
        "dependencies, and Transmission were kept."
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--uninstall", action="store_true", help="remove only this checkout's launchd service"
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--agent",
        action="store_const",
        const="agent",
        dest="mode",
        help="default to a login LaunchAgent (recommended for visible Chrome)",
    )
    modes.add_argument(
        "--daemon",
        action="store_const",
        const="daemon",
        dest="mode",
        help="default to a boot LaunchDaemon; cannot open visible Chrome",
    )
    args = parser.parse_args(argv)
    try:
        if sys.platform != "darwin":
            raise InstallError("This installer supports macOS launchd only.")
        if os.geteuid() == 0:
            raise InstallError(
                "Run install.sh as your normal user, not with sudo; "
                "daemon commands request sudo individually."
            )
        home = Path.home()
        if args.uninstall:
            uninstall(PROJECT, home, args.mode)
        else:
            if not sys.stdin.isatty():
                raise InstallError(
                    "Run install.sh in an interactive terminal for private token input."
                )
            if sys.version_info[:2] != (3, 14):
                raise InstallError("Python 3.14 is required for installation.")
            install(PROJECT, home, args.mode or "agent")
    except KeyboardInterrupt, EOFError:
        print("\nCancelled; unfinished input was not saved.")
        return 130
    except InstallError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError, ValueError:
        print(
            "Installation failed. Check filesystem permissions and retry; no secrets were printed.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
