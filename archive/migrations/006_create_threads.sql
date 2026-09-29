CREATE TABLE threads (
    thread_id INTEGER PRIMARY KEY,
    parent_channel_id INTEGER NOT NULL,
    discord_thread_id TEXT NOT NULL,
    name_current TEXT,
    thread_type TEXT,
    archived_at_current TEXT,
    first_seen_import_id INTEGER,
    last_seen_import_id INTEGER,
    UNIQUE(thread_id, parent_channel_id),
    FOREIGN KEY(parent_channel_id) REFERENCES channels(channel_id) ON DELETE NO ACTION,
    FOREIGN KEY(first_seen_import_id) REFERENCES imports(import_id) ON DELETE NO ACTION,
    FOREIGN KEY(last_seen_import_id) REFERENCES imports(import_id) ON DELETE NO ACTION
);
CREATE UNIQUE INDEX ux_threads_parent_discord ON threads(parent_channel_id, discord_thread_id);
CREATE UNIQUE INDEX ux_threads_id_parent ON threads(thread_id, parent_channel_id);
CREATE INDEX ix_threads_parent ON threads(parent_channel_id);
CREATE INDEX ix_threads_name ON threads(name_current);
CREATE INDEX ix_threads_type ON threads(thread_type);
CREATE INDEX ix_threads_first_import ON threads(first_seen_import_id);
CREATE INDEX ix_threads_last_import ON threads(last_seen_import_id);