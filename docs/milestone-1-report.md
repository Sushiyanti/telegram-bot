# Milestone 1 completion report

**Status:** Milestone 1 persistence/core services were accepted at commit `6653a4b`. The explicitly skipped configuration, authorization, and diagnostic gate items have now been closed in a follow-up commit. Work stops before Milestone 2; no upload/delivery flow has been added.

## Implemented

SQLite initializes before polling, uses WAL mode, enables foreign keys on every connection, applies a 5-second busy timeout, and uses full synchronous durability. The database file is created with owner-only permissions. Schema evolution is tracked with SQLite `user_version` and packaged versioned SQL migrations.

The schema contains the core media, link, membership-target, and pending-upload records. `items` stores media metadata and exact integer `file_size_bytes` when known; an absent Telegram size remains `NULL`, not an estimate. `item_storage_messages` maps one item to one or more channel/message references, supporting albums. `share_links` points to an item and stores only a unique 32-byte SHA-256 token digest, with optional expiry and revocation timestamp. `required_chats` persists channel, group, and supergroup membership targets. `pending_uploads` and `pending_upload_messages` persist collection state and message/file metadata so future album processing can resume after restart.

Core services coordinate stored-item creation, secure bearer-token creation/lookup/revocation, required-chat records, and recoverable pending uploads. Links remain non-expiring by default, as explicitly retained by the user. Link creation fails closed unless the stored item has references, its size is known, and the selected transfer capability has a verified end-to-end maximum that covers the file. No maximum size is hardcoded in the schema or production service. `media_transfer.py` provides a small transfer-only Protocol seam; no backend implementation exists yet.

Online backups use SQLite's backup API. Restore validates database integrity, foreign keys, and schema version before replacing the destination. Restore requires the bot service to be stopped and an explicit `--confirm-restore` flag.

The closure adds validated dotenv-backed settings using `BOT_TOKEN`, `STORAGE_CHANNEL_ID`, `ADMIN_IDS`, and the documented optional values. It provides the `python -m app` entry point, `/whoami`, and admin-only `/configcheck`. Admin authorization uses configuration rather than database state. Denied admin command attempts are recorded in an append-only `audit_log`; migration `0002_audit_log.sql` upgrades prior schema version 1 databases to version 2.

## Files changed or added

The initial Milestone 1 files include `README.md`, `src/media_bot/__main__.py`, `src/media_bot/db/database.py`, `src/media_bot/db/migrations/0001_initial.sql`, `src/media_bot/db/models.py`, `src/media_bot/db/repositories.py`, `src/media_bot/db/backup.py`, `src/media_bot/media_transfer.py`, `src/media_bot/services/core.py`, `tests/test_database.py`, and `docs/large-file-feasibility.md`.

The closure follow-up also changes `pyproject.toml` and `uv.lock`; adds `src/app/`, `src/media_bot/auth.py`, `src/media_bot/db/audit_repository.py`, `src/media_bot/db/migrations/0002_audit_log.sql`, and `src/media_bot/handlers/diagnostics.py`; and updates `.env.example`, `README.md`, `deploy/telegram-storage-bot.service`, `src/media_bot/application.py`, `src/media_bot/config.py`, `src/media_bot/startup.py`, and tests.

## Verification

The initial Milestone 1 suite passed 31 tests at the accepted base commit. After the closure changes, the complete suite passed **56 tests** on the project interpreter. Coverage includes configuration and dotenv validation, clear missing-config startup, application construction without network access, admin allow/deny behavior and its audit record, `/whoami`, mocked configcheck PASS/FAIL cases, SQLite migration from schema version 1, append-only audit enforcement, repository persistence, non-expiring default links, minimum token entropy, pending-upload recovery, and backup/restore. No Telegram network calls were made.

`uv lock --check`, `uv build`, `python -m compileall -q src tests`, and `git diff --check` all passed during closure verification. The built wheel contains both entry-point packages and migrations 0001 and 0002. Runtime dependencies now include `python-dotenv` for `.env` loading; PTB remains 22.8 and aiosqlite remains 0.22.1.

## Large-file findings

Telegram documents Premium user uploads up to 4 GB, but this is not the hosted Bot API's file retrieval allowance. The hosted Bot API `getFile` limit is 20 MB. Bot multipart-send limits are separate. A Premium upload addressed to a bot appears plausible, but the reviewed documentation does not definitively guarantee that exact route.

`copyMessage` and `copyMessages` provide a server-side copy route using message references, so the bot backend need not download and re-upload media bytes for that step. The methods state no media byte-size ceiling, but do not guarantee arbitrary-size or 4 GB copies. Telegram's Local Bot API server documents downloads without a size limit and uploads up to 2000 MB. MTProto with a Premium user is technically plausible: Telegram's published Premium part count and 512 KiB part size imply a derived 4,194,304,000-byte protocol ceiling, but that is not a transfer test or guaranteed acceptance limit. Premium entitlement for a bot-token MTProto session is unestablished.

**No sizes have been experimentally verified end to end.** The detailed report distinguishes official documentation, inference, and test status. Its test plan defines decimal-byte fixtures at 50,000,000 through 4,000,000,000 bytes and requires the full upload → storage copy → persisted reference/restart → recipient DM copy → full download/checksum journey. Visibility of a chat message alone is not a pass.

## Remaining setup and scope boundary

For a later isolated integration run, provision a test bot/token in an approved secret store, numeric admin user ID(s), and a private test storage channel ID with the bot able to post. Also provide a recipient test account that opens the bot and presses Start, and a Premium uploader account for Premium-sized tests if required. Fixtures need exact byte counts and expected SHA-256 values; they can be supplied or generated for testing. A local Bot API comparison needs its own isolated deployment. An MTProto comparison also needs the appropriate API credentials and an approved account/session setup. Keep all credentials, account identifiers, and channel IDs out of Git and test logs.

Only the diagnostic commands `/whoami` and `/configcheck` were added in this closure; no live configcheck PASS run was made because a test bot and private channel are not provisioned. Upload/delivery handlers, membership enforcement, management/admin operations, share-link UI, and production transfer backends remain unimplemented. The proposed seven-day TTL default is explicitly deferred, and share links remain non-expiring by default. This report stops before Milestone 2.
