from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol


EvidenceKind = Literal["explicit", "heuristic"]


class MessageRecord(Protocol):
    def __getitem__(self, key: str, /) -> Any: ...


_SCENE_MARKERS = {
    "scene start": "scene_start",
    "scene end": "scene_end",
    "scene break": "scene_break",
    "time skip": "time_skip",
    "transition": "transition",
}
_SCENE_MARKER_RE = re.compile(r"^\s*\[\[\s*([^\]]+?)\s*\]\]\s*$", re.IGNORECASE)
_SPEAKER_MARKER_RE = re.compile(r"^\[([^\[\]\r\n]+)\]$")
_OOC_RE = re.compile(r"^\s*(?:OOC\s*:|\(OOC\))", re.IGNORECASE)
_NARRATIVE_CUE_RE = re.compile(
    r"^\s*(location|setting|time)\s*:\s*(.*?)\s*$", re.IGNORECASE
)


@dataclass(frozen=True)
class Signal:
    """One inspectable observation; it is not a score or an accepted conclusion."""

    signal_type: str
    source_message_ids: tuple[int, ...]
    evidence_kind: EvidenceKind
    observed_value: Mapping[str, Any]
    evidence: Mapping[str, Any]


@dataclass(frozen=True)
class _Message:
    observation_id: int
    discord_message_id: str
    record: MessageRecord
    timestamp_seconds: float | None
    timestamp_issue: tuple[str, Any] | None
    source_payload: Mapping[str, Any] | None
    source_payload_issue: str | None


def _get(record: MessageRecord, key: str) -> Any:
    try:
        return record[key]
    except (KeyError, IndexError):
        return None


def _parse_timestamp(record: MessageRecord) -> tuple[float | None, tuple[str, Any] | None]:
    unix_value = _get(record, "timestamp_unix")
    utc_value = _get(record, "timestamp_utc")
    parsed_unix: float | None = None
    parsed_utc: float | None = None
    issues: list[tuple[str, Any]] = []

    if unix_value is not None:
        if isinstance(unix_value, (int, float)) and not isinstance(unix_value, bool):
            if math.isfinite(float(unix_value)):
                parsed_unix = float(unix_value)
            else:
                issues.append(("timestamp_unix_not_finite", repr(unix_value)))
        else:
            issues.append(("timestamp_unix_invalid_type", type(unix_value).__name__))

    if utc_value is not None:
        if isinstance(utc_value, str):
            try:
                parsed = datetime.fromisoformat(utc_value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("timezone is missing")
                parsed_utc = parsed.timestamp()
            except ValueError:
                issues.append(("timestamp_utc_invalid", utc_value))
        else:
            issues.append(("timestamp_utc_invalid_type", type(utc_value).__name__))

    if parsed_unix is not None and parsed_utc is not None:
        if not math.isclose(parsed_unix, parsed_utc, rel_tol=0.0, abs_tol=0.001):
            issues.append(("timestamp_fields_disagree", {
                "timestamp_unix": parsed_unix,
                "timestamp_utc": utc_value,
            }))
        return parsed_unix, issues[0] if issues else None

    parsed = parsed_unix if parsed_unix is not None else parsed_utc
    if parsed is not None:
        return parsed, issues[0] if issues else None
    if issues:
        return None, issues[0]
    return None, ("timestamp_missing", None)


def _load_source_payload(record: MessageRecord) -> tuple[Mapping[str, Any] | None, str | None]:
    raw = _get(record, "source_json")
    if raw is None:
        return None, "source_json_missing"
    if not isinstance(raw, str):
        return None, f"source_json_invalid_type:{type(raw).__name__}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, "source_json_invalid_json"
    if not isinstance(payload, dict):
        return None, "source_json_not_object"
    return payload, None


def _message(record: MessageRecord) -> _Message:
    observation_id = _get(record, "message_observation_id")
    if isinstance(observation_id, bool) or not isinstance(observation_id, int):
        raise ValueError("Each message requires an integer message_observation_id")
    discord_message_id = _get(record, "discord_message_id")
    if discord_message_id is None:
        raise ValueError("Each message requires a discord_message_id")
    timestamp_seconds, timestamp_issue = _parse_timestamp(record)
    source_payload, source_payload_issue = _load_source_payload(record)
    return _Message(
        observation_id=observation_id,
        discord_message_id=str(discord_message_id),
        record=record,
        timestamp_seconds=timestamp_seconds,
        timestamp_issue=timestamp_issue,
        source_payload=source_payload,
        source_payload_issue=source_payload_issue,
    )


def _identity(message: _Message) -> tuple[str, str] | None:
    webhook_id = _get(message.record, "webhook_id_original")
    if webhook_id is not None:
        return ("webhook", str(webhook_id))
    discord_id = _get(message.record, "author_discord_user_id_original")
    if discord_id is not None:
        return ("discord_user", str(discord_id))
    identity_id = _get(message.record, "author_identity_id")
    if identity_id is not None:
        return ("archive_identity", str(identity_id))
    return None


def _thread_value(message: _Message) -> tuple[str, str] | None:
    payload = message.source_payload
    if payload is None:
        return None
    for key in ("thread_id", "threadId", "thread_name", "threadName"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            kind = "id" if key in {"thread_id", "threadId"} else "name"
            return (kind, str(value).strip())
    return None


def _content_lines(content: Any) -> list[str]:
    if not isinstance(content, str):
        return []
    return [line.strip() for line in content.splitlines() if line.strip()]


def _speaker_marker(lines: list[str]) -> tuple[str, str] | None:
    for line in lines:
        scene_match = _SCENE_MARKER_RE.fullmatch(line)
        if scene_match and scene_match.group(1).casefold() in _SCENE_MARKERS:
            continue
        if _NARRATIVE_CUE_RE.fullmatch(line) or _OOC_RE.match(line):
            continue
        match = _SPEAKER_MARKER_RE.fullmatch(line)
        if match:
            return match.group(1).strip(), line
        break
    return None


def _narrative_context(lines: list[str]) -> dict[str, str]:
    contexts: dict[str, str] = {}
    for line in lines:
        match = _NARRATIVE_CUE_RE.fullmatch(line)
        if match and match.group(2):
            contexts[match.group(1).casefold()] = match.group(2)
    return contexts


def _speaker_marker_signal(message: _Message) -> Signal | None:
    lines = _content_lines(_get(message.record, "content_original"))
    speaker = _speaker_marker(lines)
    if speaker is None:
        return None
    speaker_text, speaker_line = speaker
    return Signal(
        signal_type="speaker_marker",
        source_message_ids=(message.observation_id,),
        evidence_kind="explicit",
        observed_value={"text": speaker_text},
        evidence={"content_line": speaker_line, "identity_resolved": False},
    )


def extract_speaker_marker_signals(messages: Sequence[MessageRecord]) -> list[Signal]:
    """Extract only header speaker-marker signals from message observations."""
    return [
        signal
        for signal in (_speaker_marker_signal(_message(record)) for record in messages)
        if signal is not None
    ]


def extract_signals(messages: Sequence[MessageRecord]) -> list[Signal]:
    """Extract deterministic signals from chronologically ordered message observations.

    The input order is retained for adjacent-message comparisons. Callers should
    provide a stable chronological ordering, such as timestamp then observation ID.
    Missing or malformed timestamps remain visible as data-quality signals.
    """
    parsed_messages = [_message(record) for record in messages]
    signals: list[Signal] = []

    for message in parsed_messages:
        message_id = message.observation_id
        if message.timestamp_issue is not None:
            issue, value = message.timestamp_issue
            signal_type = "timestamp_missing" if issue == "timestamp_missing" else "timestamp_invalid"
            signals.append(Signal(
                signal_type=signal_type,
                source_message_ids=(message_id,),
                evidence_kind="explicit",
                observed_value={"issue": issue, "value": value},
                evidence={"field": "timestamp_unix/timestamp_utc"},
            ))
        if message.source_payload_issue is not None:
            signals.append(Signal(
                signal_type="source_payload_invalid",
                source_message_ids=(message_id,),
                evidence_kind="explicit",
                observed_value={"issue": message.source_payload_issue},
                evidence={"field": "source_json"},
            ))

        lines = _content_lines(_get(message.record, "content_original"))
        for line in lines:
            marker_match = _SCENE_MARKER_RE.fullmatch(line)
            if marker_match:
                marker = _SCENE_MARKERS.get(marker_match.group(1).casefold())
                if marker is not None:
                    signals.append(Signal(
                        signal_type="explicit_scene_marker",
                        source_message_ids=(message_id,),
                        evidence_kind="explicit",
                        observed_value={"marker": marker},
                        evidence={"content_line": line, "syntax": "[[SCENE START]]"},
                    ))
                continue
            cue_match = _NARRATIVE_CUE_RE.fullmatch(line)
            if cue_match and cue_match.group(2):
                signals.append(Signal(
                    signal_type="narrative_context_marker",
                    source_message_ids=(message_id,),
                    evidence_kind="explicit",
                    observed_value={
                        "label": cue_match.group(1).casefold(),
                        "value": cue_match.group(2),
                    },
                    evidence={"content_line": line},
                ))

        for line in lines:
            if _OOC_RE.match(line):
                signals.append(Signal(
                    signal_type="ooc_marker",
                    source_message_ids=(message_id,),
                    evidence_kind="explicit",
                    observed_value={"content_prefix": line},
                    evidence={"rule": "line begins with OOC: or (OOC)"},
                ))

        speaker_signal = _speaker_marker_signal(message)
        if speaker_signal is not None:
            signals.append(speaker_signal)

    by_discord_id: dict[str, list[_Message]] = defaultdict(list)
    for message in parsed_messages:
        by_discord_id[message.discord_message_id].append(message)

    for left, right in zip(parsed_messages, parsed_messages[1:]):
        pair_ids = (left.observation_id, right.observation_id)
        left_unix = left.timestamp_seconds
        right_unix = right.timestamp_seconds
        if left_unix is not None and right_unix is not None:
            signals.append(Signal(
                signal_type="timestamp_gap",
                source_message_ids=pair_ids,
                evidence_kind="heuristic",
                observed_value={"gap_seconds": right_unix - left_unix},
                evidence={
                    "left_timestamp_unix": left_unix,
                    "right_timestamp_unix": right_unix,
                    "negative_gap": right_unix < left_unix,
                },
            ))

        left_channel = _get(left.record, "channel_id")
        right_channel = _get(right.record, "channel_id")
        if left_channel is not None and right_channel is not None and left_channel != right_channel:
            signals.append(Signal(
                signal_type="channel_change",
                source_message_ids=pair_ids,
                evidence_kind="heuristic",
                observed_value={"from": left_channel, "to": right_channel},
                evidence={"field": "channel_id"},
            ))

        left_identity = _identity(left)
        right_identity = _identity(right)
        if left_identity is not None and right_identity is not None and left_identity != right_identity:
            signals.append(Signal(
                signal_type="author_change",
                source_message_ids=pair_ids,
                evidence_kind="heuristic",
                observed_value={"from": left_identity, "to": right_identity},
                evidence={"field": "author_discord_user_id_original/webhook_id_original"},
            ))

        left_thread = _thread_value(left)
        right_thread = _thread_value(right)
        if left_thread is not None and right_thread is not None and left_thread != right_thread:
            signals.append(Signal(
                signal_type="thread_change",
                source_message_ids=pair_ids,
                evidence_kind="heuristic",
                observed_value={"from": left_thread, "to": right_thread},
                evidence={"field": "source_json thread metadata"},
            ))

        for field in ("source_id", "import_id"):
            left_value = _get(left.record, field)
            right_value = _get(right.record, field)
            if left_value is not None and right_value is not None and left_value != right_value:
                signals.append(Signal(
                    signal_type="collection_boundary",
                    source_message_ids=pair_ids,
                    evidence_kind="explicit",
                    observed_value={"field": field, "from": left_value, "to": right_value},
                    evidence={"field": field},
                ))

        left_speaker = _speaker_marker(_content_lines(_get(left.record, "content_original")))
        right_speaker = _speaker_marker(_content_lines(_get(right.record, "content_original")))
        if (
            left_speaker is not None
            and right_speaker is not None
            and left_speaker[0].casefold() != right_speaker[0].casefold()
        ):
            signals.append(Signal(
                signal_type="speaker_marker_change",
                source_message_ids=pair_ids,
                evidence_kind="heuristic",
                observed_value={
                    "from": left_speaker[0],
                    "to": right_speaker[0],
                },
                evidence={"rule": "different leading bracketed labels; not identity attribution"},
            ))

        left_context = _narrative_context(_content_lines(_get(left.record, "content_original")))
        right_context = _narrative_context(_content_lines(_get(right.record, "content_original")))
        for label in sorted(left_context.keys() & right_context.keys()):
            if left_context[label] != right_context[label]:
                signals.append(Signal(
                    signal_type="narrative_context_transition",
                    source_message_ids=pair_ids,
                    evidence_kind="heuristic",
                    observed_value={
                        "label": label,
                        "from": left_context[label],
                        "to": right_context[label],
                    },
                    evidence={"rule": "different values for the same labeled context field"},
                ))

    for message in parsed_messages:
        reply_id = _get(message.record, "reply_to_discord_message_id")
        if reply_id is None:
            continue
        targets = by_discord_id.get(str(reply_id), [])
        target_ids = tuple(target.observation_id for target in targets)
        source_ids = (message.observation_id, *target_ids)
        signals.append(Signal(
            signal_type="reply_relationship",
            source_message_ids=source_ids,
            evidence_kind="explicit",
            observed_value={
                "reply_to_discord_message_id": str(reply_id),
                "resolved_observation_ids": target_ids,
            },
            evidence={
                "field": "reply_to_discord_message_id",
                "target_resolution": (
                    "unresolved" if not targets else "ambiguous" if len(targets) > 1 else "resolved"
                ),
            },
        ))
        if len(targets) > 1:
            signals.append(Signal(
                signal_type="reply_target_ambiguous",
                source_message_ids=source_ids,
                evidence_kind="explicit",
                observed_value={"discord_message_id": str(reply_id), "candidate_count": len(targets)},
                evidence={"candidate_observation_ids": target_ids},
            ))

    return signals
