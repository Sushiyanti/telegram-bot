BEGIN IMMEDIATE;

-- Minimal identity registry required by uploaders.user_id.
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY CHECK (
        typeof(user_id) = 'integer' AND user_id > 0
    )
);

CREATE TABLE uploaders (
    user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE RESTRICT,
    added_by INTEGER CHECK (
        added_by IS NULL OR (typeof(added_by) = 'integer' AND added_by > 0)
    ),
    added_at TEXT NOT NULL CHECK (
        typeof(added_at) = 'text' AND length(trim(added_at)) BETWEEN 1 AND 40
    ),
    revoked_at TEXT CHECK (
        revoked_at IS NULL OR
        (typeof(revoked_at) = 'text' AND length(trim(revoked_at)) BETWEEN 1 AND 40)
    )
);

CREATE TABLE redeem_rate_limits (
    user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    last_attempt_at_ms INTEGER NOT NULL CHECK (
        typeof(last_attempt_at_ms) = 'integer' AND last_attempt_at_ms >= 0
    )
);

ALTER TABLE share_links ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(use_count) = 'integer' AND use_count >= 0
);
ALTER TABLE share_links ADD COLUMN max_uses INTEGER CHECK (
    max_uses IS NULL OR (typeof(max_uses) = 'integer' AND max_uses > 0)
);

CREATE TRIGGER share_links_use_limit_insert
BEFORE INSERT ON share_links
WHEN NEW.max_uses IS NOT NULL AND NEW.use_count > NEW.max_uses
BEGIN
    SELECT RAISE(ABORT, 'share link use count exceeds max_uses');
END;

CREATE TRIGGER share_links_use_limit_update
BEFORE UPDATE OF use_count, max_uses ON share_links
WHEN NEW.max_uses IS NOT NULL AND NEW.use_count > NEW.max_uses
BEGIN
    SELECT RAISE(ABORT, 'share link use count exceeds max_uses');
END;

PRAGMA user_version = 4;
COMMIT;
