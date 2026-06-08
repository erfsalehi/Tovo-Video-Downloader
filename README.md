# Tovo Video Downloader 🚀

**A desktop toolkit for content creators — batch-download videos, transcribe them, and re-sync subtitles, all from one clean GUI.**

Built on `yt-dlp` and Whisper, Tovo Video Downloader turns the repetitive grind of sourcing footage and captions into a paste-and-go workflow. Drop in a list of titles and links, pick a folder, and walk away — the app downloads everything in parallel as Premiere-ready MP4s, optionally generating and aligning subtitles as it goes.

---

## ✨ What it does

The app is organized into three tabs:

### 1 · Downloads
Batch-download videos from YouTube and the many other sites `yt-dlp` supports.

- **Paste-and-go batches** — alternate lines of *title* / *link* and the app queues them all.
- **Parallel downloads** — configurable simultaneous downloads (default 4) with per-item progress, cancel, and skip.
- **Premiere-ready output** — merges best video + audio into a single H.264/AAC **MP4** (`bv[ext=mp4]+ba[ext=m4a]`), the format editors actually want on the timeline.
- **Bot-detection bypass** — optionally pass **Chrome cookies** (or a local `cookies.txt`) and a real browser user-agent to get past "confirm you're not a robot" walls.
- **Proxy & client controls** — route through a proxy, disable the system proxy, or switch to the **TV/mweb player client** when YouTube's default extractor is throttled.
- **Inline subtitle sync** — generate an `.srt` per download, either at fixed 2-second intervals or via **Whisper smart-sync** that word-aligns captions to the audio.

### 2 · Transcription
Turn any batch of videos into transcripts without keeping the video pixels.

- **Audio-only extraction** (`-f bestaudio/best`) so transcription never wastes bandwidth on video.
- **Two engines** — **Groq AI** (cloud, fastest) or **Local Whisper** (offline, private).
- **Concurrent transcriptions** with progress and skip controls.
- Outputs clean `.srt` files to your chosen folder.

### 3 · Sync SRT
Re-time subtitles you already have.

- Re-aligns an existing `.srt` against the voiceover — using a **dub track** from the Dub folder when available, otherwise the downloaded video.
- Writes a new `*(Synced).srt` and **keeps the original** untouched.

### Quality-of-life
- **Zero-config bootstrap** — on first run the app downloads `yt-dlp`, `FFmpeg`, and `Deno` automatically if they aren't on your PATH (Deno is required by some of yt-dlp's YouTube extractors).
- **Auto-update** — one-click "Force Update yt-dlp & FFmpeg" from the Tools menu, plus a periodic update check.
- **Persistent settings** — folders, API key, and toggles are saved to a local `config.json` (atomic writes, gitignored).
- **Optional dub workflow** — point the app at a Dub folder of external audio (`.mp3/.wav/.m4a/.mp4`) to sync captions against a re-voiced track.

---

## 🛠️ Installation & Setup

**Clone the repo:**
```bash
git clone https://github.com/erfsalehi/Tovo-Video-Downloader.git
cd Tovo-Video-Downloader
```

**Run it:**
- **Windows** — double-click `Start.bat`. It creates an isolated venv, installs a lightweight **CPU-only PyTorch** (needed for Whisper smart-sync), installs the rest of `requirements.txt`, and launches the app.
- **macOS / Linux** — run `./start.sh`. For Whisper smart-sync, also install Torch inside the venv: `pip install torch torchaudio`.

On first launch the app offers to fetch `yt-dlp`, `FFmpeg`, and `Deno` if they're missing. On macOS/Linux you can instead install them via your package manager (e.g. `brew install ffmpeg deno yt-dlp`).

---

## 📖 How to use

1. **Downloads tab** — paste your batch as alternating title/link lines:
   ```
   Intro shot — city skyline
   https://www.youtube.com/watch?v=...
   B-roll — highway at night
   https://vimeo.com/...
   ```
2. Pick a **Save to** folder (and optionally a **Dub folder** if you'll sync captions to external audio).
3. Toggle **subtitle sync** and **Chrome cookies** if needed, then click **Start Download**.
4. Watch live per-item progress in the log; finished MP4s, SRTs, and a batch log land in your chosen folder.

For transcription or re-syncing existing subtitles, switch to the **Transcription** or **Sync SRT** tab and follow the same paste-pick-run flow.

> **Groq transcription** needs a free API key from [console.groq.com](https://console.groq.com/). Paste it into the Transcription tab's *Groq Key* field — it's stored locally in `config.json`, which is gitignored.

---

## 📂 Project structure

```
Tovo-Video-Downloader/
├── app.py            # GUI + the three tabs (Downloads / Transcription / Sync SRT) and orchestration
├── widgets.py        # Custom Tkinter widgets (rounded buttons, tab switcher, etc.)
├── dependencies.py   # First-run bootstrap for yt-dlp, FFmpeg, and Deno
├── subtitles.py      # SRT generation + Whisper-based alignment / re-sync
├── config.py         # Persistent settings with atomic JSON writes
├── release_app.py    # Packaging helper for distribution
├── Start.bat / start.sh   # Windows / Unix launchers
└── requirements.txt
```

---

## 📄 License & responsible use

For personal and educational use. Always respect the terms of service and copyright of the platforms you download from, and only download content you have the right to use.

---
*Created with ❤️ for efficient content creation.*
