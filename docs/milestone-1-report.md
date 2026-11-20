# Milestone 1 completion report

**Status:** complete; implementation stops at persistent schema and core services. No media ingestion or delivery workflow has been added.

## Implemented

SQLite now initializes before polling, uses WAL mode, enables foreign keys on every connection, applies a 5-second busy timeout, and uses full synchronous durability. The database file is created with owner-only permissions. Schema evolution is tracked with SQLite `user_version` and packaged versioned SQL migrations.

The schema contains five core record groups. `items` stores media metadata and exact integer `file_size_bytes` when known; an absent Telegram size remains `NULL`, not an estimate. `item_storage_messages` maps one item to one or more channel/message references, supporting albums. `share_links` points to an item and stores only a unique 32-byte SHA-256 token digest, with optional expiry and revocation timestamp. `required_chats` persists channel, group, and supergroup membership targets. `pending_uploads` and `pending_upload_messages` persist collection state and message/file metadata so future album processing can resume after restart.

Core services coordinate stored-item creation, secure bearer-token creation/lookup/revocation, required-chat records, and recoverable pending uploads. Links default to non-expiring. Link creation fails closed unless the stored item has references, its size is known, and the selected transfer capability has a verified end-to-end maximum that covers the file. No maximum size is hardcoded in the schema or production service. `media_transfer.py` provides a small transfer-only Protocol seam; no backend implementation exists yet.

Online backups use SQLite's backup API. Restore validates database integrity, foreign keys, and schema version before replacing the destination. Restore requires the bot service to be stopped and an explicit `--confirm-restore` flag.

## Files changed or added

- `README.md` now documents the Milestone 1 state, fail-closed size policy, and backup/restore procedures.
- `src/media_bot/__main__.py` initializes/migrates the database before starting long polling.
- `src/media_bot/db/__init__.py` exports database and repository types.
- `src/media_bot/db/database.py` implements connection configuration, migration application, and backup/restore.
- `src/media_bot/db/migrations/0001_initial.sql` defines schema version 1; `src/media_bot/db/migrations/__init__.py` packages the SQL resource.
- `src/media_bot/db/models.py` defines persistent record types.
- `src/media_bot/db/repositories.py` implements transactional repositories and pending-upload state transitions.
- `src/media_bot/db/backup.py` adds credential-free backup and guarded restore commands.
- `src/media_bot/media_transfer.py` isolates the transfer interface and verified-capability value.
- `src/media_bot/services/__init__.py` exports the services; `src/media_bot/services/core.py` implements their coordination and token handling.
- `tests/test_database.py` adds persistence, schema, security, recovery, and backup tests.
- `docs/large-file-feasibility.md` records source-backed findings and an isolated end-to-end test plan.

## Verification

The complete suite passed: **31 tests passed** on Python 3.15.2. Coverage includes fresh initialization, repeatable migrations, foreign-key enforcement, close/reopen persistence, invalid and inconsistent records, token generation/hash/lookup/revocation, multiple links per item, required-chat persistence, pending-upload recovery and transitions, and backup/restore. The tests make no Telegram network calls.

`uv lock --check` passed. `uv build` produced the source distribution and wheel; I also verified the wheel contains `0001_initial.sql`. `compileall` and `git diff --check` passed. Runtime dependencies remain PTB 22.8 and aiosqlite 0.22.1; no new infrastructure or dependencies were introduced.

## Large-file findings

Telegram documents Premium user uploads up to 4 GB, but this is not the hosted Bot API's file retrieval allowance. The hosted Bot API `getFile` limit is 20 MB. Bot multipart-send limits are separate. A Premium upload addressed to a bot appears plausible, but the reviewed documentation does not definitively guarantee that exact route.

`copyMessage` and `copyMessages` provide a server-side copy route using message references, so the bot backend need not download and re-upload media bytes for that step. The methods state no media byte-size ceiling, but do not guarantee arbitrary-size or 4 GB copies. Telegram's Local Bot API server documents downloads without a size limit and uploads up to 2000 MB. MTProto with a Premium user is technically plausible: Telegram's published Premium part count and 512 KiB part size imply a derived 4,194,304,000-byte protocol ceiling, but that is not a transfer test or guaranteed acceptance limit. Premium entitlement for a bot-token MTProto session is unestablished.

**No sizes have been experimentally verified end to end.** The detailed report distinguishes official documentation, inference, and test status. Its test plan defines decimal-byte fixtures at 50,000,000 through 4,000,000,000 bytes and requires the full upload → storage copy → persisted reference/restart → recipient DM copy → full download/checksum journey. Visibility of a chat message alone is not a pass.

## Remaining setup and scope boundary

For a later isolated integration run, provision a test bot/token in an approved secret store, numeric admin user ID(s), and a private test storage channel ID with the bot able to post. Also provide a recipient test account that opens the bot and presses Start, and a Premium uploader account for Premium-sized tests if required. Fixtures need exact byte counts and expected SHA-256 values; they can be supplied or generated for testing. A local Bot API comparison needs its own isolated deployment. An MTProto comparison also needs the appropriate API credentials and an approved account/session setup. Keep all credentials, account identifiers, and channel IDs out of Git and test logs.

Actual upload/delivery handlers, membership enforcement, administrator commands, share-link UI, live channel validation, and all media-transfer backends remain unimplemented. No production storage channel or recipient was tested. This is the end of Milestone 1; the next work should wait for your review.
