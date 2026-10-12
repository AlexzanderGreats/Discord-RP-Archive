CREATE TABLE attribution_review_events (
    attribution_review_event_id INTEGER PRIMARY KEY,
    review_group_id TEXT NOT NULL,
    message_observation_id INTEGER NOT NULL,
    action TEXT NOT NULL CHECK(action IN (
        'confirm',
        'assign_character',
        'reject',
        'leave_unresolved',
        'link_provisional',
        'split_evidence',
        'reassign_evidence',
        'exclude_evidence',
        'merge_candidates'
    )),
    outcome TEXT NOT NULL CHECK(outcome IN (
        'accepted', 'rejected', 'unresolved', 'conflicted',
        'identity_linked', 'evidence_adjusted'
    )),
    actor TEXT NOT NULL,
    happened_at TEXT NOT NULL,
    note TEXT,
    confidence_run_id INTEGER,
    confidence_candidate_id INTEGER,
    character_id INTEGER,
    provisional_candidate_id INTEGER,
    target_candidate_id INTEGER,
    attribution_id INTEGER,
    identity_review_event_id INTEGER,
    source_evidence_fingerprint TEXT,
    detail_json TEXT NOT NULL,
    FOREIGN KEY(message_observation_id)
        REFERENCES messages(message_observation_id) ON DELETE NO ACTION,
    FOREIGN KEY(confidence_run_id)
        REFERENCES attribution_confidence_runs(attribution_confidence_run_id) ON DELETE NO ACTION,
    FOREIGN KEY(confidence_candidate_id)
        REFERENCES attribution_confidence_candidates(attribution_confidence_candidate_id) ON DELETE NO ACTION,
    FOREIGN KEY(character_id) REFERENCES characters(character_id) ON DELETE NO ACTION,
    FOREIGN KEY(provisional_candidate_id)
        REFERENCES provisional_identity_candidates(candidate_id) ON DELETE NO ACTION,
    FOREIGN KEY(target_candidate_id)
        REFERENCES provisional_identity_candidates(candidate_id) ON DELETE NO ACTION,
    FOREIGN KEY(attribution_id) REFERENCES message_attributions(attribution_id) ON DELETE NO ACTION,
    FOREIGN KEY(identity_review_event_id)
        REFERENCES identity_candidate_review_events(review_event_id) ON DELETE NO ACTION
);
CREATE INDEX ix_attribution_review_events_message
    ON attribution_review_events(message_observation_id, attribution_review_event_id);
CREATE INDEX ix_attribution_review_events_group
    ON attribution_review_events(review_group_id, attribution_review_event_id);
CREATE INDEX ix_attribution_review_events_candidate
    ON attribution_review_events(confidence_candidate_id, attribution_review_event_id);
CREATE INDEX ix_attribution_review_events_provisional
    ON attribution_review_events(provisional_candidate_id, attribution_review_event_id);

CREATE TRIGGER attribution_review_events_no_update
BEFORE UPDATE ON attribution_review_events
BEGIN
    SELECT RAISE(ABORT, 'attribution review events are immutable');
END;

CREATE TRIGGER attribution_review_events_no_delete
BEFORE DELETE ON attribution_review_events
BEGIN
    SELECT RAISE(ABORT, 'attribution review events are immutable');
END;

CREATE TABLE attribution_review_decisions (
    attribution_review_decision_id INTEGER PRIMARY KEY,
    message_observation_id INTEGER NOT NULL,
    candidate_kind TEXT NOT NULL CHECK(candidate_kind IN (
        'character', 'provisional_identity', 'unresolved'
    )),
    character_id INTEGER,
    provisional_candidate_id INTEGER,
    status TEXT NOT NULL CHECK(status IN (
        'accepted', 'rejected', 'unresolved', 'conflicted',
        'identity_linked', 'evidence_adjusted', 'superseded'
    )),
    latest_event_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(
        (candidate_kind = 'character'
            AND character_id IS NOT NULL AND provisional_candidate_id IS NULL)
        OR (candidate_kind = 'provisional_identity'
            AND character_id IS NULL AND provisional_candidate_id IS NOT NULL)
        OR (candidate_kind = 'unresolved'
            AND character_id IS NULL AND provisional_candidate_id IS NULL)
    ),
    FOREIGN KEY(message_observation_id)
        REFERENCES messages(message_observation_id) ON DELETE NO ACTION,
    FOREIGN KEY(character_id) REFERENCES characters(character_id) ON DELETE NO ACTION,
    FOREIGN KEY(provisional_candidate_id)
        REFERENCES provisional_identity_candidates(candidate_id) ON DELETE NO ACTION,
    FOREIGN KEY(latest_event_id)
        REFERENCES attribution_review_events(attribution_review_event_id) ON DELETE NO ACTION
);
CREATE UNIQUE INDEX ux_attribution_review_decisions_character
    ON attribution_review_decisions(message_observation_id, character_id)
    WHERE candidate_kind = 'character';
CREATE UNIQUE INDEX ux_attribution_review_decisions_provisional
    ON attribution_review_decisions(message_observation_id, provisional_candidate_id)
    WHERE candidate_kind = 'provisional_identity';
CREATE UNIQUE INDEX ux_attribution_review_decisions_unresolved
    ON attribution_review_decisions(message_observation_id)
    WHERE candidate_kind = 'unresolved';
CREATE UNIQUE INDEX ux_attribution_review_decisions_one_accepted
    ON attribution_review_decisions(message_observation_id)
    WHERE status = 'accepted';
CREATE INDEX ix_attribution_review_decisions_status
    ON attribution_review_decisions(status, updated_at, message_observation_id);
CREATE INDEX ix_attribution_review_decisions_character
    ON attribution_review_decisions(character_id, status, message_observation_id);
