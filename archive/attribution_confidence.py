from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol


class EvidenceRecord(Protocol):
    def __getitem__(self, key: str, /) -> Any: ...


CandidateKind = Literal["character", "provisional_identity", "unresolved"]
ConfidenceClassification = Literal[
    "high_confidence",
    "review_required",
    "ambiguous",
    "unresolved",
    "conflicted",
    "human_decided",
]
ReviewPriority = Literal["low", "normal", "high"]
DecisionStatus = Literal["none", "pending", "accepted", "rejected", "conflicted"]
EvidenceDirection = Literal["supporting", "contradictory", "context"]


@dataclass(frozen=True)
class AttributionConfidenceConfig:
    ruleset_version: str = "attribution_confidence_v1"
    evidence_unit_weight: int = 1
    human_conflict_penalty: int = 1
    high_confidence_min_score: int = 3
    high_confidence_min_margin: int = 2

    def __post_init__(self) -> None:
        if not self.ruleset_version.strip():
            raise ValueError("ruleset_version must not be empty")
        for name, value in (
            ("evidence_unit_weight", self.evidence_unit_weight),
            ("human_conflict_penalty", self.human_conflict_penalty),
            ("high_confidence_min_score", self.high_confidence_min_score),
            ("high_confidence_min_margin", self.high_confidence_min_margin),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleset_version": self.ruleset_version,
            "score_unit": "one distinct message-level evidence record",
            "evidence_unit_weight": self.evidence_unit_weight,
            "human_conflict_penalty": self.human_conflict_penalty,
            "high_confidence_min_score": self.high_confidence_min_score,
            "high_confidence_min_margin": self.high_confidence_min_margin,
            "provisional_identity_score": None,
            "unresolved_score": None,
            "ambiguous_alias_candidates_are_all_retained": True,
            "automatic_acceptance": False,
        }


DEFAULT_ATTRIBUTION_CONFIDENCE_CONFIG = AttributionConfidenceConfig()


@dataclass(frozen=True)
class EvidenceContribution:
    evidence_type: str
    evidence_strength: Literal["explicit", "human_reviewed", "context"]
    direction: EvidenceDirection
    weight: int
    contribution: int
    source_message_ids: tuple[int, ...]
    source_identity_inference_run_id: int | None
    source_identity_evidence_id: int | None
    source_attribution_id: int | None
    source_review_event_ids: tuple[int, ...]
    evidence: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_type": self.evidence_type,
            "evidence_strength": self.evidence_strength,
            "direction": self.direction,
            "weight": self.weight,
            "contribution": self.contribution,
            "source_message_ids": list(self.source_message_ids),
            "source_identity_inference_run_id": self.source_identity_inference_run_id,
            "source_identity_evidence_id": self.source_identity_evidence_id,
            "source_attribution_id": self.source_attribution_id,
            "source_review_event_ids": list(self.source_review_event_ids),
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class AttributionConfidenceCandidate:
    message_observation_id: int
    candidate_kind: CandidateKind
    character_id: int | None
    provisional_identity_candidate_id: int | None
    candidate_label: str | None
    score: int | None
    rank: int
    score_margin: int | None
    classification: ConfidenceClassification
    review_priority: ReviewPriority
    mandatory_review: bool
    review_reasons: tuple[str, ...]
    decision_status: DecisionStatus
    source_attribution_ids: tuple[int, ...]
    evidence: tuple[EvidenceContribution, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_observation_id": self.message_observation_id,
            "candidate_kind": self.candidate_kind,
            "character_id": self.character_id,
            "provisional_identity_candidate_id": self.provisional_identity_candidate_id,
            "candidate_label": self.candidate_label,
            "score": self.score,
            "rank": self.rank,
            "score_margin": self.score_margin,
            "classification": self.classification,
            "review_priority": self.review_priority,
            "mandatory_review": self.mandatory_review,
            "review_reasons": list(self.review_reasons),
            "decision_status": self.decision_status,
            "source_attribution_ids": list(self.source_attribution_ids),
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True)
class AttributionConfidenceResult:
    candidates: tuple[AttributionConfidenceCandidate, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"candidates": [candidate.to_dict() for candidate in self.candidates]}


@dataclass
class _CandidateState:
    message_observation_id: int
    candidate_kind: CandidateKind
    character_id: int | None
    provisional_identity_candidate_id: int | None
    candidate_label: str | None
    score: int | None = 0
    evidence: list[EvidenceContribution] | None = None
    attribution_statuses: set[str] | None = None
    attribution_ids: set[int] | None = None
    review_reasons: set[str] | None = None
    conflicted: bool = False
    ambiguous_alias: bool = False
    provisional_link: bool = False
    classification: ConfidenceClassification | None = None
    review_priority: ReviewPriority | None = None
    mandatory_review: bool = False
    decision_status: DecisionStatus = "none"
    score_margin: int | None = None

    def __post_init__(self) -> None:
        self.evidence = [] if self.evidence is None else self.evidence
        self.attribution_statuses = (
            set() if self.attribution_statuses is None else self.attribution_statuses
        )
        self.attribution_ids = set() if self.attribution_ids is None else self.attribution_ids
        self.review_reasons = set() if self.review_reasons is None else self.review_reasons


def _value(record: EvidenceRecord, key: str, default: Any = None) -> Any:
    try:
        return record[key]
    except (KeyError, IndexError):
        return default


def _positive_id(value: Any, field: str, *, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _json_list(value: Any, field: str) -> tuple[int, ...]:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} must be valid JSON") from exc
    if not isinstance(decoded, list):
        raise ValueError(f"{field} must be a JSON array")
    return tuple(sorted({_positive_id(item, field) for item in decoded}))


def _integer_or_none(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _positive_id(value, field)


def score_attribution_evidence(
    identity_evidence: Sequence[EvidenceRecord],
    *,
    message_observation_ids: Sequence[int] = (),
    reviewed_attributions: Sequence[EvidenceRecord] = (),
    config: AttributionConfidenceConfig = DEFAULT_ATTRIBUTION_CONFIDENCE_CONFIG,
) -> AttributionConfidenceResult:
    """Aggregate Phase E evidence into explainable per-message candidates.

    Scores are integer evidence units, not probabilities. Alias evidence is
    aggregated only for the same normalized label in the same known server.
    Player-only claims and pending proposals never contribute to character
    scores. Existing accepted/rejected decisions remain separate from inference.
    """
    message_ids = {
        _positive_id(item, "message_observation_id")
        for item in message_observation_ids
    }
    label_groups: dict[tuple[int | None, str], list[EvidenceRecord]] = defaultdict(list)
    states: dict[tuple[int, str, int | None], _CandidateState] = {}
    seen_identity_sources: set[tuple[int | None, int]] = set()

    def state_for(
        message_id: int,
        kind: CandidateKind,
        character_id: int | None = None,
        provisional_id: int | None = None,
        label: str | None = None,
    ) -> _CandidateState:
        key_id = character_id if kind == "character" else provisional_id
        key = (message_id, kind, key_id)
        state = states.get(key)
        if state is None:
            state = _CandidateState(
                message_observation_id=message_id,
                candidate_kind=kind,
                character_id=character_id,
                provisional_identity_candidate_id=provisional_id,
                candidate_label=label,
            )
            states[key] = state
        elif state.candidate_label is None and label is not None:
            state.candidate_label = label
        return state

    for row in identity_evidence:
        message_id = _positive_id(
            _value(row, "message_observation_id"), "message_observation_id"
        )
        assert message_id is not None
        source_run_id = _integer_or_none(
            _value(row, "identity_inference_run_id"),
            "identity_inference_run_id",
        )
        source_key = (source_run_id, message_id)
        if source_key in seen_identity_sources:
            raise ValueError(
                "Identity evidence contains duplicate message observation "
                f"{message_id} for inference run {source_run_id}"
            )
        seen_identity_sources.add(source_key)
        message_ids.add(message_id)
        label = _value(row, "normalized_label_key", _value(row, "normalized_label"))
        if not isinstance(label, str) or not label.strip():
            raise ValueError("Identity evidence requires a non-empty normalized label")
        server_id = _value(row, "server_id")
        server_id = _positive_id(server_id, "server_id", allow_none=True)
        label_groups[(server_id, label)].append(row)

    ordered_groups = sorted(
        label_groups.items(),
        key=lambda item: (
            item[0][0] is not None,
            item[0][0] if item[0][0] is not None else 0,
            item[0][1],
        ),
    )
    for (server_id, label), unordered_rows in ordered_groups:
        group_rows = sorted(
            unordered_rows,
            key=lambda row: (
                int(_value(row, "message_observation_id")),
                _integer_or_none(_value(row, "evidence_id"), "evidence_id") or 0,
            ),
        )
        for row in group_rows:
            message_id = int(_value(row, "message_observation_id"))
            candidate_ids = _json_list(
                _value(row, "registered_character_ids_json", "[]"),
                "registered_character_ids_json",
            )
            evidence_kind = _value(row, "evidence_kind")
            linked_character_id = _integer_or_none(
                _value(row, "linked_character_id"), "linked_character_id"
            )
            if evidence_kind in {"registered_character_match", "ambiguous_registered_character_match"}:
                for character_id in candidate_ids:
                    candidate_state = state_for(
                        message_id,
                        "character",
                        character_id=character_id,
                        label=_value(row, "character_label"),
                    )
                    candidate_state.ambiguous_alias |= (
                        evidence_kind == "ambiguous_registered_character_match"
                        or len(candidate_ids) > 1
                    )
                    for supporting_row in group_rows:
                        supporting_ids = _json_list(
                            _value(supporting_row, "registered_character_ids_json", "[]"),
                            "registered_character_ids_json",
                        )
                        if character_id not in supporting_ids:
                            continue
                        source_message_id = int(_value(supporting_row, "message_observation_id"))
                        source_kind = _value(supporting_row, "evidence_kind")
                        contribution = config.evidence_unit_weight
                        candidate_state.score = (candidate_state.score or 0) + contribution
                        candidate_state.evidence.append(EvidenceContribution(
                            evidence_type=(
                                "ambiguous_registered_alias_match"
                                if source_kind == "ambiguous_registered_character_match"
                                or len(supporting_ids) > 1
                                else "exact_registered_character_alias_match"
                            ),
                            evidence_strength="explicit",
                            direction="supporting",
                            weight=config.evidence_unit_weight,
                            contribution=contribution,
                            source_message_ids=(source_message_id,),
                            source_identity_inference_run_id=_integer_or_none(
                                _value(supporting_row, "identity_inference_run_id"),
                                "identity_inference_run_id",
                            ),
                            source_identity_evidence_id=_integer_or_none(
                                _value(supporting_row, "evidence_id"), "evidence_id"
                            ),
                            source_attribution_id=None,
                            source_review_event_ids=(),
                            evidence={
                                "normalized_label": label,
                                "server_id": server_id,
                                "source_evidence_kind": source_kind,
                                "aggregation": "distinct message observations with same server and label",
                            },
                        ))
            elif evidence_kind != "provisional_label":
                raise ValueError(f"Unsupported Phase E evidence kind: {evidence_kind}")
            else:
                provisional_id = _integer_or_none(
                    _value(row, "candidate_id"), "candidate_id"
                )
                assert provisional_id is not None
                if linked_character_id is not None:
                    candidate_state = state_for(
                        message_id,
                        "character",
                        character_id=linked_character_id,
                        label=_value(row, "character_label"),
                    )
                    candidate_state.provisional_link = True
                    for supporting_row in group_rows:
                        if (
                            _value(supporting_row, "candidate_id") != provisional_id
                            or _value(supporting_row, "evidence_kind") != "provisional_label"
                        ):
                            continue
                        candidate_state.score = (candidate_state.score or 0) + config.evidence_unit_weight
                        candidate_state.evidence.append(EvidenceContribution(
                            evidence_type="reviewed_provisional_identity_link",
                            evidence_strength="human_reviewed",
                            direction="supporting",
                            weight=config.evidence_unit_weight,
                            contribution=config.evidence_unit_weight,
                            source_message_ids=(
                                int(_value(supporting_row, "message_observation_id")),
                            ),
                            source_identity_inference_run_id=_integer_or_none(
                                _value(supporting_row, "identity_inference_run_id"),
                                "identity_inference_run_id",
                            ),
                            source_identity_evidence_id=_integer_or_none(
                                _value(supporting_row, "evidence_id"), "evidence_id"
                            ),
                            source_attribution_id=None,
                            source_review_event_ids=_json_list(
                                _value(supporting_row, "candidate_link_review_event_ids_json", "[]"),
                                "candidate_link_review_event_ids_json",
                            ),
                            evidence={
                                "provisional_identity_candidate_id": provisional_id,
                                "normalized_label": label,
                                "server_id": server_id,
                                "automatic_message_attribution": False,
                            },
                        ))
                else:
                    candidate_state = state_for(
                        message_id,
                        "provisional_identity",
                        provisional_id=provisional_id,
                        label=_value(row, "provisional_label"),
                    )
                    for supporting_row in group_rows:
                        if (
                            _value(supporting_row, "candidate_id") != provisional_id
                            or _value(supporting_row, "evidence_kind") != "provisional_label"
                        ):
                            continue
                        candidate_state.evidence.append(EvidenceContribution(
                            evidence_type="provisional_identity_label_observation",
                            evidence_strength="explicit",
                            direction="context",
                            weight=0,
                            contribution=0,
                            source_message_ids=(
                                int(_value(supporting_row, "message_observation_id")),
                            ),
                            source_identity_inference_run_id=_integer_or_none(
                                _value(supporting_row, "identity_inference_run_id"),
                                "identity_inference_run_id",
                            ),
                            source_identity_evidence_id=_integer_or_none(
                                _value(supporting_row, "evidence_id"), "evidence_id"
                            ),
                            source_attribution_id=None,
                            source_review_event_ids=(),
                            evidence={
                                "provisional_identity_candidate_id": provisional_id,
                                "normalized_label": label,
                                "server_id": server_id,
                                "character_identity_inferred": False,
                            },
                        ))

    attributions_by_message: dict[int, list[EvidenceRecord]] = defaultdict(list)
    ordered_attributions = sorted(
        reviewed_attributions,
        key=lambda attribution: (
            int(_value(attribution, "message_observation_id")),
            _integer_or_none(_value(attribution, "attribution_id"), "attribution_id") or 0,
        ),
    )
    for attribution in ordered_attributions:
        message_id = _positive_id(
            _value(attribution, "message_observation_id"), "message_observation_id"
        )
        assert message_id is not None
        message_ids.add(message_id)
        if _value(attribution, "status") not in {"accepted", "rejected", "pending"}:
            continue
        character_id = _integer_or_none(
            _value(attribution, "character_id"), "character_id"
        )
        if character_id is None:
            continue
        attributions_by_message[message_id].append(attribution)

    for message_id, attributions in attributions_by_message.items():
        for attribution in attributions:
            character_id = int(_value(attribution, "character_id"))
            candidate_state = state_for(
                message_id,
                "character",
                character_id=character_id,
                label=_value(attribution, "character_name"),
            )
            status = str(_value(attribution, "status"))
            candidate_state.attribution_statuses.add(status)
            attribution_id = _integer_or_none(
                _value(attribution, "attribution_id"), "attribution_id"
            )
            if attribution_id is not None:
                candidate_state.attribution_ids.add(attribution_id)
            event_ids = _json_list(
                _value(attribution, "review_event_ids_json", "[]"),
                "review_event_ids_json",
            )
            if status == "rejected":
                candidate_state.score = (candidate_state.score or 0) - config.human_conflict_penalty
                candidate_state.evidence.append(EvidenceContribution(
                    evidence_type="reviewed_message_attribution_rejection",
                    evidence_strength="human_reviewed",
                    direction="contradictory",
                    weight=config.human_conflict_penalty,
                    contribution=-config.human_conflict_penalty,
                    source_message_ids=(message_id,),
                    source_identity_inference_run_id=None,
                    source_identity_evidence_id=None,
                    source_attribution_id=attribution_id,
                    source_review_event_ids=event_ids,
                    evidence={"method": _value(attribution, "method"), "status": status},
                ))
            elif status == "accepted":
                candidate_state.evidence.append(EvidenceContribution(
                    evidence_type="human_accepted_message_attribution",
                    evidence_strength="human_reviewed",
                    direction="context",
                    weight=0,
                    contribution=0,
                    source_message_ids=(message_id,),
                    source_identity_inference_run_id=None,
                    source_identity_evidence_id=None,
                    source_attribution_id=attribution_id,
                    source_review_event_ids=event_ids,
                    evidence={"method": _value(attribution, "method"), "status": status},
                ))
            else:
                candidate_state.evidence.append(EvidenceContribution(
                    evidence_type="pending_message_attribution_proposal",
                    evidence_strength="context",
                    direction="context",
                    weight=0,
                    contribution=0,
                    source_message_ids=(message_id,),
                    source_identity_inference_run_id=None,
                    source_identity_evidence_id=None,
                    source_attribution_id=attribution_id,
                    source_review_event_ids=event_ids,
                    evidence={"method": _value(attribution, "method"), "status": status},
                ))

    for message_id in message_ids:
        if not any(key[0] == message_id for key in states):
            state_for(message_id, "unresolved")
        char_states = [
            state for state in states.values()
            if state.message_observation_id == message_id and state.candidate_kind == "character"
        ]
        accepted_ids = {
            state.character_id
            for state in char_states
            if "accepted" in state.attribution_statuses
        }
        if accepted_ids:
            accepted_id = next(iter(accepted_ids))
            accepted_state = next(
                state for state in char_states if state.character_id == accepted_id
            )
            accepted_source = next(
                (
                    item
                    for item in accepted_state.evidence
                    if item.evidence_type == "human_accepted_message_attribution"
                ),
                None,
            )
            for state in char_states:
                has_inference_support = any(
                    item.direction == "supporting"
                    for item in state.evidence
                )
                if state.character_id != accepted_id and has_inference_support:
                    state.conflicted = True
                    state.review_reasons.add("inference_disagrees_with_human_accepted_attribution")
                    if accepted_source is not None:
                        state.score = (state.score or 0) - config.human_conflict_penalty
                        state.evidence.append(EvidenceContribution(
                            evidence_type="conflicting_human_accepted_attribution",
                            evidence_strength="human_reviewed",
                            direction="contradictory",
                            weight=config.human_conflict_penalty,
                            contribution=-config.human_conflict_penalty,
                            source_message_ids=(message_id,),
                            source_identity_inference_run_id=None,
                            source_identity_evidence_id=None,
                            source_attribution_id=accepted_source.source_attribution_id,
                            source_review_event_ids=accepted_source.source_review_event_ids,
                            evidence={
                                "accepted_character_id": accepted_id,
                                "conflicting_candidate_character_id": state.character_id,
                            },
                        ))
                if state.character_id == accepted_id and any(
                    other.character_id != accepted_id
                    and any(item.direction == "supporting" for item in other.evidence)
                    for other in char_states
                ):
                    state.conflicted = True
                    state.review_reasons.add("human_attribution_conflicts_with_competing_inference")
        for state in char_states:
            if "rejected" in state.attribution_statuses and any(
                item.direction == "supporting" for item in state.evidence
            ):
                state.conflicted = True
                state.review_reasons.add("inference_disagrees_with_human_rejected_attribution")
            if "accepted" in state.attribution_statuses and not state.conflicted:
                state.review_reasons.add("existing_human_decision")
            if "pending" in state.attribution_statuses:
                state.review_reasons.add("existing_pending_proposal_requires_review")
            if state.provisional_link:
                state.review_reasons.add("provisional_identity_link_is_not_message_attribution")

        plausible = [
            state for state in char_states
            if (state.score or 0) > 0
        ]
        plausible.sort(key=lambda item: (
            -(item.score or 0),
            item.character_id if item.character_id is not None else math.inf,
        ))
        top_score = plausible[0].score if plausible else 0
        second_score = plausible[1].score if len(plausible) > 1 else 0
        margin = top_score - second_score
        ambiguous = len(plausible) > 1
        has_provisional = any(
            state.message_observation_id == message_id
            and state.candidate_kind == "provisional_identity"
            for state in states.values()
        )
        if not plausible and not has_provisional and not accepted_ids:
            state_for(message_id, "unresolved")

        message_states = [
            state for state in states.values()
            if state.message_observation_id == message_id
        ]
        for state in message_states:
            statuses = state.attribution_statuses
            if state.candidate_kind == "character":
                if state.conflicted:
                    classification: ConfidenceClassification = "conflicted"
                    priority: ReviewPriority = "high"
                    mandatory = True
                    state.review_reasons.add("human_or_candidate_evidence_conflict")
                elif ambiguous and state in plausible:
                    classification = "ambiguous"
                    priority = "high"
                    mandatory = True
                    state.review_reasons.add("multiple_supported_character_candidates")
                elif state.provisional_link:
                    classification = "review_required"
                    priority = "normal"
                    mandatory = True
                elif "accepted" in statuses:
                    classification = "human_decided"
                    priority = "low"
                    mandatory = False
                elif "pending" in statuses:
                    classification = "review_required"
                    priority = "normal"
                    mandatory = True
                elif (state.score or 0) >= config.high_confidence_min_score and (
                    state.score or 0
                ) - second_score >= config.high_confidence_min_margin and not state.ambiguous_alias:
                    classification = "high_confidence"
                    priority = "low"
                    mandatory = False
                elif (state.score or 0) > 0:
                    classification = "review_required"
                    priority = "normal"
                    mandatory = True
                    state.review_reasons.add("unique_deterministic_attribution_not_established")
                elif "rejected" in statuses:
                    classification = "human_decided"
                    priority = "low"
                    mandatory = False
                else:
                    classification = "unresolved"
                    priority = "normal"
                    mandatory = True
            elif state.candidate_kind == "provisional_identity":
                classification = "unresolved"
                priority = "normal"
                mandatory = True
                state.review_reasons.add("provisional_identity_requires_character_resolution")
            else:
                classification = "unresolved"
                priority = "normal"
                mandatory = True
                state.review_reasons.add("no_usable_character_identity_evidence")

            if statuses & {"accepted", "rejected"}:
                decision_status: DecisionStatus = (
                    "accepted" if "accepted" in statuses
                    else "rejected"
                )
            elif "pending" in statuses:
                decision_status = "pending"
            else:
                decision_status = "none"

            state.classification = classification
            state.review_priority = priority
            state.mandatory_review = mandatory
            state.decision_status = decision_status
            state.score_margin = margin if plausible else None

    result_candidates: list[AttributionConfidenceCandidate] = []
    for message_id in sorted(message_ids):
        message_states = [
            state for state in states.values()
            if state.message_observation_id == message_id
        ]
        message_states.sort(key=lambda state: (
            0 if state.candidate_kind == "character" else 1,
            -(state.score or 0),
            state.character_id if state.character_id is not None else math.inf,
            state.provisional_identity_candidate_id
            if state.provisional_identity_candidate_id is not None
            else math.inf,
        ))
        for rank, state in enumerate(message_states, start=1):
            if state.classification is None or state.review_priority is None:
                raise RuntimeError("Attribution candidate classification was not assigned")
            result_candidates.append(AttributionConfidenceCandidate(
                message_observation_id=message_id,
                candidate_kind=state.candidate_kind,
                character_id=state.character_id,
                provisional_identity_candidate_id=state.provisional_identity_candidate_id,
                candidate_label=state.candidate_label,
                score=state.score if state.candidate_kind == "character" else None,
                rank=rank,
                score_margin=state.score_margin if state.candidate_kind == "character" else None,
                classification=state.classification,
                review_priority=state.review_priority,
                mandatory_review=state.mandatory_review,
                review_reasons=tuple(sorted(state.review_reasons)),
                decision_status=state.decision_status,
                source_attribution_ids=tuple(sorted(state.attribution_ids)),
                evidence=tuple(state.evidence),
            ))

    return AttributionConfidenceResult(candidates=tuple(result_candidates))
