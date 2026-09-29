CREATE TABLE channels (
    channel_id INTEGER PRIMARY KEY,
    server_id INTEGER NOT NULL,
    discord_channel_id TEXT NOT NULL,
    category_id INTEGER,
    name_current TEXT,
    channel_type TEXT,
    topic_current TEXT,
    position_current INTEGER CHECK(position_current IS NULL OR position_current >= 0),
    purpose TEXT CHECK(purpose IS NULL OR purpose IN ('rp', 'ooc', 'mixed', 'unknown')),
    first_seen_import_id INTEGER,
    last_seen_import_id INTEGER,
    UNIQUE(channel_id, server_id),
    FOREIGN KEY(server_id) REFERENCES servers(server_id) ON DELETE NO ACTION,
    FOREIGN KEY(category_id, server_id) REFERENCES categories(category_id, server_id) ON DELETE NO ACTION,
    FOREIGN KEY(first_seen_import_id) REFERENCES imports(import_id) ON DELETE NO ACTION,
    FOREIGN KEY(last_seen_import_id) REFERENCES imports(import_id) ON DELETE NO ACTION
);
CREATE UNIQUE INDEX ux_channels_server_discord ON channels(server_id, discord_channel_id);
CREATE UNIQUE INDEX ux_channels_id_server ON channels(channel_id, server_id);
CREATE INDEX ix_channels_server ON channels(server_id);
CREATE INDEX ix_channels_category_server ON channels(category_id, server_id);
CREATE INDEX ix_channels_name ON channels(name_current);
CREATE INDEX ix_channels_type ON channels(channel_type);
CREATE INDEX ix_channels_purpose ON channels(purpose);
CREATE INDEX ix_channels_first_import ON channels(first_seen_import_id);
CREATE INDEX ix_channels_last_import ON channels(last_seen_import_id);