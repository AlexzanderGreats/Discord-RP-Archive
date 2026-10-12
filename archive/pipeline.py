from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from archive.attribution_confidence import (
    DEFAULT_ATTRIBUTION_CONFIDENCE_CONFIG,
    AttributionConfidenceConfig,
)
from archive.attribution_confidence_persistence import run_attribution_confidence
from archive.attribution_review import list_attribution_review_queue
from archive.boundaries import (
    DEFAULT_CONFIG,
    BoundaryCandidate,
    BoundaryScoringConfig,
    canonical_json,
    fingerprint_analysis_inputs,
    score_boundary_candidates,
    summarize_candidates,
)
from archive.boundary_persistence import run_boundary_detection
from archive.identity_candidate_persistence import run_identity_inference
from archive.identity_inference import infer_identity_candidates
from archive.segmentation import DEFAULT_SEGMENTATION_CONFIG, SegmentationConfig
from archive.segmentation_persistence import (
    _run_statistics as _segmentation_statistics,
    load_current_message_observations,
    run_candidate_segmentation,
)
from archive.signals import MessageRecord, extract_signals


LOGGER = logging.getLogger(__name__)
PIPELINE_VERSION = "pipeline_g_v1"
IDENTITY_ALGORITHM_VERSION = "speaker_identity_candidates_v1"
_STAGES = ("signals", "boundaries", "spans", "identities", "confidence")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _positive_scope_id(value: int | None, name: str) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 1
    ):
        raise ValueError(f"{name} must be a positive integer or None")


def _scope_data(
    connection: sqlite3.Connection,
    messages: Sequence[MessageRecord],
    *,
    server_id: int | None,
    channel_id: int | None,
    source_id: int | None,
    import_id: int | None,
    message_observation_ids: Sequence[int] | None,
) -> dict[str, Any]:
    requested: dict[str, Any] = {
        "server_id": server_id,
        "channel_id": channel_id,
        "source_id": source_id,
        "import_id": import_id,
        "message_observation_ids": (
            sorted(set(message_observation_ids))
            if message_observation_ids is not None
            else None
        ),
    }
    source_ids = sorted({
        int(row["source_id"])
        for row in messages
        if row["source_id"] is not None
    })
    import_ids = sorted({
        int(row["import_id"])
        for row in messages
        if row["import_id"] is not None
    })
    selected_message_ids = [
        int(row["message_observation_id"])
        for row in messages
    ]
    effective = {
        "source_ids": source_ids,
        "import_ids": import_ids,
        "message_count": len(messages),
        "message_ids_sha256": _fingerprint(selected_message_ids),
    }
    effective["sources"] = []
    for start in range(0, len(source_ids), 500):
        batch = source_ids[start : start + 500]
        placeholders = ",".join("?" for _ in batch)
        effective["sources"].extend(
            dict(row)
            for row in connection.execute(
                f"""SELECT source_id, logical_source_key, source_kind, sha256,
                           supersedes_source_id
                    FROM sources WHERE source_id IN ({placeholders})
                    ORDER BY source_id""",
                batch,
            )
        )
    effective["imports"] = []
    for start in range(0, len(import_ids), 500):
        batch = import_ids[start : start + 500]
        placeholders = ",".join("?" for _ in batch)
        effective["imports"].extend(
            dict(row)
            for row in connection.execute(
                f"""SELECT import_id, source_id, importer_name, importer_version,
                           config_sha256, accepted_at
                    FROM imports WHERE import_id IN ({placeholders})
                    ORDER BY import_id""",
                batch,
            )
        )
    return {
        "requested": requested,
        "effective": effective,
    }


def _alias_data(connection: sqlite3.Connection) -> dict[str, list[int]]:
    aliases: dict[str, list[int]] = {}
    for row in connection.execute(
        "SELECT alias_key, character_id FROM character_aliases ORDER BY alias_key, character_id"
    ):
        aliases.setdefault(str(row["alias_key"]), []).append(int(row["character_id"]))
    return aliases


def _review_input_fingerprint(
    connection: sqlite3.Connection,
    message_ids: Sequence[int],
    candidate_keys: Sequence[str],
) -> str:
    snapshot: dict[str, list[dict[str, Any]]] = {
        "message_attributions": [],
        "attribution_events": [],
        "attribution_review_decisions": [],
        "attribution_review_events": [],
        "attribution_review_evidence_overrides": [],
        "identity_candidate_evidence_overrides": [],
        "identity_candidate_review_events": [],
        "input_changes": [],
    }
    # Automatically inferred candidate rows are outputs, not human-review inputs.
    candidate_ids: list[int] = []
    if candidate_keys:
        for start in range(0, len(candidate_keys), 500):
            batch = candidate_keys[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            candidate_ids.extend(
                int(row[0])
                for row in connection.execute(
                    f"SELECT candidate_id FROM provisional_identity_candidates "
                    f"WHERE candidate_key IN ({placeholders})",
                    batch,
                )
            )
    for start in range(0, len(message_ids), 500):
        batch = message_ids[start : start + 500]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        queries = (
            ("message_attributions",
             f"SELECT * FROM message_attributions WHERE message_observation_id IN ({placeholders}) "
             "ORDER BY message_observation_id, attribution_id"),
            ("attribution_events",
             f"""SELECT event.* FROM attribution_events event
                  JOIN message_attributions attribution
                    ON attribution.attribution_id = event.attribution_id
                  WHERE attribution.message_observation_id IN ({placeholders})
                  ORDER BY event.event_id"""),
            ("attribution_review_decisions",
             f"SELECT * FROM attribution_review_decisions WHERE message_observation_id IN ({placeholders}) "
             "ORDER BY message_observation_id, attribution_review_decision_id"),
            ("attribution_review_events",
             f"SELECT * FROM attribution_review_events WHERE message_observation_id IN ({placeholders}) "
             "ORDER BY attribution_review_event_id"),
            ("attribution_review_evidence_overrides",
             f"SELECT * FROM attribution_review_evidence_overrides "
             f"WHERE message_observation_id IN ({placeholders}) ORDER BY message_observation_id"),
            ("identity_candidate_evidence_overrides",
             f"SELECT * FROM identity_candidate_evidence_overrides "
             f"WHERE message_observation_id IN ({placeholders}) ORDER BY message_observation_id"),
        )
        for key, query in queries:
            snapshot[key].extend(dict(row) for row in connection.execute(query, batch))
    if candidate_ids:
        for start in range(0, len(candidate_ids), 500):
            batch = candidate_ids[start : start + 500]
            placeholders = ",".join("?" for _ in batch)
            snapshot["identity_candidate_review_events"].extend(
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM identity_candidate_review_events "
                    f"WHERE candidate_id IN ({placeholders}) ORDER BY review_event_id",
                    batch,
                )
            )
    snapshot["input_changes"] = [
        dict(row)
        for row in connection.execute(
            "SELECT input_change_id, input_kind, changed_at "
            "FROM attribution_review_input_changes ORDER BY input_change_id"
        )
    ]
    return _fingerprint(snapshot)


def _cached_stages(
    connection: sqlite3.Connection,
    scope_fingerprint: str,
) -> dict[str, dict[str, Any]]:
    cached: dict[str, dict[str, Any]] = {}
    rows = connection.execute(
        """SELECT stage_results_json
           FROM pipeline_runs
           WHERE scope_fingerprint = ? AND status IN ('completed', 'failed')
           ORDER BY started_at DESC, pipeline_run_id DESC""",
        (scope_fingerprint,),
    )
    for row in rows:
        stages = json.loads(row["stage_results_json"])
        for name, value in stages.items():
            if (
                name in _STAGES
                and name not in cached
                and isinstance(value, dict)
                and value.get("status") in {"completed", "reused"}
                and value.get("run_id") is not None
            ):
                cached[name] = value
    return cached


def _stage_stats_from_run(
    connection: sqlite3.Connection,
    table: str,
    id_column: str,
    run_id: int,
) -> dict[str, Any]:
    row = connection.execute(
        f"SELECT statistics_json FROM {table} WHERE {id_column} = ?",
        (run_id,),
    ).fetchone()
    return json.loads(row["statistics_json"]) if row is not None else {}


def _stage_entry(
    *,
    key: str,
    status: str,
    run_id: int | None,
    statistics: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "key": key,
        "status": status,
        "run_id": run_id,
        "statistics": dict(statistics),
    }


def _review_summary(
    connection: sqlite3.Connection,
    confidence_run_id: int,
) -> dict[str, int]:
    count = int(connection.execute(
        """SELECT COUNT(DISTINCT message_observation_id)
           FROM attribution_confidence_candidates
           WHERE attribution_confidence_run_id = ?""",
        (confidence_run_id,),
    ).fetchone()[0])
    queue = list_attribution_review_queue(
        connection,
        confidence_run_id=confidence_run_id,
        include_decided=True,
        limit=max(1, count),
        include_evidence=False,
        include_message_content=False,
    )
    return {
        "candidates_requiring_review": sum(
            1 for item in queue
            if not item["reviewed"]
            and item["state"] not in {"already_resolved", "excluded"}
        ),
        "high_priority": sum(1 for item in queue if item["review_priority"] == "high"),
        "stale": sum(1 for item in queue if item["stale"]),
        "unresolved": sum(1 for item in queue if item["state"] == "unresolved"),
        "reviewed": sum(1 for item in queue if item["reviewed"]),
        "excluded": sum(1 for item in queue if item["state"] == "excluded"),
        "provisional_identities_awaiting_linkage": sum(
            1 for item in queue
            if any(
                candidate["candidate_kind"] == "provisional_identity"
                and not item["reviewed"]
                for candidate in item["candidates"]
            )
        ),
        "candidates_with_competing_alternatives": sum(
            1 for item in queue if len(item["candidates"]) > 1
        ),
    }


def _latest_parent(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT pipeline_run_id FROM pipeline_runs "
        "ORDER BY started_at DESC, pipeline_run_id DESC LIMIT 1"
    ).fetchone()
    return str(row[0]) if row is not None else None


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()
    version = int(row[0])
    if version < 22:
        raise RuntimeError("Phase G requires schema migration 022; run `python -m archive migrate` first")
    return version


def _persist_progress(
    connection: sqlite3.Connection,
    run_id: str,
    stages: Mapping[str, Any],
    input_fingerprint: str,
) -> None:
    connection.execute(
        """UPDATE pipeline_runs
           SET stage_results_json = ?, input_fingerprint = ?
           WHERE pipeline_run_id = ? AND status = 'running'""",
        (canonical_json(dict(stages)), input_fingerprint, run_id),
    )
    connection.commit()


def run_pipeline(
    connection: sqlite3.Connection,
    *,
    server_id: int | None = None,
    channel_id: int | None = None,
    source_id: int | None = None,
    import_id: int | None = None,
    message_observation_ids: Sequence[int] | None = None,
    mode: str = "incremental",
    dry_run: bool = False,
    boundary_config: BoundaryScoringConfig = DEFAULT_CONFIG,
    segmentation_config: SegmentationConfig = DEFAULT_SEGMENTATION_CONFIG,
    confidence_config: AttributionConfidenceConfig = DEFAULT_ATTRIBUTION_CONFIDENCE_CONFIG,
) -> dict[str, Any]:
    """Run B–F1 for an explicit accepted-source scope.

    The caller must provide a dedicated connection with no open transaction.
    Each manifest/stage boundary is committed separately, so an interrupted
    stage is not reported as completed and can be retried from its last valid
    upstream result. F2 decisions and curated scene tables are never written.
    """
    if mode not in {"full", "incremental"}:
        raise ValueError("mode must be 'full' or 'incremental'")
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    if connection.in_transaction:
        raise RuntimeError("run_pipeline requires a dedicated connection with no open transaction")
    for name, value in (
        ("server_id", server_id),
        ("channel_id", channel_id),
        ("source_id", source_id),
        ("import_id", import_id),
    ):
        _positive_scope_id(value, name)
    if message_observation_ids is not None and any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in message_observation_ids
    ):
        raise ValueError("message_observation_ids must contain positive integers")

    original_row_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    run_id = str(uuid.uuid4())
    started_at = _now()
    started_clock = time.monotonic()
    stage_results: dict[str, Any] = {}
    failed_stage = "input"
    report: dict[str, Any] = {
        "run_id": run_id,
        "status": "running",
        "started_at": started_at,
        "completed_at": None,
        "processing_mode": "dry_run" if dry_run else mode,
        "pipeline_version": PIPELINE_VERSION,
        "schema_version": None,
        "scope": {},
        "ruleset_versions": {},
        "configuration": {},
        "input": {},
        "stages": stage_results,
        "review": {},
        "changes": {},
        "warnings": [],
        "errors": [],
    }
    try:
        schema_version = _schema_version(connection)
        requested_scope = {
            "server_id": server_id,
            "channel_id": channel_id,
            "source_id": source_id,
            "import_id": import_id,
            "message_observation_ids": (
                sorted(set(message_observation_ids))
                if message_observation_ids is not None
                else None
            ),
        }
        scope_fingerprint = _fingerprint(requested_scope)
        parent_id = _latest_parent(connection)
        ruleset_versions = {
            "boundary": boundary_config.algorithm_version,
            "segmentation": segmentation_config.algorithm_version,
            "identity": IDENTITY_ALGORITHM_VERSION,
            "confidence": confidence_config.ruleset_version,
        }
        configuration = {
            "boundary": boundary_config.to_dict(),
            "segmentation": segmentation_config.to_dict(),
            "identity": {"algorithm_version": IDENTITY_ALGORITHM_VERSION},
            "confidence": confidence_config.to_dict(),
        }
        report["scope"] = {"requested": requested_scope}
        report["ruleset_versions"] = ruleset_versions
        report["configuration"] = configuration
        connection.execute(
            """INSERT INTO pipeline_runs(
                   pipeline_run_id, parent_pipeline_run_id, pipeline_version,
                   schema_version, processing_mode, scope_fingerprint,
                   input_fingerprint, scope_json, ruleset_versions_json,
                   started_at, status
               ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, 'running')""",
            (
                run_id,
                parent_id,
                PIPELINE_VERSION,
                schema_version,
                "dry_run" if dry_run else mode,
                scope_fingerprint,
                canonical_json({"requested": requested_scope}),
                canonical_json(ruleset_versions),
                started_at,
            ),
        )
        connection.commit()
        LOGGER.info(
            "Pipeline run started: id=%s mode=%s scope=%s",
            run_id,
            "dry_run" if dry_run else mode,
            canonical_json(requested_scope),
        )

        messages = load_current_message_observations(
            connection,
            server_id=server_id,
            channel_id=channel_id,
            source_id=source_id,
            import_id=import_id,
            message_observation_ids=message_observation_ids,
        )
        if not messages:
            raise ValueError("Selected scope contains no accepted message observations")
        if message_observation_ids is not None:
            found_ids = {int(row["message_observation_id"]) for row in messages}
            missing_ids = sorted(set(message_observation_ids) - found_ids)
            if missing_ids:
                raise ValueError(
                    "Some requested message observations are outside the accepted scope: "
                    + ", ".join(str(value) for value in missing_ids[:20])
                )
        signals = extract_signals(messages)
        input_fingerprint = fingerprint_analysis_inputs(messages, signals)
        LOGGER.info(
            "Pipeline run %s input loaded: messages=%d signals=%d fingerprint=%s",
            run_id,
            len(messages),
            len(signals),
            input_fingerprint,
        )
        scope = _scope_data(
            connection,
            messages,
            server_id=server_id,
            channel_id=channel_id,
            source_id=source_id,
            import_id=import_id,
            message_observation_ids=message_observation_ids,
        )
        report["schema_version"] = schema_version
        report["scope"] = scope
        report["input"] = {
            "messages_examined": len(messages),
            "input_fingerprint": input_fingerprint,
            "changed_messages": None,
            "unchanged_messages": None,
            "source_ids": scope["effective"]["source_ids"],
            "import_ids": scope["effective"]["import_ids"],
        }
        report["ruleset_versions"] = ruleset_versions
        report["configuration"] = configuration
        previous_stages = _cached_stages(connection, scope_fingerprint)
        boundary_key = _fingerprint({
            "input": input_fingerprint,
            "config": boundary_config.to_dict(),
        })
        spans_key = _fingerprint({
            "input": input_fingerprint,
            "boundaries": boundary_key,
            "config": segmentation_config.to_dict(),
        })
        aliases = _alias_data(connection)
        alias_fingerprint = _fingerprint(aliases)
        identity_key = _fingerprint({
            "input": input_fingerprint,
            "aliases": alias_fingerprint,
            "algorithm_version": IDENTITY_ALGORITHM_VERSION,
        })
        identity_result = infer_identity_candidates(
            messages,
            registered_aliases=aliases,
        )
        review_input_fingerprint = _review_input_fingerprint(
            connection,
            [int(row["message_observation_id"]) for row in messages],
            [candidate.candidate_key for candidate in identity_result.candidates],
        )
        cached_boundary = previous_stages.get("boundaries")
        cached_spans = previous_stages.get("spans")
        cached_identity = previous_stages.get("identities")
        cached_confidence = previous_stages.get("confidence")
        cached_confidence_key = _fingerprint({
            "identity_run_id": (
                cached_identity.get("run_id") if cached_identity is not None else None
            ),
            "identity_key": identity_key,
            "config": confidence_config.to_dict(),
            "review_inputs": review_input_fingerprint,
        })
        all_stage_inputs_match = (
            cached_boundary is not None
            and cached_boundary.get("key") == boundary_key
            and cached_spans is not None
            and cached_spans.get("key") == spans_key
            and cached_identity is not None
            and cached_identity.get("key") == identity_key
            and cached_confidence is not None
            and cached_confidence.get("key") == cached_confidence_key
        )
        if all_stage_inputs_match and cached_confidence is not None:
            all_stage_inputs_match = (
                _review_summary(connection, int(cached_confidence["run_id"]))["stale"] == 0
            )
        force_full = mode == "full" and not all_stage_inputs_match
        spans_reused = (
            not force_full
            and cached_spans is not None
            and cached_spans.get("key") == spans_key
        )

        signal_counts = Counter(signal.signal_type for signal in signals)
        stage_results["signals"] = _stage_entry(
            key=input_fingerprint,
            status="planned" if dry_run else "completed",
            run_id=None,
            statistics={
                "signal_count": len(signals),
                "signal_type_counts": dict(sorted(signal_counts.items())),
            },
        )
        boundary_candidates: list[BoundaryCandidate] = []
        failed_stage = "boundaries"

        boundary_run_id: int | None = None
        boundary_reused = (
            not force_full
            and cached_boundary is not None
            and cached_boundary.get("key") == boundary_key
        )
        if boundary_reused:
            boundary_run_id = int(cached_boundary["run_id"])
            boundary_stats = dict(cached_boundary.get("statistics", {}))
            boundary_status = "reused"
            if not spans_reused:
                boundary_candidates = score_boundary_candidates(
                    [int(row["message_observation_id"]) for row in messages],
                    signals,
                    config=boundary_config,
                )
        elif dry_run:
            boundary_candidates = score_boundary_candidates(
                [int(row["message_observation_id"]) for row in messages],
                signals,
                config=boundary_config,
            )
            boundary_stats = summarize_candidates(boundary_candidates)
            boundary_status = "planned"
        else:
            boundary_candidates = score_boundary_candidates(
                [int(row["message_observation_id"]) for row in messages],
                signals,
                config=boundary_config,
            )
            boundary_run_id, boundary_candidates = run_boundary_detection(
                connection,
                messages,
                input_scope=scope,
                config=boundary_config,
                run_type=mode,
                precomputed_analysis=(signals, boundary_candidates),
            )
            boundary_stats = _stage_stats_from_run(
                connection,
                "boundary_detection_runs",
                "detection_run_id",
                boundary_run_id,
            )
            boundary_status = "completed"
        stage_results["boundaries"] = _stage_entry(
            key=boundary_key,
            status=boundary_status,
            run_id=boundary_run_id,
            statistics=boundary_stats,
        )
        if not dry_run:
            _persist_progress(connection, run_id, stage_results, input_fingerprint)
        LOGGER.info(
            "Pipeline run %s boundary stage %s: %s",
            run_id,
            boundary_status,
            canonical_json(boundary_stats),
        )

        failed_stage = "spans"
        if spans_reused:
            segmentation_run_id = int(cached_spans["run_id"])
            spans_stats = dict(cached_spans.get("statistics", {}))
            spans_status = "reused"
        elif dry_run:
            from archive.segmentation import segment_message_observations

            segmentation_result = segment_message_observations(
                messages,
                boundary_candidates,
                config=segmentation_config,
            )
            spans_stats = _segmentation_statistics(segmentation_result)
            segmentation_run_id = None
            spans_status = "planned"
        else:
            if boundary_run_id is None:
                raise RuntimeError("Boundary detection is required before persisting candidate spans")
            segmentation_run_id, segmentation_result = run_candidate_segmentation(
                connection,
                messages,
                input_scope=scope,
                boundary_candidates=boundary_candidates,
                config=segmentation_config,
                run_type=mode,
                detection_run_id=boundary_run_id,
            )
            spans_stats = _segmentation_statistics(segmentation_result)
            spans_status = "completed"
        stage_results["spans"] = _stage_entry(
            key=spans_key,
            status=spans_status,
            run_id=segmentation_run_id,
            statistics=spans_stats,
        )
        if not dry_run:
            _persist_progress(connection, run_id, stage_results, input_fingerprint)
        LOGGER.info(
            "Pipeline run %s span stage %s: %s",
            run_id,
            spans_status,
            canonical_json(spans_stats),
        )

        failed_stage = "identities"
        identities_reused = (
            not force_full
            and cached_identity is not None
            and cached_identity.get("key") == identity_key
        )
        if identities_reused:
            identity_run_id = int(cached_identity["run_id"])
            identity_stats = dict(cached_identity.get("statistics", {}))
            identity_status = "reused"
        elif dry_run:
            evidence_counts = Counter(item.evidence_kind for item in identity_result.evidence)
            identity_run_id = None
            identity_stats = {
                "candidate_count": len(identity_result.candidates),
                "evidence_count": len(identity_result.evidence),
                "evidence_kind_counts": dict(sorted(evidence_counts.items())),
            }
            identity_status = "planned"
        else:
            identity_run_id, identity_result = run_identity_inference(connection, messages)
            identity_stats = _stage_stats_from_run(
                connection,
                "identity_inference_runs",
                "identity_inference_run_id",
                identity_run_id,
            )
            identity_status = "completed"
        stage_results["identities"] = _stage_entry(
            key=identity_key,
            status=identity_status,
            run_id=identity_run_id,
            statistics=identity_stats,
        )
        if not dry_run:
            _persist_progress(connection, run_id, stage_results, input_fingerprint)
        LOGGER.info(
            "Pipeline run %s identity stage %s: %s",
            run_id,
            identity_status,
            canonical_json(identity_stats),
        )

        failed_stage = "confidence"
        confidence_key = _fingerprint({
            "identity_run_id": identity_run_id,
            "identity_key": identity_key,
            "config": confidence_config.to_dict(),
            "review_inputs": review_input_fingerprint,
        })
        confidence_reused = (
            not force_full
            and cached_confidence is not None
            and cached_confidence.get("key") == confidence_key
        )
        if confidence_reused:
            confidence_run_id = int(cached_confidence["run_id"])
            confidence_stats = dict(cached_confidence.get("statistics", {}))
            review_stats = _review_summary(connection, confidence_run_id)
            if review_stats["stale"]:
                confidence_reused = False
        if confidence_reused:
            confidence_status = "reused"
        elif dry_run:
            confidence_run_id = None
            confidence_stats = {"status": "not_persisted_in_dry_run"}
            review_stats = {}
            confidence_status = "planned"
        else:
            if identity_run_id is None:
                raise RuntimeError("Identity inference must complete before confidence scoring")
            confidence_run_id, confidence_result = run_attribution_confidence(
                connection,
                identity_run_id,
                message_observation_ids=[
                    int(row["message_observation_id"])
                    for row in messages
                ],
                input_scope=scope,
                config=confidence_config,
            )
            classification_counts = Counter(
                candidate.classification for candidate in confidence_result.candidates
            )
            confidence_stats = {
                "message_count": len(messages),
                "candidate_count": len(confidence_result.candidates),
                "evidence_contribution_count": sum(
                    len(candidate.evidence) for candidate in confidence_result.candidates
                ),
                "mandatory_review_count": sum(
                    int(candidate.mandatory_review)
                    for candidate in confidence_result.candidates
                ),
                "classification_counts": dict(sorted(classification_counts.items())),
            }
            review_stats = _review_summary(connection, confidence_run_id)
            confidence_status = "completed"
        stage_results["confidence"] = _stage_entry(
            key=confidence_key,
            status=confidence_status,
            run_id=confidence_run_id,
            statistics=confidence_stats,
        )
        if not dry_run:
            _persist_progress(connection, run_id, stage_results, input_fingerprint)
        LOGGER.info(
            "Pipeline run %s confidence stage %s: %s",
            run_id,
            confidence_status,
            canonical_json(confidence_stats),
        )
        report["review"] = review_stats
        if dry_run:
            report["warnings"].append(
                "Dry-run persisted only this run manifest; no inference or review records were written."
            )
        report["status"] = "completed"
        report["completed_at"] = _now()
        report["elapsed_seconds"] = round(time.monotonic() - started_clock, 6)
        report["failed_stage"] = None
        report["errors"] = []
        comparison_parent = connection.execute(
            """SELECT pipeline_run_id, input_fingerprint, report_json, stage_results_json
               FROM pipeline_runs
               WHERE pipeline_run_id != ? AND scope_fingerprint = ?
                 AND status = 'completed'
               ORDER BY started_at DESC, pipeline_run_id DESC LIMIT 1""",
            (run_id, scope_fingerprint),
        ).fetchone()
        if comparison_parent is None:
            report["input"]["changed_messages"] = len(messages)
            report["input"]["unchanged_messages"] = 0
        else:
            previous_stages = json.loads(comparison_parent["stage_results_json"])
            previous_boundary_id = previous_stages.get("boundaries", {}).get("run_id")
            if previous_boundary_id is not None:
                previous_ids = {
                    int(row[0])
                    for row in connection.execute(
                        """SELECT message_observation_id
                           FROM boundary_detection_run_messages
                           WHERE detection_run_id = ?""",
                        (previous_boundary_id,),
                    )
                }
            elif comparison_parent["input_fingerprint"] == input_fingerprint:
                previous_ids = {
                    int(row["message_observation_id"])
                    for row in messages
                }
            else:
                previous_ids = set()
            current_ids = {
                int(row["message_observation_id"])
                for row in messages
            }
            report["input"]["unchanged_messages"] = len(previous_ids & current_ids)
            report["input"]["changed_messages"] = len(previous_ids ^ current_ids)
            if (
                comparison_parent["input_fingerprint"] != input_fingerprint
                and not report["input"]["changed_messages"]
            ):
                report["warnings"].append(
                    "Input content changed within the selected observation set; per-message deltas "
                    "are unavailable because source observations are expected to be immutable."
                )
        report["changes"] = _run_deltas(
            connection,
            str(comparison_parent["pipeline_run_id"])
            if comparison_parent is not None
            else None,
            stage_results,
        )
        connection.execute(
            """UPDATE pipeline_runs
               SET input_fingerprint = ?, scope_json = ?, ruleset_versions_json = ?,
                   stage_results_json = ?, report_json = ?, warnings_json = ?,
                   errors_json = '[]', completed_at = ?, status = 'completed'
               WHERE pipeline_run_id = ? AND status = 'running'""",
            (
                input_fingerprint,
                canonical_json(scope),
                canonical_json(report["ruleset_versions"]),
                canonical_json(stage_results),
                canonical_json(report),
                canonical_json(report["warnings"]),
                report["completed_at"],
                run_id,
            ),
        )
        connection.commit()
        LOGGER.info(
            "Pipeline run completed: id=%s messages=%d reused_stages=%d",
            run_id,
            len(messages),
            sum(stage["status"] == "reused" for stage in stage_results.values()),
        )
        return report
    except Exception as exc:
        report["status"] = "failed"
        report["completed_at"] = _now()
        report["elapsed_seconds"] = round(time.monotonic() - started_clock, 6)
        report["failed_stage"] = failed_stage
        report["errors"] = [f"{type(exc).__name__}: {exc}"]
        try:
            connection.execute(
                """UPDATE pipeline_runs
                   SET input_fingerprint = ?, scope_json = ?, ruleset_versions_json = ?,
                       stage_results_json = ?, report_json = ?, errors_json = ?,
                       completed_at = ?, status = 'failed', failed_stage = ?
                   WHERE pipeline_run_id = ? AND status = 'running'""",
                (
                    report.get("input", {}).get("input_fingerprint", "pending"),
                    canonical_json(report.get("scope", {})),
                    canonical_json(report.get("ruleset_versions", {})),
                    canonical_json(stage_results),
                    canonical_json(report),
                    canonical_json(report["errors"]),
                    report["completed_at"],
                    failed_stage,
                    run_id,
                ),
            )
            connection.commit()
        except sqlite3.Error:
            LOGGER.exception("Could not persist failed pipeline run manifest %s", run_id)
            raise
        LOGGER.exception("Pipeline run failed: id=%s stage=%s", run_id, failed_stage)
        return report
    finally:
        connection.row_factory = original_row_factory


def _run_deltas(
    connection: sqlite3.Connection,
    parent_run_id: str | None,
    current_stages: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    if parent_run_id is None:
        return {}
    row = connection.execute(
        "SELECT stage_results_json FROM pipeline_runs WHERE pipeline_run_id = ?",
        (parent_run_id,),
    ).fetchone()
    if row is None:
        return {}
    prior_stages = json.loads(row["stage_results_json"])
    deltas: dict[str, Any] = {}
    pairs = (
        ("spans", "candidate_spans", "candidate_key"),
        ("identities", "provisional_identity_candidates", "candidate_key"),
    )
    for stage, table, key_column in pairs:
        previous = prior_stages.get(stage, {})
        current = current_stages.get(stage, {})
        previous_id = previous.get("run_id")
        current_id = current.get("run_id")
        if previous_id is None or current_id is None:
            continue
        if table == "candidate_spans":
            previous_keys = {
                str(item[0])
                for item in connection.execute(
                    f"SELECT {key_column} FROM {table} WHERE segmentation_run_id = ?",
                    (previous_id,),
                )
            }
            current_keys = {
                str(item[0])
                for item in connection.execute(
                    f"SELECT {key_column} FROM {table} WHERE segmentation_run_id = ?",
                    (current_id,),
                )
            }
        else:
            previous_keys = {
                str(item[0])
                for item in connection.execute(
                    """SELECT DISTINCT candidate.candidate_key
                       FROM identity_candidate_evidence evidence
                       JOIN provisional_identity_candidates candidate
                         ON candidate.candidate_id = evidence.candidate_id
                       WHERE evidence.identity_inference_run_id = ?
                         AND evidence.candidate_id IS NOT NULL""",
                    (previous_id,),
                )
            }
            current_keys = {
                str(item[0])
                for item in connection.execute(
                    """SELECT DISTINCT candidate.candidate_key
                       FROM identity_candidate_evidence evidence
                       JOIN provisional_identity_candidates candidate
                         ON candidate.candidate_id = evidence.candidate_id
                       WHERE evidence.identity_inference_run_id = ?
                         AND evidence.candidate_id IS NOT NULL""",
                    (current_id,),
                )
            }
        deltas[stage] = {
            "new_candidates": len(current_keys - previous_keys),
            "no_longer_supported": len(previous_keys - current_keys),
            "disclaimer": (
                "No longer supported means absent from current inference, not deleted historical fact."
            ),
        }
    return deltas


def get_pipeline_report(
    connection: sqlite3.Connection,
    pipeline_run_id: str | None = None,
) -> dict[str, Any] | None:
    original_row_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        if pipeline_run_id is None:
            row = connection.execute(
                "SELECT report_json FROM pipeline_runs "
                "ORDER BY started_at DESC, pipeline_run_id DESC LIMIT 1"
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT report_json FROM pipeline_runs WHERE pipeline_run_id = ?",
                (pipeline_run_id,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["report_json"])
    finally:
        connection.row_factory = original_row_factory


def get_pipeline_status(
    connection: sqlite3.Connection,
    *,
    server_id: int | None = None,
    channel_id: int | None = None,
    source_id: int | None = None,
    import_id: int | None = None,
) -> dict[str, Any]:
    original_row_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        messages = load_current_message_observations(
            connection,
            server_id=server_id,
            channel_id=channel_id,
            source_id=source_id,
            import_id=import_id,
        )
        signals = extract_signals(messages)
        current_input_fingerprint = fingerprint_analysis_inputs(messages, signals)
        current_scope = _scope_data(
            connection,
            messages,
            server_id=server_id,
            channel_id=channel_id,
            source_id=source_id,
            import_id=import_id,
            message_observation_ids=None,
        )
        scope_fingerprint = _fingerprint({
            "server_id": server_id,
            "channel_id": channel_id,
            "source_id": source_id,
            "import_id": import_id,
            "message_observation_ids": None,
        })
        latest = connection.execute(
            """SELECT pipeline_run_id, status, started_at, completed_at,
                      failed_stage, input_fingerprint, scope_fingerprint,
                      processing_mode, report_json
               FROM pipeline_runs
               WHERE scope_fingerprint = ?
               ORDER BY started_at DESC, pipeline_run_id DESC LIMIT 1""",
            (scope_fingerprint,),
        ).fetchone()
        configuration = {
            "boundary": DEFAULT_CONFIG.to_dict(),
            "segmentation": DEFAULT_SEGMENTATION_CONFIG.to_dict(),
            "identity": {"algorithm_version": IDENTITY_ALGORITHM_VERSION},
            "confidence": DEFAULT_ATTRIBUTION_CONFIDENCE_CONFIG.to_dict(),
        }
        aliases = _alias_data(connection)
        identity_result = infer_identity_candidates(messages, registered_aliases=aliases)
        identity_key = _fingerprint({
            "input": current_input_fingerprint,
            "aliases": _fingerprint(aliases),
            "algorithm_version": IDENTITY_ALGORITHM_VERSION,
        })
        boundary_key = _fingerprint({
            "input": current_input_fingerprint,
            "config": DEFAULT_CONFIG.to_dict(),
        })
        spans_key = _fingerprint({
            "input": current_input_fingerprint,
            "boundaries": boundary_key,
            "config": DEFAULT_SEGMENTATION_CONFIG.to_dict(),
        })
        review_fingerprint = _review_input_fingerprint(
            connection,
            [int(row["message_observation_id"]) for row in messages],
            [candidate.candidate_key for candidate in identity_result.candidates],
        )
        current = bool(
            latest is not None
            and latest["status"] == "completed"
            and latest["processing_mode"] != "dry_run"
            and latest["input_fingerprint"] == current_input_fingerprint
        )
        if current and latest is not None:
            latest_report = json.loads(latest["report_json"])
            stages = latest_report.get("stages", {})
            current = (
                latest_report.get("configuration") == configuration
                and stages.get("signals", {}).get("key") == current_input_fingerprint
                and stages.get("boundaries", {}).get("key") == boundary_key
                and stages.get("spans", {}).get("key") == spans_key
                and stages.get("identities", {}).get("key") == identity_key
            )
            confidence_stage = stages.get("confidence", {})
            if current:
                expected_confidence_key = _fingerprint({
                    "identity_run_id": stages.get("identities", {}).get("run_id"),
                    "identity_key": identity_key,
                    "config": DEFAULT_ATTRIBUTION_CONFIDENCE_CONFIG.to_dict(),
                    "review_inputs": review_fingerprint,
                })
                current = (
                    confidence_stage.get("key") == expected_confidence_key
                    and confidence_stage.get("run_id") is not None
                    and _review_summary(
                        connection,
                        int(confidence_stage["run_id"]),
                    )["stale"] == 0
                )
        latest_data = dict(latest) if latest is not None else None
        if latest_data is not None:
            latest_data.pop("report_json", None)
        return {
            "latest_run": latest_data,
            "current_for_scope": current,
            "messages_examined": len(messages),
            "input_fingerprint": current_input_fingerprint,
            "scope": current_scope,
        }
    finally:
        connection.row_factory = original_row_factory


def format_pipeline_report(report: Mapping[str, Any]) -> str:
    lines = [
        f"Pipeline run: {report.get('run_id', 'unknown')}",
        f"Status: {report.get('status', 'unknown')}",
        f"Mode: {report.get('processing_mode', 'unknown')}",
        f"Started: {report.get('started_at', 'unknown')}",
        f"Completed: {report.get('completed_at') or 'not completed'}",
        f"Messages examined: {report.get('input', {}).get('messages_examined', 0)}",
    ]
    for stage in _STAGES:
        result = report.get("stages", {}).get(stage)
        if result is not None:
            stats = result.get("statistics", {})
            lines.append(
                f"{stage.title()}: {result.get('status')} "
                f"(run={result.get('run_id')}, counts={canonical_json(stats)})"
            )
    review = report.get("review", {})
    if review:
        lines.append(
            "Review work: "
            + canonical_json(review)
        )
    if report.get("failed_stage"):
        lines.append(f"Failed stage: {report['failed_stage']}")
    for error in report.get("errors", []):
        lines.append(f"Error: {error}")
    for warning in report.get("warnings", []):
        lines.append(f"Warning: {warning}")
    return "\n".join(lines)
