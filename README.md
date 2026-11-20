# Telegram Private Media Storage Bot

A private Telegram media-storage and access-control bot. Milestone 0 foundation and Milestone 1 persistence/core services are implemented. The bot still has only a placeholder `/start` handler: Telegram media ingestion/delivery, share-link handlers, membership checks, and admin commands are not implemented yet.

## Runtime and dependencies

The project targets **CPython 3.15**. Python 3.15 is the current stable release line, and `python-telegram-bot` 22.8 declares Python 3.15 support. The repository pins the runtime to Python 3.15 for repeatable development. [Python's support table](https://devguide.python.org/versions/) [PTB on PyPI](https://pypi.org/project/python-telegram-bot/)

PTB 22.8 is the selected stable Telegram framework. It supports asynchronous polling and includes the message-copy, chat-member lookup, and invite-link methods planned for later milestones. Its PyPI metadata lists native Bot API support through version 10.0; the project will use only the established methods needed by this bot, not newer optional API features. [PTB Bot API method reference](https://docs.python-telegram-bot.org/en/v22.8/telegram.bot.html) [PTB package metadata](https://pypi.org/project/python-telegram-bot/)

Runtime dependencies are pinned in `pyproject.toml` and the complete dependency graph is locked in `uv.lock`. SQLite persistence uses `aiosqlite`, WAL mode, foreign keys, a 5-second busy timeout, and versioned SQL migrations. The database stores metadata and Telegram message references, not media bytes.

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

The entry point validates settings, creates and checks the database directory, applies migrations, then starts long polling. The current `/start` response only says the bot is still being configured. The actual token is never read from a tracked file and must not be committed. If a token is ever disclosed, revoke it through BotFather and replace it.

## Tests

```sh
uv run --locked --group dev pytest
```

The tests exercise configuration validation, state-directory preflight, network-free application construction, SQLite migrations and constraints, repository persistence, token hashing/lookup/revocation, multiple links per item, required-chat persistence, pending-upload recovery, and backup/restore. They do not call Telegram's network API or claim that any file size works end to end.

## Persistence and share-link safety

The versioned schema is in `src/media_bot/db/migrations/`. Items map to one or more private storage message references. Share links store only a SHA-256 digest of a cryptographically random bearer token, are non-expiring by default, and can be revoked. Required channels/groups and recoverable pending-upload album messages are also persisted. File sizes are integer byte counts when Telegram provides them; an unknown size is stored as `NULL`. There is no hardcoded maximum file size in the schema.

Share-link creation fails closed unless the item has storage message references, its size is known, and the selected transfer capability provides a verified end-to-end maximum at least as large as the item. No production transfer backend has been implemented or experimentally verified yet, so the current capability is unknown and this service does not enable real share-link creation.

## Database backup and restore

Create an online, consistent SQLite snapshot with SQLite's backup API (the destination must not already exist):

```sh
uv run --locked --no-dev python -m media_bot.db.backup backup \
  --database /var/lib/telegram-storage-bot/bot.sqlite3 \
  --destination /var/backups/telegram-storage-bot-2026-11-20.sqlite3
```

The backup contains metadata, token hashes, and Telegram message references, not the media bytes held in the private channel. Treat backup files as sensitive, keep their permissions restricted, and protect them with your normal encrypted/off-site backup policy. Backup files are created with mode `0600`.

To restore, first stop the bot so no process has the live database open. Restore validates SQLite integrity, foreign keys, and schema version before replacing the target. The explicit flag acknowledges replacement:

```sh
sudo systemctl stop telegram-storage-bot
uv run --locked --no-dev python -m media_bot.db.backup restore \
  --backup /var/backups/telegram-storage-bot-2026-11-20.sqlite3 \
  --database /var/lib/telegram-storage-bot/bot.sqlite3 \
  --confirm-restore
sudo systemctl start telegram-storage-bot
```

Restoring metadata does not restore Telegram media if the private channel or its messages were deleted. The database and the Telegram storage channel need separate recovery planning.

## Simple VPS deployment

The service template is `deploy/telegram-storage-bot.service`. It uses one systemd process, outbound long polling, a dedicated service account, and `/var/lib/telegram-storage-bot` for persistent state. It exposes no inbound HTTP port. It intentionally does not start or deploy anything automatically.

A typical manual deployment is:

1. Create a `telegrambot` system account and install the repository under `/opt/telegram-storage-bot`.
2. Create a virtual environment and install only production dependencies from the lock: `uv sync --locked --no-dev`.
3. Create `/etc/telegram-storage-bot.env` with the required settings. Set `DATABASE_PATH=/var/lib/telegram-storage-bot/bot.sqlite3` so the database is inside systemd’s writable state directory. Restrict the environment file to the service account/root (`chmod 600`) and do not place it in the repository.
4. Copy the service unit to `/etc/systemd/system/telegram-storage-bot.service`, then run `systemctl daemon-reload` and `systemctl enable --now telegram-storage-bot`.
5. Inspect logs with `journalctl -u telegram-storage-bot -f`.

Before enabling the service, add the bot to the private storage channel as an administrator with permission to post. Do not run more than one active polling instance for this bot token. Actual storage-channel validation and membership-chat setup are deferred to later milestones.

## Current scope and next integration work

The systemd template still configures one active process, outbound long polling, a dedicated service account, and `/var/lib/telegram-storage-bot` for persistent state. It does not run or deploy anything automatically. Add the bot to the private storage channel as an administrator with posting permission before later transfer testing. No storage channel, admin IDs, or membership chats are configured in this repository.

Large-file limits are being investigated separately. The complete future integration test must upload each fixture through a Premium or ordinary user account to the bot, copy it into the private channel, persist the message reference, copy it to a test recipient, and verify a full download/checksum. Documented limits and experimental results will be kept distinct; no size is supported merely because a client displays it.
