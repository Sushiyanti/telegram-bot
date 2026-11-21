BEGIN IMMEDIATE;

CREATE TABLE categories (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK (length(trim(name)) BETWEEN 1 AND 80),
    storage_channel_id INTEGER CHECK (
        storage_channel_id IS NULL OR
        (typeof(storage_channel_id) = 'integer' AND storage_channel_id <> 0)
    ),
    created_at_ms INTEGER NOT NULL CHECK (typeof(created_at_ms) = 'integer' AND created_at_ms >= 0)
);

INSERT INTO categories (name, storage_channel_id, created_at_ms)
VALUES ('Default', NULL, CAST(strftime('%s', 'now') AS INTEGER) * 1000);

ALTER TABLE items ADD COLUMN category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL;
ALTER TABLE items ADD COLUMN storage_channel_id INTEGER CHECK (
    storage_channel_id IS NULL OR
    (typeof(storage_channel_id) = 'integer' AND storage_channel_id <> 0)
);
UPDATE items
SET category_id = (SELECT id FROM categories WHERE name = 'Default')
WHERE category_id IS NULL;
UPDATE items
SET storage_channel_id = (
    SELECT storage_chat_id FROM item_storage_messages
    WHERE item_storage_messages.item_id = items.id
    ORDER BY sequence LIMIT 1
)
WHERE storage_channel_id IS NULL;

CREATE TABLE category_links (
    parent_category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    child_category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    PRIMARY KEY (parent_category_id, child_category_id),
    CHECK (parent_category_id <> child_category_id)
);
CREATE INDEX category_links_by_child ON category_links(child_category_id, parent_category_id);

CREATE TABLE requirements (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('JOIN_CHAT', 'REACT', 'START_BOT', 'TIMED_WAIT')),
    target TEXT,
    settings_json TEXT NOT NULL CHECK (json_valid(settings_json)),
    created_at_ms INTEGER NOT NULL CHECK (typeof(created_at_ms) = 'integer' AND created_at_ms >= 0),
    CHECK ((type = 'TIMED_WAIT' AND target IS NULL) OR (type <> 'TIMED_WAIT' AND target IS NOT NULL))
);

CREATE TABLE category_requirements (
    category_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
    requirement_id INTEGER NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
    CHECK (category_id IS NULL OR category_id > 0)
);
CREATE UNIQUE INDEX category_requirements_by_category
    ON category_requirements(category_id, requirement_id) WHERE category_id IS NOT NULL;
CREATE UNIQUE INDEX category_requirements_global
    ON category_requirements(requirement_id) WHERE category_id IS NULL;
CREATE INDEX category_requirements_by_requirement
    ON category_requirements(requirement_id);

CREATE TABLE bot_settings (
    key TEXT PRIMARY KEY CHECK (length(trim(key)) BETWEEN 1 AND 128),
    value TEXT NOT NULL CHECK (length(value) <= 1024)
);
INSERT INTO bot_settings (key, value) VALUES ('ENABLE_USER_COMMANDS', 'true');

ALTER TABLE pending_uploads ADD COLUMN category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL;
ALTER TABLE pending_uploads ADD COLUMN category_selection_pending INTEGER NOT NULL DEFAULT 0
    CHECK (category_selection_pending IN (0, 1));

CREATE TABLE observed_channel_posts (
    channel_id INTEGER NOT NULL CHECK (typeof(channel_id) = 'integer' AND channel_id <> 0),
    message_id INTEGER NOT NULL CHECK (typeof(message_id) = 'integer' AND message_id > 0),
    posted_at_ms INTEGER NOT NULL CHECK (typeof(posted_at_ms) = 'integer' AND posted_at_ms >= 0),
    PRIMARY KEY (channel_id, message_id)
);
CREATE INDEX observed_channel_posts_latest
    ON observed_channel_posts(channel_id, posted_at_ms DESC, message_id DESC);

CREATE TABLE user_reactions (
    user_id INTEGER NOT NULL CHECK (typeof(user_id) = 'integer' AND user_id > 0),
    channel_id INTEGER NOT NULL CHECK (typeof(channel_id) = 'integer' AND channel_id <> 0),
    message_id INTEGER NOT NULL CHECK (typeof(message_id) = 'integer' AND message_id > 0),
    emoji TEXT NOT NULL CHECK (length(emoji) BETWEEN 1 AND 128),
    updated_at_ms INTEGER NOT NULL CHECK (typeof(updated_at_ms) = 'integer' AND updated_at_ms >= 0),
    PRIMARY KEY (user_id, channel_id, message_id, emoji),
    FOREIGN KEY (channel_id, message_id)
        REFERENCES observed_channel_posts(channel_id, message_id) ON DELETE CASCADE
);
CREATE INDEX user_reactions_for_user_channel
    ON user_reactions(user_id, channel_id, updated_at_ms DESC);

PRAGMA user_version = 5;
COMMIT;
