BEGIN IMMEDIATE;

CREATE TABLE admins (
    user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE RESTRICT,
    added_by INTEGER CHECK (
        added_by IS NULL OR (typeof(added_by) = 'integer' AND added_by > 0)
    ),
    created_at TEXT NOT NULL CHECK (
        typeof(created_at) = 'text' AND length(trim(created_at)) BETWEEN 1 AND 40
    )
);

PRAGMA user_version = 6;
COMMIT;
