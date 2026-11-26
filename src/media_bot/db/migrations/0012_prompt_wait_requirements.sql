BEGIN;

-- Real reaction and real bot-start verification are discontinued. Preserve
-- assignments, canonical targets, prompt links, and wait durations while
-- making the new behavior explicit and auditable to administrators.
UPDATE requirements
SET settings_json = json_set(
    settings_json,
    '$.is_fake', 1,
    '$.verification_mode', 'prompt_wait',
    '$.migration_notice', 'Converted from discontinued real verification; this rule now prompts and waits only.'
)
WHERE type IN ('REACT', 'START_BOT')
  AND json_extract(settings_json, '$.is_fake') IS NOT 1;

PRAGMA user_version = 12;
COMMIT;
