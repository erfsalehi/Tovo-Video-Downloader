import tkinter as tk
from tkinter import messagebox, filedialog, ttk
import threading
import subprocess
import os
import json
import shutil
import urllib.request
import zipfile
import ssl

class RoundedButton(tk.Canvas):
    def __init__(self, parent, text, command=None, radius=20, bg_color="#0066CC", hover_color="#0055B3", text_color="white", font=("Segoe UI", 12, "bold"), **kwargs):
        super().__init__(parent, bg=parent["bg"], highlightthickness=0, **kwargs)
        self.text = text
        self.command = command
        self.radius = radius
        self.bg_color = bg_color
        self.hover_color = hover_color
        self.text_color = text_color
        self.font = font
        self.disabled = False
        self.default_bg = bg_color
        
        self.bind("<Configure>", self._draw)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _draw(self, event=None):
        self.delete("all")
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            return
            
        r = self.radius
        
        self.create_polygon(
            r, 0, width - r, 0,
            width, 0, width, r,
            width, height - r, width, height,
            width - r, height, r, height,
            0, height, 0, height - r,
            0, r, 0, 0,
            fill=self.bg_color, smooth=True
        )
        self.create_text(width / 2, height / 2, text=self.text, fill=self.text_color, font=self.font)

    def _on_press(self, event):
        if not self.disabled:
             # slight shade on click
             pass

    def _on_release(self, event):
        if not self.disabled and self.command:
            self.command()

    def _on_enter(self, event):
        if not self.disabled:
            self.bg_color = self.hover_color
            self._draw()
            self.config(cursor="hand2")

    def _on_leave(self, event):
        if not self.disabled:
            self.bg_color = self.default_bg
            self._draw()
            self.config(cursor="arrow")

    def config_state(self, state, text=None, bg=None):
        self.disabled = (state == 'disabled')
        if text:
            self.text = text
        if bg:
            self.bg_color = bg
            self.default_bg = bg
        self._draw()
        if self.disabled:
            self.config(cursor="arrow")


class AppleStyleApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Video Downloader")
        self.root.geometry("800x800")
        self.root.minsize(700, 700)
        
        self.config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        self._load_config()
        os.makedirs(self.downloads_dir, exist_ok=True)
        
        # Apple-like colors and fonts
        self.bg_color = "#F5F5F7"
        
        # Modern indigo/blue gradient-like palette
        self.accent_color = "#5E5CE6" # Modern iOS Indigo
        self.accent_hover = "#4B49B8"
        self.text_color = "#1D1D1F"
        
        # Clean gray
        self.gray_bg = "#E5E5EA"
        self.gray_hover = "#D1D1D6"
        
        self.font_family = "Segoe UI" if os.name == 'nt' else "Helvetica"
        
        self.root.configure(bg=self.bg_color)
        
        # Grid config for root
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        # Main container
        self.main_frame = tk.Frame(root, bg=self.bg_color)
        self.main_frame.grid(row=0, column=0, sticky="nsew", padx=35, pady=20)
        
        self.main_frame.grid_columnconfigure(0, weight=1)
        self.main_frame.grid_rowconfigure(2, weight=1) 

        # Row 0: Header
        self.header = tk.Label(self.main_frame, text="Batch Downloader", bg=self.bg_color, fg=self.text_color, font=(self.font_family, 26, "bold"))
        self.header.grid(row=0, column=0, sticky="w", pady=(0, 5))
        
        # Row 1: Subheader
        self.subheader = tk.Label(self.main_frame, text="Paste Title on line 1, Link on line 2, Title on line 3, etc.", bg=self.bg_color, fg="#86868B", font=(self.font_family, 11))
        self.subheader.grid(row=1, column=0, sticky="w", pady=(0, 10))

        # Row 2: Input Area Container
        self.input_frame = tk.Frame(self.main_frame, bg="white", highlightbackground="#D2D2D7", highlightthickness=1)
        self.input_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 15))
        
        self.input_text = tk.Text(self.input_frame, wrap=tk.WORD, font=(self.font_family, 12), bg="white", fg=self.text_color, insertbackground=self.accent_color, relief=tk.FLAT, padx=15, pady=15)
        self.input_text.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        self.input_scroll = tk.Scrollbar(self.input_frame, command=self.input_text.yview)
        self.input_scroll.pack(fill=tk.Y, side=tk.RIGHT)
        self.input_text.config(yscrollcommand=self.input_scroll.set)

        # Let's explicitly ensure it's normal and add a handy right-click menu for pasting
        self.input_text.config(state='normal')
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="Cut", command=lambda: self.input_text.event_generate("<<Cut>>"))
        self.context_menu.add_command(label="Copy", command=lambda: self.input_text.event_generate("<<Copy>>"))
        self.context_menu.add_command(label="Paste", command=lambda: self.input_text.event_generate("<<Paste>>"))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Select All", command=lambda: self.input_text.event_generate("<<SelectAll>>"))
        
        def show_context_menu(event):
            try:
                self.context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.context_menu.grab_release()

        self.input_text.bind("<Button-3>", show_context_menu)

        # Standard Keyboard Shortcuts for Text Area
        self.input_text.bind("<Control-a>", lambda e: self.input_text.tag_add(tk.SEL, "1.0", tk.END) or "break")
        self.input_text.bind("<Control-c>", lambda e: self.input_text.event_generate("<<Copy>>") or "break")
        self.input_text.bind("<Control-v>", lambda e: self.input_text.event_generate("<<Paste>>") or "break")
        self.input_text.bind("<Control-x>", lambda e: self.input_text.event_generate("<<Cut>>") or "break")
        # Support uppercase too just in case
        self.input_text.bind("<Control-A>", lambda e: self.input_text.tag_add(tk.SEL, "1.0", tk.END) or "break")
        self.input_text.bind("<Control-C>", lambda e: self.input_text.event_generate("<<Copy>>") or "break")
        self.input_text.bind("<Control-V>", lambda e: self.input_text.event_generate("<<Paste>>") or "break")
        self.input_text.bind("<Control-X>", lambda e: self.input_text.event_generate("<<Cut>>") or "break")

        # Row 3: Save Location & Browse
        self.save_frame = tk.Frame(self.main_frame, bg=self.bg_color)
        self.save_frame.grid(row=3, column=0, sticky="ew", pady=(0, 15))
        self.save_frame.grid_columnconfigure(1, weight=1)
        
        tk.Label(self.save_frame, text="Save to:", bg=self.bg_color, fg=self.text_color, font=(self.font_family, 11, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.dir_label = tk.Label(self.save_frame, text=self.downloads_dir, bg=self.bg_color, fg="#555555", font=(self.font_family, 10))
        self.dir_label.grid(row=0, column=1, sticky="w")
        
        # Modern Flat Rounded Browse Button
        self.browse_btn = RoundedButton(
            self.save_frame, text="Browse...", command=self.browse_directory,
            radius=15, bg_color=self.gray_bg, hover_color=self.gray_hover, text_color=self.text_color,
            font=(self.font_family, 10, "bold"), width=100, height=35
        )
        self.browse_btn.grid(row=0, column=2, sticky="e")

        # Row 4: Options Frame
        self.options_frame = tk.Frame(self.main_frame, bg=self.bg_color)
        self.options_frame.grid(row=4, column=0, sticky="ew", pady=(0, 15))
        
        tk.Label(self.options_frame, text="Subtitle Sync Mode:", bg=self.bg_color, fg=self.text_color, font=(self.font_family, 11, "bold")).pack(side=tk.LEFT, padx=(0, 10))
        
        self.sync_mode_var = tk.StringVar(value="None (2-second intervals)")
        self.sync_combo = ttk.Combobox(self.options_frame, textvariable=self.sync_mode_var, values=["None (2-second intervals)", "Whisper AI (Smart Sync)"], state="readonly", font=(self.font_family, 10))
        self.sync_combo.pack(side=tk.LEFT)

        # Row 5: Dub Audio Location & Browse
        self.dub_frame = tk.Frame(self.main_frame, bg=self.bg_color)
        self.dub_frame.grid(row=5, column=0, sticky="ew", pady=(0, 15))
        self.dub_frame.grid_columnconfigure(1, weight=1)
        
        tk.Label(self.dub_frame, text="(Optional) Dub Folder:", bg=self.bg_color, fg=self.text_color, font=(self.font_family, 11, "bold")).grid(row=0, column=0, sticky="w", padx=(0, 10))
        
        self.dub_dir = self.dub_dir  # Already set by _load_config
        dub_label_text = self.dub_dir if self.dub_dir else "Not Selected (Uses Video Audio)"
        self.dub_dir_label = tk.Label(self.dub_frame, text=dub_label_text, bg=self.bg_color, fg="#555555", font=(self.font_family, 10))
        self.dub_dir_label.grid(row=0, column=1, sticky="w")
        
        self.dub_browse_btn = RoundedButton(
            self.dub_frame, text="Browse...", command=self.browse_dub_directory,
            radius=15, bg_color=self.gray_bg, hover_color=self.gray_hover, text_color=self.text_color,
            font=(self.font_family, 10, "bold"), width=100, height=35
        )
        self.dub_browse_btn.grid(row=0, column=2, sticky="e")


        self.button_frame = tk.Frame(self.main_frame, bg=self.bg_color)
        self.button_frame.grid(row=7, column=0, sticky="ew", pady=(0, 20))
        self.button_frame.grid_columnconfigure(0, weight=1)
        self.button_frame.grid_columnconfigure(1, weight=1)

        self.download_btn = RoundedButton(
            self.button_frame, text="Start Download", command=self.start_download,
            radius=20, bg_color=self.accent_color, hover_color=self.accent_hover, text_color="white",
            font=(self.font_family, 13, "bold"), height=50
        )
        self.download_btn.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        self.cancel_btn = RoundedButton(
            self.button_frame, text="Cancel", command=self.cancel_download,
            radius=20, bg_color="#FF3B30", hover_color="#D70A01", text_color="white",
            font=(self.font_family, 13, "bold"), height=50
        )
        self.cancel_btn.grid(row=0, column=1, sticky="ew")
        self.cancel_btn.config_state('disabled', bg="#E5E5EA")

        # Row 8: Log Area
        self.log_frame = tk.Frame(self.main_frame, bg="#1D1D1F", highlightbackground="#D2D2D7", highlightthickness=1)
        self.log_frame.grid(row=8, column=0, sticky="ew")
        
        self.log_text = tk.Text(self.log_frame, wrap=tk.WORD, height=7, font=("Consolas", 10), bg="#1D1D1F", fg="#F5F5F7", insertbackground="#1D1D1F", relief=tk.FLAT, padx=12, pady=12, state='disabled')
        self.log_text.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        self.log_scroll = tk.Scrollbar(self.log_frame, command=self.log_text.yview)
        self.log_scroll.pack(fill=tk.Y, side=tk.RIGHT)
        self.log_text.config(yscrollcommand=self.log_scroll.set)
        
        self.downloading = False
        self.cancelled = False
        self.current_process = None

        # Dependency check on startup
        self.root.after(500, self.check_dependencies)

    def check_dependencies(self):
        """Checks if yt-dlp and ffmpeg are available."""
        missing = []
        
        # Check for yt-dlp
        yt_dlp_local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yt-dlp.exe")
        if not (shutil.which("yt-dlp") or os.path.exists(yt_dlp_local)):
            missing.append("yt-dlp")
            
        # Check for ffmpeg
        ffmpeg_local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg.exe")
        if not (shutil.which("ffmpeg") or os.path.exists(ffmpeg_local)):
            missing.append("FFmpeg")
            
        if missing:
            msg = f"The following essential tools are missing:\n\n" + "\n".join([f"• {m}" for m in missing])
            msg += "\n\nWithout these, downloads and high-quality processing will fail."
            msg += "\n\nWould you like the app to download them automatically for you? (approx. 100MB)"
            
            if messagebox.askyesno("Missing Dependencies", msg):
                threading.Thread(target=self.download_tools, daemon=True).start()
            else:
                self.log("[!] Warning: Missing dependencies. App may not function correctly.")

    def download_tools(self):
        """Downloads missing binaries."""
        self.log("--- Starting Dependency Setup ---")
        base_path = os.path.dirname(os.path.abspath(__file__))
        
        # Bypass SSL verification for some environments
        context = ssl._create_unverified_context()
        
        try:
            # 1. Download yt-dlp.exe
            yt_dlp_path = os.path.join(base_path, "yt-dlp.exe")
            if not os.path.exists(yt_dlp_path):
                self.log("-> Downloading yt-dlp.exe...")
                url = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
                with urllib.request.urlopen(url, context=context) as response, open(yt_dlp_path, 'wb') as out_file:
                    shutil.copyfileobj(response, out_file)
                self.log("   Done!")

            # 2. Download FFmpeg
            ffmpeg_path = os.path.join(base_path, "ffmpeg.exe")
            if not os.path.exists(ffmpeg_path):
                self.log("-> Downloading FFmpeg (This may take a moment)...")
                # Using a direct zip link from gyan.dev
                zip_url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
                temp_zip = os.path.join(base_path, "ffmpeg.zip")
                
                with urllib.request.urlopen(zip_url, context=context) as response, open(temp_zip, 'wb') as out_file:
                    shutil.copyfileobj(response, out_file)
                    
                self.log("-> Extracting FFmpeg...")
                with zipfile.ZipFile(temp_zip, 'r') as zip_ref:
                    # Find the bin folder inside the zip
                    for member in zip_ref.namelist():
                        if member.endswith("ffmpeg.exe"):
                            source = zip_ref.open(member)
                            with open(ffmpeg_path, 'wb') as target:
                                shutil.copyfileobj(source, target)
                                
                os.remove(temp_zip)
                self.log("   Done!")
                
            self.log("--- Setup Complete! You are ready to go. ---")
            messagebox.showinfo("Setup Complete", "All dependencies have been downloaded and installed successfully.")
            
        except Exception as e:
            self.log(f"-> Error during setup: {str(e)}")
            messagebox.showerror("Setup Error", f"Failed to download dependencies: {str(e)}\n\nPlease install them manually.")

    def _load_config(self):
        default_downloads = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Downloads")
        try:
            with open(self.config_path, 'r') as f:
                cfg = json.load(f)
            self.downloads_dir = cfg.get("downloads_dir", default_downloads)
            self.dub_dir = cfg.get("dub_dir", "")
        except Exception:
            self.downloads_dir = default_downloads
            self.dub_dir = ""

    def _save_config(self):
        try:
            with open(self.config_path, 'w') as f:
                json.dump({
                    "downloads_dir": self.downloads_dir, 
                    "dub_dir": self.dub_dir
                }, f)
        except Exception:
            pass

    def browse_directory(self):
        selected_dir = filedialog.askdirectory(initialdir=self.downloads_dir, title="Select Save Location")
        if selected_dir:
            self.downloads_dir = os.path.normpath(selected_dir)
            self.dir_label.config(text=self.downloads_dir)
            self._save_config()

    def browse_dub_directory(self):
        selected_dir = filedialog.askdirectory(title="Select Dub Audio Folder")
        if selected_dir:
            self.dub_dir = os.path.normpath(selected_dir)
            self.dub_dir_label.config(text=self.dub_dir)
            self._save_config()


    def cancel_download(self):
        if self.downloading and not self.cancelled:
            self.cancelled = True
            self.log("\n[!] Cancellation requested. Stopping process...")
            if self.current_process:
                try:
                    # On Windows, taskkill /F /T kills the process and its children
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/F', '/T', '/PID', str(self.current_process.pid)], capture_output=True)
                    else:
                        self.current_process.terminate()
                except Exception as e:
                    self.log(f"-> Error cancelling process: {str(e)}")
            self.reset_ui()

    def log(self, message):
        self.log_text.config(state='normal')
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state='disabled')
        self.root.update_idletasks()

    def start_download(self):
        if self.downloading:
            return

        raw_input = self.input_text.get("1.0", tk.END).strip()
        lines = [line.strip() for line in raw_input.split('\n') if line.strip()]

        if not lines:
            messagebox.showwarning("Input Error", "Please enter at least one title and one link.")
            return

        # Check for existing files and ask to cleanup
        if os.path.exists(self.downloads_dir):
            existing_files = [f for f in os.listdir(self.downloads_dir) if f.endswith(('.mp4', '.srt', '.txt', '.mp3', '.wav', '.m4a'))]
            if existing_files:
                if messagebox.askyesno("Cleanup Folder", f"Found {len(existing_files)} existing files in the download folder. Would you like to clear them first?"):
                    for f in existing_files:
                        try:
                            os.remove(os.path.join(self.downloads_dir, f))
                        except Exception as e:
                            self.log(f"-> Warning: Could not remove {f}: {str(e)}")

        titles = []
        links = []
        subtitles_list = []
        
        link_indices = []
        for i in range(len(lines)):
            line = lines[i]
            # Detect URLs to figure out where the pairs are
            if line.startswith("http://") or line.startswith("https://"):
                if i > 0:
                    title_candidate = lines[i-1]
                    # Ensure the title itself isn't a link
                    if not (title_candidate.startswith("http://") or title_candidate.startswith("https://")):
                        link_indices.append(i)

        for idx_pos, link_idx in enumerate(link_indices):
            title = lines[link_idx-1]
            link = lines[link_idx]
            titles.append(title)
            links.append(link)
            
            start_subtitle_idx = link_idx + 1
            if idx_pos + 1 < len(link_indices):
                end_subtitle_idx = link_indices[idx_pos+1] - 1
            else:
                end_subtitle_idx = len(lines)
                
            video_subtitles = lines[start_subtitle_idx:end_subtitle_idx]
            subtitles_list.append(video_subtitles)

        if not titles:
            messagebox.showwarning("Input Error", "Could not find any valid Title and Link pairs. Make sure every link has a title on the line above it.")
            return

        if any(len(subs) >= 2 for subs in subtitles_list):
            if messagebox.askyesno("Ignore Lines", "Would you like to ignore the first 2 lines of text between the videos? (e.g. Farsi descriptions)"):
                try:
                    titles_file_path = os.path.join(self.downloads_dir, "Titles.txt")
                    with open(titles_file_path, 'a', encoding='utf-8') as tf:
                        for i in range(len(subtitles_list)):
                            if len(subtitles_list[i]) >= 2:
                                # Store the ignored lines
                                tf.write(f"--- {titles[i]} ---\n")
                                tf.write(subtitles_list[i][0] + "\n")
                                tf.write(subtitles_list[i][1] + "\n\n")
                                
                                # Remove them from the list
                                subtitles_list[i] = subtitles_list[i][2:]
                            else:
                                subtitles_list[i] = []
                    self.log(f"-> Saved ignored lines to Titles.txt")
                except Exception as e:
                    self.log(f"-> Warning: Could not save Titles.txt: {str(e)}")

        self.downloading = True
        self.cancelled = False
        
        self.download_btn.config_state('disabled', text="Downloading...", bg="#A1C6EA")
        self.cancel_btn.config_state('normal', bg="#FF3B30")
        self.browse_btn.config_state('disabled')
        
        # Clear log
        self.log_text.config(state='normal')
        self.log_text.delete("1.0", tk.END)
        self.log_text.config(state='disabled')

        self.log(f"Starting batch download for {len(titles)} items...")
        
        # Disable input
        self.input_text.config(state='disabled', bg="#F5F5F7")
        
        # Start download thread
        sync_mode = self.sync_mode_var.get()
        threading.Thread(target=self.download_process, args=(titles, links, subtitles_list, sync_mode), daemon=True).start()

    def download_process(self, titles, links, subtitles_list, sync_mode):
        def format_time(seconds):
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            secs = seconds % 60
            ms = 0
            return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"

        try:
            # Save a text file with all titles and links
            try:
                batch_file_path = os.path.join(self.downloads_dir, "batch_links.txt")
                with open(batch_file_path, 'w', encoding='utf-8') as bf:
                    for title, link in zip(titles, links):
                        bf.write(f"{title}\n{link}\n\n")
                self.log(f"-> Saved batch_links.txt to {self.downloads_dir}")
            except Exception as e:
                self.log(f"-> Warning: Could not save batch_links.txt: {str(e)}")

            errors_found = []

            for i, (title, link, subs) in enumerate(zip(titles, links, subtitles_list), 1):
                if self.cancelled:
                    break

                self.log(f"\n[{i}/{len(titles)}] Downloading: {title}")
                self.log(f"URL: {link}")
                
                # Setup output template inside the dedicated Downloads folder
                output_path = os.path.join(self.downloads_dir, f"{title}.%(ext)s")
                final_video_path = os.path.join(self.downloads_dir, f"{title}.mp4")
                
                # Prefer local binaries if they exist
                base_path = os.path.dirname(os.path.abspath(__file__))
                yt_dlp_exe = os.path.join(base_path, "yt-dlp.exe")
                exe_path = yt_dlp_exe if os.path.exists(yt_dlp_exe) else "yt-dlp"

                cmd = [
                    exe_path,
                    "-o", output_path,
                    "--no-playlist",
                    # H.264 (avc1) + AAC (m4a) = best Premiere Pro compatibility
                    "-f", "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                    "--merge-output-format", "mp4",
                ]
                
                # If local ffmpeg exists, tell yt-dlp to use it
                ffmpeg_exe = os.path.join(base_path, "ffmpeg.exe")
                if os.path.exists(ffmpeg_exe):
                    cmd.extend(["--ffmpeg-location", ffmpeg_exe])

                cmd.append(link)
                
                if self.cancelled:
                    break

                creationflags = 0
                if os.name == 'nt':
                    creationflags = subprocess.CREATE_NO_WINDOW
                
                self.current_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    creationflags=creationflags
                )
                
                for line in self.current_process.stdout:
                    if self.cancelled:
                        break
                    self.log("  " + line.strip())
                    
                self.current_process.wait()
                return_code = self.current_process.returncode
                self.current_process = None

                if self.cancelled:
                    break
                
                if return_code == 0:
                    self.log(f"-> Successfully downloaded: {title}")
                    
                    # Create SRT file after download since Whisper needs the video/audio file
                    if subs:
                        srt_path = os.path.join(self.downloads_dir, f"{title} (SRT).srt")
                        whisper_success = False
                        
                        if sync_mode == "Whisper AI (Smart Sync)":
                            if self.cancelled: break
                            # Check for a matching dub audio file in the selected dub folder
                            audio_source = None
                            if hasattr(self, 'dub_dir') and self.dub_dir:
                                possible_exts = ['.mp3', '.wav', '.m4a']
                                for ext in possible_exts:
                                    dub_path = os.path.join(self.dub_dir, f"{title}{ext}")
                                    if os.path.exists(dub_path):
                                        audio_source = dub_path
                                        self.log(f"-> Found dub track: {title}{ext} - Syncing with Whisper AI!")
                                        break
                            
                            if audio_source:
                                self.log(f"-> Starting Whisper Smart Sync... (This may take a minute)")
                                try:
                                    if self.cancelled: raise Exception("Cancelled")
                                    import stable_whisper
                                    import whisper as _whisper
                                    import warnings
                                    import torch
                                    warnings.filterwarnings("ignore")
                                    
                                    # Load model
                                    model = stable_whisper.load_model('base')
                                    
                                    if self.cancelled: raise Exception("Cancelled")
                                    # Manual language detection using the whisper package directly
                                    self.log("-> Analyzing audio for language detection...")
                                    audio = _whisper.load_audio(audio_source)
                                    audio = _whisper.pad_or_trim(audio)
                                    mel = _whisper.log_mel_spectrogram(audio).to(model.device)
                                    _, probs = model.detect_language(mel)
                                    detected_lang = max(probs, key=probs.get)
                                    self.log(f"-> Detected language: '{detected_lang}'")

                                    text_to_align = "\n".join([line for line in subs if line.strip()])
                                    
                                    if self.cancelled: raise Exception("Cancelled")
                                    # Perform alignment - using positional arguments for language to be safe
                                    self.log(f"-> Syncing {len(subs)} lines...")
                                    result = model.align(audio_source, text_to_align, detected_lang)
                                    result.to_srt_vtt(srt_path, word_level=False)
                                    
                                    self.log(f"-> Whisper Sync successful! SRT saved.")
                                    whisper_success = True
                                except Exception as e:
                                    if str(e) == "Cancelled":
                                        pass
                                    else:
                                        import traceback
                                        err_detail = traceback.format_exc()
                                        self.log(f"-> Error with Whisper Sync: {str(e)}")
                                        errors_found.append(f"Whisper Error for '{title}': {str(e)}")
                                        print(err_detail) # Log full traceback to console/terminal
                                        self.log("-> Falling back to 2-second timestamps...")
                            else:
                                self.log(f"-> No matching dub found in folder. Falling back to 2-second timestamps.")
                        
                        # Fallback Or Standard Option
                        if not whisper_success and not self.cancelled:
                            try:
                                with open(srt_path, 'w', encoding='utf-8') as f:
                                    current_time = 0
                                    for j, sub_line in enumerate(subs, 1):
                                        f.write(f"{j}\n")
                                        start_time_str = format_time(current_time)
                                        current_time += 2
                                        end_time_str = format_time(current_time)
                                        f.write(f"{start_time_str} --> {end_time_str}\n")
                                        f.write(f"{sub_line}\n\n")
                                self.log(f"-> Created standard SRT file with {len(subs)} lines (2s intervals).")
                            except Exception as e:
                                self.log(f"-> Error creating SRT: {str(e)}")
                                
                else:
                    self.log(f"-> Error downloading: {title} (Return code: {return_code})")
                    errors_found.append(f"Download Error for '{title}': Process exited with code {return_code}")
            
            # Save errors to file if any
            if errors_found:
                try:
                    error_file_path = os.path.join(self.downloads_dir, "errors.txt")
                    with open(error_file_path, 'w', encoding='utf-8') as ef:
                        ef.write("--- Download Errors ---\n\n")
                        for err in errors_found:
                            ef.write(f"- {err}\n")
                    self.log(f"-> Exported {len(errors_found)} errors to errors.txt")
                except Exception as e:
                    self.log(f"-> Warning: Could not save errors.txt: {str(e)}")
                    
        except Exception as e:
            self.log(f"\nAn error occurred: {str(e)}")
            
        finally:
            if self.cancelled:
                self.log("\nBatch download cancelled by user.")
            else:
                self.log("\nBatch download complete.")
            self.root.after(0, self.reset_ui)

    def reset_ui(self):
        self.downloading = False
        self.download_btn.config_state('normal', text="Start Download", bg=self.accent_color)
        self.cancel_btn.config_state('disabled', bg="#E5E5EA")
        self.browse_btn.config_state('normal', bg=self.gray_bg)
        self.input_text.config(state='normal', bg="white")


if __name__ == "__main__":
    import ctypes
    # Make DPI aware (Windows) to avoid blurry text
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except:
        pass
        
    root = tk.Tk()
    app = AppleStyleApp(root)
    root.mainloop()
