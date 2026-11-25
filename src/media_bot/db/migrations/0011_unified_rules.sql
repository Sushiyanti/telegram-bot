BEGIN IMMEDIATE;

-- Snapshot the real legacy relationships before replacing them. The existing
-- schema has no source-scope columns in offer_requirements; classification is
-- derived from global/category applicability and item category inheritance.
CREATE TEMP TABLE legacy_category_requirements AS
SELECT category_id, requirement_id FROM category_requirements;
CREATE TEMP TABLE legacy_offer_requirements AS
SELECT offer_id, requirement_id, sort_order FROM offer_requirements;
CREATE TEMP TABLE legacy_offer_required_chats AS
SELECT offer_id, chat_id, sort_order, is_enabled FROM offer_required_chats;

CREATE TABLE requirements_canonical (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('JOIN_CHAT', 'REACT', 'START_BOT', 'TIMED_WAIT')),
    target TEXT,
    target_chat_id INTEGER REFERENCES required_chats(chat_id) ON DELETE RESTRICT,
    settings_json TEXT NOT NULL CHECK (json_valid(settings_json)),
    created_at_ms INTEGER NOT NULL CHECK (typeof(created_at_ms) = 'integer' AND created_at_ms >= 0),
    CHECK (
        (type IN ('JOIN_CHAT', 'REACT') AND target_chat_id IS NOT NULL)
        OR (type NOT IN ('JOIN_CHAT', 'REACT') AND target_chat_id IS NULL)
    ),
    CHECK ((type = 'TIMED_WAIT' AND target IS NULL) OR (type <> 'TIMED_WAIT' AND target IS NOT NULL))
);

INSERT INTO requirements_canonical
    (id, type, target, target_chat_id, settings_json, created_at_ms)
SELECT
    r.id,
    r.type,
    r.target,
    CASE WHEN r.type IN ('JOIN_CHAT', 'REACT') THEN CAST(r.target AS INTEGER) END,
    r.settings_json,
    r.created_at_ms
FROM requirements AS r
WHERE r.type NOT IN ('JOIN_CHAT', 'REACT')
   OR EXISTS (SELECT 1 FROM required_chats c WHERE c.chat_id = CAST(r.target AS INTEGER));

CREATE TEMP TABLE migration_target_guard (
    ok INTEGER NOT NULL CHECK (ok = 1)
);
INSERT INTO migration_target_guard(ok)
SELECT CASE WHEN EXISTS (
    SELECT 1 FROM requirements r
    WHERE r.type IN ('JOIN_CHAT', 'REACT')
      AND NOT EXISTS (SELECT 1 FROM required_chats c WHERE c.chat_id = CAST(r.target AS INTEGER))
) THEN 0 ELSE 1 END;
DROP TABLE migration_target_guard;

-- Legacy backfilled offers are marked by migrations 0007/0010 as
-- slug_hint=legacy-<share_link_id>. Campaign-created offers have no such
-- marker. A legacy row without a matching global or inherited-category source
-- is ambiguous and must abort before any old table is dropped.
CREATE TEMP TABLE migration_assignment_guard (
    ok INTEGER NOT NULL CHECK (ok = 1)
);
INSERT INTO migration_assignment_guard(ok)
WITH RECURSIVE item_categories(offer_id, category_id) AS (
    SELECT o.id, i.category_id FROM offers o JOIN items i ON i.id = o.item_id
    WHERE i.category_id IS NOT NULL
    UNION
    SELECT item_categories.offer_id, cl.child_category_id
    FROM item_categories
    JOIN category_links cl ON cl.parent_category_id = item_categories.category_id
),
applicable(requirement_id, offer_id) AS (
    SELECT lcr.requirement_id, o.id
    FROM legacy_category_requirements lcr JOIN offers o ON lcr.category_id IS NULL
    UNION
    SELECT lcr.requirement_id, item_categories.offer_id
    FROM legacy_category_requirements lcr JOIN item_categories
      ON lcr.category_id = item_categories.category_id
)
SELECT CASE WHEN EXISTS (
    SELECT 1 FROM legacy_offer_requirements lor JOIN offers o ON o.id = lor.offer_id
    WHERE o.slug_hint LIKE 'legacy-%'
      AND lor.sort_order <> 0
      AND NOT EXISTS (
          SELECT 1 FROM applicable a
          WHERE a.requirement_id = lor.requirement_id AND a.offer_id = lor.offer_id
      )
) THEN 0 ELSE 1 END;
DROP TABLE migration_assignment_guard;

DROP TABLE category_requirements;
DROP TABLE offer_requirements;
DROP TABLE offer_required_chats;
DROP TABLE requirements;
ALTER TABLE requirements_canonical RENAME TO requirements;

CREATE TABLE rule_assignments (
    id INTEGER PRIMARY KEY,
    requirement_id INTEGER NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('GLOBAL', 'CATEGORY', 'OFFER')),
    category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    offer_id INTEGER REFERENCES offers(id) ON DELETE CASCADE,
    sort_order INTEGER NOT NULL DEFAULT 0 CHECK (typeof(sort_order) = 'integer' AND sort_order >= 0),
    CHECK (
        (scope_type = 'GLOBAL' AND category_id IS NULL AND offer_id IS NULL)
        OR (scope_type = 'CATEGORY' AND category_id IS NOT NULL AND offer_id IS NULL)
        OR (scope_type = 'OFFER' AND category_id IS NULL AND offer_id IS NOT NULL)
    )
);
CREATE UNIQUE INDEX rule_assignments_global_unique
    ON rule_assignments(requirement_id) WHERE scope_type = 'GLOBAL';
CREATE UNIQUE INDEX rule_assignments_category_unique
    ON rule_assignments(requirement_id, category_id) WHERE scope_type = 'CATEGORY';
CREATE UNIQUE INDEX rule_assignments_offer_unique
    ON rule_assignments(requirement_id, offer_id) WHERE scope_type = 'OFFER';
CREATE INDEX rule_assignments_by_scope
    ON rule_assignments(scope_type, category_id, offer_id, sort_order, id);

INSERT INTO rule_assignments (requirement_id, scope_type, category_id, sort_order)
SELECT requirement_id,
       CASE WHEN category_id IS NULL THEN 'GLOBAL' ELSE 'CATEGORY' END,
       category_id,
       requirement_id
FROM legacy_category_requirements;

-- Existing offer rows that are explained by a global rule or an applicable
-- category rule were generated by migrations 0007/0010 and are copied scope
-- material. Only unexplained rows become direct OFFER assignments.
WITH RECURSIVE item_categories(offer_id, category_id) AS (
    SELECT o.id, i.category_id FROM offers o JOIN items i ON i.id = o.item_id
    WHERE i.category_id IS NOT NULL
    UNION
    SELECT item_categories.offer_id, cl.child_category_id
    FROM item_categories
    JOIN category_links cl ON cl.parent_category_id = item_categories.category_id
),
applicable(req_id, offer_id) AS (
    SELECT lcr.requirement_id, o.id
    FROM legacy_category_requirements lcr
    JOIN offers o ON lcr.category_id IS NULL
    UNION
    SELECT lcr.requirement_id, item_categories.offer_id
    FROM legacy_category_requirements lcr
    JOIN item_categories ON lcr.category_id = item_categories.category_id
)
INSERT INTO rule_assignments (requirement_id, scope_type, offer_id, sort_order)
SELECT lor.requirement_id, 'OFFER', lor.offer_id, lor.sort_order
FROM legacy_offer_requirements lor
JOIN offers o ON o.id = lor.offer_id
WHERE o.slug_hint NOT LIKE 'legacy-%'
   OR lor.sort_order = 0
   OR NOT EXISTS (
    SELECT 1 FROM applicable a
    WHERE a.req_id = lor.requirement_id AND a.offer_id = lor.offer_id
);

-- Each enabled campaign JOIN row becomes an executable JOIN_CHAT rule assigned
-- directly to that offer. A separate definition preserves per-offer ordering
-- and the historical invite URL without creating a second gate system.
CREATE TEMP TABLE legacy_join_requirements AS
SELECT
    loc.offer_id,
    loc.chat_id,
    loc.sort_order,
    (SELECT COALESCE(MAX(id), 0) FROM requirements)
      + ROW_NUMBER() OVER (ORDER BY loc.offer_id, loc.sort_order, loc.chat_id) AS requirement_id
FROM legacy_offer_required_chats loc
WHERE loc.is_enabled = 1;

INSERT INTO requirements (id, type, target, target_chat_id, settings_json, created_at_ms)
SELECT
    lj.requirement_id,
    'JOIN_CHAT',
    CAST(lj.chat_id AS TEXT),
    lj.chat_id,
    json_object('is_fake', 0, 'wait_seconds', 0,
                'link_url', COALESCE(rc.invite_url, '')),
    CAST(strftime('%s','now') AS INTEGER) * 1000
FROM legacy_join_requirements lj
JOIN required_chats rc ON rc.chat_id = lj.chat_id;

INSERT INTO rule_assignments (requirement_id, scope_type, offer_id, sort_order)
SELECT requirement_id, 'OFFER', offer_id, sort_order
FROM legacy_join_requirements;

-- The token hash and lifecycle remain owned by share_links. Offers receive a
-- stable one-to-one pointer; slug_hash is retained in this migration for
-- staged repository rollout and is not mutated.
ALTER TABLE offers ADD COLUMN share_link_id INTEGER REFERENCES share_links(id) ON DELETE RESTRICT;
CREATE UNIQUE INDEX offers_share_link_unique ON offers(share_link_id) WHERE share_link_id IS NOT NULL;
UPDATE offers
SET share_link_id = (
    SELECT sl.id FROM share_links sl
    WHERE sl.token_hash = offers.slug_hash
);

DROP TABLE legacy_category_requirements;
DROP TABLE legacy_offer_requirements;
DROP TABLE legacy_offer_required_chats;
PRAGMA user_version = 11;
COMMIT;
