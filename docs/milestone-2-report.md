# Milestone 2 report — private-DM upload intake

**Status:** Implemented and verified with mocks; stopped before M3.

## Delivered

The application now registers a private-chat media handler for documents, photos, videos, audio, voice messages, animations, and video notes. `UPLOADER_MODE` supports `admin` (the default), `allowlist`, and `all`; `UPLOADER_IDS` is validated and used only for allowlist mode. Production startup warns when all private users are permitted. Denied upload attempts receive a short refusal and an `upload_denied` audit event.

Before storage, the handler rejects unknown size, size over `MAX_RELAY_FILE_BYTES`, and protected source messages. It uses one `copyMessage` for a single message and one `copyMessages` call for an album after a 1.5-second quiet period. It never calls `getFile`, downloads or re-uploads bytes, or uses source-provided file size outside Telegram message metadata. Duplicate `(uploader_id, file_unique_id)` values are rejected before a subsequent copy.

After complete copy success, the code atomically persists one item, per-file source metadata and dedupe fingerprints, storage-message references, uploader and byte counts; it leaves `sha256` null and clears the M2 pending row in the same transaction. Album confirmation includes item ID, number of messages, and total bytes; single confirmation includes item ID and size. Replies and logs do not include Telegram file IDs or storage-message IDs. Copy, partial-copy, or persistence failures produce a sanitized structured error log, `storage_copy_failed` audit event, failure reply, and pending-row cleanup; item and file metadata are not committed. Telegram cannot roll back copies already accepted by the API, so a partial-copy or later database failure can leave unreferenced storage messages.

The application lifecycle starts recovery after initialization and cancels debounce tasks at shutdown. Recovery filters on the M2 pipeline marker, leaving legacy pending rows untouched. Recovery uses the recorded source chat ID. Idle per-group locks and expired invalid-album markers are cleaned up.

## Schema and compatibility

Migration `0003_upload_metadata.sql` advances `user_version` from 2 to 3. It adds nullable `items.sha256`, per-file metadata, an uploader/file-unique-ID fingerprint mapping, and a `pipeline` marker on pending uploads. This schema change is necessary because one album is one logical item but contains multiple source files, and the accepted M1 schema does not provide per-file metadata or a durable uploader-scoped dedupe index. Existing pending records are marked `legacy` by the migration and are excluded from M2 recovery.

M1 behavior is retained, including the user-selected non-expiring share-link default. The two mocked `/configcheck` assertions that hardcoded schema version 2 were updated to expect version 3; no TTL change was made.

`DEFAULT_PROTECT_CONTENT` remains stored on item metadata as a future recipient-copy preference. M2 explicitly passes `protect_content=False` to both storage-copy methods; the setting does not affect storage copies. Its recipient-facing meaning must be decided and tested before M3.

## Validation

The original M1 baseline had 56 passing tests. The combined M1+M2 suite now has **90 tests, all passing**; the only expectation change in the existing diagnostic tests is the intentional schema-version advance. The full verbose transcript is preserved in [the M2 test output](m2-test-output.txt).

Final checks passed:

- `uv run --locked --group dev pytest -vv` — **90 passed in 1.41s**.
- `uv lock --check` — passed.
- `uv build` — source distribution and wheel built successfully.
- `python -m compileall -q src tests` — passed.
- `git diff --check` — passed.
- Wheel inspection confirmed the app entry point, upload handler, and migrations 0001–0003 are packaged.
- Credential scan found no previously supplied token string or token-shaped credential in the working tree. `.env` is Git-ignored; `.env.example` contains placeholders only.

## Live integration limitation

**No live `/configcheck` PASS output or live Telegram upload was generated.** Although a bot token had been offered in the conversation, no test private storage channel and authorized test uploader were provisioned. No Telegram API calls were made during this M2 validation, and no transcript is fabricated. The following requested live examples remain unrun: one successful single upload, resending the same file for dedupe, an oversized-file rejection, and a three-part album. Their analogous behavior is covered by mocked tests, including single success, duplicate resend, size rejection, album copy, and partial-copy failure. Repeat those checks and capture redacted `/configcheck` output when a test bot, private channel, and uploader are available.

## Changed files

- `.env.example`, `README.md`
- `src/media_bot/application.py`, `src/media_bot/auth.py`, `src/media_bot/config.py`
- `src/media_bot/db/audit_repository.py`, `src/media_bot/db/models.py`, `src/media_bot/db/repositories.py`, `src/media_bot/db/migrations/0003_upload_metadata.sql`
- `src/media_bot/handlers/uploads.py`, `src/media_bot/services/core.py`
- `tests/test_config.py`, `tests/test_database.py`, `tests/test_diagnostics.py`, `tests/test_startup.py`, `tests/test_uploads.py`
- `docs/decision-log.md`, `docs/m2-test-output.txt`, `docs/milestone-2-report.md`

No M3 functionality—delivery, share links, membership checks, required-chat enforcement, admin-management commands, webhooks, or Docker—was implemented.
