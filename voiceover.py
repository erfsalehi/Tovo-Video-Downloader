"""Voiceover dubbing pipeline.

Three stages, all driven from the Voiceover tab in :mod:`app`:

1. **Title rename** - each raw clip is named with a placeholder prefix (``upt 5``,
   ``pat 092``). The narrator speaks the real title inside the file, so we
   transcribe it and rename the clip to that title. The *prefix* selects which
   RVC voice to use (``upt``/``uptin`` -> Uptin2, ``pat``/``patrick`` -> Patrick2);
   the *spoken words* become the new filename.
2. **Silence shortening** - every pause longer than ``threshold`` seconds is
   trimmed down to a fixed ``target`` length (e.g. >0.1s -> 0.07s) with ffmpeg.
3. **Voice conversion** - the de-silenced wav is run through Mangio-RVC's batch
   inference (``infer_batch_rvc.py``) using each voice's own settings, then the
   result is saved into the Dub folder.

This module is pure logic (no Tkinter) so it can be tested in isolation.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# A typical install location of the Mangio-RVC interface; only one of several
# candidates that find_rvc_dir() probes. The actual path is auto-detected and/or
# set via the UI ("rvc_dir" in config), so the app is not tied to one machine.
DEFAULT_RVC_DIR = Path(r"C:\Users\erfsa\Desktop\Mangio-RVC-v23.7.0")

# Our corrected batch inference script (the one bundled with Mangio v23.7.0 is
# stale and crashes). Lives alongside this module; run from the Mangio dir.
BATCH_SCRIPT = Path(__file__).resolve().parent / "rvc_batch.py"


def is_rvc_dir(path) -> bool:
    """True if ``path`` looks like a usable Mangio-RVC install (has the bundled
    Python runtime and the inference module our batch runner imports)."""
    if not path:
        return False
    p = Path(path)
    return (p / "runtime" / "python.exe").is_file() and (p / "vc_infer_pipeline.py").is_file()


def find_rvc_dir(configured: str = "") -> Optional[Path]:
    """Locate the Mangio-RVC folder so the app works on any machine.

    Checks the configured path first, then common spots (Desktop, home, next to
    the app), then globs for any ``Mangio-RVC*`` folder in those bases. Returns
    the first valid match, or None if nothing is found."""
    app_dir = Path(__file__).resolve().parent
    home = Path.home()
    bases = [home / "Desktop", home, app_dir, app_dir.parent]

    candidates: List[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.append(DEFAULT_RVC_DIR)
    for base in bases:
        candidates.append(base / "Mangio-RVC-v23.7.0")
    for base in bases:
        try:
            candidates.extend(sorted(base.glob("Mangio-RVC*")))
        except OSError:
            pass

    seen = set()
    for c in candidates:
        key = str(c).lower()
        if key in seen:
            continue
        seen.add(key)
        if is_rvc_dir(c):
            return c
    return None

# Voice routing. Each entry maps a voice id to its trained model (.pth), its
# feature index (.index), and the filename prefixes that select it. Paths are
# relative to the RVC directory. Longer prefixes are matched first so "uptin"
# wins over "upt" (both route to the same voice anyway).
VOICE_MODELS = {
    "uptin": {
        "label": "Uptin",
        "pth": "weights/Uptin2.pth",
        "index": "logs/Uptin2/added_IVF1108_Flat_nprobe_1_Uptin2_v2.index",
        "prefixes": ("uptin", "upt"),
    },
    "pat": {
        "label": "Pat",
        "pth": "weights/Patrick2.pth",
        "index": "logs/Patrick2/added_IVF1495_Flat_nprobe_1_Patrick2_v2.index",
        "prefixes": ("patrick", "pat"),
    },
}

# Source clips we are willing to ingest. RVC itself only consumes .wav, so
# anything else is transcoded during the silence-shortening pass.
AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".wma", ".mp4")

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

LogFn = Callable[[str], None]


@dataclass
class RvcSettings:
    """Per-voice inference parameters. The first four are exposed in the UI; the
    rest keep RVC's standard defaults but are still read from config if present."""

    pitch: int = -2             # f0 transpose, in semitones
    index_rate: float = 0.0     # how strongly the feature index is applied
    f0method: str = "rmvpe"     # pitch extraction (rmvpe is best + bundled here)
    protect: float = 0.33       # protect voiceless consonants
    filter_radius: int = 3
    rms_mix_rate: float = 1.0   # volume envelope: 1 = fully use the output's own
    resample_sr: int = 0        # 0 = keep the model's native sample rate


def _creationflags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


# ---------------------------------------------------------------------------
# Voice routing + titles
# ---------------------------------------------------------------------------
def detect_voice(filename: str) -> Optional[str]:
    """Return the voice id for a raw clip based on its filename prefix, or None
    if it doesn't look like an unprocessed clip (e.g. already renamed to a title)."""
    stem = Path(filename).stem.strip().lower()
    # Build (prefix, voice) sorted longest-first so "uptin" beats "upt".
    candidates: List[Tuple[str, str]] = []
    for voice, meta in VOICE_MODELS.items():
        for pre in meta["prefixes"]:
            candidates.append((pre, voice))
    candidates.sort(key=lambda x: len(x[0]), reverse=True)
    for pre, voice in candidates:
        # prefix at the start, then an optional separator, then a digit:
        # "upt 5", "pat 092", "uptin3", "upt_12" all match.
        if re.match(rf"^{re.escape(pre)}[\s_\-.]*\d", stem):
            return voice
    return None


def title_from_transcript(text: str, max_len: int = 120) -> str:
    """Turn a transcript into a safe, reasonably short filename stem."""
    text = re.sub(r"\s+", " ", text or "").strip()
    text = INVALID_FILENAME_CHARS.sub("", text).strip(" .")
    if len(text) > max_len:
        # Cut on a word boundary so we don't slice a word in half.
        text = text[:max_len].rsplit(" ", 1)[0].strip()
    return text or "untitled"


def english_title_from_transcript(text: str, max_len: int = 120) -> str:
    """Like :func:`title_from_transcript`, but drop any non-ASCII characters first.

    The voiceover clips are Persian with an English title spoken at the start;
    forcing English transcription should already yield Latin text, but this strips
    any Farsi (or other non-ASCII) glyphs that slip through so the filename is
    purely the English words."""
    ascii_text = (text or "").encode("ascii", "ignore").decode("ascii")
    return title_from_transcript(ascii_text, max_len)


def trim_clip(ffmpeg: str, in_path: str, out_wav: str, seconds: float,
              log: LogFn = print) -> bool:
    """Write the first ``seconds`` of ``in_path`` to ``out_wav`` as 16k mono wav,
    used to transcribe just the spoken title without the long body of the clip."""
    out = Path(out_wav)
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-t", f"{seconds:g}",
         "-i", str(in_path), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
         str(out)],
        capture_output=True, text=True, creationflags=_creationflags(),
    )
    if r.returncode != 0:
        log(f"[!] ffmpeg title-trim failed: {(r.stderr or '').strip()[:200]}")
    return r.returncode == 0 and out.exists()


def unique_title(title: str, used: set) -> str:
    """Append ' (2)', ' (3)', ... until the title is unused, then reserve it."""
    candidate = title
    n = 2
    lowered = {u.lower() for u in used}
    while candidate.lower() in lowered:
        candidate = f"{title} ({n})"
        n += 1
    used.add(candidate)
    return candidate


# ---------------------------------------------------------------------------
# Silence shortening
# ---------------------------------------------------------------------------
def _parse_duration(stderr: str) -> Optional[float]:
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", stderr)
    if not m:
        return None
    h, mi, se = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(se)


def _parse_silences(stderr: str) -> List[Tuple[float, Optional[float]]]:
    starts = [float(x) for x in re.findall(r"silence_start:\s*([-\d.]+)", stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([-\d.]+)", stderr)]
    pairs: List[Tuple[float, Optional[float]]] = []
    for i, s in enumerate(starts):
        pairs.append((s, ends[i] if i < len(ends) else None))
    return pairs


def shorten_silences(
    ffmpeg: str,
    in_path: str,
    out_wav: str,
    threshold: float = 0.1,
    target: float = 0.07,
    noise_db: float = -30.0,
    pad: float = 0.0,
    log: LogFn = print,
) -> bool:
    """Write ``in_path`` to ``out_wav`` (PCM wav) with every pause longer than
    ``threshold`` seconds shortened to ``target`` seconds. Returns True on success.

    Detection uses ffmpeg's ``silencedetect``; reconstruction keeps the audio
    either side of each over-long pause (leaving ``target`` worth of silence in
    the middle) and concatenates the kept ranges.

    ``pad`` keeps an extra guard of that many seconds of audio on each side of
    speech before cutting. Because ``silencedetect`` flags a fading word-tail as
    silence the instant it dips below ``noise_db``, a small pad (and/or a lower
    noise floor) prevents the ends of words from being clipped. The resulting gap
    between phrases is ``target + 2*pad``."""
    out_path = Path(out_wav)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    flags = _creationflags()

    probe = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(in_path),
         "-af", f"silencedetect=noise={noise_db}dB:d={threshold}", "-f", "null", "-"],
        capture_output=True, text=True, creationflags=flags,
    )
    stderr = probe.stderr or ""
    duration = _parse_duration(stderr)
    silences = _parse_silences(stderr)

    half = target / 2.0
    removed: List[Tuple[float, float]] = []
    for s, e in silences:
        if e is None:
            e = duration if duration is not None else s
        # Keep ``half + pad`` of audio next to the speech on each side, removing
        # only the excess in the middle. Skip silences too short to trim.
        if e - s > target + 2 * pad + 1e-3:
            a, b = s + half + pad, e - half - pad
            if b > a:
                removed.append((a, b))

    if not removed:
        # Nothing to trim - just transcode to wav so RVC has a clean input.
        r = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(in_path),
             "-c:a", "pcm_s16le", str(out_path)],
            capture_output=True, text=True, creationflags=flags,
        )
        if r.returncode != 0:
            log(f"[!] ffmpeg convert failed: {(r.stderr or '').strip()[:300]}")
        return r.returncode == 0

    if duration is None:
        duration = removed[-1][1] + 1.0

    # Keep ranges = the complement of the removed intervals over [0, duration].
    keeps: List[Tuple[float, float]] = []
    cur = 0.0
    for a, b in removed:
        if a > cur:
            keeps.append((cur, a))
        cur = b
    if cur < duration:
        keeps.append((cur, duration))
    if not keeps:
        keeps = [(0.0, duration)]

    # Build a filter graph (atrim each kept range, then concat). Written to a
    # script file so a clip with many pauses can't blow the command-line limit.
    lines, labels = [], []
    for i, (a, b) in enumerate(keeps):
        lines.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[s{i}]")
        labels.append(f"[s{i}]")
    lines.append("".join(labels) + f"concat=n={len(keeps)}:v=0:a=1[out]")
    script_path = out_path.with_suffix(".filter.txt")
    script_path.write_text(";\n".join(lines), encoding="utf-8")

    try:
        r = subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(in_path),
             "-filter_complex_script", str(script_path), "-map", "[out]",
             "-c:a", "pcm_s16le", str(out_path)],
            capture_output=True, text=True, creationflags=flags,
        )
        if r.returncode != 0:
            log(f"[!] ffmpeg silence trim failed: {(r.stderr or '').strip()[:300]}")
        return r.returncode == 0
    finally:
        try:
            script_path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# RVC voice conversion
# ---------------------------------------------------------------------------
_cuda_cache: Optional[bool] = None


def _cuda_available(rvc_dir: Path) -> bool:
    global _cuda_cache
    if _cuda_cache is not None:
        return _cuda_cache
    py = rvc_dir / "runtime" / "python.exe"
    try:
        r = subprocess.run(
            [str(py), "-c", "import torch; print(torch.cuda.is_available())"],
            capture_output=True, text=True, cwd=str(rvc_dir),
            creationflags=_creationflags(), timeout=60,
        )
        _cuda_cache = "true" in (r.stdout or "").lower()
    except Exception:
        logger.debug("CUDA probe failed", exc_info=True)
        _cuda_cache = False
    return _cuda_cache


def resolve_device(rvc_dir: Path, preference: str = "Auto") -> Tuple[str, bool]:
    """Resolve a device string + half-precision flag from the user's preference."""
    pref = (preference or "Auto").strip()
    if pref.lower() == "auto":
        device = "cuda:0" if _cuda_available(Path(rvc_dir)) else "cpu"
    else:
        device = pref
    is_half = device.lower().startswith("cuda")
    return device, is_half


def run_rvc(
    rvc_dir: Path,
    voice: str,
    settings: RvcSettings,
    device: str,
    is_half: bool,
    input_dir: str,
    output_dir: str,
    log: LogFn = print,
    register: Optional[Callable[[subprocess.Popen], None]] = None,
    unregister: Optional[Callable[[subprocess.Popen], None]] = None,
) -> bool:
    """Run Mangio-RVC batch inference over every .wav in ``input_dir``, writing
    converted wavs of the same name into ``output_dir``. Returns True on success."""
    rvc_dir = Path(rvc_dir)
    meta = VOICE_MODELS[voice]
    py = rvc_dir / "runtime" / "python.exe"
    pth = rvc_dir / meta["pth"]
    index = rvc_dir / meta["index"]
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    if not py.exists():
        log(f"[!] RVC runtime not found: {py}")
        return False
    if not pth.exists():
        log(f"[!] RVC model not found: {pth}")
        return False

    # Positional args expected by rvc_batch.py:
    #   f0up_key input_path index_path f0method opt_path model_path index_rate
    #   device is_half filter_radius resample_sr rms_mix_rate protect
    cmd = [
        str(py), str(BATCH_SCRIPT),
        str(settings.pitch),
        str(input_dir),
        str(index),
        settings.f0method,
        str(output_dir),
        str(pth),
        str(settings.index_rate),
        device,
        "True" if is_half else "False",
        str(settings.filter_radius),
        str(settings.resample_sr),
        str(settings.rms_mix_rate),
        str(settings.protect),
    ]

    log(f"-> Converting {meta['label']} voice (device={device}, f0={settings.f0method}, "
        f"pitch={settings.pitch}, index={settings.index_rate}, protect={settings.protect})...")
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(rvc_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, creationflags=_creationflags(),
        )
    except OSError as e:
        log(f"[!] Failed to launch RVC: {e}")
        return False

    if register:
        register(proc)
    tail: List[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                logger.debug("rvc: %s", line)
                tail.append(line)
                if len(tail) > 20:
                    tail.pop(0)
        proc.wait()
    finally:
        if unregister:
            unregister(proc)

    if proc.returncode != 0:
        log(f"[!] RVC exited with code {proc.returncode} for {meta['label']}.")
        for line in tail[-6:]:
            log(f"    {line}")
        return False
    return True


def gather_clip_specs(inputs: List[str], voices: List[str]) -> List[Dict]:
    """Collect matching clips across a mix of folders and .zip archives.

    Each input is either a directory (scanned for audio files) or a .zip file
    (its members are listed but not yet extracted). Returns a list of specs::

        {"kind": "file"|"zip", "path": Path, "member": str|None,
         "voice": str, "name": str}

    Only clips whose filename prefix maps to one of ``voices`` are included.
    Zip members are extracted lazily later via :func:`extract_member` so building
    this list stays cheap even for large archives."""
    specs: List[Dict] = []
    for inp in inputs:
        p = Path(inp)
        if p.is_dir():
            for entry in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                if not entry.is_file() or entry.suffix.lower() not in AUDIO_EXTS:
                    continue
                voice = detect_voice(entry.name)
                if voice and voice in voices:
                    specs.append({"kind": "file", "path": entry, "member": None,
                                  "voice": voice, "name": entry.name})
        elif p.is_file() and p.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(p) as zf:
                    for member in sorted(zf.namelist(), key=str.lower):
                        if member.endswith("/"):
                            continue
                        base = Path(member).name
                        if Path(base).suffix.lower() not in AUDIO_EXTS:
                            continue
                        voice = detect_voice(base)
                        if voice and voice in voices:
                            specs.append({"kind": "zip", "path": p, "member": member,
                                          "voice": voice, "name": base})
            except (zipfile.BadZipFile, OSError) as e:
                logger.warning("Could not read zip %s: %s", p, e)
    return specs


def extract_member(zip_path: Path, member: str, dest_dir: Path) -> Path:
    """Extract a single zip member into ``dest_dir`` (flat, by basename)."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / Path(member).name
    with zipfile.ZipFile(zip_path) as zf, zf.open(member) as src, open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return out
