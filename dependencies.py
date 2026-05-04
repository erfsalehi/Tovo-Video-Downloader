"""Bootstrap external binaries: yt-dlp, ffmpeg, and Deno.

All downloads use a verified SSL context. Set ``TOVO_INSECURE_SSL=1`` in the
environment as an emergency escape hatch (e.g. on locked-down corporate
networks); do not enable it by default.
"""
from __future__ import annotations

import logging
import os
import shutil
import ssl
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, List

logger = logging.getLogger(__name__)

YT_DLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
DENO_URL = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"

USER_AGENT = "Mozilla/5.0 (compatible; TovoVideoDownloader)"

LogFn = Callable[[str], None]


def _ssl_context() -> ssl.SSLContext:
    if os.environ.get("TOVO_INSECURE_SSL") == "1":
        logger.warning("TOVO_INSECURE_SSL=1 set; SSL verification disabled.")
        return ssl._create_unverified_context()
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _download(url: str, dest: Path) -> None:
    """Stream ``url`` to ``dest`` using a verified SSL context."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, context=_ssl_context()) as response, dest.open("wb") as out:
        shutil.copyfileobj(response, out)


def _safe_unlink(path: Path, log: LogFn) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        log(f"-> Warning: Could not remove {path.name}: {e}")


def find_missing_tools(base_path: Path) -> List[str]:
    """Return human-readable names for any tool not on PATH or alongside the script."""
    missing: List[str] = []
    checks = (
        ("yt-dlp", "yt-dlp", "yt-dlp.exe"),
        ("FFmpeg", "ffmpeg", "ffmpeg.exe"),
        ("Deno (JS Runtime)", "deno", "deno.exe"),
    )
    for label, cli, local in checks:
        if not (shutil.which(cli) or (base_path / local).exists()):
            missing.append(label)
    return missing


def _download_yt_dlp(base_path: Path, log: LogFn) -> None:
    target = base_path / "yt-dlp.exe"
    if target.exists():
        return
    log("-> Downloading yt-dlp.exe...")
    for attempt in range(3):
        try:
            _download(YT_DLP_URL, target)
            if target.exists() and target.stat().st_size > 0:
                break
        except Exception as e:
            log(f"-> Error downloading yt-dlp (Attempt {attempt + 1}/3): {e}")
            _safe_unlink(target, log)
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError("yt-dlp download failed.")
    log("   Done!")


def _download_ffmpeg(base_path: Path, log: LogFn) -> None:
    target = base_path / "ffmpeg.exe"
    if target.exists():
        return
    log("-> Downloading FFmpeg (this may take a moment)...")
    temp_zip = base_path / "ffmpeg.zip"
    for attempt in range(3):
        try:
            _download(FFMPEG_URL, temp_zip)
            log("-> Extracting FFmpeg...")
            with zipfile.ZipFile(temp_zip, "r") as zf:
                for member in zf.namelist():
                    if member.endswith("ffmpeg.exe"):
                        with zf.open(member) as src, target.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                        break
            if target.exists():
                break
        except Exception as e:
            log(f"-> Error downloading/extracting FFmpeg (Attempt {attempt + 1}/3): {e}")
            _safe_unlink(target, log)
        finally:
            _safe_unlink(temp_zip, log)
    if not target.exists():
        raise RuntimeError("FFmpeg extraction failed.")
    log("   Done!")


def _download_deno(base_path: Path, log: LogFn) -> None:
    target = base_path / "deno.exe"
    if target.exists():
        return
    log("-> Downloading Deno (JS runtime for YouTube extraction)...")
    temp_zip = base_path / "deno.zip"
    for attempt in range(3):
        try:
            _download(DENO_URL, temp_zip)
            log("-> Extracting Deno...")
            with zipfile.ZipFile(temp_zip, "r") as zf:
                zf.extractall(base_path)
            if target.exists():
                break
        except Exception as e:
            log(f"-> Error downloading/extracting Deno (Attempt {attempt + 1}/3): {e}")
            _safe_unlink(target, log)
        finally:
            _safe_unlink(temp_zip, log)
    if not target.exists():
        raise RuntimeError("Deno extraction failed.")
    log("   Done!")


def install_all(base_path: Path, log: LogFn) -> None:
    """Download every missing binary into ``base_path``."""
    log("--- Starting Dependency Setup ---")
    _download_yt_dlp(base_path, log)
    _download_ffmpeg(base_path, log)
    _download_deno(base_path, log)
    log("--- Setup Complete! You are ready to go. ---")
