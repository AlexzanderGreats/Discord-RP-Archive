from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from archive.boundaries import BoundaryCandidate, canonical_json
from archive.segmentation import (
    DEFAULT_SEGMENTATION_CONFIG,
    SegmentationConfig,
    SegmentationResult,
    segment_message_observations,
)
from archive.signals import MessageRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_current_message_observations(
    connection: sqlite3.Connection,
    *,
    server_id: int | None = None,
    channel_id: int | None = None,
    source_id: int | None = None,
) -> list[sqlite3.Row]:
    """Load one latest accepted import from each current accepted source capture."""
    filters: list[str] = []
    parameters: list[int] = []
    for field, value in (
        ("c.server_id", server_id),
        ("m.channel_id", channel_id),
        ("m.source_id", source_id),
    ):
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field.rsplit('.', 1)[-1]} must be a positive integer")
            filters.append(f"{field} = ?")
            parameters.append(value)
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    query = f"""WITH current_sources AS (
                    SELECT DISTINCT s.source_id
                    FROM sources s
                    JOIN imports accepted ON accepted.source_id = s.source_id
                    WHERE accepted.accepted_at IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM sources newer
                          JOIN imports newer_import ON newer_import.source_id = newer.source_id
                          WHERE newer.supersedes_source_id = s.source_id
                            AND newer_import.accepted_at IS NOT NULL
                      )
                ),
                latest_imports AS (
                    SELECT i.import_id, i.source_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY i.source_id
                               ORDER BY i.accepted_at DESC, i.import_id DESC
                           ) AS import_rank
                    FROM imports i
                    JOIN current_sources current ON current.source_id = i.source_id
                    WHERE i.accepted_at IS NOT NULL
                )
                SELECT m.message_observation_id, m.discord_message_id,
                       m.timestamp_unix, m.timestamp_utc, m.channel_id,
                       c.server_id, m.source_id, m.import_id,
                       m.author_identity_id, m.author_discord_user_id_original,
                       m.webhook_id_original, m.reply_to_discord_message_id,
                       m.content_original, m.source_json
                FROM messages m
                JOIN latest_imports latest
                  ON latest.import_id = m.import_id AND latest.import_rank = 1
                LEFT JOIN channels c ON c.channel_id = m.channel_id
                {where_clause}
                ORDER BY c.server_id, m.channel_id, m.source_id,
                         m.timestamp_unix IS NULL, m.timestamp_unix,
                         m.message_observation_id"""
    return connection.execute(query, parameters).fetchall()


def _input_fingerprint(
    messages: Sequence[MessageRecord],
    boundary_candidates: Sequence[BoundaryCandidate],
    config: SegmentationConfig,
) -> str:
    digest = hashlib.sha256()
    digest.update(canonical_json({"config": config.to_dict()}).encode("utf-8"))
    digest.update(b"\n")
    for record in messages:
        values = {
            key: _record_value(record, key)
            for key in (
                "message_observation_id",
                "discord_message_id",
                "timestamp_unix",
                "timestamp_utc",
                "server_id",
                "channel_id",
                "source_id",
                "import_id",
                "reply_to_discord_message_id",
            )
        }
        digest.update(canonical_json(values).encode("utf-8"))
        digest.update(b"\n")
    for candidate in sorted(
        boundary_candidates,
        key=lambda item: (
            item.left_message_observation_id,
            item.right_message_observation_id,
        ),
    ):
        digest.update(canonical_json(candidate.to_dict()).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _record_value(record: MessageRecord, key: str) -> Any:
    try:
        return record[key]
    except (KeyError, IndexError):
        return None


def _run_statistics(result: SegmentationResult) -> dict[str, int]:
    return {
        "input_message_count": sum(len(span.member_observation_ids) for span in result.spans),
        "candidate_span_count": len(result.spans),
        "candidate_membership_count": sum(len(span.member_observation_ids) for span in result.spans),
        "reply_link_count": len(result.reply_links),
        "split_transition_count": sum(bool(item["split"]) for item in result.boundary_decisions),
        "boundary_decision_count": len(result.boundary_decisions),
    }


def run_candidate_segmentation(
    connection: sqlite3.Connection,
    messages: Sequence[MessageRecord],
    *,
    input_scope: Mapping[str, Any],
    boundary_candidates: Sequence[BoundaryCandidate] = (),
    config: SegmentationConfig = DEFAULT_SEGMENTATION_CONFIG,
    run_type: str = "full",
    detection_run_id: int | None = None,
) -> tuple[int, SegmentationResult]:
    """Persist draft spans and memberships separately from curated scenes."""
    if run_type not in {"full", "incremental"}:
        raise ValueError("run_type must be 'full' or 'incremental'")
    started_at = _now()
    scope_json = canonical_json(dict(input_scope))
    config_json = canonical_json(config.to_dict())
    config_sha256 = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
    fingerprint = _input_fingerprint(messages, boundary_candidates, config)
    cursor = connection.execute(
        """INSERT INTO segmentation_runs(
               boundary_detection_run_id, run_type, algorithm_version,
               configuration_json, configuration_sha256, input_scope_json,
               input_fingerprint, started_at, status
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')""",
        (
            detection_run_id,
            run_type,
            config.algorithm_version,
            config_json,
            config_sha256,
            scope_json,
            fingerprint,
            started_at,
        ),
    )
    run_id = int(cursor.lastrowid)
    connection.execute("SAVEPOINT candidate_segmentation_results")
    try:
        result = segment_message_observations(
            messages,
            boundary_candidates,
            config=config,
        )
        completed_at = _now()
        for span_position, span in enumerate(result.spans):
            span_cursor = connection.execute(
                """INSERT INTO candidate_spans(
                       segmentation_run_id, span_position, candidate_key,
                       server_id, channel_id, source_id,
                       first_message_observation_id, last_message_observation_id,
                       start_timestamp_unix, end_timestamp_unix, start_reason,
                       start_boundary_evidence_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    span_position,
                    span.span_key,
                    span.server_id,
                    span.channel_id,
                    span.source_id,
                    span.member_observation_ids[0],
                    span.member_observation_ids[-1],
                    span.start_timestamp_unix,
                    span.end_timestamp_unix,
                    span.start_reason,
                    canonical_json(span.start_boundary_evidence)
                    if span.start_boundary_evidence is not None
                    else None,
                    completed_at,
                ),
            )
            span_id = int(span_cursor.lastrowid)
            for member_position, observation_id in enumerate(span.member_observation_ids):
                connection.execute(
                    """INSERT INTO candidate_span_messages(
                           candidate_span_id, position, message_observation_id,
                           assignment_method, assignment_status, evidence_json
                       ) VALUES (?, ?, ?, 'automatic', 'candidate', ?)""",
                    (
                        span_id,
                        member_position,
                        observation_id,
                        canonical_json({
                            "rule": "in_scope_temporal_candidate_span",
                            "scope": {
                                "server_id": span.server_id,
                                "channel_id": span.channel_id,
                                "source_id": span.source_id,
                            },
                            "start_reason": span.start_reason,
                        }),
                    ),
                )
        for link in result.reply_links:
            connection.execute(
                """INSERT INTO candidate_reply_links(
                       segmentation_run_id, source_message_observation_id,
                       target_observation_ids_json, target_discord_message_id,
                       resolution, same_scope
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    link.source_observation_id,
                    canonical_json(list(link.target_observation_ids)),
                    link.target_discord_message_id,
                    link.resolution,
                    int(link.same_scope),
                ),
            )
        for decision in result.boundary_decisions:
            connection.execute(
                """INSERT INTO segmentation_boundary_decisions(
                       segmentation_run_id, left_message_observation_id,
                       right_message_observation_id, was_split, split_reason,
                       boundary_candidate_json
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    decision["left_message_observation_id"],
                    decision["right_message_observation_id"],
                    int(bool(decision["split"])),
                    decision["split_reason"],
                    canonical_json(decision["boundary_candidate"])
                    if decision["boundary_candidate"] is not None
                    else None,
                ),
            )
        connection.execute(
            """UPDATE segmentation_runs
               SET completed_at = ?, status = 'completed', statistics_json = ?
               WHERE segmentation_run_id = ? AND status = 'running'""",
            (completed_at, canonical_json(_run_statistics(result)), run_id),
        )
        connection.execute("RELEASE SAVEPOINT candidate_segmentation_results")
        return run_id, result
    except Exception as exc:
        connection.execute("ROLLBACK TO SAVEPOINT candidate_segmentation_results")
        connection.execute("RELEASE SAVEPOINT candidate_segmentation_results")
        connection.execute(
            """UPDATE segmentation_runs
               SET completed_at = ?, status = 'failed', error_details = ?
               WHERE segmentation_run_id = ? AND status = 'running'""",
            (_now(), f"{type(exc).__name__}: {exc}", run_id),
        )
        raise
