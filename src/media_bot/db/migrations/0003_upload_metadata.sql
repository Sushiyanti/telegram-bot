BEGIN IMMEDIATE;

ALTER TABLE items ADD COLUMN sha256 BLOB CHECK (
    sha256 IS NULL OR (typeof(sha256) = 'blob' AND length(sha256) = 32)
);
ALTER TABLE pending_uploads ADD COLUMN pipeline TEXT NOT NULL DEFAULT 'legacy' CHECK (
    pipeline IN ('legacy', 'm2')
);
CREATE UNIQUE INDEX items_id_owner_unique ON items(id, owner_user_id);

-- Each logical item can contain one file or multiple album members.
CREATE TABLE item_files (
    item_id INTEGER NOT NULL,
    owner_user_id INTEGER NOT NULL CHECK (typeof(owner_user_id) = 'integer' AND owner_user_id > 0),
    sequence INTEGER NOT NULL CHECK (typeof(sequence) = 'integer' AND sequence >= 0),
    media_type TEXT NOT NULL CHECK (length(media_type) BETWEEN 1 AND 32),
    file_id TEXT NOT NULL CHECK (length(file_id) BETWEEN 1 AND 2048),
    file_unique_id TEXT NOT NULL CHECK (length(file_unique_id) BETWEEN 1 AND 256),
    file_size_bytes INTEGER NOT NULL CHECK (typeof(file_size_bytes) = 'integer' AND file_size_bytes >= 0),
    file_name TEXT,
    mime_type TEXT,
    PRIMARY KEY (item_id, sequence),
    FOREIGN KEY (item_id, owner_user_id) REFERENCES items(id, owner_user_id) ON DELETE RESTRICT
);
CREATE INDEX item_files_by_item ON item_files(item_id, sequence);

-- One fingerprint maps to at most one stored item per uploader. Album members
-- with duplicate fingerprints in the same item share this mapping.
CREATE TABLE item_file_fingerprints (
    owner_user_id INTEGER NOT NULL CHECK (typeof(owner_user_id) = 'integer' AND owner_user_id > 0),
    file_unique_id TEXT NOT NULL CHECK (length(file_unique_id) BETWEEN 1 AND 256),
    item_id INTEGER NOT NULL,
    PRIMARY KEY (owner_user_id, file_unique_id),
    FOREIGN KEY (item_id, owner_user_id) REFERENCES items(id, owner_user_id) ON DELETE RESTRICT
);
CREATE INDEX item_file_fingerprints_by_item ON item_file_fingerprints(item_id);

PRAGMA user_version = 3;
COMMIT;
