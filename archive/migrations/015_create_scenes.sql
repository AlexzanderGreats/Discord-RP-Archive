CREATE TABLE arcs (
    arc_id INTEGER PRIMARY KEY,
    arc_name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_arcs_name ON arcs(arc_name);

CREATE TABLE scenes (
    scene_id INTEGER PRIMARY KEY,
    arc_id INTEGER,
    scene_name TEXT NOT NULL,
    description TEXT,
    fictional_time TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(arc_id) REFERENCES arcs(arc_id) ON DELETE NO ACTION
);
CREATE INDEX ix_scenes_arc ON scenes(arc_id);
CREATE INDEX ix_scenes_name ON scenes(scene_name);

CREATE TABLE scene_messages (
    scene_message_id INTEGER PRIMARY KEY,
    scene_id INTEGER NOT NULL,
    message_observation_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    added_at TEXT NOT NULL,
    added_by TEXT,
    notes TEXT,
    FOREIGN KEY(scene_id) REFERENCES scenes(scene_id) ON DELETE NO ACTION,
    FOREIGN KEY(message_observation_id) REFERENCES messages(message_observation_id) ON DELETE NO ACTION,
    UNIQUE(scene_id, message_observation_id),
    UNIQUE(scene_id, position)
);
CREATE INDEX ix_scene_messages_scene_position ON scene_messages(scene_id, position);
CREATE INDEX ix_scene_messages_message ON scene_messages(message_observation_id);

CREATE TABLE scene_participants (
    scene_participant_id INTEGER PRIMARY KEY,
    scene_id INTEGER NOT NULL,
    character_id INTEGER,
    player_id INTEGER,
    added_at TEXT NOT NULL,
    added_by TEXT,
    notes TEXT,
    CHECK(character_id IS NOT NULL OR player_id IS NOT NULL),
    FOREIGN KEY(scene_id) REFERENCES scenes(scene_id) ON DELETE NO ACTION,
    FOREIGN KEY(character_id) REFERENCES characters(character_id) ON DELETE NO ACTION,
    FOREIGN KEY(player_id) REFERENCES players(player_id) ON DELETE NO ACTION
);
CREATE UNIQUE INDEX ux_scene_participants_identity
    ON scene_participants(scene_id, COALESCE(character_id, -1), COALESCE(player_id, -1));
CREATE INDEX ix_scene_participants_scene ON scene_participants(scene_id);
CREATE INDEX ix_scene_participants_character ON scene_participants(character_id);
CREATE INDEX ix_scene_participants_player ON scene_participants(player_id);

CREATE VIEW scene_channels AS
SELECT DISTINCT sm.scene_id, m.channel_id
FROM scene_messages sm
JOIN messages m ON m.message_observation_id = sm.message_observation_id
WHERE m.channel_id IS NOT NULL;