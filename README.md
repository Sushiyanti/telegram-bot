# Telegram Private Media Storage Bot

A private Telegram media-storage and access-control bot. Milestones 0 and 1 provide the foundation and persistence/core services; Milestone 2 adds the first private-DM media-ingestion flow. Delivery, membership enforcement, and share-link UI remain Milestone 3 work and are not included here.

## Runtime and dependencies

The project targets **CPython 3.15**. Python 3.15 is the current stable release line, and `python-telegram-bot` 22.8 declares Python 3.15 support. The repository pins the runtime to Python 3.15 for repeatable development. [Python's support table](https://devguide.python.org/versions/) [PTB on PyPI](https://pypi.org/project/python-telegram-bot/)

PTB 22.8 is the selected stable Telegram framework. It supports asynchronous polling and includes the message-copy, chat-member lookup, and invite-link methods planned for later milestones. Its PyPI metadata lists native Bot API support through version 10.0; the project will use only the established methods needed by this bot, not newer optional API features. [PTB Bot API method reference](https://docs.python-telegram-bot.org/en/v22.8/telegram.bot.html) [PTB package metadata](https://pypi.org/project/python-telegram-bot/)

Runtime dependencies are managed in `pyproject.toml` and the complete dependency graph is locked in `uv.lock`. SQLite persistence uses `aiosqlite`, WAL mode, foreign keys, a 5-second busy timeout, and versioned SQL migrations. The database stores metadata and Telegram message references, not media bytes. `python-dotenv` loads local `.env` values; `.env` is ignored by Git and `.env.example` contains placeholders/defaults only.

## Local setup

Install [uv](https://docs.astral.sh/uv/) and Python 3.15. From the repository root:

```sh
uv sync --locked --group dev
cp .env.example .env
```

Edit `.env` and replace the placeholders. Set `BOT_TOKEN` to a BotFather token, `STORAGE_CHANNEL_ID` to the numeric ID of the private storage channel, and `ADMIN_IDS` to comma-separated numeric Telegram user IDs. `DB_PATH` defaults to `./data/bot.db`; `LOG_LEVEL` defaults to `INFO`; `ENV` defaults to `dev`; and `MAX_RELAY_FILE_BYTES` defaults to `2147483648` (2 GiB). `UPLOADER_MODE` defaults to `admin`; choose `allowlist` and set `UPLOADER_IDS` to comma-separated numeric IDs to admit an explicit list, or `all` to accept any private user. `UPLOADER_MODE=all` with `ENV=prod` emits a startup warning. `DEFAULT_PROTECT_CONTENT` is retained as a future recipient-copy preference; M2 does not pass it to storage copies.

The bot can be started with either `uv run --locked --group dev python -m app` or `uv run --locked --group dev python -m media_bot`. Both entry points validate settings and initialize the database before polling. Missing or malformed settings produce a readable error that lists each invalid key. The token is never read from a tracked file; if a token is disclosed, revoke it through BotFather and replace it.

`/whoami` reports the caller's numeric user ID and configuration-based admin status. Admin-only `/configcheck` reports sanitized PASS/FAIL checks for `getMe`, storage-channel reachability and administrator status, channel privacy, admin-ID parsing/count, database version/writability, and the configured maximum relay size. Configcheck tests use mocked Telegram calls; no live PASS run has been made because a test bot and private channel have not been provisioned.

## M2 private media intake

Permitted users can send a document, photo, video, audio file, voice message, animation, or video note to the bot in a private DM. The bot checks that Telegram supplied a file size, the file does not exceed `MAX_RELAY_FILE_BYTES`, and the source message is not protected before attempting a copy. Albums are buffered by private chat and Telegram media-group ID with a 1.5-second debounce. A single message uses `copyMessage`; an album uses one `copyMessages` call, and a returned-count mismatch prevents item metadata from being committed. Telegram-side copies cannot be rolled back, so a partial result can leave unreferenced storage messages. The bot does not download or re-upload media and does not call `getFile`.

An accepted upload creates one item record, its storage-message references, and per-file metadata/fingerprint records. The item stores the aggregate byte count for an album; `sha256` remains `NULL`. A repeated `(uploader, file_unique_id)` is rejected before another copy. Confirmations include the item ID and size (and album message count), never a Telegram `file_id` or storage message ID. Rejections and storage-copy failures are written to `audit_log`; failures clear the M2 pending row. The M2 test suite mocks all Telegram methods. No live bot/channel integration has been run because a test bot, private storage channel, and authorized test user were not provisioned.

`DEFAULT_PROTECT_CONTENT` continues to be stored on item metadata as the future recipient-copy preference. M2 explicitly passes `protect_content=False` for both storage-copy methods; the config value has no effect on storage copies. Decide and verify recipient-facing semantics before M3.

## Tests

```sh
uv run --locked --group dev pytest
```

Tests cover configuration validation and `.env` loading, missing-config startup, application construction without network access, admin and uploader allow/deny behavior, `/whoami`, mocked configcheck pass/fail cases, single and album uploads, failure/rejection auditing, dedupe, SQLite migrations and constraints, repository persistence, secure token generation/hash/lookup/revocation, non-expiring-by-default links, pending-upload recovery, and backup/restore. They make no live Telegram calls.

## Persistence and share-link safety

The versioned schema is in `src/media_bot/db/migrations/`. Migration 0001 defines the persistent media, share-link, membership-target, and pending-upload data. Migration 0002 adds an append-only `audit_log`; denied admin commands record the actor ID, action, target type, and Python handler name. Migration 0003 adds nullable item SHA-256, per-file source metadata, a unique uploader/file-fingerprint map for dedupe, and a `pipeline` marker so M2 recovery does not consume legacy pending rows. Audit records have database triggers that reject update and delete operations.

Items map to one or more private storage message references. Share links store only a SHA-256 digest of a cryptographically random bearer token, are non-expiring by default, and can be revoked. **The proposed seven-day TTL change was deferred by the user; no TTL default change has been applied.** File sizes are integer byte counts when Telegram provides them; an unknown size is stored as `NULL`. There is no hardcoded maximum file size in the schema.

Share-link creation fails closed unless the item has storage message references, its size is known, and the selected transfer capability provides a verified end-to-end maximum at least as large as the item. No production transfer backend has been implemented or experimentally verified yet, so the current capability is unknown and this service does not enable real share-link creation.

## Database backup and restore

Create an online, consistent SQLite snapshot with SQLite's backup API (the destination must not already exist):

```sh
uv run --locked --no-dev python -m media_bot.db.backup backup \
  --database /var/lib/telegram-storage-bot/bot.db \
  --destination /var/backups/telegram-storage-bot-2026-11-20.db
```

The backup contains metadata, token hashes, audit records, and Telegram message references, not the media bytes held in the private channel. Treat backup files as sensitive, keep their permissions restricted, and protect them with your normal encrypted/off-site backup policy. Backup files are created with mode `0600`.

To restore, first stop the bot so no process has the live database open. Restore validates SQLite integrity, foreign keys, and schema version before replacing the target. The explicit flag acknowledges replacement:

```sh
sudo systemctl stop telegram-storage-bot
uv run --locked --no-dev python -m media_bot.db.backup restore \
  --backup /var/backups/telegram-storage-bot-2026-11-20.db \
  --database /var/lib/telegram-storage-bot/bot.db \
  --confirm-restore
sudo systemctl start telegram-storage-bot
```

Restoring metadata does not restore Telegram media if the private channel or its messages were deleted. The database and the Telegram storage channel need separate recovery planning.

## Simple VPS deployment

The service template is `deploy/telegram-storage-bot.service`. It uses one systemd process, outbound long polling, a dedicated service account, and `/var/lib/telegram-storage-bot` for persistent state. It exposes no inbound HTTP port. It intentionally does not start or deploy anything automatically.

A typical manual deployment is:

1. Create a `telegrambot` system account and install the repository under `/opt/telegram-storage-bot`.
2. Create a virtual environment and install only production dependencies from the lock: `uv sync --locked --no-dev`.
3. Create `/etc/telegram-storage-bot.env` with the required settings. Set `DB_PATH=/var/lib/telegram-storage-bot/bot.db` so the database is inside systemd's writable state directory. Restrict the environment file to the service account/root (`chmod 600`) and do not place it in the repository.
4. Copy the service unit to `/etc/systemd/system/telegram-storage-bot.service`, then run `systemctl daemon-reload` and `systemctl enable --now telegram-storage-bot`.
5. Inspect logs with `journalctl -u telegram-storage-bot -f`.

Before enabling the service, add the bot to the private storage channel as an administrator with permission to post. The configcheck command will test that condition when invoked by an admin, but no live check is run during startup or in automated tests. Do not run more than one active polling instance for this bot token.

## Current scope and next integration work

The implemented flow stops after storing an upload and confirming its item ID. Delivery, membership checks, share-link UI, and recipient `copyMessage` integration remain out of scope for M2 and are reserved for M3.

Large-file limits are being investigated separately. The complete future integration test must upload each fixture through a Premium or ordinary user account to the bot, copy it into the private channel, persist the message reference, copy it to a test recipient, and verify a full download/checksum. Documented limits and experimental results will be kept distinct; no size is supported merely because a client displays it.
