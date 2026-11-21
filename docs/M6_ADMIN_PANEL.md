# M6 Admin Roles and Inline Control Panel

Migration `0006_admins.sql` creates the durable `admins` role table. At application startup, validated `ADMIN_IDS` from the environment are inserted idempotently into that table. Existing database-only admins are preserved. Authorization checks environment IDs first (the protected super-admin fallback), then the database role registry. If the database lookup fails, configured environment admins remain authorized.

## Open the panel

An administrator can send `/admin` or `/panel` in a private chat. The bot sends one control-panel message. Categories, requirements, staff lists, confirmations, settings, and Add-flow responses replace the text and inline keyboard of that exact message. The panel binds callbacks to its owner, private chat, message ID, and a per-panel nonce; callbacks from another user or an old message are rejected.

The main menu provides **Categories**, **Requirements**, **Staff**, and **Settings**:

- **Categories:** paginated category list, category details, parent/child links, and deletion with an explicit choice about orphaned requirements. `Default` is protected from deletion.
- **Requirements:** global and category scopes, rules directly attached to each scope, and a button-driven Add Rule wizard. When a rule is shared across scopes, removal offers **Unlink Here** or **Delete Everywhere**; a rule used by only one scope is removed directly.
- **Staff:** separate **Add Admin** and **Add Uploader** actions, lists of database/environment administrators and active uploaders, and removal controls. Environment super-admins cannot be revoked. An admin's uploader access cannot be revoked from this UI.
- **Settings:** lock/unlock regular-user commands and uploads, and close the panel.

## Rule creation wizard

Rule creation uses inline buttons for the requirement type (`JOIN_CHAT`, `REACT`, `START_BOT`, or `TIMED_WAIT`), Real/Fake verification (where supported), wait time, REACT windows, and final confirmation. `TIMED_WAIT` skips target selection. For `JOIN_CHAT` and `REACT`, forward a message from the target channel or paste its numeric chat ID; for `START_BOT`, forward a user's message or paste the numeric user ID. Typed usernames and rule syntax are not accepted. Every wizard screen has **Cancel**.

Wait choices are 0, 1, 3, 5, 10, 30, and 60 seconds. REACT windows are chosen with buttons for the observed-message count and age in days; zero disables that limit. The configured real REACT check passes when the user has at least one tracked reaction in the selected window. The bot must have channel-admin access and have observed the post/reaction updates; unavailable admin access follows the existing wait fallback. Real `START_BOT` verification is unsupported without a separate attestation bridge, so the wizard only offers Fake/wait mode for that type.

When **Real** is selected for `JOIN_CHAT`, the panel checks whether the bot is an administrator in the target chat. If not, it immediately shows: “⚠️ Bot is not admin in this channel. Real checks will fail.” The admin can continue or switch to Fake.

## Add-flow state machine and timeout

The panel stores a small state record in the administrator's `context.user_data`: panel owner, nonce, chat/message IDs, current view, pagination/scope, and any pending input or rule-wizard data. Staff flows accept a numeric user ID or a forwarded user message. Rule target prompts accept a numeric ID or a forwarded channel/user message. Those are the only panel text-input cases; formatted rule syntax is not accepted.

Pending staff or wizard state expires after five minutes of inactivity. On the next interaction, expired state is cleared and the panel resets to its Main Menu; an expired text message is not consumed as wizard input. Completing or cancelling a flow also clears its pending state. Valid clicks and prompt/results continue to edit the original panel message rather than sending new messages.

## Command and upload lockdown

When `ENABLE_USER_COMMANDS=false`, regular users are blocked from commands except `/start` and `/redeem`, as well as from uploading media. Environment admins, database admins, and active uploaders bypass the lock. The lock does not grant upload permission by itself; normal uploader authorization still applies to upload processing.

## Verification and tests

M6 tests mock Telegram calls; no live Telegram API interaction is performed by the test suite. The M5 rules engine is exercised through mocked `/start <token>` flows covering fake JOIN_CHAT wait, real JOIN_CHAT refusal without consuming a share-link use, zero-second fake TIMED_WAIT, and real REACT with and without a stored reaction.
