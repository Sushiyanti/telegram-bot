# Decision log

Entries are ordered by milestone and record the choices that constrain implementation.

## M1 carry-forward

- **Share-link expiry:** retain the non-expiring default. The proposed seven-day default remains deferred; links may still be explicitly expired or revoked through the existing model.
- **Audit history:** denied admin commands are persisted in the append-only audit log introduced by migration 0002.

## M2 — private-DM upload intake (2026-11-20)

- **Uploader policy:** default to `UPLOADER_MODE=admin`; permit the `allowlist` and `all` modes only when configured. `UPLOADER_MODE=all` with `ENV=prod` warns at startup.
- **Storage transport:** use only Bot API `copyMessage` for one source message and `copyMessages` once for a media group. Do not download or re-upload file bytes; do not call `getFile`.
- **Size and privacy gates:** use Telegram message metadata only. Reject a missing size, a size above `MAX_RELAY_FILE_BYTES`, and a protected source message before calling a copy method.
- **Album collection:** key the debounce by `(chat_id, media_group_id)`, use a 1.5-second quiet period, and persist in-flight M2 messages in the existing pending-upload tables. Recover only rows marked as M2 so legacy pending records are not processed by the new handler.
- **Item shape and dedupe:** store one logical item per single upload or album, all source-file metadata and storage-message references, aggregate album byte size, and `sha256=NULL`. Enforce dedupe by `(uploader_id, file_unique_id)` across all files in an item. Migration 0003 adds per-file metadata and the durable fingerprint mapping because the accepted M1 `items` table did not contain these fields and an album may contain multiple files.
- **Partial copy behavior:** require the number of returned copy references to equal the number of source messages. If copying or database persistence fails, do not commit the item; log and audit the failure and clear the pending M2 row. Telegram may have copied some messages before a partial result or a later database failure; no compensation/delete method is in scope, so such channel messages can be unreferenced orphans.
- **`DEFAULT_PROTECT_CONTENT`:** retain the configured value on item metadata as a future recipient-copy preference. Do not apply it to storage copies in M2; both storage-copy calls explicitly pass `protect_content=False`. Decide and test recipient-facing semantics before M3.
- **Validation boundary:** all automated Telegram operations are mocked. A live `/configcheck` PASS and live upload transcript require a provisioned test bot, private storage channel, and authorized test uploader. None was available for this M2 run, so no live Telegram calls were made.
