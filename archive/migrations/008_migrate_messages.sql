CREATE TABLE messages (
    message_observation_id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    import_id INTEGER NOT NULL,
    discord_message_id TEXT NOT NULL,
    channel_id INTEGER,
    thread_id INTEGER,
    author_identity_id INTEGER,
    author_discord_user_id_original TEXT,
    author_discord_name_original TEXT,
    author_display_name_original TEXT,
    author_nickname_original TEXT,
    author_is_bot_original INTEGER CHECK(author_is_bot_original IS NULL OR author_is_bot_original IN (0, 1)),
    webhook_id_original TEXT,
    message_type TEXT,
    timestamp_original TEXT,
    timestamp_utc TEXT,
    timestamp_unix REAL,
    timestamp_edited_original TEXT,
    timestamp_edited_utc TEXT,
    content_original TEXT NOT NULL,
    content_search TEXT NOT NULL,
    reply_to_discord_message_id TEXT,
    is_pinned INTEGER CHECK(is_pinned IS NULL OR is_pinned IN (0, 1)),
    call_ended_timestamp_original TEXT,
    attachments_json TEXT,
    embeds_json TEXT,
    stickers_json TEXT,
    reactions_json TEXT,
    mentions_json TEXT,
    inline_emojis_json TEXT,
    source_json TEXT NOT NULL,
    CHECK(thread_id IS NULL OR channel_id IS NOT NULL),
    FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE NO ACTION,
    FOREIGN KEY(import_id, source_id) REFERENCES imports(import_id, source_id) ON DELETE NO ACTION,
    FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE NO ACTION,
    FOREIGN KEY(thread_id, channel_id) REFERENCES threads(thread_id, parent_channel_id) ON DELETE NO ACTION,
    FOREIGN KEY(author_identity_id) REFERENCES discord_identities(identity_id) ON DELETE NO ACTION
);
CREATE UNIQUE INDEX ux_messages_import_discord ON messages(import_id, discord_message_id);
CREATE INDEX ix_messages_source ON messages(source_id);
CREATE INDEX ix_messages_import ON messages(import_id);
CREATE INDEX ix_messages_discord_id ON messages(discord_message_id);
CREATE INDEX ix_messages_timestamp ON messages(timestamp_utc);
CREATE INDEX ix_messages_channel_time ON messages(channel_id, timestamp_utc);
CREATE INDEX ix_messages_thread ON messages(thread_id);
CREATE INDEX ix_messages_author_identity ON messages(author_identity_id);
CREATE INDEX ix_messages_author_name ON messages(author_discord_name_original);
CREATE INDEX ix_messages_webhook ON messages(webhook_id_original);
CREATE INDEX ix_messages_reply_id ON messages(reply_to_discord_message_id);