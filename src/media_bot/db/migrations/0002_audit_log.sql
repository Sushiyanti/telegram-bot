BEGIN IMMEDIATE;

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER NOT NULL CHECK (typeof(actor_id) = 'integer' AND actor_id > 0),
    action TEXT NOT NULL CHECK (length(trim(action)) > 0),
    target_type TEXT NOT NULL CHECK (length(trim(target_type)) > 0),
    target_id TEXT NOT NULL CHECK (length(trim(target_id)) BETWEEN 1 AND 128),
    created_at_ms INTEGER NOT NULL CHECK (typeof(created_at_ms) = 'integer' AND created_at_ms >= 0)
);
CREATE INDEX audit_log_by_actor_created ON audit_log(actor_id, created_at_ms);

CREATE TRIGGER audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

PRAGMA user_version = 2;
COMMIT;
