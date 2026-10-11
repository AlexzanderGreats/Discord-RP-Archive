CREATE TABLE event_candidate_runs (
    event_candidate_run_id INTEGER PRIMARY KEY,
    algorithm_version TEXT NOT NULL,
    configuration_json TEXT NOT NULL,
    configuration_sha256 TEXT NOT NULL,
    input_scope_json TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    run_type TEXT NOT NULL CHECK(run_type IN ('full', 'incremental')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
    statistics_json TEXT NOT NULL DEFAULT '{}',
    error_details TEXT,
    UNIQUE(algorithm_version, input_fingerprint)
);
CREATE INDEX ix_event_candidate_runs_status ON event_candidate_runs(status, started_at);
CREATE INDEX ix_event_candidate_runs_fingerprint ON event_candidate_runs(algorithm_version, input_fingerprint);

CREATE TABLE event_candidates (
    event_candidate_id INTEGER PRIMARY KEY,
    event_candidate_run_id INTEGER NOT NULL,
    candidate_key TEXT NOT NULL,
    title TEXT,
    description TEXT,
    notes TEXT,
    status TEXT NOT NULL CHECK(status IN ('proposed', 'accepted', 'rejected', 'deferred', 'stale')),
    review_state TEXT NOT NULL CHECK(review_state IN ('unreviewed', 'accepted', 'rejected', 'deferred')),
    server_id INTEGER,
    channel_id INTEGER,
    source_id INTEGER,
    start_timestamp_unix REAL,
    end_timestamp_unix REAL,
    message_count INTEGER NOT NULL DEFAULT 0,
    candidate_span_count INTEGER NOT NULL DEFAULT 0,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    stale_reason TEXT,
    accepted_scene_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(event_candidate_run_id) REFERENCES event_candidate_runs(event_candidate_run_id) ON DELETE NO ACTION,
    FOREIGN KEY(server_id) REFERENCES servers(server_id) ON DELETE NO ACTION,
    FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE NO ACTION,
    FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE NO ACTION,
    FOREIGN KEY(accepted_scene_id) REFERENCES scenes(scene_id) ON DELETE NO ACTION,
    UNIQUE(event_candidate_run_id, candidate_key)
);
CREATE INDEX ix_event_candidates_status ON event_candidates(status, updated_at);
CREATE INDEX ix_event_candidates_scope ON event_candidates(server_id, channel_id, source_id, start_timestamp_unix);
CREATE INDEX ix_event_candidates_scene ON event_candidates(accepted_scene_id);

CREATE TABLE event_candidate_members (
    event_candidate_member_id INTEGER PRIMARY KEY,
    event_candidate_id INTEGER NOT NULL,
    candidate_span_id INTEGER,
    message_observation_id INTEGER,
    position INTEGER NOT NULL CHECK(position >= 0),
    membership_kind TEXT NOT NULL CHECK(membership_kind IN ('span', 'message')),
    evidence_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(event_candidate_id, candidate_span_id),
    UNIQUE(event_candidate_id, message_observation_id),
    FOREIGN KEY(event_candidate_id) REFERENCES event_candidates(event_candidate_id) ON DELETE NO ACTION,
    FOREIGN KEY(candidate_span_id) REFERENCES candidate_spans(candidate_span_id) ON DELETE NO ACTION,
    FOREIGN KEY(message_observation_id) REFERENCES messages(message_observation_id) ON DELETE NO ACTION
);
CREATE INDEX ix_event_candidate_members_message ON event_candidate_members(message_observation_id, event_candidate_id);
CREATE INDEX ix_event_candidate_members_span ON event_candidate_members(candidate_span_id, event_candidate_id);

CREATE TABLE event_candidate_arc_memberships (
    event_candidate_id INTEGER NOT NULL,
    arc_id INTEGER NOT NULL,
    added_at TEXT NOT NULL,
    added_by TEXT,
    notes TEXT,
    PRIMARY KEY(event_candidate_id, arc_id),
    FOREIGN KEY(event_candidate_id) REFERENCES event_candidates(event_candidate_id) ON DELETE NO ACTION,
    FOREIGN KEY(arc_id) REFERENCES arcs(arc_id) ON DELETE NO ACTION
);
CREATE INDEX ix_event_candidate_arc_memberships_arc ON event_candidate_arc_memberships(arc_id, event_candidate_id);

CREATE TABLE event_candidate_review_events (
    review_event_id INTEGER PRIMARY KEY,
    event_candidate_id INTEGER NOT NULL,
    reviewer TEXT NOT NULL,
    action TEXT NOT NULL CHECK(
        action IN (
            'accepted', 'rejected', 'deferred', 'updated', 'stale',
            'arc_assigned', 'arc_removed', 'group_adjusted'
        )
    ),
    prior_status TEXT,
    new_status TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(event_candidate_id) REFERENCES event_candidates(event_candidate_id) ON DELETE NO ACTION
);
CREATE INDEX ix_event_candidate_review_events_candidate ON event_candidate_review_events(event_candidate_id, created_at);
