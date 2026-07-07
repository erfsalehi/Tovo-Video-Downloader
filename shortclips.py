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
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]
Segment = Tuple[float, float, str]

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------
# openrouter.ai sits behind Cloudflare, which resets the TLS handshake of
# Python's requests/urllib3 (its ClientHello fingerprint reads as a bot) —
# producing "SSL: UNEXPECTED_EOF_WHILE_READING". The OS-native curl (Schannel on
# Windows) uses a browser-like fingerprint that Cloudflare accepts, so we send
# these requests through curl when it's available and fall back to requests
# otherwise. curl is bundled with Windows 10/11 and the app already shells out to
# other bundled .exe tools, so this needs no new dependency.
class _Resp:
    """Minimal requests.Response stand-in for curl output."""
    __slots__ = ("status_code", "text")

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


def _curl_exe() -> Optional[str]:
    exe = shutil.which("curl")
    if exe:
        return exe
    if os.name == "nt":
        sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                             "System32", "curl.exe")
        if os.path.isfile(sys32):
            return sys32
    return None


def _proxy_curl_args(proxy: str, trust_env: bool) -> List[str]:
    """Mirror requests' proxy behaviour for curl: explicit proxy wins; otherwise
    honour the *_proxy env vars when trust_env, or force direct when not."""
    if proxy:
        return ["-x", proxy]
    if not trust_env:
        return ["--noproxy", "*"]
    return []  # curl reads http_proxy/https_proxy from the environment by default


def _curl_request(exe: str, method: str, url: str,
                  headers: Optional[Dict[str, str]] = None,
                  data: Optional[str] = None, proxy: str = "",
                  trust_env: bool = True, timeout: int = 60) -> _Resp:
    """Perform an HTTP request via curl. Returns _Resp for any HTTP reply
    (including 4xx/5xx); raises RuntimeError only on a connection-level failure."""
    args = [exe, "-sS", "--max-time", str(timeout), "-w", "\n%{http_code}",
            "-X", method, url]
    for k, v in (headers or {}).items():
        args += ["-H", f"{k}: {v}"]
    args += _proxy_curl_args(proxy, trust_env)
    payload_bytes = None
    if data is not None:
        args += ["--data-binary", "@-"]  # body via stdin: no arg-length/quoting limits
        payload_bytes = data.encode("utf-8")
    try:
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        proc = subprocess.run(args, input=payload_bytes, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout + 15,
                              creationflags=creationflags)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"curl failed to run: {e}")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"curl exit {proc.returncode}: {err[:200]}")
    out = proc.stdout.decode("utf-8", "replace")
    nl = out.rfind("\n")
    code_str = (out[nl + 1:] if nl >= 0 else out).strip()
    body = out[:nl] if nl >= 0 else ""
    try:
        status = int(code_str)
    except ValueError:
        raise RuntimeError(f"curl returned no HTTP status (got {code_str!r})")
    return _Resp(status, body)

# OpenRouter needs the exact "vendor/model" slug, not a friendly label. Map the
# labels the UI has historically shown so an old saved value doesn't 400.
_MODEL_ALIASES = {
    "deepseek v4 pro": "deepseek/deepseek-v4-pro",
    "deepseek v4 flash": "deepseek/deepseek-v4-flash",
    "deepseek v4": "deepseek/deepseek-v4",
    "deepseek v3": "deepseek/deepseek-chat",
    "gpt-4o mini": "openai/gpt-4o-mini",
    "gpt-4o": "openai/gpt-4o",
    "claude 3.5 sonnet": "anthropic/claude-3.5-sonnet",
    "gemini 2.0 flash": "google/gemini-2.0-flash-001",
}


def _normalize_model(model: str) -> str:
    """Return a valid OpenRouter model slug, translating known friendly labels.
    Returns "" if the value can't be a slug (has spaces or no vendor/ prefix)."""
    model = (model or "").strip()
    alias = _MODEL_ALIASES.get(model.lower())
    if alias:
        return alias
    if not model or " " in model or "/" not in model:
        return ""
    return model


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
        f"You are an expert short-form video editor. Below is a timed transcript of "
        f"a long video (timestamps in seconds). Select the {num_clips} best moments "
        f"to cut into standalone vertical shorts.\n\n"
        f"What makes a great short:\n"
        f"- A strong hook in the first few seconds (a question, bold claim, or tease).\n"
        f"- One complete, self-contained idea, story, or joke — it must make sense "
        f"with zero outside context and land a clear payoff or conclusion.\n"
        f"- High emotional or informational punch: surprising facts, hot takes, "
        f"advice, or a satisfying reveal.\n\n"
        f"Hard rules:\n"
        f"- Each clip must be between {min_dur:.0f} and {max_dur:.0f} seconds long.\n"
        f"- start and end must be actual timestamps from the transcript; clips must "
        f"not overlap each other.\n"
        f"- Start at a natural sentence beginning and end at a natural sentence end — "
        f"never cut mid-sentence.\n"
        f"- Write \"title\" in the SAME language as the transcript.\n"
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
    max_attempts: int = 6,
    retry_delay: float = 2.0,
) -> List[dict]:
    """Ask the LLM for highlight clips. Returns a list of dicts with keys
    ``start, end, title, reason`` (clamped/validated), or [] on failure.

    The request is retried up to ``max_attempts`` times on connection errors,
    because openrouter.ai is often reached through a flaky VPN/proxy where any
    single attempt can be dropped mid-TLS-handshake."""
    if not api_key:
        log("[!] Shorts: OpenRouter API key is missing (set it in the Shorts tab).")
        return []
    if not segments:
        log("[!] Shorts: no transcript to analyze.")
        return []

    model = _normalize_model(model)
    if not model:
        log("[!] Shorts: that Model isn't a valid OpenRouter ID. Use a "
            "'vendor/model' slug, e.g. deepseek/deepseek-v4-flash.")
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
    exe = _curl_exe()

    log(f"-> Asking {model} for {num_clips} highlight clips...")
    body = json.dumps(payload)
    resp = None
    last_err: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            if exe:
                resp = _curl_request(exe, "POST", OPENROUTER_URL, headers=headers,
                                     data=body, proxy=proxy, trust_env=trust_env,
                                     timeout=timeout)
            else:
                session = requests.Session()
                session.trust_env = trust_env
                resp = session.post(OPENROUTER_URL, headers=headers, json=payload,
                                    timeout=timeout, proxies=proxies)
            break
        except (requests.RequestException, RuntimeError) as e:
            last_err = e
            if attempt < max_attempts:
                log(f"-> OpenRouter connection dropped (attempt {attempt}/{max_attempts}), "
                    "retrying…")
                time.sleep(retry_delay)
    if resp is None:
        log(f"[!] Shorts: OpenRouter request failed after {max_attempts} attempts: {last_err}")
        log("    openrouter.ai looks unreachable — check your internet/VPN, then try again.")
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
# Connection diagnostics
# ---------------------------------------------------------------------------
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


def test_connection(
    api_key: str = "",
    proxy: str = "",
    trust_env: bool = True,
    log: LogFn = print,
    attempts: int = 3,
    timeout: int = 20,
) -> bool:
    """Probe whether openrouter.ai is reachable on the current network route and,
    if a key is given, whether that key is valid. Logs a plain-language verdict
    with a hint for the common failure modes. Returns True only on HTTP 200.

    This exists because openrouter.ai is region-blocked for the user and only
    reachable through a VPN/proxy that is often flaky — so the usual question is
    "is the network path working right now?", which this answers directly."""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    exe = _curl_exe()
    have_key = bool(api_key)
    url = OPENROUTER_KEY_URL if have_key else OPENROUTER_MODELS_URL
    headers = {"Authorization": f"Bearer {api_key}"} if have_key else {}

    route = (f"explicit proxy {proxy}" if proxy
             else ("system/VPN proxy" if trust_env else "direct (no proxy)"))
    log(f"-> Testing OpenRouter via {route} ({'curl' if exe else 'requests'})...")

    last_err: Optional[Exception] = None
    for i in range(1, attempts + 1):
        try:
            if exe:
                resp = _curl_request(exe, "GET", url, headers=headers, proxy=proxy,
                                     trust_env=trust_env, timeout=timeout)
            else:
                session = requests.Session()
                session.trust_env = trust_env
                resp = session.get(url, headers=headers, timeout=timeout, proxies=proxies)
        except (requests.RequestException, RuntimeError) as e:
            last_err = e
            log(f"   attempt {i}/{attempts}: connection failed")
            continue
        # A real HTTP response came back — the path to OpenRouter works.
        if resp.status_code == 200:
            if have_key:
                log("[OK] OpenRouter is reachable and your API key works — "
                    "Shorts analysis should run.")
            else:
                log("[OK] OpenRouter is reachable. Enter your API key to validate it.")
            return True
        if resp.status_code == 401:
            log("[!] Reached OpenRouter, but the API key is invalid or expired. "
                "Check the OpenRouter Key field.")
            return False
        if resp.status_code == 403:
            log("[!] A security filter denied the request (HTTP 403). This VPN exit/proxy "
                "IP is blocked — switch to a different VPN server.")
            return False
        log(f"[!] OpenRouter returned HTTP {resp.status_code}: {resp.text[:160]}")
        return False

    # Every attempt raised a connection-level error (never got an HTTP reply).
    msg = str(last_err)
    if any(s in msg for s in ("UNEXPECTED_EOF", "SSL", "EOF", "ended prematurely")):
        log("[!] Connection was cut before reaching OpenRouter (TLS reset). openrouter.ai "
            "isn't being tunneled — set your VPN to global/TUN mode (or add openrouter.ai "
            "to its proxy rules), or point the Proxy field at your VPN's local proxy.")
    elif "timed out" in msg.lower() or "timeout" in msg.lower():
        log("[!] Timed out reaching OpenRouter — the proxy/VPN is too slow or not routing it.")
    else:
        log(f"[!] Could not reach OpenRouter after {attempts} attempts: {msg[:160]}")
    log("    Tip: open https://openrouter.ai/api/v1/models in your browser on this VPN — "
        "if it won't load there, the app can't reach it either.")
    return False


# ---------------------------------------------------------------------------
# Rendering (ffmpeg): vertical 9:16 + burned captions
# ---------------------------------------------------------------------------
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(title: str, max_len: int = 60) -> str:
    name = re.sub(r"\s+", " ", title or "").strip()
    name = INVALID_FILENAME_CHARS.sub("", name).strip(" .")
    return (name[:max_len].rsplit(" ", 1)[0].strip() if len(name) > max_len else name) or "clip"


# ---------------------------------------------------------------------------
# Caption rendering (rounded white pill, black text, Persian-aware)
# ---------------------------------------------------------------------------
# Burned captions are drawn as PNG overlays (not libass) so we get a true
# rounded-rectangle background. Persian/Arabic text needs letter-joining
# (arabic_reshaper) and right-to-left reordering (python-bidi) before PIL can
# draw it, since PIL has no complex-text shaper of its own.
CAPTION_BOTTOM_MARGIN = 200   # px above the bottom edge of the 1080x1920 frame
CAPTION_FONT_SIZE = 54
CAPTION_MAX_TEXT_WIDTH = 900   # px; text wraps beyond this
_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\Vazirmatn-VariableFont_wght.ttf",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts\Vazirmatn-VariableFont_wght.ttf"),
]


def _caption_font_path() -> Optional[str]:
    for p in _FONT_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


def _shape_rtl(text: str) -> str:
    """Reshape + bidi-reorder so Persian/Arabic renders correctly in PIL.
    A no-op (returns text unchanged) for Latin text or if the libs are absent."""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def _load_caption_font(size: int):
    from PIL import ImageFont
    path = _caption_font_path()
    if not path:
        return ImageFont.load_default()
    font = ImageFont.truetype(path, size)
    try:
        font.set_variation_by_axes([600])  # SemiBold weight of the variable font
    except Exception:
        pass
    return font


def _wrap_display_lines(draw, text: str, font, max_width: int) -> List[str]:
    """Wrap logical text into lines that fit ``max_width`` once shaped, then
    return the shaped (render-ready) lines."""
    words = text.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        if not cur or draw.textlength(_shape_rtl(trial), font=font) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return [_shape_rtl(ln) for ln in (lines or [text])]


def render_caption_png(
    text: str,
    out_png: Path,
    font_size: int = CAPTION_FONT_SIZE,
    max_text_width: int = CAPTION_MAX_TEXT_WIDTH,
    pad_x: int = 44,
    pad_y: int = 26,
    line_gap: int = 12,
) -> Optional[Tuple[int, int]]:
    """Render ``text`` as a rounded white pill with black text to ``out_png``.
    Returns (width, height) in px, or None if nothing was drawn."""
    from PIL import Image, ImageDraw
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return None
    font = _load_caption_font(font_size)
    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    lines = _wrap_display_lines(probe, text, font, max_text_width)
    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    widths = [probe.textlength(ln, font=font) for ln in lines]
    text_w = int(max(widths)) if widths else 0
    text_h = line_h * len(lines) + line_gap * (len(lines) - 1)
    W = text_w + 2 * pad_x
    H = text_h + 2 * pad_y
    radius = min(H // 2, 52)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, W - 1, H - 1], radius=radius, fill=(255, 255, 255, 255))
    y = pad_y
    for ln, w in zip(lines, widths):
        draw.text(((W - w) / 2, y), ln, font=font, fill=(0, 0, 0, 255))
        y += line_h + line_gap
    img.save(out_png)
    return W, H


def _clip_cues(segments: List[Segment], start: float, end: float) -> List[Tuple[float, float, str]]:
    """Clip-relative (start, end, text) cues overlapping [start, end]."""
    rows: List[Tuple[float, float, str]] = []
    for s, e, t in segments:
        if e <= start or s >= end:
            continue
        cs = max(0.0, s - start)
        ce = min(end, e) - start
        t = (t or "").strip()
        if ce > cs and t:
            rows.append((cs, ce, t))
    return rows


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


def render_cut(
    ffmpeg: str,
    src_video: str,
    start: float,
    end: float,
    out_path: Path,
    log: LogFn = print,
    register: Optional[Callable[[subprocess.Popen], None]] = None,
    unregister: Optional[Callable[[subprocess.Popen], None]] = None,
) -> bool:
    """Cut [start, end] from ``src_video`` losslessly (stream copy, no re-encode,
    original aspect ratio). Fast and cheap; the cut snaps to the nearest keyframe
    at/just before ``start``. Returns True on success."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dur = max(0.1, end - start)
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(src_video), "-t", f"{dur:.3f}",
        "-c", "copy", "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart", str(out_path),
    ]
    log(f"-> Cutting clip (lossless): {out_path.name}  ({start:.1f}s–{end:.1f}s)")
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, creationflags=_creationflags(),
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
    if proc.returncode != 0:
        log(f"[!] ffmpeg failed for {out_path.name}:")
        for ln in tail[-5:]:
            log(f"    {ln}")
        return False
    return out_path.exists()


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

    # The vertical composition: blurred cover background + centered foreground.
    base_vf = (
        "[0:v]split=2[bg][fg];"
        "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=24:2[bgb];"
        "[fg]scale=1080:-2:force_original_aspect_ratio=decrease[fgs];"
        "[bgb][fgs]overlay=(W-w)/2:(H-h)/2[v0]"
    )

    # Captions: render each cue as a rounded white pill (black Vazirmatn text,
    # Persian-shaped) and overlay it low in the frame. Files are referenced by
    # basename with cwd = output dir to dodge ffmpeg path-escaping on Windows.
    # If image rendering ever fails, fall back to a plain libass subtitle box.
    cap_pngs: List[Tuple[Path, float, float]] = []
    cleanup: List[Path] = []
    clip_srt = out_path.parent / (out_path.stem + ".__cap.srt")
    srt_fallback = False
    if burn_captions and segments:
        cues = _clip_cues(segments, start, end)
        try:
            for idx, (cs, ce, txt) in enumerate(cues):
                png = out_path.parent / f"{out_path.stem}.__cap{idx:03d}.png"
                if render_caption_png(txt, png):
                    cap_pngs.append((png, cs, ce))
                    cleanup.append(png)
        except Exception as e:
            log(f"[!] Caption image render failed ({e}); using plain subtitles instead.")
            for p in cleanup:
                try:
                    p.unlink()
                except OSError:
                    pass
            cap_pngs, cleanup = [], []
            srt_fallback = write_clip_srt(segments, start, end, clip_srt)

    inputs: List[str] = ["-ss", f"{start:.3f}", "-i", str(src_video)]
    for png, _, _ in cap_pngs:
        inputs += ["-loop", "1", "-i", png.name]

    if cap_pngs:
        parts = [base_vf]
        prev = "v0"
        for i, (_png, cs, ce) in enumerate(cap_pngs, start=1):
            lbl = f"c{i}"
            parts.append(
                f"[{prev}][{i}:v]overlay=x=(W-w)/2:y=H-h-{CAPTION_BOTTOM_MARGIN}:"
                f"enable='between(t,{cs:.3f},{ce:.3f})'[{lbl}]")
            prev = lbl
        vf, final = ";".join(parts), prev
    elif srt_fallback:
        style = ("FontName=Vazirmatn,Fontsize=16,PrimaryColour=&H00000000,"
                 "BackColour=&H00FFFFFF,BorderStyle=3,Outline=6,Shadow=0,"
                 "Alignment=2,MarginV=140")
        vf = base_vf + f";[v0]subtitles={clip_srt.name}:force_style='{style}'[vout]"
        final = "vout"
    else:
        vf, final = base_vf, "v0"

    have_caps = bool(cap_pngs) or srt_fallback
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        *inputs, "-t", f"{dur:.3f}",
        "-filter_complex", vf, "-map", f"[{final}]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        out_path.name,
    ]

    log(f"-> Rendering short: {out_path.name}  ({start:.1f}s–{end:.1f}s)"
        + (f"  +captions ({len(cap_pngs)} cue(s))" if cap_pngs else
           "  +captions" if have_caps else ""))
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
        for p in [*cleanup, clip_srt]:
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass

    if proc.returncode != 0:
        log(f"[!] ffmpeg failed for {out_path.name}:")
        for ln in tail[-5:]:
            log(f"    {ln}")
        return False
    return out_path.exists()
