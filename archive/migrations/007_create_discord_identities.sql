CREATE TABLE discord_identities (
    identity_id INTEGER PRIMARY KEY,
    identity_type TEXT NOT NULL CHECK(identity_type IN ('user', 'bot', 'webhook', 'system', 'unknown')),
    discord_user_id TEXT,
    webhook_id TEXT,
    username_current TEXT,
    display_name_current TEXT,
    is_bot INTEGER CHECK(is_bot IS NULL OR is_bot IN (0, 1)),
    first_seen_import_id INTEGER,
    last_seen_import_id INTEGER,
    CHECK(identity_type NOT IN ('user', 'bot') OR discord_user_id IS NOT NULL),
    CHECK(identity_type <> 'webhook' OR webhook_id IS NOT NULL),
    FOREIGN KEY(first_seen_import_id) REFERENCES imports(import_id) ON DELETE NO ACTION,
    FOREIGN KEY(last_seen_import_id) REFERENCES imports(import_id) ON DELETE NO ACTION
);
CREATE UNIQUE INDEX ux_identities_user_id ON discord_identities(discord_user_id) WHERE discord_user_id IS NOT NULL;
CREATE UNIQUE INDEX ux_identities_webhook_id ON discord_identities(webhook_id) WHERE webhook_id IS NOT NULL;
CREATE INDEX ix_identities_type ON discord_identities(identity_type);
CREATE INDEX ix_identities_username ON discord_identities(username_current);
CREATE INDEX ix_identities_display_name ON discord_identities(display_name_current);
CREATE INDEX ix_identities_first_import ON discord_identities(first_seen_import_id);
CREATE INDEX ix_identities_last_import ON discord_identities(last_seen_import_id);