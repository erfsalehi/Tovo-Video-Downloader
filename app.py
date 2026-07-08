"""Tovo Video Downloader - GUI entry point and download orchestration."""
from __future__ import annotations

import datetime
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler

from config import Config
from dependencies import find_missing_tools, install_all, update_yt_dlp
import requests
from subtitles import (
    GroqTranscriber, generate_standard_srt, read_srt_cues,
    CaptionStyle, write_ttml,
)
from widgets import DownloadManager, RoundedButton, RoundedEntry, ModernCheckbutton, RoundedFrame
import voiceover
import shortclips

logger = logging.getLogger(__name__)

BASE_PATH = Path(__file__).resolve().parent
LOG_DIR = BASE_PATH / "logs"
LOG_FILE = LOG_DIR / "tovo.log"
URL_PREFIXES = ("http://", "https://")
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DOWNLOAD_FILE_EXTENSIONS = (".mp4", ".srt", ".txt", ".mp3", ".wav", ".m4a")
SRT_TITLE_SUFFIX = " (SRT)"  # how _maybe_generate_srt names SRT files
SYNCED_SRT_SUFFIX = " (Synced)"  # Sync tab writes a new file, keeps the original
DUB_AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".mp4")
PROCESS_TERMINATE_TIMEOUT = 5  # seconds before SIGKILL fallback


def sanitize_filename(name: str) -> str:
    """Strip path separators and unsafe characters from a video title."""
    cleaned = INVALID_FILENAME_CHARS.sub("_", name).strip(" .")
    cleaned = Path(cleaned).name  # drop any directory components
    return cleaned or "untitled"


def parse_titles_and_links(text: str):
    """Utility for transcription tab to parse Title + Link pairs.
    Handles Title-on-prev-line OR just links alone.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    titles: List[str] = []
    links: List[str] = []
    
    for i, line in enumerate(lines):
        if line.lower().startswith(URL_PREFIXES):
            # If previous line wasn't a URL, use it as title
            if i > 0 and not lines[i-1].lower().startswith(URL_PREFIXES):
                titles.append(lines[i-1])
            else:
                # Use a portion of the URL or a generic title
                titles.append(f"Video_{len(links)+1}")
            links.append(line)
            
    return titles, links


class AppleStyleApp:
    """Main GUI window: input pane, options, log, and the download worker."""

    # Max-quality cap → max video height in pixels. "Default" (None) means no
    # cap: the best H.264 stream, which on YouTube tops out at 1080p. Any other
    # value caps the height (best available H.264 up to that height). Only H.264
    # resolutions are offered because the downloader is H.264-only for Premiere
    # compatibility — YouTube has no H.264 above 1080p.
    QUALITY_HEIGHTS: Dict[str, Optional[int]] = {
        "Default": None,
        "1080p (Full HD)": 1080,
        "720p (HD)": 720,
        "480p": 480,
        "360p": 360,
    }

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Video Downloader")
        # Open at (nearly) full work-area height for a roomier layout — the paste
        # box absorbs the extra space. Width stays compact and centred. Using the
        # work area (not raw screen height) keeps the window clear of the taskbar.
        wx, wy, ww, wh = self._usable_screen_rect()
        win_w = min(860, ww)
        win_h = max(680, wh - 48)  # leave room for the title bar
        pos_x = wx + max(0, (ww - win_w) // 2)
        self.root.geometry(f"{win_w}x{win_h}+{pos_x}+{wy}")
        self.root.minsize(700, 640)

        self.config = Config(BASE_PATH / "config.json")
        # Migrate the stale default Shorts model to the current one.
        if self.config.get("shorts_model") == "deepseek/deepseek-chat":
            self.config.set("shorts_model", "deepseek/deepseek-v4-flash")
        if not self.config.get("downloads_dir"):
            self.config.set("downloads_dir", str(BASE_PATH / "Downloads"))
        self.downloads_dir = Path(self.config.get("downloads_dir"))
        self.trans_dir = Path(self.config.get("trans_dir", str(BASE_PATH / "Transcriptions")))
        self.dub_dir = self.config.get("dub_dir", "")
        self.vo_source_dir = self.config.get("vo_source_dir", "")
        # Source inputs may be a mix of folders and .zip archives. Migrate the
        # old single-folder setting into the list on first run.
        self.vo_sources: List[str] = list(self.config.get("vo_sources", []) or [])
        if not self.vo_sources and self.vo_source_dir:
            self.vo_sources = [self.vo_source_dir]
        self.caption_dir = self.config.get("caption_dir", "")
        self.shorts_video = ""  # selected per session; UI build reads this
        # Locate the Mangio-RVC folder (auto-detect if the saved path is missing,
        # e.g. on a different machine) so the Voiceover tab works everywhere.
        self.rvc_dir = self.config.get("rvc_dir", "")
        _rvc_found = voiceover.find_rvc_dir(self.rvc_dir)
        if _rvc_found:
            self.rvc_dir = str(_rvc_found)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.trans_dir.mkdir(parents=True, exist_ok=True)

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
        self._build_menu()
        self._build_ui()
        self.download_manager: Optional[DownloadManager] = None
        self.cancelled_indices: set[int] = set()

        # Sync tab state
        self.sync_manager: Optional[DownloadManager] = None
        self.sync_items: List[dict] = []   # scan results: {title, srt_path, audio_path, kind}
        self._sync_chosen: List[dict] = []

        # Voiceover tab state
        self.vo_manager: Optional[DownloadManager] = None
        self._vo_candidates: List[Dict] = []  # clip specs (folder files + zip members)

        # Auto-Caption tab state
        self.caption_dir = self.config.get("caption_dir", "")
        self.caption_manager: Optional[DownloadManager] = None
        self._caption_items: List[Path] = []

        # Short Clips tab state
        self.shorts_video = ""
        self._shorts_clips: List[dict] = []                 # highlight suggestions
        self._shorts_segments: List[Tuple[float, float, str]] = []  # cached transcript

        self.root.after(500, self.check_dependencies)

    def _usable_screen_rect(self) -> Tuple[int, int, int, int]:
        """(x, y, width, height) of the desktop work area — i.e. the screen minus
        the taskbar. Falls back to the full screen size off Windows or on error."""
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes
                rect = wintypes.RECT()
                # SPI_GETWORKAREA = 0x0030
                if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
            except Exception:
                logger.debug("Could not query screen work area", exc_info=True)
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

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
            width=120, height=44
        )
        self.btn_dl_tab.pack(side=tk.LEFT, padx=(0, 10))
        
        self.btn_trans_tab = RoundedButton(
            tab_frame, text="Transcription", command=lambda: self._switch_tab("trans"),
            radius=20, bg_color=self.bg_color, hover_color=self.gray_hover,
            text_color="#86868B", font=(self.font_family, 11, "bold"),
            width=120, height=44
        )
        self.btn_trans_tab.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_sync_tab = RoundedButton(
            tab_frame, text="Sync SRT", command=lambda: self._switch_tab("sync"),
            radius=20, bg_color=self.bg_color, hover_color=self.gray_hover,
            text_color="#86868B", font=(self.font_family, 11, "bold"),
            width=120, height=44
        )
        self.btn_sync_tab.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_vo_tab = RoundedButton(
            tab_frame, text="Voiceover", command=lambda: self._switch_tab("vo"),
            radius=20, bg_color=self.bg_color, hover_color=self.gray_hover,
            text_color="#86868B", font=(self.font_family, 11, "bold"),
            width=120, height=44
        )
        self.btn_vo_tab.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_cap_tab = RoundedButton(
            tab_frame, text="Captions", command=lambda: self._switch_tab("cap"),
            radius=20, bg_color=self.bg_color, hover_color=self.gray_hover,
            text_color="#86868B", font=(self.font_family, 11, "bold"),
            width=120, height=44
        )
        self.btn_cap_tab.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_shorts_tab = RoundedButton(
            tab_frame, text="Shorts", command=lambda: self._switch_tab("shorts"),
            radius=20, bg_color=self.bg_color, hover_color=self.gray_hover,
            text_color="#86868B", font=(self.font_family, 11, "bold"),
            width=120, height=44
        )
        self.btn_shorts_tab.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_t2s_tab = RoundedButton(
            tab_frame, text="Text → SRT", command=lambda: self._switch_tab("t2s"),
            radius=20, bg_color=self.bg_color, hover_color=self.gray_hover,
            text_color="#86868B", font=(self.font_family, 11, "bold"),
            width=120, height=44
        )
        self.btn_t2s_tab.pack(side=tk.LEFT)

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

        # Tab 3: Sync SRT
        self.sync_tab = tk.Frame(self.content_frame, bg=self.bg_color)
        self.sync_tab.grid(row=0, column=0, sticky="nsew")
        self._build_sync_tab(self.sync_tab)

        # Tab 4: Voiceover
        self.vo_tab = tk.Frame(self.content_frame, bg=self.bg_color)
        self.vo_tab.grid(row=0, column=0, sticky="nsew")
        self._build_vo_tab(self.vo_tab)

        # Tab 5: Auto-Caption
        self.cap_tab = tk.Frame(self.content_frame, bg=self.bg_color)
        self.cap_tab.grid(row=0, column=0, sticky="nsew")
        self._build_caption_tab(self.cap_tab)

        # Tab 6: Short Clips
        self.shorts_tab = tk.Frame(self.content_frame, bg=self.bg_color)
        self.shorts_tab.grid(row=0, column=0, sticky="nsew")
        self._build_shorts_tab(self.shorts_tab)

        # Tab 7: Text to SRT
        self.t2s_tab = tk.Frame(self.content_frame, bg=self.bg_color)
        self.t2s_tab.grid(row=0, column=0, sticky="nsew")
        self._build_txt2srt_tab(self.t2s_tab)

        # Initial State
        self._switch_tab("dl")

        # Shared Log Area (at the bottom of container)
        self._build_log_area(container)

    def _switch_tab(self, tab: str) -> None:
        tabs = {
            "dl": (self.dl_tab, self.btn_dl_tab),
            "trans": (self.trans_tab, self.btn_trans_tab),
            "sync": (self.sync_tab, self.btn_sync_tab),
            "vo": (self.vo_tab, self.btn_vo_tab),
            "cap": (self.cap_tab, self.btn_cap_tab),
            "shorts": (self.shorts_tab, self.btn_shorts_tab),
            "t2s": (self.t2s_tab, self.btn_t2s_tab),
        }
        if tab not in tabs:
            return
        tabs[tab][0].tkraise()
        for key, (_frame, btn) in tabs.items():
            active = key == tab
            btn.config_state("normal", bg="white" if active else self.bg_color)
            btn.text_color = self.accent_color if active else "#86868B"
            btn._draw()
        self._active_tab = tab

        # Refresh the sync list each time the tab is opened (unless a job runs).
        if tab == "sync" and not self.downloading:
            self._scan_sync_items()
        if tab == "cap" and not self.downloading:
            self._scan_caption_items()

    def _build_dl_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        # minsize keeps the input visible on first paint: row 2 is the only
        # weighted row, so without a floor it absorbs all the layout shrink and
        # collapses to ~0px until the user resizes the window. Kept modest so the
        # many option rows below + the Start button still fit without scrolling.
        parent.grid_rowconfigure(2, weight=1, minsize=80)

        tk.Label(
            parent, text="Batch Video Downloader", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 22, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(8, 2), padx=10)

        tk.Label(
            parent, text="Paste Title on line 1, Link on line 2, etc.",
            bg=self.bg_color, fg="#86868B", font=(self.font_family, 10),
        ).grid(row=1, column=0, sticky="w", pady=(0, 4), padx=10)

        # Text area with proper border via frame highlight
        border = tk.Frame(parent, bg="#D2D2D7")
        border.grid(row=2, column=0, sticky="nsew", pady=(0, 8), padx=10)
        border.grid_columnconfigure(0, weight=1)
        border.grid_rowconfigure(0, weight=1)

        inner = tk.Frame(border, bg="white")
        inner.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        inner.grid_columnconfigure(0, weight=1)
        inner.grid_rowconfigure(0, weight=1)

        self.dl_input_text = tk.Text(
            inner, wrap=tk.WORD, font=(self.font_family, 11),
            bg="white", fg=self.text_color, insertbackground=self.accent_color,
            relief=tk.FLAT, padx=10, pady=10, undo=True,
            height=6, width=1,  # small request; grid weight lets it expand
        )
        self.dl_input_text.grid(row=0, column=0, sticky="nsew")

        scroll = tk.Scrollbar(inner, command=self.dl_input_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.dl_input_text.config(yscrollcommand=scroll.set)
        self._bind_context_menu(self.dl_input_text)
        self.dl_input_frame = border  # keep reference for grid_remove

        self._build_save_row(parent, row=3)
        self._build_dl_quality_row(parent, row=4)
        self._build_dl_options_row(parent, row=5)
        self._build_dl_concurrent_row(parent, row=6)
        self._build_dub_row(parent, row=7)
        self._build_advanced_row(parent, row=8)
        self._build_dl_button_row(parent, row=9)

    def _build_trans_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1, minsize=160)

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
            relief=tk.FLAT, padx=10, pady=10, undo=True,
            height=6, width=1,  # small request; grid weight lets it expand
        )
        self.trans_input_text.grid(row=0, column=0, sticky="nsew")

        scroll = tk.Scrollbar(inner, command=self.trans_input_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.trans_input_text.config(yscrollcommand=scroll.set)
        self._bind_context_menu(self.trans_input_text)
        self.trans_border = border # Reference to hide it later

        self._build_trans_save_row(parent, row=3)

        prov_frame = tk.Frame(parent, bg=self.bg_color)
        prov_frame.grid(row=4, column=0, sticky="ew", pady=(0, 10), padx=10)
        self._build_transcription_settings(prov_frame)

        self._build_trans_concurrent_row(parent, row=5)
        self._build_trans_cookie_row(parent, row=6)
        self._build_trans_button_row(parent, row=7)

    def _build_sync_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1, minsize=160)

        tk.Label(
            parent, text="Subtitle Sync (Whisper AI)", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 22, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(20, 2), padx=10)

        tk.Label(
            parent,
            text="Re-time existing .srt files against the voiceover (Dub folder) or the "
                 "downloaded video. Picks the Dub track when available. Saves a new "
                 "“ (Synced).srt” and keeps the original.",
            bg=self.bg_color, fg="#86868B", font=(self.font_family, 10), justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(0, 8), padx=10)

        # Selection area (listbox + controls) — hidden and replaced by the
        # progress manager while a sync runs, like the other tabs' input frames.
        self.sync_select_frame = tk.Frame(parent, bg=self.bg_color)
        self.sync_select_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 12), padx=10)
        self.sync_select_frame.grid_columnconfigure(0, weight=1)
        self.sync_select_frame.grid_rowconfigure(1, weight=1)

        controls = tk.Frame(self.sync_select_frame, bg=self.bg_color)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        controls.grid_columnconfigure(2, weight=1)

        self.sync_scan_btn = RoundedButton(
            controls, text="↻ Rescan", command=self._scan_sync_items,
            radius=14, bg_color=self.gray_bg, hover_color=self.gray_hover,
            text_color=self.text_color, font=(self.font_family, 10, "bold"),
            width=100, height=32,
        )
        self.sync_scan_btn.grid(row=0, column=0, sticky="w", padx=(0, 6))

        self.sync_select_all_btn = RoundedButton(
            controls, text="Select All", command=self._sync_select_all,
            radius=14, bg_color=self.gray_bg, hover_color=self.gray_hover,
            text_color=self.text_color, font=(self.font_family, 10, "bold"),
            width=100, height=32,
        )
        self.sync_select_all_btn.grid(row=0, column=1, sticky="w")

        self.sync_count_label = tk.Label(
            controls, text="", bg=self.bg_color, fg="#86868B",
            font=(self.font_family, 10), anchor="e",
        )
        self.sync_count_label.grid(row=0, column=2, sticky="e")

        # Bordered listbox
        border = tk.Frame(self.sync_select_frame, bg="#D2D2D7")
        border.grid(row=1, column=0, sticky="nsew")
        border.grid_columnconfigure(0, weight=1)
        border.grid_rowconfigure(0, weight=1)

        self.sync_listbox = tk.Listbox(
            border, selectmode=tk.EXTENDED, activestyle="none",
            font=(self.font_family, 10), bg="white", fg=self.text_color,
            selectbackground=self.accent_color, selectforeground="white",
            relief=tk.FLAT, bd=0, highlightthickness=0,
        )
        self.sync_listbox.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        lb_scroll = tk.Scrollbar(border, command=self.sync_listbox.yview)
        lb_scroll.grid(row=0, column=1, sticky="ns")
        self.sync_listbox.config(yscrollcommand=lb_scroll.set)

        opts = tk.Frame(self.sync_select_frame, bg=self.bg_color)
        opts.grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.sync_fill_gaps_var = tk.BooleanVar(value=self.config.get("sync_fill_gaps", True))
        ModernCheckbutton(
            opts, text="Keep each line on screen until the next (fill silence)",
            variable=self.sync_fill_gaps_var, bg_color=self.bg_color,
            command=self._save_config,
        ).pack(side=tk.LEFT)

        tk.Label(opts, text="Accuracy:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).pack(side=tk.LEFT, padx=(18, 6))
        self.sync_model_var = tk.StringVar(value=self.config.get("sync_model", "small"))
        ttk.Combobox(
            opts, textvariable=self.sync_model_var, state="readonly", width=10,
            values=["base", "small", "medium", "large-v3"], font=(self.font_family, 10),
        ).pack(side=tk.LEFT)
        tk.Label(opts, text="(bigger = more accurate, slower on CPU)", bg=self.bg_color,
                 fg="#86868B", font=(self.font_family, 9)).pack(side=tk.LEFT, padx=(6, 0))
        self.sync_model_var.trace_add("write", lambda *_: self._save_config())

        self._build_sync_button_row(parent, row=3)

    def _build_sync_button_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(8, 16), padx=10)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)

        self.sync_btn = RoundedButton(
            frame, text="🔄  Start Sync", command=self.start_sync,
            radius=22, bg_color=self.accent_color, hover_color=self.accent_hover,
            text_color="white", font=(self.font_family, 12, "bold"), height=46,
        )
        self.sync_btn.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.sync_cancel_btn = RoundedButton(
            frame, text="Cancel", command=self.cancel_download,
            radius=22, bg_color="#FF3B30", hover_color="#D70A01",
            text_color="white", font=(self.font_family, 12, "bold"), height=46,
        )
        self.sync_cancel_btn.grid(row=0, column=1, sticky="ew")
        self.sync_cancel_btn.config_state("disabled", bg="#E5E5EA")

    def _build_trans_cookie_row(self, parent: tk.Frame, row: int) -> None:
        # Cookie row for transcription (Now tab-specific)
        cookie_frame = tk.Frame(parent, bg=self.bg_color)
        cookie_frame.grid(row=row, column=0, sticky="ew", pady=(0, 12), padx=10)
        self.trans_use_browser_cookies = tk.BooleanVar(value=self.config.get("trans_use_browser_cookies", False))
        ModernCheckbutton(
            cookie_frame, text="Use Chrome Cookies (Bypass Bot Detection)",
            variable=self.trans_use_browser_cookies, bg_color=self.bg_color,
            command=self._save_config,
        ).pack(side=tk.LEFT)


    def _bind_context_menu(self, widget: tk.Text) -> None:
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Cut", command=lambda: widget.event_generate("<<Cut>>"))
        menu.add_command(label="Copy", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="Paste", command=lambda: widget.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="Select All", command=lambda: widget.tag_add(tk.SEL, "1.0", "end-1c"))
        
        def show_menu(event):
            widget.focus_set()
            menu.tk_popup(event.x_root, event.y_root)
        
        widget.bind("<Button-3>", show_menu)

        # Keyboard shortcuts. We dispatch on the physical key (keycode) in
        # addition to the keysym so Cut/Copy/Paste/Select-All keep working under
        # non-Latin keyboard layouts (e.g. Persian), where Ctrl+C does not emit
        # the "c" keysym and the default English bindings silently fail.
        # Returning "break" stops Tk's default class binding from also firing.
        def handle_shortcut(event):
            if not (event.state & 0x4):  # Control held (mask is 0x4 on Win/X11)
                return None
            key = (event.keysym or "").lower()
            code = event.keycode  # Windows virtual key codes (layout-independent)
            if key == "c" or code == 67:        # C
                widget.event_generate("<<Copy>>")
            elif key == "v" or code == 86:      # V
                widget.event_generate("<<Paste>>")
            elif key == "x" or code == 88:      # X
                widget.event_generate("<<Cut>>")
            elif key == "a" or code == 65:      # A
                widget.tag_add(tk.SEL, "1.0", "end-1c")
                widget.mark_set(tk.INSERT, "1.0")
            elif key == "z" or code == 90:      # Z
                widget.event_generate("<<Undo>>")
            elif key == "y" or code == 89:      # Y
                widget.event_generate("<<Redo>>")
            else:
                return None
            return "break"

        widget.bind("<Control-KeyPress>", handle_shortcut)

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
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 6), padx=10)
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

    def _build_trans_save_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 6), padx=10)
        frame.grid_columnconfigure(1, weight=1)

        tk.Label(
            frame, text="Save Transcriptions to:", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))

        self.trans_dir_label = tk.Label(
            frame, text=str(self.trans_dir), bg=self.bg_color, fg="#5E5CE6",
            font=(self.font_family, 10), anchor="w",
        )
        self.trans_dir_label.grid(row=0, column=1, sticky="ew")
        self.trans_dir_label.bind("<Configure>", lambda e: self.trans_dir_label.config(
            text=self._truncate(str(self.trans_dir), e.width, (self.font_family, 10))
        ))

        self.trans_browse_btn = RoundedButton(
            frame, text="Browse…", command=self.browse_trans_directory,
            radius=14, bg_color=self.gray_bg, hover_color=self.gray_hover,
            text_color=self.text_color, font=(self.font_family, 10, "bold"),
            width=90, height=32,
        )
        self.trans_browse_btn.grid(row=0, column=2, sticky="e", padx=(6, 0))

    def _build_dl_quality_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 6), padx=10)

        tk.Label(
            frame, text="Max Quality:", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 10, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 8))

        self.max_quality_var = tk.StringVar(
            value=self.config.get("max_quality", "Default")
        )
        # Fall back to "Default" if a stale/unknown value was persisted.
        if self.max_quality_var.get() not in self.QUALITY_HEIGHTS:
            self.max_quality_var.set("Default")
        ttk.Combobox(
            frame, textvariable=self.max_quality_var,
            values=list(self.QUALITY_HEIGHTS.keys()),
            state="readonly", font=(self.font_family, 10), width=16,
        ).pack(side=tk.LEFT, padx=(0, 12))

        tk.Label(
            frame, text="Default = best H.264 (up to 1080p). Lower caps the height.",
            bg=self.bg_color, fg="#86868B", font=(self.font_family, 9),
        ).pack(side=tk.LEFT)

        self.max_quality_var.trace_add("write", lambda *_: self._save_config())

    def _build_dl_options_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 6), padx=10)

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

        # Optional styled .ttml export alongside every generated .srt.
        self.export_ttml_var = tk.BooleanVar(value=self.config.get("export_ttml", False))
        ModernCheckbutton(
            frame, text="Also export styled .ttml",
            variable=self.export_ttml_var, bg_color=self.bg_color,
            command=self._save_config, canvas_width=190,
        ).pack(side=tk.LEFT, padx=(16, 0))

        RoundedButton(
            frame, text="Caption Style…", command=self._open_caption_style_dialog,
            radius=14, bg_color=self.gray_bg, hover_color=self.gray_hover,
            text_color=self.text_color, font=(self.font_family, 10, "bold"),
            width=120, height=32,
        ).pack(side=tk.LEFT, padx=(6, 0))

    def _build_dl_concurrent_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 6), padx=10)

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
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 6), padx=10)
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


    # ------------------------------------------------------------------
    # Voiceover tab
    # ------------------------------------------------------------------
    def _build_vo_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)  # settings card / manager area

        tk.Label(
            parent, text="Voiceover Dubbing", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 22, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(20, 2), padx=10)

        tk.Label(
            parent,
            text="Renames each clip to its spoken title, shortens silences, then "
                 "converts the voice with RVC into the Dub folder.",
            bg=self.bg_color, fg="#86868B", font=(self.font_family, 10),
            justify="left", wraplength=760,
        ).grid(row=1, column=0, sticky="w", pady=(0, 10), padx=10)

        # All controls live in a card we hide while a batch runs (the progress
        # manager takes its place, like the other tabs do with their input box).
        self.vo_settings_frame = tk.Frame(parent, bg=self.bg_color)
        self.vo_settings_frame.grid(row=2, column=0, sticky="nsew")
        self.vo_settings_frame.grid_columnconfigure(0, weight=1)

        self.vo_voice_vars: Dict[str, Dict[str, tk.Variable]] = {}

        self._build_vo_folder_rows(self.vo_settings_frame)
        self._build_vo_options_row(self.vo_settings_frame)
        self._build_vo_silence_row(self.vo_settings_frame)
        self._build_vo_voices_row(self.vo_settings_frame)

        self._build_vo_button_row(parent, row=3)

    def _build_vo_folder_rows(self, parent: tk.Frame) -> None:
        # Sources: any mix of folders and .zip archives. All matching clips are
        # gathered across every entry and dubbed into the one Dub folder.
        src = tk.Frame(parent, bg=self.bg_color)
        src.pack(fill="x", padx=10, pady=(0, 6))
        src.grid_columnconfigure(0, weight=1)
        tk.Label(src, text="Source Folders / Zips:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).grid(row=0, column=0, sticky="w", columnspan=2)

        lb_border = tk.Frame(src, bg="#D2D2D7")
        lb_border.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        lb_border.grid_columnconfigure(0, weight=1)
        self.vo_sources_list = tk.Listbox(
            lb_border, height=3, font=(self.font_family, 9), bg="white",
            fg=self.text_color, relief=tk.FLAT, highlightthickness=0,
            activestyle="none", selectmode=tk.EXTENDED,
        )
        self.vo_sources_list.grid(row=0, column=0, sticky="ew", padx=1, pady=1)
        lb_scroll = tk.Scrollbar(lb_border, command=self.vo_sources_list.yview)
        lb_scroll.grid(row=0, column=1, sticky="ns")
        self.vo_sources_list.config(yscrollcommand=lb_scroll.set)

        btns = tk.Frame(src, bg=self.bg_color)
        btns.grid(row=1, column=1, sticky="n", padx=(6, 0))
        for text, cmd in (
            ("Add Folder", self.vo_add_folder),
            ("Add Zip(s)", self.vo_add_zips),
            ("Remove", self.vo_remove_sources),
            ("Clear", self.vo_clear_sources),
        ):
            RoundedButton(
                btns, text=text, command=cmd, radius=12,
                bg_color=self.gray_bg, hover_color=self.gray_hover, text_color=self.text_color,
                font=(self.font_family, 9, "bold"), width=104, height=28,
            ).pack(fill="x", pady=(0, 4))
        self._refresh_vo_sources()

        # Dub (output) folder - shares config with the Downloads tab.
        dub = tk.Frame(parent, bg=self.bg_color)
        dub.pack(fill="x", padx=10, pady=(0, 6))
        dub.grid_columnconfigure(1, weight=1)
        tk.Label(dub, text="Dub Folder:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.vo_dub_label = tk.Label(
            dub, text=self.dub_dir or "Not Selected", bg=self.bg_color,
            fg="#5E5CE6", font=(self.font_family, 10), anchor="w",
        )
        self.vo_dub_label.grid(row=0, column=1, sticky="ew")
        RoundedButton(
            dub, text="Browse…", command=self.browse_vo_dub, radius=14,
            bg_color=self.gray_bg, hover_color=self.gray_hover, text_color=self.text_color,
            font=(self.font_family, 10, "bold"), width=90, height=32,
        ).grid(row=0, column=2, sticky="e", padx=(6, 0))

        # RVC (Mangio) folder - auto-detected, but browsable for other machines.
        rvc = tk.Frame(parent, bg=self.bg_color)
        rvc.pack(fill="x", padx=10, pady=(0, 6))
        rvc.grid_columnconfigure(1, weight=1)
        tk.Label(rvc, text="RVC Folder:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.rvc_dir_label = tk.Label(
            rvc, text=self.rvc_dir or "Not found — click Browse", bg=self.bg_color,
            fg="#5E5CE6" if self.rvc_dir else "#FF3B30", font=(self.font_family, 10), anchor="w",
        )
        self.rvc_dir_label.grid(row=0, column=1, sticky="ew")
        RoundedButton(
            rvc, text="Browse…", command=self.browse_rvc_dir, radius=14,
            bg_color=self.gray_bg, hover_color=self.gray_hover, text_color=self.text_color,
            font=(self.font_family, 10, "bold"), width=90, height=32,
        ).grid(row=0, column=2, sticky="e", padx=(6, 0))

    def _build_vo_options_row(self, parent: tk.Frame) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.pack(fill="x", padx=10, pady=(2, 6))

        tk.Label(frame, text="Process:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).pack(side=tk.LEFT, padx=(0, 6))
        self.vo_process_var = tk.StringVar(value=self.config.get("vo_process", "Both"))
        ttk.Combobox(
            frame, textvariable=self.vo_process_var, state="readonly", width=12,
            values=["Both", "Uptin only", "Pat only"], font=(self.font_family, 10),
        ).pack(side=tk.LEFT)
        self.vo_process_var.trace_add("write", lambda *_: self._save_config())

        tk.Label(frame, text="Device:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).pack(side=tk.LEFT, padx=(18, 6))
        self.rvc_device_var = tk.StringVar(value=self.config.get("rvc_device", "Auto"))
        ttk.Combobox(
            frame, textvariable=self.rvc_device_var, state="readonly", width=8,
            values=["Auto", "cuda:0", "cpu"], font=(self.font_family, 10),
        ).pack(side=tk.LEFT)
        self.rvc_device_var.trace_add("write", lambda *_: self._save_config())

        tk.Label(frame, text="Title from first", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).pack(side=tk.LEFT, padx=(18, 6))
        self.vo_title_seconds_var = tk.IntVar(value=self.config.get("vo_title_seconds", 5))
        tk.Spinbox(frame, from_=1, to=30, increment=1, width=4,
                   textvariable=self.vo_title_seconds_var, font=(self.font_family, 10),
                   command=self._save_config).pack(side=tk.LEFT)
        tk.Label(frame, text="s (English)", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10)).pack(side=tk.LEFT, padx=(4, 0))
        self.vo_title_seconds_var.trace_add("write", lambda *_: self._save_config())

        tk.Label(
            frame, text="(Transcription uses the provider/key set on the Transcription tab)",
            bg=self.bg_color, fg="#86868B", font=(self.font_family, 9),
        ).pack(side=tk.LEFT, padx=(16, 0))

    def _build_vo_silence_row(self, parent: tk.Frame) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.pack(fill="x", padx=10, pady=(2, 8))

        tk.Label(frame, text="Silence —  shorten pauses over",
                 bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).pack(side=tk.LEFT, padx=(0, 6))

        self.vo_silence_thresh_var = tk.DoubleVar(value=self.config.get("vo_silence_threshold", 0.1))
        tk.Spinbox(frame, from_=0.02, to=2.0, increment=0.01, width=5,
                   textvariable=self.vo_silence_thresh_var, font=(self.font_family, 10),
                   command=self._save_config).pack(side=tk.LEFT)
        tk.Label(frame, text="s   down to", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10)).pack(side=tk.LEFT, padx=4)

        self.vo_silence_target_var = tk.DoubleVar(value=self.config.get("vo_silence_target", 0.07))
        tk.Spinbox(frame, from_=0.0, to=1.0, increment=0.01, width=5,
                   textvariable=self.vo_silence_target_var, font=(self.font_family, 10),
                   command=self._save_config).pack(side=tk.LEFT)
        tk.Label(frame, text="s    Noise floor", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10)).pack(side=tk.LEFT, padx=4)

        self.vo_noise_db_var = tk.IntVar(value=self.config.get("vo_silence_noise_db", -30))
        tk.Spinbox(frame, from_=-60, to=-10, increment=1, width=5,
                   textvariable=self.vo_noise_db_var, font=(self.font_family, 10),
                   command=self._save_config).pack(side=tk.LEFT)
        tk.Label(frame, text="dB    Keep", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10)).pack(side=tk.LEFT, padx=(4, 4))

        self.vo_silence_pad_var = tk.IntVar(value=self.config.get("vo_silence_pad_ms", 40))
        tk.Spinbox(frame, from_=0, to=300, increment=5, width=5,
                   textvariable=self.vo_silence_pad_var, font=(self.font_family, 10),
                   command=self._save_config).pack(side=tk.LEFT)
        tk.Label(frame, text="ms around speech", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10)).pack(side=tk.LEFT, padx=(4, 0))

        for var in (self.vo_silence_thresh_var, self.vo_silence_target_var,
                    self.vo_noise_db_var, self.vo_silence_pad_var):
            var.trace_add("write", lambda *_: self._save_config())

    def _build_vo_voices_row(self, parent: tk.Frame) -> None:
        wrap = tk.Frame(parent, bg=self.bg_color)
        wrap.pack(fill="x", padx=10, pady=(2, 8))
        wrap.grid_columnconfigure(0, weight=1, uniform="voice")
        wrap.grid_columnconfigure(1, weight=1, uniform="voice")
        self._build_vo_voice_box(wrap, "uptin", "Uptin", col=0)
        self._build_vo_voice_box(wrap, "pat", "Pat", col=1)

    def _build_vo_voice_box(self, parent: tk.Frame, voice: str, label: str, col: int) -> None:
        cfg = self.config.get(f"rvc_{voice}", {}) or {}
        box = tk.Frame(parent, bg="white", highlightbackground="#D2D2D7", highlightthickness=1)
        box.grid(row=0, column=col, sticky="nsew", padx=(0, 6) if col == 0 else (6, 0))

        tk.Label(box, text=f"{label} voice  (RVC)", bg="white", fg=self.text_color,
                 font=(self.font_family, 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=10, pady=(8, 4))

        pitch_var = tk.IntVar(value=int(cfg.get("pitch", -2)))
        index_var = tk.DoubleVar(value=float(cfg.get("index_rate", 0)))
        f0_var = tk.StringVar(value=cfg.get("f0method", "rmvpe"))
        rms_var = tk.DoubleVar(value=float(cfg.get("rms_mix_rate", 1)))
        protect_var = tk.DoubleVar(value=float(cfg.get("protect", 0.33)))
        self.vo_voice_vars[voice] = {
            "pitch": pitch_var, "index_rate": index_var, "f0method": f0_var,
            "rms_mix_rate": rms_var, "protect": protect_var,
        }

        def _field(r: int, text: str, widget: tk.Widget) -> None:
            tk.Label(box, text=text, bg="white", fg=self.text_color,
                     font=(self.font_family, 10)).grid(row=r, column=0, sticky="w", padx=10, pady=2)
            widget.grid(row=r, column=1, sticky="w", padx=(0, 10), pady=2)

        _field(1, "Pitch (semitones)", tk.Spinbox(
            box, from_=-24, to=24, increment=1, width=6, textvariable=pitch_var,
            font=(self.font_family, 10), command=self._save_config))
        _field(2, "Index rate", tk.Spinbox(
            box, from_=0.0, to=1.0, increment=0.05, width=6, textvariable=index_var,
            font=(self.font_family, 10), command=self._save_config))
        _field(3, "f0 method", ttk.Combobox(
            box, textvariable=f0_var, state="readonly", width=8,
            values=["rmvpe", "harvest", "crepe", "pm"], font=(self.font_family, 10)))
        _field(4, "Volume envelope", tk.Spinbox(
            box, from_=0.0, to=1.0, increment=0.05, width=6, textvariable=rms_var,
            font=(self.font_family, 10), command=self._save_config))
        _field(5, "Protect", tk.Spinbox(
            box, from_=0.0, to=0.5, increment=0.01, width=6, textvariable=protect_var,
            font=(self.font_family, 10), command=self._save_config))
        box.grid_rowconfigure(6, minsize=8)

        for var in (pitch_var, index_var, f0_var, rms_var, protect_var):
            var.trace_add("write", lambda *_: self._save_config())

    def _build_vo_button_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(8, 16), padx=10)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)

        self.vo_btn = RoundedButton(
            frame, text="🎚  Start Voiceover", command=self.start_voiceover,
            radius=22, bg_color=self.accent_color, hover_color=self.accent_hover,
            text_color="white", font=(self.font_family, 12, "bold"), height=46,
        )
        self.vo_btn.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.vo_cancel_btn = RoundedButton(
            frame, text="Cancel", command=self.cancel_download,
            radius=22, bg_color="#FF3B30", hover_color="#D70A01",
            text_color="white", font=(self.font_family, 12, "bold"), height=46,
        )
        self.vo_cancel_btn.grid(row=0, column=1, sticky="ew")
        self.vo_cancel_btn.config_state("disabled", bg="#E5E5EA")

    def _refresh_vo_sources(self) -> None:
        """Repaint the sources listbox from ``self.vo_sources``."""
        self.vo_sources_list.delete(0, tk.END)
        for path in self.vo_sources:
            tag = "📦 " if path.lower().endswith(".zip") else "📁 "
            self.vo_sources_list.insert(tk.END, tag + path)

    def _add_vo_sources(self, paths: Sequence[str]) -> None:
        """Append new, de-duplicated inputs and persist."""
        added = False
        for p in paths:
            norm = os.path.normpath(p)
            if norm and norm not in self.vo_sources:
                self.vo_sources.append(norm)
                added = True
        if added:
            self._refresh_vo_sources()
            self._save_config()

    def vo_add_folder(self) -> None:
        selected = filedialog.askdirectory(
            initialdir=self.vo_source_dir or os.path.expanduser("~"),
            title="Select Folder of Raw Voiceover Clips",
        )
        if selected:
            self.vo_source_dir = os.path.normpath(selected)  # remembered for next time
            self._add_vo_sources([selected])

    def vo_add_zips(self) -> None:
        selected = filedialog.askopenfilenames(
            initialdir=self.vo_source_dir or os.path.expanduser("~"),
            title="Select Zip Archive(s) of Voiceover Clips",
            filetypes=[("Zip archives", "*.zip"), ("All files", "*.*")],
        )
        if selected:
            self._add_vo_sources(list(selected))

    def vo_remove_sources(self) -> None:
        for i in sorted(self.vo_sources_list.curselection(), reverse=True):
            if 0 <= i < len(self.vo_sources):
                del self.vo_sources[i]
        self._refresh_vo_sources()
        self._save_config()

    def vo_clear_sources(self) -> None:
        if self.vo_sources:
            self.vo_sources = []
            self._refresh_vo_sources()
            self._save_config()

    def browse_vo_dub(self) -> None:
        selected = filedialog.askdirectory(
            initialdir=self.dub_dir or os.path.expanduser("~"),
            title="Select Dub (Output) Folder",
        )
        if selected:
            self.dub_dir = os.path.normpath(selected)
            self.vo_dub_label.config(text=self.dub_dir)
            if hasattr(self, "dub_dir_label"):
                self.dub_dir_label.config(text=self.dub_dir)
            self._save_config()

    def browse_rvc_dir(self) -> None:
        selected = filedialog.askdirectory(
            initialdir=self.rvc_dir or os.path.expanduser("~"),
            title="Select the Mangio-RVC Folder",
        )
        if not selected:
            return
        if voiceover.is_rvc_dir(selected):
            self.rvc_dir = os.path.normpath(selected)
            self.rvc_dir_label.config(text=self.rvc_dir, fg="#5E5CE6")
            self._save_config()
            self.log(f"-> RVC folder set: {self.rvc_dir}")
        else:
            self.log("[!] That folder doesn't look like a Mangio-RVC install "
                     "(needs runtime\\python.exe and vc_infer_pipeline.py).")

    def _resolve_rvc_dir(self) -> Optional[Path]:
        """Return a valid RVC folder, re-detecting and persisting it if needed."""
        found = voiceover.find_rvc_dir(self.rvc_dir)
        if found:
            self.rvc_dir = str(found)
            if hasattr(self, "rvc_dir_label"):
                self.rvc_dir_label.config(text=self.rvc_dir, fg="#5E5CE6")
            self._save_config()
        return found

    def _vo_rvc_settings(self, voice: str) -> "voiceover.RvcSettings":
        """Build an RvcSettings from the UI fields, keeping advanced params from config."""
        base = self.config.get(f"rvc_{voice}", {}) or {}
        v = self.vo_voice_vars[voice]
        return voiceover.RvcSettings(
            pitch=int(v["pitch"].get()),
            index_rate=float(v["index_rate"].get()),
            f0method=v["f0method"].get(),
            protect=float(v["protect"].get()),
            rms_mix_rate=float(v["rms_mix_rate"].get()),
            filter_radius=int(base.get("filter_radius", 3)),
            resample_sr=int(base.get("resample_sr", 0)),
        )

    def _review_state_active(self) -> bool:
        """True if a tab is sitting in its post-batch "Finish & Return" review
        state. The shared ``self.downloading`` flag stays True then, but nothing
        is actually running — so another tab's Start can safely clear it."""
        for attr in ("download_btn", "trans_btn", "sync_btn", "vo_btn", "cap_btn"):
            btn = getattr(self, attr, None)
            if btn is not None and getattr(btn, "text", "") == "Finish & Return":
                return True
        return False

    def start_voiceover(self) -> None:
        """Start button for the Voiceover tab. In the post-batch review state it
        just returns to the start screen, mirroring the other tabs."""
        if self.downloading and self.vo_btn.text == "Finish & Return":
            self.reset_ui()
            return
        if self.downloading:
            # Another tab may just be parked in its "Finish & Return" review
            # state (e.g. a finished transcription batch) — that keeps
            # `downloading` True even though no job is running, which used to
            # make this button silently do nothing. Clear that stale review and
            # continue; only block when a job is genuinely still active.
            if self._review_state_active():
                self.reset_ui()
            else:
                self.log("[!] Voiceover: another job is still running — cancel it "
                         "or let it finish before starting a voiceover.")
                return

        sources = [s for s in self.vo_sources
                   if Path(s).is_dir() or (Path(s).is_file() and s.lower().endswith(".zip"))]
        if not sources:
            self.log("[!] Voiceover: add at least one source folder or .zip first.")
            return
        if not self.dub_dir:
            self.log("[!] Voiceover: select a Dub (output) folder first.")
            return

        rvc_dir = self._resolve_rvc_dir()
        if rvc_dir is None:
            self.log("[!] Voiceover: Mangio-RVC folder not found. Click 'Browse…' next to "
                     "'RVC Folder' to select your Mangio-RVC-v23.7.0 folder.")
            return

        choice = self.vo_process_var.get()
        voices = {"Both": ["uptin", "pat"], "Uptin only": ["uptin"], "Pat only": ["pat"]}.get(
            choice, ["uptin", "pat"])

        specs = voiceover.gather_clip_specs(sources, voices)
        if not specs:
            self.log(f"[!] Voiceover: no matching '{choice}' clips found across "
                     f"{len(sources)} source(s).")
            return

        with self._state_lock:
            self.downloading = True
            self.cancelled = False
        self.cancelled_indices = set()
        self._vo_candidates = specs

        self.vo_btn.config_state("disabled", text="Processing...", bg="#E5E5EA")
        self.vo_cancel_btn.config_state("normal", bg="#FF3B30")
        self.vo_settings_frame.grid_remove()

        titles = [s["name"] for s in specs]
        self.vo_manager = DownloadManager(
            self.vo_tab, titles,
            self._vo_skip_item, self._vo_skip_item, self._vo_skip_item,
            None, self.bg_color, self.text_color, self.accent_color, self.font_family,
        )
        self.vo_manager.grid(row=2, column=0, sticky="nsew", pady=(0, 15))

        threading.Thread(target=self._voiceover_worker, args=(specs,), daemon=True).start()

    def _vo_skip_item(self, index: int) -> None:
        with self._state_lock:
            self.cancelled_indices.add(index)
            if self.vo_manager:
                self.vo_manager.set_item_status(index, "Skipped", "#34C759")

    def _vo_status(self, index: int, text: str, color: str) -> None:
        if self.vo_manager:
            self.root.after(0, self.vo_manager.set_item_status, index, text, color)

    def _make_sync_aligner(self) -> GroqTranscriber:
        """Return the Groq transcriber used to align/caption. Groq only powers
        every Whisper-backed feature now (Sync, Captions, Shorts)."""
        return self._make_groq_transcriber()

    def _make_groq_transcriber(self) -> GroqTranscriber:
        """Build a GroqTranscriber. Groq needs the system/VPN proxy in regions
        where api.groq.com is blocked, so we always honour environment proxies
        (trust_env=True); an explicit proxy URL, if set, takes precedence. The
        'Disable System Proxy' toggle is yt-dlp-only and intentionally not applied
        here."""
        return GroqTranscriber(self.log, self.groq_key_var.get(),
                               proxy=self.proxy_url_var.get().strip())

    def _try_transcribe(self, transcriber, audio_path: str, name: str,
                        attempts: int) -> Optional[str]:
        for attempt in range(1, attempts + 1):
            if self._is_cancelled():
                return None
            transcript = transcriber.transcribe_to_text(
                audio_path, is_cancelled=self._is_cancelled, language="en",
            )
            if transcript:
                return transcript
            if attempt < attempts and not self._is_cancelled():
                self.log(f"-> Transcription attempt {attempt} failed for {name}, retrying…")
                time.sleep(2)
        return None

    def _vo_transcribe_title(self, primary, audio_path: str, name: str,
                             attempts: int = 3) -> Optional[str]:
        """Transcribe the title (forced English) via Groq, with retries."""
        return self._try_transcribe(primary, audio_path, name, attempts)

    def _voiceover_worker(self, specs: List[Dict]) -> None:
        self.log(f"\n--- Starting Voiceover ({len(specs)} clips) ---")
        rvc_dir = Path(self.rvc_dir)
        ffmpeg = str(BASE_PATH / "ffmpeg.exe")
        dub_dir = Path(self.dub_dir)
        dub_dir.mkdir(parents=True, exist_ok=True)

        # A transcriber is only needed for clips that get re-titled; "pat long"
        # clips keep their filename, so an all-"pat long" batch needs none.
        transcriber = None
        if any(not s.get("keep_title") for s in specs):
            transcriber = self._make_groq_transcriber()

        device, is_half = voiceover.resolve_device(rvc_dir, self.config.get("rvc_device", "Auto"))
        self.log(f"-> RVC device: {device} (half precision: {is_half})")

        try:
            thresh = float(self.vo_silence_thresh_var.get())
            target = float(self.vo_silence_target_var.get())
            noise_db = float(self.vo_noise_db_var.get())
            pad = float(self.vo_silence_pad_var.get()) / 1000.0  # ms -> seconds
            title_seconds = float(self.vo_title_seconds_var.get())
        except (ValueError, tk.TclError):
            thresh, target, noise_db, pad, title_seconds = 0.1, 0.07, -30.0, 0.04, 5.0

        temp_root = Path(tempfile.mkdtemp(prefix="vo_"))
        extract_dir = temp_root / "_src"  # zip members are unpacked here on demand
        # Reserve titles already present in the Dub folder so we never clobber.
        used_titles = {p.stem for p in dub_dir.glob("*") if p.is_file()}
        # voice -> list of (index, title, prepared_wav_path)
        prepared: Dict[str, List[Tuple[int, str, Path]]] = {"uptin": [], "pat": []}

        # --- Stage 1: per-clip transcribe + rename + silence shorten ---
        for index, spec in enumerate(specs):
            if self._is_cancelled() or index in self.cancelled_indices:
                continue
            voice, name = spec["voice"], spec["name"]

            # Resolve the clip to a real file (extracting from its zip if needed).
            if spec["kind"] == "zip":
                try:
                    path = voiceover.extract_member(spec["path"], spec["member"], extract_dir)
                except (OSError, KeyError) as e:
                    self.log(f"[!] Could not extract {name} from {Path(spec['path']).name}: {e}")
                    self._vo_status(index, "Failed (Zip)", "#FF3B30")
                    continue
            else:
                path = spec["path"]

            if spec.get("keep_title"):
                # "pat long" clips keep their filename as the title — no transcription.
                title = voiceover.unique_title(Path(name).stem, used_titles)
                self.log(f"-> {name}  (keeping title, {voice})")
            else:
                # Title = the English words spoken in the first few seconds. Transcribe
                # only that opening window, forced to English, so the long Persian body
                # never becomes the filename.
                self._vo_status(index, "Transcribing title…", self.accent_color)
                title_clip = temp_root / voice / "title" / f"{index}.wav"
                if not voiceover.trim_clip(ffmpeg, str(path), str(title_clip), title_seconds, log=self.log):
                    title_clip = path  # fall back to the whole clip if the trim fails

                transcript = self._vo_transcribe_title(transcriber, str(title_clip), name)
                if self._is_cancelled():
                    break
                if not transcript:
                    self.log(f"[!] No transcript for {name} after retries — skipping.")
                    self._vo_status(index, "Failed (Title)", "#FF3B30")
                    continue
                title_text = voiceover.english_title_from_transcript(transcript)
                if not title_text or title_text == "untitled":
                    title_text = Path(name).stem  # keep the original name rather than 'untitled'
                title = voiceover.unique_title(title_text, used_titles)
                self.log(f"-> {name}  →  \"{title}\"  ({voice})")

            self._vo_status(index, "Trimming silence…", self.accent_color)
            in_dir = temp_root / voice / "in"
            prepared_wav = in_dir / f"{title}.wav"
            ok = voiceover.shorten_silences(
                ffmpeg, str(path), str(prepared_wav),
                threshold=thresh, target=target, noise_db=noise_db, pad=pad, log=self.log,
            )
            if ok and prepared_wav.exists():
                prepared[voice].append((index, title, prepared_wav))
                self._vo_status(index, "Queued for voice…", "#FF9F0A")
            else:
                self._vo_status(index, "Failed (Silence)", "#FF3B30")

        # --- Stage 2: RVC conversion, one batch per voice ---
        if device == "cpu":
            self.log("-> Note: running on CPU — voice conversion is slow "
                     "(~1–2 min/clip). Progress updates per file below.")
        for voice, items in prepared.items():
            if not items or self._is_cancelled():
                continue
            in_dir = temp_root / voice / "in"
            out_dir = temp_root / voice / "out"
            total = len(items)
            # Map the prepared wav filename back to its (manager index, title).
            by_file = {f"{title}.wav": (idx, title) for idx, title, _wav in items}
            moved: set = set()
            done_count = [0]

            def on_file(kind: str, fname: str) -> None:
                info = by_file.get(fname)
                if not info:
                    return
                idx, title = info
                if kind == "start":
                    self._vo_status(idx, f"Converting voice… ({done_count[0] + 1}/{total})",
                                    self.accent_color)
                elif kind == "done":
                    src = out_dir / fname
                    if src.exists():
                        try:
                            shutil.move(str(src), str(dub_dir / fname))
                            self._vo_status(idx, "Finished", "#34C759")
                            moved.add(idx)
                        except OSError as e:
                            self.log(f"[!] Could not move {fname} to Dub folder: {e}")
                            self._vo_status(idx, "Failed (Save)", "#FF3B30")
                    else:
                        self._vo_status(idx, "Failed (RVC)", "#FF3B30")
                    done_count[0] += 1
                elif kind == "fail":
                    self._vo_status(idx, "Failed (RVC)", "#FF3B30")
                    done_count[0] += 1

            ok = voiceover.run_rvc(
                rvc_dir, voice, self._vo_rvc_settings(voice), device, is_half,
                str(in_dir), str(out_dir), log=self.log,
                register=lambda p: self._add_active_process(90000, p),
                unregister=lambda p: self._remove_active_process(90000),
                on_file=on_file,
            )

            # Fallback for any items the per-file callback didn't resolve (e.g. an
            # older runner without progress markers, or an early exit).
            for idx, title, _wav in items:
                if idx in moved:
                    continue
                src = out_dir / f"{title}.wav"
                if ok and src.exists():
                    try:
                        shutil.move(str(src), str(dub_dir / f"{title}.wav"))
                        self._vo_status(idx, "Finished", "#34C759")
                    except OSError as e:
                        self.log(f"[!] Could not move {title}.wav to Dub folder: {e}")
                        self._vo_status(idx, "Failed (Save)", "#FF3B30")
                elif not self._is_cancelled():
                    self._vo_status(idx, "Failed (RVC)", "#FF3B30")

        try:
            shutil.rmtree(temp_root, ignore_errors=True)
        except OSError:
            pass

        if self._is_cancelled():
            self.log("\nVoiceover cancelled by user.")
            self.root.after(0, self.reset_ui)
        else:
            self.log("\n--- Voiceover Complete ---")
            self.log(f"Dubs saved in: {dub_dir}")
            self.root.after(0, self._vo_show_done)

    def _vo_show_done(self) -> None:
        """Post-batch review state: Finish & Return, plus jump to the Dub folder."""
        self.vo_btn.config_state("normal", text="Finish & Return", bg="#34C759")
        self.vo_cancel_btn.command = lambda: self._open_folder(Path(self.dub_dir))
        self.vo_cancel_btn.config_state("normal", text="📂  Open Dub Folder", bg=self.accent_color)

    # ------------------------------------------------------------------
    # Auto-Caption tab
    # ------------------------------------------------------------------
    CAPTION_MEDIA_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4a",
                          ".mp3", ".wav", ".ogg", ".flac", ".aac")

    def _build_caption_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1, minsize=160)

        tk.Label(parent, text="Auto Caption", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 22, "bold")).grid(
            row=0, column=0, sticky="w", pady=(20, 2), padx=10)
        tk.Label(
            parent,
            text="Transcribe audio/video and write a fresh .srt next to each file — "
                 "for dubs (or any media) that don't have a subtitle yet. Offline (local Whisper).",
            bg=self.bg_color, fg="#86868B", font=(self.font_family, 10),
            justify="left", wraplength=760,
        ).grid(row=1, column=0, sticky="w", pady=(0, 8), padx=10)

        # Selection area (hidden while a job runs, replaced by the progress manager).
        self.cap_select_frame = tk.Frame(parent, bg=self.bg_color)
        self.cap_select_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 12), padx=10)
        self.cap_select_frame.grid_columnconfigure(0, weight=1)
        self.cap_select_frame.grid_rowconfigure(1, weight=1)

        controls = tk.Frame(self.cap_select_frame, bg=self.bg_color)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        controls.grid_columnconfigure(3, weight=1)
        RoundedButton(
            controls, text="Folder…", command=self.browse_caption_dir, radius=14,
            bg_color=self.gray_bg, hover_color=self.gray_hover, text_color=self.text_color,
            font=(self.font_family, 10, "bold"), width=90, height=32,
        ).grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.cap_scan_btn = RoundedButton(
            controls, text="↻ Rescan", command=self._scan_caption_items, radius=14,
            bg_color=self.gray_bg, hover_color=self.gray_hover, text_color=self.text_color,
            font=(self.font_family, 10, "bold"), width=100, height=32,
        )
        self.cap_scan_btn.grid(row=0, column=1, sticky="w", padx=(0, 6))
        self.cap_overwrite_var = tk.BooleanVar(value=self.config.get("caption_overwrite", False))
        ModernCheckbutton(
            controls, text="Re-caption files that already have .srt",
            variable=self.cap_overwrite_var, bg_color=self.bg_color,
            command=lambda: (self._save_config(), self._scan_caption_items()),
        ).grid(row=0, column=2, sticky="w", padx=(6, 0))
        self.cap_count_label = tk.Label(controls, text="", bg=self.bg_color, fg="#86868B",
                                        font=(self.font_family, 10), anchor="e")
        self.cap_count_label.grid(row=0, column=3, sticky="e")

        border = tk.Frame(self.cap_select_frame, bg="#D2D2D7")
        border.grid(row=1, column=0, sticky="nsew")
        border.grid_columnconfigure(0, weight=1)
        border.grid_rowconfigure(0, weight=1)
        self.cap_listbox = tk.Listbox(
            border, selectmode=tk.EXTENDED, activestyle="none",
            font=(self.font_family, 10), bg="white", fg=self.text_color,
            selectbackground=self.accent_color, selectforeground="white",
            relief=tk.FLAT, bd=0, highlightthickness=0,
        )
        self.cap_listbox.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        cap_scroll = tk.Scrollbar(border, command=self.cap_listbox.yview)
        cap_scroll.grid(row=0, column=1, sticky="ns")
        self.cap_listbox.config(yscrollcommand=cap_scroll.set)

        opts = tk.Frame(self.cap_select_frame, bg=self.bg_color)
        opts.grid(row=2, column=0, sticky="w", pady=(8, 0))
        tk.Label(opts, text="Model:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).pack(side=tk.LEFT, padx=(0, 6))
        self.caption_model_var = tk.StringVar(value=self.config.get("caption_model", "small"))
        ttk.Combobox(opts, textvariable=self.caption_model_var, state="readonly", width=10,
                     values=["base", "small", "medium", "large-v3"],
                     font=(self.font_family, 10)).pack(side=tk.LEFT)
        tk.Label(opts, text="Language:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).pack(side=tk.LEFT, padx=(16, 6))
        self.caption_lang_var = tk.StringVar(value=self.config.get("caption_language", "Auto"))
        ttk.Combobox(opts, textvariable=self.caption_lang_var, state="readonly", width=8,
                     values=["Auto", "fa", "en", "ar", "tr"],
                     font=(self.font_family, 10)).pack(side=tk.LEFT)
        self.caption_fill_gaps_var = tk.BooleanVar(value=self.config.get("caption_fill_gaps", True))
        ModernCheckbutton(opts, text="Fill silence (line stays until next)",
                          variable=self.caption_fill_gaps_var, bg_color=self.bg_color,
                          command=self._save_config).pack(side=tk.LEFT, padx=(16, 0))
        for v in (self.caption_model_var, self.caption_lang_var):
            v.trace_add("write", lambda *_: self._save_config())

        self._build_caption_button_row(parent, row=3)

    def _build_caption_button_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(8, 16), padx=10)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)
        self.cap_btn = RoundedButton(
            frame, text="💬  Generate Captions", command=self.start_captions,
            radius=22, bg_color=self.accent_color, hover_color=self.accent_hover,
            text_color="white", font=(self.font_family, 12, "bold"), height=46,
        )
        self.cap_btn.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.cap_cancel_btn = RoundedButton(
            frame, text="Cancel", command=self.cancel_download,
            radius=22, bg_color="#FF3B30", hover_color="#D70A01",
            text_color="white", font=(self.font_family, 12, "bold"), height=46,
        )
        self.cap_cancel_btn.grid(row=0, column=1, sticky="ew")
        self.cap_cancel_btn.config_state("disabled", bg="#E5E5EA")

    def browse_caption_dir(self) -> None:
        initial = self.caption_dir or self.dub_dir or os.path.expanduser("~")
        selected = filedialog.askdirectory(initialdir=initial,
                                           title="Select Folder to Caption")
        if selected:
            self.caption_dir = os.path.normpath(selected)
            self._save_config()
            self._scan_caption_items()

    def _scan_caption_items(self) -> None:
        if self.downloading:
            return
        self.cap_listbox.delete(0, tk.END)
        self._caption_items = []
        folder = Path(self.caption_dir) if self.caption_dir else None
        if not folder or not folder.is_dir():
            self.cap_count_label.config(text="No folder selected")
            return
        overwrite = self.cap_overwrite_var.get()
        for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_file() or entry.suffix.lower() not in self.CAPTION_MEDIA_EXTS:
                continue
            if not overwrite and entry.with_suffix(".srt").exists():
                continue  # already has a subtitle
            self._caption_items.append(entry)
            self.cap_listbox.insert(tk.END, entry.name)
        n = len(self._caption_items)
        self.cap_count_label.config(
            text=f"{n} file(s) to caption" if n else "Nothing to caption (all have .srt)")

    def start_captions(self) -> None:
        if self.downloading and self.cap_btn.text == "Finish & Return":
            self.reset_ui()
            return
        if self.downloading:
            return
        if not self._caption_items:
            self.log("[!] Captions: nothing to caption — pick a folder and Rescan.")
            return

        items = list(self._caption_items)
        with self._state_lock:
            self.downloading = True
            self.cancelled = False
        self.cancelled_indices = set()

        self.cap_btn.config_state("disabled", text="Captioning...", bg="#E5E5EA")
        self.cap_cancel_btn.config_state("normal", bg="#FF3B30")
        self.cap_select_frame.grid_remove()

        self.caption_manager = DownloadManager(
            self.cap_tab, [p.name for p in items],
            self._cap_skip_item, self._cap_skip_item, self._cap_skip_item,
            None, self.bg_color, self.text_color, self.accent_color, self.font_family,
        )
        self.caption_manager.grid(row=2, column=0, sticky="nsew", pady=(0, 15))
        threading.Thread(target=self._caption_worker, args=(items,), daemon=True).start()

    def _cap_skip_item(self, index: int) -> None:
        with self._state_lock:
            self.cancelled_indices.add(index)
            if self.caption_manager:
                self.caption_manager.set_item_status(index, "Skipped", "#34C759")

    def _cap_status(self, index: int, text: str, color: str) -> None:
        if self.caption_manager:
            self.root.after(0, self.caption_manager.set_item_status, index, text, color)

    def _caption_worker(self, items: List[Path]) -> None:
        self.log(f"\n--- Starting Auto-Caption ({len(items)} file(s)) ---")
        aligner = self._make_groq_transcriber()
        if not self.groq_key_var.get().strip():
            self.log("[!] Captions: set your Groq API key on the Transcription tab first.")
            self.root.after(0, self.reset_ui)
            return

        lang = self.caption_lang_var.get()
        language = None if lang == "Auto" else lang
        fill_gaps = self.caption_fill_gaps_var.get()

        for index, media in enumerate(items):
            if self._is_cancelled() or index in self.cancelled_indices:
                continue
            self._cap_status(index, "Transcribing…", self.accent_color)
            out_srt = media.with_suffix(".srt")
            try:
                ok = aligner.transcribe_to_srt(
                    str(media), out_srt, is_cancelled=self._is_cancelled,
                    language=language, fill_gaps=fill_gaps,
                )
            except Exception as e:
                logger.exception("Auto-caption failed for %s", media.name)
                self.log(f"[!] Caption error for {media.name}: {e}")
                self._cap_status(index, "Failed", "#FF3B30")
                continue
            if self._is_cancelled():
                break
            if ok:
                self.log(f"-> Captioned: {media.name} → {out_srt.name}")
                self._maybe_export_ttml(out_srt)
                self._cap_status(index, "Finished", "#34C759")
            else:
                self._cap_status(index, "Failed (No speech)", "#FF3B30")

        if self._is_cancelled():
            self.log("\nAuto-caption cancelled by user.")
            self.root.after(0, self.reset_ui)
        else:
            self.log("\n--- Auto-Caption Complete ---")
            self.root.after(0, lambda: self.cap_btn.config_state(
                "normal", text="Finish & Return", bg="#34C759"))

    # ------------------------------------------------------------------
    # Short Clips tab
    # ------------------------------------------------------------------
    def _build_shorts_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(3, weight=1, minsize=140)

        tk.Label(parent, text="Short Clips", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 22, "bold")).grid(
            row=0, column=0, sticky="w", pady=(20, 2), padx=10)
        tk.Label(
            parent,
            text="Find the most interesting moments in a long video (from its voice only) "
                 "and render them as vertical 9:16 shorts with burned-in captions.",
            bg=self.bg_color, fg="#86868B", font=(self.font_family, 10),
            justify="left", wraplength=760,
        ).grid(row=1, column=0, sticky="w", pady=(0, 8), padx=10)

        card = tk.Frame(parent, bg=self.bg_color)
        card.grid(row=2, column=0, sticky="ew", padx=10)
        card.grid_columnconfigure(0, weight=1)

        # Video picker
        vid = tk.Frame(card, bg=self.bg_color)
        vid.pack(fill="x", pady=(0, 6))
        vid.grid_columnconfigure(1, weight=1)
        tk.Label(vid, text="Video:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.shorts_video_label = tk.Label(vid, text=self.shorts_video or "Not Selected",
                                           bg=self.bg_color, fg="#5E5CE6",
                                           font=(self.font_family, 10), anchor="w")
        self.shorts_video_label.grid(row=0, column=1, sticky="ew")
        RoundedButton(vid, text="Browse…", command=self.browse_shorts_video, radius=14,
                      bg_color=self.gray_bg, hover_color=self.gray_hover, text_color=self.text_color,
                      font=(self.font_family, 10, "bold"), width=90, height=32).grid(
            row=0, column=2, sticky="e", padx=(6, 0))

        # OpenRouter key + model
        orr = tk.Frame(card, bg=self.bg_color)
        orr.pack(fill="x", pady=(0, 6))
        orr.grid_columnconfigure(1, weight=1)
        tk.Label(orr, text="OpenRouter Key:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.openrouter_key_var = tk.StringVar(value=self.config.get("openrouter_api_key", ""))
        self.openrouter_key_entry = RoundedEntry(orr, variable=self.openrouter_key_var, show="*",
                                                 radius=12, bg_color="white")
        self.openrouter_key_entry.grid(row=0, column=1, sticky="ew")
        self.openrouter_key_entry.entry.bind("<FocusOut>", lambda e: self._save_config())
        tk.Label(orr, text="Model:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).grid(row=0, column=2, sticky="w", padx=(10, 8))
        self.shorts_model_var = tk.StringVar(value=self.config.get("shorts_model", "deepseek/deepseek-v4-pro"))
        model_entry = RoundedEntry(orr, variable=self.shorts_model_var, radius=12,
                                   bg_color="white", width=200)
        model_entry.grid(row=0, column=3, sticky="e")
        model_entry.entry.bind("<FocusOut>", lambda e: self._save_config())

        # Params
        par = tk.Frame(card, bg=self.bg_color)
        par.pack(fill="x", pady=(0, 8))
        tk.Label(par, text="Clips:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).pack(side=tk.LEFT, padx=(0, 4))
        self.shorts_num_var = tk.IntVar(value=self.config.get("shorts_num_clips", 5))
        tk.Spinbox(par, from_=1, to=20, width=4, textvariable=self.shorts_num_var,
                   font=(self.font_family, 10), command=self._save_config).pack(side=tk.LEFT)
        tk.Label(par, text="Length:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).pack(side=tk.LEFT, padx=(14, 4))
        self.shorts_min_var = tk.IntVar(value=self.config.get("shorts_min_dur", 20))
        tk.Spinbox(par, from_=5, to=120, width=4, textvariable=self.shorts_min_var,
                   font=(self.font_family, 10), command=self._save_config).pack(side=tk.LEFT)
        tk.Label(par, text="–", bg=self.bg_color, fg=self.text_color).pack(side=tk.LEFT, padx=2)
        self.shorts_max_var = tk.IntVar(value=self.config.get("shorts_max_dur", 60))
        tk.Spinbox(par, from_=10, to=180, width=4, textvariable=self.shorts_max_var,
                   font=(self.font_family, 10), command=self._save_config).pack(side=tk.LEFT)
        tk.Label(par, text="s", bg=self.bg_color, fg=self.text_color).pack(side=tk.LEFT, padx=(2, 0))
        tk.Label(par, text="Whisper:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).pack(side=tk.LEFT, padx=(14, 4))
        self.shorts_wmodel_var = tk.StringVar(value=self.config.get("shorts_caption_model", "medium"))
        ttk.Combobox(par, textvariable=self.shorts_wmodel_var, state="readonly", width=9,
                     values=["base", "small", "medium", "large-v3"],
                     font=(self.font_family, 10)).pack(side=tk.LEFT)
        tk.Label(par, text="Lang:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).pack(side=tk.LEFT, padx=(14, 4))
        self.shorts_lang_var = tk.StringVar(value=self.config.get("shorts_language", "Persian"))
        ttk.Combobox(par, textvariable=self.shorts_lang_var, state="readonly", width=9,
                     values=["Auto", "Persian", "English"],
                     font=(self.font_family, 10)).pack(side=tk.LEFT)

        # Output mode + caption row
        outrow = tk.Frame(card, bg=self.bg_color)
        outrow.pack(fill="x", pady=(0, 8))
        tk.Label(outrow, text="Output:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10, "bold")).pack(side=tk.LEFT, padx=(0, 4))
        self.shorts_output_var = tk.StringVar(
            value=self.config.get("shorts_output_mode", "Vertical + captions"))
        ttk.Combobox(outrow, textvariable=self.shorts_output_var, state="readonly", width=20,
                     values=["Vertical + captions", "Lossless 16:9 cut"],
                     font=(self.font_family, 10)).pack(side=tk.LEFT)
        self.shorts_burn_var = tk.BooleanVar(value=self.config.get("shorts_burn_captions", True))
        ModernCheckbutton(outrow, text="Burn captions (vertical only)", variable=self.shorts_burn_var,
                          bg_color=self.bg_color, command=self._save_config).pack(side=tk.LEFT, padx=(14, 0))
        tk.Label(outrow, text="Lossless = instant raw cut, no re-encode",
                 bg=self.bg_color, fg="#86868B", font=(self.font_family, 9)).pack(side=tk.LEFT, padx=(14, 0))
        self.shorts_test_btn = RoundedButton(
            outrow, text="Test Connection", command=self.start_shorts_test, radius=14,
            bg_color=self.gray_bg, hover_color=self.gray_hover, text_color=self.text_color,
            font=(self.font_family, 10, "bold"), width=160, height=32)
        self.shorts_test_btn.pack(side=tk.RIGHT)
        for v in (self.shorts_num_var, self.shorts_min_var, self.shorts_max_var,
                  self.shorts_wmodel_var, self.shorts_lang_var, self.shorts_output_var):
            v.trace_add("write", lambda *_: self._save_config())

        # Suggestions list
        border = tk.Frame(parent, bg="#D2D2D7")
        border.grid(row=3, column=0, sticky="nsew", padx=10, pady=(4, 8))
        border.grid_columnconfigure(0, weight=1)
        border.grid_rowconfigure(0, weight=1)
        self.shorts_listbox = tk.Listbox(
            border, selectmode=tk.EXTENDED, activestyle="none",
            font=(self.font_family, 10), bg="white", fg=self.text_color,
            selectbackground=self.accent_color, selectforeground="white",
            relief=tk.FLAT, bd=0, highlightthickness=0,
        )
        self.shorts_listbox.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        s_scroll = tk.Scrollbar(border, command=self.shorts_listbox.yview)
        s_scroll.grid(row=0, column=1, sticky="ns")
        self.shorts_listbox.config(yscrollcommand=s_scroll.set)

        # Buttons
        brow = tk.Frame(parent, bg=self.bg_color)
        brow.grid(row=4, column=0, sticky="ew", pady=(0, 16), padx=10)
        brow.grid_columnconfigure(0, weight=2)
        brow.grid_columnconfigure(1, weight=2)
        brow.grid_columnconfigure(2, weight=1)
        self.shorts_analyze_btn = RoundedButton(
            brow, text="🔎  Analyze", command=self.start_shorts_analyze,
            radius=22, bg_color=self.accent_color, hover_color=self.accent_hover,
            text_color="white", font=(self.font_family, 12, "bold"), height=46)
        self.shorts_analyze_btn.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.shorts_render_btn = RoundedButton(
            brow, text="🎬  Render Selected", command=self.start_shorts_render,
            radius=22, bg_color="#34C759", hover_color="#2BA149",
            text_color="white", font=(self.font_family, 12, "bold"), height=46)
        self.shorts_render_btn.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.shorts_cancel_btn = RoundedButton(
            brow, text="Cancel", command=self.cancel_download,
            radius=22, bg_color="#FF3B30", hover_color="#D70A01",
            text_color="white", font=(self.font_family, 12, "bold"), height=46)
        self.shorts_cancel_btn.grid(row=0, column=2, sticky="ew")
        self.shorts_cancel_btn.config_state("disabled", bg="#E5E5EA")

    def browse_shorts_video(self) -> None:
        sel = filedialog.askopenfilename(
            initialdir=self.config.get("shorts_dir", "") or os.path.expanduser("~"),
            title="Select Long Video",
            filetypes=[("Video", "*.mp4 *.mkv *.mov *.avi *.webm"), ("All files", "*.*")],
        )
        if sel:
            self.shorts_video = os.path.normpath(sel)
            self.shorts_video_label.config(text=self.shorts_video)
            self.config.set("shorts_dir", str(Path(self.shorts_video).parent))
            self._save_config()

    def _shorts_set_busy(self, busy: bool) -> None:
        with self._state_lock:
            self.downloading = busy
            if not busy:
                self.cancelled = False
        self.shorts_analyze_btn.config_state(
            "disabled" if busy else "normal", bg="#E5E5EA" if busy else self.accent_color)
        self.shorts_render_btn.config_state(
            "disabled" if busy else "normal", bg="#E5E5EA" if busy else "#34C759")
        self.shorts_cancel_btn.config_state(
            "normal" if busy else "disabled", bg="#FF3B30" if busy else "#E5E5EA")
        if hasattr(self, "shorts_test_btn"):
            self.shorts_test_btn.config_state(
                "disabled" if busy else "normal", bg="#E5E5EA" if busy else self.gray_bg)

    def start_shorts_test(self) -> None:
        """Probe the OpenRouter connection so the user can tell a network/VPN
        problem apart from an app problem before running a full analysis."""
        if getattr(self, "_shorts_testing", False):
            return
        self._shorts_testing = True
        self.shorts_test_btn.config_state("disabled", bg="#E5E5EA")
        key = self.openrouter_key_var.get().strip()
        threading.Thread(target=self._shorts_test_worker, args=(key,), daemon=True).start()

    def _shorts_test_worker(self, key: str) -> None:
        try:
            self.log("\n--- Testing OpenRouter connection ---")
            shortclips.test_connection(
                key, proxy=self.proxy_url_var.get().strip(), trust_env=True, log=self.log)
        except Exception as e:
            logger.exception("Shorts connection test failed")
            self.log(f"[!] Shorts: connection test error: {e}")
        finally:
            self._shorts_testing = False
            self.root.after(0, lambda: self.shorts_test_btn.config_state(
                "normal", bg=self.gray_bg))

    def start_shorts_analyze(self) -> None:
        if self.downloading:
            return
        if not self.shorts_video or not Path(self.shorts_video).is_file():
            self.log("[!] Shorts: pick a video first.")
            return
        if not self.openrouter_key_var.get().strip():
            self.log("[!] Shorts: enter your OpenRouter API key first.")
            return
        self._save_config()
        self._shorts_set_busy(True)
        threading.Thread(target=self._shorts_analyze_worker,
                         args=(self.shorts_video,), daemon=True).start()

    def _shorts_analyze_worker(self, video: str) -> None:
        try:
            self.log("\n--- Analyzing video for short clips ---")
            aligner = self._make_groq_transcriber()
            if not self.groq_key_var.get().strip():
                self.log("[!] Shorts: set your Groq API key on the Transcription tab first.")
                return
            lang = {"Persian": "fa", "English": "en"}.get(self.shorts_lang_var.get())
            segs = aligner.transcribe_segments(
                str(video), is_cancelled=self._is_cancelled, language=lang)
            if self._is_cancelled():
                return
            if not segs:
                self.log("[!] Shorts: no speech found in the video.")
                return
            self._shorts_segments = segs
            try:
                num = int(self.shorts_num_var.get())
                mn = float(self.shorts_min_var.get())
                mx = float(self.shorts_max_var.get())
            except (ValueError, tk.TclError):
                num, mn, mx = 5, 20.0, 60.0
            clips = shortclips.find_highlights(
                self.openrouter_key_var.get().strip(), self.shorts_model_var.get().strip(),
                segs, num, mn, mx, log=self.log,
                proxy=self.proxy_url_var.get().strip(), trust_env=True,
            )
            self._shorts_clips = clips
            self.root.after(0, self._populate_shorts_list)
        except Exception as e:
            logger.exception("Shorts analyze failed")
            self.log(f"[!] Shorts analyze error: {e}")
        finally:
            self.root.after(0, lambda: self._shorts_set_busy(False))

    def _populate_shorts_list(self) -> None:
        self.shorts_listbox.delete(0, tk.END)
        for i, c in enumerate(self._shorts_clips, 1):
            reason = f"  —  {c['reason']}" if c.get("reason") else ""
            self.shorts_listbox.insert(
                tk.END, f"{i}. [{c['start']:.0f}–{c['end']:.0f}s]  {c['title']}{reason}")
        if self._shorts_clips:
            self.log(f"-> {len(self._shorts_clips)} suggestion(s). Select rows to render "
                     "(or select none = render all), then click Render Selected.")

    def start_shorts_render(self) -> None:
        if self.downloading:
            return
        if not self._shorts_clips:
            self.log("[!] Shorts: analyze a video first.")
            return
        sel = list(self.shorts_listbox.curselection())
        clips = [self._shorts_clips[i] for i in sel] if sel else list(self._shorts_clips)
        self._shorts_set_busy(True)
        threading.Thread(target=self._shorts_render_worker, args=(clips,), daemon=True).start()

    def _shorts_render_worker(self, clips: List[dict]) -> None:
        done = 0
        try:
            self.log(f"\n--- Rendering {len(clips)} short(s) ---")
            ff = str(BASE_PATH / "ffmpeg.exe")
            video = self.shorts_video
            outdir = Path(video).parent / "Shorts"
            outdir.mkdir(parents=True, exist_ok=True)
            lossless = "Lossless" in self.shorts_output_var.get()
            burn = self.shorts_burn_var.get()
            segs = self._shorts_segments if burn else None
            if lossless:
                self.log("-> Lossless 16:9 mode — instant stream-copy cuts (no captions, no re-encode).")
            for i, c in enumerate(clips, 1):
                if self._is_cancelled():
                    break
                out = outdir / f"{i:02d} - {shortclips.safe_name(c['title'])}.mp4"
                if lossless:
                    ok = shortclips.render_cut(
                        ff, video, c["start"], c["end"], out, log=self.log,
                        register=lambda p: self._add_active_process(95000, p),
                        unregister=lambda p: self._remove_active_process(95000),
                    )
                else:
                    ok = shortclips.render_short(
                        ff, video, c["start"], c["end"], out, segments=segs, burn_captions=burn,
                        log=self.log,
                        register=lambda p: self._add_active_process(95000, p),
                        unregister=lambda p: self._remove_active_process(95000),
                    )
                if ok:
                    done += 1
            if self._is_cancelled():
                self.log("\nShorts rendering cancelled by user.")
            else:
                self.log(f"\n--- Done: {done}/{len(clips)} short(s) saved to {outdir} ---")
        except Exception as e:
            logger.exception("Shorts render failed")
            self.log(f"[!] Shorts render error: {e}")
        finally:
            self.root.after(0, lambda: self._shorts_set_busy(False))

    def _build_advanced_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 6), padx=10)

        # First row of advanced options
        row1 = tk.Frame(frame, bg=self.bg_color)
        row1.pack(fill="x")

        self.use_browser_cookies = tk.BooleanVar(value=self.config.get("use_browser_cookies", False))
        ModernCheckbutton(
            row1, text="Use Chrome Cookies (fallback)",
            variable=self.use_browser_cookies, bg_color=self.bg_color,
            command=self._save_config,
        ).pack(side=tk.LEFT)

        # Second row of advanced options
        row2 = tk.Frame(frame, bg=self.bg_color)
        row2.pack(fill="x", pady=(5, 0))

        self.disable_proxy_var = tk.BooleanVar(value=self.config.get("disable_proxy", False))
        ModernCheckbutton(
            row2, text="Disable System Proxy (Fix 127.0.0.1 errors)",
            variable=self.disable_proxy_var, bg_color=self.bg_color,
            command=self._save_config,
        ).pack(side=tk.LEFT)

        self.use_tv_client_var = tk.BooleanVar(value=self.config.get("use_tv_client", False))
        ModernCheckbutton(
            row2, text="TV Client (Bypass Bot detection)",
            variable=self.use_tv_client_var, bg_color=self.bg_color,
            command=self._save_config,
        ).pack(side=tk.LEFT, padx=(15, 0))

        # Third row: explicit proxy (for machines behind a local VPN proxy such
        # as V2RayN / Hiddify, where direct connections fail).
        row3 = tk.Frame(frame, bg=self.bg_color)
        row3.pack(fill="x", pady=(5, 0))
        row3.grid_columnconfigure(1, weight=1)

        tk.Label(
            row3, text="Proxy:", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))

        self.proxy_url_var = tk.StringVar(value=self.config.get("proxy_url", ""))
        self.proxy_entry = RoundedEntry(
            row3, variable=self.proxy_url_var, radius=12, bg_color="white",
        )
        self.proxy_entry.grid(row=0, column=1, sticky="ew")
        self.proxy_entry.entry.bind("<FocusOut>", lambda e: self._save_config())

        tk.Label(
            row3, text="e.g. socks5://127.0.0.1:10808  or  http://127.0.0.1:10809",
            bg=self.bg_color, fg="#86868B", font=(self.font_family, 9),
        ).grid(row=1, column=1, sticky="w", pady=(2, 0))

    def _build_transcription_settings(self, parent: tk.Frame) -> None:
        tk.Label(
            parent, text="Transcription: Groq (whisper-large-v3)", bg=self.bg_color,
            fg=self.text_color, font=(self.font_family, 11, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 10))

        # Groq powers every transcription/alignment feature now; kept as a fixed
        # value so the rest of the code (and config save) still has a provider.
        self.trans_provider_var = tk.StringVar(value="Groq AI (Fastest)")

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

    def _build_trans_concurrent_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(0, 6), padx=10)

        self.trans_concurrent_var = tk.BooleanVar(value=self.config.get("trans_concurrent", False))
        ModernCheckbutton(
            frame, text="Simultaneous Transcriptions",
            variable=self.trans_concurrent_var, bg_color=self.bg_color,
            command=self._save_config,
        ).pack(side=tk.LEFT)

        # Default to 2: TikTok / Instagram start returning 403 once you fan out wider.
        self.trans_max_concurrent_var = tk.IntVar(value=self.config.get("trans_max_concurrent", 2))
        tk.Label(frame, text="Max:", bg=self.bg_color, fg=self.text_color,
                 font=(self.font_family, 10)).pack(side=tk.LEFT, padx=(15, 4))
        self.trans_max_concurrent_spin = tk.Spinbox(
            frame, from_=1, to=20, textvariable=self.trans_max_concurrent_var,
            width=3, font=(self.font_family, 10), command=self._save_config,
        )
        self.trans_max_concurrent_spin.pack(side=tk.LEFT)


    def _build_dl_button_row(self, parent: tk.Frame, row: int) -> None:
        frame = tk.Frame(parent, bg=self.bg_color)
        frame.grid(row=row, column=0, sticky="ew", pady=(6, 8), padx=10)
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

    # ------------------------------------------------------------------
    # Text to SRT tab
    # ------------------------------------------------------------------
    def _build_txt2srt_tab(self, parent: tk.Frame) -> None:
        # Runs during _build_ui, before the per-tab state block, so seed the
        # output folder from config here (defaults to the Downloads folder).
        self.txt2srt_dir = Path(self.config.get("txt2srt_dir", str(self.downloads_dir)))

        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1, minsize=160)

        tk.Label(
            parent, text="Text to SRT", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 22, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(20, 2), padx=10)

        tk.Label(
            parent,
            text="Paste your text — each line becomes one subtitle, timed to a fixed "
                 "2-second interval (0–2s, 2–4s, …). Blank lines are skipped.",
            bg=self.bg_color, fg="#86868B", font=(self.font_family, 10), justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(0, 8), padx=10)

        border = tk.Frame(parent, bg="#D2D2D7")
        border.grid(row=2, column=0, sticky="nsew", pady=(0, 12), padx=10)
        border.grid_columnconfigure(0, weight=1)
        border.grid_rowconfigure(0, weight=1)

        inner = tk.Frame(border, bg="white")
        inner.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        inner.grid_columnconfigure(0, weight=1)
        inner.grid_rowconfigure(0, weight=1)

        self.t2s_input_text = tk.Text(
            inner, wrap=tk.WORD, font=(self.font_family, 11),
            bg="white", fg=self.text_color, insertbackground=self.accent_color,
            relief=tk.FLAT, padx=10, pady=10, undo=True,
            height=6, width=1,  # small request; grid weight lets it expand
        )
        self.t2s_input_text.grid(row=0, column=0, sticky="nsew")

        scroll = tk.Scrollbar(inner, command=self.t2s_input_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.t2s_input_text.config(yscrollcommand=scroll.set)
        self._bind_context_menu(self.t2s_input_text)

        # Save-to folder row
        save_frame = tk.Frame(parent, bg=self.bg_color)
        save_frame.grid(row=3, column=0, sticky="ew", pady=(0, 6), padx=10)
        save_frame.grid_columnconfigure(1, weight=1)

        tk.Label(
            save_frame, text="Save to:", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))

        self.t2s_dir_label = tk.Label(
            save_frame, text=str(self.txt2srt_dir), bg=self.bg_color, fg="#5E5CE6",
            font=(self.font_family, 10), anchor="w",
        )
        self.t2s_dir_label.grid(row=0, column=1, sticky="ew")
        self.t2s_dir_label.bind("<Configure>", lambda e: self.t2s_dir_label.config(
            text=self._truncate(str(self.txt2srt_dir), e.width, (self.font_family, 10))
        ))

        RoundedButton(
            save_frame, text="Browse…", command=self.browse_txt2srt_directory,
            radius=14, bg_color=self.gray_bg, hover_color=self.gray_hover,
            text_color=self.text_color, font=(self.font_family, 10, "bold"),
            width=90, height=32,
        ).grid(row=0, column=2, sticky="e", padx=(6, 0))

        # File name row
        name_frame = tk.Frame(parent, bg=self.bg_color)
        name_frame.grid(row=4, column=0, sticky="ew", pady=(0, 6), padx=10)
        name_frame.grid_columnconfigure(1, weight=1)

        tk.Label(
            name_frame, text="File name:", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 10, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 8))

        self.t2s_name_var = tk.StringVar(value="subtitles")
        name_border = tk.Frame(name_frame, bg="#D2D2D7")
        name_border.grid(row=0, column=1, sticky="ew")
        name_border.grid_columnconfigure(0, weight=1)
        tk.Entry(
            name_border, textvariable=self.t2s_name_var, font=(self.font_family, 11),
            bg="white", fg=self.text_color, insertbackground=self.accent_color,
            relief=tk.FLAT,
        ).grid(row=0, column=0, sticky="ew", padx=1, pady=1, ipady=5)

        tk.Label(
            name_frame, text=".srt", bg=self.bg_color, fg="#86868B",
            font=(self.font_family, 10),
        ).grid(row=0, column=2, sticky="w", padx=(6, 0))

        # Generate button
        btn_frame = tk.Frame(parent, bg=self.bg_color)
        btn_frame.grid(row=5, column=0, sticky="ew", pady=(8, 16), padx=10)
        btn_frame.grid_columnconfigure(0, weight=1)
        RoundedButton(
            btn_frame, text="📝  Generate SRT", command=self.generate_txt2srt,
            radius=22, bg_color=self.accent_color, hover_color=self.accent_hover,
            text_color="white", font=(self.font_family, 12, "bold"), height=46,
        ).grid(row=0, column=0, sticky="ew")

    def browse_txt2srt_directory(self) -> None:
        selected = filedialog.askdirectory(
            initialdir=str(self.txt2srt_dir), title="Select SRT Save Location",
        )
        if selected:
            self.txt2srt_dir = Path(selected)
            self.t2s_dir_label.config(text=str(self.txt2srt_dir))
            self._save_config()

    def generate_txt2srt(self) -> None:
        raw = self.t2s_input_text.get("1.0", tk.END)
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if not lines:
            self.log("[!] Text to SRT: paste some text first (one line per subtitle).")
            messagebox.showinfo("Text to SRT", "Please paste some text first — "
                                "each non-empty line becomes one subtitle.")
            return

        name = sanitize_filename(self.t2s_name_var.get().strip() or "subtitles")
        if name.lower().endswith(".srt"):
            name = name[:-4]
        try:
            self.txt2srt_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.log(f"[!] Text to SRT: cannot create folder: {e}")
            messagebox.showerror("Text to SRT", f"Could not create folder:\n{e}")
            return

        srt_path = self.txt2srt_dir / f"{name}.srt"
        if generate_standard_srt(lines, srt_path, self.log):
            self.log(f"-> Saved: {srt_path}")
            messagebox.showinfo(
                "Text to SRT",
                f"Created {len(lines)} subtitles (2s each).\n\nSaved to:\n{srt_path}",
            )

    def _build_log_area(self, parent: tk.Frame) -> None:
        # Dark terminal log box with rounded border simulation
        outer = tk.Frame(parent, bg="#3A3A3C", pady=2, padx=2)
        outer.grid(row=10, column=0, sticky="ew", pady=(8, 0), padx=0)
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(0, weight=1)

        log_inner = tk.Frame(outer, bg="#1D1D1F")
        log_inner.grid(row=0, column=0, sticky="nsew")
        log_inner.grid_columnconfigure(0, weight=1)
        log_inner.grid_rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_inner, wrap=tk.WORD, height=5, font=("Consolas", 10),
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
        self.config.set("trans_dir", str(self.trans_dir))
        if hasattr(self, "txt2srt_dir"):
            self.config.set("txt2srt_dir", str(self.txt2srt_dir))
        self.config.set("dub_dir", self.dub_dir)
        self.config.set("concurrent_downloads", self.concurrent_var.get())
        self.config.set("max_concurrent", self.max_concurrent_var.get())
        self.config.set("max_quality", self.max_quality_var.get())
        self.config.set("use_browser_cookies", self.use_browser_cookies.get())
        self.config.set("disable_proxy", self.disable_proxy_var.get())
        self.config.set("use_tv_client", self.use_tv_client_var.get())
        self.config.set("proxy_url", self.proxy_url_var.get().strip())
        self.config.set("transcription_provider", self.trans_provider_var.get())
        self.config.set("groq_api_key", self.groq_key_var.get())
        self.config.set("trans_concurrent", self.trans_concurrent_var.get())
        self.config.set("trans_max_concurrent", self.trans_max_concurrent_var.get())
        self.config.set("trans_use_browser_cookies", self.trans_use_browser_cookies.get())
        self.config.set("export_ttml", self.export_ttml_var.get())
        if hasattr(self, "sync_fill_gaps_var"):
            self.config.set("sync_fill_gaps", self.sync_fill_gaps_var.get())
        if hasattr(self, "sync_model_var"):
            self.config.set("sync_model", self.sync_model_var.get())
        if hasattr(self, "caption_model_var"):
            self.config.set("caption_dir", self.caption_dir)
            self.config.set("caption_model", self.caption_model_var.get())
            self.config.set("caption_language", self.caption_lang_var.get())
            self.config.set("caption_fill_gaps", self.caption_fill_gaps_var.get())
            self.config.set("caption_overwrite", self.cap_overwrite_var.get())
        if hasattr(self, "openrouter_key_var"):
            self.config.set("openrouter_api_key", self.openrouter_key_var.get().strip())
            self.config.set("shorts_model", self.shorts_model_var.get().strip())
            self.config.set("shorts_caption_model", self.shorts_wmodel_var.get())
            self.config.set("shorts_burn_captions", self.shorts_burn_var.get())
            self.config.set("shorts_output_mode", self.shorts_output_var.get())
            try:
                self.config.set("shorts_num_clips", int(self.shorts_num_var.get()))
                self.config.set("shorts_min_dur", int(self.shorts_min_var.get()))
                self.config.set("shorts_max_dur", int(self.shorts_max_var.get()))
            except (ValueError, tk.TclError):
                pass
        self._save_voiceover_config()
        self.config.save()

    def _save_voiceover_config(self) -> None:
        """Persist Voiceover tab fields. Guarded so a partially-typed numeric
        field (which makes a DoubleVar.get() raise) never aborts the whole save."""
        if not hasattr(self, "vo_process_var"):
            return
        self.config.set("vo_source_dir", self.vo_source_dir)
        self.config.set("vo_sources", list(self.vo_sources))
        self.config.set("rvc_dir", self.rvc_dir)
        self.config.set("vo_process", self.vo_process_var.get())
        self.config.set("rvc_device", self.rvc_device_var.get())
        try:
            self.config.set("vo_silence_threshold", float(self.vo_silence_thresh_var.get()))
            self.config.set("vo_silence_target", float(self.vo_silence_target_var.get()))
            self.config.set("vo_silence_noise_db", int(self.vo_noise_db_var.get()))
            self.config.set("vo_silence_pad_ms", int(self.vo_silence_pad_var.get()))
            self.config.set("vo_title_seconds", int(self.vo_title_seconds_var.get()))
        except (ValueError, tk.TclError):
            pass
        for voice in ("uptin", "pat"):
            base = dict(self.config.get(f"rvc_{voice}", {}) or {})
            v = self.vo_voice_vars.get(voice, {})
            try:
                base.update({
                    "pitch": int(v["pitch"].get()),
                    "index_rate": float(v["index_rate"].get()),
                    "f0method": v["f0method"].get(),
                    "rms_mix_rate": float(v["rms_mix_rate"].get()),
                    "protect": float(v["protect"].get()),
                })
            except (KeyError, ValueError, tk.TclError):
                pass
            self.config.set(f"rvc_{voice}", base)

    def browse_directory(self) -> None:
        selected = filedialog.askdirectory(
            initialdir=str(self.downloads_dir), title="Select Video Save Location",
        )
        if selected:
            self.downloads_dir = Path(selected)
            self.dir_label.config(text=str(self.downloads_dir))
            self._save_config()

    def browse_trans_directory(self) -> None:
        selected = filedialog.askdirectory(
            initialdir=str(self.trans_dir), title="Select Transcription Save Location",
        )
        if selected:
            self.trans_dir = Path(selected)
            self.trans_dir_label.config(text=str(self.trans_dir))
            self._save_config()

    def browse_dub_directory(self) -> None:
        selected = filedialog.askdirectory(title="Select Dub Audio Folder")
        if selected:
            self.dub_dir = os.path.normpath(selected)
            self.dub_dir_label.config(text=self.dub_dir)
            self._save_config()

    def _build_menu(self) -> None:
        """Create a professional menu bar for tool management."""
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)
        
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Open Downloads", command=lambda: self._open_folder(self.downloads_dir))
        file_menu.add_command(label="Open Transcriptions", command=lambda: self._open_folder(self.trans_dir))
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.quit)

        tools_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Tools", menu=tools_menu)
        tools_menu.add_command(label="Force Update yt-dlp & FFmpeg", command=lambda: threading.Thread(target=self._download_tools_thread, kwargs={"force_update": True}, daemon=True).start())
        tools_menu.add_command(label="Clear local cookies.txt", command=self.clear_local_cookies)

        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="About", command=lambda: messagebox.showinfo("About", "Tovo Video Downloader v2.1\nA professional batch video & transcription tool."))

    def _open_folder(self, path: Path) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(path))
            else:
                subprocess.run(["open", str(path)] if os.uname().sysname == "Darwin" else ["xdg-open", str(path)])
        except Exception as e:
            self.log(f"-> Could not open folder: {e}")

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
    def _log_environment(self) -> None:
        """Record tool locations and yt-dlp version to the log file.

        This is the first thing to compare when a batch works on one machine
        but fails on another: a missing deno/ffmpeg or an outdated yt-dlp is
        the usual culprit behind TikTok/YouTube 'exit code 1' failures.
        """
        for name in ("yt-dlp", "ffmpeg", "ffprobe", "deno"):
            local = BASE_PATH / f"{name}.exe"
            location = str(local) if local.exists() else (shutil.which(name) or "NOT FOUND")
            logger.info("Tool %s: %s", name, location)

        yt_dlp_exe = BASE_PATH / "yt-dlp.exe"
        exe = str(yt_dlp_exe) if yt_dlp_exe.exists() else (shutil.which("yt-dlp") or "")
        if exe:
            try:
                creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                result = subprocess.run(
                    [exe, "--version"], capture_output=True, text=True,
                    timeout=15, creationflags=creationflags,
                )
                logger.info("yt-dlp version: %s", result.stdout.strip() or result.stderr.strip())
            except (OSError, subprocess.SubprocessError) as e:
                logger.warning("Could not query yt-dlp version: %s", e)

    def _maybe_auto_update_yt_dlp(self, force: bool = False) -> None:
        """Update yt-dlp at most once per day (always when ``force``).

        TikTok/YouTube break frequently against stale yt-dlp builds, so we keep
        the bundled binary current. Throttled via a stored date so normal
        launches stay fast and offline-friendly.
        """
        today = datetime.date.today().isoformat()
        if not force and self.config.get("last_yt_dlp_update_check", "") == today:
            return
        proxy = self.config.get("proxy_url", "") or ""
        if update_yt_dlp(BASE_PATH, self.log, proxy=proxy):
            self.config.set("last_yt_dlp_update_check", today)
            self.config.save()

    def _startup_maintenance(self) -> None:
        """Background startup work: log the environment and refresh yt-dlp."""
        self._log_environment()
        self._maybe_auto_update_yt_dlp()

    def check_dependencies(self) -> None:
        threading.Thread(target=self._startup_maintenance, daemon=True).start()
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

    def _download_tools_thread(self, force_update: bool = False) -> None:
        try:
            # Pull in anything missing first (fresh machine), then force the
            # in-place yt-dlp self-update so an already-present but stale binary
            # actually gets refreshed.
            install_all(BASE_PATH, self.log)
            self._maybe_auto_update_yt_dlp(force=force_update)
            self.root.after(
                0, messagebox.showinfo, "Setup Complete",
                "All dependencies have been downloaded and updated successfully.",
            )
        except Exception as e:
            logger.exception("Dependency setup failed")
            self.log(f"-> Error during setup: {e}")
            self.root.after(
                0, messagebox.showerror, "Setup Error",
                f"Failed to download dependencies: {e}\n\nPlease install them manually.",
            )

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
        """Parses the bulk text input into (Titles, Links, Subtitles)."""
        titles: List[str] = []
        links: List[str] = []
        subtitles_list: List[List[str]] = []

        # Find all link indices
        link_indices: List[int] = []
        for i, line in enumerate(lines):
            if self._is_url(line):
                link_indices.append(i)

        for idx_pos, link_idx in enumerate(link_indices):
            # Title is the line before, IF it's not a URL
            if link_idx > 0 and not self._is_url(lines[link_idx - 1]):
                titles.append(lines[link_idx - 1])
            else:
                titles.append(f"Download_{len(links)+1}")
                
            links.append(lines[link_idx])

            # Subtitles/ignored lines are everything between this link and the next
            start = link_idx + 1
            # The next link OR if it's the last link, then the end of lines
            if idx_pos + 1 < len(link_indices):
                # If there's a title for the next link, stop before it
                next_link_idx = link_indices[idx_pos + 1]
                if next_link_idx > 0 and not self._is_url(lines[next_link_idx - 1]):
                    end = next_link_idx - 1
                else:
                    end = next_link_idx
            else:
                end = len(lines)
                
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
        
        title, link, subs = self._download_items[index]
        self.log(f"-> Manually retrying item {index + 1}: {title}")
        
        # Remove from cancelled set in case it was cancelled before
        self.cancelled_indices.discard(index)
        sync_mode = self.sync_mode_var.get()

        def _worker():
            self.log(f"-> Manual retry started for item {index + 1}...")
            aligner: Optional[GroqTranscriber] = None
            if sync_mode == "Whisper AI (Smart Sync)":
                aligner = self._make_sync_aligner()

            success = self._download_item_worker(
                index, title, link, subs, aligner, is_retry=True
            )
            if success:
                self.log(f"-> Manual retry succeeded for item {index + 1}!")
            else:
                self.log(f"-> Manual retry failed for item {index + 1}.")
                
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


    def start_download(self) -> None:
        # In the post-batch "Finish & Return" review state the same button just
        # returns to the start screen — it must NOT kick off a fresh download.
        if self.downloading and self.download_btn.text == "Finish & Return":
            self.reset_ui()
            return
        # Otherwise ignore clicks while a batch is actively running.
        if self.downloading:
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
            None,
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

    def _proxy_args(self) -> List[str]:
        """yt-dlp ``--proxy`` arguments based on the user's settings.

        An explicit proxy URL (for a local VPN proxy like V2RayN/Hiddify) wins;
        otherwise the "Disable System Proxy" toggle forces a direct connection.
        """
        proxy = self.proxy_url_var.get().strip()
        if proxy:
            return ["--proxy", proxy]
        if self.disable_proxy_var.get():
            return ["--proxy", ""]
        return []

    def _add_active_process(self, index: int, proc: subprocess.Popen) -> None:
        with self._state_lock:
            self.active_processes[index] = proc

    def _remove_active_process(self, index: int) -> None:
        with self._state_lock:
            self.active_processes.pop(index, None)

    def _build_format_selector(self) -> str:
        """yt-dlp ``-f`` selector. Always H.264 (avc1) video + AAC audio in mp4 —
        the one combination YouTube serves that Adobe Premiere imports natively,
        with no re-encoding. Everything else YouTube offers breaks Premiere's
        importer: VP9 (``vp09``) and AV1 (``av01``) — even when muxed into an mp4
        — and 10-bit streams. H.264 is also DASH (separate video+audio, https), so
        the merged file is video-first and 8-bit.

        "Default" (no cap) takes the best H.264, which tops out at 1080p because
        YouTube has no H.264 above that. A height cap restricts it further. The
        trailing fallbacks only fire for the rare video with no H.264 at all.
        """
        avc1 = "[ext=mp4][vcodec^=avc1]"
        height = self.QUALITY_HEIGHTS.get(self.max_quality_var.get())
        if not height:
            return f"bv{avc1}+ba[ext=m4a]/b{avc1}/b[ext=mp4]/b"
        h = f"[height<={height}]"
        return (
            f"bv{avc1}{h}+ba[ext=m4a]/b{avc1}{h}"
            f"/b[ext=mp4]{h}/b[ext=mp4]/b"
        )

    def _build_yt_dlp_command(self, title: str, link: str) -> List[str]:
        safe_title = sanitize_filename(title)
        output_path = str(self.downloads_dir / f"{safe_title}.%(ext)s")

        yt_dlp_exe = BASE_PATH / "yt-dlp.exe"
        exe_path = str(yt_dlp_exe) if yt_dlp_exe.exists() else "yt-dlp"

        # Player clients. yt-dlp's default set returns DASH H.264 at full
        # resolution (the android/mweb clients are now SABR-capped to 360p). The
        # TV client (bot-detection bypass) is *added* to the default set, not
        # substituted, when the toggle is on.
        extractor_args = (
            "youtube:player_client=default,tv"
            if self.use_tv_client_var.get() else None
        )

        cmd: List[str] = [
            exe_path,
            "-o", output_path,
            "--no-playlist",
            # H.264/AAC mp4 only (see _build_format_selector) — imports into
            # Premiere natively, so there is no re-encode step and nothing to go
            # wrong with VP9/AV1/10-bit. H.264 is DASH, so streams merge into a
            # clean video-first mp4.
            "-f", self._build_format_selector(),
            "--merge-output-format", "mp4",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        ]
        if extractor_args:
            cmd.extend(["--extractor-args", extractor_args])

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
        cmd.extend(self._proxy_args())

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
        # Keep the tail of yt-dlp's output so we can record the real failure
        # reason (e.g. "ERROR: ... Unable to extract") to the log file, not just
        # the bare exit code. This is what lets us diagnose per-machine failures.
        recent_output: deque = deque(maxlen=25)

        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if self._is_cancelled():
                    break

                clean_line = line.strip()
                if clean_line:
                    recent_output.append(clean_line)
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
            return_code = proc.returncode if proc.returncode is not None else -1
            if return_code != 0 and not self._is_cancelled():
                logger.error(
                    "yt-dlp exited %s using %s\n  Last output:\n%s",
                    return_code,
                    cmd[0],
                    "\n".join(f"    {ln}" for ln in recent_output) or "    (no output captured)",
                )
            return return_code
        finally:
            self._remove_active_process(index)

    def _download_item_worker(
        self,
        index: int,
        title: str,
        link: str,
        subs: Sequence[str],
        aligner: Optional[GroqTranscriber],
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
            self.log(f"-> Error downloading: {title} (Code: {return_code})")
            self.log("   Check the log for details. Common fixes: update yt-dlp or check cookies.")
            logger.error("Download failed for %r (link=%s) exit code %s", title, link, return_code)
            if not is_retry:
                if self.download_manager:
                    self.root.after(0, self.download_manager.set_item_status, index, "Failed (Queued for Retry)", "#FF9500")
            else:
                if self.download_manager:
                    self.root.after(0, self.download_manager.set_item_status, index, "Failed", "#FF3B30")
            
            with self.errors_lock:
                self.batch_errors.append(f"Download Error for '{title}': Code {return_code}")
            return False

    def download_process(
        self,
        titles: Sequence[str],
        links: Sequence[str],
        subtitles_list: Sequence[Sequence[str]],
        sync_mode: str,
    ) -> None:
        aligner: Optional[GroqTranscriber] = None
        if sync_mode == "Whisper AI (Smart Sync)":
            if self.groq_key_var.get().strip():
                aligner = self._make_sync_aligner()
            else:
                self.log("[!] Smart Sync needs a Groq API key; using 2-second intervals.")
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
            # Check if any items are still in "Failed" state after retries
            has_failures = False
            if self.download_manager:
                for item in self.download_manager.items:
                    if item.status_label["text"] in ("Failed", "Failed (Retry)"):
                        has_failures = True
                        break

            if not self._is_cancelled():
                if has_failures:
                    self.log("\nBatch finished with errors. You can manually Retry or Skip items now.")
                else:
                    self.log("\nBatch download complete.")
                
                # Always allow user to review before returning
                self.root.after(0, self._show_download_review_buttons)
            else:
                self.log("\nBatch download cancelled by user.")
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

    def _save_errors(self, errors_found: Sequence[str], target_dir: Optional[Path] = None) -> None:
        target_dir = target_dir or self.downloads_dir
        try:
            path = target_dir / "errors.txt"
            with path.open("w", encoding="utf-8") as ef:
                ef.write("--- Errors ---\n\n")
                for err in errors_found:
                    ef.write(f"- {err}\n")
            self.log(f"-> Exported {len(errors_found)} errors to {path}")
        except OSError as e:
            self.log(f"-> Warning: Could not save errors.txt: {e}")

    def _maybe_generate_srt(
        self,
        title: str,
        subs: Sequence[str],
        aligner: Optional[GroqTranscriber],
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
                        fill_gaps=self.sync_fill_gaps_var.get(),
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
            generate_standard_srt(subs, srt_path, self.log)

        self._maybe_export_ttml(srt_path)

    def _maybe_export_ttml(self, srt_path: Path) -> None:
        """Emit a sibling styled .ttml when the export toggle is on."""
        if not self.export_ttml_var.get():
            return
        if not Path(srt_path).exists():
            return
        try:
            style = CaptionStyle.from_dict(self.config.get("caption_style"))
            write_ttml(srt_path, style, self.log)
        except Exception as e:
            logger.exception("TTML export failed for %s", srt_path)
            self.log(f"-> Warning: could not export .ttml: {e}")

    def _open_caption_style_dialog(self) -> None:
        """Edit and persist the CaptionStyle used for every .ttml export."""
        style = CaptionStyle.from_dict(self.config.get("caption_style"))

        win = tk.Toplevel(self.root)
        win.title("Caption Style (.ttml)")
        win.configure(bg=self.bg_color)
        win.transient(self.root)
        win.resizable(False, False)
        win.grab_set()

        pad = {"padx": 12, "pady": 6}
        body = tk.Frame(win, bg=self.bg_color)
        body.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        def _label(row: int, text: str) -> None:
            tk.Label(
                body, text=text, bg=self.bg_color, fg=self.text_color,
                font=(self.font_family, 10), anchor="w",
            ).grid(row=row, column=0, sticky="w", **pad)

        # --- numeric / text fields -------------------------------------
        font_var = tk.StringVar(value=style.font_family)
        size_var = tk.IntVar(value=style.font_size)
        x_var = tk.IntVar(value=style.x_offset)
        y_var = tk.IntVar(value=style.y_offset)
        opacity_var = tk.IntVar(value=style.bg_opacity)
        padding_var = tk.IntVar(value=style.padding)
        radius_var = tk.IntVar(value=style.corner_radius)
        text_color_var = tk.StringVar(value=style.text_color)
        bg_color_var = tk.StringVar(value=style.bg_color)

        _label(0, "Font family")
        ttk.Combobox(
            body, textvariable=font_var, width=22, font=(self.font_family, 10),
            values=["Arial", "Helvetica", "Segoe UI", "Times New Roman",
                    "Verdana", "Tahoma", "Georgia", "Calibri", "Courier New"],
        ).grid(row=0, column=1, columnspan=2, sticky="w", **pad)

        _label(1, "Font size (px)")
        tk.Spinbox(body, from_=8, to=200, textvariable=size_var, width=6,
                   font=(self.font_family, 10)).grid(row=1, column=1, sticky="w", **pad)

        _label(2, "X offset from centre (px)")
        tk.Spinbox(body, from_=-960, to=960, textvariable=x_var, width=6,
                   font=(self.font_family, 10)).grid(row=2, column=1, sticky="w", **pad)

        _label(3, "Y offset from centre (px)")
        tk.Spinbox(body, from_=-540, to=540, textvariable=y_var, width=6,
                   font=(self.font_family, 10)).grid(row=3, column=1, sticky="w", **pad)

        # --- colour swatches -------------------------------------------
        def _make_swatch(row: int, label: str, var: tk.StringVar) -> None:
            _label(row, label)
            swatch = tk.Label(body, bg=var.get(), width=4, relief=tk.SOLID, bd=1)
            swatch.grid(row=row, column=1, sticky="w", **pad)

            def _pick() -> None:
                chosen = colorchooser.askcolor(color=var.get(), parent=win)[1]
                if chosen:
                    var.set(chosen)
                    swatch.config(bg=chosen)

            RoundedButton(
                body, text="Pick…", command=_pick, radius=12,
                bg_color=self.gray_bg, hover_color=self.gray_hover,
                text_color=self.text_color, font=(self.font_family, 9, "bold"),
                width=70, height=28,
            ).grid(row=row, column=2, sticky="w", **pad)

        _make_swatch(4, "Text colour", text_color_var)
        _make_swatch(5, "Background colour", bg_color_var)

        _label(6, "Background opacity (%)")
        tk.Spinbox(body, from_=0, to=100, textvariable=opacity_var, width=6,
                   font=(self.font_family, 10)).grid(row=6, column=1, sticky="w", **pad)

        _label(7, "Background padding (px)")
        tk.Spinbox(body, from_=0, to=100, textvariable=padding_var, width=6,
                   font=(self.font_family, 10)).grid(row=7, column=1, sticky="w", **pad)

        _label(8, "Corner radius (px)")
        tk.Spinbox(body, from_=0, to=100, textvariable=radius_var, width=6,
                   font=(self.font_family, 10)).grid(row=8, column=1, sticky="w", **pad)

        tk.Label(
            body,
            text="Note: Premiere TTML captions honour font, size, position and\n"
                 "colours. Corner radius is saved for the overlay export but is\n"
                 "not represented in TTML captions.",
            bg=self.bg_color, fg="#86868B", font=(self.font_family, 8),
            justify="left", anchor="w",
        ).grid(row=9, column=0, columnspan=3, sticky="w", **pad)

        # --- save / cancel ---------------------------------------------
        def _save_and_close() -> None:
            new_style = CaptionStyle(
                font_family=font_var.get().strip() or "Arial",
                font_size=int(size_var.get()),
                x_offset=int(x_var.get()),
                y_offset=int(y_var.get()),
                text_color=text_color_var.get(),
                bg_color=bg_color_var.get(),
                bg_opacity=int(opacity_var.get()),
                padding=int(padding_var.get()),
                corner_radius=int(radius_var.get()),
            )
            self.config.set("caption_style", new_style.to_dict())
            self.config.save()
            self.log("-> Caption style saved.")
            win.destroy()

        btn_row = tk.Frame(win, bg=self.bg_color)
        btn_row.pack(fill=tk.X, padx=12, pady=(0, 12))
        RoundedButton(
            btn_row, text="Save", command=_save_and_close, radius=14,
            bg_color=self.accent_color, hover_color="#4B49C4",
            text_color="white", font=(self.font_family, 10, "bold"),
            width=90, height=34,
        ).pack(side=tk.RIGHT)
        RoundedButton(
            btn_row, text="Cancel", command=win.destroy, radius=14,
            bg_color=self.gray_bg, hover_color=self.gray_hover,
            text_color=self.text_color, font=(self.font_family, 10, "bold"),
            width=90, height=34,
        ).pack(side=tk.RIGHT, padx=(0, 8))

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
    # Sync Tab Logic
    # ------------------------------------------------------------------
    def _scan_sync_items(self) -> None:
        """Find .srt files in the download folder that have a matching audio
        source (Dub folder preferred, else the downloaded video)."""
        if self.downloading:
            return
        self.sync_items = []
        self.sync_listbox.delete(0, tk.END)

        dl_dir = self.downloads_dir
        dub_dir = Path(self.dub_dir) if self.dub_dir else None
        srt_files = sorted(dl_dir.glob("*.srt")) if dl_dir.exists() else []

        for srt in srt_files:
            stem = srt.stem
            if stem.endswith(SYNCED_SRT_SUFFIX):  # don't re-sync our own output
                continue
            title = stem[: -len(SRT_TITLE_SUFFIX)] if stem.endswith(SRT_TITLE_SUFFIX) else stem

            audio_path: Optional[str] = None
            kind = ""
            if dub_dir and dub_dir.exists():
                for ext in DUB_AUDIO_EXTENSIONS:
                    candidate = dub_dir / f"{title}{ext}"
                    if candidate.exists():
                        audio_path, kind = str(candidate), "Dub"
                        break
            if not audio_path:
                video = dl_dir / f"{title}.mp4"
                if video.exists():
                    audio_path, kind = str(video), "Video"

            if audio_path:
                self.sync_items.append({
                    "title": title, "srt_path": srt, "audio_path": audio_path, "kind": kind,
                })
                self.sync_listbox.insert(tk.END, f"{title}    [{kind}]")

        count = len(self.sync_items)
        self.sync_count_label.config(text=f"{count} syncable item(s) found")
        if count == 0:
            self.log("-> Sync: no .srt files with a matching Dub/Video audio source were found.")

    def _sync_select_all(self) -> None:
        if self.sync_items:
            self.sync_listbox.select_set(0, tk.END)

    def start_sync(self) -> None:
        """Start button on the Sync tab (doubles as 'Finish & Return')."""
        if self.downloading and self.sync_btn.text == "Finish & Return":
            self.reset_ui()
            return
        if self.downloading:
            return

        if not self.sync_items:
            messagebox.showinfo("Sync", "No syncable items found. Add a .srt + its Dub/Video, then Rescan.")
            return

        selected = self.sync_listbox.curselection()
        if selected:
            chosen = [self.sync_items[i] for i in selected]
        else:
            if not messagebox.askyesno(
                "Sync All", "No items selected. Sync ALL listed items?",
            ):
                return
            chosen = list(self.sync_items)

        with self._state_lock:
            self.downloading = True
            self.cancelled = False
        self.cancelled_indices = set()
        with self.errors_lock:
            self.batch_errors = []

        self.sync_btn.config_state("disabled", text="Syncing...", bg="#A1C6EA")
        self.sync_cancel_btn.config_state("normal", bg="#FF3B30")
        self.sync_scan_btn.config_state("disabled", bg="#E5E5EA")

        self.log_text.config(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state="disabled")

        self.sync_select_frame.grid_remove()
        self._sync_chosen = chosen
        titles = [it["title"] for it in chosen]
        self.sync_manager = DownloadManager(
            self.sync_tab, titles,
            self._cancel_single_item,
            self._sync_retry_item,
            self._sync_skip_item,
            None,
            self.bg_color, self.text_color, self.accent_color, self.font_family,
        )
        self.sync_manager.grid(row=2, column=0, sticky="nsew", pady=(0, 15))

        threading.Thread(target=self._sync_process, args=(chosen,), daemon=True).start()

    def _sync_process(self, chosen: List[dict]) -> None:
        self.log(f"\n--- Starting Subtitle Sync ({len(chosen)} item(s)) ---")
        if not self.groq_key_var.get().strip():
            self.log("[!] Sync needs a Groq API key (set it on the Transcription tab).")
            self.root.after(0, self.reset_ui)
            return
        aligner = self._make_sync_aligner()

        try:
            for index, item in enumerate(chosen):
                if self._is_cancelled():
                    break
                self._sync_item_worker(index, item, aligner)
            with self.errors_lock:
                if self.batch_errors:
                    self._save_errors(self.batch_errors, target_dir=self.downloads_dir)
        except Exception as e:
            logger.exception("Subtitle sync crashed")
            self.log(f"\nAn error occurred during sync: {e}")
        finally:
            has_failures = False
            if self.sync_manager:
                for it in self.sync_manager.items:
                    if it.status_label["text"] in ("Failed", "Failed (Retry)"):
                        has_failures = True
                        break
            if not self._is_cancelled():
                if has_failures:
                    self.log("\nSync finished with errors. You can Retry or Skip items, or Finish & Return.")
                else:
                    self.log("\n--- Subtitle Sync Complete ---")
                self.root.after(0, lambda: self.sync_btn.config_state("normal", text="Finish & Return", bg="#34C759"))
            else:
                self.log("\nSync cancelled by user.")
                self.root.after(0, self.reset_ui)

    def _sync_item_worker(self, index: int, item: dict, aligner: GroqTranscriber, is_retry: bool = False) -> bool:
        if self._is_cancelled() or index in self.cancelled_indices:
            return False

        title = item["title"]
        status = "Retrying..." if is_retry else "Active"
        if self.sync_manager:
            self.root.after(0, self.sync_manager.set_item_status, index, status, self.accent_color)

        self.log(f"\n[{index + 1}] Syncing: {title}  (audio: {item['kind']})")
        cues = read_srt_cues(item["srt_path"])
        if not cues:
            self.log(f"-> No subtitle text found in {Path(item['srt_path']).name}; skipping.")
            logger.error("Sync: empty/unreadable SRT for %r (%s)", title, item["srt_path"])
            if self.sync_manager:
                self.root.after(0, self.sync_manager.set_item_status, index, "Failed", "#FF3B30")
            with self.errors_lock:
                self.batch_errors.append(f"Sync Error for '{title}': no readable subtitle text")
            return False

        # Write a new file and leave the original .srt untouched.
        src_srt = Path(item["srt_path"])
        out_srt = src_srt.with_name(f"{title}{SYNCED_SRT_SUFFIX}.srt")

        try:
            ok = aligner.align(
                item["audio_path"], cues, out_srt, is_cancelled=self._is_cancelled,
                fill_gaps=self.sync_fill_gaps_var.get(),
            )
        except Exception as e:
            logger.exception("Whisper sync failed for %s", title)
            self.log(f"-> Error during sync: {e}")
            if self.sync_manager:
                self.root.after(0, self.sync_manager.set_item_status, index, "Failed", "#FF3B30")
            with self.errors_lock:
                self.batch_errors.append(f"Sync Error for '{title}': {e}")
            return False

        if self._is_cancelled():
            return False

        if ok:
            self.log(f"-> Synced: {title}  ->  {out_srt.name} (original kept)")
            self._maybe_export_ttml(out_srt)
            if self.sync_manager:
                self.root.after(0, self.sync_manager.set_item_status, index, "Finished", "#34C759")
            return True
        else:
            self.log(f"-> Sync failed: {title}")
            if self.sync_manager:
                self.root.after(0, self.sync_manager.set_item_status, index, "Failed", "#FF3B30")
            with self.errors_lock:
                self.batch_errors.append(f"Sync Error for '{title}': alignment returned no result")
            return False

    def _sync_retry_item(self, index: int) -> None:
        if not self._sync_chosen or index >= len(self._sync_chosen):
            return
        self.cancelled_indices.discard(index)
        item = self._sync_chosen[index]

        def _worker():
            if not self.groq_key_var.get().strip():
                if self.sync_manager:
                    self.root.after(0, self.sync_manager.set_item_status, index, "Failed (No Groq key)", "#FF3B30")
                return
            aligner = self._make_sync_aligner()
            self._sync_item_worker(index, item, aligner, is_retry=True)

        threading.Thread(target=_worker, daemon=True).start()

    def _sync_skip_item(self, index: int) -> None:
        with self._state_lock:
            if self.sync_manager:
                self.sync_manager.set_item_status(index, "Skipped", "#34C759")
            self.log(f"-> Sync item marked as skipped: {index + 1}")

    # ------------------------------------------------------------------
    # Transcription Tab Logic
    # ------------------------------------------------------------------

    def start_transcription(self) -> None:
        """Called by the Start button on the Transcription tab."""
        # In the post-batch "Finish & Return" review state the same button just
        # returns to the start screen — it must NOT kick off a fresh batch.
        if self.downloading and self.trans_btn.text == "Finish & Return":
            self.reset_ui()
            return
        # Otherwise ignore clicks while a batch is actively running.
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

        with self._state_lock:
            self.downloading = True
            self.cancelled = False
        self.cancelled_indices = set()
        with self.errors_lock:
            self.batch_errors = []
        # Clear any stale errors file from a previous transcription run.
        trans_errors = self.trans_dir / "errors.txt"
        if trans_errors.exists():
            try:
                trans_errors.unlink()
            except OSError:
                pass

        self.trans_btn.config_state("disabled", text="Transcribing...", bg="#E5E5EA")
        self.trans_cancel_btn.config_state("normal", bg="#FF3B30")
        self.groq_key_entry.entry.config(state="disabled")
        
        # Hide input area: hiding the parent border also hides the inner text widget,
        # so we don't grid_remove() the text widget separately (reset_ui only restores
        # the border, and an independent grid_remove on the child would leave the
        # input invisible after a batch).
        self.trans_input_text.config(state="disabled")  # Lock text
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

    def _trans_retry_item(self, index: int) -> None:
        """Manually retry a single failed transcription item."""
        if not hasattr(self, "_trans_batch_items") or index >= len(self._trans_batch_items):
            return
        
        self.cancelled_indices.discard(index)
        title, link = self._trans_batch_items[index]
        
        def _worker():
            # Groq powers transcription; build a dedicated transcriber for the retry.
            transcriber = self._make_groq_transcriber()

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
            logger.error("Transcription audio extraction failed for %r (link=%s)", title, link)
            self.root.after(0, self.trans_manager.set_item_status, index, "Failed (Audio)", "#FF3B30")
            with self.errors_lock:
                self.batch_errors.append(f"Audio Extraction Error for '{title}'")
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
            logger.error("Transcription produced no text for %r (link=%s)", title, link)
            self.root.after(0, self.trans_manager.set_item_status, index, "Failed", "#FF3B30")
            with self.errors_lock:
                self.batch_errors.append(f"Transcription Error for '{title}'")
            return False

    def _append_to_combined_report(self, title: str, link: str, transcript: str) -> None:
        report_path = self.trans_dir / "All_Transcriptions.txt"
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
        report_path = self.trans_dir / "All_Transcriptions.txt"
        if report_path.exists():
            try: report_path.unlink()
            except: pass

        self._trans_batch_items = list(zip(titles, links))

        concurrent_mode = self.trans_concurrent_var.get()
        max_workers = self.trans_max_concurrent_var.get() if concurrent_mode else 1

        all_results = [None] * len(titles) # Use fixed size for order
        results_lock = threading.Lock()

        transcriber = self._make_groq_transcriber()
        if not self.groq_key_var.get().strip():
            self.log("[!] Transcription: set your Groq API key first.")
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

        # Transcripts are appended to the combined report in real time by
        # _trans_item_task, so there is no separate result-collection step here.
        # Persist any per-item errors next to the transcriptions for diagnosis.
        with self.errors_lock:
            if self.batch_errors:
                self._save_errors(self.batch_errors, target_dir=self.trans_dir)

        # Batch finished logic
        has_failures = any(item.status_label["text"] in ("Failed", "Failed (Retry)") for item in self.trans_manager.items)
        if not self._is_cancelled():
            if has_failures:
                self.log("\nBatch finished with errors. You can manually Retry or Skip items now.")
            else:
                self.log("\n--- Batch Transcription Complete ---")
            
            self.log(f"Results saved in: {report_path.name}")
            # Always show Finish & Return so user can see progress bars
            self.root.after(0, lambda: self.trans_btn.config_state("normal", text="Finish & Return", bg="#34C759"))
        else:
            self.log("\nBatch transcription cancelled by user.")
            self.root.after(0, self.reset_ui)

    def _extract_audio_only(self, title: str, link: str, index: int = -1) -> Optional[str]:
        """Download the audio-only stream and return its path.

        Uses ``-f bestaudio/best`` so we never pull the video pixels and never
        invoke yt-dlp's audio post-processor. Whisper (local + Groq) accept the
        resulting m4a/webm natively, so ffmpeg/ffprobe stay out of the loop.
        Also clears stale leftovers up-front so retries don't choke on a
        corrupted partial from a previous run.
        """
        safe_title = sanitize_filename(title)
        output_template = str(self.trans_dir / f"{safe_title}_audio.%(ext)s")

        # Drop any leftover from a prior interrupted run before starting fresh.
        for stale in self.trans_dir.glob(f"{safe_title}_audio.*"):
            try:
                stale.unlink()
            except OSError:
                pass

        yt_dlp_exe = BASE_PATH / "yt-dlp.exe"
        exe_path = str(yt_dlp_exe) if yt_dlp_exe.exists() else "yt-dlp"

        cmd = [
            exe_path,
            "-f", "bestaudio/best",
            "-o", output_template,
            "--no-playlist",
            "--force-overwrites",
            # Politeness throttle so TikTok / Instagram don't 403 us when running concurrently.
            "--sleep-requests", "1",
            "--sleep-interval", "1",
            "--max-sleep-interval", "3",
            "--extractor-args", "youtube:player_client=android,mweb;player_skip=webpage",
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        ]

        cookies_txt = BASE_PATH / "cookies.txt"
        if cookies_txt.exists():
            cmd.extend(["--cookies", str(cookies_txt)])
        elif self.trans_use_browser_cookies.get():
            cmd.extend(["--cookies-from-browser", "chrome"])

        deno_exe = BASE_PATH / "deno.exe"
        if deno_exe.exists():
            cmd.extend(["--js-runtime", "deno:" + str(deno_exe)])

        cmd.extend(self._proxy_args())

        cmd.append(link)

        self.log(f"-> Extracting audio for {title}...")

        def audio_progress(p):
            if index >= 0 and hasattr(self, "trans_manager") and self.trans_manager:
                self.root.after(0, self.trans_manager.update_item_progress, index, p * 0.1)
            elif index >= 0 and self.download_manager:
                self.root.after(0, self.download_manager.update_item_progress, index, p * 0.1)

        return_code = self._run_yt_dlp(index, cmd, progress_callback=audio_progress)

        if return_code != 0:
            logger.error("Audio extraction failed for %r (link=%s) exit code %s", title, link, return_code)
            return None

        # yt-dlp picked the source extension (.m4a / .webm / .mp3 / ...);
        # find the file that actually landed.
        matches = sorted(self.trans_dir.glob(f"{safe_title}_audio.*"))
        if matches:
            return str(matches[0])
        return None

    # ------------------------------------------------------------------
    # UI reset
    # ------------------------------------------------------------------
    def _show_download_review_buttons(self) -> None:
        """Post-batch review state: 'Finish & Return' plus a one-click shortcut
        to the download folder (the Cancel button is repurposed, since there is
        nothing left to cancel)."""
        self.download_btn.config_state("normal", text="Finish & Return", bg="#34C759")
        self.cancel_btn.command = lambda: self._open_folder(self.downloads_dir)
        self.cancel_btn.config_state("normal", text="📂  Open Folder", bg=self.accent_color)

    def reset_ui(self) -> None:
        with self._state_lock:
            self.downloading = False

        # Reset Download Tab Buttons
        self.download_btn.config_state("normal", text="⬇  Start Batch Download", bg=self.accent_color)
        # Restore the Cancel button (it doubles as 'Open Folder' in review state).
        self.cancel_btn.command = self.cancel_download
        self.cancel_btn.config_state("disabled", text="Cancel", bg="#E5E5EA")
        self.browse_btn.config_state("normal", bg=self.gray_bg)
        self.dl_input_text.config(state="normal", bg="white")
        if hasattr(self, "dl_input_frame"):
            self.dl_input_frame.grid()
        
        # Reset Transcription Tab Buttons
        self.trans_btn.config_state("normal", text="🎙  Start Transcription", bg=self.accent_color)
        self.trans_cancel_btn.config_state("disabled", bg="#E5E5EA")
        self.groq_key_entry.entry.config(state="normal")
        if hasattr(self, "trans_border"):
            self.trans_border.grid()

        # Reset Sync Tab Buttons
        if hasattr(self, "sync_btn"):
            self.sync_btn.config_state("normal", text="🔄  Start Sync", bg=self.accent_color)
            self.sync_cancel_btn.config_state("disabled", bg="#E5E5EA")
            self.sync_scan_btn.config_state("normal", bg=self.gray_bg)
        if hasattr(self, "sync_select_frame"):
            self.sync_select_frame.grid()

        # Reset Voiceover Tab
        if hasattr(self, "vo_btn"):
            self.vo_btn.config_state("normal", text="🎚  Start Voiceover", bg=self.accent_color)
            # The Cancel button doubles as 'Open Dub Folder' in the review state.
            self.vo_cancel_btn.command = self.cancel_download
            self.vo_cancel_btn.config_state("disabled", text="Cancel", bg="#E5E5EA")
        if hasattr(self, "vo_settings_frame"):
            self.vo_settings_frame.grid()

        # Clean up managers
        if hasattr(self, "download_manager") and self.download_manager:
            self.download_manager.destroy()
            self.download_manager = None

        if hasattr(self, "trans_manager") and self.trans_manager:
            self.trans_manager.destroy()
            self.trans_manager = None

        if hasattr(self, "sync_manager") and self.sync_manager:
            self.sync_manager.destroy()
            self.sync_manager = None

        if hasattr(self, "vo_manager") and self.vo_manager:
            self.vo_manager.destroy()
            self.vo_manager = None

        # Reset Auto-Caption tab
        if hasattr(self, "cap_btn"):
            self.cap_btn.config_state("normal", text="💬  Generate Captions", bg=self.accent_color)
            self.cap_cancel_btn.config_state("disabled", text="Cancel", bg="#E5E5EA")
        if hasattr(self, "cap_select_frame"):
            self.cap_select_frame.grid()
        if hasattr(self, "caption_manager") and self.caption_manager:
            self.caption_manager.destroy()
            self.caption_manager = None

        # Reset Short Clips tab (no manager/frame hide; just re-enable buttons)
        if hasattr(self, "shorts_analyze_btn"):
            self.shorts_analyze_btn.config_state("normal", bg=self.accent_color)
            self.shorts_render_btn.config_state("normal", bg="#34C759")
            self.shorts_cancel_btn.config_state("disabled", text="Cancel", bg="#E5E5EA")
        if hasattr(self, "shorts_test_btn"):
            self.shorts_test_btn.config_state("normal", bg=self.gray_bg)
            
        self.dl_input_text.config(state="normal", bg="white")
        self.trans_input_text.config(state="normal", bg="white")
        
        # If we returned to the Sync tab, refresh the list so freshly-synced
        # files are reflected.
        if getattr(self, "_active_tab", None) == "sync":
            self._scan_sync_items()

        # Force focus back to the active tab's input area
        if hasattr(self, "btn_dl_tab") and self.btn_dl_tab.text_color == self.accent_color:
            self.dl_input_text.focus_set()
        elif getattr(self, "_active_tab", None) != "sync":
            self.trans_input_text.focus_set()


def _enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError) as e:
        logger.debug("Could not enable DPI awareness: %s", e)


def _setup_logging() -> None:
    """Configure logging to both the console and a rotating file.

    When the app runs windowed (pythonw / packaged .exe) there is no console,
    so error output would otherwise be lost. The file handler in ``logs/`` keeps
    a durable record of warnings, errors and stack traces for diagnosis.
    """
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        logger.info("Logging to %s", LOG_FILE)
    except OSError as e:
        # Never let a logging failure stop the app from starting.
        logger.warning("Could not open log file %s: %s", LOG_FILE, e)


def main() -> None:
    _setup_logging()
    _enable_dpi_awareness()
    root = tk.Tk()
    AppleStyleApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
