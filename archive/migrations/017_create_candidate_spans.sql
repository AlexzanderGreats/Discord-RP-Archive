CREATE TABLE segmentation_runs (
    segmentation_run_id INTEGER PRIMARY KEY,
    boundary_detection_run_id INTEGER,
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
    FOREIGN KEY(boundary_detection_run_id)
        REFERENCES boundary_detection_runs(detection_run_id) ON DELETE NO ACTION,
    CHECK(
        (status = 'running' AND completed_at IS NULL AND error_details IS NULL)
        OR (status = 'completed' AND completed_at IS NOT NULL AND error_details IS NULL)
        OR (status = 'failed' AND completed_at IS NOT NULL AND error_details IS NOT NULL)
    )
);
CREATE INDEX ix_segmentation_runs_status ON segmentation_runs(status, started_at);
CREATE INDEX ix_segmentation_runs_fingerprint ON segmentation_runs(input_fingerprint);
CREATE INDEX ix_segmentation_runs_boundary_detection ON segmentation_runs(boundary_detection_run_id);

CREATE TABLE candidate_spans (
    candidate_span_id INTEGER PRIMARY KEY,
    segmentation_run_id INTEGER NOT NULL,
    span_position INTEGER NOT NULL CHECK(span_position >= 0),
    candidate_key TEXT NOT NULL,
    server_id INTEGER,
    channel_id INTEGER,
    source_id INTEGER,
    first_message_observation_id INTEGER NOT NULL,
    last_message_observation_id INTEGER NOT NULL,
    start_timestamp_unix REAL,
    end_timestamp_unix REAL,
    start_reason TEXT NOT NULL CHECK(
        start_reason IN ('scope_start', 'hard_boundary', 'inactivity_gap')
    ),
    start_boundary_evidence_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(segmentation_run_id) REFERENCES segmentation_runs(segmentation_run_id) ON DELETE NO ACTION,
    FOREIGN KEY(server_id) REFERENCES servers(server_id) ON DELETE NO ACTION,
    FOREIGN KEY(channel_id) REFERENCES channels(channel_id) ON DELETE NO ACTION,
    FOREIGN KEY(source_id) REFERENCES sources(source_id) ON DELETE NO ACTION,
    FOREIGN KEY(first_message_observation_id) REFERENCES messages(message_observation_id) ON DELETE NO ACTION,
    FOREIGN KEY(last_message_observation_id) REFERENCES messages(message_observation_id) ON DELETE NO ACTION,
    UNIQUE(segmentation_run_id, span_position),
    UNIQUE(segmentation_run_id, candidate_key)
);
CREATE INDEX ix_candidate_spans_run_scope
    ON candidate_spans(segmentation_run_id, server_id, channel_id, source_id, span_position);
CREATE INDEX ix_candidate_spans_messages
    ON candidate_spans(first_message_observation_id, last_message_observation_id);

CREATE TABLE candidate_span_messages (
    candidate_span_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 0),
    message_observation_id INTEGER NOT NULL,
    assignment_method TEXT NOT NULL CHECK(assignment_method IN ('automatic', 'manual')),
    assignment_status TEXT NOT NULL CHECK(assignment_status IN ('candidate', 'confirmed', 'rejected')),
    evidence_json TEXT NOT NULL,
    PRIMARY KEY(candidate_span_id, position),
    UNIQUE(candidate_span_id, message_observation_id),
    FOREIGN KEY(candidate_span_id) REFERENCES candidate_spans(candidate_span_id) ON DELETE NO ACTION,
    FOREIGN KEY(message_observation_id) REFERENCES messages(message_observation_id) ON DELETE NO ACTION
);
CREATE INDEX ix_candidate_span_messages_message
    ON candidate_span_messages(message_observation_id, candidate_span_id);

CREATE TABLE candidate_reply_links (
    candidate_reply_link_id INTEGER PRIMARY KEY,
    segmentation_run_id INTEGER NOT NULL,
    source_message_observation_id INTEGER NOT NULL,
    target_observation_ids_json TEXT NOT NULL,
    target_discord_message_id TEXT NOT NULL,
    resolution TEXT NOT NULL CHECK(
        resolution IN ('resolved', 'cross_scope', 'unresolved', 'ambiguous')
    ),
    same_scope INTEGER NOT NULL CHECK(same_scope IN (0, 1)),
    FOREIGN KEY(segmentation_run_id) REFERENCES segmentation_runs(segmentation_run_id) ON DELETE NO ACTION,
    FOREIGN KEY(source_message_observation_id) REFERENCES messages(message_observation_id) ON DELETE NO ACTION,
    UNIQUE(segmentation_run_id, source_message_observation_id, target_discord_message_id)
);
CREATE INDEX ix_candidate_reply_links_source
    ON candidate_reply_links(source_message_observation_id, segmentation_run_id);
CREATE INDEX ix_candidate_reply_links_resolution
    ON candidate_reply_links(segmentation_run_id, resolution);

CREATE TABLE segmentation_boundary_decisions (
    segmentation_run_id INTEGER NOT NULL,
    left_message_observation_id INTEGER NOT NULL,
    right_message_observation_id INTEGER NOT NULL,
    was_split INTEGER NOT NULL CHECK(was_split IN (0, 1)),
    split_reason TEXT CHECK(
        split_reason IS NULL
        OR split_reason IN ('hard_boundary', 'inactivity_gap')
    ),
    boundary_candidate_json TEXT,
    PRIMARY KEY(segmentation_run_id, left_message_observation_id, right_message_observation_id),
    FOREIGN KEY(segmentation_run_id) REFERENCES segmentation_runs(segmentation_run_id) ON DELETE NO ACTION,
    FOREIGN KEY(left_message_observation_id) REFERENCES messages(message_observation_id) ON DELETE NO ACTION,
    FOREIGN KEY(right_message_observation_id) REFERENCES messages(message_observation_id) ON DELETE NO ACTION,
    CHECK(
        (was_split = 1 AND split_reason IS NOT NULL)
        OR (was_split = 0 AND split_reason IS NULL)
    )
);
CREATE INDEX ix_segmentation_boundary_decisions_right
    ON segmentation_boundary_decisions(right_message_observation_id, was_split);
