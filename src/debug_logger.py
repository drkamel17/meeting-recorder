"""
Debug tools: Screenshots and UI tree dumping for automation troubleshooting.
"""
import os
import datetime
from PySide6.QtGui import QPixmap
from src.config import DATA_DIR

DEBUG_DIR = DATA_DIR / "debug"

def ensure_debug_dir():
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

def take_zoom_window_screenshot(title_filter=""):
    """Captures a screenshot and saves it in the debug folder."""
    ensure_debug_dir()
    try:
        # Utilise pywinauto pour capturer la fenêtre Zoom active
        from pywinauto import Desktop
        desktop = Desktop(backend="uia")
        
        if title_filter:
            windows = desktop.windows(title_re=title_filter)
        else:
            windows = desktop.windows()

        for w in windows:
            if w.is_visible():
                filename = f"{datetime.datetime.now().strftime('%H%M%S')}_{w.window_text()[:30]}.png"
                path = str(DEBUG_DIR / filename)
                w.capture_as_image().save(path)
                return path
    except Exception as e:
        print(f"Erreur capture: {e}")
    return None

def dump_zoom_ui_tree():
    """Dumps the UI hierarchy of all visible Zoom windows to the console."""
    try:
        from pywinauto import Desktop
        desktop = Desktop(backend="uia")
        for w in desktop.windows(title_re=".*Zoom.*"):
            if w.is_visible():
                print(f"--- Window: {w.window_text()} ---")
                try:
                    ctrl = w.wrapper_object()
                    print(ctrl.print_control_identifiers())
                except:
                    pass
    except Exception as e:
        print(f"Erreur dump UI: {e}")
