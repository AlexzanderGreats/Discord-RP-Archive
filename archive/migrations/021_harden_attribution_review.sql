ALTER TABLE attribution_review_events
    ADD COLUMN previous_state_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE attribution_review_events
    ADD COLUMN requested_action TEXT NOT NULL DEFAULT '';

CREATE TABLE attribution_review_evidence_overrides (
    message_observation_id INTEGER PRIMARY KEY,
    is_excluded INTEGER NOT NULL CHECK(is_excluded IN (0, 1)),
    review_event_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(message_observation_id)
        REFERENCES messages(message_observation_id) ON DELETE NO ACTION,
    FOREIGN KEY(review_event_id)
        REFERENCES attribution_review_events(attribution_review_event_id) ON DELETE NO ACTION
);

CREATE TABLE attribution_review_input_changes (
    input_change_id INTEGER PRIMARY KEY,
    input_kind TEXT NOT NULL CHECK(input_kind IN (
        'character_alias', 'player_identity_mapping', 'player_character_mapping'
    )),
    changed_at TEXT NOT NULL
);
CREATE INDEX ix_attribution_review_input_changes_time
    ON attribution_review_input_changes(changed_at, input_kind);

CREATE TRIGGER attribution_review_alias_insert
AFTER INSERT ON character_aliases
BEGIN
    INSERT INTO attribution_review_input_changes(input_kind, changed_at)
    VALUES ('character_alias', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));
END;

CREATE TRIGGER attribution_review_alias_update
AFTER UPDATE ON character_aliases
BEGIN
    INSERT INTO attribution_review_input_changes(input_kind, changed_at)
    VALUES ('character_alias', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));
END;

CREATE TRIGGER attribution_review_alias_delete
AFTER DELETE ON character_aliases
BEGIN
    INSERT INTO attribution_review_input_changes(input_kind, changed_at)
    VALUES ('character_alias', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));
END;

CREATE TRIGGER attribution_review_player_identity_insert
AFTER INSERT ON player_discord_identities
BEGIN
    INSERT INTO attribution_review_input_changes(input_kind, changed_at)
    VALUES ('player_identity_mapping', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));
END;

CREATE TRIGGER attribution_review_player_identity_update
AFTER UPDATE ON player_discord_identities
BEGIN
    INSERT INTO attribution_review_input_changes(input_kind, changed_at)
    VALUES ('player_identity_mapping', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));
END;

CREATE TRIGGER attribution_review_player_identity_delete
AFTER DELETE ON player_discord_identities
BEGIN
    INSERT INTO attribution_review_input_changes(input_kind, changed_at)
    VALUES ('player_identity_mapping', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));
END;

CREATE TRIGGER attribution_review_player_character_insert
AFTER INSERT ON player_characters
BEGIN
    INSERT INTO attribution_review_input_changes(input_kind, changed_at)
    VALUES ('player_character_mapping', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));
END;

CREATE TRIGGER attribution_review_player_character_update
AFTER UPDATE ON player_characters
BEGIN
    INSERT INTO attribution_review_input_changes(input_kind, changed_at)
    VALUES ('player_character_mapping', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));
END;

CREATE TRIGGER attribution_review_player_character_delete
AFTER DELETE ON player_characters
BEGIN
    INSERT INTO attribution_review_input_changes(input_kind, changed_at)
    VALUES ('player_character_mapping', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'));
END;
