"""
Configuration constants and paths for Zoom Auto Recorder.
"""
import os
import sys
import shutil
from pathlib import Path

APP_NAME = "ZoomAutoRecorder"
APP_VERSION = "1.0.0"

# --- Application directories -----------------------------------------------
if getattr(sys, "frozen", False):
    # Running from a PyInstaller bundle.
    # Keep an existing adjacent data/ folder (portable installs), otherwise
    # use a per-user writable location so Program Files installs also work.
    APP_DIR = Path(sys.executable).resolve().parent
    portable_data = APP_DIR / "data"
    if (portable_data / "zoom_auto_recorder.db").exists():
        DATA_DIR = portable_data
        LOG_DIR = APP_DIR / "logs"
    else:
        local_root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
        DATA_DIR = local_root / "data"
        LOG_DIR = local_root / "logs"
else:
    APP_DIR = Path(__file__).resolve().parent.parent
    DATA_DIR = APP_DIR / "data"
    LOG_DIR = APP_DIR / "logs"

DB_PATH = DATA_DIR / "zoom_auto_recorder.db"
APP_SETTINGS_PATH = DATA_DIR / "settings.json"
LOG_FILE = LOG_DIR / "app.log"

# Default recording storage location
DEFAULT_RECORDINGS_DIR = Path.home() / "Documents" / "Zoom Auto Recorder"

# KamelRec portable recorder (records a window with system audio)
# The recorder is resolved dynamically: next to the app, on PATH, or via env var.
_kamelrec_from_env = os.environ.get("ZOOM_AUTORECORDER_KAMELREC_EXE")
KAMELREC_EXE_CANDIDATES = list(dict.fromkeys(filter(None, [
    str(APP_DIR / "KamelRec-1.0.0-Portable.exe"),
    str(APP_DIR / "KamelRec.exe"),
    _kamelrec_from_env,
    shutil.which("KamelRec-1.0.0-Portable.exe"),
    shutil.which("KamelRec.exe"),
])))
# Use KamelRec as the recording engine when available
USE_KAMELREC = True

# Zoom application paths (common install locations, resolved dynamically)
_program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
_program_files_x86 = os.environ.get("ProgramFiles(x86", r"C:\Program Files (x86)")
_appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
_local_appdata = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
ZOOM_EXE_CANDIDATES = list(dict.fromkeys(filter(None, [
    os.environ.get("ZOOM_AUTORECORDER_ZOOM_EXE"),
    str(Path(_program_files) / "ZoomClient" / "Zoom.exe"),
    str(Path(_program_files_x86) / "ZoomClient" / "Zoom.exe"),
    str(Path(_program_files) / "Zoom" / "bin" / "Zoom.exe"),
    str(Path(_program_files_x86) / "Zoom" / "bin" / "Zoom.exe"),
    str(Path(_appdata) / "Zoom" / "bin" / "Zoom.exe"),
    str(Path(_local_appdata) / "Zoom" / "bin" / "Zoom.exe"),
])))

# Zoom meeting URL patterns
ZOOM_JOIN_URL_PREFIX = "zoommtg://"
ZOOM_HTTP_URL_PREFIX = "https://"
ZOOM_MEETING_ID_REGEX = r"(\d{9,12})(?:[?\s#]|$)"

# State machine states
STATE_WAITING = "Waiting"
STATE_LAUNCHING_ZOOM = "Launching Zoom"
STATE_JOINING_MEETING = "Joining Meeting"
STATE_WAITING_ROOM = "Waiting Room"
STATE_IN_MEETING = "In Meeting"
STATE_RECORDING = "Recording"
STATE_COMPLETED = "Completed"
STATE_PAUSED = "Paused"
STATE_ERROR = "Error"
STATE_CANCELLED = "Cancelled"

# Recording file naming
RECORDING_FILENAME_TEMPLATE = "{name}_{date}_{time}.{ext}"

# Polling intervals (seconds)
POLL_INTERVAL_IDLE = 30       # when waiting for a meeting
POLL_INTERVAL_ACTIVE = 1      # when a meeting is in progress
POLL_INTERVAL_JOINING = 0.5   # during join sequence
POLL_INTERVAL_RECORDING = 2   # while recording

# Timeouts (seconds)
ZOOM_LAUNCH_TIMEOUT = 60
JOIN_TIMEOUT = 180
WAITING_ROOM_TIMEOUT = 600
RECORDING_END_TIMEOUT = 120

# Retry settings
MAX_PASSCODE_RETRIES = 3

# UI refresh interval (ms)
UI_REFRESH_MS = 500