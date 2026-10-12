from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from archive.signals import MessageRecord, Signal, extract_signals


BoundaryClassification = Literal["none", "soft_candidate", "strong_candidate", "hard_candidate"]
BoundaryType = Literal["hard", "soft"]
EvidenceRole = Literal["supporting", "contradictory", "neutral"]

ALGORITHM_VERSION = "event_boundaries_v1"


@dataclass(frozen=True)
class BoundaryScoringConfig:
    """Versioned ruleset; values are operational starting points, not calibration."""

    algorithm_version: str = ALGORITHM_VERSION
    timestamp_gap_reference_seconds: float = 4443.177
    soft_candidate_threshold: float = 0.35
    strong_candidate_threshold: float = 0.65
    weights: tuple[tuple[str, float], ...] | Mapping[str, float] = (
        ("explicit_scene_marker", 1.0),
        ("collection_boundary", 1.0),
        ("timestamp_gap", 0.25),
        ("channel_change", 0.20),
        ("narrative_context_transition", 0.25),
        ("author_change", 0.10),
        ("speaker_marker_change", 0.10),
        ("thread_change", 0.15),
        ("reply_relationship", -0.30),
    )
    hard_scene_markers: tuple[str, ...] = (
        "scene_start",
        "scene_end",
        "scene_break",
        "time_skip",
        "transition",
    )

    def __post_init__(self) -> None:
        if isinstance(self.weights, Mapping):
            normalized_weights = tuple(sorted(self.weights.items()))
        else:
            normalized_weights = tuple(self.weights)
        object.__setattr__(self, "weights", normalized_weights)
        if not self.algorithm_version.strip():
            raise ValueError("algorithm_version must not be empty")
        if not math.isfinite(self.timestamp_gap_reference_seconds) or self.timestamp_gap_reference_seconds <= 0:
            raise ValueError("timestamp_gap_reference_seconds must be finite and positive")
        if not math.isfinite(self.soft_candidate_threshold) or not math.isfinite(self.strong_candidate_threshold):
            raise ValueError("classification thresholds must be finite")
        if self.soft_candidate_threshold < 0 or self.strong_candidate_threshold < self.soft_candidate_threshold:
            raise ValueError("classification thresholds must be non-negative and ordered")
        names = [name for name, _ in normalized_weights]
        if len(names) != len(set(names)):
            raise ValueError("signal weights must not contain duplicate signal types")
        if any(not math.isfinite(weight) for _, weight in normalized_weights):
            raise ValueError("signal weights must be finite")

    def weight_for(self, signal_type: str) -> float:
        return dict(self.weights).get(signal_type, 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm_version": self.algorithm_version,
            "timestamp_gap_reference_seconds": self.timestamp_gap_reference_seconds,
            "soft_candidate_threshold": self.soft_candidate_threshold,
            "strong_candidate_threshold": self.strong_candidate_threshold,
            "weights": dict(self.weights),
            "hard_scene_markers": list(self.hard_scene_markers),
            "score_semantics": "uncalibrated operational boundary detection score",
            "timestamp_gap_normalization": "min(max(gap_seconds, 0) / reference_seconds, 1)",
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BoundaryScoringConfig:
        return cls(
            algorithm_version=value.get("algorithm_version", ALGORITHM_VERSION),
            timestamp_gap_reference_seconds=value.get(
                "timestamp_gap_reference_seconds", cls.timestamp_gap_reference_seconds
            ),
            soft_candidate_threshold=value.get(
                "soft_candidate_threshold", cls.soft_candidate_threshold
            ),
            strong_candidate_threshold=value.get(
                "strong_candidate_threshold", cls.strong_candidate_threshold
            ),
            weights=value.get("weights", cls.weights),
            hard_scene_markers=tuple(value.get("hard_scene_markers", cls.hard_scene_markers)),
        )

    @classmethod
    def from_json(cls, value: str) -> BoundaryScoringConfig:
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("scoring configuration JSON must contain an object")
        return cls.from_dict(parsed)


DEFAULT_CONFIG = BoundaryScoringConfig()


@dataclass(frozen=True)
class SignalContribution:
    signal_type: str
    strength: float
    weight: float
    contribution: float
    source_observation_ids: tuple[int, ...]
    evidence_kind: str
    role: EvidenceRole
    hard_boundary: bool
    observed_value: Mapping[str, Any]
    evidence: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_type": self.signal_type,
            "strength": self.strength,
            "weight": self.weight,
            "contribution": self.contribution,
            "source_observation_ids": list(self.source_observation_ids),
            "evidence_kind": self.evidence_kind,
            "role": self.role,
            "hard_boundary": self.hard_boundary,
            "observed_value": dict(self.observed_value),
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class BoundaryCandidate:
    left_message_observation_id: int
    right_message_observation_id: int
    boundary_type: BoundaryType | None
    raw_score: float
    classification: BoundaryClassification
    supporting_evidence: tuple[SignalContribution, ...]
    contradictory_evidence: tuple[SignalContribution, ...]
    contributions: tuple[SignalContribution, ...]
    algorithm_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "left_message_observation_id": self.left_message_observation_id,
            "right_message_observation_id": self.right_message_observation_id,
            "boundary_type": self.boundary_type,
            "raw_score": self.raw_score,
            "classification": self.classification,
            "supporting_evidence": [item.to_dict() for item in self.supporting_evidence],
            "contradictory_evidence": [item.to_dict() for item in self.contradictory_evidence],
            "contributions": [item.to_dict() for item in self.contributions],
            "algorithm_version": self.algorithm_version,
        }


def _signal_strength(signal: Signal, config: BoundaryScoringConfig) -> float | None:
    if signal.signal_type == "timestamp_gap":
        gap = signal.observed_value.get("gap_seconds")
        if isinstance(gap, bool) or not isinstance(gap, (int, float)) or not math.isfinite(float(gap)):
            return None
        if gap < 0:
            raise ValueError("Message observations are not chronologically ordered")
        return min(float(gap) / config.timestamp_gap_reference_seconds, 1.0)
    if signal.signal_type == "reply_relationship":
        return 1.0
    return 1.0


def _signal_applies(
    signal: Signal,
    left_id: int,
    right_id: int,
    config: BoundaryScoringConfig,
) -> tuple[bool, bool]:
    """Return (applies, hard) for one transition."""
    if signal.signal_type == "explicit_scene_marker":
        marker = signal.observed_value.get("marker")
        if marker not in config.hard_scene_markers:
            return False, False
        anchor_id = signal.source_message_ids[0] if signal.source_message_ids else None
        if marker == "scene_end":
            return anchor_id == left_id, anchor_id == left_id
        return anchor_id == right_id, anchor_id == right_id

    if signal.signal_type == "collection_boundary":
        if signal.observed_value.get("field") != "source_id":
            return False, False
        applies = signal.source_message_ids == (left_id, right_id)
        return applies, applies

    pair_signals = {
        "timestamp_gap",
        "channel_change",
        "author_change",
        "speaker_marker_change",
        "thread_change",
        "narrative_context_transition",
    }
    if signal.signal_type in pair_signals:
        return signal.source_message_ids == (left_id, right_id), False

    if signal.signal_type == "reply_relationship":
        if signal.evidence.get("target_resolution") != "resolved":
            return False, False
        return (
            signal.source_message_ids[0] == right_id
            and left_id in signal.source_message_ids[1:],
            False,
        )
    return False, False


def score_boundary_candidates(
    ordered_message_observation_ids: Sequence[int],
    signals: Sequence[Signal],
    *,
    config: BoundaryScoringConfig = DEFAULT_CONFIG,
) -> list[BoundaryCandidate]:
    """Score each adjacent transition from Phase B signals without persisting it."""
    ids = tuple(ordered_message_observation_ids)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in ids):
        raise ValueError("message observation IDs must be integers")
    if len(ids) != len(set(ids)):
        raise ValueError("message observation IDs must be unique in the ordered input")

    invalid_timestamp_ids = {
        signal.source_message_ids[0]
        for signal in signals
        if signal.signal_type in {"timestamp_invalid", "timestamp_missing"}
        and signal.source_message_ids
    }
    pair_signals: dict[tuple[int, int], list[Signal]] = defaultdict(list)
    marker_signals: dict[int, list[Signal]] = defaultdict(list)
    reply_signals: dict[tuple[int, int], list[Signal]] = defaultdict(list)
    direct_pairs = set(zip(ids, ids[1:]))
    pair_signal_types = {
        "timestamp_gap",
        "channel_change",
        "author_change",
        "speaker_marker_change",
        "thread_change",
        "narrative_context_transition",
        "collection_boundary",
    }
    for signal in signals:
        if signal.signal_type in pair_signal_types and len(signal.source_message_ids) == 2:
            pair_signals[(signal.source_message_ids[0], signal.source_message_ids[1])].append(signal)
        elif signal.signal_type == "explicit_scene_marker" and signal.source_message_ids:
            marker_signals[signal.source_message_ids[0]].append(signal)
        elif (
            signal.signal_type == "reply_relationship"
            and signal.evidence.get("target_resolution") == "resolved"
            and signal.source_message_ids
        ):
            reply_message_id = signal.source_message_ids[0]
            for target_id in signal.source_message_ids[1:]:
                edge = (target_id, reply_message_id)
                if edge in direct_pairs:
                    reply_signals[edge].append(signal)

    candidates: list[BoundaryCandidate] = []
    for left_id, right_id in zip(ids, ids[1:]):
        contributions: list[SignalContribution] = []
        raw_score = 0.0
        has_hard_evidence = False
        edge = (left_id, right_id)
        applicable_signals = (
            pair_signals.get(edge, [])
            + marker_signals.get(left_id, [])
            + marker_signals.get(right_id, [])
            + reply_signals.get(edge, [])
        )
        for signal in applicable_signals:
            applies, hard = _signal_applies(signal, left_id, right_id, config)
            if not applies:
                continue
            if (
                signal.signal_type == "timestamp_gap"
                and (left_id in invalid_timestamp_ids or right_id in invalid_timestamp_ids)
            ):
                continue
            strength = _signal_strength(signal, config)
            if strength is None:
                continue
            weight = config.weight_for(signal.signal_type)
            contribution = weight * strength
            raw_score += contribution
            has_hard_evidence = has_hard_evidence or hard
            if contribution < 0:
                role: EvidenceRole = "contradictory"
            elif contribution > 0:
                role = "supporting"
            else:
                role = "neutral"
            contributions.append(SignalContribution(
                signal_type=signal.signal_type,
                strength=strength,
                weight=weight,
                contribution=contribution,
                source_observation_ids=signal.source_message_ids,
                evidence_kind=signal.evidence_kind,
                role=role,
                hard_boundary=hard,
                observed_value=signal.observed_value,
                evidence=signal.evidence,
            ))

        if has_hard_evidence:
            classification: BoundaryClassification = "hard_candidate"
            boundary_type: BoundaryType | None = "hard"
        elif raw_score >= config.strong_candidate_threshold:
            classification = "strong_candidate"
            boundary_type = "soft"
        elif raw_score >= config.soft_candidate_threshold:
            classification = "soft_candidate"
            boundary_type = "soft"
        else:
            classification = "none"
            boundary_type = None

        candidates.append(BoundaryCandidate(
            left_message_observation_id=left_id,
            right_message_observation_id=right_id,
            boundary_type=boundary_type,
            raw_score=raw_score,
            classification=classification,
            supporting_evidence=tuple(item for item in contributions if item.role == "supporting"),
            contradictory_evidence=tuple(item for item in contributions if item.role == "contradictory"),
            contributions=tuple(contributions),
            algorithm_version=config.algorithm_version,
        ))
    return candidates


def analyze_message_boundaries(
    messages: Sequence[MessageRecord],
    *,
    config: BoundaryScoringConfig = DEFAULT_CONFIG,
) -> tuple[list[Signal], list[BoundaryCandidate]]:
    """Extract Phase B evidence then score the supplied, ordered observations."""
    ids: list[int] = []
    for record in messages:
        observation_id = record["message_observation_id"]
        if isinstance(observation_id, bool) or not isinstance(observation_id, int):
            raise ValueError("Each message requires an integer message_observation_id")
        ids.append(observation_id)
    signals = extract_signals(messages)
    for signal in signals:
        if signal.signal_type != "timestamp_gap":
            continue
        gap = signal.observed_value.get("gap_seconds")
        if isinstance(gap, (int, float)) and gap < 0:
            raise ValueError("Message observations must be ordered by timestamp then observation ID")
        if (
            gap == 0
            and len(signal.source_message_ids) == 2
            and signal.source_message_ids[1] < signal.source_message_ids[0]
        ):
            raise ValueError("Message observations must be ordered by timestamp then observation ID")
    candidates = score_boundary_candidates(ids, signals, config=config)
    return signals, candidates


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint_analysis_inputs(messages: Sequence[MessageRecord], signals: Sequence[Signal]) -> str:
    fields = (
        "message_observation_id",
        "discord_message_id",
        "timestamp_unix",
        "timestamp_utc",
        "channel_id",
        "author_identity_id",
        "author_discord_user_id_original",
        "webhook_id_original",
        "reply_to_discord_message_id",
        "content_original",
        "source_id",
        "import_id",
        "source_json",
    )
    digest = hashlib.sha256()
    for record in messages:
        digest.update(b"message\0")
        digest.update(canonical_json({
            field: _record_value(record, field) for field in fields
        }).encode("utf-8"))
        digest.update(b"\n")
    for signal in signals:
        digest.update(b"signal\0")
        digest.update(canonical_json({
            "signal_type": signal.signal_type,
            "source_message_ids": signal.source_message_ids,
            "evidence_kind": signal.evidence_kind,
            "observed_value": signal.observed_value,
            "evidence": signal.evidence,
        }).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _record_value(record: MessageRecord, field: str) -> Any:
    try:
        return record[field]
    except (KeyError, IndexError):
        return None


def summarize_candidates(candidates: Sequence[BoundaryCandidate]) -> dict[str, Any]:
    counts = Counter(candidate.classification for candidate in candidates)
    return {
        "transition_count": len(candidates),
        "classification_counts": dict(sorted(counts.items())),
        "persisted_candidate_count": sum(
            candidate.classification != "none" for candidate in candidates
        ),
        "signal_count": sum(len(candidate.contributions) for candidate in candidates),
    }
