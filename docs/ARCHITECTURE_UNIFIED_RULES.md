# Unified Access Rules Architecture

## Status and goals

This is a foundation design for the public Advertise-to-Unlock bot. It is approved as the direction, but live reaction acceptance remains an open blocker until the migrated implementation is exercised against Telegram. Synthetic handler tests are not treated as proof that the live reaction pipeline works.

The bot has one canonical access-rule definition, one canonical Telegram channel/group registry, and one assignment model. A rule is reusable; its scope is represented by assignments rather than by copying rules into offers or maintaining separate JOIN and REACT systems.

## Canonical data model

### Registered Telegram chats

`required_chats` is the canonical registry. `chat_id` is the Telegram numeric identity and is the primary key. The row stores the current title/username, join URL, enabled state, and access metadata. JOIN and REACT rules refer to this registry through a real foreign key; an unregistered chat cannot be referenced by a saved rule.

### Rule definitions

`requirements` is the canonical rule-definition table. It contains the rule type, verification mode, wait configuration, and type-specific settings. For `JOIN_CHAT` and `REACT`, `target_chat_id` is a nullable foreign key to `required_chats(chat_id)` and is the only stored target identity. The generic legacy `target` field is not used by the new application code. REACT definitions additionally store:

- `react_post`: `any_recent`, a numeric message ID, or a validated Telegram post URL;
- `reaction_emoji`: the required single emoji;
- recent-message and recent-day windows.

The database CHECK constraint requires `target_chat_id` for JOIN_CHAT and REACT and requires it to be NULL for rule types that do not target a chat. The application also validates that a post URL's encoded channel is the same `target_chat_id` before saving.

### Rule assignments

`rule_assignments` is the only assignment table. It contains:

- `requirement_id` — a foreign key to `requirements`;
- `scope_type` — exactly `GLOBAL`, `CATEGORY`, or `OFFER`;
- `category_id` — nullable foreign key to `categories`;
- `offer_id` — nullable foreign key to `offers`;
- explicit ordering metadata.

A SQLite CHECK constraint enforces the scope shape:

- `GLOBAL`: `category_id IS NULL AND offer_id IS NULL`;
- `CATEGORY`: `category_id IS NOT NULL AND offer_id IS NULL`;
- `OFFER`: `offer_id IS NOT NULL AND category_id IS NULL`.

Partial unique indexes prevent duplicate `(requirement_id, category_id)` category assignments, duplicate `(requirement_id, offer_id)` offer assignments, and duplicate global assignments. This gives SQLite actual referential integrity instead of a polymorphic `scope_id` that cannot be enforced by a normal foreign key.

`offers` remains the public campaign/share-link record and owns the media-item relationship, opaque slug hash, enabled state, and lifecycle fields. Rule definitions are never copied into offers. `share_links` retains token hashes, expiry, revocation, use limits, and use counts.

## Effective-rule evaluation

For an offer, effective rules are assembled as:

```text
GLOBAL
+ applicable CATEGORY/inherited-category assignments
+ direct OFFER assignments
```

The existing inheritance direction is preserved: a rule assigned to a parent category applies to items assigned to that parent or to any descendant category. A child-category rule does not flow upward to its parent.

Example:

```text
Media (parent)
├── Movies
└── Music
```

If `JOIN_CHAT @brand` is assigned to `Media`, an offer categorized as `Movies` receives that JOIN gate, and an offer categorized as `Music` receives it too. If `REACT @release-posts` is assigned only to `Movies`, it applies to Movie offers but not Music offers. A rule assigned directly to an offer applies only to that offer.

Assignments are deduplicated by `requirement_id`. The deterministic evaluation order is global rules first, inherited categories from the highest ancestor to the most specific category, then direct offer rules; each scope retains its explicit assignment order. A duplicate rule encountered later is ignored. All remaining effective gates must pass before delivery. Editing a shared global or category rule changes future evaluations because the evaluator reads the rule definition at evaluation time; no stale copy exists in an offer.

During migration, rows in the old `offer_requirements` table are classified. Rows that duplicate a global/category rule copied into an offer are identified by matching the original requirement identity and the source scope metadata, then omitted from direct OFFER assignments. Only genuine campaign-specific rows become OFFER assignments. This prevents historical copied assignments from becoming permanent stale offer rules.

## REACT semantics and acceptance blocker

Exact-post mode validates the exact `(target_chat_id, message_id, reaction_emoji)` tuple. Any-recent mode evaluates observed posts within the configured message/day window. Both modes require the configured emoji. A reaction removal fails verification; a replacement reaction passes only when the current reaction matches the required emoji.

The wizard must cover and test private `t.me/c/...` links, public channel post links resolved through Telegram, numeric message IDs, mismatched post/target channels, exact-post mode, any-recent mode, emoji matching, reaction removal/replacement, and administrator validation against the exact target. Real REACT configuration requires the bot to be administrator or creator in `target_chat_id` and cannot be saved when that check fails.

The previous live test remains a tracked blocker: channel posts were observed, but no corresponding `user_reactions` row was received, so the evaluator correctly returned `missing_reaction`. Wizard validation and synthetic tests do not claim live acceptance. No new live test should be requested until migration, repository consolidation, panel workflows, diagnostics, and regression coverage are complete.

## Admin workflow

The home panel has two primary areas:

- **Uploads:** a paginated stored-item list; item detail; storage metadata; share links/campaigns; link-specific requirements; enable, disable, and revoke actions; and item/link relationships.
- **Rules & Requirements:** global rules; category/inheritance rules; campaign/link assignments; rule usage; and the central registered-channel library.

There is exactly one channel-registration workflow under Rules & Requirements. An item or offer workflow selects an existing registered chat or navigates to central registration and returns with its context. It never creates a second campaign-specific registration implementation. JOIN and REACT use the same registered-chat identity.

Rule creation knows its assignment scope before presenting rule choices. Campaign/link workflows expose only supported rule types and do not show unsupported internal choices such as `START_BOT`, `TIMED_WAIT`, or `JOIN_CHAT` when that workflow cannot execute them.

## Reference-safe deletion

All unlink and delete operations are transactional. Unlinking one scope removes only that assignment and preserves the rule when another assignment remains. Delete-everywhere removes all assignments atomically before deleting the rule. The UI shows global, category, and campaign/link usages before deletion.

A registered chat cannot be removed while any JOIN_CHAT or REACT rule references it. The UI shows every rule usage and requires those references to be unlinked or deleted in the same controlled workflow. No valid admin-panel action may surface a foreign-key failure.

## One-time migration and safety

The migration is a one-time schema transition, not a compatibility layer:

1. Create a timestamped SQLite backup before opening the migration transaction.
2. Create the canonical `rule_assignments` table with `category_id`, `offer_id`, CHECK constraints, foreign keys, and partial unique indexes.
3. Add `required_chats(chat_id)` foreign-key support and add `requirements.target_chat_id`.
4. Resolve existing JOIN and REACT targets against the registered-chat registry. Unresolvable rows are reported and abort the migration rather than being silently weakened.
5. Convert global/category assignments into canonical assignments.
6. Convert genuine campaign-specific assignments into OFFER assignments. Convert campaign JOIN-chat links into JOIN_CHAT definitions targeting registered chats, then assign them to the relevant offer.
7. Remove copied category/global rows from direct OFFER assignments using the source-scope classification described above.
8. Validate row counts, duplicate elimination, `PRAGMA foreign_key_check`, target-chat coverage, and preservation of item/media/token lifecycle records.
9. Switch repositories and evaluator reads/writes to the canonical schema.
10. Drop obsolete assignment tables and obsolete target/duplicate columns only after validation succeeds.

The migration does not delete uploaded items, storage references, active token hashes, expiry state, revocation state, use limits, or use counts. A development reset is separate, explicitly destructive, confirmation-protected, guarded against production paths, and backs up the database first.

## Delivery and maintenance definition

Before the final commit, the implementation must provide the actual migration SQL, repository and evaluator consolidation, Uploads and Rules & Requirements navigation, reference-safe deletion, maintenance commands (`make clean`, `make test`, `make lint`, `make format-check`, `make check`, and `make reset-dev-db`), migration validation evidence, full regression results, and the final commit SHA. No live acceptance claim will be made until the live reaction pipeline is separately verified.
