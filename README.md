# Telegram Private Media Storage Bot

A private Telegram media-storage and access-control bot. Milestones 0 and 1 provide the foundation and persistence/core services; Milestone 2 adds private-DM media ingestion; Milestone 3 adds uploader self-verification, share-link commands, optional membership enforcement, and recipient delivery; Milestone 4 adds admin item management, failure handling, and VPS deployment guidance; M5 adds category-specific storage and dynamic access requirements; M6 adds database-backed admin roles and an inline admin panel. M6 was accepted on 2026-11-23 based on mocked validation; live Telegram behavior remains unverified. The [M6 completion handoff](docs/M6_COMPLETION_HANDOFF.md) records the acceptance boundary, and [M7 plan](docs/M7_PLAN.md) scopes isolated transfer-readiness work.

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

`/whoami` reports the caller's numeric user ID and effective environment-or-database admin status. Admin-only `/configcheck` reports sanitized PASS/FAIL checks for `getMe`, storage-channel reachability and administrator status, channel privacy, admin-ID parsing/count, database version/writability, and the configured maximum relay size. A live `/configcheck` PASS was verified previously; automated configcheck tests continue to mock Telegram calls.

## M2 private media intake

Permitted users can send a document, photo, video, audio file, voice message, animation, or video note to the bot in a private DM. The bot checks that Telegram supplied a file size, the file does not exceed `MAX_RELAY_FILE_BYTES`, and the source message is not protected before attempting a copy. Albums are buffered by private chat and Telegram media-group ID with a 1.5-second debounce. A single message uses `copyMessage`; an album uses one `copyMessages` call, and a returned-count mismatch prevents item metadata from being committed. Telegram-side copies cannot be rolled back, so a partial result can leave unreferenced storage messages. The bot does not download or re-upload media and does not call `getFile`.

An accepted upload creates one item record, its storage-message references, and per-file metadata/fingerprint records. The item stores the aggregate byte count for an album; `sha256` remains `NULL`. A repeated `(uploader, file_unique_id)` is rejected before another copy. Confirmations include the item ID and size (and album message count), never a Telegram `file_id` or storage message ID. Rejections and storage-copy failures are written to `audit_log`; failures clear the M2 pending row. The M2 test suite mocks all Telegram methods. No live bot/channel integration has been run because a test bot, private storage channel, and authorized test user were not provisioned.

`DEFAULT_PROTECT_CONTENT` is passed to recipient-facing delivery copies only. The M2 intake path explicitly passes `protect_content=False` for storage copies.

## M3 uploader, sharing, and delivery

`/redeem <code>` verifies the optional uploader code and grants a database-backed uploader record. Attempts are limited to one per 30 seconds per user; successes and failures are audited without recording the supplied code. `/adduser <user_id>` and `/revoke <user_id>` are admin-only; `/revoke` refuses targets in `ADMIN_IDS`. `/share <item_id>` is limited to admins and uploaders; non-admin uploaders may share only items they own, while admins may share any item. The command returns a `t.me` deep link; only the SHA-256 token digest is stored. Links retain the previously accepted non-expiring default and support optional persisted use limits at the repository/service layer.

Opening `/start <token>` checks token validity, expiry, revocation, remaining uses, item availability, and (when configured) `getChatMember` status before atomically consuming one use. Delivery loops through the ordered storage message references and calls Telegram `copyMessage`; albums are delivered as successive messages. Non-members are refused with a join instruction. Failed membership checks fail closed and do not consume a use; if membership cannot be checked, the recipient is directed to contact the administrator. If Telegram reports that a source storage message is missing, the bot replies `This file is no longer available` and removes the item and its dependent metadata from the database. The bot does not download or re-upload media.

## M4 admin management and resilience

Admin-only `/list [limit]` shows up to 100 recent items (10 by default), with item ID, uploader ID, media type, byte size, and UTC creation date; Telegram file IDs are never included. Admin-only `/delete <item_id>` transactionally removes the item, storage references, share links, per-file metadata, and fingerprints, then best-effort deletes the associated storage-channel messages. Telegram deletion errors (including a message already removed manually) do not interrupt database cleanup. At M4, the schema was version 4; see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for a headless VPS and scheduled-backup guide.

## M5 categories and dynamic requirements

Admin-only `/addcategory`, `/linkcategory`, `/addrequirement`, `/delcategory`, and `/togglecommands` provide multi-category storage, inherited/global requirements, safe category deletion, and a persistent non-admin command/upload lock. REACT rules use the latest five eligible posts by default, with optional message-count and age windows. When more than one category exists, an uploader must choose a storage destination before any Telegram copy occurs; the pending choice is durable and owner-checked. Global, category, and inherited child requirements are evaluated before a share-link use is consumed. Details, command syntax, fallback behavior, and Telegram API constraints are documented in [docs/M5_CATEGORIES_AND_REQUIREMENTS.md](docs/M5_CATEGORIES_AND_REQUIREMENTS.md).

## M6 admin roles and control panel

Migration 0006 adds durable database-backed administrator roles. On startup, every validated `ADMIN_IDS` entry is upserted into `admins`; those environment IDs remain super-admins even if the database is unavailable and cannot be removed through the panel. Other DB administrators and uploaders can be managed in `/admin` (also `/panel`). Each explicit panel command opens a fresh message and retires the earlier one; navigation and Add flows edit the active message. The Categories section supports Add Category. Rule creation is a button-driven wizard; targets come from forwarded messages or numeric IDs, shared rules offer unlink-versus-delete-everywhere choices, and pending wizard/Add state expires after five minutes. When `ENABLE_USER_COMMANDS=false`, admins and active uploaders bypass the lock; regular users are blocked from commands other than `/start` and from uploads. See [docs/M6_ADMIN_PANEL.md](docs/M6_ADMIN_PANEL.md) and the [M6 completion handoff](docs/M6_COMPLETION_HANDOFF.md) for exact behavior and accepted scope.

## Tests

```sh
uv run --locked --group dev pytest
```

Tests cover configuration validation and `.env` loading, missing-config startup, application construction without network access, environment/database admin roles and startup seeding, admin-panel keyboards/callbacks/state transitions, admin and database-backed uploader authorization, `/whoami`, mocked configcheck pass/fail cases, single and album uploads, failure/rejection auditing, dedupe, `/redeem` success/failure/cooldown, admin grant/revoke/list/delete, share URLs, member/non-member and album delivery, missing-channel and missing-storage behavior, expiry and use limits, SQLite migrations and constraints, secure token generation/hash/lookup/revocation, non-expiring-by-default links, pending-upload recovery, category selection/recovery/deletion, inherited and global requirements, START_BOT limitations, JOIN_CHAT and REACT checks, command lockdown and uploader bypass, and backup/restore. All Telegram calls in tests are mocked.

## Persistence and share-link safety

The current schema is version 6. Migration 0001 defines the persistent media, share-link, membership-target, and pending-upload data. Migration 0002 adds an append-only `audit_log`; denied admin commands record the actor ID, action, target type, and Python handler name. Migration 0003 adds nullable item SHA-256, per-file source metadata, a unique uploader/file-fingerprint map for dedupe, and a `pipeline` marker so M2 recovery does not consume legacy pending rows. Migration 0004 adds the identity parent table, uploader grants, durable redemption throttles, and share-link use counters/limits. Migration 0005 adds categories, item and pending-upload category metadata, requirements and inheritance, command settings, observed posts, and durable reaction state. Migration 0006 adds the `admins` role registry; startup seeds configured environment administrators without removing DB-only roles. Audit records have database triggers that reject update and delete operations.

Items map to one or more private storage message references. Share links store only a SHA-256 digest of a cryptographically random bearer token, are non-expiring by default, and can be revoked. File sizes are integer byte counts when Telegram provides them; an unknown size is stored as `NULL`. There is no hardcoded maximum file size in the schema.

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

The service template is `deploy/telegram-storage-bot.service`. It uses one systemd process, outbound long polling, a dedicated service account, and `/var/lib/telegram-storage-bot` for persistent state. It exposes no inbound HTTP port. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for complete headless installation and backup scheduling instructions; this guide does not deploy or start anything automatically.

A typical manual deployment is:

1. Create a `telegrambot` system account and install the repository under `/opt/telegram-storage-bot`.
2. Create a virtual environment and install only production dependencies from the lock: `uv sync --locked --no-dev`.
3. Create `/etc/telegram-storage-bot.env` with the required settings. Set `DB_PATH=/var/lib/telegram-storage-bot/bot.db` so the database is inside systemd's writable state directory. Restrict the environment file to the service account/root (`chmod 600`) and do not place it in the repository.
4. Copy the service unit to `/etc/systemd/system/telegram-storage-bot.service`, then run `systemctl daemon-reload` and `systemctl enable --now telegram-storage-bot`.
5. Inspect logs with `journalctl -u telegram-storage-bot -f`.

Before enabling the service, add the bot to the private storage channel as an administrator with permission to post. The configcheck command will test that condition when invoked by an admin, but no live check is run during startup or in automated tests. Do not run more than one active polling instance for this bot token.

## Current scope and next integration work

M6 was accepted on 2026-11-23 based on the mocked automated suite; live Telegram integration remains unverified and requires a provisioned non-production bot, storage channels, and authorized test accounts. M7 is scoped in [docs/M7_PLAN.md](docs/M7_PLAN.md) around isolated end-to-end transfer readiness. No file-size capability is certified until the full recipient-download/checksum path passes.

Large-file limits are being investigated separately. The complete future integration test must upload each fixture through a Premium or ordinary user account to the bot, copy it into the private channel, persist the message reference, copy it to a test recipient, and verify a full download/checksum. Documented limits and experimental results will be kept distinct; no size is supported merely because a client displays it.
