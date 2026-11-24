BEGIN IMMEDIATE;

ALTER TABLE offers ADD COLUMN slug_plain TEXT NULL CHECK (
    slug_plain IS NULL OR (typeof(slug_plain) = 'text' AND length(slug_plain) BETWEEN 1 AND 64)
);

PRAGMA user_version = 8;
COMMIT;
