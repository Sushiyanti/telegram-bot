BEGIN IMMEDIATE;
CREATE TABLE user_reactions_v2 (
    user_id INTEGER NOT NULL CHECK (typeof(user_id) = 'integer' AND user_id > 0),
    channel_id INTEGER NOT NULL CHECK (typeof(channel_id) = 'integer' AND channel_id <> 0),
    message_id INTEGER NOT NULL CHECK (typeof(message_id) = 'integer' AND message_id > 0),
    emoji TEXT NOT NULL CHECK (length(trim(emoji)) BETWEEN 1 AND 64),
    updated_at_ms INTEGER NOT NULL CHECK (typeof(updated_at_ms) = 'integer' AND updated_at_ms >= 0),
    PRIMARY KEY (user_id, channel_id, message_id, emoji)
);
INSERT INTO user_reactions_v2 (user_id, channel_id, message_id, emoji, updated_at_ms)
SELECT user_id, channel_id, message_id, emoji, updated_at_ms
FROM user_reactions;
DROP TABLE user_reactions;
ALTER TABLE user_reactions_v2 RENAME TO user_reactions;
CREATE INDEX user_reactions_for_user_channel
    ON user_reactions(user_id, channel_id, updated_at_ms DESC);
PRAGMA user_version = 9;
COMMIT;
