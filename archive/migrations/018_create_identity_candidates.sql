CREATE TABLE identity_inference_runs (
    identity_inference_run_id INTEGER PRIMARY KEY,
    algorithm_version TEXT NOT NULL,
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
CREATE INDEX ix_identity_inference_runs_fingerprint
    ON identity_inference_runs(input_fingerprint, identity_inference_run_id);
CREATE INDEX ix_identity_inference_runs_status
    ON identity_inference_runs(status, started_at);

CREATE TABLE provisional_identity_candidates (
    candidate_id INTEGER PRIMARY KEY,
    candidate_key TEXT NOT NULL UNIQUE,
    server_id INTEGER,
    display_label TEXT NOT NULL,
    normalized_label_key TEXT NOT NULL,
    resolution_status TEXT NOT NULL CHECK(resolution_status IN ('unresolved', 'linked')),
    linked_character_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(
        (resolution_status = 'linked' AND linked_character_id IS NOT NULL)
        OR (resolution_status = 'unresolved' AND linked_character_id IS NULL)
    ),
    FOREIGN KEY(linked_character_id) REFERENCES characters(character_id) ON DELETE NO ACTION,
    FOREIGN KEY(server_id) REFERENCES servers(server_id) ON DELETE NO ACTION
);
CREATE INDEX ix_provisional_identity_candidates_label
    ON provisional_identity_candidates(server_id, normalized_label_key, candidate_id);
CREATE INDEX ix_provisional_identity_candidates_status
    ON provisional_identity_candidates(resolution_status, candidate_id);

CREATE TABLE identity_candidate_evidence (
    evidence_id INTEGER PRIMARY KEY,
    identity_inference_run_id INTEGER NOT NULL,
    candidate_id INTEGER,
    server_id INTEGER,
    message_observation_id INTEGER NOT NULL,
    observed_label TEXT NOT NULL,
    normalized_label_key TEXT NOT NULL,
    evidence_kind TEXT NOT NULL CHECK(evidence_kind IN (
        'provisional_label',
        'registered_character_match',
        'ambiguous_registered_character_match'
    )),
    registered_character_ids_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    CHECK(
        (evidence_kind = 'provisional_label' AND candidate_id IS NOT NULL)
        OR (evidence_kind != 'provisional_label' AND candidate_id IS NULL)
    ),
    FOREIGN KEY(identity_inference_run_id)
        REFERENCES identity_inference_runs(identity_inference_run_id) ON DELETE NO ACTION,
    FOREIGN KEY(candidate_id)
        REFERENCES provisional_identity_candidates(candidate_id) ON DELETE NO ACTION,
    FOREIGN KEY(server_id) REFERENCES servers(server_id) ON DELETE NO ACTION,
    FOREIGN KEY(message_observation_id)
        REFERENCES messages(message_observation_id) ON DELETE NO ACTION,
    UNIQUE(identity_inference_run_id, message_observation_id)
);
CREATE INDEX ix_identity_candidate_evidence_candidate
    ON identity_candidate_evidence(candidate_id, message_observation_id);
CREATE INDEX ix_identity_candidate_evidence_message
    ON identity_candidate_evidence(message_observation_id, identity_inference_run_id);
CREATE INDEX ix_identity_candidate_evidence_kind
    ON identity_candidate_evidence(evidence_kind, identity_inference_run_id);

CREATE TABLE identity_candidate_review_events (
    review_event_id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN (
        'link_to_character',
        'leave_unresolved',
        'split_evidence',
        'reassign_evidence',
        'exclude_evidence'
    )),
    actor TEXT NOT NULL,
    happened_at TEXT NOT NULL,
    linked_character_id INTEGER,
    target_candidate_id INTEGER,
    affected_message_ids_json TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    CHECK(
        (decision = 'link_to_character'
            AND linked_character_id IS NOT NULL AND target_candidate_id IS NULL)
        OR (decision = 'leave_unresolved'
            AND linked_character_id IS NULL AND target_candidate_id IS NULL)
        OR (decision IN ('split_evidence', 'reassign_evidence')
            AND linked_character_id IS NULL AND target_candidate_id IS NOT NULL)
        OR (decision = 'exclude_evidence'
            AND linked_character_id IS NULL AND target_candidate_id IS NULL)
    ),
    FOREIGN KEY(candidate_id)
        REFERENCES provisional_identity_candidates(candidate_id) ON DELETE NO ACTION,
    FOREIGN KEY(linked_character_id)
        REFERENCES characters(character_id) ON DELETE NO ACTION,
    FOREIGN KEY(target_candidate_id)
        REFERENCES provisional_identity_candidates(candidate_id) ON DELETE NO ACTION
);
CREATE INDEX ix_identity_candidate_review_events_candidate
    ON identity_candidate_review_events(candidate_id, review_event_id);
CREATE INDEX ix_identity_candidate_review_events_target
    ON identity_candidate_review_events(target_candidate_id, review_event_id);

CREATE TRIGGER identity_candidate_review_events_no_update
BEFORE UPDATE ON identity_candidate_review_events
BEGIN
    SELECT RAISE(ABORT, 'identity candidate review events are immutable');
END;

CREATE TRIGGER identity_candidate_review_events_no_delete
BEFORE DELETE ON identity_candidate_review_events
BEGIN
    SELECT RAISE(ABORT, 'identity candidate review events are immutable');
END;

CREATE TABLE identity_candidate_evidence_overrides (
    message_observation_id INTEGER PRIMARY KEY,
    candidate_id INTEGER,
    is_excluded INTEGER NOT NULL CHECK(is_excluded IN (0, 1)),
    review_event_id INTEGER NOT NULL,
    CHECK(
        (is_excluded = 1 AND candidate_id IS NULL)
        OR (is_excluded = 0 AND candidate_id IS NOT NULL)
    ),
    FOREIGN KEY(message_observation_id)
        REFERENCES messages(message_observation_id) ON DELETE NO ACTION,
    FOREIGN KEY(candidate_id)
        REFERENCES provisional_identity_candidates(candidate_id) ON DELETE NO ACTION,
    FOREIGN KEY(review_event_id)
        REFERENCES identity_candidate_review_events(review_event_id) ON DELETE NO ACTION
);
CREATE INDEX ix_identity_candidate_evidence_overrides_candidate
    ON identity_candidate_evidence_overrides(candidate_id, message_observation_id);
