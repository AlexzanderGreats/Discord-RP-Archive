CREATE TABLE categories (
    category_id INTEGER PRIMARY KEY,
    server_id INTEGER NOT NULL,
    discord_category_id TEXT NOT NULL,
    name_current TEXT,
    position_current INTEGER CHECK(position_current IS NULL OR position_current >= 0),
    first_seen_import_id INTEGER,
    last_seen_import_id INTEGER,
    UNIQUE(category_id, server_id),
    FOREIGN KEY(server_id) REFERENCES servers(server_id) ON DELETE NO ACTION,
    FOREIGN KEY(first_seen_import_id) REFERENCES imports(import_id) ON DELETE NO ACTION,
    FOREIGN KEY(last_seen_import_id) REFERENCES imports(import_id) ON DELETE NO ACTION
);
CREATE UNIQUE INDEX ux_categories_server_discord ON categories(server_id, discord_category_id);
CREATE UNIQUE INDEX ux_categories_id_server ON categories(category_id, server_id);
CREATE INDEX ix_categories_server ON categories(server_id);
CREATE INDEX ix_categories_name ON categories(name_current);
CREATE INDEX ix_categories_first_import ON categories(first_seen_import_id);
CREATE INDEX ix_categories_last_import ON categories(last_seen_import_id);