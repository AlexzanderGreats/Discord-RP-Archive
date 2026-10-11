from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from archive.scenes import add_message_to_scene, create_scene


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class EventCandidateConfig:
    algorithm_version: str = "event_candidates_v1"
    max_gap_seconds: float = 4443.177 * 2.0
    require_same_server: bool = True
    require_same_source: bool = True
    allow_cross_channel: bool = True

    def __post_init__(self) -> None:
        if not self.algorithm_version.strip():
            raise ValueError("algorithm_version must not be empty")
        if self.max_gap_seconds <= 0:
            raise ValueError("max_gap_seconds must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm_version": self.algorithm_version,
            "max_gap_seconds": self.max_gap_seconds,
            "require_same_server": self.require_same_server,
            "require_same_source": self.require_same_source,
            "allow_cross_channel": self.allow_cross_channel,
        }


DEFAULT_EVENT_CANDIDATE_CONFIG = EventCandidateConfig()


def _group_candidate_spans(
    rows: Sequence[sqlite3.Row],
    *,
    config: EventCandidateConfig,
) -> list[list[sqlite3.Row]]:
    if not rows:
        return []
    ordered = sorted(
        rows,
        key=lambda row: (
            row["server_id"] if row["server_id"] is not None else -1,
            row["channel_id"] if row["channel_id"] is not None else -1,
            row["source_id"] if row["source_id"] is not None else -1,
            row["start_timestamp_unix"] if row["start_timestamp_unix"] is not None else 0.0,
            row["candidate_span_id"],
        ),
    )
    groups: list[list[sqlite3.Row]] = [[ordered[0]]]
    for row in ordered[1:]:
        previous = groups[-1][-1]
        if _can_merge_spans(previous, row, config=config):
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def _can_merge_spans(
    left: sqlite3.Row,
    right: sqlite3.Row,
    *,
    config: EventCandidateConfig,
) -> bool:
    if config.require_same_server and left["server_id"] != right["server_id"]:
        return False
    if config.require_same_source and left["source_id"] != right["source_id"]:
        return False
    if left["server_id"] is None and right["server_id"] is not None:
        return False
    if left["source_id"] is None and right["source_id"] is not None:
        return False
    if not config.allow_cross_channel and left["channel_id"] != right["channel_id"]:
        return False
    left_end = left["end_timestamp_unix"]
    right_start = right["start_timestamp_unix"]
    if left_end is None or right_start is None:
        return False
    if right_start < left_end:
        return False
    return (right_start - left_end) <= config.max_gap_seconds


def _candidate_key_for_span_ids(config: EventCandidateConfig, span_ids: Sequence[int]) -> str:
    payload = {
        "algorithm_version": config.algorithm_version,
        "span_ids": list(span_ids),
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"event_{digest[:24]}"


def _list_candidate_span_rows(connection: sqlite3.Connection, *, span_ids: Sequence[int] | None = None) -> list[sqlite3.Row]:
    if span_ids is not None:
        if not span_ids:
            return []
        placeholders = ",".join("?" for _ in span_ids)
        query = f"""
            SELECT cs.candidate_span_id, cs.server_id, cs.channel_id, cs.source_id,
                   cs.first_message_observation_id, cs.last_message_observation_id,
                   cs.start_timestamp_unix, cs.end_timestamp_unix,
                   cs.start_reason, cs.start_boundary_evidence_json
            FROM candidate_spans cs
            WHERE cs.candidate_span_id IN ({placeholders})
            ORDER BY cs.start_timestamp_unix IS NULL, cs.start_timestamp_unix, cs.candidate_span_id
        """
        return connection.execute(query, tuple(span_ids)).fetchall()
    query = """
        SELECT cs.candidate_span_id, cs.server_id, cs.channel_id, cs.source_id,
               cs.first_message_observation_id, cs.last_message_observation_id,
               cs.start_timestamp_unix, cs.end_timestamp_unix,
               cs.start_reason, cs.start_boundary_evidence_json
        FROM candidate_spans cs
        ORDER BY cs.start_timestamp_unix IS NULL, cs.start_timestamp_unix, cs.candidate_span_id
    """
    return connection.execute(query).fetchall()


def _message_ids_for_span_ids(connection: sqlite3.Connection, span_ids: Sequence[int]) -> list[int]:
    if not span_ids:
        return []
    placeholders = ",".join("?" for _ in span_ids)
    rows = connection.execute(
        f"""
            SELECT DISTINCT csm.message_observation_id
            FROM candidate_span_messages csm
            WHERE csm.candidate_span_id IN ({placeholders})
            ORDER BY csm.message_observation_id
        """,
        tuple(span_ids),
    ).fetchall()
    return [int(row["message_observation_id"]) for row in rows]


def _run_exists(connection: sqlite3.Connection, *, input_fingerprint: str, algorithm_version: str) -> int | None:
    row = connection.execute(
        """
            SELECT event_candidate_run_id
            FROM event_candidate_runs
            WHERE input_fingerprint = ? AND algorithm_version = ?
            ORDER BY event_candidate_run_id DESC
            LIMIT 1
        """,
        (input_fingerprint, algorithm_version),
    ).fetchone()
    return None if row is None else int(row["event_candidate_run_id"])


def generate_event_candidates(
    connection: sqlite3.Connection,
    *,
    span_ids: Sequence[int] | None = None,
    config: EventCandidateConfig = DEFAULT_EVENT_CANDIDATE_CONFIG,
    run_type: str = "full",
    input_scope: Mapping[str, Any] | None = None,
) -> tuple[int, list[sqlite3.Row]]:
    """Generate event candidates from existing candidate spans.

    The algorithm is intentionally deterministic and uses structural evidence only:
    adjacent candidate spans are merged when they remain in the relevant scope and
    their observed gap stays within the configured threshold.
    """
    if run_type not in {"full", "incremental"}:
        raise ValueError("run_type must be 'full' or 'incremental'")
    span_rows = _list_candidate_span_rows(connection, span_ids=span_ids)
    groups = _group_candidate_spans(span_rows, config=config)
    scope_json = _canonical_json(dict(input_scope or {}))
    config_json = _canonical_json(config.to_dict())
    input_fingerprint = hashlib.sha256(
        (config_json + "\n" + scope_json + "\n" + _canonical_json([
            {"candidate_span_id": row["candidate_span_id"], "server_id": row["server_id"], "channel_id": row["channel_id"], "source_id": row["source_id"], "start": row["start_timestamp_unix"], "end": row["end_timestamp_unix"]}
            for row in span_rows
        ])).encode("utf-8")
    ).hexdigest()
    run_id = _run_exists(connection, input_fingerprint=input_fingerprint, algorithm_version=config.algorithm_version)
    if run_id is None:
        cursor = connection.execute(
            """
                INSERT INTO event_candidate_runs(
                    algorithm_version, configuration_json, configuration_sha256,
                    input_scope_json, input_fingerprint, started_at, status,
                    run_type, statistics_json
                ) VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?)
            """,
            (
                config.algorithm_version,
                config_json,
                hashlib.sha256(config_json.encode("utf-8")).hexdigest(),
                scope_json,
                input_fingerprint,
                _now(),
                run_type,
                _canonical_json({"candidate_count": len(groups), "candidate_span_count": len(span_rows)}),
            ),
        )
        run_id = int(cursor.lastrowid)
    rows: list[sqlite3.Row] = []
    for group_index, group in enumerate(groups):
        span_ids_for_group = [int(row["candidate_span_id"]) for row in group]
        key = _candidate_key_for_span_ids(config, span_ids_for_group)
        existing = connection.execute(
            "SELECT event_candidate_id FROM event_candidates WHERE event_candidate_run_id = ? AND candidate_key = ?",
            (run_id, key),
        ).fetchone()
        if existing is not None:
            rows.append(connection.execute("SELECT * FROM event_candidates WHERE event_candidate_id = ?", (int(existing[0]),)).fetchone())
            continue
        message_ids = _message_ids_for_span_ids(connection, span_ids_for_group)
        title = f"Event {group_index + 1}"
        start = min(row["start_timestamp_unix"] for row in group if row["start_timestamp_unix"] is not None)
        end = max(row["end_timestamp_unix"] for row in group if row["end_timestamp_unix"] is not None)
        evidence = {
            "algorithm_version": config.algorithm_version,
            "candidate_span_ids": span_ids_for_group,
            "message_count": len(message_ids),
            "source_ids": sorted({row["source_id"] for row in group if row["source_id"] is not None}),
            "server_ids": sorted({row["server_id"] for row in group if row["server_id"] is not None}),
            "channel_ids": sorted({row["channel_id"] for row in group if row["channel_id"] is not None}),
            "time_gap_seconds": None if len(group) < 2 else max(0.0, group[1]["start_timestamp_unix"] - group[0]["end_timestamp_unix"]),
        }
        cursor = connection.execute(
            """
                INSERT INTO event_candidates(
                    event_candidate_run_id, candidate_key, title, description,
                    notes, status, review_state, server_id, channel_id,
                    source_id, start_timestamp_unix, end_timestamp_unix,
                    message_count, candidate_span_count, evidence_json,
                    stale_reason, created_at, updated_at, accepted_scene_id
                ) VALUES (?, ?, ?, ?, ?, 'proposed', 'unreviewed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                run_id,
                key,
                title,
                None,
                None,
                group[0]["server_id"],
                group[0]["channel_id"],
                group[0]["source_id"],
                start,
                end,
                len(message_ids),
                len(span_ids_for_group),
                _canonical_json(evidence),
                None,
                _now(),
                _now(),
            ),
        )
        event_candidate_id = int(cursor.lastrowid)
        for position, message_id in enumerate(message_ids):
            connection.execute(
                """
                    INSERT INTO event_candidate_members(
                        event_candidate_id, candidate_span_id, message_observation_id,
                        position, membership_kind, evidence_json
                    ) VALUES (?, NULL, ?, ?, 'message', ?)
                """,
                (
                    event_candidate_id,
                    message_id,
                    position,
                    _canonical_json({"rule": "message_in_grouped_candidate_span", "message_observation_id": message_id}),
                ),
            )
        for position, span_id in enumerate(span_ids_for_group):
            connection.execute(
                """
                    INSERT INTO event_candidate_members(
                        event_candidate_id, candidate_span_id, message_observation_id,
                        position, membership_kind, evidence_json
                    ) VALUES (?, ?, NULL, ?, 'span', ?)
                """,
                (
                    event_candidate_id,
                    span_id,
                    len(message_ids) + position,
                    _canonical_json({"rule": "candidate_span_reference", "candidate_span_id": span_id}),
                ),
            )
        rows.append(connection.execute("SELECT * FROM event_candidates WHERE event_candidate_id = ?", (event_candidate_id,)).fetchone())
    return run_id, rows


def list_event_candidates(
    connection: sqlite3.Connection,
    *,
    status: str | None = None,
    arc_id: int | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    query = """
        SELECT ec.*
        FROM event_candidates ec
        LEFT JOIN event_candidate_arc_memberships ecm
          ON ecm.event_candidate_id = ec.event_candidate_id
        WHERE (? IS NULL OR ec.status = ?)
          AND (? IS NULL OR ecm.arc_id = ?)
    """
    params: list[Any] = [status, status, arc_id, arc_id]
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    query += " ORDER BY ec.start_timestamp_unix IS NULL, ec.start_timestamp_unix, ec.event_candidate_id"
    return connection.execute(query, params).fetchall()


def get_event_candidate(connection: sqlite3.Connection, event_candidate_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM event_candidates WHERE event_candidate_id = ?", (event_candidate_id,)).fetchone()
    if row is None:
        raise ValueError(f"No event candidate found for id {event_candidate_id}")
    return row


def list_event_candidate_messages(connection: sqlite3.Connection, event_candidate_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        """
            SELECT m.message_observation_id, m.discord_message_id, m.timestamp_utc,
                   m.channel_id, c.name_current AS channel_name,
                   m.content_original, m.reply_to_discord_message_id,
                   ecm.position
            FROM event_candidate_members ecm
            JOIN messages m ON m.message_observation_id = ecm.message_observation_id
            LEFT JOIN channels c ON c.channel_id = m.channel_id
            WHERE ecm.event_candidate_id = ? AND ecm.message_observation_id IS NOT NULL
            ORDER BY ecm.position
        """,
        (event_candidate_id,),
    ).fetchall()


def _record_review_event(
    connection: sqlite3.Connection,
    *,
    event_candidate_id: int,
    actor: str,
    action: str,
    details: Mapping[str, Any],
    previous_status: str | None,
    new_status: str | None,
) -> None:
    connection.execute(
        """
            INSERT INTO event_candidate_review_events(
                event_candidate_id, reviewer, action, prior_status, new_status,
                details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_candidate_id,
            actor,
            action,
            previous_status,
            new_status,
            _canonical_json(dict(details)),
            _now(),
        ),
    )


def update_event_candidate(
    connection: sqlite3.Connection,
    event_candidate_id: int,
    *,
    title: str | None = None,
    description: str | None = None,
    notes: str | None = None,
    actor: str = "user",
    status: str | None = None,
) -> sqlite3.Row:
    row = get_event_candidate(connection, event_candidate_id)
    previous_status = row["status"]
    payload = {
        "title": row["title"],
        "description": row["description"],
        "notes": row["notes"],
        "status": row["status"],
    }
    if title is not None:
        cleaned = title.strip()
        if not cleaned:
            raise ValueError("Event title must not be empty")
        payload["title"] = cleaned
    if description is not None:
        payload["description"] = description.strip() or None
    if notes is not None:
        payload["notes"] = notes.strip() or None
    if status is not None:
        if status not in {"proposed", "accepted", "rejected", "deferred", "stale"}:
            raise ValueError("status must be one of proposed, accepted, rejected, deferred, stale")
        payload["status"] = status
    connection.execute(
        """
            UPDATE event_candidates
            SET title = ?, description = ?, notes = ?, status = ?, review_state = ?, updated_at = ?
            WHERE event_candidate_id = ?
        """,
        (
            payload["title"],
            payload["description"],
            payload["notes"],
            payload["status"],
            "accepted" if payload["status"] == "accepted" else "rejected" if payload["status"] == "rejected" else "deferred" if payload["status"] == "deferred" else "unreviewed",
            _now(),
            event_candidate_id,
        ),
    )
    new_status = payload["status"]
    _record_review_event(
        connection,
        event_candidate_id=event_candidate_id,
        actor=actor,
        action="updated",
        details={
            "title": title,
            "description": description,
            "notes": notes,
            "status": status,
        },
        previous_status=previous_status,
        new_status=new_status,
    )
    return get_event_candidate(connection, event_candidate_id)


def reject_event_candidate(connection: sqlite3.Connection, event_candidate_id: int, *, actor: str = "user", reason: str | None = None) -> sqlite3.Row:
    row = get_event_candidate(connection, event_candidate_id)
    connection.execute(
        "UPDATE event_candidates SET status = 'rejected', review_state = 'rejected', updated_at = ? WHERE event_candidate_id = ?",
        (_now(), event_candidate_id),
    )
    _record_review_event(
        connection,
        event_candidate_id=event_candidate_id,
        actor=actor,
        action="rejected",
        details={"reason": reason},
        previous_status=row["status"],
        new_status="rejected",
    )
    return get_event_candidate(connection, event_candidate_id)


def defer_event_candidate(connection: sqlite3.Connection, event_candidate_id: int, *, actor: str = "user", reason: str | None = None) -> sqlite3.Row:
    row = get_event_candidate(connection, event_candidate_id)
    connection.execute(
        "UPDATE event_candidates SET status = 'deferred', review_state = 'deferred', updated_at = ? WHERE event_candidate_id = ?",
        (_now(), event_candidate_id),
    )
    _record_review_event(
        connection,
        event_candidate_id=event_candidate_id,
        actor=actor,
        action="deferred",
        details={"reason": reason},
        previous_status=row["status"],
        new_status="deferred",
    )
    return get_event_candidate(connection, event_candidate_id)


def mark_event_candidate_stale(connection: sqlite3.Connection, event_candidate_id: int, *, reason: str, actor: str = "system") -> sqlite3.Row:
    row = get_event_candidate(connection, event_candidate_id)
    connection.execute(
        "UPDATE event_candidates SET status = 'stale', stale_reason = ?, updated_at = ? WHERE event_candidate_id = ?",
        (reason, _now(), event_candidate_id),
    )
    _record_review_event(
        connection,
        event_candidate_id=event_candidate_id,
        actor=actor,
        action="stale",
        details={"reason": reason},
        previous_status=row["status"],
        new_status="stale",
    )
    return get_event_candidate(connection, event_candidate_id)


def assign_event_candidate_to_arc(connection: sqlite3.Connection, event_candidate_id: int, arc_id: int, *, actor: str = "user", notes: str | None = None) -> None:
    connection.execute(
        """
            INSERT OR IGNORE INTO event_candidate_arc_memberships(event_candidate_id, arc_id, added_at, added_by, notes)
            VALUES (?, ?, ?, ?, ?)
        """,
        (event_candidate_id, arc_id, _now(), actor, notes),
    )
    _record_review_event(
        connection,
        event_candidate_id=event_candidate_id,
        actor=actor,
        action="arc_assigned",
        details={"arc_id": arc_id, "notes": notes},
        previous_status=get_event_candidate(connection, event_candidate_id)["status"],
        new_status=get_event_candidate(connection, event_candidate_id)["status"],
    )


def remove_event_candidate_from_arc(connection: sqlite3.Connection, event_candidate_id: int, arc_id: int, *, actor: str = "user") -> None:
    connection.execute(
        "DELETE FROM event_candidate_arc_memberships WHERE event_candidate_id = ? AND arc_id = ?",
        (event_candidate_id, arc_id),
    )
    _record_review_event(
        connection,
        event_candidate_id=event_candidate_id,
        actor=actor,
        action="arc_removed",
        details={"arc_id": arc_id},
        previous_status=get_event_candidate(connection, event_candidate_id)["status"],
        new_status=get_event_candidate(connection, event_candidate_id)["status"],
    )


def accept_event_candidate(
    connection: sqlite3.Connection,
    event_candidate_id: int,
    *,
    actor: str = "user",
    scene_name: str | None = None,
    arc_id: int | None = None,
    description: str | None = None,
) -> int:
    row = get_event_candidate(connection, event_candidate_id)
    if row["status"] == "stale":
        raise ValueError("This event candidate is stale and cannot be accepted as current")
    title = row["title"] or scene_name or f"Event {event_candidate_id}"
    scene_id = create_scene(
        connection,
        title,
        arc_id=arc_id,
        description=description or row["description"],
    )
    for position, message in enumerate(list_event_candidate_messages(connection, event_candidate_id)):
        add_message_to_scene(connection, scene_id, int(message["message_observation_id"]), position=position, added_by=actor)
    connection.execute(
        "UPDATE event_candidates SET status = 'accepted', review_state = 'accepted', accepted_scene_id = ?, updated_at = ? WHERE event_candidate_id = ?",
        (scene_id, _now(), event_candidate_id),
    )
    _record_review_event(
        connection,
        event_candidate_id=event_candidate_id,
        actor=actor,
        action="accepted",
        details={"scene_id": scene_id, "arc_id": arc_id},
        previous_status=row["status"],
        new_status="accepted",
    )
    return scene_id


def adjust_event_candidate_grouping(
    connection: sqlite3.Connection,
    event_candidate_id: int,
    *,
    candidate_span_ids: Sequence[int],
    actor: str = "user",
) -> sqlite3.Row:
    if not candidate_span_ids:
        raise ValueError("candidate_span_ids must not be empty")
    connection.execute("DELETE FROM event_candidate_members WHERE event_candidate_id = ?", (event_candidate_id,))
    message_ids = _message_ids_for_span_ids(connection, tuple(candidate_span_ids))
    for position, message_id in enumerate(message_ids):
        connection.execute(
            "INSERT INTO event_candidate_members(event_candidate_id, candidate_span_id, message_observation_id, position, membership_kind, evidence_json) VALUES (?, NULL, ?, ?, 'message', ?)",
            (event_candidate_id, message_id, position, _canonical_json({"rule": "adjusted_grouping", "message_observation_id": message_id})),
        )
    for position, span_id in enumerate(candidate_span_ids):
        connection.execute(
            "INSERT INTO event_candidate_members(event_candidate_id, candidate_span_id, message_observation_id, position, membership_kind, evidence_json) VALUES (?, ?, NULL, ?, 'span', ?)",
            (event_candidate_id, span_id, len(message_ids) + position, _canonical_json({"rule": "adjusted_grouping", "candidate_span_id": span_id})),
        )
    connection.execute(
        "UPDATE event_candidates SET updated_at = ? WHERE event_candidate_id = ?",
        (_now(), event_candidate_id),
    )
    _record_review_event(
        connection,
        event_candidate_id=event_candidate_id,
        actor=actor,
        action="group_adjusted",
        details={"candidate_span_ids": list(candidate_span_ids)},
        previous_status=get_event_candidate(connection, event_candidate_id)["status"],
        new_status=get_event_candidate(connection, event_candidate_id)["status"],
    )
    return get_event_candidate(connection, event_candidate_id)
