# Tovo Video Downloader 🚀

A modern, streamlined GUI application for batch downloading videos from various social media platforms using `yt-dlp`. It features automatic subtitle synchronization with Whisper AI and a streamlined workflow for video editors and content creators.

## ✨ Features

- **Batch Downloading**: Paste multiple titles and links to download them all at once.
- **Modern GUI**: A clean, premium interface built with Python's Tkinter.
- **Whisper AI Integration**: Automatically sync subtitles with video or dub audio using Whisper's smart alignment.
- **Premiere Pro Ready**: Downloads videos in high-quality H.264/AAC MP4 format for maximum compatibility.
- **Automatic Setup**: Automatically downloads required binaries (`yt-dlp` and `FFmpeg`) on first run.
- **Flexible Options**: Support for 2-second interval subtitles or AI-driven smart sync.

## 🛠️ Installation & Setup

1. **Clone the Repository**:
   ```bash
   git clone https://github.com/erfsalehi/Tovo-Video-Downloader.git
   cd Tovo-Video-Downloader
   ```

2. **Install Dependencies**:
   Ensure you have Python 3.8+ installed, then run:
   ```bash
   pip install -r requirements.txt
   ```

3. **Run the App**:
   ```bash
   python app.py
   ```

*Note: On the first run, the application will prompt you to download `yt-dlp` and `FFmpeg` if they are not already in your path.*

## 📖 How to Use

1. **Input**: Paste your titles and links in the text area. The format should be:
   ```
   Video Title 1
   https://link-to-video-1
   Video Title 2
   https://link-to-video-2
   ```
2. **Settings**: Choose your "Save to" directory and optional "Dub Folder" if you're using Whisper AI for syncing with external audio.
3. **Download**: Click "Start Download" and watch the progress in the log area.
4. **Output**: Your videos, subtitles (SRT), and a batch log will be saved to your selected folder.

## 📂 Project Structure

- `app.py`: The main application core and GUI logic.
- `release_app.py`: Utility script to package the app for distribution.
- `requirements.txt`: Python dependencies.
- `.gitignore`: Excludes local configurations and large binaries from the repository.

## 📄 License

This project is for personal and educational use. Please respect the terms of service of the platforms you download from.

---
*Created with ❤️ for efficient content creation.*
