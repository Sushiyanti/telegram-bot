# M3 Implementation Report

**Status:** M3 implementation is complete. M1/M2 behavior is retained, the full automated suite passes, and the database initializes at schema version 4. No later milestone has been started. Live Telegram verification remains pending.

## Implemented

- Added migration `0004_uploaders_and_delivery.sql`: minimal `users` parent table; `uploaders` grants with actor/time/revocation fields; durable per-user redemption cooldown state; share-link `use_count` and optional `max_uses`, with database constraints/triggers.
- Added optional `UPLOADER_REDEEM_CODE`, `REQUIRED_CHANNEL_ID`, and `VERIFIED_TRANSFER_MAX_FILE_BYTES` settings. The last setting is an additional safety measure: because M1 requires verified end-to-end capability, the implementation does not treat `MAX_RELAY_FILE_BYTES` as proof that recipient copies work. Share creation and delivery remain fail-closed until a verified transfer limit is explicitly configured.
- Implemented the effective upload allowlist as admins ∪ active database uploader grants ∪ configured uploader IDs, while preserving the explicit `UPLOADER_MODE=all` behavior. M2 storage copies still use `protect_content=False`.
- Added `/redeem`, `/share`, `/start <token>`, and admin-only `/adduser` and `/revoke`. Redemption attempts are limited to one per 30 seconds per user and audited transactionally without storing or logging supplied codes. Share links persist only SHA-256 digests. Delivery checks expiry/revocation/use limits, item/reference validity, configured transfer capability, and optional membership before atomically consuming a use and copying stored messages. Album references are delivered in order, and recipient copies use `DEFAULT_PROTECT_CONTENT`.
- Updated schema/config/startup/upload tests, `.env.example`, and the README.

## Changed files

- `.env.example`
- `README.md`
- `docs/M3_IMPLEMENTATION_REPORT.md` (this report)
- `src/media_bot/application.py`
- `src/media_bot/auth.py`
- `src/media_bot/config.py`
- `src/media_bot/db/migrations/0004_uploaders_and_delivery.sql` (new)
- `src/media_bot/db/models.py`
- `src/media_bot/db/repositories.py`
- `src/media_bot/handlers/sharing.py` (new)
- `src/media_bot/handlers/uploads.py`
- `src/media_bot/services/core.py`
- `tests/test_config.py`
- `tests/test_database.py`
- `tests/test_diagnostics.py`
- `tests/test_m3_commands.py` (new)
- `tests/test_startup.py`
- `tests/test_uploads.py`

## Verification

Command run from `/tmp/telegram-bot-review`:

```text
$ ./.venv/bin/python -m pytest -q && ./.venv/bin/python -m compileall -q src tests && git diff --check
........................................................................ [ 57%]
......................................................                   [100%]
126 passed in 1.77s
```

Compilation and `git diff --check` completed successfully. The suite covers schema-v4 migration, redemption success/failure/cooldown, admin grant/revoke and denials, upload authorization after redemption, share URL/hash persistence, member/non-member and album delivery, expiry, use-limit exhaustion/races, config validation, and existing M1/M2 regressions.

**Schema version:** 4 (`PRAGMA user_version`), confirmed by tests against initialized databases.

## Live Telegram verification

- `/configcheck` PASS was previously reported as verified by the user; that result is not a new M3 test.
- `/share` → opening the link → live file delivery: **not run**.
- `/redeem` → live upload by the newly verified user: **not run**.
- Redacted M3 transcript: **none available**; no live transcript is claimed or fabricated. A no-value credential check found no `.env`, `BOT_TOKEN`, `STORAGE_CHANNEL_ID`, or `ADMIN_IDS` configured in this sandbox. The live tests also require a Telegram user to initiate the bot interaction and send an upload. `VERIFIED_TRANSFER_MAX_FILE_BYTES` must be set only after recipient-copy capability is verified.

## Scope and safety notes

The raw bearer token is returned only as the intended `/share` response; it is not persisted or logged. The previously supplied bot token is not reproduced or committed. No M3 live claim is made. Work stops at M3 until the user confirms live tests before any later milestone.
