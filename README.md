# Tovo Video Downloader 🚀

**A solo-built content-localization pipeline: source footage, clone a voice, dub it, quality-review the sync, shape RTL captions correctly, and cut it into vertical shorts — all from one desktop app.**

I built this to run my own dubbing/localization workflow end-to-end: raw voiceover clips go in, and a synced, captioned, re-voiced deliverable comes out. It combines **RVC voice cloning**, **Groq's cloud Whisper (`whisper-large-v3`)** for transcription and alignment, and an **OpenRouter LLM** for highlight selection, wrapped in seven tabs so each stage of the pipeline — dubbing, review/sync, captioning, clipping, sourcing — is a paste-and-go batch job instead of a manual one-off.

---

## ✨ What it does

### AI Dubbing & Voice Cloning
An end-to-end pipeline that turns raw voice clips into a finished dub track in a cloned voice, using **RVC (Retrieval-based Voice Conversion)**.

- **Batch inputs** — point it at any mix of folders and `.zip` archives of raw clips.
- **Voice routing by filename** — clips are automatically routed to the right voice model by filename prefix (multiple speakers/personas mapped to multiple trained RVC voices), with per-voice pitch, index rate, F0 method (RMVPE/Harvest/Crepe/PM), protect, volume-envelope, and resample settings, all editable in the UI.
- **AI title transcription** — the first few seconds of each clip are transcribed with Groq Whisper (forced to English) to auto-generate a clean, de-duplicated filename for the finished dub, even when the spoken body is in a different language — the real case this was built for: English-titled clips with Persian voiceover underneath.
- **Automatic silence shortening** — detects overly long pauses via `ffmpeg silencedetect` and rebuilds the audio with tighter, natural-sounding gaps (configurable threshold, target length, noise floor, and padding so word-tails never clip) — a production QA step, not just a technical nicety.
- **RVC voice conversion** — runs the clip through a local Mangio-RVC-v23.7.0 install to convert it to the target voice, with live per-clip progress in the UI. The bundled Mangio batch script was broken on newer dependency versions, so this pipeline ships its own corrected runner (`rvc_batch.py`).
- **GPU or CPU** — auto-detects CUDA and uses the GPU when available, with a manual override to force CPU.
- Converted clips land, deduplicated, directly in a shared Dub folder — ready to feed straight into the sync/review and download stages below.

### Quality Review & Localization Captions
Once a track is dubbed, the rest of the pipeline is about making sure the captions actually match — and read correctly.

- **Sync SRT** — re-times an existing `.srt` against the *real* dubbed audio (not the original), using Groq Whisper word-level timestamps redistributed across the existing lines, with a "keep each line on screen until the next" gap-fill mode. Writes a new `*(Synced).srt` and never touches the original — so a bad re-sync is always recoverable.
- **Auto-Caption** — batch-generates a fresh `.srt` from any audio/video that doesn't have one yet, from scratch via Groq Whisper.
- **Right-to-left caption shaping** — this was the part off-the-shelf subtitle tooling got wrong: Persian/Arabic text needs letter-joining and reordering to render correctly, which most caption renderers don't handle. Captions are drawn as Pillow-rendered "pill" overlays with `arabic_reshaper` + `python-bidi` doing the shaping, rather than relying on standard subtitle rendering.
- **Text → SRT** — hand-write captions with no audio at all: paste text, each line becomes a fixed 2-second subtitle. Useful for scripting captions before a dub even exists.
- **Styled `.ttml` export** — any `.srt` produced by the pipeline can also emit a sibling styled W3C TTML file (font, color, position, corner-radius) that imports into Premiere with formatting intact, so localized captions don't have to be re-styled by hand per deliverable.

### AI Highlight Detection & Vertical Shorts
Turns one long localized video into a set of short-form vertical clips — a repurposing/distribution step downstream of the dub.

- **Transcribe** the video's audio (Groq Whisper) into a timed transcript.
- **AI highlight selection** — the timed transcript (text only, no audio/video sent) goes to an **OpenRouter LLM** (configurable model, defaults to DeepSeek) which picks the most compelling moments, respecting natural sentence boundaries, a target clip count/duration range, and returns a title in the transcript's own language.
- **Render modes** — full vertical **9:16** render (blurred/cropped background behind centered footage) or a **lossless 16:9 cut** that stream-copies the range with no re-encode.
- Same RTL-correct burned-in captions as above.
- **Test Connection** button to isolate a network/API-key problem from an app problem before running a full analysis — added after repeated Cloudflare-related connectivity issues in the field.

### Sourcing: Downloads & Transcription
The tabs that feed the pipeline with source material.

- **Downloads** — batch-download videos from YouTube and the many other sites `yt-dlp` supports, paste-and-go title/link pairs, parallel downloads, Premiere-ready H.264/AAC MP4 output, Chrome-cookie/TV-client bot-detection bypass, proxy controls, and inline subtitle generation (fixed-interval or Groq Whisper smart-sync) as it downloads.
- **Transcription** — audio-only batch transcription via Groq's cloud `whisper-large-v3`, the same engine and API key that backs every AI feature above.

### Quality-of-life
- **Zero-config bootstrap** — on first run the app downloads `yt-dlp`, `FFmpeg`, and `Deno` automatically if they aren't on your PATH.
- **Auto-update** — one-click "Force Update yt-dlp & FFmpeg" from the Tools menu, plus a periodic update check.
- **Persistent settings** — folders, API keys, and toggles are saved to a local `config.json` (atomic writes, gitignored).

---

## 🤖 AI under the hood

| Stage | Engine | What it does |
|---|---|---|
| Voice cloning / dubbing | **RVC (Retrieval-based Voice Conversion)** via a local Mangio-RVC-v23.7.0 install | Local, GPU-accelerated voice conversion — clones a target voice onto source clips entirely on-machine. |
| Transcription, sync, captions, highlight transcripts | **Groq Cloud Whisper (`whisper-large-v3`)** | Cloud speech-to-text with word/segment timestamps. Single transcription engine for the whole app — offline/local Whisper was removed once Groq proved reliable enough to depend on. |
| Shorts highlight picking | **OpenRouter LLM** (default DeepSeek, any model slug supported) | Reads a plain-text timed transcript and returns the best clip ranges + titles as JSON — never sees audio or video, keeping it fast and cheap. |

Both cloud services (Groq and OpenRouter) sit behind Cloudflare, which can reset naive HTTPS clients as bot-like traffic; the app routes around this with the official `groq` SDK and a `curl.exe`-based transport for OpenRouter, and honors an explicit proxy/VPN if you need one — a real reliability problem that surfaced in production use, not a hypothetical.

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

**For the full pipeline you'll need:**
- A free **Groq API key** from [console.groq.com](https://console.groq.com/) — powers Transcription, Sync SRT, Auto-Caption, Shorts transcription, and Voiceover title detection.
- An **OpenRouter API key** from [openrouter.ai](https://openrouter.ai/) — powers Shorts highlight selection (only needed for that tab).
- A local **Mangio-RVC-v23.7.0** install with your trained voice models — only needed for the Voiceover/dubbing tab. The app auto-detects a nearby install, or you can point it at one manually.

Keys and folders are entered directly in the app and stored locally in `config.json` (gitignored) — nothing is sent anywhere except the two APIs above.

---

## 📖 How to use

1. **Voiceover tab** — point it at a folder or zip of raw clips and a Dub folder, pick device (Auto/GPU/CPU), and start; converted, retitled, de-silenced dubs land in the Dub folder.
2. **Sync SRT tab** — pick the video (and its dub track, if separate) plus its existing `.srt`, and re-sync against the real audio.
3. **Captions / Shorts / Text → SRT tabs** — same paste-pick-run flow, each with its own log console showing live progress.
4. **Downloads tab** — paste your batch as alternating title/link lines:
   ```
   Intro shot — city skyline
   https://www.youtube.com/watch?v=...
   B-roll — highway at night
   https://vimeo.com/...
   ```
   pick a Save-to folder (and optional Dub folder), toggle subtitle sync/cookies, and start.

> **Groq** needs a free API key from [console.groq.com](https://console.groq.com/) — paste it into the Transcription tab's *Groq Key* field. **OpenRouter** (Shorts tab) needs a key from [openrouter.ai](https://openrouter.ai/). Both are stored locally in `config.json`, which is gitignored.

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
*Created with ❤️ for efficient content localization.*
