"""Persistent application configuration with atomic writes."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "downloads_dir": "",
    "dub_dir": "",
    "use_browser_cookies": False,
    "concurrent_downloads": False,
    "max_concurrent": 5,
    "groq_api_key": "",
    "transcription_provider": "Local Whisper",
}


class Config:
    """Loads, mutates, and atomically saves a JSON config file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Could not load config %s: %s", self.path, e)

    def save(self) -> None:
        # Write to a temp file then os.replace() so a crash mid-write
        # never leaves config.json in a half-written state.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("Could not save config %s: %s", self.path, e)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
