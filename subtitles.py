"""SRT generation and Whisper-based subtitle alignment."""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import requests
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

BASE_PATH = Path(__file__).resolve().parent


def _ffmpeg_exe() -> str:
    """Return the bundled ffmpeg binary if present, else fall back to PATH."""
    local = BASE_PATH / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if local.exists():
        return str(local)
    return shutil.which("ffmpeg") or "ffmpeg"


def _safe_remove(path: Optional[str]) -> None:
    """Delete a temp file if it exists, ignoring errors."""
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


class _Resp:
    """Minimal requests.Response stand-in for curl output (status + text)."""
    __slots__ = ("status_code", "text")

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


def _curl_exe() -> Optional[str]:
    """Locate curl. Bundled with Windows 10/11. Used to reach api.groq.com, which
    sits behind Cloudflare: Cloudflare resets Python requests/urllib3's TLS
    handshake (its ClientHello reads as a bot) with 'SSL:
    UNEXPECTED_EOF_WHILE_READING'. OS-native curl (Schannel on Windows) uses a
    browser-like fingerprint Cloudflare accepts."""
    exe = shutil.which("curl")
    if exe:
        return exe
    if os.name == "nt":
        sys32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                             "System32", "curl.exe")
        if os.path.isfile(sys32):
            return sys32
    return None

LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]
# ProgressCallback: (current_seconds, total_seconds)
ProgressFn = Callable[[float, float], None]

UNALIGNED_INTERVAL = 2.0
WHISPER_SAMPLE_RATE = 16000


def format_time(seconds: float) -> str:
    """Convert seconds to SRT timestamp format ``HH:MM:SS,mmm``."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def read_srt_cues(path: Path) -> List[str]:
    """Parse an existing .srt file back into its cue text lines.

    Each returned entry is one cue's text (multi-line cues are joined with a
    space), preserving order so the result can be fed straight back into
    :meth:`GroqTranscriber.align` to re-time it against an audio track.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    cues: List[str] = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        text_lines: List[str] = []
        for i, line in enumerate(block.splitlines()):
            s = line.strip()
            if not s:
                continue
            if "-->" in s:          # timestamp line
                continue
            if i == 0 and s.isdigit():  # leading cue index
                continue
            text_lines.append(s)
        if text_lines:
            cues.append(" ".join(text_lines))
    return cues


def generate_standard_srt(
    subs: Sequence[str],
    srt_path: Path,
    log: LogFn,
    interval: float = UNALIGNED_INTERVAL,
) -> bool:
    """Write a basic SRT file with fixed-interval timestamps."""
    try:
        with srt_path.open("w", encoding="utf-8") as f:
            current = 0.0
            for j, sub_line in enumerate(subs, 1):
                start = format_time(current)
                current += interval
                end = format_time(current)
                f.write(f"{j}\n{start} --> {end}\n{sub_line}\n\n")
        log(f"-> Created standard SRT file with {len(subs)} lines ({interval:g}s intervals).")
        return True
    except OSError as e:
        log(f"-> Error creating SRT: {e}")
        return False


# ----------------------------------------------------------------------
# Styled caption export (.ttml)
# ----------------------------------------------------------------------

# Reference frame the style offsets are authored against. Premiere scales the
# region to the actual sequence size on import, so the numbers stay meaningful
# regardless of the final timeline resolution.
TTML_REFERENCE_RESOLUTION: Tuple[int, int] = (1920, 1080)


@dataclass
class CaptionStyle:
    """Visual style for exported .ttml captions, persisted in config.json.

    Offsets are measured in reference-frame pixels from the *centre* of the
    frame: ``x_offset`` positive moves right, ``y_offset`` positive moves down
    (so a positive ``y_offset`` lands in the familiar lower-third zone).
    """

    font_family: str = "Arial"
    font_size: int = 36          # px in the reference frame
    x_offset: int = 0            # px from horizontal centre (+ = right)
    y_offset: int = 360          # px from vertical centre (+ = down)
    text_color: str = "#FFFFFF"
    bg_color: str = "#000000"
    bg_opacity: int = 70         # percent (0 = transparent, 100 = opaque)
    padding: int = 12            # px of background around the text
    corner_radius: int = 8       # px; honoured by the overlay export, not TTML

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "CaptionStyle":
        if not isinstance(data, dict):
            return cls()
        valid = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def bg_color_rgba(self) -> str:
        """Return the background colour as ``#RRGGBBAA`` using ``bg_opacity``."""
        alpha = round(max(0, min(100, int(self.bg_opacity))) * 255 / 100)
        hexcol = str(self.bg_color).lstrip("#")
        if len(hexcol) == 3:
            hexcol = "".join(c * 2 for c in hexcol)
        if len(hexcol) != 6:
            hexcol = "000000"
        return f"#{hexcol.upper()}{alpha:02X}"


_SRT_TIME_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")


def _srt_time_to_seconds(text: str) -> float:
    m = _SRT_TIME_RE.search(text)
    if not m:
        return 0.0
    h, mm, ss, ms = m.groups()
    return int(h) * 3600 + int(mm) * 60 + int(ss) + int(ms.ljust(3, "0")) / 1000.0


def parse_srt_timed(path: Path) -> List[Tuple[float, float, List[str]]]:
    """Parse an .srt into ``(start, end, [text_line, ...])`` tuples.

    Unlike :func:`read_srt_cues`, this keeps the cue timing so the captions can
    be re-emitted in another format without re-running alignment.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    cues: List[Tuple[float, float, List[str]]] = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = block.splitlines()
        time_idx = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if time_idx is None:
            continue
        start_part, _, end_part = lines[time_idx].partition("-->")
        start = _srt_time_to_seconds(start_part)
        end = _srt_time_to_seconds(end_part)
        text_lines = [ln.strip() for ln in lines[time_idx + 1:] if ln.strip()]
        if text_lines:
            cues.append((start, end, text_lines))
    return cues


def _ttml_time(seconds: float) -> str:
    """Format seconds as a TTML media clock time ``HH:MM:SS.mmm``."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    hours = total_s // 3600
    minutes = (total_s % 3600) // 60
    secs = total_s % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def write_ttml(
    srt_path: Path,
    style: CaptionStyle,
    log: LogFn,
    ttml_path: Optional[Path] = None,
    resolution: Tuple[int, int] = TTML_REFERENCE_RESOLUTION,
) -> bool:
    """Convert a finished .srt into a styled W3C TTML caption file.

    The text sits in a positioned region (origin derived from the style's
    centre-relative offsets) with a text-hugging background span, giving an
    SRT-like overlay that Premiere imports with its position, font, colours and
    background intact.
    """
    srt_path = Path(srt_path)
    if ttml_path is None:
        ttml_path = srt_path.with_suffix(".ttml")
    ttml_path = Path(ttml_path)

    cues = parse_srt_timed(srt_path)
    if not cues:
        log(f"-> No cues to export to TTML from {srt_path.name}.")
        return False

    width, height = resolution
    region_w = int(width * 0.8)
    region_h = int(height * 0.25)
    centre_x = width / 2 + style.x_offset
    centre_y = height / 2 + style.y_offset
    origin_x = int(round(centre_x - region_w / 2))
    origin_y = int(round(centre_y - region_h / 2))
    origin_x = max(0, min(width - region_w, origin_x))
    origin_y = max(0, min(height - region_h, origin_y))

    font_family = _xml_escape(str(style.font_family) or "Arial")
    bg_rgba = style.bg_color_rgba()

    header = f"""<?xml version="1.0" encoding="UTF-8"?>
<!-- Generated by Tovo Video Downloader. corner-radius: {int(style.corner_radius)}px
     is honoured by the transparent-overlay export, not by TTML captions. -->
<tt xmlns="http://www.w3.org/ns/ttml"
    xmlns:tts="http://www.w3.org/ns/ttml#styling"
    xmlns:ttp="http://www.w3.org/ns/ttml#parameter"
    ttp:timeBase="media"
    xml:lang="en"
    tts:extent="{width}px {height}px">
  <head>
    <styling>
      <style xml:id="capStyle"
             tts:fontFamily="{font_family}"
             tts:fontSize="{int(style.font_size)}px"
             tts:color="{style.text_color}"
             tts:backgroundColor="{bg_rgba}"
             tts:padding="{int(style.padding)}px"
             tts:textAlign="center"/>
    </styling>
    <layout>
      <region xml:id="capRegion"
              tts:origin="{origin_x}px {origin_y}px"
              tts:extent="{region_w}px {region_h}px"
              tts:displayAlign="center"
              tts:textAlign="center"/>
    </layout>
  </head>
  <body>
    <div>
"""

    lines = [header]
    for start, end, text_lines in cues:
        content = "<br/>".join(_xml_escape(ln) for ln in text_lines)
        lines.append(
            f'      <p begin="{_ttml_time(start)}" end="{_ttml_time(end)}"'
            f' style="capStyle" region="capRegion">{content}</p>\n'
        )
    lines.append("    </div>\n  </body>\n</tt>\n")

    try:
        ttml_path.write_text("".join(lines), encoding="utf-8")
    except OSError as e:
        log(f"-> Error creating TTML: {e}")
        return False

    log(f"-> Created styled TTML: {ttml_path.name} ({len(cues)} cues).")
    return True


# ----------------------------------------------------------------------
# Timestamp / cue helpers (transcription-engine agnostic)
#
# These operate on plain word/segment objects exposing ``.word``/``.text`` plus
# ``.start``/``.end``, so the same alignment math works whether the timestamps
# came from a local model or Groq's verbose_json response.
# ----------------------------------------------------------------------
class _Word:
    """Normalised word timestamp (word text + start/end seconds)."""
    __slots__ = ("word", "start", "end")

    def __init__(self, word: str, start: float, end: float) -> None:
        self.word, self.start, self.end = word, start, end


class _Seg:
    """Normalised segment timestamp (text + start/end seconds)."""
    __slots__ = ("text", "start", "end")

    def __init__(self, text: str, start: float, end: float) -> None:
        self.text, self.start, self.end = text, start, end


def _get(obj, key, default=None):
    """Read ``key`` from either a mapping or an attribute-bearing object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_words(resp) -> List[_Word]:
    """Normalise a Groq verbose_json ``.words`` list into _Word objects."""
    out: List[_Word] = []
    for w in (_get(resp, "words", None) or []):
        s, e = _get(w, "start"), _get(w, "end")
        if s is None or e is None:
            continue
        out.append(_Word(str(_get(w, "word", "")), float(s), float(e)))
    return out


def _as_segments(resp) -> List[_Seg]:
    """Normalise a Groq verbose_json ``.segments`` list into _Seg objects."""
    out: List[_Seg] = []
    for s in (_get(resp, "segments", None) or []):
        st, en = _get(s, "start"), _get(s, "end")
        if st is None or en is None:
            continue
        out.append(_Seg(str(_get(s, "text", "")), float(st), float(en)))
    return out


def _map_words_to_lines(valid_subs: Sequence[str], all_words: List) -> List[List]:
    """Walk the flat word list once, distributing words to lines by char count."""
    mapping: List[List] = []
    word_idx = 0
    for line in valid_subs:
        target_len = len(line.replace(" ", "").replace("\n", ""))
        chars = 0
        words: List = []
        while chars < target_len and word_idx < len(all_words):
            w = all_words[word_idx]
            chars += len(w.word.replace(" ", ""))
            words.append(w)
            word_idx += 1
        mapping.append(words)
    return mapping


def _finalize_cues(
    raw_times: Sequence[Tuple[Optional[float], Optional[float]]],
    audio_duration: float,
    unaligned_interval: float,
    min_duration: float,
    fill_gaps: bool,
) -> List[Tuple[float, float]]:
    """Turn raw per-line times into clean, ordered, non-overlapping cues."""
    n = len(raw_times)
    starts: List[float] = [0.0] * n
    ends: List[float] = [0.0] * n

    # Pass 1: fill in unaligned lines sequentially; clamp to valid ranges.
    last_end = 0.0
    for i, (s, e) in enumerate(raw_times):
        if s is None or e is None:
            s = last_end
            e = s + unaligned_interval
        s = max(0.0, s)
        e = max(e, s + min_duration)
        starts[i], ends[i] = s, e
        last_end = e

    # Pass 2: enforce non-decreasing starts with a minimum spacing so cues
    # never overlap and each remains readable.
    for i in range(1, n):
        if starts[i] < starts[i - 1] + min_duration:
            starts[i] = starts[i - 1] + min_duration
        if ends[i] < starts[i] + min_duration:
            ends[i] = starts[i] + min_duration

    # Pass 3: gap handling.
    if fill_gaps:
        for i in range(n - 1):
            ends[i] = starts[i + 1]          # stay on screen until the next line
        if audio_duration and ends[n - 1] < audio_duration:
            ends[n - 1] = audio_duration     # last line runs to the end
    else:
        for i in range(n - 1):
            if ends[i] > starts[i + 1]:
                ends[i] = starts[i + 1]       # just trim any overlap

    return list(zip(starts, ends))


class GroqTranscriber:
    """Uses Groq's cloud Whisper API for fast transcription."""

    def __init__(self, log: LogFn, api_key: str, proxy: str = "",
                 trust_env: bool = True) -> None:
        self.log = log
        self.api_key = api_key
        self.proxy = proxy
        # When False, ignore system/environment proxies (HTTP(S)_PROXY). A dead
        # local VPN proxy otherwise breaks the TLS handshake with SSL EOF errors.
        self.trust_env = trust_env
        self.url = "https://api.groq.com/openai/v1/audio/transcriptions"

    def _upload(self, temp_audio: str, data: dict):
        """POST the prepared audio to Groq. Prefer OS-native curl because
        api.groq.com sits behind Cloudflare, which resets Python requests' TLS
        handshake ('SSL: UNEXPECTED_EOF_WHILE_READING'). Fall back to requests
        when curl is missing or fails to run. Returns an object exposing
        ``.status_code`` and ``.text`` (both curl and requests responses do)."""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        exe = _curl_exe()
        if exe:
            try:
                return self._upload_via_curl(exe, temp_audio, data, headers)
            except RuntimeError as e:
                self.log(f"-> curl upload failed ({e}); retrying with Python requests...")

        with open(temp_audio, "rb") as f:
            files = {"file": (os.path.basename(temp_audio), f, "audio/mpeg")}
            proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
            session = requests.Session()
            session.trust_env = self.trust_env  # honour "Disable System Proxy"
            return session.post(
                self.url, headers=headers, files=files, data=data,
                timeout=60, proxies=proxies,
            )

    def _upload_via_curl(self, exe: str, temp_audio: str, data: dict,
                         headers: dict) -> _Resp:
        """Multipart file upload to Groq via curl. Returns _Resp for any HTTP
        reply (including 4xx/5xx); raises RuntimeError on a connection-level
        failure so the caller can fall back to requests."""
        timeout = 120
        args = [exe, "-sS", "--max-time", str(timeout), "-w", "\n%{http_code}",
                "-X", "POST", self.url]
        for k, v in headers.items():
            args += ["-H", f"{k}: {v}"]
        args += ["-F", f"file=@{temp_audio};type=audio/mpeg"]
        for k, v in data.items():
            args += ["-F", f"{k}={v}"]
        # Mirror requests' proxy behaviour: explicit proxy wins; otherwise honour
        # env proxies when trust_env, or force a direct route when not.
        if self.proxy:
            args += ["-x", self.proxy]
        elif not self.trust_env:
            args += ["--noproxy", "*"]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=timeout + 15, creationflags=creationflags)
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

    def _sdk_call(self, temp_audio: str, response_format: str,
                  language: Optional[str] = None,
                  granularities: Optional[Sequence[str]] = None):
        """One Groq SDK transcription call over an httpx client that honours our
        proxy/trust_env. Returns the SDK response (a ``str`` for 'text', or an
        object exposing ``.text``/``.segments``/``.words`` for 'verbose_json').
        Returns ``None`` only when the ``groq`` SDK isn't importable; a real
        API/transport error propagates to the caller.

        The SDK's httpx/HTTP-2 handshake is accepted by Cloudflare in regions
        where our ``requests``/``curl`` fingerprint is reset."""
        try:
            from groq import Groq
            import httpx
        except ImportError:
            return None
        with open(temp_audio, "rb") as f:
            payload = f.read()
        # Explicit proxy URL wins; otherwise trust_env lets httpx read the env.
        http_client = httpx.Client(
            proxy=self.proxy or None, trust_env=self.trust_env, timeout=180.0,
        )
        try:
            client = Groq(api_key=self.api_key, http_client=http_client, max_retries=2)
            kwargs = {
                "file": (os.path.basename(temp_audio), payload),
                "model": "whisper-large-v3",
                "temperature": 0,
                "response_format": response_format,
            }
            if language:
                kwargs["language"] = language
            if granularities:
                kwargs["timestamp_granularities"] = list(granularities)
            return client.audio.transcriptions.create(**kwargs)
        finally:
            http_client.close()

    def _transcribe_via_sdk(self, temp_audio: str,
                            language: Optional[str]) -> Optional[str]:
        """Text transcript via the SDK. Returns the text, or ``None`` if the SDK
        isn't importable so the caller can fall back to the curl/requests path."""
        resp = self._sdk_call(temp_audio, "text", language=language)
        if resp is None:
            return None
        if isinstance(resp, str):
            return resp
        return getattr(resp, "text", str(resp))

    def _prepare_audio(self, audio_path: str,
                       is_cancelled: CancelFn) -> Optional[str]:
        """Downmix/compress to a small mono 16k mp3 for a fast, reliable upload.
        Returns the temp path (caller deletes) or ``None`` on cancel/ffmpeg
        failure."""
        if is_cancelled():
            return None
        self.log("-> Preparing audio for Groq AI upload...")
        temp_audio = str(Path(audio_path).with_suffix(".tmp.mp3"))
        cmd = [
            _ffmpeg_exe(), "-y", "-i", audio_path,
            "-vn", "-map_metadata", "-1", "-ac", "1", "-ar", "16000", "-b:a", "32k",
            temp_audio,
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            subprocess.run(cmd, capture_output=True, check=True, creationflags=creationflags)
        except (subprocess.CalledProcessError, OSError) as e:
            self.log(f"[!] ffmpeg failed preparing audio: {e}")
            return None
        return temp_audio

    def align(
        self,
        audio_source: str,
        subs: Sequence[str],
        srt_path: Path,
        is_cancelled: CancelFn = lambda: False,
        unaligned_interval: float = UNALIGNED_INTERVAL,
        progress_callback: Optional[ProgressFn] = None,
        fill_gaps: bool = True,
        min_duration: float = 0.3,
    ) -> bool:
        """Re-time existing subtitle lines against the audio and write an SRT.

        Groq has no forced-alignment mode, so we transcribe the audio with
        word-level timestamps and distribute those words across the given lines
        by character count (the same mapping the local aligner used as a
        fallback). ``fill_gaps`` keeps each line on screen until the next."""
        if not self.api_key:
            self.log("[!] Error: Groq API Key is missing in settings.")
            return False
        valid_subs = [ln for ln in subs if ln.strip()]
        if not valid_subs:
            self.log("-> No subtitle text to sync.")
            return False
        temp = self._prepare_audio(audio_source, is_cancelled)
        if not temp:
            return False
        try:
            if is_cancelled():
                return False
            self.log(f"-> Syncing {len(valid_subs)} lines with Groq (whisper-large-v3)...")
            resp = self._sdk_call(temp, "verbose_json", granularities=["word", "segment"])
            if resp is None:
                self.log("[!] Groq SDK not available for alignment.")
                return False
            words = _as_words(resp)
            if not words:
                self.log("-> Groq returned no word timestamps; cannot sync.")
                return False
            audio_duration = float(_get(resp, "duration", 0.0) or 0.0)
            raw_times: List[Tuple[Optional[float], Optional[float]]] = []
            for wl in _map_words_to_lines(valid_subs, words):
                aligned = [w for w in wl if w.end > w.start]
                if aligned:
                    raw_times.append((min(w.start for w in aligned),
                                      max(w.end for w in aligned)))
                else:
                    raw_times.append((None, None))
            cues = _finalize_cues(raw_times, audio_duration, unaligned_interval,
                                  min_duration, fill_gaps)
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, ((s, e), line) in enumerate(zip(cues, valid_subs), 1):
                    f.write(f"{i}\n{format_time(s)} --> {format_time(e)}\n{line}\n\n")
            self.log("-> Groq sync successful! SRT saved.")
            return True
        except Exception as e:
            self.log(f"[!] Groq alignment failed: {e}")
            logger.exception("Groq align failed for %s", audio_source)
            return False
        finally:
            _safe_remove(temp)

    def transcribe_to_srt(
        self,
        audio_source: str,
        srt_path: Path,
        is_cancelled: CancelFn = lambda: False,
        language: Optional[str] = None,
        fill_gaps: bool = True,
        min_duration: float = 0.3,
        progress_callback: Optional[ProgressFn] = None,
    ) -> bool:
        """Transcribe from scratch and write a timed SRT (captions), using Groq
        segment timestamps. ``fill_gaps`` keeps each caption on screen until the
        next one starts."""
        if not self.api_key:
            self.log("[!] Error: Groq API Key is missing in settings.")
            return False
        temp = self._prepare_audio(audio_source, is_cancelled)
        if not temp:
            return False
        try:
            if is_cancelled():
                return False
            self.log("-> Transcribing audio for captions (Groq)...")
            resp = self._sdk_call(temp, "verbose_json", language=language,
                                  granularities=["segment"])
            if resp is None:
                self.log("[!] Groq SDK not available for captions.")
                return False
            segs = [s for s in _as_segments(resp) if s.text.strip() and s.end > s.start]
            if not segs:
                self.log("-> No speech detected; no captions written.")
                return False
            audio_duration = float(_get(resp, "duration", 0.0) or 0.0)
            raw_times = [(s.start, s.end) for s in segs]
            texts = [s.text.strip() for s in segs]
            cues = _finalize_cues(raw_times, audio_duration, UNALIGNED_INTERVAL,
                                  min_duration, fill_gaps)
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, ((s, e), text) in enumerate(zip(cues, texts), 1):
                    f.write(f"{i}\n{format_time(s)} --> {format_time(e)}\n{text}\n\n")
            self.log(f"-> Captions written: {Path(srt_path).name}")
            return True
        except Exception as e:
            self.log(f"[!] Groq captioning failed: {e}")
            logger.exception("Groq transcribe_to_srt failed for %s", audio_source)
            return False
        finally:
            _safe_remove(temp)

    def transcribe_segments(
        self,
        audio_source: str,
        is_cancelled: CancelFn = lambda: False,
        language: Optional[str] = None,
        progress_callback: Optional[ProgressFn] = None,
    ) -> List[Tuple[float, float, str]]:
        """Return ``(start, end, text)`` per segment — the timed transcript used
        to pick highlight clips and burn per-clip captions."""
        if not self.api_key:
            self.log("[!] Error: Groq API Key is missing in settings.")
            return []
        temp = self._prepare_audio(audio_source, is_cancelled)
        if not temp:
            return []
        try:
            if is_cancelled():
                return []
            self.log("-> Transcribing audio (timed transcript, Groq)...")
            resp = self._sdk_call(temp, "verbose_json", language=language,
                                  granularities=["segment"])
            if resp is None:
                self.log("[!] Groq SDK not available.")
                return []
            return [(s.start, s.end, s.text.strip()) for s in _as_segments(resp)
                    if s.text.strip() and s.end > s.start]
        except Exception as e:
            self.log(f"[!] Groq transcript failed: {e}")
            logger.exception("Groq transcribe_segments failed for %s", audio_source)
            return []
        finally:
            _safe_remove(temp)

    def transcribe_to_text(
        self,
        audio_path: str,
        is_cancelled: CancelFn = lambda: False,
        progress_callback: Optional[ProgressFn] = None,
        language: Optional[str] = None,
    ) -> Optional[str]:
        if not self.api_key:
            self.log("[!] Error: Groq API Key is missing in settings.")
            return None

        temp_audio = self._prepare_audio(audio_path, is_cancelled)
        if not temp_audio:
            return None
        try:
            if is_cancelled():
                return None
            self.log("-> Uploading to Groq AI (Whisper-large-v3)...")

            if progress_callback:
                progress_callback(50, 100) # Mock 50% for upload started

            # Prefer the official groq SDK (httpx / HTTP-2 transport). Cloudflare
            # accepts its TLS fingerprint, where our hand-rolled requests/curl
            # path gets reset ('SSL: UNEXPECTED_EOF' / schannel handshake). The
            # SDK returns None only when it isn't importable, so we can fall back
            # to the curl/requests uploader in that one case; a real API error
            # from the SDK propagates and is handled by the outer except.
            text = self._transcribe_via_sdk(temp_audio, language)
            if text is None:
                data = {"model": "whisper-large-v3", "response_format": "text"}
                if language:
                    data["language"] = language
                response = self._upload(temp_audio, data)
                if response.status_code != 200:
                    self.log(f"[!] Groq API Error: {response.status_code} - {response.text}")
                    logger.error("Groq API error %s: %s", response.status_code, response.text)
                    return None
                text = response.text

            if progress_callback:
                progress_callback(100, 100) # Mock 100% for completed

            self.log("-> Groq transcription successful!")
            return text
        except Exception as e:
            self.log(f"[!] Error during Groq transcription: {e}")
            logger.exception("Groq transcription failed for %s", audio_path)
            return None
        finally:
            _safe_remove(temp_audio)
