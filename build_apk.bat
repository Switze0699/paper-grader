@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Build APK

REM ---- use the fast China mirror, otherwise the download is ~200x slower ----
set FLUTTER_STORAGE_BASE_URL=https://storage.flutter-io.cn
set PUB_HOSTED_URL=https://pub.flutter-io.cn

echo ==========================================================
echo   Build APK  -  Batch Grader
echo ==========================================================
echo.
echo   Toolchain status:
echo     - Flutter SDK  : already installed (3.7 GB, no need to download)
echo     - Android SDK  : will be downloaded now (~2 GB)
echo.
echo   IMPORTANT
echo     1. It will ask you a few questions (y/n) - just press Enter
echo        to accept the default, which is always "y".
echo     2. If Windows asks about firewall, click "Allow access".
echo     3. Do NOT close this window while it is working.
echo.
echo   The APK will be saved to:
echo     build\app\outputs\flutter-apk\app-release.apk
echo.
echo   If you want to stop it, just close this window.
echo.
echo ----------------------------------------------------------
pause
echo.
echo Starting... (normal even if nothing shows for several minutes)
echo.

".venv\Scripts\flet.exe" build apk --org com.grader.batch --project batch_grader --product "Batch Grader"

echo.
echo ==========================================================
echo   Finished (or stopped). Read the messages above.
echo.
echo   The APK is normally at:
echo     build\app\outputs\flutter-apk\app-release.apk
echo ==========================================================
echo.
pause
