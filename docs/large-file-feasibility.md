# Telegram large-file bot feasibility review

Several Telegram paths appear technically plausible for multi-gigabyte delivery, but their boundaries differ. Telegram documents Premium users’ 4 GB upload allowance, a 20 MB hosted Bot API download ceiling, a local Bot API server with uncapped downloads and uploads up to 2000 MB, and Bot API message-copy methods that relay Telegram-stored messages without a bot-side media download/upload step. MTProto libraries expose chunked transfer operations, and the published Premium part-count configuration is consistent with a 4 GB-scale transfer. These documented capabilities do not establish a successful end-to-end workflow.

**No end-to-end file sizes have been experimentally verified yet.** In particular, the research supplied here reports no completed upload → storage-channel copy → persisted reference → recipient-DM copy → full download/checksum test, at any size.

## Documented facts

### Cloud Bot API and Premium uploads

Telegram’s user-facing materials state that users may send files up to 2 GB, or 4 GB with Premium; they also say other users can download Premium-uploaded files over 2 GB [1–3]. The Bot API FAQ says bots receive all messages from private chats [4]. The Bot API changelog records support for 4 GB files uploaded by Premium accounts, while the API’s file metadata represents `file_size` in bytes [5–6]. Together these establish relevant support and update visibility, but do not explicitly guarantee delivery of every 4 GB Premium upload addressed to a bot.

The hosted Bot API’s `getFile` download limit is 20 MB [4, 7]. Its multipart upload limit is 50 MB for non-photo files (10 MB for photos); URL-based sending has separate limits [8]. These are bot-side API limits, not the Premium user’s account upload allowance. Receiving message metadata or a `file_id` is not the same as retrieving the complete file through hosted `getFile`.

### Local Bot API server

Telegram documents local mode as permitting downloads without a size limit and uploads up to 2000 MB. It supports local paths/URIs and can return absolute local paths from `getFile` [8–9]. “Without a size limit” describes the API limit, not available disk, throughput, durability, or a tested 4 GB transfer. Upload and download limits are separate.

Self-hosting shifts operational responsibility to the operator: service availability, storage lifecycle, capacity, network and TLS termination, access control, monitoring, upgrades, and migration. Telegram documents an HTTP-only API server, the need for `api_id`/`api_hash`, and migration steps including `logOut` against the hosted endpoint before moving a bot [9]. The docs do not establish local media as a durable archive. Telegram says most bots can use the hosted configuration [8].

### `copyMessage` / `copyMessages`

The Bot API provides `copyMessage` and `copyMessages` to copy source message(s) to a target chat using chat and message identifiers, without a link to the original [10–11]. `copyMessages` accepts 1–100 ordered message IDs and preserves album grouping; unavailable or uncopyable messages are skipped [11]. The API enumerates message-type exclusions, and permissions and source accessibility remain relevant [10–12]. Neither method documents a media byte-size ceiling.

A storage-channel-to-recipient relay is therefore a reasonable **documentary inference**: copy the incoming message to a storage channel, persist that channel message reference, then copy from the channel to the recipient. This should avoid downloading and re-uploading media bytes on the bot backend. It is not an explicit unlimited-size guarantee; protected-content behavior is also not unambiguously resolved by the cited pages [10–12]. The general `file_id` no-size-limit wording concerns reusing files already stored on Telegram, not a specific guarantee for every large message-copy operation [8].

### MTProto alternatives

Telegram’s Premium announcement documents 4 GB uploads, and its MTProto client configuration lists 8000 Premium file parts at up to 512 KiB each; that configuration yields a derived ceiling of 4,194,304,000 bytes. This is a protocol/configuration calculation, not a tested success limit [2, 13–14]. Telegram’s MTProto documentation describes chunked transfer; TDLib and Telethon document upload/download functionality [14–17]. Forwarding an existing message is a distinct operation, not a documented way to bypass upload limits [18].

Telethon and TDLib support bot-token authentication, but the reviewed sources do not establish that a bot-authenticated session qualifies for the Premium upload allowance. For planning a 4 GB upload, assume an authorized Premium user account unless Telegram confirms otherwise [19–20]. MTProto introduces more sensitive client authorization credentials and additional implementation, protocol/error, and maintenance responsibilities. Telethon warns that session credentials can grant account access; Telegram’s API terms and MTProto security guidance impose privacy and security expectations [21–22]. TDLib encrypts local data with a user-provided key [16].

## Feasibility and tradeoffs

**Hosted Bot API direct retrieval.** The bot can receive a private-chat update and file metadata, but hosted `getFile` downloads are capped at 20 MB. Bot-originated multipart uploads have separate 50 MB/10 MB limits. A Premium user’s account allowance does not remove these bot-side API caps [4] [7] [8].

**Premium user to bot, then Bot API copy relay.** It is plausible that a bot can receive a Premium-sized message and copy it first to a storage channel and then to a recipient without transferring file bytes through its own backend. However, Telegram does not explicitly guarantee a 4 GB upload addressed to a bot or arbitrary-size copying. The transfer remains unverified [2] [4] [5] [6] [10] [11] [12].

**Local Bot API server.** Local mode documents downloads without a size limit and uploads up to 2000 MB. That is not a documented 4 GB upload path or unlimited durable storage. Self-hosting also requires operating the API service and filesystem, securing its HTTP/TLS boundary and credentials, and handling migrations and upgrades [8] [9].

**MTProto through TDLib or Telethon.** Chunked transfer and the published Premium part-count configuration make a 4 GB upload technically plausible for a Premium user. Neither the part-count arithmetic nor the client-library methods guarantee a successful end-to-end transfer. The reviewed sources do not establish Premium upload entitlement for a bot-token session. MTProto also means more sensitive session credentials, client error handling, and maintenance [13] [14] [15] [16] [17] [19] [20] [21] [22] [23].

**`file_id` reuse or message forwarding.** Telegram documents no size limit for reusing a file already stored on Telegram, but does not guarantee every Premium-sized item can first be obtained or copied by the bot. Source access, message eligibility, and protected-content behavior still matter [8] [10] [11] [12] [18].

Across these approaches, Telegram’s advertised GB/MB limits do not define decimal versus binary units, except where MTProto’s published configuration provides a part size from which a byte figure can be calculated [1] [2] [3] [8] [13] [14]. Backend storage, bandwidth, timeouts, checksums, retention, and recipient access still require separate design and testing.

## Experimental verification status

**Verified end-to-end sizes: none.** The research contains no live API calls or completed transfer tests. The 4 GB Premium-to-bot route, local-server large-file retrieval, and copy relay are documented or inferential feasibility arguments—not experimental results. The exact acceptance boundary, successful delivery of a 4 GB bot-chat upload, large-file copy behavior, practical throughput, and full-download integrity remain unverified.

## Isolated future integration test plan

Run only in a dedicated test environment with an isolated bot, storage channel, recipient test account, and non-sensitive fixtures. Do not place user accounts, tokens, API credentials, channel IDs, or other secrets in the repository, test output, screenshots, or logs; inject secrets through an approved secret store/environment and redact identifiers. Request/provision a Premium test uploader account if the 500 MB–4 GB uploads require it, plus a bot, private storage channel with appropriate bot permissions, recipient test account/chat, isolated storage, and fixtures with known expected checksums. Have the recipient open the bot and press **Start** before the private-delivery test; Telegram documents `/start` as beginning the interaction and provides a Start button on first opening the bot chat [24]. If comparing MTProto or local Bot API, separately provision the credentials and isolated deployment they require. Confirm Telegram’s effective size units and fixture sizes before each run; the documented GB labels do not settle decimal versus binary interpretation.

For **each** target size below, run the complete path independently and record pass/fail, timestamps, observed byte count, message references (redacted in logs), retries/errors, and SHA-256 at source and destination:

1. **Upload:** upload the fixture from the authorized test account to the bot’s direct chat; confirm the bot receives the update and record whether complete media retrieval is available through the selected route.
2. **Storage-channel copy:** have the bot copy the incoming message to the isolated storage channel. Confirm the resulting message is present and downloadable.
3. **Persisted message reference:** persist the storage channel/message reference in the test database, restart the bot/service, and verify the reference still resolves to the expected message.
4. **Recipient DM copy:** copy the stored message to the recipient test account’s DM and confirm the recipient receives the expected content.
5. **Full download/checksum:** download the entire recipient file, verify exact byte count and SHA-256 against the fixture, and record the result. A message visible in the chat or metadata alone is not a pass.

Run the complete path at each target: **50 MB, 100 MB, 500 MB, 1 GB, 2 GB, 3 GB, and 4 GB.** For repeatability, define these fixtures using decimal SI sizes: 50,000,000; 100,000,000; 500,000,000; 1,000,000,000; 2,000,000,000; 3,000,000,000; and 4,000,000,000 bytes, respectively. Record the exact integer byte count and SHA-256 for every fixture. This test convention does not resolve Telegram’s undocumented MB/GB unit interpretation. Each target requires upload, storage-channel copy, a persisted-reference/restart check, recipient-DM copy, and full download/checksum verification.

Test cloud Bot API copying separately from media retrieval: its 20 MB `getFile` limit means a successful copy does not prove the backend can fetch the bytes. If comparing local Bot API or MTProto, repeat the same end-to-end path as separately labelled configurations; do not infer one route’s result from another. Stop safely on unexpected permission errors, account restrictions, storage exhaustion, or integrity mismatch; preserve only redacted diagnostics.

## References

[1]: https://telegram.org/faq "Telegram FAQ"
[2]: https://telegram.org/blog/700-million-and-premium "700 Million Users and Telegram Premium"
[3]: https://telegram.org/faq_premium "Telegram Premium FAQ"
[4]: https://core.telegram.org/bots/faq "Telegram Bot FAQ"
[5]: https://core.telegram.org/bots/api-changelog "Bot API changelog"
[6]: https://core.telegram.org/bots/api/types/document "Document object"
[7]: https://core.telegram.org/bots/api/methods/getfile "getFile"
[8]: https://core.telegram.org/bots/api "Telegram Bot API"
[9]: https://github.com/tdlib/telegram-bot-api "Telegram Bot API server repository README"
[10]: https://core.telegram.org/bots/api/methods/copymessage "copyMessage"
[11]: https://core.telegram.org/bots/api/methods/copymessages "copyMessages"
[12]: https://core.telegram.org/bots/api/types/message "Message object"
[13]: https://core.telegram.org/api/config "Client Configuration"
[14]: https://core.telegram.org/api/files "Uploading and Downloading Files"
[15]: https://docs.telethon.dev/en/stable/modules/client.html "Telethon client reference"
[16]: https://core.telegram.org/tdlib/docs/ "TDLib Documentation"
[17]: https://github.com/LonamiWebs/Telethon/blob/v1/telethon/client/uploads.py "Telethon upload implementation"
[18]: https://core.telegram.org/tdlib/docs/classtd_1_1td__api_1_1forward_messages.html "TDLib forwardMessages reference"
[19]: https://docs.telethon.dev/en/stable/basic/signing-in.html "Telethon signing in"
[20]: https://core.telegram.org/tdlib/docs/classtd_1_1td__api_1_1check_authentication_bot_token.html "TDLib bot-token authentication"
[21]: https://docs.telethon.dev/en/stable/concepts/sessions.html "Telethon session files"
[22]: https://core.telegram.org/api/terms "Telegram API Terms of Service"
[23]: https://core.telegram.org/mtproto/security_guidelines "Security Guidelines for Client Developers"
[24]: https://core.telegram.org/bots/features "Telegram Bot Features"
