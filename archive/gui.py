from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tkinter as tk
from queue import Empty, Queue
from pathlib import Path
from threading import Thread
from tkinter import filedialog, messagebox, ttk
from typing import Any

from archive.reader_service import ReaderService
from archive.ollama_service import ArchiveAnswerService, OllamaClient, OllamaRequestDetails
from archive.search_service import SearchService
from archive.sync_service import StartupSyncReport, synchronize_archive


THEME_PALETTES = {
    "light": {
        "background": "#f4f6f8",
        "surface": "#ffffff",
        "foreground": "#202830",
        "muted": "#58636d",
        "border": "#c7d0d8",
        "field": "#ffffff",
        "selection": "#cfe3f5",
        "selection_foreground": "#17212b",
        "warning": "#9a3f2b",
    },
    "dark": {
        "background": "#1e2228",
        "surface": "#282e36",
        "foreground": "#e6e9ed",
        "muted": "#aab4bf",
        "border": "#414a55",
        "field": "#20252b",
        "selection": "#355779",
        "selection_foreground": "#ffffff",
        "warning": "#ff9c82",
    },
}
THEME_OPTIONS = ("System", "Light", "Dark")


def _theme_config_path() -> Path:
    if sys.platform == "win32":
        config_directory = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return config_directory / "Discord RP Archive" / "settings.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Discord RP Archive" / "settings.json"
    config_directory = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_directory / "discord-rp-archive" / "settings.json"


def _detect_system_theme() -> str | None:
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                use_light_theme, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        except FileNotFoundError:
            return None
        return "light" if use_light_theme else "dark"

    if sys.platform == "darwin":
        result = subprocess.run(
            ["defaults", "read", "-g", "AppleInterfaceStyle"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        if result.returncode != 0:
            return "light"
        return "dark" if result.stdout.strip().lower() == "dark" else "light"

    try:
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except FileNotFoundError:
        result = None
    if result is not None and result.returncode == 0:
        preference = result.stdout.strip().strip("'\"").lower()
        if preference in {"prefer-dark", "prefer-light"}:
            return "dark" if preference == "prefer-dark" else "light"
        if preference == "default":
            return "light"

    gtk_theme = os.environ.get("GTK_THEME", "").lower()
    if gtk_theme:
        return "dark" if "dark" in gtk_theme else "light"
    return None


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

        self.style = ttk.Style(self.root)
        self.style.theme_use("clam")
        self.theme_preference = self._load_theme_preference()
        self._system_theme: str | None = None

        container = ttk.Frame(root, padding=16)
        container.pack(fill="both", expand=True)
        heading = ttk.Frame(container)
        heading.pack(fill="x")
        ttk.Label(heading, text="Discord RP Archive", font=("Segoe UI", 20, "bold")).pack(side="left", anchor="w")
        theme_controls = ttk.Frame(heading)
        theme_controls.pack(side="right", anchor="e")
        ttk.Label(theme_controls, text="Theme").pack(side="left", padx=(0, 6))
        self.theme_choice = tk.StringVar(value=self.theme_preference)
        self.theme_picker = ttk.Combobox(
            theme_controls,
            textvariable=self.theme_choice,
            values=THEME_OPTIONS,
            state="readonly",
            width=9,
        )
        self.theme_picker.pack(side="left")
        self.theme_picker.bind("<<ComboboxSelected>>", self._change_theme)
        self.theme_status = ttk.Label(theme_controls, style="Muted.TLabel")
        self.theme_status.pack(side="left", padx=(8, 0))
        ttk.Label(container, text=str(database_path), style="Muted.TLabel").pack(anchor="w", pady=(2, 14))

        self.pages = ttk.Notebook(container)
        self.pages.pack(fill="both", expand=True)
        self._build_home(sync_report)
        self._build_search()
        self._build_reader()
        self._build_ask()
        self._apply_theme()
        self._poll_system_theme()

    def _load_theme_preference(self) -> str:
        try:
            settings = json.loads(_theme_config_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "System"
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            messagebox.showwarning(
                "Theme settings unavailable",
                f"Could not read saved theme settings; using System theme.\n\n{error}",
                parent=self.root,
            )
            return "System"
        preference = settings.get("theme", "System") if isinstance(settings, dict) else None
        if preference in THEME_OPTIONS:
            return preference
        messagebox.showwarning(
            "Theme settings unavailable",
            "The saved theme setting is invalid; using System theme.",
            parent=self.root,
        )
        return "System"

    def _change_theme(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        preference = self.theme_choice.get()
        if preference not in THEME_OPTIONS:
            return
        try:
            path = _theme_config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            settings: dict[str, Any] = {}
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    settings = existing
            except FileNotFoundError:
                pass
            settings["theme"] = preference
            path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            messagebox.showerror(
                "Could not save theme",
                f"The theme was changed for this session, but could not be saved.\n\n{error}",
                parent=self.root,
            )
        self.theme_preference = preference
        self._apply_theme()

    def _poll_system_theme(self) -> None:
        if self.theme_preference == "System":
            try:
                detected_theme = _detect_system_theme()
            except (OSError, subprocess.SubprocessError) as error:
                detected_theme = None
                self._system_theme = None
                self.theme_status.configure(text=f"System preference unavailable: {error}")
            else:
                self._system_theme = detected_theme
                if detected_theme is None:
                    self.theme_status.configure(text="System preference unavailable")
                else:
                    self.theme_status.configure(text=f"System: {detected_theme.title()}")
            effective_theme = detected_theme or "light"
            if effective_theme != getattr(self, "_applied_theme", None):
                self._apply_theme()
        else:
            self.theme_status.configure(text="")
        self.root.after(3000, self._poll_system_theme)

    def _apply_theme(self) -> None:
        if self.theme_preference == "System":
            theme = self._system_theme or "light"
        else:
            theme = self.theme_preference.lower()
        colors = THEME_PALETTES[theme]
        self._applied_theme = theme
        self.root.configure(background=colors["background"])
        self.style.configure(".", background=colors["background"], foreground=colors["foreground"])
        self.style.configure("TFrame", background=colors["background"])
        self.style.configure("TLabel", background=colors["background"], foreground=colors["foreground"])
        self.style.configure("Muted.TLabel", foreground=colors["muted"])
        self.style.configure("Warning.TLabel", foreground=colors["warning"])
        self.style.configure("TButton", background=colors["surface"], foreground=colors["foreground"])
        self.style.map(
            "TButton",
            background=[("active", colors["border"]), ("disabled", colors["background"])],
            foreground=[("disabled", colors["muted"])],
        )
        self.style.configure(
            "TEntry",
            fieldbackground=colors["field"],
            foreground=colors["foreground"],
            insertcolor=colors["foreground"],
        )
        self.style.configure(
            "TCombobox",
            fieldbackground=colors["field"],
            background=colors["surface"],
            foreground=colors["foreground"],
            arrowcolor=colors["foreground"],
        )
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", colors["field"])],
            foreground=[("readonly", colors["foreground"])],
            selectbackground=[("readonly", colors["selection"])],
            selectforeground=[("readonly", colors["selection_foreground"])],
        )
        self.style.configure(
            "TNotebook",
            background=colors["background"],
            bordercolor=colors["border"],
            tabmargins=(2, 4, 2, 0),
        )
        self.style.configure(
            "TNotebook.Tab",
            background=colors["surface"],
            foreground=colors["muted"],
            padding=(10, 5),
        )
        self.style.map(
            "TNotebook.Tab",
            background=[("selected", colors["background"]), ("active", colors["border"])],
            foreground=[("selected", colors["foreground"])],
        )
        self.style.configure(
            "Treeview",
            background=colors["field"],
            fieldbackground=colors["field"],
            foreground=colors["foreground"],
            rowheight=24,
        )
        self.style.map(
            "Treeview",
            background=[("selected", colors["selection"])],
            foreground=[("selected", colors["selection_foreground"])],
        )
        self.style.configure(
            "Treeview.Heading",
            background=colors["surface"],
            foreground=colors["foreground"],
        )
        self.style.configure(
            "TScrollbar",
            background=colors["surface"],
            troughcolor=colors["background"],
            bordercolor=colors["border"],
            arrowcolor=colors["foreground"],
        )
        self.style.configure(
            "Horizontal.TProgressbar",
            background=colors["selection"],
            troughcolor=colors["surface"],
            bordercolor=colors["border"],
        )
        for widget in self._walk_widgets(self.root):
            if isinstance(widget, tk.Text):
                widget.configure(
                    background=colors["field"],
                    foreground=colors["foreground"],
                    insertbackground=colors["foreground"],
                    selectbackground=colors["selection"],
                    selectforeground=colors["selection_foreground"],
                )

    @staticmethod
    def _walk_widgets(parent: tk.Misc) -> list[tk.Misc]:
        widgets: list[tk.Misc] = []
        for child in parent.winfo_children():
            widgets.append(child)
            widgets.extend(ArchiveApp._walk_widgets(child))
        return widgets

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
            ttk.Label(page, text=details, style="Warning.TLabel", wraplength=760).pack(anchor="w", pady=(8, 0))

    @staticmethod
    def _metric(parent: ttk.Frame, label: str, value: str, column: int) -> None:
        item = ttk.Frame(parent, padding=(12, 10))
        item.grid(row=0, column=column, sticky="nsew", padx=(0, 8))
        ttk.Label(item, text=label, style="Muted.TLabel").pack(anchor="w")
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
        self.search_status = ttk.Label(page, text="Enter a phrase to search the archive.", style="Muted.TLabel")
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
        ask_actions = ttk.Frame(page)
        ask_actions.pack(fill="x")
        self.ask_button = ttk.Button(ask_actions, text="Ask archive", command=self._ask_archive)
        self.ask_button.pack(side="left")
        self.export_answer_button = ttk.Button(
            ask_actions, text="Export test", command=self._export_answer, state="disabled"
        )
        self.export_answer_button.pack(side="left", padx=(8, 0))
        self.last_answer_export: str | None = None
        self.ask_status = ttk.Label(page, text="Answers are generated only from retrieved archive messages.", style="Muted.TLabel")
        self.ask_status.pack(anchor="w", pady=(8, 6))

        self.ask_result_pages = ttk.Notebook(page)
        self.ask_result_pages.pack(fill="both", expand=True, pady=(4, 0))
        response_page = ttk.Frame(self.ask_result_pages, padding=8)
        self.ask_result_pages.add(response_page, text="Answer")
        ttk.Label(response_page, text="Model answer", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.ask_answer = tk.Text(response_page, height=8, wrap="word", state="disabled", padx=10, pady=8)
        self.ask_answer.pack(fill="both", expand=True, pady=(4, 8))
        ttk.Label(response_page, text="Retrieved evidence", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.ask_evidence = tk.Text(response_page, height=7, wrap="word", state="disabled", padx=10, pady=8)
        self.ask_evidence.pack(fill="both", expand=True, pady=(4, 0))

        diagnostics_page = ttk.Frame(self.ask_result_pages, padding=8)
        self.ask_result_pages.add(diagnostics_page, text="LLM Diagnostics")
        self.ask_diagnostics = tk.Text(diagnostics_page, wrap="word", state="disabled", padx=10, pady=8)
        diagnostics_scrollbar = ttk.Scrollbar(diagnostics_page, orient="vertical", command=self.ask_diagnostics.yview)
        self.ask_diagnostics.configure(yscrollcommand=diagnostics_scrollbar.set)
        self.ask_diagnostics.pack(side="left", fill="both", expand=True)
        diagnostics_scrollbar.pack(side="right", fill="y")

    def _ask_archive(self) -> None:
        question = self.ask_question.get("1.0", "end").strip()
        if not question:
            self.ask_status.configure(text="Enter a question first.")
            return
        self.last_answer_export = None
        self.export_answer_button.configure(state="disabled")
        try:
            evidence = self.answer_service.retrieve(question)
        except sqlite3.Error as exc:
            self.ask_status.configure(text=f"Archive retrieval failed: {exc}")
            return

        self._set_text(self.ask_answer, "")
        if not evidence:
            self._set_text(self.ask_evidence, "")
            self._set_text(self.ask_diagnostics, "")
            self._set_text(self.ask_answer, "No matching archive evidence was found; no model answer was generated.")
            self.ask_status.configure(text="No evidence matched this question.")
            return

        self._set_text(self.ask_evidence, self._format_evidence(evidence))
        self.ask_button.configure(state="disabled")
        self.ask_status.configure(text="Asking Ollama...")
        model = self.model_name.get().strip()
        self._run_background(
            lambda: self.ollama_client.generate_with_details(model, question, evidence),
            lambda result: self._finish_answer(result[0], question, evidence, result[1]),
            lambda error: self._finish_answer_error(error),
        )

    def _export_answer(self) -> None:
        if self.last_answer_export is None:
            return
        filename = filedialog.asksaveasfilename(
            title="Export model test",
            parent=self.root,
            defaultextension=".txt",
            filetypes=(("Text files", "*.txt"), ("All files", "*.*")),
            initialfile="archive-model-test.txt",
        )
        if not filename:
            return
        try:
            Path(filename).write_text(self.last_answer_export, encoding="utf-8")
        except OSError as error:
            messagebox.showerror("Export failed", str(error), parent=self.root)

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

    def _finish_answer(
        self,
        answer: str,
        question: str,
        evidence: list[dict[str, Any]],
        details: OllamaRequestDetails,
    ) -> None:
        self._set_text(self.ask_answer, answer)
        report = self._format_test_export(question, answer, evidence, details)
        self._set_text(self.ask_diagnostics, report)
        self.last_answer_export = report
        self.export_answer_button.configure(state="normal")
        self.ask_status.configure(text=f"Answer generated from {len(evidence)} retrieved message(s).")
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

    @staticmethod
    def _format_test_export(
        question: str,
        answer: str,
        evidence: list[dict[str, Any]],
        details: OllamaRequestDetails,
    ) -> str:
        retrieval = []
        for index, item in enumerate(evidence, start=1):
            retrieval.append(
                f"Result #{index}\n"
                f"Source: {item.get('source_path') or 'Unknown source'}\n"
                f"Date: {item.get('timestamp_utc') or 'Unknown date'}\n"
                f"Score: {item.get('relevance_score', 'N/A')}"
            )
        settings = "\n".join(f"{name}: {value}" for name, value in details.settings.items())
        retrieval_text = "\n\n".join(retrieval) or "No results"
        return (
            f"USER QUESTION\n----------------------------\n{question}\n\n"
            f"RETRIEVAL\n----------------------------\n{retrieval_text}\n\n"
            f"LLM CONTEXT\n----------------------------\n{details.user_context}\n\n"
            f"SYSTEM INSTRUCTIONS\n----------------------------\n{details.system_instructions}\n\n"
            f"MODEL\n----------------------------\n{details.model}\n\n"
            f"SETTINGS\n----------------------------\n{settings}\n\n"
            f"ANSWER\n----------------------------\n{answer}\n"
        )

    @classmethod
    def _format_evidence(cls, evidence: list[dict[str, Any]]) -> str:
        blocks = []
        for index, item in enumerate(evidence, start=1):
            author = cls._author(item)
            relation = item.get("context_relation")
            label = f" [{relation}]" if relation else ""
            header = " | ".join(
                value for value in (item.get("timestamp_utc"), item.get("channel_name"), author) if value
            )
            blocks.append(
                f"[E{index}]{label} {header}\nSource: {item.get('source_path') or 'Unknown source'}\n"
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