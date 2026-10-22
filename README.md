# Seed Scout — Telegram Torrent Downloader

Search RuTracker from Telegram and send a release to Transmission without copying torrent files between apps.

## What it does

- Shows search results with **Details**, **Download**, and **Sequential Download** buttons.
- Displays release descriptions, available posters, and paginated results.
- Lists active Transmission downloads and checks recent submissions with `/status`.
- Restores screens after inactivity or a restart. An expired download button asks you to confirm again.

## What you need

- Python 3.14 and [uv](https://docs.astral.sh/uv/).
- Google Chrome for RuTracker browser verification.
- Transmission with Remote Access (RPC) enabled.
- A Telegram bot token from [BotFather](https://t.me/BotFather), your Telegram user ID, and a RuTracker account.

The macOS installer expects Transmission.app and a graphical login session. Chrome may open during login; complete any browser verification while its window is open.

## Get started on macOS

Run the installer from the project directory as your normal user in an interactive terminal. Stop any manually running bot first.

```bash
./install.sh
```

The installer installs the locked runtime dependencies, checks Python and Transmission.app, and prompts for:

- `TELEGRAM_BOT_TOKEN` — your bot token.
- `TELEGRAM_ALLOWED_USER_IDS` — Telegram user IDs allowed to use the bot, separated by commas.
- `TRANSMISSION_RPC_URL` — defaults to `http://127.0.0.1:9091/transmission/rpc`.
- Whether Transmission RPC requires authentication; if so, `TRANSMISSION_USERNAME` and `TRANSMISSION_PASSWORD`.

An empty username and password are accepted together for RPC without authentication. A partially filled credential pair is rejected.

Choose the default **LaunchAgent** option. Existing values are offered as defaults; press **Enter** to keep them. Token and password entry are hidden. Choosing **no** for RPC authentication clears the saved RPC username and password when you confirm installation. At any prompt, **`:restart`** starts input over and **`:quit`** cancels. Review the summary and enter **yes** to save `.env` and install the service; other `.env` settings are preserved.

The service starts immediately and at sign-in. Keep the project at the same path; you can close the installer terminal afterward.

Enable Remote Access in Transmission itself and enter matching RPC settings in the installer. No manual `.env` setup is needed for these settings. Run `./install.sh` again to change them and reload the service. See `.env.example` for other optional settings.

Open your bot in a private Telegram chat and send `/start`. Choose **log in** or **search**, then follow the prompts. Enter your RuTracker credentials in Telegram when asked; they do not belong in `.env`.

To remove the service, run `./install.sh --uninstall`. Your configuration, database, logs, and other project files stay in place.

## Use the bot

| Command or button | What it does |
| --- | --- |
| `/start` | Opens the menu. |
| `/search` | Starts a new search. |
| **Details** | Opens the selected release’s description. |
| **Download** | Sends that release to Transmission. |
| **Sequential Download** | Sends the release with sequential downloading enabled; requires Transmission 4.1 or later. |
| **downloads** | Shows active Transmission downloads. |
| `/status` | Checks recent submissions and uncertain outcomes. |
| `/cancel` | Stops the current bot operation; does not remove torrents already sent to Transmission. |
| `/help` | Shows help. |

Send a new query from the results or details screen to search again. Previously issued buttons can restore their screen after inactivity or a service restart; expired download actions require a fresh confirmation. RuTracker may ask you to log in or complete a CAPTCHA again.

## Manual run (alternative)

Use this instead of the installer to run the bot in a terminal. If the login service is installed, stop it first with `./install.sh --uninstall`.

Create `.env` from `.env.example` only if it does not already exist. Set the Telegram and Transmission variables listed above, then run from the project directory:

```bash
uv sync --locked
uv run --locked tg-torrent-downloader --check-config
uv run --locked tg-torrent-downloader
```

Keep the terminal running; press Ctrl+C to stop. Exported environment variables override `.env` values in both setup paths.

## Keep your account information private

`.env`, runtime data, credentials, logs, browser-session exports, and local agent files are excluded from Git. The tracked `.env.example` contains placeholders only.

RuTracker credentials are stored **unencrypted** in `.runtime/bot.db`, restricted to your local user. Treat the database, backups, and any exported cookies as sensitive. Never share them or paste them into an issue.

## Troubleshooting

- **Login verification times out:** retry **log in** and complete any verification in the Chrome window. Check `.runtime/bot.log` for the failure stage.
- **Transmission is unavailable:** check that it is running and that Remote Access, RPC address, and credentials match your `.env`.
- **A submission is uncertain:** use `/status` before trying again.

## Development

```bash
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
```
