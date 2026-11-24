BEGIN IMMEDIATE;

ALTER TABLE required_chats ADD COLUMN invite_url TEXT CHECK (
    invite_url IS NULL OR (typeof(invite_url) = 'text' AND length(trim(invite_url)) BETWEEN 1 AND 512)
);

CREATE TABLE offers (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    slug_hash BLOB NOT NULL UNIQUE CHECK (typeof(slug_hash) = 'blob' AND length(slug_hash) = 32),
    slug_hint TEXT CHECK (slug_hint IS NULL OR length(trim(slug_hint)) BETWEEN 1 AND 128),
    title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 256),
    description TEXT,
    is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
    created_by_user_id INTEGER NOT NULL CHECK (typeof(created_by_user_id) = 'integer' AND created_by_user_id > 0),
    created_at_ms INTEGER NOT NULL CHECK (typeof(created_at_ms) = 'integer' AND created_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK (typeof(updated_at_ms) = 'integer' AND updated_at_ms >= created_at_ms)
);
CREATE INDEX offers_by_item ON offers(item_id);
CREATE INDEX offers_active_updated ON offers(is_enabled, updated_at_ms DESC);

CREATE TABLE offer_required_chats (
    offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL REFERENCES required_chats(chat_id) ON DELETE RESTRICT,
    sort_order INTEGER NOT NULL DEFAULT 0 CHECK (typeof(sort_order) = 'integer' AND sort_order >= 0),
    is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
    PRIMARY KEY (offer_id, chat_id)
);
CREATE INDEX offer_required_chats_ordered ON offer_required_chats(offer_id, sort_order, chat_id);

CREATE TABLE offer_requirements (
    offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    requirement_id INTEGER NOT NULL REFERENCES requirements(id) ON DELETE RESTRICT,
    sort_order INTEGER NOT NULL DEFAULT 0 CHECK (typeof(sort_order) = 'integer' AND sort_order >= 0),
    PRIMARY KEY (offer_id, requirement_id)
);
CREATE INDEX offer_requirements_ordered ON offer_requirements(offer_id, sort_order, requirement_id);

CREATE TABLE offer_deliveries (
    offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL CHECK (typeof(user_id) = 'integer' AND user_id > 0),
    first_unlocked_at_ms INTEGER NOT NULL CHECK (typeof(first_unlocked_at_ms) = 'integer' AND first_unlocked_at_ms >= 0),
    last_delivered_at_ms INTEGER NOT NULL CHECK (typeof(last_delivered_at_ms) = 'integer' AND last_delivered_at_ms >= 0),
    delivery_count INTEGER NOT NULL DEFAULT 0 CHECK (typeof(delivery_count) = 'integer' AND delivery_count >= 0),
    PRIMARY KEY (offer_id, user_id)
);
CREATE INDEX offer_deliveries_by_user ON offer_deliveries(user_id, offer_id);

CREATE TABLE offer_events (
    id INTEGER PRIMARY KEY,
    offer_id INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL CHECK (typeof(user_id) = 'integer' AND user_id > 0),
    event_type TEXT NOT NULL CHECK (event_type IN ('start', 'verification', 'unlock', 'delivery', 'delivery_failure')),
    verification_result TEXT,
    created_at_ms INTEGER NOT NULL CHECK (typeof(created_at_ms) = 'integer' AND created_at_ms >= 0),
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json))
);
CREATE INDEX offer_events_by_offer ON offer_events(offer_id, event_type, created_at_ms);
CREATE INDEX offer_events_by_user ON offer_events(user_id, offer_id, event_type, created_at_ms);
CREATE UNIQUE INDEX offer_events_unlock_once
    ON offer_events(offer_id, user_id) WHERE event_type = 'unlock';

-- Preserve every circulating active legacy link by using its existing token hash.
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
WHERE links.revoked_at_ms IS NULL;

-- Preserve global and category/inherited requirement behavior for backfilled offers.
WITH RECURSIVE item_scopes(offer_id, category_id) AS (
    SELECT offers.id, items.category_id
    FROM offers
    JOIN items ON items.id = offers.item_id
    WHERE items.category_id IS NOT NULL
    UNION
    SELECT item_scopes.offer_id, links.child_category_id
    FROM item_scopes
    JOIN category_links AS links
      ON links.parent_category_id = item_scopes.category_id
)
INSERT INTO offer_requirements (offer_id, requirement_id, sort_order)
SELECT DISTINCT offers.id, associations.requirement_id, associations.requirement_id
FROM offers
JOIN item_scopes ON item_scopes.offer_id = offers.id
JOIN category_requirements AS associations
  ON associations.category_id IS NULL OR associations.category_id = item_scopes.category_id;

INSERT INTO bot_settings (key, value)
VALUES ('REQUIRED_CHANNEL_IMPORT_V1', 'pending');

PRAGMA user_version = 7;
COMMIT;
