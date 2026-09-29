from __future__ import annotations

import os
import sqlite3
import tkinter as tk
from queue import Empty, Queue
from pathlib import Path
from threading import Thread
from tkinter import filedialog, messagebox, ttk
from typing import Any

from archive.reader_service import ReaderService
from archive.ollama_service import ArchiveAnswerService, OllamaClient
from archive.search_service import SearchService
from archive.sync_service import StartupSyncReport, synchronize_archive


class ArchiveApp:
    def __init__(self, root: tk.Tk, database_path: Path, sync_report: StartupSyncReport) -> None:
        self.root = root
        self.database_path = database_path
        self.connection = sqlite3.connect(database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.search_service = SearchService(self.connection)
        self.reader_service = ReaderService(self.connection)
        self.ollama_client = OllamaClient()
        self.answer_service = ArchiveAnswerService(self.search_service, self.ollama_client)

        self.root.title("Discord RP Archive")
        self.root.geometry("1080x720")
        self.root.minsize(760, 520)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        container = ttk.Frame(root, padding=16)
        container.pack(fill="both", expand=True)
        ttk.Label(container, text="Discord RP Archive", font=("Segoe UI", 20, "bold")).pack(anchor="w")
        ttk.Label(container, text=str(database_path), foreground="#58636d").pack(anchor="w", pady=(2, 14))

        self.pages = ttk.Notebook(container)
        self.pages.pack(fill="both", expand=True)
        self._build_home(sync_report)
        self._build_search()
        self._build_reader()
        self._build_ask()

    def _build_home(self, sync_report: StartupSyncReport) -> None:
        page = ttk.Frame(self.pages, padding=20)
        self.pages.add(page, text="Home")
        ttk.Label(page, text="Archive status", font=("Segoe UI", 15, "bold")).pack(anchor="w")

        status = self.connection.execute("PRAGMA quick_check").fetchone()[0]
        schema_version = self._scalar("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
        source_count = self._scalar("SELECT COUNT(*) FROM sources")
        message_count = self._scalar("SELECT COUNT(*) FROM messages")
        status_text = "Healthy" if status == "ok" else f"Check required: {status}"

        summary = ttk.Frame(page, padding=(0, 14, 0, 0))
        summary.pack(fill="x")
        self._metric(summary, "Database", status_text, 0)
        self._metric(summary, "Schema", str(schema_version), 1)
        self._metric(summary, "Sources", f"{source_count:,}", 2)
        self._metric(summary, "Messages", f"{message_count:,}", 3)

        ttk.Label(
            page,
            text="The raw archive remains unchanged. Search and Reader use the indexed SQLite archive.",
            wraplength=760,
        ).pack(anchor="w", pady=(22, 0))
        ttk.Label(
            page,
            text=(
                f"Startup sync: {sync_report.imported} imported, "
                f"{sync_report.unchanged} unchanged, {sync_report.failed} failed."
            ),
        ).pack(anchor="w", pady=(14, 0))
        issues = [item for item in sync_report.items if item.status == "failed" or item.warnings]
        if issues:
            details = "\n".join(
                f"{item.path.name}: {'; '.join(item.warnings) or item.detail}"
                for item in issues[:5]
            )
            if len(issues) > 5:
                details += f"\nAnd {len(issues) - 5} more item(s)."
            ttk.Label(page, text=details, foreground="#9a3f2b", wraplength=760).pack(anchor="w", pady=(8, 0))

    @staticmethod
    def _metric(parent: ttk.Frame, label: str, value: str, column: int) -> None:
        item = ttk.Frame(parent, padding=(12, 10))
        item.grid(row=0, column=column, sticky="nsew", padx=(0, 8))
        ttk.Label(item, text=label, foreground="#58636d").pack(anchor="w")
        ttk.Label(item, text=value, font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(4, 0))
        parent.columnconfigure(column, weight=1)

    def _build_search(self) -> None:
        page = ttk.Frame(self.pages, padding=12)
        self.pages.add(page, text="Search")

        controls = ttk.Frame(page)
        controls.pack(fill="x", pady=(0, 10))
        self.query = tk.StringVar()
        entry = ttk.Entry(controls, textvariable=self.query)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _event: self._search())
        ttk.Button(controls, text="Search", command=self._search).pack(side="left", padx=(8, 0))
        self.search_status = ttk.Label(page, text="Enter a phrase to search the archive.", foreground="#58636d")
        self.search_status.pack(anchor="w", pady=(0, 6))

        columns = ("timestamp", "channel", "author", "character", "content")
        self.results = ttk.Treeview(page, columns=columns, show="headings", selectmode="browse")
        for column, title, width in (
            ("timestamp", "Date", 155),
            ("channel", "Channel", 130),
            ("author", "Author", 140),
            ("character", "Character", 140),
            ("content", "Message", 440),
        ):
            self.results.heading(column, text=title)
            self.results.column(column, width=width, minwidth=80, stretch=column == "content")
        scrollbar = ttk.Scrollbar(page, orient="vertical", command=self.results.yview)
        self.results.configure(yscrollcommand=scrollbar.set)
        self.results.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.results.bind("<Double-1>", lambda _event: self._open_selected_result())

    def _build_reader(self) -> None:
        page = ttk.Frame(self.pages, padding=14)
        self.pages.add(page, text="Reader")
        self.reader_title = ttk.Label(page, text="Select a search result to read its channel context.", font=("Segoe UI", 12, "bold"))
        self.reader_title.pack(anchor="w", pady=(0, 10))
        self.reader_text = tk.Text(page, wrap="word", state="disabled", padx=12, pady=10)
        scrollbar = ttk.Scrollbar(page, orient="vertical", command=self.reader_text.yview)
        self.reader_text.configure(yscrollcommand=scrollbar.set)
        self.reader_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _build_ask(self) -> None:
        page = ttk.Frame(self.pages, padding=14)
        self.pages.add(page, text="Ask")

        model_row = ttk.Frame(page)
        model_row.pack(fill="x", pady=(0, 10))
        ttk.Label(model_row, text="Ollama model").pack(side="left")
        self.model_name = tk.StringVar(value=os.environ.get("OLLAMA_MODEL", ""))
        self.model_picker = ttk.Combobox(model_row, textvariable=self.model_name, state="normal", width=32)
        self.model_picker.pack(side="left", padx=(8, 8))
        self.model_refresh = ttk.Button(model_row, text="Refresh models", command=self._refresh_models)
        self.model_refresh.pack(side="left")

        ttk.Label(page, text="Question").pack(anchor="w")
        self.ask_question = tk.Text(page, height=4, wrap="word", padx=8, pady=6)
        self.ask_question.pack(fill="x", pady=(4, 8))
        self.ask_button = ttk.Button(page, text="Ask archive", command=self._ask_archive)
        self.ask_button.pack(anchor="w")
        self.ask_status = ttk.Label(page, text="Answers are generated only from retrieved archive messages.", foreground="#58636d")
        self.ask_status.pack(anchor="w", pady=(8, 6))

        ttk.Label(page, text="Answer", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.ask_answer = tk.Text(page, height=10, wrap="word", state="disabled", padx=10, pady=8)
        self.ask_answer.pack(fill="both", expand=True, pady=(4, 8))
        ttk.Label(page, text="Retrieved evidence", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.ask_evidence = tk.Text(page, height=8, wrap="word", state="disabled", padx=10, pady=8)
        self.ask_evidence.pack(fill="both", expand=True, pady=(4, 0))

    def _ask_archive(self) -> None:
        question = self.ask_question.get("1.0", "end").strip()
        if not question:
            self.ask_status.configure(text="Enter a question first.")
            return
        try:
            evidence = self.answer_service.retrieve(question)
        except sqlite3.Error as exc:
            self.ask_status.configure(text=f"Archive retrieval failed: {exc}")
            return

        self._set_text(self.ask_answer, "")
        if not evidence:
            self._set_text(self.ask_evidence, "")
            self._set_text(self.ask_answer, "No matching archive evidence was found; no model answer was generated.")
            self.ask_status.configure(text="No evidence matched this question.")
            return

        self._set_text(self.ask_evidence, self._format_evidence(evidence))
        self.ask_button.configure(state="disabled")
        self.ask_status.configure(text="Asking Ollama...")
        model = self.model_name.get().strip()
        self._run_background(
            lambda: self.ollama_client.generate(model, question, evidence),
            lambda answer: self._finish_answer(answer, len(evidence)),
            lambda error: self._finish_answer_error(error),
        )

    def _refresh_models(self) -> None:
        self.model_refresh.configure(state="disabled")
        self.ask_status.configure(text="Checking local Ollama models...")
        self._run_background(
            self.ollama_client.list_models,
            self._finish_model_refresh,
            lambda error: self._finish_model_refresh_error(error),
        )

    def _run_background(self, task: Any, on_success: Any, on_error: Any) -> None:
        events: Queue[tuple[str, Any]] = Queue()

        def run_task() -> None:
            try:
                events.put(("success", task()))
            except Exception as error:
                events.put(("error", error))

        def poll() -> None:
            try:
                event, result = events.get_nowait()
            except Empty:
                self.root.after(80, poll)
                return
            if event == "success":
                on_success(result)
            else:
                on_error(result)

        Thread(target=run_task, daemon=True).start()
        self.root.after(80, poll)

    def _finish_answer(self, answer: str, evidence_count: int) -> None:
        self._set_text(self.ask_answer, answer)
        self.ask_status.configure(text=f"Answer generated from {evidence_count} retrieved message(s).")
        self.ask_button.configure(state="normal")

    def _finish_answer_error(self, error: Exception) -> None:
        self.ask_status.configure(text=f"Ollama request failed: {error}")
        self.ask_button.configure(state="normal")

    def _finish_model_refresh(self, models: list[str]) -> None:
        self.model_picker.configure(values=models)
        self.ask_status.configure(text=f"Found {len(models)} local model(s); select one to continue.")
        self.model_refresh.configure(state="normal")

    def _finish_model_refresh_error(self, error: Exception) -> None:
        self.ask_status.configure(text=f"Could not list Ollama models: {error}")
        self.model_refresh.configure(state="normal")

    @staticmethod
    def _set_text(widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    @classmethod
    def _format_evidence(cls, evidence: list[dict[str, Any]]) -> str:
        blocks = []
        for index, item in enumerate(evidence, start=1):
            author = cls._author(item)
            header = " | ".join(
                value for value in (item.get("timestamp_utc"), item.get("channel_name"), author) if value
            )
            blocks.append(
                f"[E{index}] {header}\nSource: {item.get('source_path') or 'Unknown source'}\n"
                f"{item.get('content_original') or ''}"
            )
        return "\n\n".join(blocks)

    def _search(self) -> None:
        query = self.query.get().strip()
        self.results.delete(*self.results.get_children())
        try:
            matches = self.search_service.search(query)
        except sqlite3.Error as exc:
            messagebox.showerror("Search failed", str(exc), parent=self.root)
            return

        for match in matches:
            self.results.insert(
                "",
                "end",
                iid=str(match["message_observation_id"]),
                values=(
                    match["timestamp_utc"] or "",
                    match["channel_name"] or "",
                    self._author(match),
                    match["character_name"] or "",
                    self._preview(match["content_original"]),
                ),
            )
        self.search_status.configure(text=f"{len(matches)} result(s). Double-click a message to open its context.")

    def _open_selected_result(self) -> None:
        selected = self.results.selection()
        if not selected:
            return
        context = self.reader_service.get_message_context(int(selected[0]))
        if context is None:
            self.reader_title.configure(text="This message is no longer available in the current archive.")
            return

        message = context["message"]
        channel = message["channel_name"] or "Unknown channel"
        self.reader_title.configure(text=f"{message['timestamp_utc'] or 'Undated'}  |  {channel}")
        entries = context["before"] + [message] + context["after"]
        lines = [self._format_message(entry, entry is message) for entry in entries]
        self.reader_text.configure(state="normal")
        self.reader_text.delete("1.0", "end")
        self.reader_text.insert("1.0", "\n\n".join(lines))
        self.reader_text.configure(state="disabled")
        self.pages.select(2)

    @staticmethod
    def _author(message: dict[str, Any]) -> str:
        return (
            message["author_display_name_original"]
            or message["author_nickname_original"]
            or message["author_discord_name_original"]
            or "Unknown author"
        )

    @staticmethod
    def _preview(content: str | None) -> str:
        return " ".join((content or "").split())

    @classmethod
    def _format_message(cls, message: dict[str, Any], selected: bool) -> str:
        marker = "  [selected]" if selected else ""
        character = f" as {message['character_name']}" if message["character_name"] else ""
        header = f"{message['timestamp_utc'] or 'Undated'}  |  {cls._author(message)}{character}{marker}"
        return f"{header}\n{message['content_original'] or ''}"

    def _scalar(self, sql: str) -> int:
        try:
            return int(self.connection.execute(sql).fetchone()[0])
        except sqlite3.OperationalError:
            return 0

    def close(self) -> None:
        self.connection.close()
        self.root.destroy()


def _clear_root(root: tk.Tk) -> None:
    for child in root.winfo_children():
        child.destroy()


def _show_startup(root: tk.Tk, database_path: Path, raw_directory: Path) -> None:
    if not raw_directory.is_dir():
        selected = filedialog.askdirectory(title="Locate the raw archive directory", parent=root)
        if not selected:
            root.destroy()
            return
        raw_directory = Path(selected).resolve()

    root.title("Discord RP Archive | Startup")
    root.geometry("620x190")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=22)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Preparing archive", font=("Segoe UI", 16, "bold")).pack(anchor="w")
    status = tk.StringVar(value=f"Locating source files in {raw_directory}")
    ttk.Label(frame, textvariable=status, wraplength=560).pack(anchor="w", pady=(12, 8))
    progress_bar = ttk.Progressbar(frame, mode="determinate", maximum=1)
    progress_bar.pack(fill="x")

    events: Queue[tuple[str, Any]] = Queue()

    def synchronize() -> None:
        try:
            connection = sqlite3.connect(database_path)
            try:
                report = synchronize_archive(
                    connection,
                    raw_directory,
                    progress=lambda current, total, path, stage: events.put(
                        ("progress", (current, total, path, stage))
                    ),
                )
            finally:
                connection.close()
        except Exception as exc:
            events.put(("error", exc))
        else:
            events.put(("complete", report))

    def poll_events() -> None:
        try:
            while True:
                event, payload = events.get_nowait()
                if event == "progress":
                    current, total, path, stage = payload
                    progress_bar.configure(maximum=max(total, 1), value=current)
                    status.set(f"{stage}: {path}")
                elif event == "error":
                    messagebox.showerror("Startup synchronization failed", str(payload), parent=root)
                    root.destroy()
                    return
                elif event == "complete":
                    _clear_root(root)
                    root.geometry("1080x720")
                    root.resizable(True, True)
                    ArchiveApp(root, database_path, payload)
                    return
        except Empty:
            pass
        root.after(80, poll_events)

    Thread(target=synchronize, daemon=True).start()
    root.after(80, poll_events)


def run(database_path: Path, raw_directory: Path) -> int:
    try:
        root = tk.Tk()
        _show_startup(root, database_path, raw_directory)
    except (sqlite3.Error, tk.TclError) as exc:
        messagebox.showerror("Unable to open archive", str(exc))
        return 1
    root.mainloop()
    return 0