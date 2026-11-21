# Telegram Private Media Storage Bot

A private Telegram media-storage and access-control bot. Milestones 0 and 1 provide the foundation and persistence/core services; Milestone 2 adds private-DM media ingestion; Milestone 3 adds uploader self-verification, share-link commands, optional membership enforcement, and recipient delivery.

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

Edit `.env` and replace the placeholders. Set `BOT_TOKEN` to a BotFather token, `STORAGE_CHANNEL_ID` to the numeric ID of the private storage channel, and `ADMIN_IDS` to comma-separated numeric Telegram user IDs. `DB_PATH` defaults to `./data/bot.db`; `LOG_LEVEL` defaults to `INFO`; `ENV` defaults to `dev`; and `MAX_RELAY_FILE_BYTES` defaults to `2147483648` (2 GiB). The effective upload allowlist includes admins, active database uploader grants, and any supplied `UPLOADER_IDS`; `UPLOADER_MODE=all` remains the explicit open-upload override. Set optional `UPLOADER_REDEEM_CODE` to enable `/redeem <code>`, and optional `REQUIRED_CHANNEL_ID` to require channel membership before delivery. `VERIFIED_TRANSFER_MAX_FILE_BYTES` is an additional safety setting: leave it unset until recipient `copyMessage` support has been verified end to end; without it, share creation and delivery fail closed. `DEFAULT_PROTECT_CONTENT` applies to recipient-facing copies; storage copies continue to pass `protect_content=False`.

The bot can be started with either `uv run --locked --group dev python -m app` or `uv run --locked --group dev python -m media_bot`. Both entry points validate settings and initialize the database before polling. Missing or malformed settings produce a readable error that lists each invalid key. The token is never read from a tracked file; if a token is disclosed, revoke it through BotFather and replace it.

`/whoami` reports the caller's numeric user ID and configuration-based admin status. Admin-only `/configcheck` reports sanitized PASS/FAIL checks for `getMe`, storage-channel reachability and administrator status, channel privacy, admin-ID parsing/count, database version/writability, and the configured maximum relay size. A live `/configcheck` PASS was verified previously; automated configcheck tests continue to mock Telegram calls.

## M2 private media intake

Permitted users can send a document, photo, video, audio file, voice message, animation, or video note to the bot in a private DM. The bot checks that Telegram supplied a file size, the file does not exceed `MAX_RELAY_FILE_BYTES`, and the source message is not protected before attempting a copy. Albums are buffered by private chat and Telegram media-group ID with a 1.5-second debounce. A single message uses `copyMessage`; an album uses one `copyMessages` call, and a returned-count mismatch prevents item metadata from being committed. Telegram-side copies cannot be rolled back, so a partial result can leave unreferenced storage messages. The bot does not download or re-upload media and does not call `getFile`.

An accepted upload creates one item record, its storage-message references, and per-file metadata/fingerprint records. The item stores the aggregate byte count for an album; `sha256` remains `NULL`. A repeated `(uploader, file_unique_id)` is rejected before another copy. Confirmations include the item ID and size (and album message count), never a Telegram `file_id` or storage message ID. Rejections and storage-copy failures are written to `audit_log`; failures clear the M2 pending row. The M2 test suite mocks all Telegram methods. No live bot/channel integration has been run because a test bot, private storage channel, and authorized test user were not provisioned.

`DEFAULT_PROTECT_CONTENT` is passed to recipient-facing delivery copies only. The M2 intake path explicitly passes `protect_content=False` for storage copies.

## M3 uploader, sharing, and delivery

`/redeem <code>` verifies the optional uploader code and grants a database-backed uploader record. Attempts are limited to one per 30 seconds per user; successes and failures are audited without recording the supplied code. `/adduser <user_id>` and `/revoke <user_id>` are admin-only. `/share <item_id>` is limited to admins and uploaders and returns a `t.me` deep link; only the SHA-256 token digest is stored. Links retain the previously accepted non-expiring default and support optional persisted use limits at the repository/service layer.

Opening `/start <token>` checks token validity, expiry, revocation, remaining uses, item availability, and (when configured) `getChatMember` status before atomically consuming one use. Delivery loops through the ordered storage message references and calls Telegram `copyMessage`; albums are delivered as successive messages. Non-members are refused with a join instruction. Failed membership checks fail closed and do not consume a use. The bot does not download or re-upload media.

## Tests

```sh
uv run --locked --group dev pytest
```

Tests cover configuration validation and `.env` loading, missing-config startup, application construction without network access, admin and database-backed uploader authorization, `/whoami`, mocked configcheck pass/fail cases, single and album uploads, failure/rejection auditing, dedupe, `/redeem` success/failure/cooldown, admin grant/revoke, share URLs, member/non-member and album delivery, expiry and use limits, SQLite migrations and constraints, secure token generation/hash/lookup/revocation, non-expiring-by-default links, pending-upload recovery, and backup/restore. All Telegram calls in tests are mocked.

## Persistence and share-link safety

The current schema is version 4. Migration 0001 defines the persistent media, share-link, membership-target, and pending-upload data. Migration 0002 adds an append-only `audit_log`; denied admin commands record the actor ID, action, target type, and Python handler name. Migration 0003 adds nullable item SHA-256, per-file source metadata, a unique uploader/file-fingerprint map for dedupe, and a `pipeline` marker so M2 recovery does not consume legacy pending rows. Migration 0004 adds the identity parent table, uploader grants, durable redemption throttles, and share-link use counters/limits. Audit records have database triggers that reject update and delete operations.

Items map to one or more private storage message references. Share links store only a SHA-256 digest of a cryptographically random bearer token, are non-expiring by default, and can be revoked. **The proposed seven-day TTL change was deferred by the user; no TTL default change has been applied.** File sizes are integer byte counts when Telegram provides them; an unknown size is stored as `NULL`. There is no hardcoded maximum file size in the schema.

Share-link creation and delivery fail closed unless the item has storage message references, its size is known, and `VERIFIED_TRANSFER_MAX_FILE_BYTES` supplies a verified end-to-end maximum at least as large as the item. This explicit optional setting was added to preserve the accepted M1 transfer-capability contract rather than assuming `MAX_RELAY_FILE_BYTES` also proves recipient-copy support.

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

M3 implementation is complete. No later milestone work is included here; live Telegram integration tests are separate from the mocked automated suite and require a provisioned bot, storage channel, and authorized test accounts.

Large-file limits are being investigated separately. The complete future integration test must upload each fixture through a Premium or ordinary user account to the bot, copy it into the private channel, persist the message reference, copy it to a test recipient, and verify a full download/checksum. Documented limits and experimental results will be kept distinct; no size is supported merely because a client displays it.
