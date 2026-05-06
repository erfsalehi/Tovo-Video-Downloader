"""SRT generation and Whisper-based subtitle alignment."""
from __future__ import annotations

import json
import logging
import os
import requests
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Sequence

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]
CancelFn = Callable[[], bool]

UNALIGNED_INTERVAL = 2.0
WHISPER_SAMPLE_RATE = 16000


def format_time(seconds: float) -> str:
    """Convert seconds to SRT timestamp format ``HH:MM:SS,mmm``."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


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
    ) -> Optional[str]:
        """Transcribe ``audio_source`` and return the full text transcript."""
        if is_cancelled():
            return None
        model = self._ensure_model()

        if is_cancelled():
            return None
        self.log("-> Transcribing audio with Whisper AI (Full Transcript)...")
        
        result = model.transcribe(audio_source)

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
        result = model.align(audio_source, text_to_align, detected_lang)

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

    def __init__(self, log: LogFn, api_key: str) -> None:
        self.log = log
        self.api_key = api_key
        self.url = "https://api.groq.com/openai/v1/audio/transcriptions"

    def transcribe_to_text(
        self,
        audio_path: str,
        is_cancelled: CancelFn = lambda: False,
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
                "ffmpeg", "-y", "-i", audio_path,
                "-vn", "-map_metadata", "-1", "-ac", "1", "-ar", "16000", "-b:a", "32k",
                temp_audio
            ]
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.run(cmd, capture_output=True, check=True, creationflags=creationflags)

            if is_cancelled():
                return None
            self.log("-> Uploading to Groq AI (Whisper-large-v3)...")

            with open(temp_audio, "rb") as f:
                files = {"file": (os.path.basename(temp_audio), f, "audio/mpeg")}
                data = {
                    "model": "whisper-large-v3",
                    "response_format": "text"
                }
                headers = {"Authorization": f"Bearer {self.api_key}"}
                response = requests.post(self.url, headers=headers, files=files, data=data, timeout=60)

            if response.status_code != 200:
                self.log(f"[!] Groq API Error: {response.status_code} - {response.text}")
                return None

            self.log("-> Groq transcription successful!")
            return response.text
        except Exception as e:
            self.log(f"[!] Error during Groq transcription: {e}")
            return None
        finally:
            if os.path.exists(temp_audio):
                try:
                    os.remove(temp_audio)
                except OSError:
                    pass
