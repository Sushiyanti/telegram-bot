# M7 Plan — End-to-End Transfer Readiness

## Status and rationale

M7 is the next project milestone after the user accepted M6 on 2026-11-23 based on mocked validation. Live Telegram behavior remains explicitly unverified. The repository has no earlier M7 specification; this plan derives its scope from the documented open question in `docs/large-file-feasibility.md`: the bot’s end-to-end transfer path has not yet passed a complete upload-to-recipient-download/checksum test at any size.

M7 establishes evidence for the current Bot API copy-based path before the project advertises or enables a transfer-size capability. It is not a commitment to support a particular size. Telegram’s published upload allowances and API method descriptions are not treated as experimental proof.

## Scope

1. Preserve the current fail-closed behavior: `VERIFIED_TRANSFER_MAX_FILE_BYTES` remains unset unless an end-to-end tested capability is deliberately configured.
2. Prepare a reproducible, opt-in integration procedure or harness for an isolated environment. It must not run as part of the ordinary mocked test suite, must not log tokens or raw identifiers, and must not target production chats.
3. Test the complete path independently at each selected size: authorized user upload to bot, bot copy to private storage chat, persistence of the resulting message reference, bot restart/reference check, share-link delivery to a separate recipient test account, and full recipient download with exact byte-count and SHA-256 comparison.
4. Record each size as pass, fail, or not run, including the selected account/API route, exact fixture bytes, elapsed time, and redacted diagnostics. A successful copy or visible recipient message without a full download and checksum is not a pass.
5. Set any supported-size ceiling only to the largest size that passed the complete path under the documented test conditions. Preserve fail-closed behavior above that ceiling.

The existing feasibility note proposes decimal SI fixtures at 50 MB, 100 MB, 500 MB, 1 GB, 2 GB, 3 GB, and 4 GB. M7 may select a smaller subset first. Multi-gigabyte testing is optional until isolated storage, time, bandwidth, and an authorized uploader/recipient environment are available.

## Explicit non-goals

M7 does not introduce MTProto, Telethon/TDLib account sessions, a local Bot API server, or another transfer architecture. It does not increase upload/delivery limits based on documentation alone, promise Premium-sized support, change the default size gate, or test against production users/storage. Any transport change requires a separate architecture and credential-security review after results show a concrete need.

## Phases and acceptance gates

### M7-A — Test-environment and runner readiness

Identify a non-production bot, isolated private storage chat, dedicated uploader and recipient test accounts, disposable database, and safe fixture storage. For tests above ordinary account limits, arrange an authorized Premium test uploader. Keep secrets in an approved secret store/environment and never commit them. Decide whether the operator-run workflow is manual-assisted or automation before adding dependencies or client-account authentication code.

**Gate:** the environment and access method are documented without exposing secrets; no test can silently target production; no end-to-end size is yet certified.

An offline fixture utility is now available at `scripts/transfer_fixture.py`. It creates exact-size random, non-sensitive files, writes a SHA-256 sidecar, streams checksum verification, refuses overwrites, checks free disk space, caps fixtures at 4,000,000,000 bytes, and requires an explicit `--confirm-large` flag above 500,000,000 bytes. It never calls Telegram, and no large fixture was generated during implementation. For example:

```sh
uv run --locked --group dev python scripts/transfer_fixture.py create \
  --output /tmp/transfer-50mb.bin --size-bytes 50000000
uv run --locked --group dev python scripts/transfer_fixture.py verify \
  --input /tmp/transfer-50mb.bin --expected-size-bytes 50000000 \
  --expected-sha256 <digest-from-create>
```

### M7-B — Low-size end-to-end baseline

Run the complete path at one or more modest sizes, beginning with 50 MB if practical. Confirm the stored message survives a bot restart and the recipient downloads the complete file. Compare the source and downloaded exact byte count and SHA-256.

**Gate:** a redacted report records the result and route. Failures remain failures; no size is added to configuration based on partial progress.

### M7-C — Capability tiers

Only after the baseline passes, test higher sizes independently (for example 100 MB, 500 MB, 1 GB, then larger tiers selected according to resources). Stop safely on permission/account restrictions, storage exhaustion, timeouts, unexpected copy behavior, or integrity mismatches. Keep hosted `getFile` limits distinct from message-copy results.

**Gate:** every certified size has a complete source-to-recipient checksum result, a persistent-reference/restart check, and redacted diagnostics.

### M7-D — Product size policy and operational record

Choose the maximum certified byte count from observed successful results, keep the setting unset if no size passes, update operator documentation, and add regression coverage for behavior at, below, and above the chosen limit. Document test conditions and explicitly distinguish measured results from Telegram documentation/inference.

**Gate:** the configured size ceiling is justified by reproducible end-to-end evidence, and all unsupported or untested sizes remain refused.

## Current state and next action

M7 scope is defined and the offline fixture utility has **4 passing tests**. The environment gate is not met: this sandbox has no authorized test bot, designated storage chat, uploader, or recipient. The full repository suite currently passes **240 tests**; compileall and `git diff --check` pass. No live Telegram call has been made and no test fixture was generated. Next, continue offline safeguards or wait for a dedicated non-production Telegram environment; do not claim any file size is verified.
