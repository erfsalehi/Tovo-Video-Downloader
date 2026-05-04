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
from subtitles import WhisperAligner, generate_standard_srt
from widgets import DownloadManager, RoundedButton

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

        self.main_frame = tk.Frame(self.root, bg=self.bg_color)
        self.main_frame.grid(row=0, column=0, sticky="nsew", padx=35, pady=20)
        self.main_frame.grid_columnconfigure(0, weight=1)
        self.main_frame.grid_rowconfigure(2, weight=1)

        tk.Label(
            self.main_frame, text="Batch Downloader", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 26, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 5))

        tk.Label(
            self.main_frame,
            text="Paste Title on line 1, Link on line 2, Title on line 3, etc.",
            bg=self.bg_color, fg="#86868B", font=(self.font_family, 11),
        ).grid(row=1, column=0, sticky="w", pady=(0, 10))

        self._build_input_area()
        self._build_save_row()
        self._build_options_row()
        self._build_dub_row()
        self._build_advanced_row()
        self._build_button_row()
        self._build_log_area()

    def _build_input_area(self) -> None:
        self.input_frame = tk.Frame(
            self.main_frame, bg="white", highlightbackground="#D2D2D7", highlightthickness=1,
        )
        self.input_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 15))

        self.input_text = tk.Text(
            self.input_frame, wrap=tk.WORD, font=(self.font_family, 12),
            bg="white", fg=self.text_color, insertbackground=self.accent_color,
            relief=tk.FLAT, padx=15, pady=15,
        )
        self.input_text.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        scroll = tk.Scrollbar(self.input_frame, command=self.input_text.yview)
        scroll.pack(fill=tk.Y, side=tk.RIGHT)
        self.input_text.config(yscrollcommand=scroll.set, state="normal")

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Cut", command=lambda: self.input_text.event_generate("<<Cut>>"))
        menu.add_command(label="Copy", command=lambda: self.input_text.event_generate("<<Copy>>"))
        menu.add_command(label="Paste", command=lambda: self.input_text.event_generate("<<Paste>>"))
        menu.add_separator()
        menu.add_command(label="Select All", command=lambda: self.input_text.event_generate("<<SelectAll>>"))
        self.context_menu = menu

        def show_context_menu(event):
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        self.input_text.bind("<Button-3>", show_context_menu)

        shortcuts = {
            "<Control-a>": "<<SelectAll>>",
            "<Control-A>": "<<SelectAll>>",
            "<Control-c>": "<<Copy>>",
            "<Control-C>": "<<Copy>>",
            "<Control-v>": "<<Paste>>",
            "<Control-V>": "<<Paste>>",
            "<Control-x>": "<<Cut>>",
            "<Control-X>": "<<Cut>>",
        }
        for key, virtual_event in shortcuts.items():
            if virtual_event == "<<SelectAll>>":
                self.input_text.bind(
                    key, lambda e: self.input_text.tag_add(tk.SEL, "1.0", tk.END) or "break"
                )
            else:
                self.input_text.bind(
                    key, lambda e, ve=virtual_event: self.input_text.event_generate(ve) or "break"
                )

    def _build_save_row(self) -> None:
        frame = tk.Frame(self.main_frame, bg=self.bg_color)
        frame.grid(row=3, column=0, sticky="ew", pady=(0, 15))
        frame.grid_columnconfigure(1, weight=1)

        tk.Label(
            frame, text="Save to:", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 11, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 10))

        self.dir_label = tk.Label(
            frame, text=str(self.downloads_dir), bg=self.bg_color, fg="#555555",
            font=(self.font_family, 10),
        )
        self.dir_label.grid(row=0, column=1, sticky="w")

        self.browse_btn = RoundedButton(
            frame, text="Browse...", command=self.browse_directory,
            radius=15, bg_color=self.gray_bg, hover_color=self.gray_hover,
            text_color=self.text_color, font=(self.font_family, 10, "bold"),
            width=100, height=35,
        )
        self.browse_btn.grid(row=0, column=2, sticky="e")

    def _build_options_row(self) -> None:
        frame = tk.Frame(self.main_frame, bg=self.bg_color)
        frame.grid(row=4, column=0, sticky="ew", pady=(0, 15))

        tk.Label(
            frame, text="Subtitle Sync Mode:", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 11, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 10))

        self.sync_mode_var = tk.StringVar(value="None (2-second intervals)")
        ttk.Combobox(
            frame, textvariable=self.sync_mode_var,
            values=["None (2-second intervals)", "Whisper AI (Smart Sync)"],
            state="readonly", font=(self.font_family, 10), width=25,
        ).pack(side=tk.LEFT)

        tk.Frame(frame, bg=self.bg_color, width=20).pack(side=tk.LEFT)

        self.concurrent_var = tk.BooleanVar(value=self.config.get("concurrent_downloads", False))
        tk.Checkbutton(
            frame, text="Simultaneous",
            variable=self.concurrent_var, bg=self.bg_color, activebackground=self.bg_color,
            fg=self.text_color, font=(self.font_family, 10),
            command=self._save_config,
        ).pack(side=tk.LEFT)

        self.max_concurrent_var = tk.IntVar(value=self.config.get("max_concurrent", 5))
        self.max_concurrent_spin = tk.Spinbox(
            frame, from_=1, to=10, textvariable=self.max_concurrent_var,
            width=3, font=(self.font_family, 10), command=self._save_config,
        )
        self.max_concurrent_spin.pack(side=tk.LEFT, padx=(5, 0))

    def _build_dub_row(self) -> None:
        frame = tk.Frame(self.main_frame, bg=self.bg_color)
        frame.grid(row=5, column=0, sticky="ew", pady=(0, 15))
        frame.grid_columnconfigure(1, weight=1)

        tk.Label(
            frame, text="(Optional) Dub Folder:", bg=self.bg_color, fg=self.text_color,
            font=(self.font_family, 11, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=(0, 10))

        dub_label_text = self.dub_dir if self.dub_dir else "Not Selected (Uses Video Audio)"
        self.dub_dir_label = tk.Label(
            frame, text=dub_label_text, bg=self.bg_color, fg="#555555",
            font=(self.font_family, 10),
        )
        self.dub_dir_label.grid(row=0, column=1, sticky="w")

        self.dub_browse_btn = RoundedButton(
            frame, text="Browse...", command=self.browse_dub_directory,
            radius=15, bg_color=self.gray_bg, hover_color=self.gray_hover,
            text_color=self.text_color, font=(self.font_family, 10, "bold"),
            width=100, height=35,
        )
        self.dub_browse_btn.grid(row=0, column=2, sticky="e")

    def _build_advanced_row(self) -> None:
        frame = tk.Frame(self.main_frame, bg=self.bg_color)
        frame.grid(row=6, column=0, sticky="ew", pady=(0, 15))

        self.use_browser_cookies = tk.BooleanVar(value=self.config.get("use_browser_cookies", False))
        tk.Checkbutton(
            frame, text="Use Chrome Cookies (as fallback)",
            variable=self.use_browser_cookies, bg=self.bg_color, activebackground=self.bg_color,
            fg=self.text_color, font=(self.font_family, 10),
            command=self._save_config,
        ).pack(side=tk.LEFT)

        tk.Button(
            frame, text="Clear local cookies.txt", font=(self.font_family, 9),
            command=self.clear_local_cookies, bg=self.gray_bg, relief=tk.FLAT,
        ).pack(side=tk.RIGHT)

    def _build_button_row(self) -> None:
        frame = tk.Frame(self.main_frame, bg=self.bg_color)
        frame.grid(row=7, column=0, sticky="ew", pady=(0, 20))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_columnconfigure(1, weight=1)

        self.download_btn = RoundedButton(
            frame, text="Start Download", command=self.start_download,
            radius=20, bg_color=self.accent_color, hover_color=self.accent_hover,
            text_color="white", font=(self.font_family, 13, "bold"), height=50,
        )
        self.download_btn.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        self.cancel_btn = RoundedButton(
            frame, text="Cancel", command=self.cancel_download,
            radius=20, bg_color="#FF3B30", hover_color="#D70A01",
            text_color="white", font=(self.font_family, 13, "bold"), height=50,
        )
        self.cancel_btn.grid(row=0, column=1, sticky="ew")
        self.cancel_btn.config_state("disabled", bg="#E5E5EA")

    def _build_log_area(self) -> None:
        frame = tk.Frame(
            self.main_frame, bg="#1D1D1F",
            highlightbackground="#D2D2D7", highlightthickness=1,
        )
        frame.grid(row=8, column=0, sticky="ew")

        self.log_text = tk.Text(
            frame, wrap=tk.WORD, height=7, font=("Consolas", 10),
            bg="#1D1D1F", fg="#F5F5F7", insertbackground="#1D1D1F",
            relief=tk.FLAT, padx=12, pady=12, state="disabled",
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        scroll = tk.Scrollbar(frame, command=self.log_text.yview)
        scroll.pack(fill=tk.Y, side=tk.RIGHT)
        self.log_text.config(yscrollcommand=scroll.set)

    # ------------------------------------------------------------------
    # Config / browsing
    # ------------------------------------------------------------------
    def _save_config(self) -> None:
        self.config.set("downloads_dir", str(self.downloads_dir))
        self.config.set("dub_dir", self.dub_dir)
        self.config.set("use_browser_cookies", self.use_browser_cookies.get())
        self.config.set("concurrent_downloads", self.concurrent_var.get())
        self.config.set("max_concurrent", self.max_concurrent_var.get())
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

    def start_download(self) -> None:
        with self._state_lock:
            if self.downloading:
                return

        raw_input = self.input_text.get("1.0", tk.END).strip()
        lines = [line.strip() for line in raw_input.split("\n") if line.strip()]
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
        self.input_text.config(state="disabled", bg="#F5F5F7")

        sync_mode = self.sync_mode_var.get()
        self.cancelled_indices = set()
        
        # Hide input, show manager
        self.input_frame.grid_remove()
        self.download_manager = DownloadManager(
            self.main_frame, titles, self._cancel_single_item,
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
        errors_found: List[str],
        error_lock: threading.Lock,
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
            self._maybe_generate_srt(title, subs, aligner, errors_found, error_lock)
            return True
        else:
            self.log(f"-> Error downloading: {title} (Return code: {return_code})")
            if not is_retry:
                if self.download_manager:
                    self.root.after(0, self.download_manager.set_item_status, index, "Failed (Queued for Retry)", "#FF9500")
            else:
                if self.download_manager:
                    self.root.after(0, self.download_manager.set_item_status, index, "Failed", "#FF3B30")
            
            with error_lock:
                errors_found.append(f"Download Error for '{title}': Process exited with code {return_code}")
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
                        self._download_item_worker, i, title, link, subs, aligner, errors_found, error_lock
                    )] = i
                
                for future, item_index in futures.items():
                    success = future.result()
                    if not success and not self._is_cancelled() and item_index not in self.cancelled_indices:
                        failed_items.append(items[item_index])

            # Phase 2: Retry Failed Items
            if failed_items and not self._is_cancelled():
                self.log(f"\n--- Retrying {len(failed_items)} failed downloads ---")
                # We retry them sequentially or concurrently? 
                # User said "at the end of download give it another try", let's do them concurrently too but maybe with fewer workers or just same.
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    retry_futures = []
                    for i, title, link, subs in failed_items:
                        retry_futures.append(executor.submit(
                            self._download_item_worker, i, title, link, subs, aligner, errors_found, error_lock, is_retry=True
                        ))
                    for future in retry_futures:
                        future.result()

            if errors_found:
                self._save_errors(errors_found)

        except Exception as e:
            logger.exception("Batch download crashed")
            self.log(f"\nAn error occurred: {e}")
        finally:
            self.log(
                "\nBatch download cancelled by user."
                if self._is_cancelled()
                else "\nBatch download complete."
            )
            if self.download_manager:
                self.root.after(0, self.download_manager.destroy)
                self.download_manager = None
            self.input_frame.grid()
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
        errors_found: List[str],
        error_lock: Optional[threading.Lock] = None,
    ) -> None:
        if not subs:
            return

        safe_title = sanitize_filename(title)
        srt_path = self.downloads_dir / f"{safe_title} (SRT).srt"
        whisper_success = False

        if aligner is not None:
            if self._is_cancelled():
                return

            audio_source = self._get_dub_track(title)
            if audio_source:
                self.log("-> Starting Whisper Smart Sync... (This may take a minute)")
                try:
                    whisper_success = aligner.align(
                        audio_source, subs, srt_path, is_cancelled=self._is_cancelled,
                    )
                except Exception as e:
                    logger.exception("Whisper sync failed for %s", title)
                    self.log(f"-> Error with Whisper Sync: {e}")
                    err_msg = f"Whisper Error for '{title}': {e}"
                    if error_lock:
                        with error_lock:
                            errors_found.append(err_msg)
                    else:
                        errors_found.append(err_msg)
                    self.log("-> Falling back to 2-second timestamps...")
            else:
                self.log("-> No matching dub found in folder. Falling back to 2-second timestamps.")

        if not whisper_success and not self._is_cancelled():
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
    # UI reset
    # ------------------------------------------------------------------
    def reset_ui(self) -> None:
        with self._state_lock:
            self.downloading = False
        self.download_btn.config_state("normal", text="Start Download", bg=self.accent_color)
        self.cancel_btn.config_state("disabled", bg="#E5E5EA")
        self.browse_btn.config_state("normal", bg=self.gray_bg)
        self.input_text.config(state="normal", bg="white")


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
