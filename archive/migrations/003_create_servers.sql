CREATE TABLE servers (
    server_id INTEGER PRIMARY KEY,
    discord_server_id TEXT NOT NULL,
    name_current TEXT,
    first_seen_import_id INTEGER,
    last_seen_import_id INTEGER,
    FOREIGN KEY(first_seen_import_id) REFERENCES imports(import_id) ON DELETE NO ACTION,
    FOREIGN KEY(last_seen_import_id) REFERENCES imports(import_id) ON DELETE NO ACTION
);
CREATE UNIQUE INDEX ux_servers_discord_id ON servers(discord_server_id);
CREATE INDEX ix_servers_name ON servers(name_current);
CREATE INDEX ix_servers_first_import ON servers(first_seen_import_id);
CREATE INDEX ix_servers_last_import ON servers(last_seen_import_id);