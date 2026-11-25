BEGIN IMMEDIATE;
-- Preserve the original bearer-token digest and all share-link lifecycle
-- fields. Only active legacy rows without an offer are backfilled.
INSERT INTO offers (
    item_id, slug_hash, slug_hint, title, description, is_enabled,
    created_by_user_id, created_at_ms, updated_at_ms
)
SELECT
    links.item_id,
    links.token_hash,
    'legacy-' || CAST(links.id AS TEXT),
    COALESCE(NULLIF(trim(items.file_name), ''), 'Offer ' || CAST(items.id AS TEXT)),
    items.caption,
    1,
    links.created_by_user_id,
    links.created_at_ms,
    links.created_at_ms
FROM share_links AS links
JOIN items ON items.id = links.item_id
LEFT JOIN offers ON offers.slug_hash = links.token_hash
WHERE offers.id IS NULL
  AND links.revoked_at_ms IS NULL
  AND (links.expires_at_ms IS NULL OR links.expires_at_ms > CAST(strftime('%s', 'now') AS INTEGER) * 1000)
  AND (links.max_uses IS NULL OR links.use_count < links.max_uses);
WITH RECURSIVE item_scopes(offer_id, category_id) AS (
    SELECT offers.id, items.category_id
    FROM offers
    JOIN items ON items.id = offers.item_id
    WHERE items.category_id IS NOT NULL
      AND offers.slug_hint LIKE 'legacy-%'
    UNION
    SELECT item_scopes.offer_id, links.child_category_id
    FROM item_scopes
    JOIN category_links AS links
      ON links.parent_category_id = item_scopes.category_id
)
INSERT OR IGNORE INTO offer_requirements (offer_id, requirement_id, sort_order)
SELECT DISTINCT offers.id, associations.requirement_id, associations.requirement_id
FROM offers
JOIN item_scopes ON item_scopes.offer_id = offers.id
JOIN category_requirements AS associations
  ON associations.category_id IS NULL OR associations.category_id = item_scopes.category_id;
PRAGMA user_version = 10;
COMMIT;
