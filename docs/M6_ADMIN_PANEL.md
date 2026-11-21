# M6 Admin Roles and Inline Control Panel

Migration `0006_admins.sql` creates the durable `admins` role table. At application startup, validated `ADMIN_IDS` from the environment are inserted idempotently into that table. Existing database-only admins are preserved. Authorization checks the environment IDs first (the protected super-admin fallback), then checks the database role registry. If the database lookup fails, configured environment admins remain authorized.

## Open the panel

An administrator can send `/admin` or `/panel` in a private chat. The bot sends one control-panel message. Categories, requirements, staff lists, confirmations, settings, and Add-flow responses all replace the text and inline keyboard of that exact message. The panel binds callbacks to its owner, private chat, message ID, and a per-panel nonce; callbacks from another user or an old message are rejected.

The main menu opens:

- **Categories:** paginated category list, category details, parent/child links, and deletion with an explicit choice about orphaned requirements. `Default` is protected from deletion.
- **Requirements:** global and category scopes, rules directly attached to each scope, rule creation, and removal of a scope association. If a rule is associated with another scope, that association is retained.
- **Staff:** DB/environment administrators and active uploaders, with pagination, Add, and Remove actions. Environment administrators cannot be removed. A user with admin status cannot have uploader access revoked from this UI.
- **Settings:** lock/unlock regular-user commands and uploads, and close the panel.

## Add-flow state machine

The panel stores one small state record in the administrator's `context.user_data`: panel owner, nonce, chat/message IDs, current view, pagination/scope, and an optional pending input action. Choosing **Add** edits the panel to prompt for a numeric Telegram user ID or a forwarded user message. The next eligible private message is parsed and persisted, and then the bot edits the existing panel back to the relevant staff list with a result notice. Rule creation similarly consumes one formatted rule input. Cancel clears the pending state and returns to the prior view. Pending input is stopped from reaching upload intake; ordinary private messages without a pending panel flow continue through normal handlers.

## Command and upload lockdown

When `ENABLE_USER_COMMANDS=false`, regular users are blocked from commands except `/start` and `/redeem`, as well as from uploading media. Environment admins, database admins, and active uploaders bypass the lock. The lock does not grant upload permission by itself; normal uploader authorization still applies to upload processing.

## Rule input format

To add a requirement in the panel, send:

```text
TYPE TARGET IS_FAKE WAIT_SECONDS
```

Supported types are `JOIN_CHAT`, `REACT`, `START_BOT`, and `TIMED_WAIT`. Use `-` for a missing target, such as `TIMED_WAIT - false 60`. A `REACT` rule can optionally include a message/day window:

```text
REACT - true 0 LAST_X_MESSAGES LAST_X_DAYS
```

The repository validates all values and scope references. Invalid input leaves the flow pending and updates the prompt in the same panel message.

M6 UI actions and role changes are audited where applicable. Automated tests mock Telegram calls; no live Telegram API interaction is performed by the test suite.
