from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from archive.attribution_confidence import (
    DEFAULT_ATTRIBUTION_CONFIDENCE_CONFIG,
    AttributionConfidenceConfig,
    AttributionConfidenceResult,
    EvidenceContribution,
    score_attribution_evidence,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _positive_id(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _fetch_evidence(
    connection: sqlite3.Connection,
    run_id: int,
    message_ids: Sequence[int],
) -> list[sqlite3.Row]:
    if not message_ids:
        return []
    placeholders = ",".join("?" for _ in message_ids)
    return connection.execute(
        f"""SELECT evidence.evidence_id,
                   evidence.identity_inference_run_id,
                   COALESCE(override.candidate_id, evidence.candidate_id) AS candidate_id,
                   evidence.server_id,
                   evidence.message_observation_id,
                   evidence.observed_label,
                   evidence.normalized_label_key,
                   evidence.evidence_kind,
                   evidence.registered_character_ids_json,
                   evidence.evidence_json,
                   provisional.display_label AS provisional_label,
                   provisional.linked_character_id,
                   linked_character.character_name AS character_label,
                   COALESCE((
                       SELECT json_group_array(review_event_id)
                       FROM identity_candidate_review_events review_event
                       WHERE review_event.candidate_id = provisional.candidate_id
                         AND review_event.decision = 'link_to_character'
                         AND review_event.linked_character_id = provisional.linked_character_id
                   ), '[]') AS candidate_link_review_event_ids_json
            FROM identity_candidate_evidence evidence
            LEFT JOIN identity_candidate_evidence_overrides override
              ON override.message_observation_id = evidence.message_observation_id
            LEFT JOIN provisional_identity_candidates provisional
              ON provisional.candidate_id = COALESCE(override.candidate_id, evidence.candidate_id)
            LEFT JOIN characters linked_character
              ON linked_character.character_id = provisional.linked_character_id
            WHERE evidence.identity_inference_run_id = ?
              AND evidence.message_observation_id IN ({placeholders})
              AND COALESCE(override.is_excluded, 0) = 0
            ORDER BY evidence.message_observation_id, evidence.evidence_id""",
        (run_id, *message_ids),
    ).fetchall()


def _fetch_attributions(
    connection: sqlite3.Connection,
    message_ids: Sequence[int],
) -> list[sqlite3.Row]:
    if not message_ids:
        return []
    placeholders = ",".join("?" for _ in message_ids)
    return connection.execute(
        f"""SELECT attribution.attribution_id, attribution.message_observation_id,
                   attribution.character_id, attribution.player_id,
                   attribution.method, attribution.status,
                   character.character_name,
                   COALESCE((
                       SELECT json_group_array(event.event_id)
                       FROM attribution_events event
                       WHERE event.attribution_id = attribution.attribution_id
                         AND event.event_type IN ('accepted', 'rejected')
                   ), '[]') AS review_event_ids_json
            FROM message_attributions attribution
            LEFT JOIN characters character ON character.character_id = attribution.character_id
            WHERE attribution.message_observation_id IN ({placeholders})
              AND attribution.status IN ('accepted', 'rejected', 'pending')
            ORDER BY attribution.message_observation_id, attribution.attribution_id""",
        tuple(message_ids),
    ).fetchall()


def _input_fingerprint(
    identity_inference_run_id: int,
    message_ids: Sequence[int],
    identity_evidence: Sequence[Mapping[str, Any]],
    attributions: Sequence[Mapping[str, Any]],
    config: AttributionConfidenceConfig,
) -> str:
    payload = {
        "identity_inference_run_id": identity_inference_run_id,
        "message_observation_ids": list(message_ids),
        "identity_evidence": [dict(row) for row in identity_evidence],
        "reviewed_attributions": [dict(row) for row in attributions],
        "configuration": config.to_dict(),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def run_attribution_confidence(
    connection: sqlite3.Connection,
    identity_inference_run_id: int,
    *,
    message_observation_ids: Sequence[int] | None = None,
    input_scope: Mapping[str, Any] | None = None,
    config: AttributionConfidenceConfig = DEFAULT_ATTRIBUTION_CONFIDENCE_CONFIG,
) -> tuple[int, AttributionConfidenceResult]:
    """Persist a confidence snapshot without changing identity or attribution records."""
    identity_inference_run_id = _positive_id(
        identity_inference_run_id, "identity_inference_run_id"
    )
    source_run = connection.execute(
        """SELECT status FROM identity_inference_runs
           WHERE identity_inference_run_id = ?""",
        (identity_inference_run_id,),
    ).fetchone()
    if source_run is None:
        raise ValueError(f"Identity inference run {identity_inference_run_id} does not exist")
    if source_run["status"] != "completed":
        raise ValueError(f"Identity inference run {identity_inference_run_id} is not completed")

    if message_observation_ids is None:
        rows = connection.execute(
            """SELECT message_observation_id
               FROM identity_candidate_evidence
               WHERE identity_inference_run_id = ?
               UNION
               SELECT message_observation_id
               FROM message_attributions
               WHERE status IN ('accepted', 'rejected', 'pending')
               ORDER BY message_observation_id""",
            (identity_inference_run_id,),
        ).fetchall()
        message_ids = tuple(int(row[0]) for row in rows)
    else:
        provided = tuple(message_observation_ids)
        for message_id in provided:
            _positive_id(message_id, "message_observation_id")
        message_ids = tuple(sorted(set(provided)))

    if len(message_ids) > 800:
        evidence_rows: list[sqlite3.Row] = []
        attribution_rows: list[sqlite3.Row] = []
        for start in range(0, len(message_ids), 800):
            batch = message_ids[start : start + 800]
            evidence_rows.extend(_fetch_evidence(connection, identity_inference_run_id, batch))
            attribution_rows.extend(_fetch_attributions(connection, batch))
    else:
        evidence_rows = _fetch_evidence(connection, identity_inference_run_id, message_ids)
        attribution_rows = _fetch_attributions(connection, message_ids)

    if message_ids:
        found_ids: set[int] = set()
        for start in range(0, len(message_ids), 800):
            batch = message_ids[start : start + 800]
            placeholders = ",".join("?" for _ in batch)
            found_ids.update(
                int(row[0])
                for row in connection.execute(
                    f"SELECT message_observation_id FROM messages "
                    f"WHERE message_observation_id IN ({placeholders})",
                    batch,
                )
            )
        missing_ids = set(message_ids) - found_ids
        if missing_ids:
            raise ValueError(
                "Unknown message observation IDs: "
                + ", ".join(str(item) for item in sorted(missing_ids)[:10])
            )

    result = score_attribution_evidence(
        evidence_rows,
        message_observation_ids=message_ids,
        reviewed_attributions=attribution_rows,
        config=config,
    )
    scope = dict(input_scope or {})
    scope["message_observation_ids"] = list(message_ids)
    config_json = _canonical_json(config.to_dict())
    fingerprint = _input_fingerprint(
        identity_inference_run_id,
        message_ids,
        evidence_rows,
        attribution_rows,
        config,
    )
    started_at = _now()
    cursor = connection.execute(
        """INSERT INTO attribution_confidence_runs(
               identity_inference_run_id, ruleset_version, configuration_json,
               configuration_sha256, input_scope_json, input_fingerprint,
               started_at, status
           ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running')""",
        (
            identity_inference_run_id,
            config.ruleset_version,
            config_json,
            hashlib.sha256(config_json.encode("utf-8")).hexdigest(),
            _canonical_json(scope),
            fingerprint,
            started_at,
        ),
    )
    run_id = int(cursor.lastrowid)
    connection.execute("SAVEPOINT attribution_confidence_results")
    try:
        classification_counts: Counter[str] = Counter()
        review_count = 0
        for candidate in result.candidates:
            candidate_cursor = connection.execute(
                """INSERT INTO attribution_confidence_candidates(
                       attribution_confidence_run_id, message_observation_id,
                       candidate_kind, character_id, provisional_identity_candidate_id,
                       candidate_label, candidate_rank, score, score_margin,
                       classification, review_priority, mandatory_review,
                       review_reasons_json, decision_status, source_attribution_ids_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    candidate.message_observation_id,
                    candidate.candidate_kind,
                    candidate.character_id,
                    candidate.provisional_identity_candidate_id,
                    candidate.candidate_label,
                    candidate.rank,
                    candidate.score,
                    candidate.score_margin,
                    candidate.classification,
                    candidate.review_priority,
                    int(candidate.mandatory_review),
                    _canonical_json(list(candidate.review_reasons)),
                    candidate.decision_status,
                    _canonical_json(list(candidate.source_attribution_ids)),
                ),
            )
            candidate_row_id = int(candidate_cursor.lastrowid)
            classification_counts[candidate.classification] += 1
            review_count += int(candidate.mandatory_review)
            for evidence in candidate.evidence:
                _insert_evidence(connection, candidate_row_id, evidence)

        statistics = {
            "message_count": len(message_ids),
            "candidate_count": len(result.candidates),
            "evidence_contribution_count": sum(
                len(candidate.evidence) for candidate in result.candidates
            ),
            "mandatory_review_count": review_count,
            "classification_counts": dict(sorted(classification_counts.items())),
        }
        completed_at = _now()
        connection.execute(
            """UPDATE attribution_confidence_runs
               SET completed_at = ?, status = 'completed', statistics_json = ?
               WHERE attribution_confidence_run_id = ? AND status = 'running'""",
            (completed_at, _canonical_json(statistics), run_id),
        )
        connection.execute("RELEASE SAVEPOINT attribution_confidence_results")
        return run_id, result
    except Exception as exc:
        connection.execute("ROLLBACK TO SAVEPOINT attribution_confidence_results")
        connection.execute("RELEASE SAVEPOINT attribution_confidence_results")
        connection.execute(
            """UPDATE attribution_confidence_runs
               SET completed_at = ?, status = 'failed', error_details = ?
               WHERE attribution_confidence_run_id = ? AND status = 'running'""",
            (_now(), f"{type(exc).__name__}: {exc}", run_id),
        )
        raise


def _insert_evidence(
    connection: sqlite3.Connection,
    candidate_row_id: int,
    evidence: EvidenceContribution,
) -> None:
    connection.execute(
        """INSERT INTO attribution_confidence_evidence(
               attribution_confidence_candidate_id, evidence_type,
               evidence_strength, direction, weight, contribution,
               source_message_ids_json, source_identity_inference_run_id,
               source_identity_evidence_id, source_attribution_id,
               source_review_event_ids_json, evidence_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            candidate_row_id,
            evidence.evidence_type,
            evidence.evidence_strength,
            evidence.direction,
            evidence.weight,
            evidence.contribution,
            _canonical_json(list(evidence.source_message_ids)),
            evidence.source_identity_inference_run_id,
            evidence.source_identity_evidence_id,
            evidence.source_attribution_id,
            _canonical_json(list(evidence.source_review_event_ids)),
            _canonical_json(dict(evidence.evidence)),
        ),
    )


def list_attribution_confidence_candidates(
    connection: sqlite3.Connection,
    attribution_confidence_run_id: int,
    message_observation_id: int,
) -> list[sqlite3.Row]:
    attribution_confidence_run_id = _positive_id(
        attribution_confidence_run_id, "attribution_confidence_run_id"
    )
    message_observation_id = _positive_id(
        message_observation_id, "message_observation_id"
    )
    return connection.execute(
        """SELECT candidate.*,
                  character.character_name,
                  provisional.display_label AS provisional_identity_label
           FROM attribution_confidence_candidates candidate
           LEFT JOIN characters character ON character.character_id = candidate.character_id
           LEFT JOIN provisional_identity_candidates provisional
             ON provisional.candidate_id = candidate.provisional_identity_candidate_id
           WHERE candidate.attribution_confidence_run_id = ?
             AND candidate.message_observation_id = ?
           ORDER BY candidate.candidate_rank""",
        (attribution_confidence_run_id, message_observation_id),
    ).fetchall()


def list_attribution_confidence_evidence(
    connection: sqlite3.Connection,
    attribution_confidence_candidate_id: int,
) -> list[sqlite3.Row]:
    attribution_confidence_candidate_id = _positive_id(
        attribution_confidence_candidate_id,
        "attribution_confidence_candidate_id",
    )
    return connection.execute(
        """SELECT * FROM attribution_confidence_evidence
           WHERE attribution_confidence_candidate_id = ?
           ORDER BY attribution_confidence_evidence_id""",
        (attribution_confidence_candidate_id,),
    ).fetchall()
