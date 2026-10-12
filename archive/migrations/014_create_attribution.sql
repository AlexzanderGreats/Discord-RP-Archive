CREATE TABLE players (
    player_id INTEGER PRIMARY KEY,
    player_name TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_players_name ON players(player_name);

CREATE TABLE characters (
    character_id INTEGER PRIMARY KEY,
    character_name TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_characters_name ON characters(character_name);

CREATE TABLE character_aliases (
    alias_id INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    alias_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(character_id) REFERENCES characters(character_id) ON DELETE NO ACTION,
    UNIQUE(character_id, alias_key)
);
CREATE INDEX ix_character_aliases_key ON character_aliases(alias_key);

CREATE TABLE player_discord_identities (
    link_id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL,
    identity_id INTEGER NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT,
    notes TEXT,
    CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from),
    FOREIGN KEY(player_id) REFERENCES players(player_id) ON DELETE NO ACTION,
    FOREIGN KEY(identity_id) REFERENCES discord_identities(identity_id) ON DELETE NO ACTION
);
CREATE INDEX ix_player_discord_identity_identity ON player_discord_identities(identity_id);
CREATE INDEX ix_player_discord_identity_player ON player_discord_identities(player_id);

CREATE TABLE player_characters (
    link_id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL,
    character_id INTEGER NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    created_at TEXT NOT NULL,
    created_by TEXT,
    notes TEXT,
    CHECK(valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from),
    FOREIGN KEY(player_id) REFERENCES players(player_id) ON DELETE NO ACTION,
    FOREIGN KEY(character_id) REFERENCES characters(character_id) ON DELETE NO ACTION
);
CREATE INDEX ix_player_characters_player ON player_characters(player_id);
CREATE INDEX ix_player_characters_character ON player_characters(character_id);

CREATE TABLE message_attributions (
    attribution_id INTEGER PRIMARY KEY,
    message_observation_id INTEGER NOT NULL,
    character_id INTEGER,
    player_id INTEGER,
    method TEXT NOT NULL CHECK(method IN ('explicit_marker', 'identity_link', 'rule', 'manual', 'model')),
    confidence REAL CHECK(confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    status TEXT NOT NULL CHECK(status IN ('pending', 'accepted', 'rejected', 'superseded')),
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    FOREIGN KEY(message_observation_id) REFERENCES messages(message_observation_id) ON DELETE NO ACTION,
    FOREIGN KEY(character_id) REFERENCES characters(character_id) ON DELETE NO ACTION,
    FOREIGN KEY(player_id) REFERENCES players(player_id) ON DELETE NO ACTION,
    CHECK(character_id IS NOT NULL OR player_id IS NOT NULL)
);
CREATE UNIQUE INDEX ux_message_attribution_proposal
    ON message_attributions(
        message_observation_id, method, COALESCE(character_id, -1),
        COALESCE(player_id, -1), evidence_json
    );
CREATE UNIQUE INDEX ux_message_attribution_accepted
    ON message_attributions(message_observation_id) WHERE status = 'accepted';
CREATE INDEX ix_message_attributions_status ON message_attributions(status, created_at);
CREATE INDEX ix_message_attributions_message ON message_attributions(message_observation_id);
CREATE INDEX ix_message_attributions_character ON message_attributions(character_id);
CREATE INDEX ix_message_attributions_player ON message_attributions(player_id);

CREATE TABLE attribution_events (
    event_id INTEGER PRIMARY KEY,
    attribution_id INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('proposed', 'accepted', 'rejected', 'superseded')),
    previous_status TEXT,
    new_status TEXT NOT NULL CHECK(new_status IN ('pending', 'accepted', 'rejected', 'superseded')),
    happened_at TEXT NOT NULL,
    actor TEXT,
    detail_json TEXT NOT NULL,
    FOREIGN KEY(attribution_id) REFERENCES message_attributions(attribution_id) ON DELETE NO ACTION
);
CREATE INDEX ix_attribution_events_claim ON attribution_events(attribution_id, happened_at);

CREATE TRIGGER attribution_events_no_update
BEFORE UPDATE ON attribution_events
BEGIN
    SELECT RAISE(ABORT, 'attribution audit events are immutable');
END;

CREATE TRIGGER attribution_events_no_delete
BEFORE DELETE ON attribution_events
BEGIN
    SELECT RAISE(ABORT, 'attribution audit events are immutable');
END;