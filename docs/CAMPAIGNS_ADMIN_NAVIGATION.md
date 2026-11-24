# Campaigns admin navigation smoke script

Use a private chat with an administrator account. Open `/admin`, choose **📣 Campaigns**, then choose **＋ Create campaign**. Select a media item, send the campaign title, and press **Create offer**. The campaign detail view shows the opaque deep link, channel count, requirement count, and (when configured) the real bot username.

## Add Channel on a fresh install

From campaign detail, press **Add channel**. The empty registry still presents **＋ Add new channel**. Follow the exact prompts:

1. `Send the channel @username, t.me link, or -100... id.`
2. `Send a display name (or press Skip to use the username).`
3. `Send the invite URL (t.me/+... or t.me/... ), or Skip to derive from username.`

The bot resolves the identifier with `get_chat` before writing anything. If Telegram cannot resolve it, the panel says: `Bot cannot access that channel — is it public or is the bot an admin there?` A public username without an invite URL derives `https://t.me/<username>`. For a chat without a public username, an invite URL is required. After success, the detail view reports `Channels: 1`.

## Add Requirement

From campaign detail, press **Add requirement**. Existing registered requirements, including **REACT**, are shown as picker buttons. If the registry is empty, the panel displays:

`No REACT requirements defined yet. Use /addreq on the admin channel to create one, then return here.`

The empty state is intentional and does not throw an exception. Create the requirement through the existing Requirements flow, return to the campaign, and press **Add requirement** again.

## Verification checklist

Confirm that the deep link uses `https://t.me/<real_username>?start=<opaque-slug>`. If Telegram identity is unavailable or is a placeholder such as `YourBot`, the panel displays `(bot username not configured)` rather than a broken link. Confirm that an enabled campaign with zero channels and zero requirements displays the warning that files will be delivered freely. Use **View stats** to confirm starts, unlocks, deliveries, and conversion rate.
