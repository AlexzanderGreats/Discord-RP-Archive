from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from tkinter import messagebox, ttk
import tkinter as tk
from typing import Any

from archive.attribution_review import (
    ReviewAction,
    get_attribution_review_candidate,
    list_attribution_review_candidate_messages,
    list_attribution_review_characters,
    list_attribution_review_queue,
    review_attribution_candidate,
)
from archive.reader_service import ReaderService


PAGE_SIZE = 100
MESSAGE_PAGE_SIZE = 25


def format_attribution_evidence(message: dict[str, Any]) -> str:
    evidence = message.get("evidence", [])
    blocks = [
        f"Evidence message {message['discord_message_id']} "
        f"(observation {message['message_observation_id']})",
        f"Timestamp: {message.get('timestamp_utc') or 'Undated'}",
        f"Server / channel: {message.get('server_name') or 'Unknown'} / "
        f"{message.get('channel_name') or 'Unknown'}",
        f"Discord author: {message.get('author_display_name_original') or message.get('author_nickname_original') or message.get('author_discord_name_original') or 'Unknown'} "
        f"(ID {message.get('author_discord_user_id_original') or 'unavailable'})",
        f"Source: {message.get('source_path') or 'source ID ' + str(message.get('source_id') or 'unavailable')}",
        f"Candidate: {message.get('candidate_kind')} | {message.get('classification')} | "
        f"operational score {message.get('score') if message.get('score') is not None else 'not assigned'}",
        f"Evidence state: {'excluded' if message.get('is_excluded') else 'active'}",
        "",
        "Original message text (unchanged):",
        message.get("content_original") or "",
        "",
        "Identity evidence:",
    ]
    if not evidence:
        blocks.append("No identity evidence was attached to this candidate.")
    for item in evidence:
        blocks.append(
            f"- {item.get('direction', 'unknown').upper()} / "
            f"{item.get('evidence_strength', 'unknown')} / {item.get('evidence_type', 'unknown')} "
            f"(weight {item.get('weight')}, contribution {item.get('contribution')})"
        )
        blocks.append(json.dumps(item.get("evidence", {}), ensure_ascii=False, indent=2))
    return "\n".join(blocks)


def format_attribution_audit(detail: dict[str, Any]) -> str:
    lines = ["F2 audit history (immutable backend records)"]
    for row in detail.get("audit_history", []):
        lines.extend(
            [
                "",
                f"{row.get('happened_at')} — {row.get('action')} / {row.get('outcome')}",
                f"Reviewer: {row.get('actor') or 'Unknown'}",
                f"Note: {row.get('note') or 'None'}",
                f"Previous state: {row.get('previous_state_json') or '{}'}",
                f"Details: {row.get('detail_json') or '{}'}",
            ]
        )
    if not detail.get("audit_history"):
        lines.append("\nNo prior F2 review events.")
    lines.append("\nCurrent authoritative attributions (read-only):")
    lines.append(
        json.dumps(
            detail.get("current_authoritative_attributions", []),
            ensure_ascii=False,
            indent=2,
        )
    )
    return "\n".join(lines)


def sort_review_queue(
    items: Sequence[dict[str, Any]],
    order: str,
) -> list[dict[str, Any]]:
    rows = list(items)
    if order == "Label":
        rows.sort(
            key=lambda row: (
                row["candidates"][0].get("character_name")
                or row["candidates"][0].get("provisional_label")
                or row["candidates"][0].get("candidate_label")
                or "Unresolved identity"
            ).casefold()
        )
    elif order == "Operational score":
        rows.sort(
            key=lambda row: (
                row["candidates"][0].get("score") is not None,
                row["candidates"][0].get("score") or 0,
            ),
            reverse=True,
        )
    elif order == "Evidence count":
        rows.sort(
            key=lambda row: max(
                (int(candidate.get("evidence_count", 0)) for candidate in row["candidates"]),
                default=0,
            ),
            reverse=True,
        )
    return rows


class AttributionReviewService:
    """GUI-neutral adapter over the F2 review API and read-only message context."""

    def __init__(
        self,
        connection_context: Callable[[], AbstractContextManager[sqlite3.Connection]],
    ) -> None:
        self._connection_context = connection_context

    def load_queue(
        self,
        *,
        offset: int,
        filters: dict[str, str],
        limit: int = PAGE_SIZE,
    ) -> dict[str, Any]:
        with self._connection_context() as connection:
            rows = list_attribution_review_queue(
                connection,
                include_decided=True,
                limit=limit + 1,
                offset=offset,
                priorities=(filters["priority"],) if filters.get("priority") else (
                    "high", "normal", "low"
                ),
                states=(filters["state"],) if filters.get("state") else None,
                classifications=(filters["classification"],)
                if filters.get("classification") else None,
                candidate_kinds=(filters["kind"],) if filters.get("kind") else None,
                label_query=filters.get("label") or None,
                server_name=filters.get("server") or None,
                channel_name=filters.get("channel") or None,
                stale_only=(
                    filters["stale"] == "Stale"
                    if filters.get("stale") in {"Stale", "Fresh"}
                    else None
                ),
                include_evidence=False,
                include_message_content=False,
            )
        return {"items": rows[:limit], "has_more": len(rows) > limit}

    def get_candidate(self, confidence_candidate_id: int) -> dict[str, Any]:
        with self._connection_context() as connection:
            return get_attribution_review_candidate(
                connection, confidence_candidate_id, include_group_history=False
            )

    @staticmethod
    def _last_reviewed(decisions: list[dict[str, Any]]) -> str:
            latest = max(
                decisions,
                key=lambda decision: int(decision.get("latest_event_id") or 0),
                default=None,
            )
            return str(latest.get("happened_at") or "") if latest else ""

    def get_candidate_messages(
        self,
        confidence_candidate_id: int,
        *,
        offset: int,
    ) -> dict[str, Any]:
        with self._connection_context() as connection:
            return list_attribution_review_candidate_messages(
                connection,
                confidence_candidate_id,
                offset=offset,
                limit=MESSAGE_PAGE_SIZE,
            )

    def list_characters(self) -> list[dict[str, Any]]:
        with self._connection_context() as connection:
            return list_attribution_review_characters(connection)

    def get_message_context(self, message_id: int) -> dict[str, Any] | None:
        with self._connection_context() as connection:
            return ReaderService(connection).get_message_context(
                message_id, before=5, after=5
            )

    def perform_action(
        self,
        *,
        confidence_run_id: int,
        confidence_candidate_id: int,
        action: ReviewAction,
        actor: str,
        note: str | None,
        character_id: int | None,
        target_candidate_id: int | None,
        message_ids: Sequence[int],
        expected_latest_event_id: int,
        queue_offset: int,
        filters: dict[str, str],
    ) -> dict[str, Any]:
        with self._connection_context() as connection:
            try:
                event_ids = review_attribution_candidate(
                    connection,
                    confidence_run_id=confidence_run_id,
                    confidence_candidate_id=confidence_candidate_id,
                    action=action,
                    actor=actor,
                    note=note,
                    character_id=character_id,
                    target_candidate_id=target_candidate_id,
                    message_observation_ids=message_ids,
                    expected_latest_event_id=expected_latest_event_id,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            detail = get_attribution_review_candidate(
                connection, confidence_candidate_id, include_group_history=False
            )
            rows = list_attribution_review_queue(
                connection,
                confidence_run_id=confidence_run_id,
                include_decided=True,
                limit=PAGE_SIZE + 1,
                offset=queue_offset,
                priorities=(filters["priority"],) if filters.get("priority") else (
                    "high", "normal", "low"
                ),
                states=(filters["state"],) if filters.get("state") else None,
                classifications=(filters["classification"],)
                if filters.get("classification") else None,
                candidate_kinds=(filters["kind"],) if filters.get("kind") else None,
                label_query=filters.get("label") or None,
                server_name=filters.get("server") or None,
                channel_name=filters.get("channel") or None,
                stale_only=(
                    filters["stale"] == "Stale"
                    if filters.get("stale") in {"Stale", "Fresh"}
                    else None
                ),
                include_evidence=False,
                include_message_content=False,
            )
        return {
            "event_ids": event_ids,
            "detail": detail,
            "queue": {"items": rows[:PAGE_SIZE], "has_more": len(rows) > PAGE_SIZE},
        }


class AttributionReviewTab:
    """Tk presentation for the Phase F2 attribution-review workflow."""

    def __init__(
        self,
        parent: ttk.Notebook,
        root: tk.Tk,
        service: AttributionReviewService,
        run_background: Callable[[Callable[[], Any], Callable[[Any], None], Callable[[Exception], None]], None],
    ) -> None:
        self.root = root
        self.service = service
        self.run_background = run_background
        self.page = ttk.Frame(parent, padding=10)
        parent.add(self.page, text="Attribution Review")
        self.queue_offset = 0
        self._has_more = False
        self.queue_items: list[dict[str, Any]] = []
        self.current_queue_item: dict[str, Any] | None = None
        self.current_detail: dict[str, Any] | None = None
        self.current_candidate_id: int | None = None
        self.message_page_offset = 0
        self.message_page: dict[str, Any] = {"messages": [], "total": 0}
        self._detail_request_id: int | None = None
        self._build()
        self.refresh_queue()

    def _build(self) -> None:
        filters = ttk.Frame(self.page)
        filters.pack(fill="x", pady=(0, 8))
        self.filter_vars = {
            "label": tk.StringVar(),
            "priority": tk.StringVar(value=""),
            "state": tk.StringVar(value=""),
            "classification": tk.StringVar(value=""),
            "kind": tk.StringVar(value=""),
            "stale": tk.StringVar(value=""),
            "server": tk.StringVar(),
            "channel": tk.StringVar(),
        }
        ttk.Label(filters, text="Label").grid(row=0, column=0, sticky="w")
        label_entry = ttk.Entry(filters, textvariable=self.filter_vars["label"], width=18)
        label_entry.grid(row=0, column=1, padx=(4, 10), sticky="ew")
        self._filter_combo(filters, "Priority", "priority", ("", "high", "normal", "low"), 2)
        self._filter_combo(
            filters,
            "State",
            "state",
            ("", "review_required", "already_resolved", "unresolved", "conflicted", "excluded", "stale"),
            4,
        )
        self._filter_combo(
            filters,
            "Confidence",
            "classification",
            ("", "high_confidence", "review_required", "ambiguous", "unresolved", "conflicted", "human_decided"),
            6,
        )
        self._filter_combo(
            filters, "Identity", "kind", ("", "character", "provisional_identity", "unresolved"), 8
        )
        self._filter_combo(filters, "Input", "stale", ("", "Fresh", "Stale"), 10)
        ttk.Label(filters, text="Server").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(filters, textvariable=self.filter_vars["server"], width=18).grid(
            row=1, column=1, padx=(4, 10), sticky="ew", pady=(6, 0)
        )
        ttk.Label(filters, text="Channel").grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Entry(filters, textvariable=self.filter_vars["channel"], width=18).grid(
            row=1, column=3, padx=(4, 10), sticky="ew", pady=(6, 0)
        )
        ttk.Label(filters, text="Sort").grid(row=1, column=4, sticky="w", pady=(6, 0))
        self.sort_var = tk.StringVar(value="F2 priority")
        sort_picker = ttk.Combobox(
            filters,
            textvariable=self.sort_var,
            values=("F2 priority", "Label", "Operational score", "Evidence count"),
            state="readonly",
            width=20,
        )
        sort_picker.grid(row=1, column=5, columnspan=2, sticky="w", pady=(6, 0))
        sort_picker.bind("<<ComboboxSelected>>", self._sort_changed)
        ttk.Button(filters, text="Apply filters", command=self.refresh_queue).grid(
            row=1, column=8, padx=(4, 0), pady=(6, 0)
        )
        ttk.Button(filters, text="Refresh", command=self.refresh_queue).grid(
            row=1, column=9, padx=(4, 0), pady=(6, 0)
        )
        filters.columnconfigure(1, weight=1)

        self.status = ttk.Label(self.page, text="Loading review queue…", style="Muted.TLabel")
        self.status.pack(fill="x", pady=(0, 6))
        panes = ttk.Panedwindow(self.page, orient="horizontal")
        panes.pack(fill="both", expand=True)
        queue_frame = ttk.Frame(panes)
        detail_frame = ttk.Frame(panes, padding=(10, 0, 0, 0))
        panes.add(queue_frame, weight=2)
        panes.add(detail_frame, weight=5)

        columns = (
            "candidate_id", "label", "kind", "state", "priority",
            "confidence", "score", "count", "scope", "last_review",
        )
        self.queue_tree = ttk.Treeview(
            queue_frame, columns=columns, show="headings", selectmode="browse", height=18
        )
        headings = (
            ("candidate_id", "ID", 72),
            ("label", "Candidate", 150),
            ("kind", "Identity", 105),
            ("state", "F2 state", 135),
            ("priority", "Priority", 75),
            ("confidence", "Classification", 125),
            ("score", "F1 score", 65),
            ("count", "Evidence", 65),
            ("scope", "Server / channel", 145),
            ("last_review", "Last reviewed", 145),
        )
        for column, title, width in headings:
            self.queue_tree.heading(column, text=title)
            self.queue_tree.column(column, width=width, minwidth=55, stretch=column in {"label", "scope"})
        self.queue_tree.tag_configure("stale", foreground="#a33b2b")
        self.queue_tree.tag_configure("excluded", foreground="#7a5a00")
        queue_scroll = ttk.Scrollbar(queue_frame, orient="vertical", command=self.queue_tree.yview)
        queue_horizontal_scroll = ttk.Scrollbar(
            queue_frame, orient="horizontal", command=self.queue_tree.xview
        )
        self.queue_tree.configure(
            yscrollcommand=queue_scroll.set,
            xscrollcommand=queue_horizontal_scroll.set,
        )
        self.queue_tree.pack(side="left", fill="both", expand=True)
        queue_scroll.pack(side="right", fill="y")
        queue_horizontal_scroll.pack(side="bottom", fill="x")
        self.queue_tree.bind("<<TreeviewSelect>>", self._queue_selected)

        page_controls = ttk.Frame(queue_frame)
        page_controls.pack(fill="x", pady=(6, 0))
        self.previous_button = ttk.Button(page_controls, text="Previous", command=self._previous_page)
        self.previous_button.pack(side="left")
        self.page_label = ttk.Label(page_controls, text="Page 1", style="Muted.TLabel")
        self.page_label.pack(side="left", padx=8)
        self.next_button = ttk.Button(page_controls, text="Next", command=self._next_page)
        self.next_button.pack(side="left")

        self.candidate_heading = ttk.Label(
            detail_frame, text="Select a review item.", font=("Segoe UI", 13, "bold")
        )
        self.candidate_heading.pack(anchor="w")
        self.candidate_summary = ttk.Label(detail_frame, text="", style="Muted.TLabel", wraplength=900)
        self.candidate_summary.pack(anchor="w", pady=(3, 8))
        actor_row = ttk.Frame(detail_frame)
        actor_row.pack(fill="x", pady=(0, 6))
        ttk.Label(actor_row, text="Reviewer").pack(side="left")
        self.actor_var = tk.StringVar(value=self._default_actor())
        ttk.Entry(actor_row, textvariable=self.actor_var, width=24).pack(side="left", padx=(6, 16))
        ttk.Label(actor_row, text="Human review note").pack(side="left")
        self.note_var = tk.StringVar()
        ttk.Entry(actor_row, textvariable=self.note_var).pack(side="left", fill="x", expand=True, padx=(6, 0))

        actions = ttk.Frame(detail_frame)
        actions.pack(fill="x", pady=(0, 8))
        buttons = (
            ("Confirm candidate", "confirm"),
            ("Assign character", "assign_character"),
            ("Link provisional", "link_provisional"),
            ("Leave unresolved", "leave_unresolved"),
            ("Reject candidate", "reject"),
            ("Exclude evidence", "exclude_evidence"),
            ("Restore evidence", "restore_evidence"),
            ("Split evidence", "split_evidence"),
            ("Reassign evidence", "reassign_evidence"),
            ("Merge candidate", "merge_candidates"),
        )
        self.action_buttons: dict[str, ttk.Button] = {}
        for index, (title, action) in enumerate(buttons):
            button = ttk.Button(
                actions,
                text=title,
                command=lambda selected_action=action: self._action(selected_action),
            )
            button.grid(row=index // 5, column=index % 5, padx=(0, 5), pady=2, sticky="ew")
            self.action_buttons[action] = button
            actions.columnconfigure(index % 5, weight=1)
        self._set_actions_enabled(False)

        self.detail_pages = ttk.Notebook(detail_frame)
        self.detail_pages.pack(fill="both", expand=True)
        evidence_tab = ttk.Frame(self.detail_pages, padding=6)
        alternatives_tab = ttk.Frame(self.detail_pages, padding=6)
        audit_tab = ttk.Frame(self.detail_pages, padding=6)
        context_tab = ttk.Frame(self.detail_pages, padding=6)
        self.detail_pages.add(evidence_tab, text="Evidence")
        self.detail_pages.add(alternatives_tab, text="Alternatives")
        self.detail_pages.add(audit_tab, text="Review history")
        self.detail_pages.add(context_tab, text="Channel context")

        evidence_nav = ttk.Frame(evidence_tab)
        evidence_nav.pack(fill="x", pady=(0, 5))
        self.message_previous = ttk.Button(evidence_nav, text="Previous evidence", command=self._previous_message)
        self.message_previous.pack(side="left")
        self.message_page_label = ttk.Label(evidence_nav, text="", style="Muted.TLabel")
        self.message_page_label.pack(side="left", padx=8)
        self.message_next = ttk.Button(evidence_nav, text="Next evidence", command=self._next_message_page)
        self.message_next.pack(side="left")
        ttk.Label(
            evidence_tab,
            text="Select one or more equivalent messages. Context messages are never treated as attribution evidence.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 5))
        self.evidence_messages = ttk.Treeview(
            evidence_tab,
            columns=("timestamp", "author", "message", "identity"),
            show="headings",
            selectmode="extended",
            height=6,
        )
        for column, title, width in (
            ("timestamp", "Timestamp", 145),
            ("author", "Discord author", 150),
            ("message", "Original message", 300),
            ("identity", "Candidate", 150),
        ):
            self.evidence_messages.heading(column, text=title)
            self.evidence_messages.column(column, width=width, minwidth=60, stretch=column == "message")
        self.evidence_messages.pack(fill="x", expand=False)
        self.evidence_messages.bind("<<TreeviewSelect>>", self._evidence_selected)
        self.evidence_text = tk.Text(evidence_tab, height=11, wrap="word", state="disabled")
        evidence_scroll = ttk.Scrollbar(evidence_tab, orient="vertical", command=self.evidence_text.yview)
        self.evidence_text.configure(yscrollcommand=evidence_scroll.set)
        self.evidence_text.pack(side="left", fill="both", expand=True, pady=(6, 0))
        evidence_scroll.pack(side="right", fill="y", pady=(6, 0))

        self.alternatives = ttk.Treeview(
            alternatives_tab,
            columns=(
                "identity", "kind", "classification", "score",
                "supporting", "contradictory", "review", "rank",
            ),
            show="headings",
            selectmode="browse",
        )
        for column, title, width in (
            ("identity", "Alternative identity", 220),
            ("kind", "Type", 140),
            ("classification", "Operational classification", 190),
            ("score", "F1 score", 70),
            ("supporting", "Support", 75),
            ("contradictory", "Contradict", 80),
            ("review", "Review state", 120),
            ("rank", "Rank", 65),
        ):
            self.alternatives.heading(column, text=title)
            self.alternatives.column(column, width=width, minwidth=60, stretch=column == "identity")
        self.alternatives.pack(fill="both", expand=True)
        self.alternatives.bind("<<TreeviewSelect>>", self._alternative_selected)

        self.audit_text = tk.Text(audit_tab, wrap="word", state="disabled")
        audit_scroll = ttk.Scrollbar(audit_tab, orient="vertical", command=self.audit_text.yview)
        self.audit_text.configure(yscrollcommand=audit_scroll.set)
        self.audit_text.pack(side="left", fill="both", expand=True)
        audit_scroll.pack(side="right", fill="y")

        context_controls = ttk.Frame(context_tab)
        context_controls.pack(fill="x")
        ttk.Button(
            context_controls, text="Load surrounding channel messages", command=self._load_context
        ).pack(side="left")
        self.context_status = ttk.Label(context_controls, text="", style="Muted.TLabel")
        self.context_status.pack(side="left", padx=8)
        self.context_text = tk.Text(context_tab, wrap="word", state="disabled")
        context_scroll = ttk.Scrollbar(context_tab, orient="vertical", command=self.context_text.yview)
        self.context_text.configure(yscrollcommand=context_scroll.set)
        self.context_text.pack(side="left", fill="both", expand=True, pady=(6, 0))
        context_scroll.pack(side="right", fill="y", pady=(6, 0))

    def _filter_combo(
        self,
        parent: ttk.Frame,
        label: str,
        key: str,
        values: Sequence[str],
        column: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=0, column=column, sticky="w")
        picker = ttk.Combobox(
            parent,
            textvariable=self.filter_vars[key],
            values=values,
            state="readonly",
            width=17,
        )
        picker.grid(row=0, column=column + 1, padx=(4, 10), sticky="ew")

    @staticmethod
    def _default_actor() -> str:
        import getpass

        try:
            return getpass.getuser()
        except OSError:
            return ""

    def _filters(self) -> dict[str, str]:
        return {key: variable.get().strip() for key, variable in self.filter_vars.items()}

    def refresh_queue(self, *, reset: bool = True) -> None:
        if reset:
            self.queue_offset = 0
        self.status.configure(text="Loading review queue from F2…")
        self.run_background(
            lambda: self.service.load_queue(offset=self.queue_offset, filters=self._filters()),
            self._queue_loaded,
            self._load_failed,
        )

    def _queue_loaded(self, result: dict[str, Any]) -> None:
        self.queue_items = result["items"]
        self._has_more = result["has_more"]
        self._populate_queue()
        self.page_label.configure(text=f"Page {self.queue_offset // PAGE_SIZE + 1}")
        self.previous_button.configure(
            state="normal" if self.queue_offset else "disabled"
        )
        self.next_button.configure(state="normal" if self._has_more else "disabled")
        if not self.queue_items:
            self.status.configure(text="No candidates match these filters on this page.")
        else:
            self.status.configure(
                text=f"Showing {len(self.queue_items)} bounded F2 queue item(s); filters are applied by F2."
            )

    def _populate_queue(self) -> None:
        self.queue_tree.delete(*self.queue_tree.get_children())
        rows = sort_review_queue(self.queue_items, self.sort_var.get())
        for row in rows:
            candidate = row["candidates"][0]
            message_id = int(row["message_observation_id"])
            score = candidate.get("score")
            values = (
                candidate["attribution_confidence_candidate_id"],
                self._candidate_label(candidate),
                candidate["candidate_kind"],
                self._display_state(row),
                row["review_priority"],
                candidate["classification"],
                "" if score is None else str(score),
                str(self._evidence_count(row)),
                " / ".join(value or "?" for value in (row["server_name"], row["channel_name"])),
                self._last_reviewed(row["decisions"]),
            )
            tags = ("stale",) if row["stale"] else ("excluded",) if row["state"] == "excluded" else ()
            self.queue_tree.insert("", "end", iid=str(message_id), values=values, tags=tags)
        self._sync_page_controls()

    @staticmethod
    def _candidate_label(candidate: dict[str, Any]) -> str:
        return str(
            candidate.get("character_name")
            or candidate.get("provisional_label")
            or candidate.get("candidate_label")
            or "Unresolved identity"
        )

    @staticmethod
    def _evidence_count(row: dict[str, Any]) -> int:
        return max((int(candidate.get("evidence_count", 0)) for candidate in row["candidates"]), default=0)

    @staticmethod
    def _display_state(row: dict[str, Any]) -> str:
        if row["stale"]:
            return "stale"
        if row["state"] == "excluded":
            return "excluded"
        if not row["reviewed"]:
            return f"Never reviewed · {row['state']}"
        if row.get("human_decision_status") == "unresolved":
            return "Reviewed — unresolved"
        if row.get("human_decision_status") == "accepted":
            return "Resolved"
        if row["reviewed"]:
            return f"Previously reviewed · {row['state']}"
        return str(row["state"])

    def _sync_page_controls(self) -> None:
        if hasattr(self, "_has_more"):
            self.next_button.configure(state="normal" if self._has_more else "disabled")

    def _sort_changed(self, event: tk.Event[tk.Misc]) -> None:
        del event
        self._populate_queue()

    def _previous_page(self) -> None:
        if self.queue_offset >= PAGE_SIZE:
            self.queue_offset -= PAGE_SIZE
            self.refresh_queue(reset=False)

    def _next_page(self) -> None:
        if self._has_more:
            self.queue_offset += PAGE_SIZE
            self.refresh_queue(reset=False)

    def _queue_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        del _event
        selected = self.queue_tree.selection()
        if not selected:
            return
        message_id = int(selected[0])
        self.current_queue_item = next(
            item for item in self.queue_items
            if int(item["message_observation_id"]) == message_id
        )
        candidate = self.current_queue_item["candidates"][0]
        self._select_candidate(int(candidate["attribution_confidence_candidate_id"]))

    def _select_candidate(self, confidence_candidate_id: int) -> None:
        self.current_candidate_id = confidence_candidate_id
        self._detail_request_id = confidence_candidate_id
        self.candidate_heading.configure(text="Loading candidate detail from F2…")
        self.run_background(
            lambda: self.service.get_candidate(confidence_candidate_id),
            lambda detail: self._detail_loaded(confidence_candidate_id, detail),
            self._load_failed,
        )

    def _detail_loaded(self, requested_id: int, detail: dict[str, Any]) -> None:
        if requested_id != self._detail_request_id:
            return
        self.current_detail = detail
        candidate = detail["candidate"]
        label = self._candidate_label(candidate)
        warning = " — STALE: underlying inputs changed; F2 will reject unsafe actions" if candidate["stale"] else ""
        self.candidate_heading.configure(
            text=f"{label} (candidate {candidate['attribution_confidence_candidate_id']}){warning}"
        )
        self.candidate_summary.configure(
            text=(
                f"Candidate type: {detail['candidate_type']}  |  "
                f"Provisional identity ID: {candidate.get('provisional_identity_candidate_id') or 'not applicable'}  |  "
                f"F2 state: {detail['current_review_state']}  |  "
                f"Operational classification: {detail['operational_confidence']}  |  "
                f"Operational score: {candidate.get('score') if candidate.get('score') is not None else 'not assigned'}  |  "
                f"Review priority: {detail['review_priority']}  |  "
                f"Equivalent messages: {detail['affected_message_count']}"
            )
        )
        self._populate_alternatives(detail["alternatives"], candidate["attribution_confidence_candidate_id"])
        self._render_audit(detail)
        self.message_page_offset = 0
        self._load_candidate_messages()
        self._update_action_availability()

    def _populate_alternatives(
        self,
        alternatives: list[dict[str, Any]],
        selected_candidate_id: int,
    ) -> None:
        self.alternatives.delete(*self.alternatives.get_children())
        for candidate in alternatives:
            evidence = candidate.get("evidence", [])
            candidate_id = int(candidate["attribution_confidence_candidate_id"])
            self.alternatives.insert(
                "",
                "end",
                iid=str(candidate_id),
                values=(
                    self._candidate_label(candidate),
                    candidate["candidate_kind"],
                    candidate["classification"],
                    "" if candidate["score"] is None else candidate["score"],
                    sum(item["direction"] == "supporting" for item in evidence),
                    sum(item["direction"] == "contradictory" for item in evidence),
                    candidate.get("review_status", "never_reviewed"),
                    candidate["candidate_rank"],
                ),
            )
        selected_id = str(selected_candidate_id)
        if self.alternatives.exists(selected_id):
            self.alternatives.selection_set(selected_id)

    def _alternative_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        del _event
        selected = self.alternatives.selection()
        if selected and int(selected[0]) != self.current_candidate_id:
            self._select_candidate(int(selected[0]))

    def _load_candidate_messages(self) -> None:
        if self.current_candidate_id is None:
            return
        candidate_id = self.current_candidate_id
        offset = self.message_page_offset
        self.run_background(
            lambda: self.service.get_candidate_messages(candidate_id, offset=offset),
            lambda page: self._candidate_messages_loaded(candidate_id, offset, page),
            self._load_failed,
        )

    def _candidate_messages_loaded(
        self,
        candidate_id: int,
        offset: int,
        page: dict[str, Any],
    ) -> None:
        if candidate_id != self.current_candidate_id or offset != self.message_page_offset:
            return
        self.message_page = page
        self.evidence_messages.delete(*self.evidence_messages.get_children())
        for message in page["messages"]:
            message_id = int(message["message_observation_id"])
            author = (
                message.get("author_display_name_original")
                or message.get("author_nickname_original")
                or message.get("author_discord_name_original")
                or message.get("author_discord_user_id_original")
                or "Unknown Discord author"
            )
            preview = " ".join((message.get("content_original") or "").split())
            self.evidence_messages.insert(
                "",
                "end",
                iid=str(message_id),
                values=(
                    message.get("timestamp_utc") or "Undated",
                    author,
                    preview[:220],
                    message.get("candidate_kind", ""),
                ),
            )
        start = min(offset + 1, page["total"]) if page["total"] else 0
        end = min(offset + len(page["messages"]), page["total"])
        self.message_page_label.configure(text=f"Messages {start}–{end} of {page['total']}")
        self.message_previous.configure(state="normal" if offset else "disabled")
        self.message_next.configure(
            state="normal" if offset + MESSAGE_PAGE_SIZE < page["total"] else "disabled"
        )
        if page["messages"]:
            self.evidence_messages.selection_set(str(page["messages"][0]["message_observation_id"]))
            self._render_evidence(page["messages"][0])
        else:
            self._set_text(self.evidence_text, "No evidence messages are available for this candidate.")
        self._update_action_availability()

    def _evidence_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        del _event
        selected = self.evidence_messages.selection()
        message = next(
            (
                item for item in self.message_page.get("messages", [])
                if str(item["message_observation_id"]) in selected
            ),
            None,
        )
        if message:
            self._render_evidence(message)

    def _render_evidence(self, message: dict[str, Any]) -> None:
        self._set_text(self.evidence_text, format_attribution_evidence(message))

    def _previous_message(self) -> None:
        self.message_page_offset = max(0, self.message_page_offset - MESSAGE_PAGE_SIZE)
        self._load_candidate_messages()

    def _next_message_page(self) -> None:
        if self.message_page_offset + MESSAGE_PAGE_SIZE < self.message_page.get("total", 0):
            self.message_page_offset += MESSAGE_PAGE_SIZE
            self._load_candidate_messages()

    def _render_audit(self, detail: dict[str, Any]) -> None:
        self._set_text(self.audit_text, format_attribution_audit(detail))

    def _load_context(self) -> None:
        selected = self.evidence_messages.selection()
        if not selected:
            messagebox.showinfo("No evidence selected", "Select an evidence message first.", parent=self.root)
            return
        message_id = int(selected[0])
        self.context_status.configure(text="Loading read-only channel context…")
        self.run_background(
            lambda: self.service.get_message_context(message_id),
            lambda context: self._context_loaded(message_id, context),
            self._load_failed,
        )

    def _context_loaded(self, message_id: int, context: dict[str, Any] | None) -> None:
        if context is None:
            self.context_status.configure(text="The source message is no longer available.")
            self._set_text(self.context_text, "")
            return
        message = context["message"]
        self.context_status.configure(
            text=f"Context only — channel {message.get('channel_name') or 'unknown'}; "
            "surrounding messages are not attribution evidence."
        )
        entries = context["before"] + [message] + context["after"]
        blocks = []
        for item in entries:
            anchor = int(item["message_observation_id"]) == message_id
            author = (
                item.get("author_display_name_original")
                or item.get("author_nickname_original")
                or item.get("author_discord_name_original")
                or item.get("author_discord_user_id_original")
                or "Unknown Discord author"
            )
            marker = "  [selected evidence]" if anchor else "  [context only]"
            blocks.append(
                f"{item.get('timestamp_utc') or 'Undated'} | {author}{marker}\n"
                f"{item.get('content_original') or ''}"
            )
        self._set_text(self.context_text, "\n\n".join(blocks))

    def _selected_messages(self) -> list[dict[str, Any]]:
        selected_ids = set(self.evidence_messages.selection())
        return [
            item for item in self.message_page.get("messages", [])
            if str(item["message_observation_id"]) in selected_ids
        ]

    def _update_action_availability(self) -> None:
        if not self.current_detail:
            self._set_actions_enabled(False)
            return
        candidate = self.current_detail["candidate"]
        kind = candidate["candidate_kind"]
        stale = bool(candidate["stale"])
        excluded = bool(candidate.get("excluded"))
        has_message = bool(self.message_page.get("messages"))
        self._set_actions_enabled(not stale and has_message)
        self.action_buttons["confirm"].configure(
            state="normal" if not stale and has_message and kind == "character" else "disabled"
        )
        self.action_buttons["reject"].configure(
            state="normal" if not stale and has_message and kind == "character" else "disabled"
        )
        self.action_buttons["link_provisional"].configure(
            state="normal" if not stale and has_message and kind == "provisional_identity" else "disabled"
        )
        self.action_buttons["leave_unresolved"].configure(
            state="normal" if not stale and has_message and kind in {"provisional_identity", "unresolved"} else "disabled"
        )
        self.action_buttons["restore_evidence"].configure(
            state="normal" if has_message and excluded else "disabled"
        )
        self.action_buttons["split_evidence"].configure(
            state="normal" if not stale and has_message and kind == "provisional_identity" else "disabled"
        )
        target_available = any(
            item["candidate_kind"] == "provisional_identity"
            and item.get("provisional_identity_candidate_id")
            != candidate.get("provisional_identity_candidate_id")
            for item in self.current_detail["alternatives"]
        )
        for action in ("reassign_evidence", "merge_candidates"):
            self.action_buttons[action].configure(
                state="normal" if not stale and has_message and kind == "provisional_identity" and target_available else "disabled"
            )

    def _set_actions_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in getattr(self, "action_buttons", {}).values():
            button.configure(state=state)

    def _action(self, action: str) -> None:
        if not self.current_detail or not self.current_queue_item:
            return
        if not self._is_action_fresh(action):
            return
        chosen_messages = self._selected_messages()
        if not chosen_messages:
            messagebox.showinfo(
                "Select evidence",
                "Select at least one evidence message before reviewing this candidate.",
                parent=self.root,
            )
            return
        candidate = chosen_messages[0]
        message_ids = [int(item["message_observation_id"]) for item in chosen_messages]
        candidate_id = int(candidate["confidence_candidate_id"])
        character_id: int | None = None
        target_id: int | None = None
        if action in {"assign_character", "link_provisional"}:
            character = self._choose_character()
            if character is None:
                return
            character_id = int(character["character_id"])
        if action in {"reassign_evidence", "merge_candidates"}:
            target_id = self._choose_target_candidate()
            if target_id is None:
                return
        action_label = action.replace("_", " ")
        if action == "merge_candidates":
            confirmation = (
                "Merge the entire source provisional identity into the selected target?\n\n"
                "F2 will reassign every active evidence message attached to the source "
                "provisional identity; this is candidate-wide and is not limited to the "
                "currently selected evidence rows. The evidence list is paged and may not "
                "show every source message at once. F2 will validate the full input set "
                "and write per-message audit records."
            )
        elif action == "link_provisional":
            confirmation = (
                "Link this entire provisional identity to the selected registered character?\n\n"
                "The link applies to the provisional identity, not just the selected message. "
                "It does not create a character, a player, or a message-level attribution."
            )
        else:
            confirmation = (
                f"Apply '{action_label}' to {len(message_ids)} selected evidence message(s)?\n\n"
                f"Message observation IDs: {', '.join(map(str, message_ids))}\n"
                "The F2 workflow will validate the candidate and write the audit record."
            )
        if action == "exclude_evidence":
            confirmation += (
                "\n\nExcluded evidence is retained in the archive but omitted from "
                "subsequent F1 confidence runs. This can be reversed with Restore evidence."
            )
        elif action == "restore_evidence":
            confirmation += (
                "\n\nRestored evidence becomes eligible for subsequent F1 confidence runs; "
                "the original message and audit history remain unchanged."
            )
        if action in {
            "confirm", "reject", "assign_character", "link_provisional",
            "exclude_evidence", "restore_evidence", "split_evidence",
            "reassign_evidence", "merge_candidates", "leave_unresolved",
        } and not messagebox.askyesno("Confirm review action", confirmation, parent=self.root):
            return
        actor = self.actor_var.get().strip()
        if not actor:
            messagebox.showerror("Reviewer required", "Enter a reviewer identity.", parent=self.root)
            return
        selected_event_ids = {
            item.get("latest_review_event_id")
            for item in chosen_messages
        }
        if len(selected_event_ids) > 1:
            messagebox.showwarning(
                "Review messages individually",
                "The selected messages have different prior review versions. "
                "Review them individually so F2 can safely validate each decision.",
                parent=self.root,
            )
            return
        expected_event_id = int(next(iter(selected_event_ids)) or 0)
        self._set_actions_enabled(False)
        self.status.configure(text=f"Applying '{action_label}' through F2…")
        self.run_background(
            lambda: self.service.perform_action(
                confidence_run_id=int(self.current_queue_item["confidence_run_id"]),
                confidence_candidate_id=candidate_id,
                action=action,  # type: ignore[arg-type]
                actor=actor,
                note=self.note_var.get().strip() or None,
                character_id=character_id,
                target_candidate_id=target_id,
                message_ids=message_ids,
                expected_latest_event_id=expected_event_id,
                queue_offset=self.queue_offset,
                filters=self._filters(),
            ),
            self._action_succeeded,
            self._action_failed,
        )

    def _is_action_fresh(self, action: str) -> bool:
        candidate = self.current_detail["candidate"]
        if not candidate["stale"]:
            return True
        if action == "restore_evidence" and candidate.get("excluded"):
            return True
        messagebox.showwarning(
            "Stale candidate",
            "F2 marks this candidate stale. Refresh or recompute attribution inputs before reviewing it.",
            parent=self.root,
        )
        return False

    def _choose_character(self) -> dict[str, Any] | None:
        try:
            characters = self.service.list_characters()
        except Exception as error:
            self._show_backend_error("Could not load registered characters", error)
            return None
        if not characters:
            messagebox.showinfo(
                "No registered characters",
                "There are no registered characters to assign or link. F3 does not create characters.",
                parent=self.root,
            )
            return None
        dialog = tk.Toplevel(self.root)
        dialog.title("Select registered character")
        dialog.transient(self.root)
        dialog.grab_set()
        ttk.Label(
            dialog,
            text="Select an existing registered character. This does not create a character or assign a player.",
            wraplength=420,
        ).pack(anchor="w", padx=12, pady=(12, 6))
        by_label = {
            f"{item['character_name']} (ID {item['character_id']})": item
            for item in characters
        }
        selected_label = tk.StringVar(value=next(iter(by_label)))
        ttk.Combobox(
            dialog,
            textvariable=selected_label,
            values=list(by_label),
            state="readonly",
            width=48,
        ).pack(fill="x", padx=12, pady=6)
        result: dict[str, Any] | None = None

        def accept() -> None:
            nonlocal result
            result = by_label[selected_label.get()]
            dialog.destroy()

        actions = ttk.Frame(dialog)
        actions.pack(fill="x", padx=12, pady=12)
        ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(actions, text="Select", command=accept).pack(side="right", padx=(0, 8))
        dialog.wait_window()
        return result

    def _choose_target_candidate(self) -> int | None:
        alternatives = [
            item for item in self.current_detail["alternatives"]
            if item["candidate_kind"] == "provisional_identity"
            and item.get("provisional_identity_candidate_id")
            != self.current_detail["candidate"].get("provisional_identity_candidate_id")
        ]
        if not alternatives:
            messagebox.showinfo(
                "No target candidate",
                "F2 exposes no alternative provisional identity candidate for this action.",
                parent=self.root,
            )
            return None
        labels = {
            f"{self._candidate_label(item)} (candidate ID {item['provisional_identity_candidate_id']})":
                int(item["provisional_identity_candidate_id"])
            for item in alternatives
        }
        dialog = tk.Toplevel(self.root)
        dialog.title("Select target provisional identity")
        dialog.transient(self.root)
        dialog.grab_set()
        ttk.Label(
            dialog,
            text="Choose an existing alternative provisional identity. F2 will validate the operation.",
            wraplength=420,
        ).pack(anchor="w", padx=12, pady=(12, 6))
        selected_label = tk.StringVar(value=next(iter(labels)))
        ttk.Combobox(
            dialog,
            textvariable=selected_label,
            values=list(labels),
            state="readonly",
            width=48,
        ).pack(fill="x", padx=12, pady=6)
        result: int | None = None

        def accept() -> None:
            nonlocal result
            result = labels[selected_label.get()]
            dialog.destroy()

        actions = ttk.Frame(dialog)
        actions.pack(fill="x", padx=12, pady=12)
        ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(actions, text="Select", command=accept).pack(side="right", padx=(0, 8))
        dialog.wait_window()
        return result

    def _action_succeeded(self, result: dict[str, Any]) -> None:
        self.status.configure(
            text=f"F2 recorded {len(result['event_ids'])} review event(s); reloaded current state."
        )
        self.current_detail = result["detail"]
        self.current_candidate_id = int(result["detail"]["candidate"]["attribution_confidence_candidate_id"])
        self._detail_request_id = self.current_candidate_id
        self._detail_loaded(self.current_candidate_id, result["detail"])
        self._queue_loaded(result["queue"])

    def _action_failed(self, error: Exception) -> None:
        self._show_backend_error("Review action failed", error)
        self.refresh_queue(reset=False)
        if self.current_candidate_id is not None:
            self._select_candidate(self.current_candidate_id)

    def _load_failed(self, error: Exception) -> None:
        self._show_backend_error("Could not load attribution review data", error)

    def _show_backend_error(self, title: str, error: Exception) -> None:
        if isinstance(error, sqlite3.Error):
            category = "Database or transaction error"
        elif isinstance(error, ValueError):
            if "does not exist" in str(error):
                category = "Candidate no longer exists"
            elif "stale" in str(error).casefold():
                category = "Candidate became stale"
            else:
                category = "F2 rejected the action or the review state is no longer valid"
        else:
            category = "Unexpected backend error"
        messagebox.showerror(title, f"{category}:\n\n{error}", parent=self.root)

    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")
