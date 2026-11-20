# Telegram Private Media Storage Bot

A private Telegram media-storage and access-control bot. This repository currently contains **Milestone 0 only**: project setup, environment validation, a minimal polling entry point, logging, tests, and a systemd template. Uploads, storage operations, deep links, membership checks, and admin commands are not implemented yet.

## Runtime and dependencies

The project targets **CPython 3.15**. Python 3.15 is the current stable release line; `python-telegram-bot` 22.8 declares Python 3.15 support. Python 3.14 remains a reasonable conservative fallback, but the repository is currently pinned to 3.15 for repeatable development. [Python's support table](https://devguide.python.org/versions/) [PTB on PyPI](https://pypi.org/project/python-telegram-bot/)

PTB 22.8 is the selected stable Telegram framework. It supports asynchronous polling and includes the message-copy, chat-member lookup, and invite-link methods planned for later milestones. Its PyPI metadata lists native Bot API support through version 10.0; the project will use only the established methods needed by this bot, not newer optional API features. [PTB Bot API method reference](https://docs.python-telegram-bot.org/en/v22.8/telegram.bot.html) [PTB package metadata](https://pypi.org/project/python-telegram-bot/)

Runtime dependencies are pinned in `pyproject.toml` and the complete dependency graph is locked in `uv.lock`. SQLite access will use `aiosqlite`; schema and repository code are deferred to Milestone 1.

## Local setup

Install [uv](https://docs.astral.sh/uv/) and Python 3.15. From the repository root:

```sh
uv sync --locked --group dev
cp .env.example .env
```

Edit `.env` and replace all placeholders. Set `BOT_TOKEN` to a BotFather token, `ADMIN_IDS` to one or more comma-separated numeric Telegram user IDs, and `STORAGE_CHAT_ID` to the numeric ID of the private storage channel. `DATABASE_PATH` defaults to `./data/bot.sqlite3` in the example. The default `DEFAULT_PROTECT_CONTENT=false` keeps normal recipient saving/forwarding behavior; later milestones can override it per item or delivery.

Load local settings into the current shell and start the placeholder bot:

```sh
set -a
. ./.env
set +a
uv run --locked --group dev telegram-storage-bot
```

The entry point validates settings, creates and checks the database directory, then starts long polling. The current `/start` response only says the bot is still being configured. The actual token is never read from a tracked file and must not be committed. If a token is ever disclosed, revoke it through BotFather and replace it.

## Tests

```sh
uv run --locked --group dev pytest
```

The tests exercise settings validation, token-safe errors, state-directory preflight, and construction of the PTB application without network calls. They do not claim to verify live Telegram permissions or media transfer. Those integration checks will be added when the relevant milestones are implemented and the bot’s private storage channel and administrator IDs are configured.

## Simple VPS deployment

The service template is `deploy/telegram-storage-bot.service`. It uses one systemd process, outbound long polling, a dedicated service account, and `/var/lib/telegram-storage-bot` for persistent state. It exposes no inbound HTTP port. It intentionally does not start or deploy anything automatically.

A typical manual deployment is:

1. Create a `telegrambot` system account and install the repository under `/opt/telegram-storage-bot`.
2. Create a virtual environment and install only production dependencies from the lock: `uv sync --locked --no-dev`.
3. Create `/etc/telegram-storage-bot.env` with the required settings. Set `DATABASE_PATH=/var/lib/telegram-storage-bot/bot.sqlite3` so the database is inside systemd’s writable state directory. Restrict the environment file to the service account/root (`chmod 600`) and do not place it in the repository.
4. Copy the service unit to `/etc/systemd/system/telegram-storage-bot.service`, then run `systemctl daemon-reload` and `systemctl enable --now telegram-storage-bot`.
5. Inspect logs with `journalctl -u telegram-storage-bot -f`.

Before enabling the service, add the bot to the private storage channel as an administrator with permission to post. Do not run more than one active polling instance for this bot token. Actual storage-channel validation and membership-chat setup are deferred to later milestones.

## Storage and backup note

The planned SQLite database stores metadata and Telegram message references, **not the media bytes**. Media will live in the private Telegram storage channel. Losing that channel or deleting its stored messages can therefore mean losing the files even if the SQLite database is intact. The WAL-aware backup and restore procedure will be documented alongside the database schema in Milestone 1.
