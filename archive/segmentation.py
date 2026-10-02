from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from archive.boundaries import BoundaryCandidate, canonical_json
from archive.signals import MessageRecord


SpanSplitReason = Literal["scope_start", "hard_boundary", "inactivity_gap"]
ReplyResolution = Literal["resolved", "unresolved", "ambiguous", "cross_scope"]


@dataclass(frozen=True)
class SegmentationConfig:
    algorithm_version: str = "candidate_spans_v1"
    inactivity_gap_seconds: float = 4443.177

    def __post_init__(self) -> None:
        if not self.algorithm_version.strip():
            raise ValueError("algorithm_version must not be empty")
        if (
            not math.isfinite(self.inactivity_gap_seconds)
            or self.inactivity_gap_seconds <= 0
        ):
            raise ValueError("inactivity_gap_seconds must be finite and positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm_version": self.algorithm_version,
            "inactivity_gap_seconds": self.inactivity_gap_seconds,
            "scope_fields": ["server_id", "channel_id", "source_id"],
            "soft_boundary_score_splits_span": False,
            "reply_to_prior_in_scope_message_bridges_inactivity_gap": True,
        }


DEFAULT_SEGMENTATION_CONFIG = SegmentationConfig()


@dataclass(frozen=True)
class CandidateSpan:
    span_key: str
    algorithm_version: str
    server_id: int | None
    channel_id: int | None
    source_id: int | None
    member_observation_ids: tuple[int, ...]
    start_timestamp_unix: float | None
    end_timestamp_unix: float | None
    start_reason: SpanSplitReason
    start_boundary_evidence: Mapping[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_key": self.span_key,
            "algorithm_version": self.algorithm_version,
            "server_id": self.server_id,
            "channel_id": self.channel_id,
            "source_id": self.source_id,
            "member_observation_ids": list(self.member_observation_ids),
            "start_timestamp_unix": self.start_timestamp_unix,
            "end_timestamp_unix": self.end_timestamp_unix,
            "start_reason": self.start_reason,
            "start_boundary_evidence": self.start_boundary_evidence,
        }


@dataclass(frozen=True)
class CandidateReplyLink:
    source_observation_id: int
    target_observation_ids: tuple[int, ...]
    target_discord_message_id: str
    resolution: ReplyResolution
    same_scope: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_observation_id": self.source_observation_id,
            "target_observation_ids": list(self.target_observation_ids),
            "target_discord_message_id": self.target_discord_message_id,
            "resolution": self.resolution,
            "same_scope": self.same_scope,
        }


@dataclass(frozen=True)
class SegmentationResult:
    spans: tuple[CandidateSpan, ...]
    reply_links: tuple[CandidateReplyLink, ...]
    boundary_decisions: tuple[Mapping[str, Any], ...]


def _value(record: MessageRecord, key: str) -> Any:
    try:
        return record[key]
    except (KeyError, IndexError):
        return None


def _scope(record: MessageRecord) -> tuple[int | None, int | None, int | None]:
    values = tuple(_value(record, key) for key in ("server_id", "channel_id", "source_id"))
    for key, value in zip(("server_id", "channel_id", "source_id"), values):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"{key} must be an integer or None")
    return values[0], values[1], values[2]


def _timestamp(record: MessageRecord) -> float | None:
    value = _value(record, "timestamp_unix")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return float(value)


def _stable_span_key(
    config: SegmentationConfig,
    scope: tuple[int | None, int | None, int | None],
    member_ids: Sequence[int],
) -> str:
    payload = {
        "algorithm_version": config.algorithm_version,
        "scope": scope,
        "members": list(member_ids),
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"span_{digest[:24]}"


def _boundary_for(
    boundaries: Mapping[tuple[int, int], BoundaryCandidate],
    left_id: int | None,
    right_id: int,
) -> BoundaryCandidate | None:
    if left_id is None:
        return None
    return boundaries.get((left_id, right_id))


def _reply_links(
    records: Sequence[MessageRecord],
) -> tuple[CandidateReplyLink, ...]:
    by_discord_id: dict[str, list[MessageRecord]] = defaultdict(list)
    for record in records:
        discord_id = _value(record, "discord_message_id")
        if discord_id is not None:
            by_discord_id[str(discord_id)].append(record)

    links: list[CandidateReplyLink] = []
    for record in records:
        reply_id = _value(record, "reply_to_discord_message_id")
        if reply_id is None:
            continue
        matches = by_discord_id.get(str(reply_id), [])
        source_id = _value(record, "message_observation_id")
        source_scope = _scope(record)
        target_ids = tuple(
            int(_value(target, "message_observation_id"))
            for target in matches
        )
        same_scope = bool(matches) and all(_scope(target) == source_scope for target in matches)
        if not matches:
            resolution: ReplyResolution = "unresolved"
        elif len(matches) > 1:
            resolution = "ambiguous"
        elif not same_scope:
            resolution = "cross_scope"
        else:
            resolution = "resolved"
        links.append(CandidateReplyLink(
            source_observation_id=int(source_id),
            target_observation_ids=target_ids,
            target_discord_message_id=str(reply_id),
            resolution=resolution,
            same_scope=same_scope,
        ))
    return tuple(links)


def segment_message_observations(
    messages: Sequence[MessageRecord],
    boundary_candidates: Sequence[BoundaryCandidate] = (),
    *,
    config: SegmentationConfig = DEFAULT_SEGMENTATION_CONFIG,
) -> SegmentationResult:
    """Create draft spans without treating soft boundary scores as split decisions.

    Each span is restricted to one `(server_id, channel_id, source_id)` scope.
    Rows missing any scope field are kept in singleton spans rather than being
    combined under an unknown scope. Results are deterministic for identical
    observations, boundary evidence, configuration, and input ordering.
    """
    observation_ids: set[int] = set()
    scope_messages: dict[tuple[int | None, int | None, int | None], list[MessageRecord]] = defaultdict(list)
    missing_scope_records: list[MessageRecord] = []
    for record in messages:
        observation_id = _value(record, "message_observation_id")
        if isinstance(observation_id, bool) or not isinstance(observation_id, int):
            raise ValueError("Each message requires an integer message_observation_id")
        if observation_id in observation_ids:
            raise ValueError(f"Duplicate message_observation_id: {observation_id}")
        observation_ids.add(observation_id)
        scope = _scope(record)
        if any(value is None for value in scope):
            missing_scope_records.append(record)
        else:
            scope_messages[scope].append(record)
    boundary_map = {
        (candidate.left_message_observation_id, candidate.right_message_observation_id): candidate
        for candidate in boundary_candidates
    }
    all_links = _reply_links(messages)
    reply_targets: dict[int, set[int]] = defaultdict(set)
    for link in all_links:
        if link.resolution == "resolved" and link.same_scope:
            reply_targets[link.source_observation_id].update(link.target_observation_ids)

    spans: list[CandidateSpan] = []
    boundary_decisions: list[Mapping[str, Any]] = []
    scopes = sorted(scope_messages)
    for scope in scopes:
        ordered = sorted(
            scope_messages[scope],
            key=lambda item: (
                _timestamp(item) is None,
                _timestamp(item) if _timestamp(item) is not None else 0.0,
                int(_value(item, "message_observation_id")),
            ),
        )
        current: list[MessageRecord] = []
        current_ids: set[int] = set()
        current_start_reason: SpanSplitReason = "scope_start"
        current_start_boundary: Mapping[str, Any] | None = None
        previous: MessageRecord | None = None

        def finish_current() -> None:
            if not current:
                return
            member_ids = tuple(int(_value(item, "message_observation_id")) for item in current)
            spans.append(CandidateSpan(
                span_key=_stable_span_key(config, scope, member_ids),
                algorithm_version=config.algorithm_version,
                server_id=scope[0],
                channel_id=scope[1],
                source_id=scope[2],
                member_observation_ids=member_ids,
                start_timestamp_unix=_timestamp(current[0]),
                end_timestamp_unix=_timestamp(current[-1]),
                start_reason=current_start_reason,
                start_boundary_evidence=current_start_boundary,
            ))

        for record in ordered:
            observation_id = int(_value(record, "message_observation_id"))
            split_reason: SpanSplitReason | None = None
            boundary_json: Mapping[str, Any] | None = None
            previous_id = (
                int(_value(previous, "message_observation_id"))
                if previous is not None
                else None
            )
            candidate = _boundary_for(boundary_map, previous_id, observation_id)
            hard_boundary = candidate is not None and candidate.boundary_type == "hard"
            if previous is not None and hard_boundary:
                split_reason = "hard_boundary"
            elif previous is not None:
                previous_time = _timestamp(previous)
                current_time = _timestamp(record)
                if previous_time is not None and current_time is not None:
                    gap = current_time - previous_time
                    if gap < 0:
                        raise ValueError("Scoped observations are not chronologically ordered")
                    if gap >= config.inactivity_gap_seconds:
                        has_in_scope_reply = any(
                            target_id in current_ids
                            for target_id in reply_targets.get(observation_id, set())
                        )
                        if not has_in_scope_reply:
                            split_reason = "inactivity_gap"
            if split_reason is not None:
                if candidate is not None:
                    boundary_json = candidate.to_dict()
                    boundary_decisions.append({
                        "left_message_observation_id": previous_id,
                        "right_message_observation_id": observation_id,
                        "split": True,
                        "split_reason": split_reason,
                        "boundary_candidate": boundary_json,
                    })
                else:
                    boundary_decisions.append({
                        "left_message_observation_id": previous_id,
                        "right_message_observation_id": observation_id,
                        "split": True,
                        "split_reason": split_reason,
                        "boundary_candidate": None,
                    })
                finish_current()
                current = []
                current_ids = set()
                current_start_reason = split_reason
                current_start_boundary = boundary_json
            elif previous is not None:
                boundary_decisions.append({
                    "left_message_observation_id": previous_id,
                    "right_message_observation_id": observation_id,
                    "split": False,
                    "split_reason": None,
                    "boundary_candidate": candidate.to_dict() if candidate is not None else None,
                })
            current.append(record)
            current_ids.add(observation_id)
            previous = record
        finish_current()

    for record in sorted(
        missing_scope_records,
        key=lambda item: int(_value(item, "message_observation_id")),
    ):
        observation_id = int(_value(record, "message_observation_id"))
        scope = _scope(record)
        member_ids = (observation_id,)
        spans.append(CandidateSpan(
            span_key=_stable_span_key(config, scope, member_ids),
            algorithm_version=config.algorithm_version,
            server_id=scope[0],
            channel_id=scope[1],
            source_id=scope[2],
            member_observation_ids=member_ids,
            start_timestamp_unix=_timestamp(record),
            end_timestamp_unix=_timestamp(record),
            start_reason="scope_start",
            start_boundary_evidence=None,
        ))

    spans.sort(key=lambda span: (
        span.server_id if span.server_id is not None else -1,
        span.channel_id if span.channel_id is not None else -1,
        span.source_id if span.source_id is not None else -1,
        span.start_timestamp_unix is None,
        span.start_timestamp_unix if span.start_timestamp_unix is not None else 0.0,
        span.member_observation_ids[0],
    ))

    boundary_decisions.sort(key=lambda item: (
        int(item["left_message_observation_id"]),
        int(item["right_message_observation_id"]),
    ))
    return SegmentationResult(
        spans=tuple(spans),
        reply_links=all_links,
        boundary_decisions=tuple(boundary_decisions),
    )
