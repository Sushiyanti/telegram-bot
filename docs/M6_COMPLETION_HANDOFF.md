# Telegram File-Sharing Bot — M6 Completion Handoff

**Status:** M6 was explicitly accepted by the user on 2026-11-23 based on the mocked validation summarized below. This acceptance does **not** constitute live Telegram validation; real API behavior and end-to-end transfer sizes remain unverified.

## Repository and continuation state

- Repository: `/tmp/telegram-bot-review`
- Branch: `main`
- M6 implementation commits are pushed through `e5c62da634db6a6d23ca373391175aa074d638d1`; check current Git status, `HEAD`, and `origin/main` before continuing.
- Schema remains version 6. The changes below use the existing `bot_settings` table; no migration or dependency was added.
- No live Telegram action was performed; no designated test channels/users or authorized test credentials are present.
- Latest validation after the JOIN wizard fix: focused `tests/test_m5_rules.py tests/test_m6_panel.py` — **96 passed**; full `uv run --locked --group dev pytest -q` — **236 passed**; compileall and `git diff --check` passed. A temporary DB reported `PRAGMA user_version=6`; no `REAL_CHECK_FAILURE_POLICY` row is seeded, so the code default is fail-closed.
- The previously intermittent M2 partial-album test failure was caused by checking pending-row cleanup after a fixed 40 ms sleep while its async failure handler was still running. The test now awaits its settle task; it passed **10/10** isolated repetitions. Upload implementation was not changed.

## User-confirmed decisions

These resolve the prior open product questions; do not ask the user again unless code inspection reveals a materially different ambiguity.

1. Keep `/addrequirement` for backward compatibility. Zero rule-syntax typing applies inside the panel wizard only.
2. Keep the M6 panel admin-only. A Django-like dynamic permission manager is a future idea, not required now.
3. Make the Real JOIN_CHAT/REACT unavailable-check policy configurable by admins. Prefer and default to fail closed: deny before share-use consumption. Keep the configured-wait fallback as an option; it is not proof of a Real check.
4. On confirmed category deletion, revoke share links for items assigned to that category while retaining media/items and storage messages, reassigning items to `Default`, removing graph edges to the deleted category, and preserving linked categories/shared storage channels. Keep the existing explicit choice to keep or delete orphaned requirements.
5. Keep Settings suitable for future expansion. M6 baseline controls are the regular-user command/upload lock and Real-check failure policy; do not invent unrelated settings or a broad permission matrix.

## Accepted M6 implementation

### Real-check failure policy

`BotSettingsRepository` stores `REAL_CHECK_FAILURE_POLICY` in the existing key/value table. An absent or invalid setting, or a setting-read error during check handling, resolves fail-closed. Admins can switch between `fail_closed` and `wait_fallback` from the Settings panel; changes are audited. Real JOIN_CHAT API errors and Real REACT API/admin/state lookup failures use the selected policy. A confirmed user denial (not a member/no qualifying reaction) remains a denial. With fail-closed, `RequirementEvaluator.verify` returns false before `start_command` calls `ShareLinkService.consume`, so the share use remains available. With `wait_fallback`, the rule's configured wait is applied and delivery may proceed, consuming the usual use. Recipient wording does not claim the wait proves verification. The JOIN wizard also distinguishes a confirmed non-admin lookup from an unavailable Telegram lookup.

### Category deletion

`CategoryRepository.delete` now revokes active `share_links` rows for items whose `category_id` is the category being removed, inside the same transaction as item reassignment and category/link deletion. It does not delete item/media rows or Telegram storage messages and does not delete a storage channel. Other linked categories survive, while graph edges attached to the deleted category are removed. Both panel and legacy-command confirmation/result text warn that links for the category's items will stop working and explain that media and linked categories remain. The orphaned-requirement choice remains explicit.

### Regression coverage and docs

- `tests/test_m5_rules.py` exercises fail-closed default and wait fallback for Real JOIN_CHAT and REACT unavailable/error paths, including a confirmed bot-non-admin REACT state; it checks delivery and durable share-use counts.
- Category tests assert revoked links for removed-category items, unaffected links for another category sharing the same storage channel, retained/reassigned items and storage messages, and surviving linked categories.
- `tests/test_m6_panel.py` covers Settings policy selection and category-delete warning text.
- `docs/M6_ADMIN_PANEL.md`, `docs/M5_CATEGORIES_AND_REQUIREMENTS.md`, and `docs/decision-log.md` describe the implemented behavior.

## M6 validation boundary and transition

- M6 is **accepted based on mocked validation**. All automated Telegram calls are mocked; no live Telegram validation has been performed.
- The scoped source/test audit found no other concrete implementation gap after the JOIN wizard lookup distinction was added. Acceptance is not a claim of bug-free or live-verified behavior.
- Category creation still warns about posting rights without a live preflight check; do not test against production channels without authorization.
- Large-file end-to-end guarantees remain unverified. Optional per-link expiry UI remains outside M6; preserve non-expiring links by default.
- M7 begins with a scoped plan for isolated end-to-end transfer readiness and large-file capability verification. No transport change or file-size claim is authorized by this handoff alone; see `docs/M7_PLAN.md`.
- Continue to report mocked and live evidence separately. Require a dedicated non-production Telegram environment before live integration tests.
