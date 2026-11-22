"""Copie de texte dans le presse-papiers Windows, de façon fiable.

``QApplication.clipboard().setText()`` passe par OLE avec rendu différé. Sur
certaines configurations (PySide6 récent + Python récent), le texte n'est
jamais publié sur le presse-papiers système et Qt garde même le presse-papiers
ouvert, ce qui bloque les autres applications. On écrit donc directement via
l'API Win32 (``OpenClipboard`` / ``SetClipboardData``), avec repli sur Qt.
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

logger = logging.getLogger("ZoomAutoRecorder")

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_user32 = None
_kernel32 = None


def _api():
    """Charge les fonctions Win32 une seule fois (no-op hors Windows)."""
    global _user32, _kernel32
    if _user32 is not None:
        return _user32, _kernel32
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
    except (AttributeError, OSError) as exc:  # pragma: no cover - hors Windows
        logger.debug("Presse-papiers Win32 indisponible: %s", exc)
        user32 = kernel32 = None
    _user32, _kernel32 = user32, kernel32
    return _user32, _kernel32


def _open_clipboard(user32, attempts: int = 20, delay: float = 0.05) -> bool:
    for _ in range(attempts):
        if user32.OpenClipboard(None):
            return True
        # Le presse-papiers peut être verrouillé quelques millisecondes par un
        # autre programme (ou par Qt en rendu différé).
        time.sleep(delay)
    return False


def get_text() -> str:
    """Lit le texte du presse-papiers (chaîne vide si indisponible)."""
    user32, kernel32 = _api()
    if user32 is None:
        return ""
    if not _open_clipboard(user32):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _set_text_win32(text: str) -> bool:
    user32, kernel32 = _api()
    if user32 is None:
        return False
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not _open_clipboard(user32):
        logger.debug("Presse-papiers verrouillé: OpenClipboard a échoué.")
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            return False
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return False
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            kernel32.GlobalUnlock(handle)
        # Le handle appartient désormais au presse-papiers : ne pas le libérer.
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            return False
        return True
    finally:
        user32.CloseClipboard()


def copy_text(text: str) -> bool:
    """Copie ``text`` et vérifie que le presse-papiers contient bien la valeur."""
    text = "" if text is None else str(text)
    if not text:
        return False

    if _set_text_win32(text):
        if get_text() == text:
            return True
        logger.debug("Copie Win32 non confirmée, essai via Qt.")

    # Repli Qt (utile si l'API Win32 est indisponible).
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            clipboard = app.clipboard()
            clipboard.setText(text)
            app.processEvents()
            if clipboard.text() == text:
                return True
    except Exception as exc:  # pragma: no cover - défensif
        logger.debug("Copie via Qt impossible: %s", exc)
    return False
