"""Tovo Video Downloader - GUI entry point and download orchestration."""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional, Sequence, Set
from concurrent.futures import ThreadPoolExecutor

from config import Config
from dependencies import find_missing_tools, install_all
import requests
from subtitles import WhisperAligner, GroqTranscriber, generate_standard_srt
from widgets import DownloadManager, RoundedButton, RoundedEntry, ModernCheckbutton, RoundedFrame

logger = logging.getLogger(__name__)

BASE_PATH = Path(__file__).resolve().parent
URL_PREFIXES = ("http://", "https://")
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DOWNLOAD_FILE_EXTENSIONS = (".mp4", ".srt", ".txt", ".mp3", ".wav", ".m4a")
PROCESS_TERMINATE_TIMEOUT = 5  # seconds before SIGKILL fallback


def sanitize_filename(name: str) -> str:
    """Strip path separators and unsafe characters from a video title."""
    cleaned = INVALID_FILENAME_CHARS.sub("_", name).strip(" .")
    cleaned = Path(cleaned).name  # drop any directory components
    return cleaned or "untitled"


def parse_titles_and_links(text: str):
    """Utility for transcription tab to parse Title + Link pairs."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    titles: List[str] = []
    links: List[str] = []
    
    # Simple logic: line 1 title, line 2 link, etc.
    # If a line is a URL, assume the previous line was the title.
    for i, line in enumerate(lines):
        if line.lower().startswith(URL_PREFIXES) and i > 0:
            titles.append(lines[i-1])
            links.append(line)
            
    return titles, links


class AppleStyleApp:
    """Main GUI window: input pane, options, log, and the download worker."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Video Downloader")
        self.root.geometry("800x800")
        self.root.minsize(700, 700)

        self.config = Config(BASE_PATH / "config.json")
        if not self.config.get("downloads_dir"):
            self.config.set("downloads_dir", str(BASE_PATH / "Downloads"))
        self.downloads_dir = Path(self.config.get("downloads_dir"))
        self.dub_dir = self.config.get("dub_dir", "")
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

        # Apple-like palette
        self.bg_color = "#F5F5F7"
        self.accent_color = "#5E5CE6"
        self.accent_hover = "#4B49B8"
        self.text_color = "#1D1D1F"
        self.gray_bg = "#E5E5EA"
        self.gray_hover = "#D1D1D6"
        self.font_family = "Segoe UI" if os.name == "nt" else "Helvetica"

        self._state_lock = threading.Lock()
        self.downloading = False
        self.cancelled = False
        self.active_processes: Dict[int, subprocess.Popen] = {}
        self.batch_errors: List[str] = []
        self.errors_lock = threading.Lock()

        self.root.configure(bg=self.bg_color)
        self._build_ui()
        self.download_manager: Optional[DownloadManager] = None
        self.cancelled_indices: set[int] = set()

        self.root.after(500, self.check_dependencies)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        # Main container with some padding
        container = tk.Frame(self.root, bg=self.bg_color)
        container.grid(row=0, column=0, sticky="nsew", padx=25, pady=10)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(1, weight=1) # Tab content area

        # Custom Tab Switcher Row
        tab_frame = tk.Frame(container, bg=self.bg_color)
        tab_frame.grid(row=0, column=0, sticky="ew", pady=(0, 20))
        
        self.btn_dl_tab = RoundedButton(
            tab_frame, text="Downloads", command=lambda: self._switch_tab("dl"),
            radius=20, bg_color="white", hover_color=self.gray_hover,
            text_color=self.accent_color, font=(self.font_family, 11, "bold"),
            width=150, height=45
        )
        self.btn_dl_tab.pack(side=tk.LEFT, padx=(0, 10))
        
        self.btn_trans_tab = RoundedButton(
            tab_frame, text="Transcription", command=lambda: self._switch_tab("trans"),
            radius=20, bg_color=self.bg_color, hover_color=self.gray_hover,
            text_color="#86868B", font=(self.font_family, 11, "bold"),
            width=150, height=45
        )
        self.btn_trans_tab.pack(side=tk.LEFT)

        # Tab Content Area
        self.content_frame = tk.Frame(container, bg=self.bg_color)
        self.content_frame.grid(row=1, column=0, sticky="nsew")
        self.content_frame.grid_columnconfigure(0, weight=1)
        self.content_frame.grid_rowconfigure(0, weight=1)

        # Tab 1: Downloads
        self.dl_tab = tk.Frame(self.content_frame, bg=self.bg_color)
        self.dl_tab.grid(row=0, column=0, sticky="nsew")
        self._build_dl_tab(self.dl_tab)

        # Tab 2: Transcription
        self.trans_tab = tk.Frame(self.content_frame, bg=self.bg_color)
        self.trans_tab.grid(row=0, column=0, sticky="nsew")
        self._build_trans_tab(self.trans_tab)
        
        # Initial State
        self._switch_tab("dl")

        # Shared Log Area (at the bottom of container)
        self._build_log_area(container)

    def _switch_tab(self, tab: str) -> None:
        if tab == "dl":
            self.dl_tab.tkraise()
            self.btn_dl_tab.config_state("normal", bg="white")
            self.btn_dl_tab.text_color = self.accent_color
            self.btn_dl_tab._draw()
            
            self.btn_trans_tab.config_state("normal", bg=self.bg_color)
            self.btn_trans_tab.text_color = "#86868B"
            self.btn_trans_tab._draw()
        else:
            self.trans_tab.tkraise()
            self.btn_trans_tab.config_state("normal", bg="white")
            self.btn_trans_tab.text_color = self.accent_color
            self.btn_trans_tab._draw()
            
            self.btn_dl_tab.config_state("normal", bg=self.bg_color)
            self.btn_dl_tab.text_color = "#86868B"
            self.btn_dl_tab._draw()

    def _build_dl_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)

        tk.Label(
            parent, text="Batch Video Downloader", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 22, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(20, 2), padx=10)

        tk.Label(
            parent, text="Paste Title on line 1, Link on line 2, etc.",
            bg=self.bg_color, fg="#86868B", font=(self.font_family, 10),
        ).grid(row=1, column=0, sticky="w", pady=(0, 8), padx=10)

        # Text area with proper border via frame highlight
        border = tk.Frame(parent, bg="#D2D2D7")
        border.grid(row=2, column=0, sticky="nsew", pady=(0, 12), padx=10)
        border.grid_columnconfigure(0, weight=1)
        border.grid_rowconfigure(0, weight=1)

        inner = tk.Frame(border, bg="white")
        inner.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        inner.grid_columnconfigure(0, weight=1)
        inner.grid_rowconfigure(0, weight=1)

        self.dl_input_text = tk.Text(
            inner, wrap=tk.WORD, font=(self.font_family, 11),
            bg="white", fg=self.text_color, insertbackground=self.accent_color,
            relief=tk.FLAT, padx=10, pady=10,
        )
        self.dl_input_text.grid(row=0, column=0, sticky="nsew")

        scroll = tk.Scrollbar(inner, command=self.dl_input_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.dl_input_text.config(yscrollcommand=scroll.set)
        self._bind_context_menu(self.dl_input_text)
        self.dl_input_frame = border  # keep reference for grid_remove

        self._build_save_row(parent, row=3)
        self._build_dl_options_row(parent, row=4)
        self._build_dl_concurrent_row(parent, row=5)
        self._build_dub_row(parent, row=6)
        self._build_advanced_row(parent, row=7)
        self._build_dl_button_row(parent, row=8)

    def _build_trans_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)

        tk.Label(
            parent, text="Batch Transcription", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 22, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(20, 2), padx=10)

        tk.Label(
            parent, text="Paste Title on line 1, Link on line 2, etc. Output: Title + Link + Text",
            bg=self.bg_color, fg="#86868B", font=(self.font_family, 10),
        ).grid(row=1, column=0, sticky="w", pady=(0, 8), padx=10)

        border = tk.Frame(parent, bg="#D2D2D7")
        border.grid(row=2, column=0, sticky="nsew", pady=(0, 12), padx=10)
        border.grid_columnconfigure(0, weight=1)
        border.grid_rowconfigure(0, weight=1)

        inner = tk.Frame(border, bg="white")
        inner.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        inner.grid_columnconfigure(0, weight=1)
        inner.grid_rowconfigure(0, weight=1)

        self.trans_input_text = tk.Text(
            inner, wrap=tk.WORD, font=(self.font_family, 11),
            bg="white", fg=self.text_color, insertbackground=self.accent_color,
            relief=tk.FLAT, padx=10, pady=10,
        )
        self.trans_input_text.grid(row=0, column=0, sticky="nsew")

        scroll = tk.Scrollbar(inner, command=self.trans_input_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.trans_input_text.config(yscrollcommand=scroll.set)
        self._bind_context_menu(self.trans_input_text)
        self.trans_border = border # Reference to hide it later

        # Provider row
        prov_frame = tk.Frame(parent, bg=self.bg_color)
        prov_frame.grid(row=3, column=0, sticky="ew", pady=(0, 12), padx=10)
        self._build_transcription_settings(prov_frame)

        self._build_trans_button_row(parent, row=4)

    def _bind_context_menu(self, widget: tk.Text) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Cut", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="Copy", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="Paste", command=lambda: widget.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="Select All", command=lambda: widget.tag_add(tk.SEL, "1.0", tk.END))
        
        def show_menu(event):
            menu.tk_popup(event.x_root, event.y_root)
        widget.bind("<Button-3>", show_menu)

    def _truncate(self, text: str, width: int, font: Tuple) -> str:
        """Truncate text to fit a pixel width using ellipsis."""
        import tkinter.font as tkfont
        f = tkfont.Font(family=font[0], size=font[1])
        if f.measure(text) <= width:
            return text
        
        for i in range(len(text), 0, -1):
            if f.measure(text[:i] + "...") <= width:
                return text[:i] + "..."
        return "..."


    def _build_save_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 10), padx=10)
        frame.grid_columnconfigure(1, weight=1)

        tk.Label(
            frame, text="Save to:", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))

        self.dir_label = tk.Label(
            frame, text=str(self.downloads_dir), bg=self.bg_color, fg="#5E5CE6",
            font=(self.font_family, 10), anchor="w",
        )
        self.dir_label.grid(row=0, column=1, sticky="ew")
        # Truncate with ellipsis when too narrow
        self.dir_label.bind("<Configure>", lambda e: self.dir_label.config(
            text=self._truncate(str(self.downloads_dir), e.width, (self.font_family, 10))
        ))

        self.browse_btn = RoundedButton(
            frame, text="Browse…", command=self.browse_directory,
            radius=14, bg_color=self.gray_bg, hover_color=self.gray_hover,
            text_color=self.text_color, font=(self.font_family, 10, "bold"),
            width=90, height=32,
        )
        self.browse_btn.grid(row=0, column=2, sticky="e", padx=(6, 0))

    def _build_dl_options_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 10), padx=10)

        tk.Label(
            frame, text="Subtitle Sync Mode:", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 10, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 8))

        self.sync_mode_var = tk.StringVar(value="None (2-second intervals)")
        ttk.Combobox(
            frame, textvariable=self.sync_mode_var,
            values=["None (2-second intervals)", "Whisper AI (Smart Sync)"],
            state="readonly", font=(self.font_family, 10), width=22,
        ).pack(side=tk.LEFT)

    def _build_dl_concurrent_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 10), padx=10)

        self.concurrent_var = tk.BooleanVar(value=self.config.get("concurrent_downloads", False))
        ModernCheckbutton(
            frame, text="Simultaneous Downloads",
            variable=self.concurrent_var, bg_color=self.bg_color,
            command=self._save_config,
        ).pack(side=tk.LEFT)

        self.max_concurrent_var = tk.IntVar(value=self.config.get("max_concurrent", 5))
        tk.Label(frame, text="Max:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10)).pack(side=tk.LEFT, padx=(15, 4))
        self.max_concurrent_spin = tk.Spinbox(
            frame, from_=1, to=10, textvariable=self.max_concurrent_var,
            width=3, font=(self.font_family, 10), command=self._save_config,
        )
        self.max_concurrent_spin.pack(side=tk.LEFT)

    def _build_dub_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 10), padx=10)
        frame.grid_columnconfigure(1, weight=1)

        tk.Label(
            frame, text="(Optional) Dub Folder:", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))

        dub_label_text = self.dub_dir if self.dub_dir else "Not Selected"
        self.dub_dir_label = tk.Label(
            frame, text=dub_label_text, bg=self.bg_color, fg="#5E5CE6",
            font=(self.font_family, 10), anchor="w",
        )
        self.dub_dir_label.grid(row=0, column=1, sticky="ew")
        self.dub_dir_label.bind("<Configure>", lambda e: self.dub_dir_label.config(
            text=self._truncate(self.dub_dir or "Not Selected", e.width, (self.font_family, 10))
        ))

        self.dub_browse_btn = RoundedButton(
            frame, text="Browse…", command=self.browse_dub_directory,
            radius=14, bg_color=self.gray_bg, hover_color=self.gray_hover,
            text_color=self.text_color, font=(self.font_family, 10, "bold"),
            width=90, height=32,
        )
        self.dub_browse_btn.grid(row=0, column=2, sticky="e", padx=(6, 0))


    def _build_advanced_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 10), padx=10)

        self.use_browser_cookies = tk.BooleanVar(value=self.config.get("use_browser_cookies", False))
        ModernCheckbutton(
            frame, text="Use Chrome Cookies (as fallback)",
            variable=self.use_browser_cookies, bg_color=self.bg_color,
            command=self._save_config,
        ).pack(side=tk.LEFT)

        RoundedButton(
            frame, text="Clear Cookies", command=self.clear_local_cookies,
            radius=12, bg_color=self.gray_bg, hover_color=self.gray_hover,
            text_color=self.text_color, font=(self.font_family, 9), width=110, height=30,
        ).pack(side=tk.RIGHT)

    def _build_transcription_settings(self, parent: tk.Frame) -> None:
        tk.Label(
            parent, text="Transcription Provider:", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 11, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 10))

        self.trans_provider_var = tk.StringVar(value=self.config.get("transcription_provider", "Local Whisper"))
        ttk.Combobox(
            parent, textvariable=self.trans_provider_var,
            values=["Local Whisper", "Groq AI (Fastest)"],
            state="readonly", font=(self.font_family, 10), width=20,
        ).pack(side=tk.LEFT)

        tk.Label(
            parent, text="Groq Key:", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 10),
        ).pack(side=tk.LEFT, padx=(15, 5))

        self.groq_key_var = tk.StringVar(value=self.config.get("groq_api_key", ""))
        self.groq_key_entry = RoundedEntry(
            parent, variable=self.groq_key_var, show="*",
            width=180, radius=15, bg_color="white",
        )
        self.groq_key_entry.pack(side=tk.LEFT)
        self.groq_key_entry.entry.bind("<FocusOut>", lambda e: self._save_config())

        # Concurrency for transcription
        self.trans_concurrent_var = tk.BooleanVar(value=self.config.get("trans_concurrent", False))
        ModernCheckbutton(
            parent, text="Simultaneous Transcriptions",
            variable=self.trans_concurrent_var, bg_color=self.bg_color,
            command=self._save_config,
        ).pack(side=tk.LEFT, padx=(20, 0))

        self.trans_max_concurrent_var = tk.IntVar(value=self.config.get("trans_max_concurrent", 5))
        tk.Label(parent, text="Max:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10)).pack(side=tk.LEFT, padx=(10, 4))
        self.trans_max_concurrent_spin = tk.Spinbox(
            parent, from_=1, to=20, textvariable=self.trans_max_concurrent_var,
            width=3, font=(self.font_family, 10), command=self._save_config,
        )
        self.trans_max_concurrent_spin.pack(side=tk.LEFT)

    def _build_dl_button_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(8, 16), padx=10)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)

        self.download_btn = RoundedButton(
            frame, text="⬇  Start Batch Download", command=self.start_download,
            radius=22, bg_color=self.accent_color, hover_color=self.accent_hover,
            text_color="white", font=(self.font_family, 12, "bold"), height=46,
        )
        self.download_btn.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.cancel_btn = RoundedButton(
            frame, text="Cancel", command=self.cancel_download,
            radius=22, bg_color="#FF3B30", hover_color="#D70A01",
            text_color="white", font=(self.font_family, 12, "bold"), height=46,
        )
        self.cancel_btn.grid(row=0, column=1, sticky="ew")
        self.cancel_btn.config_state("disabled", bg="#E5E5EA")

    def _build_trans_button_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(8, 16), padx=10)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)

        self.trans_btn = RoundedButton(
            frame, text="🎙  Start Transcription", command=self.start_transcription,
            radius=22, bg_color=self.accent_color, hover_color=self.accent_hover,
            text_color="white", font=(self.font_family, 12, "bold"), height=46,
        )
        self.trans_btn.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.trans_cancel_btn = RoundedButton(
            frame, text="Cancel", command=self.cancel_download,
            radius=22, bg_color="#FF3B30", hover_color="#D70A01",
            text_color="white", font=(self.font_family, 12, "bold"), height=46,
        )
        self.trans_cancel_btn.grid(row=0, column=1, sticky="ew")
        self.trans_cancel_btn.config_state("disabled", bg="#E5E5EA")

    def _build_log_area(self, parent: tk.Frame) -> None:
        # Dark terminal log box with rounded border simulation
        outer = tk.Frame(parent, bg="#3A3A3C", pady=2, padx=2)
        outer.grid(row=10, column=0, sticky="ew", pady=(12, 0), padx=0)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(0, weight=1)

        log_inner = tk.Frame(outer, bg="#1D1D1F")
        log_inner.grid(row=0, column=0, sticky="nsew")
        log_inner.grid_columnconfigure(0, weight=1)
        log_inner.grid_rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_inner, wrap=tk.WORD, height=7, font=("Consolas", 10),
            bg="#1D1D1F", fg="#A8FF78", insertbackground="#1D1D1F",
            relief=tk.FLAT, padx=12, pady=10, state="disabled",
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        scroll = tk.Scrollbar(log_inner, command=self.log_text.yview, bg="#2C2C2E")
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.config(yscrollcommand=scroll.set)

    # ------------------------------------------------------------------
    # Config / browsing
    # ------------------------------------------------------------------
    def _save_config(self) -> None:
        self.config.set("downloads_dir", str(self.downloads_dir))
        self.config.set("dub_dir", self.dub_dir)
        self.config.set("concurrent_downloads", self.concurrent_var.get())
        self.config.set("max_concurrent", self.max_concurrent_var.get())
        self.config.set("use_browser_cookies", self.use_browser_cookies.get())
        self.config.set("transcription_provider", self.trans_provider_var.get())
        self.config.set("groq_api_key", self.groq_key_var.get())
        self.config.set("trans_concurrent", self.trans_concurrent_var.get())
        self.config.set("trans_max_concurrent", self.trans_max_concurrent_var.get())
        self.config.save()

    def browse_directory(self) -> None:
        selected = filedialog.askdirectory(
            initialdir=str(self.downloads_dir), title="Select Save Location",
        )
        if selected:
            self.downloads_dir = Path(selected)
            self.dir_label.config(text=str(self.downloads_dir))
            self._save_config()

    def browse_dub_directory(self) -> None:
        selected = filedialog.askdirectory(title="Select Dub Audio Folder")
        if selected:
            self.dub_dir = os.path.normpath(selected)
            self.dub_dir_label.config(text=self.dub_dir)
            self._save_config()

    def clear_local_cookies(self) -> None:
        cookies_txt = BASE_PATH / "cookies.txt"
        if not cookies_txt.exists():
            messagebox.showinfo("Clear Cookies", "No local cookies.txt found.")
            return
        if not messagebox.askyesno(
            "Clear Cookies",
            "Are you sure you want to delete the local cookies.txt file? "
            "This may help if you're getting authentication errors.",
        ):
            return
        try:
            cookies_txt.unlink()
            self.log("-> Removed local cookies.txt")
        except OSError as e:
            self.log(f"-> Error removing cookies: {e}")

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def log(self, message: str) -> None:
        # Marshal to the UI thread when called from a worker.
        if threading.current_thread() is not threading.main_thread():
            self.root.after(0, self.log, message)
            return
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")

    # ------------------------------------------------------------------
    # Dependencies
    # ------------------------------------------------------------------
    def check_dependencies(self) -> None:
        missing = find_missing_tools(BASE_PATH)
        if not missing:
            return

        msg = (
            "The following essential tools are missing:\n\n"
            + "\n".join(f"• {m}" for m in missing)
            + "\n\nWithout these, downloads and high-quality processing may be restricted."
            + "\n\nWould you like the app to download them automatically for you?"
        )
        if messagebox.askyesno("Missing Dependencies", msg):
            threading.Thread(target=self._download_tools_thread, daemon=True).start()
        else:
            self.log("[!] Warning: Missing dependencies. App may not function correctly.")

    def _download_tools_thread(self) -> None:
        try:
            install_all(BASE_PATH, self.log)
            self.root.after(
                0, messagebox.showinfo, "Setup Complete",
                "All dependencies have been downloaded and installed successfully.",
            )
        except Exception as e:
            logger.exception("Dependency setup failed")
            self.log(f"-> Error during setup: {e}")
            self.root.after(
                0, messagebox.showerror, "Setup Error",
                f"Failed to download dependencies: {e}\n\nPlease install them manually.",
            )

    @staticmethod
    def _is_url(line: str) -> bool:
        return line.lower().startswith(URL_PREFIXES)

    def _parse_input(self, lines: Sequence[str]):
        titles: List[str] = []
        links: List[str] = []
        subtitles_list: List[List[str]] = []

        link_indices: List[int] = []
        for i, line in enumerate(lines):
            if self._is_url(line) and i > 0 and not self._is_url(lines[i - 1]):
                link_indices.append(i)

        for idx_pos, link_idx in enumerate(link_indices):
            titles.append(lines[link_idx - 1])
            links.append(lines[link_idx])

            start = link_idx + 1
            end = link_indices[idx_pos + 1] - 1 if idx_pos + 1 < len(link_indices) else len(lines)
            subtitles_list.append(list(lines[start:end]))

        return titles, links, subtitles_list

    # ------------------------------------------------------------------
    # Cancellation / state
    # ------------------------------------------------------------------
    def cancel_download(self) -> None:
        with self._state_lock:
            if not self.downloading or self.cancelled:
                return
            self.cancelled = True
            procs = list(self.active_processes.values())

        self.log("\n[!] Cancellation requested. Stopping all processes...")
        for proc in procs:
            self._terminate_process(proc)
        self.root.after(0, self.reset_ui)

    def _terminate_process(self, proc: subprocess.Popen) -> None:
        try:
            if os.name == "nt":
                # /T kills children, /F is force; capture output to avoid console spam.
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, check=False,
                )
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=PROCESS_TERMINATE_TIMEOUT)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=PROCESS_TERMINATE_TIMEOUT)
        except OSError as e:
            self.log(f"-> Error cancelling process: {e}")

    # ------------------------------------------------------------------
    # Input parsing / start
    # ------------------------------------------------------------------
    @staticmethod
    def _is_url(line: str) -> bool:
        return line.lower().startswith(URL_PREFIXES)

    def _parse_input(self, lines: Sequence[str]):
        titles: List[str] = []
        links: List[str] = []
        subtitles_list: List[List[str]] = []

        link_indices: List[int] = []
        for i, line in enumerate(lines):
            if self._is_url(line) and i > 0 and not self._is_url(lines[i - 1]):
                link_indices.append(i)

        for idx_pos, link_idx in enumerate(link_indices):
            titles.append(lines[link_idx - 1])
            links.append(lines[link_idx])

            start = link_idx + 1
            end = link_indices[idx_pos + 1] - 1 if idx_pos + 1 < len(link_indices) else len(lines)
            subtitles_list.append(list(lines[start:end]))

        return titles, links, subtitles_list

    def _cancel_single_item(self, index: int) -> None:
        with self._state_lock:
            self.cancelled_indices.add(index)
            if self.download_manager:
                self.download_manager.set_item_status(index, "Cancelled", "#86868B")
            
            proc = self.active_processes.get(index)
            if proc:
                self._terminate_process(proc)

    def _manual_retry_item(self, index: int) -> None:
        """Manually retry a single failed item from the UI button."""
        if not hasattr(self, "_download_items") or index >= len(self._download_items):
            return
        # Remove from cancelled set in case it was cancelled before
        self.cancelled_indices.discard(index)
        title, link, subs = self._download_items[index]
        sync_mode = self.sync_mode_var.get()

        def _worker():
            aligner: Optional[WhisperAligner] = None
            if sync_mode == "Whisper AI (Smart Sync)":
                aligner = WhisperAligner.try_create(self.log)
            
            success = self._download_item_worker(
                index, title, link, subs, aligner, is_retry=True
            )
            with self.errors_lock:
                if not success and self.batch_errors:
                    self._save_errors(self.batch_errors)

        threading.Thread(target=_worker, daemon=True).start()

    def _skip_item(self, index: int) -> None:
        """Mark an item as skipped/done manually."""
        with self._state_lock:
            self.skipped_indices.add(index)
            if self.download_manager:
                self.download_manager.set_item_status(index, "Skipped", "#34C759")
            self.log(f"-> Item marked as skipped: {index + 1}")

    def _handle_transcribe_item(self, index: int) -> None:
        """Triggered by the 'Transcribe' button in the UI."""
        if not hasattr(self, "_download_items") or index >= len(self._download_items):
            return
            
        title, link, subs = self._download_items[index]
        provider = self.trans_provider_var.get()
        api_key = self.groq_key_var.get()
        
        def _worker():
            self.root.after(0, self.download_manager.set_item_status, index, "Transcribing...", "#FF9500")
            
            safe_title = sanitize_filename(title)
            srt_path = self.downloads_dir / f"{safe_title} (SRT).srt"
            
            audio_source = self._get_dub_track(title)
            if not audio_source:
                video_path = self.downloads_dir / f"{safe_title}.mp4"
                if video_path.exists():
                    audio_source = str(video_path)
            
            if not audio_source:
                self.log(f"[!] Error: Could not find video/audio for transcription of '{title}'")
                self.root.after(0, self.download_manager.set_item_status, index, "Failed (No File)", "#FF3B30")
                return

            success = False
            if provider == "Groq AI (Fastest)":
                transcriber = GroqTranscriber(self.log, api_key)
                success = transcriber.transcribe(audio_source, srt_path, is_cancelled=self._is_cancelled)
            else:
                aligner = WhisperAligner.try_create(self.log)
                if aligner:
                    success = aligner.transcribe(audio_source, srt_path, is_cancelled=self._is_cancelled)
                else:
                    self.log("[!] Error: Local Whisper is not available.")

            status = "Finished" if success else "Finished (Transcribe Failed)"
            color = "#34C759" if success else "#FF3B30"
            self.root.after(0, self.download_manager.set_item_status, index, status, color)

        threading.Thread(target=_worker, daemon=True).start()

    def start_download(self) -> None:
        if self.downloading:
            # If we're showing the "Finish & Return" button, reset
            if self.download_btn.text == "Finish & Return":
                if self.download_manager:
                    self.download_manager.destroy()
                    self.download_manager = None
                self.dl_input_frame.grid()
                self.reset_ui()
            return

        text = self.dl_input_text.get("1.0", tk.END).strip()
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if not lines:
            messagebox.showwarning("Input Error", "Please enter at least one title and one link.")
            return

        if self.downloads_dir.exists():
            existing = [f for f in os.listdir(self.downloads_dir)
                        if f.endswith(DOWNLOAD_FILE_EXTENSIONS)]
            if existing and messagebox.askyesno(
                "Cleanup Folder",
                f"Found {len(existing)} existing files in the download folder. "
                "Would you like to clear them first?",
            ):
                for name in existing:
                    try:
                        (self.downloads_dir / name).unlink()
                    except OSError as e:
                        self.log(f"-> Warning: Could not remove {name}: {e}")

        titles, links, subtitles_list = self._parse_input(lines)
        if not titles:
            messagebox.showwarning(
                "Input Error",
                "Could not find any valid Title and Link pairs. "
                "Make sure every link has a title on the line above it.",
            )
            return

        if any(len(subs) >= 2 for subs in subtitles_list):
            if messagebox.askyesno(
                "Ignore Lines",
                "Would you like to ignore the first 2 lines of text between the videos? "
                "(e.g. Farsi descriptions)",
            ):
                self._archive_ignored_lines(titles, subtitles_list)

        with self._state_lock:
            self.downloading = True
            self.cancelled = False

        self.download_btn.config_state("disabled", text="Downloading...", bg="#A1C6EA")
        self.cancel_btn.config_state("normal", bg="#FF3B30")
        self.browse_btn.config_state("disabled")

        self.log_text.config(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state="disabled")

        self.log(f"Starting batch download for {len(titles)} items...")
        self.dl_input_text.config(state="disabled", bg="#F5F5F7")

        sync_mode = self.sync_mode_var.get()
        self.cancelled_indices = set()
        self.skipped_indices = set()
        
        with self.errors_lock:
            self.batch_errors = []
        # Clear the errors file at start
        errors_txt = self.downloads_dir / "errors.txt"
        if errors_txt.exists():
            try:
                errors_txt.unlink()
            except OSError:
                pass
        
        # Store items so manual retry can access them after the batch finishes
        self._download_items = list(zip(titles, links, subtitles_list))

        # Hide input, show manager
        self.dl_input_frame.grid_remove()
        self.download_manager = DownloadManager(
            self.dl_tab, titles,
            self._cancel_single_item,
            self._manual_retry_item,
            self._skip_item,
            self._handle_transcribe_item,
            self.bg_color, self.text_color, self.accent_color, self.font_family
        )
        self.download_manager.grid(row=2, column=0, sticky="nsew", pady=(0, 15))

        threading.Thread(
            target=self.download_process,
            args=(titles, links, subtitles_list, sync_mode),
            daemon=True,
        ).start()

    def _archive_ignored_lines(
        self, titles: Sequence[str], subtitles_list: List[List[str]],
    ) -> None:
        try:
            titles_file_path = self.downloads_dir / "Titles.txt"
            with titles_file_path.open("a", encoding="utf-8") as tf:
                for i in range(len(subtitles_list)):
                    if len(subtitles_list[i]) >= 2:
                        tf.write(f"--- {titles[i]} ---\n")
                        tf.write(subtitles_list[i][0] + "\n")
                        tf.write(subtitles_list[i][1] + "\n\n")
                        subtitles_list[i] = subtitles_list[i][2:]
            self.log("-> Saved ignored lines to Titles.txt")
        except OSError as e:
            self.log(f"-> Warning: Could not save Titles.txt: {e}")

    # ------------------------------------------------------------------
    # Download orchestration (worker thread)
    # ------------------------------------------------------------------
    def _is_cancelled(self) -> bool:
        with self._state_lock:
            return self.cancelled

    def _add_active_process(self, index: int, proc: subprocess.Popen) -> None:
        with self._state_lock:
            self.active_processes[index] = proc

    def _remove_active_process(self, index: int) -> None:
        with self._state_lock:
            self.active_processes.pop(index, None)

    def _build_yt_dlp_command(self, title: str, link: str) -> List[str]:
        safe_title = sanitize_filename(title)
        output_path = str(self.downloads_dir / f"{safe_title}.%(ext)s")

        yt_dlp_exe = BASE_PATH / "yt-dlp.exe"
        exe_path = str(yt_dlp_exe) if yt_dlp_exe.exists() else "yt-dlp"

        cmd: List[str] = [
            exe_path,
            "-o", output_path,
            "--no-playlist",
            # H.264 (avc1) + AAC (m4a) keeps Premiere Pro happy.
            "-f",
            "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
        ]

        cookies_txt = BASE_PATH / "cookies.txt"
        if cookies_txt.exists():
            cmd.extend(["--cookies", str(cookies_txt)])
        elif self.use_browser_cookies.get():
            cmd.extend(["--cookies-from-browser", "chrome"])

        deno_exe = BASE_PATH / "deno.exe"
        if deno_exe.exists():
            cmd.extend(["--js-runtime", "deno:" + str(deno_exe)])

        ffmpeg_exe = BASE_PATH / "ffmpeg.exe"
        if ffmpeg_exe.exists():
            cmd.extend(["--ffmpeg-location", str(ffmpeg_exe)])

        cmd.append(link)
        return cmd

    def _run_yt_dlp(
        self, index: int, cmd: List[str], progress_callback: Optional[Callable[[float], None]] = None
    ) -> int:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, creationflags=creationflags,
        )
        self._add_active_process(index, proc)
        
        # Regex for yt-dlp progress: [download]  10.0% of ...
        progress_re = re.compile(r"\[download\]\s+(\d+\.\d+)%")

        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if self._is_cancelled():
                    break
                
                clean_line = line.strip()
                if clean_line:
                    self.log("  " + clean_line)
                    if progress_callback:
                        match = progress_re.search(clean_line)
                        if match:
                            try:
                                percent = float(match.group(1))
                                progress_callback(percent)
                            except ValueError:
                                pass

            try:
                proc.wait(timeout=PROCESS_TERMINATE_TIMEOUT)
            except subprocess.TimeoutExpired:
                self._terminate_process(proc)
                proc.wait()
            return proc.returncode if proc.returncode is not None else -1
        finally:
            self._remove_active_process(index)

    def _download_item_worker(
        self,
        index: int,
        title: str,
        link: str,
        subs: Sequence[str],
        aligner: Optional[WhisperAligner],
        is_retry: bool = False,
    ) -> bool:
        """Worker function for a single item download. Returns True if successful."""
        if self._is_cancelled() or index in self.cancelled_indices:
            return False

        label = f" (Retry)" if is_retry else ""
        self.log(f"\n[{index+1}] Starting: {title}{label}")
        self.log(f"URL: {link}")

        if self.download_manager:
            status = "Retrying..." if is_retry else "Active"
            self.root.after(0, self.download_manager.set_item_status, index, status, self.accent_color)

        cmd = self._build_yt_dlp_command(title, link)
        if self._is_cancelled() or index in self.cancelled_indices:
            return False

        def update_ui(p: float):
            if self.download_manager:
                self.root.after(0, self.download_manager.update_item_progress, index, p)

        return_code = self._run_yt_dlp(index, cmd, progress_callback=update_ui)

        if self._is_cancelled():
            return False
        
        if index in self.cancelled_indices:
            self.log(f"-> Item cancelled: {title}")
            return False

        if return_code == 0:
            self.log(f"-> Successfully downloaded: {title}")
            if self.download_manager:
                self.root.after(0, self.download_manager.set_item_status, index, "Finished", "#34C759")
            self._maybe_generate_srt(title, subs, aligner)
            return True
        else:
            self.log(f"-> Error downloading: {title} (Return code: {return_code})")
            if not is_retry:
                if self.download_manager:
                    self.root.after(0, self.download_manager.set_item_status, index, "Failed (Queued for Retry)", "#FF9500")
            else:
                if self.download_manager:
                    self.root.after(0, self.download_manager.set_item_status, index, "Failed", "#FF3B30")
            
            with self.errors_lock:
                self.batch_errors.append(f"Download Error for '{title}': Process exited with code {return_code}")
            return False

    def download_process(
        self,
        titles: Sequence[str],
        links: Sequence[str],
        subtitles_list: Sequence[Sequence[str]],
        sync_mode: str,
    ) -> None:
        aligner: Optional[WhisperAligner] = None
        if sync_mode == "Whisper AI (Smart Sync)":
            aligner = WhisperAligner.try_create(self.log)
            if aligner is None:
                sync_mode = "None (2-second intervals)"

        try:
            self._save_batch_links(titles, links)
            errors_found: List[str] = []
            error_lock = threading.Lock()
            
            concurrent_mode = self.concurrent_var.get()
            max_workers = self.max_concurrent_var.get() if concurrent_mode else 1
            
            items = list(zip(range(len(titles)), titles, links, subtitles_list))
            failed_items = []

            # Phase 1: Initial Download
            self.log(f"Starting downloads (Mode: {'Concurrent' if concurrent_mode else 'Sequential'}, Workers: {max_workers})...")
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {}
                for i, title, link, subs in items:
                    futures[executor.submit(
                        self._download_item_worker, i, title, link, subs, aligner
                    )] = i
                
                for future, item_index in futures.items():
                    success = future.result()
                    if not success and not self._is_cancelled() and item_index not in self.cancelled_indices and item_index not in self.skipped_indices:
                        failed_items.append(items[item_index])

            # Phases 2-6: Retry failed items up to 5 times total
            MAX_AUTO_RETRIES = 5
            for attempt in range(1, MAX_AUTO_RETRIES + 1):
                if not failed_items or self._is_cancelled():
                    break
                self.log(f"\n--- Retry attempt {attempt}/{MAX_AUTO_RETRIES} for {len(failed_items)} item(s) ---")
                still_failing = []
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    retry_futures = {}
                    for i, title, link, subs in failed_items:
                        retry_futures[executor.submit(
                            self._download_item_worker, i, title, link, subs, aligner, is_retry=True
                        )] = (i, title, link, subs)
                    for future, item_tuple in retry_futures.items():
                        success = future.result()
                        i = item_tuple[0]
                        if not success and not self._is_cancelled() and i not in self.cancelled_indices and i not in self.skipped_indices:
                            still_failing.append(item_tuple)
                failed_items = still_failing

            with self.errors_lock:
                if self.batch_errors:
                    self._save_errors(self.batch_errors)

        except Exception as e:
            logger.exception("Batch download crashed")
            self.log(f"\nAn error occurred: {e}")
        finally:
            has_failures = False
            if self.download_manager:
                for item in self.download_manager.items:
                    if item.status_label["text"] == "Failed":
                        has_failures = True
                        break

            if has_failures and not self._is_cancelled():
                self.log("\nBatch finished with errors. You can manually Retry or Skip items now.")
                self.root.after(0, lambda: self.download_btn.config_state("normal", text="Finish & Return", bg="#34C759"))
            else:
                self.log(
                    "\nBatch download cancelled by user."
                    if self._is_cancelled()
                    else "\nBatch download complete."
                )
                if self.download_manager:
                    self.root.after(0, self.download_manager.destroy)
                    self.download_manager = None
                self.dl_input_frame.grid()
                self.root.after(0, self.reset_ui)

    def _save_batch_links(self, titles: Sequence[str], links: Sequence[str]) -> None:
        try:
            path = self.downloads_dir / "batch_links.txt"
            with path.open("w", encoding="utf-8") as bf:
                for title, link in zip(titles, links):
                    bf.write(f"{title}\n{link}\n\n")
            self.log(f"-> Saved batch_links.txt to {self.downloads_dir}")
        except OSError as e:
            self.log(f"-> Warning: Could not save batch_links.txt: {e}")

    def _save_errors(self, errors_found: Sequence[str]) -> None:
        try:
            path = self.downloads_dir / "errors.txt"
            with path.open("w", encoding="utf-8") as ef:
                ef.write("--- Download Errors ---\n\n")
                for err in errors_found:
                    ef.write(f"- {err}\n")
            self.log(f"-> Exported {len(errors_found)} errors to errors.txt")
        except OSError as e:
            self.log(f"-> Warning: Could not save errors.txt: {e}")

    def _maybe_generate_srt(
        self,
        title: str,
        subs: Sequence[str],
        aligner: Optional[WhisperAligner],
    ) -> None:
        if not subs:
            return

        safe_title = sanitize_filename(title)
        srt_path = self.downloads_dir / f"{safe_title} (SRT).srt"
        whisper_success = False

        if aligner is not None:
            if self._is_cancelled():
                return

            # 1. Determine audio source (Dub folder > Downloaded Video)
            audio_source = self._get_dub_track(title)
            if not audio_source:
                video_path = self.downloads_dir / f"{safe_title}.mp4"
                if video_path.exists():
                    audio_source = str(video_path)

            if audio_source:
                self.log("-> Starting Whisper Smart Sync... (This may take a minute)")
                try:
                    whisper_success = aligner.align(
                        audio_source, subs, srt_path, is_cancelled=self._is_cancelled,
                    )
                except Exception as e:
                    logger.exception("Whisper sync failed for %s", title)
                    self.log(f"-> Error with Whisper Sync: {e}")
                    with self.errors_lock:
                        self.batch_errors.append(f"Whisper Sync Error for '{title}': {e}")
                    self.log("-> Falling back to 2-second timestamps...")
            else:
                self.log("-> No matching audio found for sync. Falling back to 2-second timestamps.")

        if not whisper_success and not self._is_cancelled():
            def progress_fallback(curr, total):
                if self.download_manager:
                    # aligner doesn't have a clean way to report progress for standard SRT
                    # but we can just set it to 50% while "Generating..."
                    self.root.after(0, self.download_manager.update_item_progress, -2, (curr/total)*100) # -2 as dummy if needed

            generate_standard_srt(subs, srt_path, self.log)

    def _get_dub_track(self, title: str) -> Optional[str]:
        if not self.dub_dir:
            return None
        dub_dir = Path(self.dub_dir)
        for ext in (".mp3", ".wav", ".m4a"):
            candidate = dub_dir / f"{title}{ext}"
            if candidate.exists():
                self.log(f"-> Found dub track: {candidate.name} - Syncing with Whisper AI!")
                return str(candidate)
        return None

    # ------------------------------------------------------------------
    # Transcription Tab Logic
    # ------------------------------------------------------------------

    def start_transcription(self) -> None:
        """Called by the Start button on the Transcription tab."""
        if self.downloading:
            return

        text = self.trans_input_text.get("1.0", tk.END).strip()
        if not text:
            self.log("[!] No links provided in the transcription tab.")
            return

        titles, links = parse_titles_and_links(text)
        if not links:
            self.log("[!] Could not find any valid links in the input.")
            return

        self.downloading = True
        self.cancelled = False
        self.batch_errors = []
        
        self.trans_btn.config_state("disabled", text="Transcribing...", bg="#E5E5EA")
        self.trans_cancel_btn.config_state("normal", bg="#FF3B30")
        self.groq_key_entry.entry.config(state="disabled")
        
        # Hide input, show manager in transcription tab
        self.trans_input_text.config(state="disabled") # Lock text
        self.trans_input_text.grid_remove() 
        # Wait, I need to make sure the input area can be hidden. 
        # In _build_trans_tab, it's 'border'. I should save a reference to it.
        if hasattr(self, "trans_border"):
            self.trans_border.grid_remove()

        self.trans_manager = DownloadManager(
            self.trans_tab, titles,
            self._cancel_single_item, 
            self._trans_retry_item, # New retry callback
            self._trans_skip_item,  # New skip callback
            None, 
            self.bg_color, self.text_color, self.accent_color, self.font_family
        )
        self.trans_manager.grid(row=2, column=0, sticky="nsew", pady=(0, 15))
        
        thread = threading.Thread(
            target=self._transcribe_batch_worker,
            args=(titles, links),
            daemon=True
        )
        thread.start()

    def on_transcribe_item(self, index: int) -> None:
        """Called by the Transcribe button in the Download Manager."""
        if not self.download_manager or not hasattr(self, "_download_items"):
            return
            
        item = self.download_manager.items[index]
        title = item.title
        # Get link from stored download items
        link = self._download_items[index][1] if index < len(self._download_items) else "Unknown Link"
        
        # For single items from the download list, we'll try to find the downloaded video or dub
        self.root.after(0, self.download_manager.set_item_status, index, "Transcribing...", "#FF9500")
        
        thread = threading.Thread(
            target=self._transcribe_single_worker,
            args=(index, title, link),
            daemon=True
        )
        thread.start()

    def _trans_retry_item(self, index: int) -> None:
        """Manually retry a single failed transcription item."""
        if not hasattr(self, "_trans_batch_items") or index >= len(self._trans_batch_items):
            return
        
        self.cancelled_indices.discard(index)
        title, link = self._trans_batch_items[index]
        
        def _worker():
            # Create a dedicated aligner/transcriber for the retry
            provider = self.trans_provider_var.get()
            transcriber = None
            if provider == "Groq AI (Fastest)":
                transcriber = GroqTranscriber(self.log, self.groq_key_var.get())
            else:
                transcriber = WhisperAligner.try_create(self.log)
                
            if not transcriber:
                self.root.after(0, self.trans_manager.set_item_status, index, "Failed (AI Init)", "#FF3B30")
                return

            self._trans_item_task(index, title, link, transcriber, is_retry=True)
            
        threading.Thread(target=_worker, daemon=True).start()

    def _trans_skip_item(self, index: int) -> None:
        """Mark a transcription item as skipped."""
        with self._state_lock:
            if self.trans_manager:
                self.trans_manager.set_item_status(index, "Skipped", "#34C759")
            self.log(f"-> Transcription item marked as skipped: {index + 1}")

    def _trans_item_task(self, index: int, title: str, link: str, transcriber, is_retry: bool = False) -> bool:
        """Worker for a single transcription item. Returns True if successful."""
        if self._is_cancelled() or index in self.cancelled_indices:
            return False
            
        status = "Retrying..." if is_retry else "Active"
        self.root.after(0, self.trans_manager.set_item_status, index, status, self.accent_color)
        
        # 1. Extract audio
        audio_path = self._extract_audio_only(title, link, index)
        if not audio_path:
            self.log(f"-> Failed to extract audio for: {title}")
            self.root.after(0, self.trans_manager.set_item_status, index, "Failed (Audio)", "#FF3B30")
            return False

        # 2. Transcribe
        self.log(f"-> Transcribing: {title}")
        
        def progress_cb(curr, total):
            if total > 0:
                percent = (curr / total) * 100
                self.root.after(0, self.trans_manager.update_item_progress, index, percent)

        transcript = transcriber.transcribe_to_text(
            audio_path, is_cancelled=self._is_cancelled, progress_callback=progress_cb
        )
        
        # Cleanup temp audio immediately to save space
        if audio_path and os.path.exists(audio_path):
            try: os.remove(audio_path)
            except: pass

        if transcript:
            # Append to the combined report immediately
            self._append_to_combined_report(title, link, transcript)
            self.root.after(0, self.trans_manager.set_item_status, index, "Finished", "#34C759")
            return True
        else:
            self.root.after(0, self.trans_manager.set_item_status, index, "Failed", "#FF3B30")
            return False

    def _append_to_combined_report(self, title: str, link: str, transcript: str) -> None:
        report_path = self.downloads_dir / "All_Transcriptions.txt"
        with self._state_lock: # Reuse state lock for file writing safety
            try:
                first_write = not report_path.exists()
                with report_path.open("a", encoding="utf-8") as f:
                    if first_write:
                        f.write("--- BATCH TRANSCRIPTION REPORT ---\n\n")
                    f.write(f"TITLE: {title}\n")
                    f.write(f"LINK: {link}\n")
                    f.write("-" * 20 + "\n")
                    f.write(transcript.strip())
                    f.write("\n\n" + "=" * 60 + "\n\n")
            except OSError as e:
                self.log(f"[!] Error appending to report: {e}")

    def _transcribe_batch_worker(self, titles: List[str], links: List[str]) -> None:
        self.log(f"\n--- Starting Batch Transcription ({len(links)} items) ---")
        
        # Clear/initialize combined report at start of batch
        report_path = self.downloads_dir / "All_Transcriptions.txt"
        if report_path.exists():
            try: report_path.unlink()
            except: pass

        self._trans_batch_items = list(zip(titles, links)) 
        
        provider = self.trans_provider_var.get()
        concurrent_mode = self.trans_concurrent_var.get()
        max_workers = self.trans_max_concurrent_var.get() if concurrent_mode else 1
        
        all_results = [None] * len(titles) # Use fixed size for order
        results_lock = threading.Lock()
        
        transcriber = None
        if provider == "Groq AI (Fastest)":
            transcriber = GroqTranscriber(self.log, self.groq_key_var.get())
        else:
            transcriber = WhisperAligner.try_create(self.log)

        if not transcriber:
            self.log("[!] Failed to initialize transcription provider.")
            self.root.after(0, self.reset_ui)
            return

        items = list(zip(range(len(titles)), titles, links))
        failed_items = []

        # Phase 1: Initial Run
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for i, title, link in items:
                futures[executor.submit(self._trans_item_task, i, title, link, transcriber)] = (i, title, link)
            
            for future, (i, title, link) in futures.items():
                success = future.result()
                if not success and not self._is_cancelled() and i not in self.cancelled_indices:
                    failed_items.append((i, title, link))

        # Phase 2: Auto-Retries (up to 2 times)
        for attempt in range(1, 3):
            if not failed_items or self._is_cancelled():
                break
            self.log(f"\n--- Auto-Retry attempt {attempt} for {len(failed_items)} items ---")
            still_failing = []
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                retry_futures = {}
                for i, title, link in failed_items:
                    retry_futures[executor.submit(self._trans_item_task, i, title, link, transcriber, is_retry=True)] = (i, title, link)
                for future, (i, title, link) in retry_futures.items():
                    if not future.result() and not self._is_cancelled() and i not in self.cancelled_indices:
                        still_failing.append((i, title, link))
            failed_items = still_failing

        # 3. Save combined report (only for items that finished)
        # We need to collect results. Wait, _trans_item_task didn't return the transcript.
        # Let's fix that.
        # Actually, let's just re-read finished individual reports if needed, 
        # OR just have _trans_item_task store it in a shared dict.
        
        # Phase 2 and 3 omitted for brevity in targetContent, but I need to make sure I don't break them.
        # Wait, the targetContent I picked is too large or I might mess up.
        # Let's just replace the result collection part.

        # (Removing the old result collection logic since we append in real-time now)
        
        has_failures = any(item.status_label["text"] == "Failed" for item in self.trans_manager.items)
        if has_failures and not self._is_cancelled():
            self.log("\nBatch finished with errors. You can manually Retry or Skip items now.")
            self.log(f"Partial results saved in: {report_path.name}")
            self.root.after(0, lambda: self.trans_btn.config_state("normal", text="Finish & Return", bg="#34C759"))
        else:
            self.log("\n--- Batch Transcription Complete ---")
            self.log(f"All results saved in: {report_path.name}")
            self.root.after(0, self.reset_ui)

    def _save_combined_transcription_report(self, results: List[Tuple[str, str, str]]) -> None:
        report_path = self.downloads_dir / "All_Transcriptions.txt"
        try:
            with report_path.open("w", encoding="utf-8") as f:
                f.write("--- BATCH TRANSCRIPTION REPORT ---\n")
                f.write(f"Generated on: {os.path.basename(str(report_path))}\n\n")
                for title, link, transcript in results:
                    f.write(f"TITLE: {title}\n")
                    f.write(f"LINK: {link}\n")
                    f.write("-" * 20 + "\n")
                    f.write(transcript.strip())
                    f.write("\n\n" + "=" * 60 + "\n\n")
            self.log(f"-> Combined report saved to: {report_path.name}")
        except OSError as e:
            self.log(f"[!] Error saving combined report: {e}")

    def _transcribe_single_worker(self, index: int, title: str, link: str) -> None:
        # Similar logic but updates the DownloadManager status
        provider = self.trans_provider_var.get()
        transcriber = None
        if provider == "Groq AI (Fastest)":
            transcriber = GroqTranscriber(self.log, self.groq_key_var.get())
        else:
            transcriber = WhisperAligner.try_create(self.log)
            
        if not transcriber:
            self.root.after(0, self.download_manager.set_item_status, index, "Failed", "#FF3B30")
            return

        # Try to find existing audio/video first
        audio_source = self._get_dub_track(title)
        if not audio_source:
            video_path = self.downloads_dir / f"{sanitize_filename(title)}.mp4"
            if video_path.exists():
                audio_source = str(video_path)
        
        # If no local file, we have to download audio (though usually single transcribe is called after download)
        temp_audio = None
        if not audio_source:
            self.log(f"-> No local file found for {title}, extracting audio from link...")
            audio_source = self._extract_audio_only(title, link)
            temp_audio = audio_source

        if not audio_source:
            self.root.after(0, self.download_manager.set_item_status, index, "No Audio", "#FF3B30")
            return

        def progress_cb(curr, total):
            if total > 0:
                percent = (curr / total) * 100
                self.root.after(0, self.download_manager.update_item_progress, index, percent)

        transcript = transcriber.transcribe_to_text(
            audio_source, is_cancelled=self._is_cancelled, progress_callback=progress_cb
        )
        
        if transcript:
            # For single items from download tab, we still save individual reports but could also append?
            # User said "all the transcriptions in 1 text file" for batch. 
            # For single item click, I'll keep it individual or also save to the combined one?
            # Let's keep single individual for now as it's a different trigger.
            self._save_transcription_report(title, link, transcript)
            self.root.after(0, self.download_manager.set_item_status, index, "Transcript Saved", "#34C759")
        else:
            self.root.after(0, self.download_manager.set_item_status, index, "Failed", "#FF3B30")

        if temp_audio and os.path.exists(temp_audio):
            try: os.remove(temp_audio)
            except: pass

    def _extract_audio_only(self, title: str, link: str, index: int = -1) -> Optional[str]:
        """Extracts mono audio to a temp file for transcription."""
        safe_title = sanitize_filename(title)
        output_path = self.downloads_dir / f"{safe_title}_audio.mp3"
        
        yt_dlp_exe = BASE_PATH / "yt-dlp.exe"
        exe_path = str(yt_dlp_exe) if yt_dlp_exe.exists() else "yt-dlp"

        # yt-dlp command to just get audio
        cmd = [
            exe_path,
            "-x", "--audio-format", "mp3",
            "--audio-quality", "0",
            "-o", str(output_path),
            "--no-playlist",
        ]

        cookies_txt = BASE_PATH / "cookies.txt"
        if cookies_txt.exists():
            cmd.extend(["--cookies", str(cookies_txt)])
        elif self.use_browser_cookies.get():
            cmd.extend(["--cookies-from-browser", "chrome"])

        deno_exe = BASE_PATH / "deno.exe"
        if deno_exe.exists():
            cmd.extend(["--js-runtime", "deno:" + str(deno_exe)])

        ffmpeg_exe = BASE_PATH / "ffmpeg.exe"
        if ffmpeg_exe.exists():
            cmd.extend(["--ffmpeg-location", str(ffmpeg_exe)])

        cmd.append(link)
        
        self.log(f"-> Extracting audio for {title}...")
        
        def audio_progress(p):
            if index >= 0 and hasattr(self, "trans_manager") and self.trans_manager:
                self.root.after(0, self.trans_manager.update_item_progress, index, p * 0.1) # 0-10% for audio extract
            elif index >= 0 and self.download_manager:
                self.root.after(0, self.download_manager.update_item_progress, index, p * 0.1)

        return_code = self._run_yt_dlp(index, cmd, progress_callback=audio_progress)
        
        if return_code == 0 and output_path.exists():
            return str(output_path)
        return None

    def _save_transcription_report(self, title: str, link: str, transcript: str) -> None:
        safe_title = sanitize_filename(title)
        report_path = self.downloads_dir / f"{safe_title} (Transcript).txt"
        try:
            with report_path.open("w", encoding="utf-8") as f:
                f.write(f"TITLE: {title}\n")
                f.write(f"LINK: {link}\n")
                f.write("-" * 40 + "\n\n")
                f.write(transcript)
        except OSError as e:
            self.log(f"[!] Error saving report: {e}")

    # ------------------------------------------------------------------
    # UI reset
    # ------------------------------------------------------------------
    def reset_ui(self) -> None:
        with self._state_lock:
            self.downloading = False
        
        # Reset Download Tab Buttons
        self.download_btn.config_state("normal", text="Start Batch Download", bg=self.accent_color)
        self.cancel_btn.config_state("disabled", bg="#E5E5EA")
        self.browse_btn.config_state("normal", bg=self.gray_bg)
        self.dl_input_text.config(state="normal", bg="white")
        
        # Reset Transcription Tab Buttons
        self.trans_btn.config_state("normal", text="🎙  Start Transcription", bg=self.accent_color)
        self.trans_cancel_btn.config_state("disabled", bg="#E5E5EA")
        self.groq_key_entry.entry.config(state="normal")
        if hasattr(self, "trans_border"):
            self.trans_border.grid()
        if hasattr(self, "trans_manager") and self.trans_manager:
            self.trans_manager.destroy()
            self.trans_manager = None
        self.trans_input_text.config(state="normal", bg="white")


def _enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError) as e:
        logger.debug("Could not enable DPI awareness: %s", e)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _enable_dpi_awareness()
    root = tk.Tk()
    AppleStyleApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
