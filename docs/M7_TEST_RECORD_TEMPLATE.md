# M7 Transfer Test Record

> Copy this template for each isolated test run. Do not put bot tokens, account credentials, raw Telegram IDs, private invite links, bearer share tokens, or sensitive fixture contents in this file. Use redacted environment labels and sanitized diagnostics. A test is **PASS** only after the complete recipient download and checksum comparison.

## Run metadata

- Run label:
- Date/time (UTC):
- Operator:
- Non-production environment confirmed: yes / no
- Bot/API route and version:
- Uploader account class (ordinary/Premium/other):
- Recipient test account label (redacted):
- Storage chat label (redacted):
- Application commit:
- Configured `VERIFIED_TRANSFER_MAX_FILE_BYTES` during test:

## Per-size results

| Target bytes | Observed source bytes | Upload update | Storage copy | DB reference after restart | Share delivery | Recipient full download | Observed recipient bytes | Source SHA-256 | Recipient SHA-256 | Elapsed | Result |
|---:|---:|---|---|---|---|---|---:|---|---|---:|---|
| 50,000,000 | | | | | | | | | | | NOT RUN |
| 100,000,000 | | | | | | | | | | | NOT RUN |
| 500,000,000 | | | | | | | | | | | NOT RUN |
| 1,000,000,000 | | | | | | | | | | | NOT RUN |
| 2,000,000,000 | | | | | | | | | | | NOT RUN |
| 3,000,000,000 | | | | | | | | | | | NOT RUN |
| 4,000,000,000 | | | | | | | | | | | NOT RUN |

Use `PASS`, `FAIL`, or `NOT RUN` in the result column. Record exact byte counts; “message appeared” or “copy succeeded” alone is not a pass. If any stage fails, include only redacted error class/message below and preserve no bearer token or raw identifiers.

## Redacted diagnostics and deviations

- Sanitized error class/message:
- Retries/timeouts:
- Storage and bandwidth constraints:
- Deviations from `docs/large-file-feasibility.md`:
- Evidence artifact locations (access-controlled; no public URLs):

## Capability decision

- Largest size with a complete PASS:
- Proposed `VERIFIED_TRANSFER_MAX_FILE_BYTES` (or leave unset):
- Rationale and test-condition limits:
- Reviewer/approval:

Leave the setting unset if no size passes. A failed or unrun larger size does not invalidate smaller complete passes, but it must not be represented as supported. Re-test after changing the Bot API route, account class, storage configuration, or delivery path.
