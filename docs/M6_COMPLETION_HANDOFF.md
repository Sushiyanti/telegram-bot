# Telegram File-Sharing Bot — M6 Completion Handoff

**Purpose:** Continue the M6 completion review from the pushed checkpoint below. This document is not a declaration that M6 or the entire bot is complete. Use the original user-authored requests as the specification; do not begin M7.

## Repository checkpoint

- Workspace: `/tmp/telegram-bot-review`
- Branch: `main`
- Pushed base for this continuation: `84eb314` (`Checkpoint M6 completion review`). This handoff describes the subsequent M6 changes in this continuation; consult `git log` and `git status` for the latest saved/pushed state.
- Database schema remains version 6. No migration or dependency was added.
- No live Telegram action was performed. This environment has no provisioned bot credentials or designated test channels/users.

## Authoritative user-authored requests

The primary source is `/home/ubuntu/upload/deepseek_data-2026-11-21.zip`, especially USER turns #69, #71, #73, #79, #85, #89, #91, and #93. The supplemental wizard requirements are in `/home/ubuntu/upload/pasted_content.txt`; the category-deletion request is in `/home/ubuntu/upload/pasted_content_2.txt`.

- **USER #69:** The bot is for sharing requested files through generated links while promoting required channels/groups/actions. Regular users may be restricted to `/start`; admins and allowlisted staff should bypass that lock. Multiple storage categories are needed.
- **USER #71:** Requirements may be scoped/inherited by category. The panel should be understandable, low-clutter, and single-message. Passing methods should be configurable, including real checks, waits, and prompt/link-style flows for channels/groups and bot-start/reaction requirements. REACT message and day bounds must be configurable and independently disableable. The user also stated that Fake-style behavior should not be exposed as such to recipients.
- **USER #73:** Category deletion needs confirmation and a choice to keep or delete orphaned requirements.
- **USER #79:** Non-real/“cheating” passing approaches should remain configurable.
- **USER #85:** Allowlisted users are staff and bypass the regular-user lock; a broad Django-like permissions manager was not necessary then.
- **USER #89/#91/#93:** The implementation was not to be called complete just because the milestone had previously been accepted; the unfinished work belongs to M6, not a new M7.
- **USER #13:** Keep share links non-expiring by default while allowing optional per-link expiry. The data model/service support optional expiry, but `/share` currently exposes only the non-expiring default. Keep that UI gap outside M6 unless the user expands scope; do not change the default.

The export does not define a broader Settings/permissions matrix. Do not invent one. If additional controls are expected beyond the documented command/upload lock, ask for specifics. There is also a tension between #69's mention of allowlisted staff managing requirements and the admin-only panel request; do not expand uploader privileges without clarification.

## Work completed in this continuation

1. Added an end-to-end panel test for **Add Uploader**: selecting the uploader role, submitting a numeric ID, persisting the active grant, refreshing the uploader list in the same panel message, and ensuring no additional bot message is sent.
2. Added a lockdown regression test for a **database-backed, non-environment admin**, exercising both command and media bypass while the regular-user lock is on.
3. Implemented the user-requested optional **prompt/link path** using the existing `requirements.settings_json` field; no schema change. Public channel/group/user usernames from forwarded messages can prefill a Telegram URL. For private targets, an admin can paste an optional invite URL. Only HTTPS links on `t.me` or `telegram.me` are accepted.
4. Fake JOIN_CHAT, REACT, and START_BOT rules now show a generic recipient-facing prompt and optional URL button, then pass immediately or after the configured wait. Real JOIN/REACT denials also show the configured link button when available. Link clicks are not monitored or treated as proof of membership, bot-start, or reaction. Recipient-facing wait text is neutral (“Please wait...”); it does not claim a check is happening. The admin wizard describes the Real versus prompt/wait behavior explicitly.
5. Forwarded `MessageOriginChat` group targets are now accepted for channel/group rules as well as forwarded channels; public usernames can prefill the link.
6. Matched the button choices to the supplemental specification: waits `0, 3, 5, 10, 30, 60`; REACT post bounds `0, 3, 5, 10`; age bounds `0, 7, 30`. Zero bounds disable those respective limits, not the reaction requirement.
7. Corrected the handoff and decision log to cite USER #13 accurately, and updated the M5/M6 guides with link/prompt semantics and wait behavior.

## Validation completed

- `uv run --locked --group dev pytest -q` — **229 passed**.
- `uv run --locked python -m compileall -q src tests` — passed.
- `git diff --check` — passed.
- Direct SQLite check — `PRAGMA user_version=6`.
- Ruff is not configured in `pyproject.toml`; no Ruff check was run.
- Automated Telegram calls are mocked. **No live Telegram testing** was performed.

## Preserved behavior and scope boundaries

- M6 remains the scope; do not start M7.
- Admin-panel navigation and input responses edit the same active panel message. An explicit `/admin` or `/panel` command intentionally opens a new panel; exceptional edit failure may send a replacement panel.
- Rule syntax is not typed in the panel. Numeric IDs/forwarded messages, category names, and the optional Telegram URL are the accepted inputs.
- Real `START_BOT` verification remains unsupported without a separate attestation bridge; only prompt/wait behavior is offered.
- Real JOIN_CHAT and REACT API/admin failures retain the existing configured-wait fallback. A real JOIN denial does not consume the share-link use.
- Real REACT can only use posts/reaction updates observed by the running bot; there is no historical backfill.
- Category creation warns that the bot needs posting rights but does not verify them live.
- Optional share-link expiry UI remains outside this continuation. `/share` keeps the existing unlimited/non-expiring default.

## Key files

- `src/media_bot/handlers/panel.py` — single-message panel, rule wizard, input validation, staff flows, and timeouts.
- `src/media_bot/db/rules.py` — requirements/settings persistence and Telegram URL validation.
- `src/media_bot/services/rule_engine.py` — real checks, neutral wait messaging, prompt/link buttons, and Fake wait/prompt behavior.
- `src/media_bot/handlers/policy.py` — regular-user lockdown with admin/uploader bypass.
- `tests/test_m6_panel.py`, `tests/test_m5_rules.py` — panel and mocked `/start <token>` acceptance coverage.
- `docs/M6_ADMIN_PANEL.md`, `docs/M5_CATEGORIES_AND_REQUIREMENTS.md`, `docs/decision-log.md` — behavior and decision documentation.

## Status / next step

The listed automated checks pass, and the continuation closes the two identified test gaps and the prompt/link product-flow gap. **Do not declare M6 complete solely from this handoff or the test count.** Review the current Git status/history, preserve the checkpoint, obtain any needed live Telegram validation on the user's side, and wait for explicit user acceptance before treating M6 as complete. Do not start M7.
