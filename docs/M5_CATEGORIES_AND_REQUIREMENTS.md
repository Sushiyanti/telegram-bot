# M5 categories and dynamic requirements

M5 adds category-specific storage destinations and requirements checked before a share link is consumed. The database schema is version 5. Migration `0005_dynamic_rules_categories.sql` creates the protected `Default` category, assigns existing items to it, and backfills each item's original storage-channel ID from its message references. Existing references are retained even when an item's category is later deleted.

## Admin commands

All commands below are private-chat, admin-only commands and use the existing authorization and audit handling.

- `/addcategory <name> <channel_id>` creates a category and selects the numeric Telegram channel ID where new uploads in that category are stored. Names are supplied as one command argument; `Default` and `Global` are reserved.
- `/linkcategory <parent_name> <child_name>` makes the child category's requirements apply to items in the parent category as well. Links can be nested, but cycles are rejected.
- `/addrequirement <category_name|global> <type> <target> <is_fake> <wait_seconds> [last_x_messages last_x_days]` adds a rule to one category or globally. Supported types are `JOIN_CHAT`, `REACT`, `START_BOT`, and `TIMED_WAIT`. `JOIN_CHAT` and `REACT` require a numeric chat ID; fake `START_BOT` accepts an `@username`; `TIMED_WAIT` uses `-` as its target. `is_fake` must be `true` or `false`; waits are bounded to 0–86,400 seconds. The two optional window arguments are REACT-only; message counts are bounded to 0–1,000 and days to 0–36,500.
- `/delcategory <name>` presents an inline confirmation to cancel, delete requirements that become orphaned, or keep them. Items and pending uploads are reassigned to `Default`. `Default` itself cannot be deleted.
- `/togglecommands` flips and persists the regular-user command/upload lock. When locked, non-admins may use only `/start` and `/redeem`; other commands and media uploads are stopped. Admins bypass the lock.

The `Default` category is created during initialization and receives the configured `STORAGE_CHANNEL_ID` if it does not already have a storage destination. If it is the only category, uploads continue to use it automatically. With multiple categories, the uploader must choose a destination from an inline keyboard before the bot copies anything. The pending upload and selection state survive restarts. Selection callbacks are bounded in size and accepted only from the user who submitted the pending upload.

The bot validates that each category has a non-zero numeric channel ID, but it does not verify channel membership or posting rights when a category is created. Add the bot as an administrator with posting permission in every configured storage channel before using that category.

## Rule behavior and Telegram limitations

Global rules apply to every category. A category inherits rules from all descendants linked beneath it, in addition to its own rules and global rules. A denied requirement leaves the share-link use count unchanged. Fake requirements skip the Telegram check, send `Verifying...`, wait for `wait_seconds`, and pass.

Real `START_BOT` verification is not supported by the Bot API: a bot cannot query whether someone started another bot. Creation of a non-fake `START_BOT` rule is rejected with `Real START_BOT verification is not supported yet, please use is_fake=True`. The [Telegram Bot FAQ](https://core.telegram.org/bots/faq) describes the visibility limits that prevent this check.

Real `JOIN_CHAT` rules use `getChatMember`. A user who is not a member is denied with a join instruction. If the bot cannot perform the check or Telegram returns an API error, verification falls back to the configured wait and passes after the wait.

Real `REACT` rules default to at least one reaction on one of the latest five eligible posts (`last_x_messages=5`, `last_x_days=0`). For example, append `10 30` to require a reaction on one of the latest 10 eligible posts published within the last 30 days. `last_x_days=0` disables the age cutoff, and a message limit of zero disables the reaction condition. The bot stores observed channel posts and identifiable users' current reactions, and replaces/removes those records as reaction updates arrive. Verification requires the bot to be an administrator and requires `message_reaction` updates to be requested. If the bot is not an administrator or the admin/API check fails, the configured wait is used instead. If the check is available but the user has no qualifying reaction, delivery is denied.

Reaction state is not backfilled. The Bot API does not provide a general method to retrieve historical channel posts and per-user reaction state, so only posts and reaction changes observed by the running bot are available for verification. Have the bot as an administrator and receiving updates before relying on reaction checks. See the [Bot API](https://core.telegram.org/bots/api) and [python-telegram-bot Update documentation](https://docs.python-telegram-bot.org/en/v22.8/telegram.update.html) for the admin and allowed-update requirements.

No live Telegram checks are part of the automated suite. Tests mock Telegram interactions; a provisioned bot, channels, and authorized test users are required for live integration verification.
