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


def _stale_message_ids(
    connection: sqlite3.Connection,
    confidence_run: sqlite3.Row,
    message_ids: Sequence[int],
) -> set[int]:
    if not message_ids:
        return set()
    run_id = int(confidence_run["attribution_confidence_run_id"])
    started_at = confidence_run["started_at"]
    if confidence_run["ruleset_version"] != "attribution_confidence_v1":
        return set(message_ids)
    if connection.execute(
        """SELECT 1 FROM attribution_review_input_changes
           WHERE julianday(changed_at) > julianday(?) LIMIT 1""",
        (started_at,),
    ).fetchone() is not None:
        return set(message_ids)

    scope = json.loads(confidence_run["input_scope_json"])
    scoped_message_ids = scope.get("message_observation_ids", [])
    if scoped_message_ids:
        query = """SELECT 1 FROM messages message
                   WHERE message.message_observation_id > ?"""
        parameters: list[Any] = [max(scoped_message_ids)]
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
            return set(message_ids)

    stale: set[int] = set()
    source_run_id = int(confidence_run["identity_inference_run_id"])
    for start in range(0, len(message_ids), 800):
        batch = message_ids[start : start + 800]
        placeholders = ",".join("?" for _ in batch)
        queries = (
            (
                """SELECT override.message_observation_id
                   FROM attribution_review_evidence_overrides override
                   JOIN attribution_review_events event
                     ON event.attribution_review_event_id = override.review_event_id
                   WHERE override.message_observation_id IN ({})
                     AND julianday(event.happened_at) > julianday(?)""",
                (*batch, started_at),
            ),
            (
                """SELECT DISTINCT candidate.message_observation_id
                   FROM attribution_confidence_candidates candidate
                   JOIN attribution_confidence_runs run
                     ON run.attribution_confidence_run_id = candidate.attribution_confidence_run_id
                   WHERE candidate.message_observation_id IN ({})
                     AND candidate.attribution_confidence_run_id > ?
                     AND run.status = 'completed'""",
                (*batch, run_id),
            ),
            (
                """SELECT DISTINCT message_observation_id
                   FROM identity_candidate_evidence
                   WHERE message_observation_id IN ({})
                     AND identity_inference_run_id > ?""",
                (*batch, source_run_id),
            ),
            (
                """SELECT DISTINCT attribution.message_observation_id
                   FROM attribution_events event
                   JOIN message_attributions attribution
                     ON attribution.attribution_id = event.attribution_id
                   WHERE attribution.message_observation_id IN ({})
                     AND event.happened_at > ?""",
                (*batch, started_at),
            ),
            (
                """SELECT DISTINCT candidate.message_observation_id
                   FROM attribution_confidence_candidates candidate
                   JOIN identity_candidate_review_events event
                     ON event.candidate_id = candidate.provisional_identity_candidate_id
                   WHERE candidate.attribution_confidence_run_id = ?
                     AND candidate.message_observation_id IN ({})
                     AND event.happened_at > ?""",
                (run_id, *batch, started_at),
            ),
        )
        for query, parameters in queries:
            formatted_query = query.format(placeholders)
            stale.update(
                int(row[0])
                for row in connection.execute(formatted_query, parameters)
            )
    return stale


def _candidate_evidence_signatures(
    connection: sqlite3.Connection,
    confidence_candidate_ids: Sequence[int],
) -> dict[int, str]:
    evidence_by_candidate: dict[int, list[dict[str, Any]]] = {
        candidate_id: [] for candidate_id in confidence_candidate_ids
    }
    for start in range(0, len(confidence_candidate_ids), 800):
        batch = confidence_candidate_ids[start : start + 800]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""SELECT attribution_confidence_candidate_id, evidence_type,
                       evidence_strength, direction, weight, contribution,
                       evidence_json
                FROM attribution_confidence_evidence
                WHERE attribution_confidence_candidate_id IN ({placeholders})
                ORDER BY attribution_confidence_candidate_id,
                         attribution_confidence_evidence_id""",
            tuple(batch),
        ).fetchall()
        for row in rows:
            evidence_by_candidate[int(row["attribution_confidence_candidate_id"])].append({
                "evidence_type": row["evidence_type"],
                "evidence_strength": row["evidence_strength"],
                "direction": row["direction"],
                "weight": row["weight"],
                "contribution": row["contribution"],
                "evidence": json.loads(row["evidence_json"]),
            })
    return {
        candidate_id: hashlib.sha256(
            _canonical_json(sorted(evidence, key=_canonical_json)).encode("utf-8")
        ).hexdigest()
        for candidate_id, evidence in evidence_by_candidate.items()
    }


def _candidate_evidence_signature(
    connection: sqlite3.Connection,
    confidence_candidate_id: int,
) -> str:
    return _candidate_evidence_signatures(connection, (confidence_candidate_id,))[
        confidence_candidate_id
    ]


def _evidence_by_candidate(
    connection: sqlite3.Connection,
    confidence_candidate_ids: Sequence[int],
) -> dict[int, list[dict[str, Any]]]:
    evidence_by_candidate: dict[int, list[dict[str, Any]]] = {
        candidate_id: [] for candidate_id in confidence_candidate_ids
    }
    for start in range(0, len(confidence_candidate_ids), 800):
        batch = confidence_candidate_ids[start : start + 800]
        if not batch:
            continue
        placeholders = ",".join("?" for _ in batch)
        for row in connection.execute(
            f"""SELECT * FROM attribution_confidence_evidence
                WHERE attribution_confidence_candidate_id IN ({placeholders})
                ORDER BY attribution_confidence_candidate_id,
                         attribution_confidence_evidence_id""",
            tuple(batch),
        ):
            evidence_by_candidate[int(row["attribution_confidence_candidate_id"])].append(
                dict(row)
            )
    return evidence_by_candidate


def list_attribution_review_queue(
    connection: sqlite3.Connection,
    *,
    confidence_run_id: int | None = None,
    priorities: Sequence[str] = ("high", "normal", "low"),
    include_decided: bool = False,
    limit: int = 100,
    offset: int = 0,
    states: Sequence[str] | None = None,
    classifications: Sequence[str] | None = None,
    candidate_kinds: Sequence[str] | None = None,
    label_query: str | None = None,
    server_name: str | None = None,
    channel_name: str | None = None,
    stale_only: bool | None = None,
    include_evidence: bool = True,
    include_message_content: bool = True,
) -> list[dict[str, Any]]:
    """Build a GUI-neutral queue from an immutable F1 snapshot and current decisions."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
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
    content_column = "message.content_original," if include_message_content else ""
    rows = connection.execute(
        f"""SELECT candidate.*, message.discord_message_id, message.timestamp_utc,
                  {content_column} message.author_discord_user_id_original,
                  channel.name_current AS channel_name,
                  server.name_current AS server_name,
                  character.character_name,
                  provisional.display_label AS provisional_label,
                  (SELECT COUNT(*) FROM attribution_confidence_evidence evidence
                   WHERE evidence.attribution_confidence_candidate_id =
                         candidate.attribution_confidence_candidate_id) AS evidence_count
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

    evidence_by_candidate = (
        _evidence_by_candidate(
            connection,
            tuple(int(row["attribution_confidence_candidate_id"]) for row in rows),
        )
        if include_evidence else {}
    )
    state_filter = set(states) if states is not None else None
    classification_filter = set(classifications) if classifications is not None else None
    kind_filter = set(candidate_kinds) if candidate_kinds is not None else None
    label_filter = label_query.strip().casefold() if label_query else ""
    server_filter = server_name.strip().casefold() if server_name else ""
    channel_filter = channel_name.strip().casefold() if channel_name else ""
    by_message: dict[int, list[dict[str, Any]]] = defaultdict(list)
    headers: dict[int, dict[str, Any]] = {}
    for row in rows:
        message_id = int(row["message_observation_id"])
        candidate = dict(row)
        if include_evidence:
            candidate["evidence"] = evidence_by_candidate[
                int(row["attribution_confidence_candidate_id"])
            ]
        by_message[message_id].append(candidate)
        headers[message_id] = {
            "message_observation_id": message_id,
            "discord_message_id": row["discord_message_id"],
            "timestamp_utc": row["timestamp_utc"],
            "author_discord_user_id_original": row["author_discord_user_id_original"],
            "channel_name": row["channel_name"],
            "server_name": row["server_name"],
        }
        if include_message_content:
            headers[message_id]["content_original"] = row["content_original"]

    queued_message_ids = tuple(by_message)
    decisions_by_message: dict[int, list[dict[str, Any]]] = defaultdict(list)
    authoritative_by_message: dict[int, list[dict[str, Any]]] = defaultdict(list)
    excluded_message_ids: set[int] = set()
    for start in range(0, len(queued_message_ids), 800):
        batch = queued_message_ids[start : start + 800]
        placeholders = ",".join("?" for _ in batch)
        for row in connection.execute(
            f"""SELECT decision.*, event.action, event.actor, event.happened_at,
                       event.note, event.detail_json
                FROM attribution_review_decisions decision
                JOIN attribution_review_events event
                  ON event.attribution_review_event_id = decision.latest_event_id
                WHERE decision.message_observation_id IN ({placeholders})
                ORDER BY decision.attribution_review_decision_id""",
            batch,
        ):
            decisions_by_message[int(row["message_observation_id"])].append(dict(row))
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
            batch,
        ):
            authoritative_by_message[int(row["message_observation_id"])].append(dict(row))
        excluded_message_ids.update(
            int(row[0])
            for row in connection.execute(
                f"""SELECT message_observation_id
                    FROM attribution_review_evidence_overrides
                    WHERE message_observation_id IN ({placeholders})
                      AND is_excluded = 1""",
                batch,
            )
        )
    stale_message_ids = _stale_message_ids(
        connection, confidence_run, queued_message_ids
    )
    priority_rank = {"high": 0, "normal": 1, "low": 2}
    priority_set = set(priorities)
    queue: list[dict[str, Any]] = []
    for message_id, candidates in by_message.items():
        decisions = decisions_by_message[message_id]
        authoritative = authoritative_by_message[message_id]
        stale = message_id in stale_message_ids
        is_excluded = message_id in excluded_message_ids
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
        item = {
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
        }
        if state_filter is not None and item["state"] not in state_filter:
            continue
        if stale_only is not None and item["stale"] is not stale_only:
            continue
        if classification_filter is not None and not any(
            candidate["classification"] in classification_filter for candidate in candidates
        ):
            continue
        if kind_filter is not None and not any(
            candidate["candidate_kind"] in kind_filter for candidate in candidates
        ):
            continue
        if label_filter and not any(
            label_filter in str(
                candidate.get("character_name")
                or candidate.get("provisional_label")
                or candidate.get("candidate_label")
                or ""
            ).casefold()
            for candidate in candidates
        ):
            continue
        if server_filter and server_filter not in str(item["server_name"] or "").casefold():
            continue
        if channel_filter and channel_filter not in str(item["channel_name"] or "").casefold():
            continue
        queue.append(item)
    queue.sort(key=lambda item: (
        priority_rank[item["review_priority"]],
        min(candidate["candidate_rank"] for candidate in item["candidates"]),
        item["timestamp_utc"] or "",
        item["message_observation_id"],
    ))
    return queue[offset : offset + limit]


def list_attribution_review_characters(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """List registered characters available to F2 review actions."""
    return [
        dict(row)
        for row in connection.execute(
            "SELECT character_id, character_name FROM characters ORDER BY character_name COLLATE NOCASE"
        )
    ]


def list_attribution_review_candidate_messages(
    connection: sqlite3.Connection,
    confidence_candidate_id: int,
    *,
    offset: int = 0,
    limit: int = 25,
) -> dict[str, Any]:
    """Return a bounded page of messages with evidence identical to the selected candidate."""
    confidence_candidate_id = _positive_id(confidence_candidate_id, "confidence_candidate_id")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    selected = connection.execute(
        """SELECT * FROM attribution_confidence_candidates
           WHERE attribution_confidence_candidate_id = ?""",
        (confidence_candidate_id,),
    ).fetchone()
    if selected is None:
        raise ValueError(f"Confidence candidate {confidence_candidate_id} does not exist")

    if selected["candidate_kind"] == "character":
        key_column, key_value = "character_id", selected["character_id"]
    elif selected["candidate_kind"] == "provisional_identity":
        key_column, key_value = "provisional_identity_candidate_id", selected[
            "provisional_identity_candidate_id"
        ]
    else:
        key_column, key_value = "message_observation_id", selected["message_observation_id"]
    matching = connection.execute(
        f"""SELECT * FROM attribution_confidence_candidates
            WHERE attribution_confidence_run_id = ? AND candidate_kind = ?
              AND {key_column} IS ?
            ORDER BY message_observation_id""",
        (
            selected["attribution_confidence_run_id"],
            selected["candidate_kind"],
            key_value,
        ),
    ).fetchall()
    signatures = _candidate_evidence_signatures(
        connection,
        tuple(int(row["attribution_confidence_candidate_id"]) for row in matching),
    )
    signature = signatures[confidence_candidate_id]
    matched = [
        row for row in matching
        if signatures[int(row["attribution_confidence_candidate_id"])] == signature
    ]
    if selected["candidate_kind"] == "unresolved":
        matched = [selected]
    total = len(matched)
    page = matched[offset : offset + limit]
    if not page:
        return {"offset": offset, "limit": limit, "total": total, "messages": []}
    message_ids = [int(row["message_observation_id"]) for row in page]
    placeholders = ",".join("?" for _ in message_ids)
    message_rows = connection.execute(
        f"""SELECT message.message_observation_id, message.discord_message_id,
                   message.timestamp_utc, message.content_original,
                   message.author_discord_user_id_original,
               message.author_discord_name_original,
               message.author_display_name_original,
               message.author_nickname_original,
               channel.channel_id, channel.name_current AS channel_name,
               server.server_id, server.name_current AS server_name,
               message.source_id, source.source_path,
               COALESCE(review_override.is_excluded, 0) AS is_excluded
        FROM messages message
        LEFT JOIN channels channel ON channel.channel_id = message.channel_id
        LEFT JOIN servers server ON server.server_id = channel.server_id
        LEFT JOIN sources source ON source.source_id = message.source_id
        LEFT JOIN attribution_review_evidence_overrides review_override
          ON review_override.message_observation_id = message.message_observation_id
        WHERE message.message_observation_id IN ({placeholders})""",
        message_ids,
    ).fetchall()
    message_by_id = {int(row["message_observation_id"]): dict(row) for row in message_rows}
    evidence_by_candidate = _evidence_by_candidate(
        connection,
        tuple(int(row["attribution_confidence_candidate_id"]) for row in page),
    )
    review_events_by_message = {
        int(row["message_observation_id"]): row["latest_event_id"]
        for row in connection.execute(
            f"""SELECT message_observation_id, MAX(latest_event_id) AS latest_event_id
                FROM attribution_review_decisions
                WHERE message_observation_id IN ({placeholders})
                GROUP BY message_observation_id""",
            message_ids,
        )
    }
    messages = []
    for candidate in page:
        candidate_id = int(candidate["attribution_confidence_candidate_id"])
        message_id = int(candidate["message_observation_id"])
        message = message_by_id[message_id]
        message["confidence_candidate_id"] = candidate_id
        message["candidate_kind"] = candidate["candidate_kind"]
        message["character_id"] = candidate["character_id"]
        message["provisional_identity_candidate_id"] = candidate[
            "provisional_identity_candidate_id"
        ]
        message["classification"] = candidate["classification"]
        message["score"] = candidate["score"]
        message["latest_review_event_id"] = review_events_by_message.get(message_id)
        message["evidence"] = evidence_by_candidate[candidate_id]
        messages.append(message)
    return {"offset": offset, "limit": limit, "total": total, "messages": messages}


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
    include_group_history: bool = True,
) -> dict[str, Any]:
    """Return a candidate-centered read model for a future review client."""
    confidence_candidate_id = _positive_id(confidence_candidate_id, "confidence_candidate_id")
    if (
        isinstance(representative_message_limit, bool)
        or not isinstance(representative_message_limit, int)
        or representative_message_limit < 1
    ):
        raise ValueError("representative_message_limit must be a positive integer")
    if not isinstance(include_group_history, bool):
        raise ValueError("include_group_history must be a boolean")
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
        signatures = _candidate_evidence_signatures(
            connection,
            tuple(int(row["attribution_confidence_candidate_id"]) for row in candidate_rows),
        )
        selected_signature = signatures[confidence_candidate_id]
        candidate_rows = [
            row for row in candidate_rows
            if signatures[int(row["attribution_confidence_candidate_id"])]
            == selected_signature
        ]
    else:
        candidate_rows = [selected]
    message_ids = tuple(sorted({int(row["message_observation_id"]) for row in candidate_rows}))
    message_id = int(selected["message_observation_id"])
    representative_ids = list(message_ids[: max(0, representative_message_limit - 1)])
    if message_id not in representative_ids:
        representative_ids.append(message_id)
    representative_placeholders = ",".join("?" for _ in representative_ids)
    messages = connection.execute(
        f"""SELECT message.message_observation_id, message.discord_message_id,
                   message.timestamp_utc, message.content_original,
                   message.author_discord_user_id_original,
                   message.author_discord_name_original,
                   message.author_display_name_original,
                   message.author_nickname_original,
                   channel.name_current AS channel_name,
                   server.name_current AS server_name,
                   message.source_id, source.source_path
            FROM messages message
            LEFT JOIN channels channel ON channel.channel_id = message.channel_id
            LEFT JOIN servers server ON server.server_id = channel.server_id
            LEFT JOIN sources source ON source.source_id = message.source_id
            WHERE message.message_observation_id IN ({representative_placeholders})
            ORDER BY message.timestamp_utc, message.message_observation_id""",
        representative_ids,
    ).fetchall()
    message_records = [dict(row) for row in messages]
    current = dict(selected)
    current["evidence"] = _evidence_by_candidate(
        connection, (confidence_candidate_id,)
    )[confidence_candidate_id]
    history_message_ids = message_ids if include_group_history else (message_id,)
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
    decisions: list[dict[str, Any]] = []
    for start in range(0, len(history_message_ids), 800):
        history_batch = history_message_ids[start : start + 800]
        history_batch_placeholders = ",".join("?" for _ in history_batch)
        decisions.extend(
            dict(row)
            for row in connection.execute(
                f"""SELECT decision.*, event.action, event.actor, event.happened_at,
                           event.note, event.previous_state_json
                    FROM attribution_review_decisions decision
                    JOIN attribution_review_events event
                      ON event.attribution_review_event_id = decision.latest_event_id
                    WHERE decision.message_observation_id IN ({history_batch_placeholders})
                    ORDER BY event.attribution_review_event_id""",
                history_batch,
            )
        )
    decisions.sort(key=lambda row: row["latest_event_id"])
    selected_message_decisions = [
        row
        for row in decisions
        if int(row["message_observation_id"]) == message_id
    ]
    alternative_evidence = _evidence_by_candidate(
        connection,
        tuple(int(row["attribution_confidence_candidate_id"]) for row in alternatives),
    )
    for alternative in alternatives:
        alternative_id = int(alternative["attribution_confidence_candidate_id"])
        alternative["evidence"] = alternative_evidence[alternative_id]
        matching_decision = next(
            (
                row for row in selected_message_decisions
                if row["candidate_kind"] == alternative["candidate_kind"]
                and row["character_id"] == alternative["character_id"]
                and row["provisional_candidate_id"]
                == alternative["provisional_identity_candidate_id"]
            ),
            None,
        )
        alternative["review_status"] = (
            matching_decision["status"] if matching_decision else "never_reviewed"
        )
    history: list[dict[str, Any]] = []
    for start in range(0, len(history_message_ids), 800):
        history_batch = history_message_ids[start : start + 800]
        history_batch_placeholders = ",".join("?" for _ in history_batch)
        history.extend(
            dict(row)
            for row in connection.execute(
                f"""SELECT event.*, character.character_name,
                           provisional.display_label AS provisional_label,
                           target.display_label AS target_label
                    FROM attribution_review_events event
                    LEFT JOIN characters character
                      ON character.character_id = event.character_id
                    LEFT JOIN provisional_identity_candidates provisional
                      ON provisional.candidate_id = event.provisional_candidate_id
                    LEFT JOIN provisional_identity_candidates target
                      ON target.candidate_id = event.target_candidate_id
                    WHERE event.message_observation_id IN ({history_batch_placeholders})
                    ORDER BY event.attribution_review_event_id""",
                history_batch,
            )
        )
    authoritative = []
    for start in range(0, len(history_message_ids), 800):
        history_batch = history_message_ids[start : start + 800]
        history_batch_placeholders = ",".join("?" for _ in history_batch)
        authoritative.extend(
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
                WHERE attribution.message_observation_id IN ({history_batch_placeholders})
                  AND attribution.status IN ('accepted', 'rejected', 'pending')
                ORDER BY attribution.message_observation_id, attribution.attribution_id""",
                history_batch,
            )
        )
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
        if (
            isinstance(expected_latest_event_id, bool)
            or not isinstance(expected_latest_event_id, int)
            or expected_latest_event_id < 0
        ):
            raise ValueError("expected_latest_event_id must be a non-negative integer")
        for message_id in message_ids:
            current = connection.execute(
                """SELECT MAX(latest_event_id) FROM attribution_review_decisions
                   WHERE message_observation_id = ?""",
                (message_id,),
            ).fetchone()[0]
            current = int(current) if current is not None else 0
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
            if expected_latest_event_id is not None:
                for start in range(0, len(affected_ids), 800):
                    batch = affected_ids[start : start + 800]
                    placeholders = ",".join("?" for _ in batch)
                    latest_events = {
                        int(row["message_observation_id"]): int(row["latest_event_id"])
                        for row in connection.execute(
                            f"""SELECT message_observation_id,
                                       MAX(latest_event_id) AS latest_event_id
                                FROM attribution_review_decisions
                                WHERE message_observation_id IN ({placeholders})
                                GROUP BY message_observation_id""",
                            batch,
                        )
                    }
                    if any(
                        latest_events.get(message_id, 0) != expected_latest_event_id
                        for message_id in batch
                    ):
                        raise ValueError(
                            "At least one merge input changed after it was displayed; refresh the candidate"
                        )
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
