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
    :meth:`WhisperAligner.align` to re-time it against an audio track.
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


class WhisperAligner:
    """Lazily loads Whisper once, then reuses the model across batch items."""

    def __init__(self, log: LogFn, model_name: str = "base") -> None:
        self.log = log
        self.model_name = model_name
        self._stable_whisper = None
        self._whisper = None
        self._model = None

    @classmethod
    def try_create(cls, log: LogFn, model_name: str = "base") -> Optional["WhisperAligner"]:
        """Return an aligner if Whisper deps import cleanly, else ``None``."""
        try:
            import warnings
            warnings.filterwarnings("ignore")
            import stable_whisper  # type: ignore
            import whisper  # type: ignore
        except ImportError as e:
            log(f"[!] Whisper or Torch not available ({e.name}). Falling back to standard sync.")
            return None
        instance = cls(log, model_name)
        instance._stable_whisper = stable_whisper
        instance._whisper = whisper
        return instance

    def _ensure_model(self):
        if self._model is None:
            self.log(f"-> Loading Whisper '{self.model_name}' model (one-time)...")
            self._model = self._stable_whisper.load_model(self.model_name)
        return self._model

    def transcribe_to_text(
        self,
        audio_source: str,
        is_cancelled: CancelFn = lambda: False,
        progress_callback: Optional[ProgressFn] = None,
        language: Optional[str] = None,
    ) -> Optional[str]:
        """Transcribe ``audio_source`` and return the full text transcript.

        Pass ``language`` (e.g. ``"en"``) to force a language instead of letting
        Whisper auto-detect it."""
        if is_cancelled():
            return None
        model = self._ensure_model()

        if is_cancelled():
            return None
        self.log("-> Transcribing audio with Whisper AI (Full Transcript)...")

        # stable-whisper supports progress_callback
        kwargs = {"progress_callback": progress_callback}
        if language:
            kwargs["language"] = language
        result = model.transcribe(audio_source, **kwargs)

        if is_cancelled():
            return None

        # Concatenate segments into a clean paragraph
        text = " ".join([s.text.strip() for s in result.segments])
        self.log(f"-> Whisper transcription successful!")
        return text

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
        """Align ``subs`` to ``audio_source`` and write an SRT file.

        Each input line is aligned to the audio as its own cue (via stable-whisper
        ``original_split``), giving accurate per-line start/end times. Lines whose
        speech extends past the audio (e.g. the dub ends before the original) are
        fanned out into sequential ``unaligned_interval``-second cues so they stay
        visible.

        When ``fill_gaps`` is True, each cue is extended to end exactly where the
        next one starts, so a line stays on screen through any pause/silence until
        the following line begins (the trailing silence belongs to the previous
        line). The final cue runs to the end of the audio.
        """
        if is_cancelled():
            return False
        model = self._ensure_model()

        if is_cancelled():
            return False
        self.log("-> Analyzing audio for language detection...")
        audio = self._whisper.load_audio(audio_source)
        audio_duration = audio.shape[0] / float(WHISPER_SAMPLE_RATE)

        trimmed = self._whisper.pad_or_trim(audio)
        mel = self._whisper.log_mel_spectrogram(trimmed).to(model.device)
        _, probs = model.detect_language(mel)
        detected_lang = max(probs, key=probs.get)
        self.log(f"-> Detected language: '{detected_lang}'")

        valid_subs = [line for line in subs if line.strip()]
        if not valid_subs:
            self.log("-> No subtitle text to sync.")
            return False
        text_to_align = "\n".join(valid_subs)

        if is_cancelled():
            return False
        self.log(f"-> Syncing {len(valid_subs)} lines...")

        raw_times = self._aligned_line_times(
            model, audio_source, valid_subs, text_to_align, detected_lang,
            audio_duration, progress_callback,
        )
        if raw_times is None:
            return False

        cues = self._finalize_cues(
            raw_times, audio_duration, unaligned_interval, min_duration, fill_gaps,
        )

        with srt_path.open("w", encoding="utf-8") as f:
            for i, ((start_time, end_time), line_text) in enumerate(
                zip(cues, valid_subs), 1
            ):
                f.write(
                    f"{i}\n{format_time(start_time)} --> {format_time(end_time)}\n{line_text}\n\n"
                )

        self.log("-> Whisper sync successful! SRT saved.")
        return True

    def _aligned_line_times(
        self, model, audio_source: str, valid_subs: Sequence[str],
        text_to_align: str, detected_lang: str, audio_duration: float,
        progress_callback: Optional[ProgressFn],
    ) -> Optional[List[Tuple[Optional[float], Optional[float]]]]:
        """Return a (start, end) per line; ``(None, None)`` marks an unaligned line.

        Prefers ``original_split`` (one segment per input line); if that yields a
        different segment count, falls back to the legacy char-count word mapping.
        """
        align_kwargs = {"original_split": True}
        if progress_callback is not None:
            align_kwargs["progress_callback"] = progress_callback
        try:
            result = model.align(audio_source, text_to_align, detected_lang, **align_kwargs)
        except TypeError:
            # Older stable-whisper without original_split/progress_callback kwargs.
            result = model.align(audio_source, text_to_align, detected_lang)
        except Exception:
            logger.exception("stable-whisper align failed")
            self.log("[!] Whisper alignment failed.")
            return None
        if result is None:
            return None

        segments = list(result.segments)
        if len(segments) == len(valid_subs):
            return [self._segment_times(s, audio_duration) for s in segments]

        # Fallback: flatten words and re-map to lines by character count.
        self.log(
            f"-> Note: alignment produced {len(segments)} segments for "
            f"{len(valid_subs)} lines; using word-level mapping."
        )
        all_words: List = []
        for s in segments:
            all_words.extend(getattr(s, "words", []) or [])
        times: List[Tuple[Optional[float], Optional[float]]] = []
        for words in self._map_words_to_lines(valid_subs, all_words):
            aligned = [
                w for w in words
                if w.start != w.end and w.end < audio_duration - 0.05
            ]
            if aligned:
                times.append((min(w.start for w in aligned), max(w.end for w in aligned)))
            else:
                times.append((None, None))
        return times

    @staticmethod
    def _segment_times(seg, audio_duration: float) -> Tuple[Optional[float], Optional[float]]:
        s = getattr(seg, "start", None)
        e = getattr(seg, "end", None)
        # Treat empty or end-clamped segments (speech ran past the audio) as
        # unaligned so they get fanned out instead of piling up at the very end.
        if s is None or e is None or e <= s or s >= audio_duration - 0.05:
            return (None, None)
        return (float(s), float(e))

    @staticmethod
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

    @staticmethod
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

        if is_cancelled():
            return None
        self.log("-> Preparing audio for Groq AI upload...")

        temp_audio = str(Path(audio_path).with_suffix(".tmp.mp3"))
        try:
            cmd = [
                _ffmpeg_exe(), "-y", "-i", audio_path,
                "-vn", "-map_metadata", "-1", "-ac", "1", "-ar", "16000", "-b:a", "32k",
                temp_audio
            ]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.run(cmd, capture_output=True, check=True, creationflags=creationflags)

            if is_cancelled():
                return None
            self.log("-> Uploading to Groq AI (Whisper-large-v3)...")
            
            if progress_callback:
                progress_callback(50, 100) # Mock 50% for upload started

            with open(temp_audio, "rb") as f:
                files = {"file": (os.path.basename(temp_audio), f, "audio/mpeg")}
                data = {
                    "model": "whisper-large-v3",
                    "response_format": "text"
                }
                if language:
                    data["language"] = language
                headers = {"Authorization": f"Bearer {self.api_key}"}
                proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
                session = requests.Session()
                session.trust_env = self.trust_env  # honour "Disable System Proxy"
                response = session.post(
                    self.url, headers=headers, files=files, data=data,
                    timeout=60, proxies=proxies,
                )

            if response.status_code != 200:
                self.log(f"[!] Groq API Error: {response.status_code} - {response.text}")
                logger.error("Groq API error %s: %s", response.status_code, response.text)
                return None

            if progress_callback:
                progress_callback(100, 100) # Mock 100% for completed

            self.log("-> Groq transcription successful!")
            return response.text
        except Exception as e:
            self.log(f"[!] Error during Groq transcription: {e}")
            logger.exception("Groq transcription failed for %s", audio_path)
            return None
        finally:
            if os.path.exists(temp_audio):
                try:
                    os.remove(temp_audio)
                except OSError:
                    pass
