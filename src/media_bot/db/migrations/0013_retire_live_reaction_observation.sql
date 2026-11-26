BEGIN IMMEDIATE;
-- Legacy reaction tables remain readable for rollback/audit compatibility; no
-- production handler or evaluator consumes them after this migration.
PRAGMA user_version = 13;
COMMIT;
