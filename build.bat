@echo off
chcp 65001 >nul
echo ============================================
echo   Zoom Auto Recorder - Build Script
echo ============================================
echo.

REM Check Python
python --version
echo.

REM Install dependencies
echo Installing dependencies...
pip install -r requirements.txt --quiet
pip install pyinstaller --quiet
echo.

REM Clean previous builds
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
echo.

REM Build executable
echo Building EXE...
python -m PyInstaller --onefile ^
    --windowed ^
    --name "ZoomAutoRecorder" ^
    --add-data "src;src" ^
    --icon=NONE ^
    --clean ^
    main.py
echo.

REM Create data folders
if not exist "dist\data" mkdir "dist\data"
if not exist "dist\logs" mkdir "dist\logs"
if not exist "dist\data\debug" mkdir "dist\data\debug"

REM Copy the portable recorder next to the executable
if exist "KamelRec-1.0.0-Portable.exe" (
    copy /y "KamelRec-1.0.0-Portable.exe" "dist\KamelRec-1.0.0-Portable.exe" >nul
    echo KamelRec copied to dist
) else if exist "KamelRec.exe" (
    copy /y "KamelRec.exe" "dist\KamelRec.exe" >nul
    echo KamelRec copied to dist
) else (
    echo WARNING: KamelRec executable not found next to build.bat
)
echo.

echo ============================================
echo   Build complete!
echo   Executable: dist\ZoomAutoRecorder.exe
echo ============================================
pause
