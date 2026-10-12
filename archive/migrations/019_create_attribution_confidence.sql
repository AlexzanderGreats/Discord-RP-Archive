CREATE TABLE attribution_confidence_runs (
    attribution_confidence_run_id INTEGER PRIMARY KEY,
    identity_inference_run_id INTEGER NOT NULL,
    ruleset_version TEXT NOT NULL,
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
    ),
    FOREIGN KEY(identity_inference_run_id)
        REFERENCES identity_inference_runs(identity_inference_run_id) ON DELETE NO ACTION
);
CREATE INDEX ix_attribution_confidence_runs_identity
    ON attribution_confidence_runs(identity_inference_run_id, attribution_confidence_run_id);
CREATE INDEX ix_attribution_confidence_runs_fingerprint
    ON attribution_confidence_runs(input_fingerprint);
CREATE INDEX ix_attribution_confidence_runs_status
    ON attribution_confidence_runs(status, started_at);

CREATE TABLE attribution_confidence_candidates (
    attribution_confidence_candidate_id INTEGER PRIMARY KEY,
    attribution_confidence_run_id INTEGER NOT NULL,
    message_observation_id INTEGER NOT NULL,
    candidate_kind TEXT NOT NULL CHECK(candidate_kind IN (
        'character', 'provisional_identity', 'unresolved'
    )),
    character_id INTEGER,
    provisional_identity_candidate_id INTEGER,
    candidate_label TEXT,
    candidate_rank INTEGER NOT NULL CHECK(candidate_rank >= 1),
    score INTEGER,
    score_margin INTEGER,
    classification TEXT NOT NULL CHECK(classification IN (
        'high_confidence', 'review_required', 'ambiguous', 'unresolved',
        'conflicted', 'human_decided'
    )),
    review_priority TEXT NOT NULL CHECK(review_priority IN ('low', 'normal', 'high')),
    mandatory_review INTEGER NOT NULL CHECK(mandatory_review IN (0, 1)),
    review_reasons_json TEXT NOT NULL,
    decision_status TEXT NOT NULL CHECK(decision_status IN (
        'none', 'pending', 'accepted', 'rejected', 'conflicted'
    )),
    source_attribution_ids_json TEXT NOT NULL,
    CHECK(
        (candidate_kind = 'character'
            AND character_id IS NOT NULL
            AND provisional_identity_candidate_id IS NULL
            AND score IS NOT NULL)
        OR (candidate_kind = 'provisional_identity'
            AND character_id IS NULL
            AND provisional_identity_candidate_id IS NOT NULL
            AND score IS NULL)
        OR (candidate_kind = 'unresolved'
            AND character_id IS NULL
            AND provisional_identity_candidate_id IS NULL
            AND score IS NULL)
    ),
    FOREIGN KEY(attribution_confidence_run_id)
        REFERENCES attribution_confidence_runs(attribution_confidence_run_id) ON DELETE NO ACTION,
    FOREIGN KEY(message_observation_id)
        REFERENCES messages(message_observation_id) ON DELETE NO ACTION,
    FOREIGN KEY(character_id) REFERENCES characters(character_id) ON DELETE NO ACTION,
    FOREIGN KEY(provisional_identity_candidate_id)
        REFERENCES provisional_identity_candidates(candidate_id) ON DELETE NO ACTION,
    UNIQUE(attribution_confidence_run_id, message_observation_id, candidate_rank)
);
CREATE INDEX ix_attribution_confidence_candidates_message
    ON attribution_confidence_candidates(message_observation_id, attribution_confidence_run_id);
CREATE INDEX ix_attribution_confidence_candidates_character
    ON attribution_confidence_candidates(character_id, classification, mandatory_review);
CREATE INDEX ix_attribution_confidence_candidates_review
    ON attribution_confidence_candidates(attribution_confidence_run_id, mandatory_review, review_priority);

CREATE TABLE attribution_confidence_evidence (
    attribution_confidence_evidence_id INTEGER PRIMARY KEY,
    attribution_confidence_candidate_id INTEGER NOT NULL,
    evidence_type TEXT NOT NULL,
    evidence_strength TEXT NOT NULL CHECK(evidence_strength IN (
        'explicit', 'human_reviewed', 'context'
    )),
    direction TEXT NOT NULL CHECK(direction IN ('supporting', 'contradictory', 'context')),
    weight INTEGER NOT NULL CHECK(weight >= 0),
    contribution INTEGER NOT NULL,
    source_message_ids_json TEXT NOT NULL,
    source_identity_inference_run_id INTEGER,
    source_identity_evidence_id INTEGER,
    source_attribution_id INTEGER,
    source_review_event_ids_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    CHECK(
        (direction = 'supporting' AND contribution >= 0)
        OR (direction = 'contradictory' AND contribution <= 0)
        OR (direction = 'context' AND contribution = 0)
    ),
    FOREIGN KEY(attribution_confidence_candidate_id)
        REFERENCES attribution_confidence_candidates(attribution_confidence_candidate_id) ON DELETE NO ACTION,
    FOREIGN KEY(source_identity_inference_run_id)
        REFERENCES identity_inference_runs(identity_inference_run_id) ON DELETE NO ACTION,
    FOREIGN KEY(source_identity_evidence_id)
        REFERENCES identity_candidate_evidence(evidence_id) ON DELETE NO ACTION,
    FOREIGN KEY(source_attribution_id)
        REFERENCES message_attributions(attribution_id) ON DELETE NO ACTION
);
CREATE INDEX ix_attribution_confidence_evidence_candidate
    ON attribution_confidence_evidence(attribution_confidence_candidate_id);
CREATE INDEX ix_attribution_confidence_evidence_identity
    ON attribution_confidence_evidence(source_identity_evidence_id);
CREATE INDEX ix_attribution_confidence_evidence_attribution
    ON attribution_confidence_evidence(source_attribution_id);
