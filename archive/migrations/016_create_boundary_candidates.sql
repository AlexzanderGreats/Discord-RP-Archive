CREATE TABLE boundary_detection_runs (
    detection_run_id INTEGER PRIMARY KEY,
    run_type TEXT NOT NULL CHECK(run_type IN ('full', 'incremental')),
    algorithm_version TEXT NOT NULL,
    configuration_json TEXT NOT NULL,
    configuration_sha256 TEXT NOT NULL,
    input_scope_json TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
    statistics_json TEXT NOT NULL DEFAULT '{}',
    error_details TEXT,
    CHECK(
        (status = 'running' AND completed_at IS NULL AND error_details IS NULL)
        OR (status = 'completed' AND completed_at IS NOT NULL AND error_details IS NULL)
        OR (status = 'failed' AND completed_at IS NOT NULL AND error_details IS NOT NULL)
    )
);
CREATE INDEX ix_boundary_detection_runs_status ON boundary_detection_runs(status, started_at);
CREATE INDEX ix_boundary_detection_runs_algorithm ON boundary_detection_runs(algorithm_version, started_at);
CREATE INDEX ix_boundary_detection_runs_fingerprint ON boundary_detection_runs(input_fingerprint);

CREATE TABLE boundary_detection_run_messages (
    detection_run_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    message_observation_id INTEGER NOT NULL,
    PRIMARY KEY(detection_run_id, position),
    UNIQUE(detection_run_id, message_observation_id),
    FOREIGN KEY(detection_run_id) REFERENCES boundary_detection_runs(detection_run_id) ON DELETE NO ACTION,
    FOREIGN KEY(message_observation_id) REFERENCES messages(message_observation_id) ON DELETE NO ACTION
);
CREATE INDEX ix_boundary_detection_run_messages_message
    ON boundary_detection_run_messages(message_observation_id, detection_run_id);

CREATE TABLE event_boundary_candidates (
    boundary_candidate_id INTEGER PRIMARY KEY,
    detection_run_id INTEGER NOT NULL,
    left_message_observation_id INTEGER NOT NULL,
    right_message_observation_id INTEGER NOT NULL,
    boundary_type TEXT NOT NULL CHECK(boundary_type IN ('hard', 'soft')),
    raw_score REAL NOT NULL,
    classification TEXT NOT NULL CHECK(
        classification IN ('soft_candidate', 'strong_candidate', 'hard_candidate')
    ),
    supporting_evidence_json TEXT NOT NULL,
    contradictory_evidence_json TEXT NOT NULL,
    contributions_json TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK(
        review_status IN ('pending', 'accepted', 'rejected', 'deferred')
    ),
    created_at TEXT NOT NULL,
    FOREIGN KEY(detection_run_id) REFERENCES boundary_detection_runs(detection_run_id) ON DELETE NO ACTION,
    FOREIGN KEY(left_message_observation_id) REFERENCES messages(message_observation_id) ON DELETE NO ACTION,
    FOREIGN KEY(right_message_observation_id) REFERENCES messages(message_observation_id) ON DELETE NO ACTION,
    CHECK(left_message_observation_id <> right_message_observation_id),
    UNIQUE(detection_run_id, left_message_observation_id, right_message_observation_id)
);
CREATE INDEX ix_event_boundary_candidates_run ON event_boundary_candidates(detection_run_id, classification);
CREATE INDEX ix_event_boundary_candidates_pair
    ON event_boundary_candidates(left_message_observation_id, right_message_observation_id);
CREATE INDEX ix_event_boundary_candidates_review
    ON event_boundary_candidates(review_status, created_at);
