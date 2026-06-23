"""Short-form clip generation from long videos.

Pipeline (audio-only analysis to keep it cheap):
1. Transcribe the long video's audio into a timed transcript (done by the caller
   via :class:`subtitles.WhisperAligner`).
2. Send just the *text* transcript (with timestamps) to an LLM via OpenRouter,
   which returns the most interesting moments as clip time-ranges + titles.
3. Optionally render each pick as a vertical 9:16 short with burned-in captions
   using ffmpeg (blurred-fill background + centered original + subtitles).

Only the transcript text is sent to the LLM and only the chosen ranges are cut,
so cost/processing stay low. This module is pure logic (no Tkinter).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]
Segment = Tuple[float, float, str]

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _creationflags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _format_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# ---------------------------------------------------------------------------
# Highlight selection (OpenRouter)
# ---------------------------------------------------------------------------
def _build_prompt(segments: List[Segment], num_clips: int,
                  min_dur: float, max_dur: float) -> str:
    lines = [f"[{s:.1f}-{e:.1f}] {t}" for s, e, t in segments]
    transcript = "\n".join(lines)
    return (
        f"You are a short-form video editor. Below is a timed transcript of a long "
        f"video (timestamps in seconds). Pick the {num_clips} most interesting, "
        f"self-contained moments that would make great standalone short clips "
        f"(hooks, surprising facts, strong statements, punchlines).\n\n"
        f"Rules:\n"
        f"- Each clip must be between {min_dur:.0f} and {max_dur:.0f} seconds long.\n"
        f"- start/end must fall on the transcript's timestamps and not overlap.\n"
        f"- Prefer clips that start at a natural sentence beginning.\n"
        f"- Respond with ONLY a JSON array, no prose, no markdown fences.\n\n"
        f'Each item: {{"start": <sec>, "end": <sec>, "title": "<short catchy title>", '
        f'"reason": "<why it works, one line>"}}\n\n'
        f"Transcript:\n{transcript}"
    )


def _extract_json_array(text: str) -> Optional[list]:
    """Parse a JSON array from a model reply, tolerating ```json fences / prose."""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def find_highlights(
    api_key: str,
    model: str,
    segments: List[Segment],
    num_clips: int = 5,
    min_dur: float = 20.0,
    max_dur: float = 60.0,
    log: LogFn = print,
    proxy: str = "",
    trust_env: bool = True,
    timeout: int = 120,
) -> List[dict]:
    """Ask the LLM for highlight clips. Returns a list of dicts with keys
    ``start, end, title, reason`` (clamped/validated), or [] on failure."""
    if not api_key:
        log("[!] Shorts: OpenRouter API key is missing (set it in the Shorts tab).")
        return []
    if not segments:
        log("[!] Shorts: no transcript to analyze.")
        return []

    total = segments[-1][1]
    payload = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": "You return only valid JSON. No markdown, no commentary."},
            {"role": "user",
             "content": _build_prompt(segments, num_clips, min_dur, max_dur)},
        ],
        "temperature": 0.4,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # OpenRouter likes these for attribution; harmless if ignored.
        "HTTP-Referer": "https://github.com/erfsalehi/Tovo-Video-Downloader",
        "X-Title": "Tovo Video Downloader",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None

    log(f"-> Asking {model} for {num_clips} highlight clips...")
    try:
        session = requests.Session()
        session.trust_env = trust_env
        resp = session.post(OPENROUTER_URL, headers=headers, json=payload,
                            timeout=timeout, proxies=proxies)
    except requests.RequestException as e:
        log(f"[!] Shorts: OpenRouter request failed: {e}")
        return []
    if resp.status_code != 200:
        log(f"[!] Shorts: OpenRouter error {resp.status_code}: {resp.text[:300]}")
        return []

    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as e:
        log(f"[!] Shorts: unexpected OpenRouter response: {e}")
        return []

    items = _extract_json_array(content)
    if not isinstance(items, list):
        log("[!] Shorts: model did not return a JSON array of clips.")
        return []

    clips: List[dict] = []
    for it in items:
        try:
            start = max(0.0, float(it["start"]))
            end = min(total, float(it["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if end - start < 1.0:
            continue
        clips.append({
            "start": start,
            "end": end,
            "title": str(it.get("title", "")).strip() or "clip",
            "reason": str(it.get("reason", "")).strip(),
        })
    clips.sort(key=lambda c: c["start"])
    log(f"-> Got {len(clips)} clip suggestion(s).")
    return clips


# ---------------------------------------------------------------------------
# Rendering (ffmpeg): vertical 9:16 + burned captions
# ---------------------------------------------------------------------------
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(title: str, max_len: int = 60) -> str:
    name = re.sub(r"\s+", " ", title or "").strip()
    name = INVALID_FILENAME_CHARS.sub("", name).strip(" .")
    return (name[:max_len].rsplit(" ", 1)[0].strip() if len(name) > max_len else name) or "clip"


def write_clip_srt(segments: List[Segment], start: float, end: float, out_srt: Path) -> bool:
    """Write a per-clip .srt covering [start, end], with times shifted so the clip
    begins at 0 (what the burned-in subtitles filter expects)."""
    rows = []
    for s, e, t in segments:
        if e <= start or s >= end:
            continue
        cs = max(0.0, s - start)
        ce = min(end, e) - start
        if ce > cs:
            rows.append((cs, ce, t))
    if not rows:
        return False
    with out_srt.open("w", encoding="utf-8") as f:
        for i, (cs, ce, t) in enumerate(rows, 1):
            f.write(f"{i}\n{_format_ts(cs)} --> {_format_ts(ce)}\n{t}\n\n")
    return True


def render_short(
    ffmpeg: str,
    src_video: str,
    start: float,
    end: float,
    out_path: Path,
    segments: Optional[List[Segment]] = None,
    burn_captions: bool = True,
    log: LogFn = print,
    register: Optional[Callable[[subprocess.Popen], None]] = None,
    unregister: Optional[Callable[[subprocess.Popen], None]] = None,
) -> bool:
    """Cut [start, end] from ``src_video`` and render a 1080x1920 vertical short:
    a blurred fill background, the original centered, and (optionally) burned-in
    captions. Returns True on success."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.1, end - start)

    # Per-clip subtitles live next to the output with an ASCII name; we run ffmpeg
    # with cwd = output dir and reference it by basename, sidestepping the
    # subtitles filter's painful path-escaping on Windows.
    clip_srt_name = out_path.stem + ".__cap.srt"
    clip_srt = out_path.parent / clip_srt_name
    have_caps = False
    if burn_captions and segments:
        have_caps = write_clip_srt(segments, start, end, clip_srt)

    # Build the vertical filter: blurred cover background + centered foreground.
    vf = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=24:2[bgb];"
        "[fg]scale=1080:-2:force_original_aspect_ratio=decrease[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[v0]"
    )
    if have_caps:
        style = ("FontName=Arial,Fontsize=14,PrimaryColour=&H00FFFFFF,"
                 "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,"
                 "Alignment=2,MarginV=120")
        vf += f";[v0]subtitles={clip_srt_name}:force_style='{style}'[vout]"
    else:
        vf += ";[v0]copy[vout]"

    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(src_video), "-t", f"{dur:.3f}",
        "-filter_complex", vf, "-map", "[vout]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        out_path.name,
    ]

    log(f"-> Rendering short: {out_path.name}  ({start:.1f}s–{end:.1f}s)"
        + ("  +captions" if have_caps else ""))
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(out_path.parent), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, creationflags=_creationflags(),
        )
    except OSError as e:
        log(f"[!] Failed to launch ffmpeg: {e}")
        return False
    if register:
        register(proc)
    tail: List[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                tail.append(line)
                if len(tail) > 15:
                    tail.pop(0)
        proc.wait()
    finally:
        if unregister:
            unregister(proc)
        try:
            if clip_srt.exists():
                clip_srt.unlink()
        except OSError:
            pass

    if proc.returncode != 0:
        log(f"[!] ffmpeg failed for {out_path.name}:")
        for ln in tail[-5:]:
            log(f"    {ln}")
        return False
    return out_path.exists()
