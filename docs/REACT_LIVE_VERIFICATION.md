# Live REACT verification

Automated tests verify that identifiable `message_reaction` updates are parsed, that reaction replacement and removal update the durable rows, that anonymous reactions are rejected, and that `any_recent` is constrained by the configured channel and observed posts. They do not prove Telegram delivered a real update.

## Telegram prerequisites

The bot must be an administrator in the exact target channel containing the post. The configured `target_chat_id` must equal that channel's numeric ID; do not infer it from a username or accept a different channel. The bot must be running with `message_reaction` in `allowed_updates`, and the target post must be eligible under the configured `react_post`, message-count, and day-window settings.

## Verification procedure

1. Register the exact channel and confirm the bot's administrator status using the panel's access test.
2. Create or inspect a REACT rule and record its canonical `target_chat_id` and post/window configuration.
3. Confirm that only one polling process is using the bot token.
4. Start the bot and watch for `reaction_update_received_raw`, followed by `reaction_update_received`, `reaction_update_persisted`, or `reaction_update_rejected`.
5. From a normal, identifiable Telegram account, react to an eligible post in the exact target channel, then remove the reaction and replace it with another emoji.
6. Confirm the corresponding `user_id`, channel ID, post ID, and emoji in the structured logs and verify the evaluator result changes from `missing_reaction` to passed and back after removal.

Anonymous channel reactions cannot be attributed to a user and must remain rejected. If no raw receipt appears, the failure is upstream of the handler: inspect bot administrator status, target channel identity, `allowed_updates`, polling conflicts, and the active network route.
