from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Literal

from archive.identity_inference import (
    IdentityInferenceResult,
    MessageRecord,
    infer_identity_candidates,
)


ReviewDecision = Literal[
    "link_to_character",
    "leave_unresolved",
    "split_evidence",
    "reassign_evidence",
    "exclude_evidence",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _registered_aliases(connection: sqlite3.Connection) -> dict[str, list[int]]:
    aliases: dict[str, list[int]] = defaultdict(list)
    rows = connection.execute(
        "SELECT alias_key, character_id FROM character_aliases ORDER BY alias_key, character_id"
    )
    for row in rows:
        aliases[str(row["alias_key"])].append(int(row["character_id"]))
    return dict(aliases)


def _fingerprint(
    result: IdentityInferenceResult,
    registered_aliases: Mapping[str, Sequence[int]],
) -> str:
    payload = {
        "algorithm_version": "speaker_identity_candidates_v1",
        "registered_aliases": {
            key: sorted(set(values))
            for key, values in sorted(registered_aliases.items())
        },
        "candidates": [candidate.to_dict() for candidate in result.candidates],
        "evidence": [item.to_dict() for item in result.evidence],
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def run_identity_inference(
    connection: sqlite3.Connection,
    messages: Sequence[MessageRecord],
) -> tuple[int, IdentityInferenceResult]:
    """Persist independent identity candidates and their message-level evidence."""
    registered_aliases = _registered_aliases(connection)
    result = infer_identity_candidates(
        messages,
        registered_aliases=registered_aliases,
    )
    fingerprint = _fingerprint(result, registered_aliases)
    started_at = _now()
    cursor = connection.execute(
        """INSERT INTO identity_inference_runs(
               algorithm_version, input_fingerprint, started_at, status
           ) VALUES ('speaker_identity_candidates_v1', ?, ?, 'running')""",
        (fingerprint, started_at),
    )
    run_id = int(cursor.lastrowid)
    connection.execute("SAVEPOINT identity_inference_results")
    try:
        candidate_ids: dict[str, int] = {}
        for candidate in result.candidates:
            connection.execute(
                """INSERT INTO provisional_identity_candidates(
                       candidate_key, server_id, display_label, normalized_label_key,
                       resolution_status, linked_character_id, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, 'unresolved', NULL, ?, ?)
                   ON CONFLICT(candidate_key) DO NOTHING""",
                (
                    candidate.candidate_key,
                    candidate.server_id,
                    candidate.display_label,
                    candidate.normalized_label,
                    started_at,
                    started_at,
                ),
            )
            row = connection.execute(
                "SELECT candidate_id FROM provisional_identity_candidates WHERE candidate_key = ?",
                (candidate.candidate_key,),
            ).fetchone()
            candidate_ids[candidate.candidate_key] = int(row["candidate_id"])

        for item in result.evidence:
            connection.execute(
                """INSERT INTO identity_candidate_evidence(
                       identity_inference_run_id, candidate_id, server_id,
                       message_observation_id, observed_label, normalized_label_key,
                       evidence_kind, registered_character_ids_json, evidence_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    candidate_ids.get(item.candidate_key) if item.candidate_key else None,
                    item.server_id,
                    item.message_observation_id,
                    item.observed_label,
                    item.normalized_label,
                    item.evidence_kind,
                    _canonical_json(list(item.registered_character_ids)),
                    _canonical_json(dict(item.evidence)),
                ),
            )

        counts: dict[str, int] = defaultdict(int)
        for item in result.evidence:
            counts[item.evidence_kind] += 1
        completed_at = _now()
        connection.execute(
            """UPDATE identity_inference_runs
               SET completed_at = ?, status = 'completed', statistics_json = ?
               WHERE identity_inference_run_id = ? AND status = 'running'""",
            (
                completed_at,
                _canonical_json({
                    "candidate_count": len(result.candidates),
                    "evidence_count": len(result.evidence),
                    "evidence_kind_counts": dict(sorted(counts.items())),
                }),
                run_id,
            ),
        )
        connection.execute("RELEASE SAVEPOINT identity_inference_results")
        return run_id, result
    except Exception as exc:
        connection.execute("ROLLBACK TO SAVEPOINT identity_inference_results")
        connection.execute("RELEASE SAVEPOINT identity_inference_results")
        connection.execute(
            """UPDATE identity_inference_runs
               SET completed_at = ?, status = 'failed', error_details = ?
               WHERE identity_inference_run_id = ? AND status = 'running'""",
            (_now(), f"{type(exc).__name__}: {exc}", run_id),
        )
        raise


def list_provisional_identity_candidates(
    connection: sqlite3.Connection,
    *,
    resolution_status: Literal["unresolved", "linked"] | None = None,
    limit: int = 100,
) -> list[sqlite3.Row]:
    if resolution_status not in {None, "unresolved", "linked"}:
        raise ValueError("resolution_status must be 'unresolved', 'linked', or None")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("Limit must be a positive integer")
    query = "SELECT * FROM provisional_identity_candidates"
    parameters: list[Any] = []
    if resolution_status is not None:
        query += " WHERE resolution_status = ?"
        parameters.append(resolution_status)
    query += " ORDER BY server_id, normalized_label_key, candidate_id LIMIT ?"
    parameters.append(limit)
    return connection.execute(query, parameters).fetchall()


def _effective_candidate_id(
    connection: sqlite3.Connection,
    message_observation_id: int,
) -> int | None:
    override = connection.execute(
        """SELECT candidate_id, is_excluded
           FROM identity_candidate_evidence_overrides
           WHERE message_observation_id = ?""",
        (message_observation_id,),
    ).fetchone()
    if override is not None:
        if override["is_excluded"]:
            return None
        return int(override["candidate_id"])
    evidence = connection.execute(
        """SELECT candidate_id
           FROM identity_candidate_evidence
           WHERE message_observation_id = ?
             AND evidence_kind = 'provisional_label'
           ORDER BY identity_inference_run_id DESC, evidence_id DESC
           LIMIT 1""",
        (message_observation_id,),
    ).fetchone()
    return int(evidence["candidate_id"]) if evidence is not None else None


def review_identity_candidate(
    connection: sqlite3.Connection,
    candidate_id: int,
    *,
    decision: ReviewDecision,
    actor: str,
    character_id: int | None = None,
    target_candidate_id: int | None = None,
    message_observation_ids: Sequence[int] = (),
    note: str | None = None,
) -> int | None:
    """Record a review action; message-level overrides survive later inference runs."""
    decisions = {
        "link_to_character",
        "leave_unresolved",
        "split_evidence",
        "reassign_evidence",
        "exclude_evidence",
    }
    if decision not in decisions:
        raise ValueError(f"Unsupported identity-candidate decision: {decision}")
    if not actor.strip():
        raise ValueError("Reviewer identity must not be empty")

    candidate = connection.execute(
        """SELECT candidate_key, server_id, display_label, normalized_label_key
           FROM provisional_identity_candidates WHERE candidate_id = ?""",
        (candidate_id,),
    ).fetchone()
    if candidate is None:
        raise ValueError(f"Identity candidate {candidate_id} does not exist")

    provided_ids = tuple(message_observation_ids)
    for observation_id in provided_ids:
        if isinstance(observation_id, bool) or not isinstance(observation_id, int) or observation_id < 1:
            raise ValueError("Message observation IDs must be positive integers")
    affected_ids = tuple(sorted(set(provided_ids)))

    if decision in {"split_evidence", "reassign_evidence", "exclude_evidence"}:
        if not affected_ids:
            raise ValueError(f"{decision} requires at least one message observation")
        for observation_id in affected_ids:
            if _effective_candidate_id(connection, observation_id) != candidate_id:
                raise ValueError(
                    f"Message observation {observation_id} is not currently assigned to candidate {candidate_id}"
                )
    elif affected_ids:
        raise ValueError(f"{decision} does not accept message observations")

    if decision == "link_to_character":
        if character_id is None:
            raise ValueError("link_to_character requires character_id")
        if connection.execute(
            "SELECT 1 FROM characters WHERE character_id = ?", (character_id,)
        ).fetchone() is None:
            raise ValueError(f"Character {character_id} does not exist")
    elif character_id is not None:
        raise ValueError(f"{decision} does not accept character_id")

    if decision == "reassign_evidence":
        if target_candidate_id is None or target_candidate_id == candidate_id:
            raise ValueError("reassign_evidence requires a different target candidate")
        if connection.execute(
            "SELECT 1 FROM provisional_identity_candidates WHERE candidate_id = ?",
            (target_candidate_id,),
        ).fetchone() is None:
            raise ValueError(f"Identity candidate {target_candidate_id} does not exist")
    elif target_candidate_id is not None:
        raise ValueError(f"{decision} does not accept target_candidate_id")

    now = _now()
    created_candidate_id: int | None = None
    connection.execute("SAVEPOINT review_identity_candidate")
    try:
        if decision == "split_evidence":
            key_payload = {
                "parent_candidate_id": candidate_id,
                "message_observation_ids": affected_ids,
                "reviewed_at": now,
            }
            split_key = "reviewed_split_" + hashlib.sha256(
                _canonical_json(key_payload).encode("utf-8")
            ).hexdigest()[:32]
            cursor = connection.execute(
                """INSERT INTO provisional_identity_candidates(
                       candidate_key, server_id, display_label, normalized_label_key,
                       resolution_status, linked_character_id, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, 'unresolved', NULL, ?, ?)""",
                (
                    split_key,
                    candidate["server_id"],
                    candidate["display_label"],
                    candidate["normalized_label_key"],
                    now,
                    now,
                ),
            )
            created_candidate_id = int(cursor.lastrowid)

        target_id = (
            created_candidate_id
            if decision == "split_evidence"
            else target_candidate_id
        )
        event_cursor = connection.execute(
            """INSERT INTO identity_candidate_review_events(
                   candidate_id, decision, actor, happened_at,
                   linked_character_id, target_candidate_id,
                   affected_message_ids_json, detail_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                candidate_id,
                decision,
                actor.strip(),
                now,
                character_id,
                target_id,
                _canonical_json(list(affected_ids)),
                _canonical_json({"note": note} if note is not None else {}),
            ),
        )
        event_id = int(event_cursor.lastrowid)

        if decision == "link_to_character":
            connection.execute(
                """UPDATE provisional_identity_candidates
                   SET resolution_status = 'linked', linked_character_id = ?, updated_at = ?
                   WHERE candidate_id = ?""",
                (character_id, now, candidate_id),
            )
        elif decision == "leave_unresolved":
            connection.execute(
                """UPDATE provisional_identity_candidates
                   SET resolution_status = 'unresolved', linked_character_id = NULL, updated_at = ?
                   WHERE candidate_id = ?""",
                (now, candidate_id),
            )

        if decision in {"split_evidence", "reassign_evidence", "exclude_evidence"}:
            excluded = int(decision == "exclude_evidence")
            for observation_id in affected_ids:
                connection.execute(
                    """INSERT INTO identity_candidate_evidence_overrides(
                           message_observation_id, candidate_id, is_excluded, review_event_id
                       ) VALUES (?, ?, ?, ?)
                       ON CONFLICT(message_observation_id) DO UPDATE SET
                           candidate_id = excluded.candidate_id,
                           is_excluded = excluded.is_excluded,
                           review_event_id = excluded.review_event_id""",
                    (
                        observation_id,
                        None if excluded else target_id,
                        excluded,
                        event_id,
                    ),
                )

        connection.execute("RELEASE SAVEPOINT review_identity_candidate")
        return created_candidate_id
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT review_identity_candidate")
        connection.execute("RELEASE SAVEPOINT review_identity_candidate")
        raise


def list_identity_candidate_evidence(
    connection: sqlite3.Connection,
    candidate_id: int,
) -> list[sqlite3.Row]:
    """Return each latest observed marker with any durable reviewer reassignment."""
    return connection.execute(
        """WITH ranked AS (
               SELECT evidence.*,
                      ROW_NUMBER() OVER (
                          PARTITION BY message_observation_id
                          ORDER BY identity_inference_run_id DESC, evidence_id DESC
                      ) AS row_number
               FROM identity_candidate_evidence evidence
               WHERE evidence_kind = 'provisional_label'
           ),
           effective AS (
               SELECT ranked.*,
                      CASE WHEN override.message_observation_id IS NOT NULL
                           THEN override.candidate_id ELSE ranked.candidate_id END AS effective_candidate_id,
                      COALESCE(override.is_excluded, 0) AS is_excluded
               FROM ranked
               LEFT JOIN identity_candidate_evidence_overrides override
                 ON override.message_observation_id = ranked.message_observation_id
               WHERE ranked.row_number = 1
           )
           SELECT * FROM effective
           WHERE effective_candidate_id = ? AND is_excluded = 0
           ORDER BY message_observation_id""",
        (candidate_id,),
    ).fetchall()
