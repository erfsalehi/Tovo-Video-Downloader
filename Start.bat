@echo off
title Tovo Video Downloader Setup
echo ========================================================
echo        Tovo Video Downloader - Automated Setup
echo ========================================================
echo.

:: Check for python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not added to PATH. 
    echo Please install Python 3.8+ from python.org and check the box "Add Python to PATH" during installation.
    echo Press any key to exit...
    pause >nul
    exit /b
)

:: Check/Create Virtual Environment
if not exist "venv\Scripts\activate.bat" (
    echo [1/3] Creating isolated Python Virtual Environment...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment. Make sure you have permissions in this folder.
        pause
        exit /b
    )
)

:: Activate venv
call venv\Scripts\activate.bat

:: Install Requirements
echo [2/3] Checking and installing dependencies...
echo       (This may take a few minutes the first time depending on your internet speed)
echo.

:: Upgrade pip first
python -m pip install --upgrade pip >nul 2>&1

:: Force CPU version of PyTorch (much smaller: ~200MB vs ~2.5GB)
python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu

:: Install the rest of the requirements (yt-dlp, stable-whisper)
python -m pip install -r requirements.txt

echo.
echo [3/3] Starting Application...
echo ========================================================
python app.py

:: If app crashes, show pause so the window doesn't immediately close
if %errorlevel% neq 0 (
    echo.
    echo [!] Application exited with an error. 
    pause
)
