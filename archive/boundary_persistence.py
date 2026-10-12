from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from archive.boundaries import (
    DEFAULT_CONFIG,
    BoundaryCandidate,
    BoundaryScoringConfig,
    analyze_message_boundaries,
    canonical_json,
    fingerprint_analysis_inputs,
    summarize_candidates,
)
from archive.signals import MessageRecord, Signal


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_boundary_detection(
    connection: sqlite3.Connection,
    messages: Sequence[MessageRecord],
    *,
    input_scope: Mapping[str, Any],
    config: BoundaryScoringConfig = DEFAULT_CONFIG,
    run_type: str = "full",
    precomputed_analysis: tuple[Sequence[Signal], Sequence[BoundaryCandidate]] | None = None,
) -> tuple[int, list[BoundaryCandidate]]:
    """Persist one reproducible boundary run and its non-none candidates.

    Caller owns the surrounding transaction. A failed analysis leaves an
    auditable failed run inside that transaction and re-raises the original error.
    """
    if run_type not in {"full", "incremental"}:
        raise ValueError("run_type must be 'full' or 'incremental'")
    scope_json = canonical_json(dict(input_scope))
    config_json = canonical_json(config.to_dict())
    config_hash = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
    started_at = _now()
    cursor = connection.execute(
        """INSERT INTO boundary_detection_runs(
               run_type, algorithm_version, configuration_json, configuration_sha256,
               input_scope_json, input_fingerprint, started_at, status
           ) VALUES (?, ?, ?, ?, ?, ?, ?, 'running')""",
        (
            run_type,
            config.algorithm_version,
            config_json,
            config_hash,
            scope_json,
            "pending",
            started_at,
        ),
    )
    run_id = int(cursor.lastrowid)

    connection.execute("SAVEPOINT boundary_detection_results")
    try:
        if precomputed_analysis is None:
            signals, candidates = analyze_message_boundaries(messages, config=config)
        else:
            signals, candidates = precomputed_analysis
            candidates = list(candidates)
        fingerprint = fingerprint_analysis_inputs(messages, signals)
        completed_at = _now()
        for position, record in enumerate(messages):
            connection.execute(
                """INSERT INTO boundary_detection_run_messages(
                       detection_run_id, position, message_observation_id
                   ) VALUES (?, ?, ?)""",
                (run_id, position, record["message_observation_id"]),
            )
        for candidate in candidates:
            if candidate.classification == "none":
                continue
            connection.execute(
                """INSERT INTO event_boundary_candidates(
                       detection_run_id, left_message_observation_id,
                       right_message_observation_id, boundary_type, raw_score,
                       classification, supporting_evidence_json,
                       contradictory_evidence_json, contributions_json, review_status,
                       created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    run_id,
                    candidate.left_message_observation_id,
                    candidate.right_message_observation_id,
                    candidate.boundary_type,
                    candidate.raw_score,
                    candidate.classification,
                    canonical_json([item.to_dict() for item in candidate.supporting_evidence]),
                    canonical_json([item.to_dict() for item in candidate.contradictory_evidence]),
                    canonical_json([item.to_dict() for item in candidate.contributions]),
                    completed_at,
                ),
            )
        connection.execute(
            """UPDATE boundary_detection_runs
               SET input_fingerprint = ?, completed_at = ?, status = 'completed', statistics_json = ?
               WHERE detection_run_id = ? AND status = 'running'""",
            (fingerprint, completed_at, canonical_json(summarize_candidates(candidates)), run_id),
        )
        connection.execute("RELEASE SAVEPOINT boundary_detection_results")
        return run_id, candidates
    except Exception as exc:
        connection.execute("ROLLBACK TO SAVEPOINT boundary_detection_results")
        connection.execute("RELEASE SAVEPOINT boundary_detection_results")
        try:
            input_fingerprint = fingerprint_analysis_inputs(messages, ())
        except Exception:
            input_fingerprint = "unavailable"
        connection.execute(
            """UPDATE boundary_detection_runs
               SET input_fingerprint = ?, completed_at = ?, status = 'failed', error_details = ?
               WHERE detection_run_id = ? AND status = 'running'""",
            (input_fingerprint, _now(), f"{type(exc).__name__}: {exc}", run_id),
        )
        raise
