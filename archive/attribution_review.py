from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Literal

from archive.attribution import propose_attribution, review_attribution
from archive.attribution_confidence_persistence import (
    list_attribution_confidence_evidence,
)
from archive.identity_candidate_persistence import (
    list_identity_candidate_evidence,
    review_identity_candidate,
)


ReviewAction = Literal[
    "confirm",
    "assign_character",
    "reject",
    "leave_unresolved",
    "link_provisional",
    "split_evidence",
    "reassign_evidence",
    "exclude_evidence",
    "merge_candidates",
    "restore_evidence",
]
_VALID_ACTIONS = {
    "confirm", "assign_character", "reject", "leave_unresolved",
    "link_provisional", "split_evidence", "reassign_evidence",
    "exclude_evidence", "merge_candidates", "restore_evidence",
}
_CHARACTER_ACTIONS = {"confirm", "assign_character", "reject"}
_EVIDENCE_ACTIONS = {
    "split_evidence", "reassign_evidence", "exclude_evidence", "restore_evidence"
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _positive_id(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _confidence_run(
    connection: sqlite3.Connection,
    confidence_run_id: int,
) -> sqlite3.Row:
    row = connection.execute(
        """SELECT attribution_confidence_run_id, identity_inference_run_id,
                  started_at, status, ruleset_version, input_scope_json
           FROM attribution_confidence_runs
           WHERE attribution_confidence_run_id = ?""",
        (confidence_run_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Attribution confidence run {confidence_run_id} does not exist")
    if row["status"] != "completed":
        raise ValueError(f"Attribution confidence run {confidence_run_id} is not completed")
    return row


def _is_stale(
    connection: sqlite3.Connection,
    confidence_run: sqlite3.Row,
    message_id: int,
    *,
    allow_excluded_evidence: bool = False,
) -> bool:
    run_id = int(confidence_run["attribution_confidence_run_id"])
    source_identity_run_id = int(confidence_run["identity_inference_run_id"])
    if confidence_run["ruleset_version"] != "attribution_confidence_v1":
        return True
    if not allow_excluded_evidence and connection.execute(
        """SELECT 1 FROM attribution_review_input_changes
           WHERE julianday(changed_at) > julianday(?) LIMIT 1""",
        (confidence_run["started_at"],),
    ).fetchone() is not None:
        return True
    scope = json.loads(confidence_run["input_scope_json"])
    scoped_message_ids = scope.get("message_observation_ids", [])
    if scoped_message_ids:
        latest_input_id = max(scoped_message_ids)
        parameters: list[Any] = [latest_input_id]
        query = """SELECT 1 FROM messages message
                   WHERE message.message_observation_id > ?"""
        if scope.get("server_id") is not None:
            query += """ AND EXISTS (
                SELECT 1 FROM channels channel
                WHERE channel.channel_id = message.channel_id AND channel.server_id = ?
            )"""
            parameters.append(scope["server_id"])
        if scope.get("channel_id") is not None:
            query += " AND message.channel_id = ?"
            parameters.append(scope["channel_id"])
        query += " LIMIT 1"
        if connection.execute(query, parameters).fetchone() is not None:
            return True
    if connection.execute(
        """SELECT 1
           FROM attribution_review_evidence_overrides override
           JOIN attribution_review_events event
             ON event.attribution_review_event_id = override.review_event_id
           WHERE override.message_observation_id = ?
             AND julianday(event.happened_at) > julianday(?)
           LIMIT 1""",
        (message_id, confidence_run["started_at"]),
    ).fetchone() is not None:
        return True
    later_confidence = connection.execute(
        """SELECT 1
           FROM attribution_confidence_candidates candidate
           JOIN attribution_confidence_runs run
             ON run.attribution_confidence_run_id = candidate.attribution_confidence_run_id
           WHERE candidate.message_observation_id = ?
             AND candidate.attribution_confidence_run_id > ?
             AND run.status = 'completed'
           LIMIT 1""",
        (message_id, run_id),
    ).fetchone()
    if later_confidence is not None:
        return True
    if connection.execute(
        """SELECT 1 FROM identity_candidate_evidence
           WHERE message_observation_id = ? AND identity_inference_run_id > ?
           LIMIT 1""",
        (message_id, source_identity_run_id),
    ).fetchone() is not None:
        return True
    provisional_ids = [
        int(row[0])
        for row in connection.execute(
            """SELECT DISTINCT provisional_identity_candidate_id
               FROM attribution_confidence_candidates
               WHERE attribution_confidence_run_id = ?
                 AND message_observation_id = ?
                 AND provisional_identity_candidate_id IS NOT NULL""",
            (run_id, message_id),
        )
    ]
    if provisional_ids:
        placeholders = ",".join("?" for _ in provisional_ids)
        if connection.execute(
            f"""SELECT 1 FROM identity_candidate_review_events
                WHERE candidate_id IN ({placeholders}) AND happened_at > ?
                LIMIT 1""",
            (*provisional_ids, confidence_run["started_at"]),
        ).fetchone() is not None:
            return True
    return connection.execute(
        """SELECT 1 FROM attribution_events event
           JOIN message_attributions attribution
             ON attribution.attribution_id = event.attribution_id
           WHERE attribution.message_observation_id = ?
             AND event.happened_at > ?
           LIMIT 1""",
        (message_id, confidence_run["started_at"]),
    ).fetchone() is not None


def _candidate_evidence_signature(
    connection: sqlite3.Connection,
    confidence_candidate_id: int,
) -> str:
    rows = list_attribution_confidence_evidence(connection, confidence_candidate_id)
    evidence: list[dict[str, Any]] = []
    for row in rows:
        value = json.loads(row["evidence_json"])
        evidence.append({
            "evidence_type": row["evidence_type"],
            "evidence_strength": row["evidence_strength"],
            "direction": row["direction"],
            "weight": row["weight"],
            "contribution": row["contribution"],
            "evidence": value,
        })
    evidence.sort(key=_canonical_json)
    return hashlib.sha256(_canonical_json(evidence).encode("utf-8")).hexdigest()


def list_attribution_review_queue(
    connection: sqlite3.Connection,
    *,
    confidence_run_id: int | None = None,
    priorities: Sequence[str] = ("high", "normal", "low"),
    include_decided: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Build a GUI-neutral queue from an immutable F1 snapshot and current decisions."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if not priorities or any(item not in {"high", "normal", "low"} for item in priorities):
        raise ValueError("priorities must contain only high, normal, or low")

    if confidence_run_id is None:
        row = connection.execute(
            """SELECT attribution_confidence_run_id
               FROM attribution_confidence_runs
               WHERE status = 'completed'
               ORDER BY attribution_confidence_run_id DESC LIMIT 1"""
        ).fetchone()
        if row is None:
            return []
        confidence_run_id = int(row[0])
    confidence_run_id = _positive_id(confidence_run_id, "confidence_run_id")
    confidence_run = _confidence_run(connection, confidence_run_id)
    rows = connection.execute(
        """SELECT candidate.*, message.discord_message_id, message.timestamp_utc,
                  message.content_original, message.author_discord_user_id_original,
                  channel.name_current AS channel_name,
                  server.name_current AS server_name,
                  character.character_name,
                  provisional.display_label AS provisional_label
           FROM attribution_confidence_candidates candidate
           JOIN messages message
             ON message.message_observation_id = candidate.message_observation_id
           LEFT JOIN channels channel ON channel.channel_id = message.channel_id
           LEFT JOIN servers server ON server.server_id = channel.server_id
           LEFT JOIN characters character ON character.character_id = candidate.character_id
           LEFT JOIN provisional_identity_candidates provisional
             ON provisional.candidate_id = candidate.provisional_identity_candidate_id
           WHERE candidate.attribution_confidence_run_id = ?
           ORDER BY candidate.message_observation_id, candidate.candidate_rank""",
        (confidence_run_id,),
    ).fetchall()

    by_message: dict[int, list[dict[str, Any]]] = defaultdict(list)
    headers: dict[int, dict[str, Any]] = {}
    for row in rows:
        message_id = int(row["message_observation_id"])
        candidate = dict(row)
        candidate["evidence"] = [
            dict(item)
            for item in list_attribution_confidence_evidence(
                connection, int(row["attribution_confidence_candidate_id"])
            )
        ]
        by_message[message_id].append(candidate)
        headers[message_id] = {
            "message_observation_id": message_id,
            "discord_message_id": row["discord_message_id"],
            "timestamp_utc": row["timestamp_utc"],
            "content_original": row["content_original"],
            "author_discord_user_id_original": row["author_discord_user_id_original"],
            "channel_name": row["channel_name"],
            "server_name": row["server_name"],
        }

    priority_rank = {"high": 0, "normal": 1, "low": 2}
    priority_set = set(priorities)
    queue: list[dict[str, Any]] = []
    for message_id, candidates in by_message.items():
        decisions = [
            dict(row)
            for row in connection.execute(
                """SELECT decision.*, event.action, event.actor, event.happened_at,
                          event.note,
                          event.detail_json
                   FROM attribution_review_decisions decision
                   JOIN attribution_review_events event
                     ON event.attribution_review_event_id = decision.latest_event_id
                   WHERE decision.message_observation_id = ?
                   ORDER BY decision.attribution_review_decision_id""",
                (message_id,),
            )
        ]
        authoritative = [
            dict(row)
            for row in connection.execute(
                """SELECT attribution.attribution_id, attribution.character_id,
                          character.character_name, attribution.player_id,
                          player.player_name, attribution.method, attribution.status,
                          attribution.reviewed_by, attribution.reviewed_at,
                          attribution.review_note
                   FROM message_attributions attribution
                   LEFT JOIN characters character ON character.character_id = attribution.character_id
                   LEFT JOIN players player ON player.player_id = attribution.player_id
                   WHERE attribution.message_observation_id = ?
                     AND attribution.status IN ('accepted', 'rejected', 'pending')
                   ORDER BY attribution.attribution_id""",
                (message_id,),
            )
        ]
        stale = _is_stale(connection, confidence_run, message_id)
        excluded = connection.execute(
            """SELECT is_excluded FROM attribution_review_evidence_overrides
               WHERE message_observation_id = ?""",
            (message_id,),
        ).fetchone()
        is_excluded = excluded is not None and bool(excluded["is_excluded"])
        has_conflict = (
            any(item["status"] == "conflicted" for item in decisions)
            or any(item["classification"] == "conflicted" for item in candidates)
        )
        has_resolved = any(
            item["status"] in {"accepted", "unresolved"} for item in decisions
        )
        if not include_decided and has_resolved and not has_conflict and not stale:
            continue
        needs_review = (
            any(candidate["mandatory_review"] for candidate in candidates)
            or any(candidate["classification"] in {"conflicted", "unresolved"} for candidate in candidates)
        )
        if not needs_review and not include_decided and not stale and not is_excluded:
            continue
        current_priority = (
            "high" if stale or has_conflict else
            min((candidate["review_priority"] for candidate in candidates),
                key=lambda item: priority_rank[item])
        )
        if current_priority not in priority_set:
            continue
        queue.append({
            **headers[message_id],
            "confidence_run_id": confidence_run_id,
            "identity_inference_run_id": int(confidence_run["identity_inference_run_id"]),
            "state": "conflicted" if has_conflict else (
                "excluded" if is_excluded else "stale" if stale else
                "already_resolved" if any(item["status"] == "accepted" for item in decisions)
                else "unresolved" if any(item["status"] == "unresolved" for item in decisions)
                else "unresolved" if any(
                    item["candidate_kind"] in {"provisional_identity", "unresolved"}
                    for item in candidates
                )
                else "review_required" if needs_review else "already_resolved"
            ),
            "reviewed": bool(decisions),
            "human_decision_status": (
                "unresolved" if any(item["status"] == "unresolved" for item in decisions)
                else "accepted" if any(item["status"] == "accepted" for item in decisions)
                else None
            ),
            "review_priority": current_priority,
            "stale": stale,
            "decisions": decisions,
            "authoritative_attributions": authoritative,
            "candidates": candidates,
        })
    queue.sort(key=lambda item: (
        priority_rank[item["review_priority"]],
        min(candidate["candidate_rank"] for candidate in item["candidates"]),
        item["timestamp_utc"] or "",
        item["message_observation_id"],
    ))
    return queue[:limit]


def list_attribution_review_history(
    connection: sqlite3.Connection,
    message_observation_id: int,
) -> list[sqlite3.Row]:
    message_observation_id = _positive_id(message_observation_id, "message_observation_id")
    return connection.execute(
        """SELECT event.*, character.character_name,
                  provisional.display_label AS provisional_label,
                  target.display_label AS target_label
           FROM attribution_review_events event
           LEFT JOIN characters character ON character.character_id = event.character_id
           LEFT JOIN provisional_identity_candidates provisional
             ON provisional.candidate_id = event.provisional_candidate_id
           LEFT JOIN provisional_identity_candidates target
             ON target.candidate_id = event.target_candidate_id
           WHERE event.message_observation_id = ?
           ORDER BY event.attribution_review_event_id""",
        (message_observation_id,),
    ).fetchall()


def get_attribution_review_candidate(
    connection: sqlite3.Connection,
    confidence_candidate_id: int,
    *,
    representative_message_limit: int = 5,
) -> dict[str, Any]:
    """Return a candidate-centered read model for a future review client."""
    confidence_candidate_id = _positive_id(confidence_candidate_id, "confidence_candidate_id")
    if (
        isinstance(representative_message_limit, bool)
        or not isinstance(representative_message_limit, int)
        or representative_message_limit < 1
    ):
        raise ValueError("representative_message_limit must be a positive integer")
    selected = connection.execute(
        """SELECT candidate.*, run.identity_inference_run_id,
                  run.ruleset_version, run.input_fingerprint, run.status AS run_status
           FROM attribution_confidence_candidates candidate
           JOIN attribution_confidence_runs run
             ON run.attribution_confidence_run_id = candidate.attribution_confidence_run_id
           WHERE candidate.attribution_confidence_candidate_id = ?""",
        (confidence_candidate_id,),
    ).fetchone()
    if selected is None:
        raise ValueError(f"Confidence candidate {confidence_candidate_id} does not exist")

    run_id = int(selected["attribution_confidence_run_id"])
    candidate_kind = str(selected["candidate_kind"])
    if candidate_kind == "character":
        key_column, key_value = "character_id", selected["character_id"]
    elif candidate_kind == "provisional_identity":
        key_column, key_value = "provisional_identity_candidate_id", selected[
            "provisional_identity_candidate_id"
        ]
    else:
        key_column, key_value = "message_observation_id", selected["message_observation_id"]
    candidate_rows = connection.execute(
        f"""SELECT * FROM attribution_confidence_candidates
            WHERE attribution_confidence_run_id = ? AND candidate_kind = ?
              AND {key_column} IS ?
            ORDER BY message_observation_id, candidate_rank""",
        (run_id, candidate_kind, key_value),
    ).fetchall()
    if candidate_kind in {"character", "provisional_identity"}:
        selected_signature = _candidate_evidence_signature(
            connection, confidence_candidate_id
        )
        candidate_rows = [
            row for row in candidate_rows
            if _candidate_evidence_signature(
                connection, int(row["attribution_confidence_candidate_id"])
            ) == selected_signature
        ]
    else:
        candidate_rows = [selected]
    message_ids = tuple(sorted({int(row["message_observation_id"]) for row in candidate_rows}))
    placeholders = ",".join("?" for _ in message_ids)
    messages = connection.execute(
        f"""SELECT message.message_observation_id, message.discord_message_id,
                   message.timestamp_utc, message.content_original,
                   message.author_discord_user_id_original,
                   channel.name_current AS channel_name,
                   server.name_current AS server_name
            FROM messages message
            LEFT JOIN channels channel ON channel.channel_id = message.channel_id
            LEFT JOIN servers server ON server.server_id = channel.server_id
            WHERE message.message_observation_id IN ({placeholders})
            ORDER BY message.timestamp_utc, message.message_observation_id""",
        message_ids,
    ).fetchall()
    message_records = [dict(row) for row in messages]
    current = dict(selected)
    current["evidence"] = [
        dict(row)
        for row in list_attribution_confidence_evidence(
            connection, confidence_candidate_id
        )
    ]
    message_id = int(selected["message_observation_id"])
    alternatives = [
        dict(row)
        for row in connection.execute(
            """SELECT candidate.*, character.character_name,
                      provisional.display_label AS provisional_label
               FROM attribution_confidence_candidates candidate
               LEFT JOIN characters character ON character.character_id = candidate.character_id
               LEFT JOIN provisional_identity_candidates provisional
                 ON provisional.candidate_id = candidate.provisional_identity_candidate_id
               WHERE candidate.attribution_confidence_run_id = ?
                 AND candidate.message_observation_id = ?
               ORDER BY candidate.candidate_rank""",
            (run_id, message_id),
        )
    ]
    decisions = [
        dict(row)
        for row in connection.execute(
            """SELECT decision.*, event.action, event.actor, event.happened_at,
                      event.note, event.previous_state_json
               FROM attribution_review_decisions decision
               JOIN attribution_review_events event
                 ON event.attribution_review_event_id = decision.latest_event_id
               WHERE decision.message_observation_id IN ("""
            + placeholders
            + """) ORDER BY event.attribution_review_event_id""",
            message_ids,
        )
    ]
    history = [
        dict(row)
        for item_id in message_ids
        for row in list_attribution_review_history(connection, item_id)
    ]
    authoritative = [
        dict(row)
        for row in connection.execute(
            f"""SELECT attribution.message_observation_id, attribution.attribution_id,
                       attribution.character_id, character.character_name,
                       attribution.player_id, player.player_name, attribution.method,
                       attribution.status, attribution.reviewed_by,
                       attribution.reviewed_at, attribution.review_note
                FROM message_attributions attribution
                LEFT JOIN characters character ON character.character_id = attribution.character_id
                LEFT JOIN players player ON player.player_id = attribution.player_id
                WHERE attribution.message_observation_id IN ({placeholders})
                  AND attribution.status IN ('accepted', 'rejected', 'pending')
                ORDER BY attribution.message_observation_id, attribution.attribution_id""",
            message_ids,
        )
    ]
    selected_message = next(
        (item for item in message_records if item["message_observation_id"] == message_id),
        None,
    )
    exclusion = connection.execute(
        """SELECT is_excluded FROM attribution_review_evidence_overrides
           WHERE message_observation_id = ?""",
        (message_id,),
    ).fetchone()
    current["excluded"] = exclusion is not None and bool(exclusion["is_excluded"])
    current["stale"] = _is_stale(
        connection,
        _confidence_run(connection, run_id),
        message_id,
    )
    return {
        "candidate": current,
        "candidate_type": candidate_kind,
        "current_review_state": (
            "excluded" if current["excluded"]
            else "stale" if current["stale"]
            else "conflicted" if current["classification"] == "conflicted"
            else "review_required" if current["mandatory_review"]
            else "already_resolved"
        ),
        "recommended_character_id": selected["character_id"],
        "alternatives": alternatives,
        "operational_confidence": selected["classification"],
        "review_priority": selected["review_priority"],
        "supporting_evidence": [
            item for item in current["evidence"] if item["direction"] == "supporting"
        ],
        "contradictory_evidence": [
            item for item in current["evidence"] if item["direction"] == "contradictory"
        ],
        "affected_message_count": len(message_ids),
        "affected_message_ids": list(message_ids),
        "representative_messages": message_records[:representative_message_limit],
        "selected_message": selected_message,
        "source": {
            "confidence_run_id": run_id,
            "identity_inference_run_id": int(selected["identity_inference_run_id"]),
            "ruleset_version": selected["ruleset_version"],
            "input_fingerprint": selected["input_fingerprint"],
        },
        "previous_human_decisions": decisions,
        "current_authoritative_attributions": authoritative,
        "audit_history": history,
    }


def _upsert_decision(
    connection: sqlite3.Connection,
    *,
    message_id: int,
    candidate_kind: str,
    character_id: int | None,
    provisional_candidate_id: int | None,
    status: str,
    event_id: int,
    now: str,
) -> None:
    if status == "accepted":
        connection.execute(
            """UPDATE attribution_review_decisions
               SET status = 'superseded', updated_at = ?
               WHERE message_observation_id = ? AND candidate_kind = 'character'
                 AND status = 'accepted' AND character_id != ?""",
            (now, message_id, character_id),
        )
    existing = connection.execute(
        """SELECT attribution_review_decision_id
           FROM attribution_review_decisions
           WHERE message_observation_id = ? AND candidate_kind = ?
             AND character_id IS ? AND provisional_candidate_id IS ?""",
        (message_id, candidate_kind, character_id, provisional_candidate_id),
    ).fetchone()
    if existing is None:
        connection.execute(
            """INSERT INTO attribution_review_decisions(
                   message_observation_id, candidate_kind, character_id,
                   provisional_candidate_id, status, latest_event_id, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                message_id, candidate_kind, character_id, provisional_candidate_id,
                status, event_id, now,
            ),
        )
    else:
        connection.execute(
            """UPDATE attribution_review_decisions
               SET status = ?, latest_event_id = ?, updated_at = ?
               WHERE attribution_review_decision_id = ?""",
            (status, event_id, now, existing[0]),
        )


def _check_bulk_equivalence(
    connection: sqlite3.Connection,
    confidence_run_id: int,
    first_candidate: sqlite3.Row,
    message_ids: Sequence[int],
) -> list[sqlite3.Row]:
    first_signature = _candidate_evidence_signature(
        connection, int(first_candidate["attribution_confidence_candidate_id"])
    )
    matched = {int(first_candidate["message_observation_id"]): first_candidate}
    for message_id in message_ids:
        if message_id == first_candidate["message_observation_id"]:
            continue
        row = connection.execute(
            """SELECT * FROM attribution_confidence_candidates
               WHERE attribution_confidence_run_id = ?
                 AND message_observation_id = ?
                 AND candidate_kind = ? AND character_id IS ?
                 AND provisional_identity_candidate_id IS ?""",
            (
                confidence_run_id, message_id, first_candidate["candidate_kind"],
                first_candidate["character_id"],
                first_candidate["provisional_identity_candidate_id"],
            ),
        ).fetchone()
        if row is None or _candidate_evidence_signature(
            connection, int(row["attribution_confidence_candidate_id"])
        ) != first_signature:
            raise ValueError(
                "Bulk review requires the same candidate and identical evidence "
                "for every message"
            )
        matched[message_id] = row
    return [matched[message_id] for message_id in message_ids]


def review_attribution_candidate(
    connection: sqlite3.Connection,
    *,
    confidence_run_id: int,
    confidence_candidate_id: int,
    action: ReviewAction,
    actor: str,
    note: str | None = None,
    character_id: int | None = None,
    target_candidate_id: int | None = None,
    message_observation_ids: Sequence[int] = (),
    expected_latest_event_id: int | None = None,
) -> list[int]:
    """Apply a reviewed action, preserving authoritative claims and immutable F2 history."""
    if action not in _VALID_ACTIONS:
        raise ValueError(f"Unsupported attribution review action: {action}")
    if not isinstance(actor, str) or not actor.strip():
        raise ValueError("Reviewer identity must not be empty")
    confidence_run_id = _positive_id(confidence_run_id, "confidence_run_id")
    confidence_candidate_id = _positive_id(confidence_candidate_id, "confidence_candidate_id")
    provided_ids = tuple(message_observation_ids)
    for value in provided_ids:
        _positive_id(value, "message_observation_id")
    message_ids = tuple(sorted(set(provided_ids)))
    first = connection.execute(
        """SELECT * FROM attribution_confidence_candidates
           WHERE attribution_confidence_candidate_id = ?
             AND attribution_confidence_run_id = ?""",
        (confidence_candidate_id, confidence_run_id),
    ).fetchone()
    if first is None:
        raise ValueError("Confidence candidate does not belong to the selected run")
    primary_message_id = int(first["message_observation_id"])
    if not message_ids:
        message_ids = (primary_message_id,)
    if primary_message_id not in message_ids:
        raise ValueError("Bulk message IDs must include the selected candidate's message")
    confidence_run = _confidence_run(connection, confidence_run_id)
    if any(
        _is_stale(
            connection, confidence_run, message_id,
            allow_excluded_evidence=action == "restore_evidence",
        )
        for message_id in message_ids
    ):
        raise ValueError("Attribution review input is stale; recompute F1 before reviewing")
    matched_candidates = (
        _check_bulk_equivalence(connection, confidence_run_id, first, message_ids)
        if len(message_ids) > 1 else [first]
    )
    if expected_latest_event_id is not None:
        expected_latest_event_id = _positive_id(
            expected_latest_event_id, "expected_latest_event_id"
        )
        for message_id in message_ids:
            current = connection.execute(
                """SELECT MAX(latest_event_id) FROM attribution_review_decisions
                   WHERE message_observation_id = ?""",
                (message_id,),
            ).fetchone()[0]
            if current != expected_latest_event_id:
                raise ValueError(
                    f"Review state changed for message {message_id}; refresh the queue"
                )

    if action in _CHARACTER_ACTIONS:
        if action == "assign_character":
            if character_id is None:
                raise ValueError("assign_character requires character_id")
        else:
            if first["candidate_kind"] != "character" or first["character_id"] is None:
                raise ValueError(f"{action} requires a character candidate")
            if character_id is not None and character_id != first["character_id"]:
                raise ValueError(f"{action} does not accept a different character_id")
            character_id = int(first["character_id"])
        character_id = _positive_id(character_id, "character_id")
        if connection.execute(
            "SELECT 1 FROM characters WHERE character_id = ?", (character_id,)
        ).fetchone() is None:
            raise ValueError(f"Character {character_id} does not exist")
    elif action == "link_provisional":
        if first["candidate_kind"] != "provisional_identity":
            raise ValueError("link_provisional requires a provisional identity candidate")
        if character_id is None:
            raise ValueError("link_provisional requires character_id")
        character_id = _positive_id(character_id, "character_id")
        if connection.execute(
            "SELECT 1 FROM characters WHERE character_id = ?", (character_id,)
        ).fetchone() is None:
            raise ValueError(f"Character {character_id} does not exist")
    elif action in _EVIDENCE_ACTIONS:
        if action not in {"exclude_evidence", "restore_evidence"} and first["candidate_kind"] != "provisional_identity":
            raise ValueError(f"{action} requires a provisional identity candidate")
        if action == "reassign_evidence":
            if target_candidate_id is None:
                raise ValueError("reassign_evidence requires target_candidate_id")
        elif target_candidate_id is not None:
            raise ValueError(f"{action} does not accept target_candidate_id")
    elif action == "merge_candidates":
        if first["candidate_kind"] != "provisional_identity":
            raise ValueError("merge_candidates requires a provisional identity candidate")
        if target_candidate_id is None or target_candidate_id == first["provisional_identity_candidate_id"]:
            raise ValueError("merge_candidates requires a different target candidate")
    elif action == "leave_unresolved":
        if first["candidate_kind"] not in {"provisional_identity", "unresolved"}:
            raise ValueError("leave_unresolved requires a provisional or unresolved candidate")
    if action in {"split_evidence", "reassign_evidence", "exclude_evidence", "merge_candidates"}:
        if not message_ids:
            raise ValueError(f"{action} requires at least one message")
    if action == "restore_evidence":
        if not message_ids or any(
            (row := connection.execute(
                """SELECT is_excluded FROM attribution_review_evidence_overrides
                   WHERE message_observation_id = ?""",
                (message_id,),
            ).fetchone()) is None or not row["is_excluded"]
            for message_id in message_ids
        ):
            raise ValueError("restore_evidence requires excluded evidence for every selected message")

    identity_review_event_id: int | None = None
    resulting_candidate_id: int | None = None
    affected_ids = message_ids
    operation_savepoint = "attribution_review_action"
    connection.execute("SAVEPOINT attribution_review_action")
    try:
        if action == "link_provisional":
            source_candidate_id = int(first["provisional_identity_candidate_id"])
            review_identity_candidate(
                connection, source_candidate_id, decision="link_to_character",
                actor=actor, character_id=character_id, note=note,
            )
            latest_event = connection.execute(
                """SELECT review_event_id FROM identity_candidate_review_events
                   WHERE candidate_id = ? ORDER BY review_event_id DESC LIMIT 1""",
                (source_candidate_id,),
            ).fetchone()
            identity_review_event_id = int(latest_event[0])
        elif action in {"split_evidence", "reassign_evidence"}:
            source_candidate_id = int(first["provisional_identity_candidate_id"])
            if action == "split_evidence":
                resulting_candidate_id = review_identity_candidate(
                    connection, source_candidate_id, decision="split_evidence",
                    actor=actor, message_observation_ids=message_ids, note=note,
                )
            elif action == "reassign_evidence":
                assert target_candidate_id is not None
                review_identity_candidate(
                    connection, source_candidate_id, decision="reassign_evidence",
                    actor=actor, target_candidate_id=target_candidate_id,
                    message_observation_ids=message_ids, note=note,
                )
                resulting_candidate_id = target_candidate_id
            else:
                review_identity_candidate(
                    connection, source_candidate_id, decision="exclude_evidence",
                    actor=actor, message_observation_ids=message_ids, note=note,
                )
            latest_event = connection.execute(
                """SELECT review_event_id FROM identity_candidate_review_events
                   WHERE candidate_id = ? ORDER BY review_event_id DESC LIMIT 1""",
                (source_candidate_id,),
            ).fetchone()
            identity_review_event_id = int(latest_event[0])
        elif action == "merge_candidates":
            source_candidate_id = int(first["provisional_identity_candidate_id"])
            assert target_candidate_id is not None
            affected_ids = tuple(
                int(row["message_observation_id"])
                for row in list_identity_candidate_evidence(connection, source_candidate_id)
            )
            if not affected_ids:
                raise ValueError("Cannot merge a provisional candidate with no active evidence")
            if any(_is_stale(connection, confidence_run, message_id) for message_id in affected_ids):
                raise ValueError("At least one merge input is stale; recompute F1 before reviewing")
            matched_candidates = [
                connection.execute(
                    """SELECT * FROM attribution_confidence_candidates
                       WHERE attribution_confidence_run_id = ?
                         AND message_observation_id = ?
                       ORDER BY candidate_rank LIMIT 1""",
                    (confidence_run_id, message_id),
                ).fetchone()
                for message_id in affected_ids
            ]
            if any(candidate is None for candidate in matched_candidates):
                raise ValueError("The selected F1 run does not cover all merge evidence")
            review_identity_candidate(
                connection, source_candidate_id, decision="reassign_evidence",
                actor=actor, target_candidate_id=target_candidate_id,
                message_observation_ids=affected_ids, note=note,
            )
            latest_event = connection.execute(
                """SELECT review_event_id FROM identity_candidate_review_events
                   WHERE candidate_id = ? ORDER BY review_event_id DESC LIMIT 1""",
                (source_candidate_id,),
            ).fetchone()
            identity_review_event_id = int(latest_event[0])
            message_ids = affected_ids
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT attribution_review_action")
        connection.execute("RELEASE SAVEPOINT attribution_review_action")
        raise

    group_id = str(uuid.uuid4())
    now = _now()
    inserted: list[int] = []
    connection.execute("SAVEPOINT attribution_review_records")
    try:
        for message_id, candidate in zip(message_ids, matched_candidates, strict=True):
            if candidate is None:
                raise ValueError(f"No confidence candidate exists for message {message_id}")
            selected_candidate_id = int(candidate["attribution_confidence_candidate_id"])
            selected_kind = str(candidate["candidate_kind"])
            selected_character_id = (
                int(candidate["character_id"]) if candidate["character_id"] is not None else None
            )
            selected_provisional_id = (
                int(candidate["provisional_identity_candidate_id"])
                if candidate["provisional_identity_candidate_id"] is not None else None
            )
            source_fingerprint = _candidate_evidence_signature(connection, selected_candidate_id)
            attribution_id: int | None = None
            previous_state = {
                "review_decisions": [
                    dict(row)
                    for row in connection.execute(
                        """SELECT candidate_kind, character_id, provisional_candidate_id,
                                  status, latest_event_id
                           FROM attribution_review_decisions
                           WHERE message_observation_id = ?
                           ORDER BY attribution_review_decision_id""",
                        (message_id,),
                    )
                ],
                "authoritative_attributions": [
                    dict(row)
                    for row in connection.execute(
                        """SELECT attribution_id, character_id, player_id, status,
                                  method, reviewed_by, reviewed_at, review_note
                           FROM message_attributions
                           WHERE message_observation_id = ?
                             AND status IN ('accepted', 'rejected', 'pending')
                           ORDER BY attribution_id""",
                        (message_id,),
                    )
                ],
            }
            outcome: str
            decision_kind = selected_kind
            decision_character_id = selected_character_id
            decision_provisional_id = selected_provisional_id

            if action in {"confirm", "assign_character"}:
                decision_character_id = int(character_id)
                decision_kind = "character"
                accepted = connection.execute(
                    """SELECT attribution_id FROM message_attributions
                       WHERE message_observation_id = ? AND character_id = ?
                         AND status = 'accepted'""",
                    (message_id, decision_character_id),
                ).fetchone()
                if accepted is not None:
                    attribution_id = int(accepted[0])
                else:
                    attribution_id = propose_attribution(
                        connection, message_id, character_id=decision_character_id,
                        method="manual",
                        evidence={
                            "review_system": "attribution_review_v1",
                            "confidence_run_id": confidence_run_id,
                            "confidence_candidate_id": selected_candidate_id,
                            "action": action,
                        },
                        created_by=actor.strip(),
                    )
                    state = connection.execute(
                        "SELECT status FROM message_attributions WHERE attribution_id = ?",
                        (attribution_id,),
                    ).fetchone()[0]
                    if state == "pending":
                        review_attribution(
                            connection, attribution_id, decision="accepted",
                            actor=actor.strip(), note=note,
                        )
                outcome = "accepted"
                decision_status = "accepted"
            elif action == "reject":
                decision_character_id = selected_character_id
                accepted = connection.execute(
                    """SELECT attribution_id FROM message_attributions
                       WHERE message_observation_id = ? AND character_id = ?
                         AND status = 'accepted'""",
                    (message_id, decision_character_id),
                ).fetchone()
                if accepted is not None:
                    attribution_id = int(accepted[0])
                    outcome = "conflicted"
                    decision_status = "conflicted"
                else:
                    attribution_id = propose_attribution(
                        connection, message_id, character_id=decision_character_id,
                        method="manual",
                        evidence={
                            "review_system": "attribution_review_v1",
                            "confidence_run_id": confidence_run_id,
                            "confidence_candidate_id": selected_candidate_id,
                            "action": "reject",
                        },
                        created_by=actor.strip(),
                    )
                    state = connection.execute(
                        "SELECT status FROM message_attributions WHERE attribution_id = ?",
                        (attribution_id,),
                    ).fetchone()[0]
                    if state == "pending":
                        review_attribution(
                            connection, attribution_id, decision="rejected",
                            actor=actor.strip(), note=note,
                        )
                    outcome = "rejected"
                    decision_status = "rejected"
            elif action == "leave_unresolved":
                decision_kind = "unresolved"
                decision_character_id = None
                decision_provisional_id = None
                existing_accepted = connection.execute(
                    """SELECT attribution_id FROM message_attributions
                       WHERE message_observation_id = ? AND status = 'accepted'""",
                    (message_id,),
                ).fetchone()
                if existing_accepted is not None:
                    outcome = "conflicted"
                    decision_status = "conflicted"
                else:
                    outcome = "unresolved"
                    decision_status = "unresolved"
            elif action == "link_provisional":
                event_character_id = int(character_id)
                decision_character_id = None
                outcome = "identity_linked"
                decision_status = "identity_linked"
            elif action in _EVIDENCE_ACTIONS or action == "merge_candidates":
                event_character_id = None
                decision_provisional_id = (
                    int(resulting_candidate_id)
                    if action == "split_evidence" and resulting_candidate_id is not None
                    else int(target_candidate_id)
                    if action in {"reassign_evidence", "merge_candidates"}
                    else selected_provisional_id
                )
                outcome = "evidence_adjusted"
                decision_status = "evidence_adjusted"
            else:
                raise RuntimeError(f"Unhandled review action: {action}")
            if action != "link_provisional":
                event_character_id = decision_character_id

            persisted_action = "exclude_evidence" if action == "restore_evidence" else action
            event_cursor = connection.execute(
                """INSERT INTO attribution_review_events(
                       review_group_id, message_observation_id, action, outcome,
                       actor, happened_at, note, confidence_run_id,
                       confidence_candidate_id, character_id,
                       provisional_candidate_id, target_candidate_id,
                       attribution_id, identity_review_event_id,
                       source_evidence_fingerprint, detail_json, previous_state_json,
                       requested_action
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    group_id, message_id, persisted_action, outcome, actor.strip(), now, note,
                    confidence_run_id, selected_candidate_id,
                    event_character_id, selected_provisional_id,
                    resulting_candidate_id or target_candidate_id,
                    attribution_id, identity_review_event_id, source_fingerprint,
                    _canonical_json({
                        "requested_action": action,
                        "affected_message_observation_ids": list(affected_ids),
                        "identity_review_event_id": identity_review_event_id,
                        "target_candidate_id": target_candidate_id,
                    }),
                    _canonical_json(previous_state),
                    action,
                ),
            )
            event_id = int(event_cursor.lastrowid)
            if action in {"exclude_evidence", "restore_evidence"}:
                connection.execute(
                    """INSERT INTO attribution_review_evidence_overrides(
                           message_observation_id, is_excluded, review_event_id, updated_at
                       ) VALUES (?, ?, ?, ?)
                       ON CONFLICT(message_observation_id) DO UPDATE SET
                           is_excluded = excluded.is_excluded,
                           review_event_id = excluded.review_event_id,
                           updated_at = excluded.updated_at""",
                    (message_id, int(action == "exclude_evidence"), event_id, now),
                )
            _upsert_decision(
                connection,
                message_id=message_id,
                candidate_kind=decision_kind,
                character_id=decision_character_id,
                provisional_candidate_id=decision_provisional_id,
                status=decision_status,
                event_id=event_id,
                now=now,
            )
            inserted.append(event_id)
        connection.execute("RELEASE SAVEPOINT attribution_review_records")
        connection.execute(f"RELEASE SAVEPOINT {operation_savepoint}")
        return inserted
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT attribution_review_action")
        connection.execute("RELEASE SAVEPOINT attribution_review_action")
        raise
