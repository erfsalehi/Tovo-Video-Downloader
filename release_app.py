"""Bundle the application into a portable ZIP for distribution.

User-private files (config.json, cookies.txt) are intentionally excluded.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

RELEASE_NAME = "Video_Downloader_Portable.zip"

INCLUDE = (
    "app.py",
    "config.py",
    "dependencies.py",
    "subtitles.py",
    "widgets.py",
    "Start.bat",
    "start.sh",
    "requirements.txt",
    "README.md",
)


def create_release() -> Path:
    project_dir = Path(__file__).resolve().parent
    release_path = project_dir / RELEASE_NAME

    print(f"Creating release: {RELEASE_NAME}...")

    with zipfile.ZipFile(release_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for name in INCLUDE:
            file_path = project_dir / name
            if file_path.exists():
                print(f"  Adding {name}...")
                zipf.write(file_path, name)
            else:
                print(f"  Skipping {name} (not found)")
        zipf.writestr("Downloads/", "")

    print(f"\nSuccess! Release bundle created at: {release_path}")
    print("You can now share this ZIP file with others.")
    return release_path


if __name__ == "__main__":
    create_release()
