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
    ) -> Optional[str]:
        """Transcribe ``audio_source`` and return the full text transcript."""
        if is_cancelled():
            return None
        model = self._ensure_model()

        if is_cancelled():
            return None
        self.log("-> Transcribing audio with Whisper AI (Full Transcript)...")
        
        # stable-whisper supports progress_callback
        result = model.transcribe(
            audio_source,
            progress_callback=progress_callback
        )

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
    ) -> bool:
        """Align ``subs`` to ``audio_source`` and write an SRT file.

        Lines whose words extend past the audio (e.g. dub ends before the
        original) are fanned out into sequential ``unaligned_interval``-second
        intervals so they remain visible.
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
        text_to_align = "\n".join(valid_subs)

        if is_cancelled():
            return False
        self.log(f"-> Syncing {len(valid_subs)} lines...")
        result = model.align(
            audio_source, text_to_align, detected_lang,
            progress_callback=progress_callback
        )

        all_words: List = []
        for s in result.segments:
            all_words.extend(getattr(s, "words", []))

        line_word_mapping = self._map_words_to_lines(valid_subs, all_words)

        with srt_path.open("w", encoding="utf-8") as f:
            current_unaligned: Optional[float] = None

            for i, (line_text, words_in_line) in enumerate(zip(valid_subs, line_word_mapping), 1):
                if not words_in_line:
                    continue

                is_unaligned = all(
                    (w.start == w.end or w.start >= audio_duration - 0.5)
                    for w in words_in_line
                )
                if current_unaligned is not None:
                    is_unaligned = True

                if is_unaligned:
                    if current_unaligned is None:
                        last_end = 0.0
                        for prev_words in line_word_mapping[: i - 1]:
                            for pw in prev_words:
                                if pw.start != pw.end and pw.end < audio_duration - 0.5:
                                    last_end = max(last_end, pw.end)
                        current_unaligned = last_end if last_end > 0 else audio_duration

                    start_time = current_unaligned
                    end_time = start_time + unaligned_interval
                    current_unaligned = end_time
                else:
                    start_time = words_in_line[0].start
                    end_time = words_in_line[-1].end
                    if start_time >= end_time:
                        end_time = start_time + unaligned_interval

                f.write(
                    f"{i}\n{format_time(start_time)} --> {format_time(end_time)}\n{line_text}\n\n"
                )

        self.log("-> Whisper sync successful! SRT saved.")
        return True

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

    def __init__(self, log: LogFn, api_key: str, proxy: str = "") -> None:
        self.log = log
        self.api_key = api_key
        self.proxy = proxy
        self.url = "https://api.groq.com/openai/v1/audio/transcriptions"

    def transcribe_to_text(
        self,
        audio_path: str,
        is_cancelled: CancelFn = lambda: False,
        progress_callback: Optional[ProgressFn] = None,
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
                headers = {"Authorization": f"Bearer {self.api_key}"}
                proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
                response = requests.post(
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
