BEGIN IMMEDIATE;

CREATE TABLE items (
    id INTEGER PRIMARY KEY,
    owner_user_id INTEGER NOT NULL CHECK (typeof(owner_user_id) = 'integer' AND owner_user_id > 0),
    media_type TEXT NOT NULL CHECK (length(media_type) BETWEEN 1 AND 32),
    file_size_bytes INTEGER CHECK (
        file_size_bytes IS NULL OR
        (typeof(file_size_bytes) = 'integer' AND file_size_bytes >= 0)
    ),
    file_name TEXT,
    mime_type TEXT,
    caption TEXT,
    protect_content INTEGER NOT NULL DEFAULT 0 CHECK (protect_content IN (0, 1)),
    created_at_ms INTEGER NOT NULL CHECK (typeof(created_at_ms) = 'integer' AND created_at_ms >= 0)
);

CREATE TABLE item_storage_messages (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    storage_chat_id INTEGER NOT NULL CHECK (typeof(storage_chat_id) = 'integer' AND storage_chat_id <> 0),
    storage_message_id INTEGER NOT NULL CHECK (typeof(storage_message_id) = 'integer' AND storage_message_id > 0),
    sequence INTEGER NOT NULL DEFAULT 0 CHECK (typeof(sequence) = 'integer' AND sequence >= 0),
    PRIMARY KEY (item_id, sequence),
    UNIQUE (storage_chat_id, storage_message_id)
);

CREATE TABLE share_links (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    token_hash BLOB NOT NULL UNIQUE CHECK (typeof(token_hash) = 'blob' AND length(token_hash) = 32),
    created_by_user_id INTEGER NOT NULL CHECK (typeof(created_by_user_id) = 'integer' AND created_by_user_id > 0),
    created_at_ms INTEGER NOT NULL CHECK (typeof(created_at_ms) = 'integer' AND created_at_ms >= 0),
    expires_at_ms INTEGER CHECK (
        expires_at_ms IS NULL OR
        (typeof(expires_at_ms) = 'integer' AND expires_at_ms > created_at_ms)
    ),
    revoked_at_ms INTEGER CHECK (
        revoked_at_ms IS NULL OR
        (typeof(revoked_at_ms) = 'integer' AND revoked_at_ms >= created_at_ms)
    )
);
CREATE INDEX share_links_by_item_id ON share_links(item_id, created_at_ms);

CREATE TABLE required_chats (
    chat_id INTEGER PRIMARY KEY CHECK (typeof(chat_id) = 'integer' AND chat_id <> 0),
    chat_type TEXT NOT NULL CHECK (chat_type IN ('channel', 'group', 'supergroup')),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    username TEXT,
    is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
    created_at_ms INTEGER NOT NULL CHECK (typeof(created_at_ms) = 'integer' AND created_at_ms >= 0),
    updated_at_ms INTEGER NOT NULL CHECK (typeof(updated_at_ms) = 'integer' AND updated_at_ms >= created_at_ms)
);

CREATE TABLE pending_uploads (
    upload_id TEXT PRIMARY KEY CHECK (length(upload_id) BETWEEN 1 AND 128),
    owner_user_id INTEGER NOT NULL CHECK (typeof(owner_user_id) = 'integer' AND owner_user_id > 0),
    media_group_id TEXT NOT NULL CHECK (length(media_group_id) BETWEEN 1 AND 128),
    status TEXT NOT NULL DEFAULT 'collecting' CHECK (
        status IN ('collecting', 'ready', 'processing', 'completed', 'failed', 'cancelled')
    ),
    created_at_ms INTEGER NOT NULL CHECK (typeof(created_at_ms) = 'integer' AND created_at_ms >= 0),
    last_seen_at_ms INTEGER NOT NULL CHECK (typeof(last_seen_at_ms) = 'integer' AND last_seen_at_ms >= created_at_ms),
    settle_at_ms INTEGER NOT NULL CHECK (typeof(settle_at_ms) = 'integer' AND settle_at_ms >= last_seen_at_ms),
    started_at_ms INTEGER CHECK (started_at_ms IS NULL OR (typeof(started_at_ms) = 'integer' AND started_at_ms >= created_at_ms)),
    finished_at_ms INTEGER CHECK (finished_at_ms IS NULL OR (typeof(finished_at_ms) = 'integer' AND finished_at_ms >= created_at_ms)),
    error_code TEXT,
    UNIQUE (owner_user_id, media_group_id),
    CHECK (status <> 'processing' OR started_at_ms IS NOT NULL),
    CHECK (status NOT IN ('completed', 'failed', 'cancelled') OR finished_at_ms IS NOT NULL)
);
CREATE INDEX pending_uploads_by_status_settle ON pending_uploads(status, settle_at_ms);

CREATE TABLE pending_upload_messages (
    upload_id TEXT NOT NULL REFERENCES pending_uploads(upload_id) ON DELETE CASCADE,
    source_chat_id INTEGER NOT NULL CHECK (typeof(source_chat_id) = 'integer' AND source_chat_id > 0),
    source_message_id INTEGER NOT NULL CHECK (typeof(source_message_id) = 'integer' AND source_message_id > 0),
    sequence INTEGER NOT NULL CHECK (typeof(sequence) = 'integer' AND sequence >= 0),
    media_type TEXT NOT NULL CHECK (length(media_type) BETWEEN 1 AND 32),
    file_id TEXT NOT NULL CHECK (length(file_id) > 0),
    file_unique_id TEXT NOT NULL CHECK (length(file_unique_id) > 0),
    file_size_bytes INTEGER CHECK (
        file_size_bytes IS NULL OR
        (typeof(file_size_bytes) = 'integer' AND file_size_bytes >= 0)
    ),
    file_name TEXT,
    mime_type TEXT,
    caption TEXT,
    received_at_ms INTEGER NOT NULL CHECK (typeof(received_at_ms) = 'integer' AND received_at_ms >= 0),
    PRIMARY KEY (upload_id, sequence),
    UNIQUE (source_chat_id, source_message_id)
);

PRAGMA user_version = 1;
COMMIT;
