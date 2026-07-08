# Tovo Video Downloader 🚀

**An AI-powered desktop toolkit for content creators — download, transcribe, caption, dub, and clip video, all from one clean GUI.**

Built on `yt-dlp`, **Groq's cloud Whisper (whisper-large-v3)**, an **OpenRouter LLM**, and **RVC voice conversion**, Tovo Video Downloader turns the repetitive grind of a video-localization pipeline — sourcing footage, transcribing it, captioning it, dubbing it into another voice, and cutting it into shorts — into a paste-and-go workflow across seven tabs.

---

## ✨ What it does

### 1 · Downloads
Batch-download videos from YouTube and the many other sites `yt-dlp` supports.

- **Paste-and-go batches** — alternate lines of *title* / *link* and the app queues them all.
- **Parallel downloads** — configurable simultaneous downloads with per-item progress, cancel, and skip.
- **Premiere-ready output** — merges best video + audio into a single H.264/AAC **MP4**, the format editors actually want on the timeline (intentionally capped at H.264 quality since YouTube has no H.264 above 1080p).
- **Bot-detection bypass** — optionally pass **Chrome cookies** (or a local `cookies.txt`) and a real browser user-agent, or switch to the **TV/mweb player client**, when YouTube's default extractor is throttled.
- **Proxy controls** — route through a proxy or disable the system proxy.
- **Inline subtitle sync** — generate an `.srt` per download, either at fixed 2-second intervals or via **AI smart-sync** (Groq Whisper word-alignment).
- **Styled `.ttml` export** — optionally export a styled W3C TTML caption file alongside every `.srt`, with a persisted font/color/position/corner-radius style that imports cleanly into Premiere Pro.

### 2 · Transcription
Turn any batch of videos into transcripts without keeping the video pixels.

- **Audio-only extraction** so transcription never wastes bandwidth on video.
- Powered by **Groq's cloud `whisper-large-v3`** — the same engine and API key back every AI feature in the app (Sync, Captions, Shorts, Voiceover).
- **Concurrent transcriptions** with progress and skip controls, outputting clean `.srt`/text files.

### 3 · Sync SRT
Re-time subtitles you already have against real audio.

- Re-aligns an existing `.srt` — using a **dub track** from the Dub folder when available, otherwise the downloaded video's own audio.
- Uses Groq Whisper to get word-level timestamps, then redistributes them across your existing lines.
- **Keep each line on screen until the next** (gap-fill) option.
- Writes a new `*(Synced).srt` and **keeps the original** untouched.

### 4 · Voiceover — AI dubbing & voice cloning
An end-to-end pipeline that turns raw voice clips into a finished dub track in someone else's voice, using **RVC (Retrieval-based Voice Conversion)**.

- **Batch inputs** — point it at any mix of folders and `.zip` archives of raw clips.
- **Voice routing by filename** — clips are automatically routed to the right voice model by their filename prefix (e.g. multiple speakers/characters mapped to multiple trained RVC voices), with per-voice pitch, index rate, F0 method (RMVPE/Harvest/Crepe/PM), protect, volume-envelope, and resample settings, all editable in the UI.
- **AI title transcription** — the first few seconds of each clip are transcribed with Groq Whisper (forced to English) to auto-generate a clean, de-duplicated filename for the finished dub, even when the spoken body is in another language.
- **Automatic silence shortening** — detects overly long pauses via `ffmpeg silencedetect` and rebuilds the audio with tighter, natural-sounding gaps (configurable threshold, target length, noise floor, and padding so word-tails never clip).
- **RVC voice conversion** — runs the clip through a local Mangio-RVC-v23.7.0 install to convert it to the target voice, with live per-clip progress in the UI.
- **GPU or CPU** — auto-detects CUDA and uses the GPU when available, with a manual override to force CPU.
- Converted clips land, deduplicated, directly in your Dub folder — ready to feed straight into the Downloads or Sync SRT tabs.

### 5 · Captions (Auto-Caption)
Batch-generates a fresh `.srt` from audio/video that doesn't have one yet (or re-captions on demand).

- Transcribes from scratch via Groq Whisper with segment-level timestamps.
- Language selector and gap-fill option.
- Every caption run can also emit a styled `.ttml` alongside the `.srt`.

### 6 · Shorts — AI highlight detection & vertical clips
Turns one long video into a set of short-form vertical clips, powered by an LLM.

- **Transcribe** the video's audio (Groq Whisper) into a timed transcript.
- **AI highlight selection** — the timed transcript (text only, no audio/video sent) is sent to an **OpenRouter LLM** (model is configurable — defaults to DeepSeek, but any OpenRouter model slug works) which picks the most compelling moments, respecting natural sentence boundaries, your target clip count and duration range, and returns a title for each in the transcript's own language.
- **Render modes** — full vertical **9:16** render with a blurred/cropped background behind the centered original footage, or a **lossless 16:9 cut** that stream-copies the range with no re-encode.
- **Burned-in captions with right-to-left support** — captions are drawn as rounded "pill" overlays via Pillow instead of standard subtitle rendering, specifically so **Persian/Arabic (RTL) text shapes and reorders correctly** on screen.
- **Test Connection** button to check your OpenRouter connectivity/API key independently of running a full analysis.

### 7 · Text → SRT
Hand-write captions without any audio at all — paste text, and each non-blank line becomes one subtitle timed to a fixed 2-second interval. Useful for scripting captions before a recording exists.

### Quality-of-life
- **Zero-config bootstrap** — on first run the app downloads `yt-dlp`, `FFmpeg`, and `Deno` automatically if they aren't on your PATH.
- **Auto-update** — one-click "Force Update yt-dlp & FFmpeg" from the Tools menu, plus a periodic update check.
- **Persistent settings** — folders, API keys, and toggles are saved to a local `config.json` (atomic writes, gitignored).
- **Optional dub workflow** — point the app at a Dub folder of external audio (from the Voiceover tab or elsewhere) to merge and sync captions against a re-voiced track.

---

## 🤖 AI under the hood

| Feature | Engine | What it does |
|---|---|---|
| Transcription, Sync SRT, Auto-Caption, Shorts transcript, Voiceover title extraction | **Groq Cloud Whisper (`whisper-large-v3`)** | Cloud speech-to-text with word/segment timestamps. This is the single transcription engine for the whole app — there is no offline/local Whisper anymore. |
| Shorts highlight picking | **OpenRouter LLM** (default DeepSeek, any model slug supported) | Reads a plain-text timed transcript and returns the best clip ranges + titles as JSON — never sees audio or video, keeping it fast and cheap. |
| Voiceover / dubbing | **RVC (Retrieval-based Voice Conversion)** via a local Mangio-RVC-v23.7.0 install | Local, GPU-accelerated voice cloning/conversion — clones a target voice onto your source clips entirely on your own machine. |

Both cloud services (Groq and OpenRouter) sit behind Cloudflare, which can reset naive HTTPS clients as bot-like traffic; the app routes around this with the official `groq` SDK and a `curl.exe`-based transport for OpenRouter, and honors an explicit proxy/VPN if you need one.

---

## 🛠️ Installation & Setup

**Clone the repo:**
```bash
git clone https://github.com/erfsalehi/Tovo-Video-Downloader.git
cd Tovo-Video-Downloader
```

**Run it:**
- **Windows** — double-click `Start.bat`. It creates an isolated venv, installs `requirements.txt`, and launches the app.
- **macOS / Linux** — run `./start.sh`.

On first launch the app offers to fetch `yt-dlp`, `FFmpeg`, and `Deno` if they're missing. On macOS/Linux you can instead install them via your package manager (e.g. `brew install ffmpeg deno yt-dlp`).

**For AI features you'll need:**
- A free **Groq API key** from [console.groq.com](https://console.groq.com/) — powers Transcription, Sync SRT, Auto-Caption, Shorts transcription, and Voiceover title detection.
- An **OpenRouter API key** from [openrouter.ai](https://openrouter.ai/) — powers Shorts highlight selection (only needed for that tab).
- A local **Mangio-RVC-v23.7.0** install with your trained voice models — only needed for the Voiceover/dubbing tab. The app auto-detects a nearby install, or you can point it at one manually.

Keys and folders are entered directly in the app and stored locally in `config.json` (gitignored) — nothing is sent anywhere except the two APIs above.

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

For transcription, re-syncing subtitles, dubbing, captioning, or generating shorts, switch to the corresponding tab and follow the same paste-pick-run flow — each tab's log console shows live progress.

> **Groq transcription** needs a free API key from [console.groq.com](https://console.groq.com/) — paste it into the Transcription tab's *Groq Key* field. **OpenRouter** (Shorts tab) needs a key from [openrouter.ai](https://openrouter.ai/). Both are stored locally in `config.json`, which is gitignored.

---

## 📂 Project structure

```
Tovo-Video-Downloader/
├── app.py            # GUI + all seven tabs and orchestration
├── widgets.py        # Custom Tkinter widgets (rounded buttons, tab switcher, etc.)
├── dependencies.py   # First-run bootstrap for yt-dlp, FFmpeg, and Deno
├── subtitles.py      # SRT generation, Groq Whisper alignment/transcription, TTML export
├── voiceover.py      # Dubbing pipeline: rename, silence-shortening, RVC voice routing
├── rvc_batch.py      # Corrected RVC batch-inference runner (fixes a stale Mangio script)
├── shortclips.py     # Shorts pipeline: transcript, OpenRouter highlight picking, vertical render
├── config.py         # Persistent settings with atomic JSON writes
├── release_app.py    # Packaging helper for distribution
├── Start.bat / start.sh   # Windows / Unix launchers
└── requirements.txt
```

---

## 📄 License & responsible use

For personal and educational use. Always respect the terms of service and copyright of the platforms you download from, and only download, dub, or re-distribute content you have the right to use — including consent for any voice you clone.

---
*Created with ❤️ for efficient content creation.*
