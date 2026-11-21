# Telegram File-Sharing Bot — M6 Completion Handoff

**Purpose:** Resume M6 completion safely from this checkpoint. This is not a declaration that the entire bot or every M6 expectation is complete. Use the user-authored requests in the DeepSeek export as the specification; assistant-generated summaries and milestone completion claims are not requirements or proof.

## Repository checkpoint

- Workspace: `/tmp/telegram-bot-review`
- Branch: `main`
- Starting commit for this pass: `4355fa4` — `Complete M6 rule wizard and edge-case coverage`
- Schema version: 6; do not start M7.
- No live Telegram actions were performed. This sandbox has no provisioned bot credentials or test channel/user environment.
- This pass added no dependencies.
- This checkpoint is based on `4355fa4`. The handoff does not assume whether a later save or push has occurred; use `git status`, `git log`, and upstream state as authoritative before continuing.

## Authoritative user-authored product requests

The source is `/home/ubuntu/upload/deepseek_data-2026-11-21.zip`, specifically the USER-role turns in `chat_sessions/41f33959-4b1a-4d1b-b758-256eaed67ce9.json`. Relevant original user turns include:

- **Message #69 (2026-11-21 11:02Z):** The bot's main purpose is for admins and allowlisted staff to add/manage required channels, groups, and other actions before delivery through a generated file link. Admins should be able to enable/disable commands other than `/start` for regular users. Multiple storage channels/categories are needed.
- **Message #71 (2026-11-21 11:31Z):** Requirements can be category-specific and dynamically inherited through linked categories; admins should manage complex settings in an understandable low-clutter UI that edits a single message. For bot-start and reaction checks, the user proposed multiple configurable passing approaches, including real checks, waits, and link/prompt approaches. The REACT message-count bound and day bound must be configurable, and the “last X messages” bound must be disableable while a reaction can still be required.
- **Message #73 (2026-11-21 11:42Z):** Category deletion needs confirmation and a choice about removing or preserving requirement references that become orphaned. A large M5 implementation prompt is quoted in that turn; treat the quoted assistant prompt as context, not user-authored specification.
- **Message #85 (2026-11-21 12:36Z):** The command lock should affect regular users, not allowlisted staff; the user explicitly said a broader Django-like permissions manager was not necessary at that time.
- **Message #89 and #93:** The user objected to calling the panel finished when its UI/rule-testing work was incomplete and clarified that the work remained M6, not a new M7.
- **Current-session direct concerns:** `/admin` should create a new panel when explicitly resent; Settings felt incomplete; Add Category was missing; the user asked for a thoughtful completion/reassessment, not a superficial button patch.
- **Current-session checkpoint instruction:** If work remains, leave a controlled, resumable state and a file that lets another agent continue with the same mindset and source grounding.

The export's user-authored turns do **not** enumerate additional Settings toggles beyond the regular-user command/upload lock. Do not silently invent a permission matrix; the user previously deferred it. If broader Settings controls are expected, clarify them with the user or locate an exact user-authored request.

## Changes made in this checkpoint

1. **Panel lifecycle:** Every explicit `/admin` or `/panel` command now creates a fresh panel message and retires the previous one. Navigation clicks still edit the active panel in place. Replaced-message callbacks are rejected. If an edit fails, the bot sends a clearly labelled fresh panel and invalidates the old message instead of silently leaving it unusable.
2. **Add Category:** The Categories list now has an Add Category action. The panel collects a name, then a numeric storage chat ID or forwarded channel/chat message; it validates duplicate/reserved names, persists the category, audits the action, and returns to the category list in the panel. It warns that the bot needs administrator/post permission, but does not call Telegram to verify permissions.
3. **Pending-state timeout:** The five-minute timeout is now checked on private commands as well as callbacks and private non-command input. An expired flow is cleared and the panel returns home without consuming the unrelated command.
4. **Settings clarity and command policy:** Settings now explains who the regular-user lock affects. When locked, regular users are limited to `/start`; `/redeem` is locked too. Environment/database admins and active uploaders remain exempt. This follows the user’s “commands other than start” and staff-bypass requests.
5. **Rule semantics:** `TIMED_WAIT` no longer presents a fake Real/Fake choice; it is explicitly wait-only. A REACT message bound of zero now means “any observed post,” but still requires a reaction. The optional day cutoff remains active, and zero days means no age cutoff. The wizard and confirmation text describe this accurately.
6. **Category deletion disclosure:** Confirmation now explains that items move to Default, links to the deleted category are removed while other category records remain, and orphaned requirements can be kept or deleted.
7. **Docs/tests:** M5/M6 guides were corrected, and regression coverage was added for category creation/navigation/deletion, fresh/reopened panels, stale callbacks, edit-failure recovery, timeout-on-command, Settings behavior, lock bypass, and REACT/TIMED_WAIT semantics.

## Validation already run

- Focused tests: `uv run --locked --group dev pytest -q tests/test_m6_panel.py tests/test_m5_rules.py` — **77 passed**.
- Full suite: `uv run --locked --group dev pytest -q` — **217 passed**.
- `uv run --locked python -m compileall -q src tests` — passed.
- `git diff --check` — passed.
- Re-run all checks after any further edits. Confirm `PRAGMA user_version == 6` if database/migration files are touched.

## Preserved decisions and scope boundaries

- M6 is still the scope. Do not begin M7.
- The panel rule wizard requires no typed rule syntax. Numeric IDs/forwarded messages and category names are input, as intended. The legacy M5 `/addrequirement` command still accepts typed rule syntax for compatibility; it is not the M6 panel wizard.
- Real `START_BOT` verification remains unsupported without an attestation/integration bridge. The UI offers wait-only/fake behavior and does not claim to prove another bot was started.
- Existing M5 behavior for a real JOIN_CHAT Telegram API error is retained: it falls back to the configured wait and passes. Existing M5 docs/tests encode this, and the user's history explicitly allows “cheating”/wait approaches. Do not change this policy silently; if tightening it, ask the user and update the user-facing configuration semantics.
- Real REACT is limited to observed posts/reaction updates while the bot has the required admin/update access; there is no historical backfill. The existing documented fallback to wait when bot admin/API access is unavailable remains.
- Category creation does not verify that the bot can post in the selected storage channel. The panel warns the admin; M5 documentation already states the operator must grant permissions.
- UI navigation and Add flows remain inside the current panel message. An explicit `/admin` command intentionally opens a fresh message; an edit failure is the exceptional case where a replacement message is sent.

## Remaining work / questions for the next agent

### M6 completion checkpoint

1. Inspect `git status`, rerun the validation commands above, and verify the handoff accurately matches the working tree.
2. Decide with the user whether the one-tap category/storage flow should perform a live bot-permission check (or keep the current warning-only behavior).
3. The user says Settings is incomplete, but the export only explicitly specifies the command/upload lock and explicitly defers broader permissions. Do not fabricate more settings. Ask what additional controls they expect if the current explanation/toggle is insufficient.
4. If the user expects all M6 flows to be live-tested, request/provision a safe Telegram test environment; no live verification has been done in this pass.

### Product gaps visible from the original purpose (do not silently expand M6)

- **Staff management scope is ambiguous:** Message #69 describes admins and allowlisted staff as managing required channels/groups, while the M6 panel request and current implementation make `/admin` admin-only. Clarify whether uploaders should have any category/requirement read or management access; do not grant broader staff authority solely from this ambiguity.
- For private required channels/groups, the current schema/UI stores the numeric target but does not store or show a user-facing invite URL. Real JOIN denial can show a public `@username`; private targets may only produce a generic “required chat” prompt. The user’s bot purpose is advertising/join conversion, so ask whether each JOIN rule needs a separate invite URL and how it should be validated/displayed.
- The export describes alternate wait/link/prompt passing approaches, but the current Fake JOIN_CHAT behavior is wait-only and does not display a target link or prompt. Clarify whether the desired Fake mode should include an optional URL/button, be configurable per requirement, or remain wait-only.
- The user-authored source reviewed here did not establish a share-link expiry control. Do not rely on assistant-authored summaries as authorization to add one; locate an original user request or ask.
- Album DB persistence is all-or-nothing, but already-copied Telegram storage messages cannot be rolled back if a later copy/DB step fails; investigate cleanup/compensation behavior if continuing broader product-hardening work.

## Key files

- `src/media_bot/handlers/panel.py` — panel lifecycle, category creation, staff/settings, rule wizard, callbacks and timeouts.
- `src/media_bot/application.py` — command-time timeout middleware and Telegram handler order.
- `src/media_bot/handlers/policy.py` — regular-user lock with staff bypass.
- `src/media_bot/db/rules.py` — category/settings/reaction repositories and REACT window semantics.
- `src/media_bot/services/rule_engine.py` — JOIN_CHAT/TIMED_WAIT/START_BOT/REACT runtime checks.
- `tests/test_m6_panel.py`, `tests/test_m5_rules.py` — focused regression and `/start <token>` coverage.
- `docs/M6_ADMIN_PANEL.md`, `docs/M5_CATEGORIES_AND_REQUIREMENTS.md` — user/operator-facing behavior.
- Original export: `/home/ubuntu/upload/deepseek_data-2026-11-21.zip`; supplemental user text: `/home/ubuntu/upload/pasted_content.txt` and `/home/ubuntu/upload/pasted_content_2.txt`.
