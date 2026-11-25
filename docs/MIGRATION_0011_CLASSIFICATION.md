# Migration 0011 Historical Assignment Classification

Source: disposable copy `/tmp/telegram-bot-live/bot.db` (schema version 10).

Classification uses the actual legacy `offers.slug_hint` marker and historical sort-order evidence. Migrations 0007/0010 generated `legacy-<share_link_id>` offers and copied assignments with `sort_order=requirement_id`; the campaign repository's default direct-assignment sort order is `0`. A legacy row with `sort_order=0` is therefore retained as direct, while a legacy row with a nonzero sort order and no currently applicable global/descendant-category source is ambiguous and aborts migration. Category applicability follows the existing implementation: a category receives assignments attached to itself and linked descendants. The reviewed copy contains no category assignments, so separate disposable category fixtures are covered by `tests/test_migration_0011.py`.

| Offer | Item | Item category | Requirement | Type | Target | Global match | Category assignments | Classification | Reason |
|---:|---:|---:|---:|---|---:|---:|---|---|---|
| 1 | 1 | 1 | 1 | JOIN_CHAT | -1003927518547 | 1 | — | copied assignment | legacy backfill marker legacy-1; matching global=True, applicable descendant categories=none |
| 1 | 1 | 1 | 2 | REACT | -1004465118588 | 1 | — | copied assignment | legacy backfill marker legacy-1; matching global=True, applicable descendant categories=none |
| 2 | 2 | 1 | 1 | JOIN_CHAT | -1003927518547 | 1 | — | copied assignment | legacy backfill marker legacy-2; matching global=True, applicable descendant categories=none |
| 2 | 2 | 1 | 2 | REACT | -1004465118588 | 1 | — | copied assignment | legacy backfill marker legacy-2; matching global=True, applicable descendant categories=none |
| 3 | 1 | 1 | 1 | JOIN_CHAT | -1003927518547 | 1 | — | copied assignment | legacy backfill marker legacy-3; matching global=True, applicable descendant categories=none |
| 3 | 1 | 1 | 2 | REACT | -1004465118588 | 1 | — | copied assignment | legacy backfill marker legacy-3; matching global=True, applicable descendant categories=none |
| 4 | 6 | 1 | 1 | JOIN_CHAT | -1003927518547 | 1 | — | copied assignment | legacy backfill marker legacy-8; matching global=True, applicable descendant categories=none |
| 4 | 6 | 1 | 2 | REACT | -1004465118588 | 1 | — | copied assignment | legacy backfill marker legacy-8; matching global=True, applicable descendant categories=none |
| 5 | 8 | 2 | 1 | JOIN_CHAT | -1003927518547 | 1 | — | copied assignment | legacy backfill marker legacy-9; matching global=True, applicable descendant categories=none |
| 5 | 8 | 2 | 2 | REACT | -1004465118588 | 1 | — | copied assignment | legacy backfill marker legacy-9; matching global=True, applicable descendant categories=none |
| 6 | 9 | 2 | 1 | JOIN_CHAT | -1003927518547 | 1 | — | copied assignment | legacy backfill marker legacy-10; matching global=True, applicable descendant categories=none |
| 6 | 9 | 2 | 2 | REACT | -1004465118588 | 1 | — | copied assignment | legacy backfill marker legacy-10; matching global=True, applicable descendant categories=none |
| 7 | 10 | 1 | 1 | JOIN_CHAT | -1003927518547 | 1 | — | copied assignment | legacy backfill marker legacy-11; matching global=True, applicable descendant categories=none |
| 7 | 10 | 1 | 2 | REACT | -1004465118588 | 1 | — | copied assignment | legacy backfill marker legacy-11; matching global=True, applicable descendant categories=none |
| 11 | 8 | 2 | 2 | REACT | -1004465118588 | 1 | — | direct Offer assignment | campaign offer has no legacy backfill marker; retained as direct campaign linkage |
| 12 | 10 | 1 | 1 | JOIN_CHAT | -1003927518547 | 1 | — | copied assignment | legacy backfill marker legacy-12; matching global=True, applicable descendant categories=none |
| 12 | 10 | 1 | 2 | REACT | -1004465118588 | 1 | — | copied assignment | legacy backfill marker legacy-12; matching global=True, applicable descendant categories=none |
| 13 | 2 | 1 | 1 | JOIN_CHAT | -1003927518547 | 1 | — | copied assignment | legacy backfill marker legacy-13; matching global=True, applicable descendant categories=none |
| 13 | 2 | 1 | 2 | REACT | -1004465118588 | 1 | — | copied assignment | legacy backfill marker legacy-13; matching global=True, applicable descendant categories=none |
| 14 | 8 | 2 | 1 | JOIN_CHAT | -1003927518547 | 1 | — | copied assignment | legacy backfill marker legacy-14; matching global=True, applicable descendant categories=none |
| 14 | 8 | 2 | 2 | REACT | -1004465118588 | 1 | — | copied assignment | legacy backfill marker legacy-14; matching global=True, applicable descendant categories=none |

**Rows:** 21  
**Copied:** 20  
**Direct:** 1  
**Ambiguous:** 0
