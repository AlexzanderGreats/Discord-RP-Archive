from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from archive.signals import extract_speaker_marker_signals


class MessageRecord(Protocol):
    def __getitem__(self, key: str, /) -> Any: ...


IdentityEvidenceKind = Literal[
    "provisional_label",
    "registered_character_match",
    "ambiguous_registered_character_match",
]


@dataclass(frozen=True)
class ProvisionalIdentityCandidate:
    candidate_key: str
    server_id: int | None
    normalized_label: str
    display_label: str
    message_observation_ids: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_key": self.candidate_key,
            "server_id": self.server_id,
            "normalized_label": self.normalized_label,
            "display_label": self.display_label,
            "message_observation_ids": list(self.message_observation_ids),
        }


@dataclass(frozen=True)
class IdentityEvidence:
    message_observation_id: int
    server_id: int | None
    observed_label: str
    normalized_label: str
    evidence_kind: IdentityEvidenceKind
    candidate_key: str | None
    registered_character_ids: tuple[int, ...]
    evidence: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_observation_id": self.message_observation_id,
            "server_id": self.server_id,
            "observed_label": self.observed_label,
            "normalized_label": self.normalized_label,
            "evidence_kind": self.evidence_kind,
            "candidate_key": self.candidate_key,
            "registered_character_ids": list(self.registered_character_ids),
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class IdentityInferenceResult:
    candidates: tuple[ProvisionalIdentityCandidate, ...]
    evidence: tuple[IdentityEvidence, ...]


_BRACKETED_LABEL_RE = re.compile(r"^\[([^\[\]\r\n]+)\]$")


def normalize_identity_label(label: str) -> str:
    """Normalize labels for deterministic grouping without changing stored text."""
    return " ".join(label.casefold().split())


def _candidate_key(
    normalized_label: str,
    server_id: int | None,
    observation_id: int,
) -> str:
    payload = json.dumps(
        {
            "server_id": server_id,
            "normalized_label": normalized_label,
            "singleton_observation_id": observation_id if server_id is None else None,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"speaker_{digest[:32]}"


def _record_value(record: MessageRecord, key: str) -> Any:
    try:
        return record[key]
    except (KeyError, IndexError):
        return None


def infer_identity_candidates(
    messages: Sequence[MessageRecord],
    *,
    registered_aliases: Mapping[str, Sequence[int]] | None = None,
) -> IdentityInferenceResult:
    """Infer provisional speaker-label groups while retaining all ambiguity.

    Alias keys are normalized character aliases mapped to their registered
    character IDs. Provisional labels group only within a known server; when
    server scope is missing, each observation remains a separate candidate.
    This function never infers player identity.
    """
    aliases: dict[str, set[int]] = defaultdict(set)
    for alias, character_ids in (registered_aliases or {}).items():
        normalized_alias = normalize_identity_label(alias)
        if not normalized_alias:
            continue
        for character_id in character_ids:
            if isinstance(character_id, bool) or not isinstance(character_id, int) or character_id < 1:
                raise ValueError("registered character IDs must be positive integers")
            aliases[normalized_alias].add(character_id)

    messages_by_id: dict[int, MessageRecord] = {}
    for record in messages:
        observation_id = _record_value(record, "message_observation_id")
        if (
            isinstance(observation_id, bool)
            or not isinstance(observation_id, int)
            or observation_id < 1
        ):
            raise ValueError("Each message requires a positive integer message_observation_id")
        if observation_id in messages_by_id:
            raise ValueError(f"Duplicate message_observation_id: {observation_id}")
        messages_by_id[observation_id] = record

    candidates_by_key: dict[str, list[tuple[int, int | None, str, str]]] = defaultdict(list)
    evidence_items: list[IdentityEvidence] = []
    for signal in extract_speaker_marker_signals(messages):
        observation_id = signal.source_message_ids[0]
        line = signal.evidence.get("content_line")
        match = _BRACKETED_LABEL_RE.fullmatch(line) if isinstance(line, str) else None
        observed_label = (
            match.group(1)
            if match is not None
            else str(signal.observed_value["text"])
        )
        normalized_label = normalize_identity_label(observed_label)
        if not normalized_label:
            continue

        server_id = _record_value(messages_by_id[observation_id], "server_id")
        if server_id is not None and (
            isinstance(server_id, bool) or not isinstance(server_id, int) or server_id < 1
        ):
            raise ValueError("server_id must be a positive integer or None")
        matched_character_ids = tuple(sorted(aliases.get(normalized_label, set())))
        candidate_key: str | None = None
        if len(matched_character_ids) == 1:
            evidence_kind: IdentityEvidenceKind = "registered_character_match"
        elif len(matched_character_ids) > 1:
            evidence_kind = "ambiguous_registered_character_match"
        else:
            evidence_kind = "provisional_label"
            candidate_key = _candidate_key(normalized_label, server_id, observation_id)
            candidates_by_key[candidate_key].append(
                (observation_id, server_id, observed_label, normalized_label)
            )

        record = messages_by_id[observation_id]
        evidence_items.append(IdentityEvidence(
            message_observation_id=observation_id,
            server_id=server_id,
            observed_label=observed_label,
            normalized_label=normalized_label,
            evidence_kind=evidence_kind,
            candidate_key=candidate_key,
            registered_character_ids=matched_character_ids,
            evidence={
                "signal_type": signal.signal_type,
                "evidence_kind": signal.evidence_kind,
                "content_line": line,
                "identity_resolved": False,
                "author_identity_id": _record_value(record, "author_identity_id"),
                "author_discord_user_id_original": _record_value(
                    record, "author_discord_user_id_original"
                ),
                "webhook_id_original": _record_value(record, "webhook_id_original"),
                "player_inferred": False,
            },
        ))

    candidates: list[ProvisionalIdentityCandidate] = []
    for candidate_key, entries in candidates_by_key.items():
        ordered = sorted(entries, key=lambda item: item[0])
        candidates.append(ProvisionalIdentityCandidate(
            candidate_key=candidate_key,
            server_id=ordered[0][1],
            normalized_label=ordered[0][3],
            display_label=ordered[0][2],
            message_observation_ids=tuple(item[0] for item in ordered),
        ))
    candidates.sort(key=lambda candidate: candidate.candidate_key)
    evidence_items.sort(key=lambda item: item.message_observation_id)
    return IdentityInferenceResult(
        candidates=tuple(candidates),
        evidence=tuple(evidence_items),
    )
