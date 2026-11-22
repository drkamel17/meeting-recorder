"""
KamelRec integration
====================
Drives the KamelRec portable screen recorder (Electron app) so it records the
Zoom meeting window with system audio.

The app exposes a Chromium accessibility tree when started with
``--force-renderer-accessibility``, which lets us find and click its controls
reliably (by automation id, by label, with positional fallbacks).

Typical flow:
    kr = KamelRecController()
    kr.prepare_settings()          # output dir, audio, quality
    kr.start(zoom_window_title)    # select "Windows" -> Zoom window -> record
    ...
    path = kr.stop()               # stop and return the saved file
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from pywinauto import Desktop
from pyautogui import click

from src.config import DEFAULT_RECORDINGS_DIR, KAMELREC_EXE_CANDIDATES

logger = logging.getLogger("ZoomAutoRecorder")

KAMELREC_APP_DIR = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")) / "kamelrec"
KAMELREC_SETTINGS_PATH = KAMELREC_APP_DIR / "settings.json"
KAMELREC_LOG_PATH = KAMELREC_APP_DIR / "logs" / "app.log"

# UI labels (Arabic UI by default, English/French as fallbacks)
START_LABELS = {"بدء التسجيل", "بدء", "Start recording", "Start", "Démarrer l'enregistrement",
                "Démarrer", "بدء التسجيل "}
STOP_LABELS = {"إيقاف التسجيل", "إيقاف", "إنهاء التسجيل", "Stop recording", "Stop",
               "Arrêter l'enregistrement", "Arrêter"}
PAUSE_LABELS = {"إيقاف مؤقت", "Pause", "Pause recording", "إيقاف مؤقت مؤقت"}
RESUME_LABELS = {"استئناف", "استئناف التسجيل", "Resume", "Resume recording", "Reprendre"}
CONFIRM_LABELS = {"إيقاف", "نعم", "موافق", "تأكيد", "Stop", "Yes", "OK", "Oui", "Confirmer"}
WINDOWS_TAB_LABELS = {"النوافذ", "Windows", "Fenêtres"}
SCREENS_TAB_LABELS = {"الشاشات", "Screens", "Écrans", "Ecrans"}
IGNORED_BUTTON_PREFIXES = (
    "معاينة:", "Open with", "Preview:",
    "فتح بالمشغّل", "إظهار في المجلد",
    "إعادة تسمية:", "حذف:",
    "Show in folder", "Rename:", "Delete:",
)


IGNORED_BUTTONS = {
    "تحديث", "فتح المجلد", "إعادة ضبط المنطقة", "المصدر كاملًا", "منطقة مخصّصة",
    "لقطة شاشة", "الإعدادات", "تصغير", "تكبير", "إغلاق", "جديد",
    "Refresh", "Open folder", "Reset region", "Full source", "Custom region",
}


@dataclass
class UiNode:
    rect: tuple  # (left, top, right, bottom)


class KamelRecController:
    """Small UI-automation driver for the KamelRec Electron recorder."""

    def __init__(self, exe_path: Optional[str] = None):
        self.exe_path = exe_path or self.find_exe()
        self._proc = None
        self._record_button_pos: Optional[tuple[int, int]] = None
        self._output_dir: Optional[Path] = None

    # ------------------------------------------------------------------
    # Discovery / process management
    # ------------------------------------------------------------------
    @staticmethod
    def find_exe() -> Optional[str]:
        for candidate in KAMELREC_EXE_CANDIDATES:
            p = Path(candidate)
            if p.exists():
                return str(p)
        return None

    @property
    def available(self) -> bool:
        return bool(self.exe_path and os.path.exists(self.exe_path))

    def _find_window(self):
        try:
            desktop = Desktop(backend="uia")
            for w in desktop.windows():
                try:
                    if w.window_text().strip() == "KamelRec" and w.is_visible():
                        return w
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"KamelRec: erreur enumeration fenêtres: {e}")
        return None

    def is_running(self) -> bool:
        return self._find_window() is not None

    def ensure_running(self, timeout: int = 60):
        """Launch KamelRec if needed and return its window."""
        win = self._find_window()
        if win:
            return win

        if not self.available:
            raise RuntimeError("KamelRec introuvable (KamelRec-1.0.0-Portable.exe)")

        logger.info("Lancement de KamelRec...")
        # Kill stale hidden instances that would block the single-instance lock
        subprocess_kill()
        self._proc = subprocess_popen([self.exe_path, "--force-renderer-accessibility"])

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1)
            win = self._find_window()
            if win:
                logger.info("KamelRec ouvert.")
                time.sleep(2)  # let the renderer build its accessibility tree
                return win
            if self._proc is not None and self._proc.poll() is not None:
                # A previous instance may already own the lock; retry detection
                win = self._find_window()
                if win:
                    return win
                raise RuntimeError("KamelRec s'est arrêté au démarrage")

        raise RuntimeError("KamelRec n'a pas démarré à temps")

    def close(self):
        """Close KamelRec (best effort)."""
        try:
            self._proc and self._proc.terminate()
        except Exception:
            pass
        self._proc = None

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def prepare_settings(self) -> None:
        """Configure KamelRec output/audio before it starts.

        Writes the settings file, then restarts KamelRec if it was already
        running with a different configuration (settings are read at startup).
        Every other user setting is preserved.
        """
        try:
            existing = {}
            if KAMELREC_SETTINGS_PATH.exists():
                existing = json.loads(KAMELREC_SETTINGS_PATH.read_text(encoding="utf-8"))

            desired = {
                "outputDir": str(DEFAULT_RECORDINGS_DIR),
                "defaultSystemAudio": True,
                "defaultMicEnabled": False,
                "countdownSec": 3,
                "minimizeOnRecord": True,
                "showToolbarOnRecord": True,
                "hideToolbarFromCapture": True,
                "openFolderAfterSave": False,
            }

            changed = any(existing.get(k) != v for k, v in desired.items())
            existing.update(desired)
            existing.setdefault("defaultFormat", "mp4")
            existing.setdefault("defaultQuality", "medium")
            existing.setdefault("defaultFps", 30)
            existing.setdefault("defaultResolution", "720p")

            KAMELREC_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            KAMELREC_SETTINGS_PATH.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            if changed and self.is_running():
                logger.info("KamelRec: redémarrage pour appliquer les paramètres...")
                subprocess_kill()
                time.sleep(2)

            logger.info(f"KamelRec: dossier de sortie = {DEFAULT_RECORDINGS_DIR}")
        except Exception as e:
            logger.warning(f"KamelRec: impossible de configurer les paramètres: {e}")

    def _settings_output_dir(self) -> Path:
        try:
            data = json.loads(KAMELREC_SETTINGS_PATH.read_text(encoding="utf-8"))
            return Path(data.get("outputDir") or DEFAULT_RECORDINGS_DIR)
        except Exception:
            return DEFAULT_RECORDINGS_DIR

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _nodes(window) -> list:
        try:
            return list(window.descendants())
        except Exception:
            return []

    @staticmethod
    def _info(node):
        try:
            info = node.element_info
            rect = info.rectangle
            return info, (rect.left, rect.top, rect.right, rect.bottom)
        except Exception:
            return None, None

    def _find(self, window, *, name=None, names: Optional[Iterable[str]] = None,
              contains: Optional[str] = None, automation_id: Optional[str] = None,
              control_type: Optional[str] = None) -> Optional[UiNode]:
        wanted_names = {n for n in (names or [])}
        if name:
            wanted_names.add(name)
        for node in self._nodes(window):
            info, rect = self._info(node)
            if info is None:
                continue
            try:
                if automation_id and getattr(info, "automation_id", "") != automation_id:
                    continue
                if control_type and getattr(info, "control_type", "") != control_type:
                    continue
                label = (getattr(info, "name", "") or "").strip()
                if wanted_names and label not in wanted_names:
                    continue
                if contains and contains.lower() not in label.lower():
                    continue
                if rect[2] <= rect[0] or rect[3] <= rect[1]:
                    continue
                return UiNode(rect)
            except Exception:
                continue
        return None

    @staticmethod
    def _center(rect) -> tuple[int, int]:
        return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2

    def _click_node(self, node: UiNode, pause: float = 0.6) -> bool:
        if not node:
            return False
        x, y = self._center(node.rect)
        click(x, y)
        time.sleep(pause)
        return True

    def _tab_items(self, window, control_type="TabItem") -> list[tuple[str, tuple]]:
        result = []
        for node in self._nodes(window):
            info, rect = self._info(node)
            if info is None:
                continue
            try:
                if getattr(info, "control_type", "") == control_type:
                    label = (getattr(info, "name", "") or "").strip()
                    result.append((label, rect))
            except Exception:
                continue
        return result

    def _buttons(self, window) -> list[tuple[str, tuple]]:
        result = []
        for node in self._nodes(window):
            info, rect = self._info(node)
            if info is None:
                continue
            try:
                if getattr(info, "control_type", "") == "Button":
                    label = (getattr(info, "name", "") or "").strip()
                    if rect[2] > rect[0] and rect[3] > rect[1]:
                        result.append((label, rect))
            except Exception:
                continue
        return result

    # ------------------------------------------------------------------
    # Source selection
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_label(text: str) -> str:
        """Normalise un libelle (marques RTL, espaces multiples, casse)."""
        invisible = dict.fromkeys(
            map(ord, "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\ufeff"),
            None,
        )
        return " ".join((text or "").translate(invisible).split()).lower()

    @staticmethod
    def _title_tokens(title: str) -> list[str]:
        """Mots distinctifs d'un titre de fenetre (ex: code de reunion)."""
        skip = {"google", "chrome", "meet", "zoom", "microsoft", "edge",
                "fenetre", "fenêtre", "window", "reunion", "réunion",
                "meeting", "navigateur", "browser"}
        tokens = []
        for raw in (title or "").split():
            token = raw.strip(" -–—·:•|[](){}<>,.").lower()
            if len(token) >= 5 and token not in skip:
                tokens.append(token)
        return tokens

    def _select_windows_source(self, window, zoom_title: str) -> bool:
        # 1) Onglet "Source"
        source_tab = self._find(window, automation_id="tab-source")
        if source_tab:
            self._click_node(source_tab, 0.8)

        # 2) Sous-onglet "Fenetres" (les sous-onglets sont sous la barre
        #    d'onglets principale, donc plus bas a l'ecran).
        tabs = self._tab_items(window)
        sub_tabs = [(lbl, r) for lbl, r in tabs if r[1] > 150]
        chosen = None
        for lbl, r in sub_tabs:
            if lbl in WINDOWS_TAB_LABELS:
                chosen = (lbl, r)
                break
        if chosen is None and sub_tabs:
            # le sous-onglet de gauche est "Fenetres", celui de droite "Ecrans"
            sub_tabs.sort(key=lambda item: item[1][0])
            chosen = sub_tabs[0]

        if chosen:
            self._click_node(UiNode(chosen[1]), 1.5)
            logger.info(f"KamelRec: onglet source '{chosen[0]}'.")
        else:
            logger.warning("KamelRec: onglet 'Fenetres' introuvable.")

        # 3) Zone des vignettes, deduite de la geometrie des sous-onglets
        #    (jamais de coordonnees absolues : la fenetre KamelRec bouge).
        if sub_tabs:
            pane_left = min(r[0] for _lbl, r in sub_tabs)
            pane_right = max(r[2] for _lbl, r in sub_tabs)
            band_top = max(r[3] for _lbl, r in sub_tabs)
            band_bottom = band_top + 620
        else:
            pane_left = pane_right = band_top = band_bottom = None

        def _is_thumbnail(rect) -> bool:
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]
            if width < 120 or height < 90:
                return False
            if pane_left is None:
                return True
            return (
                rect[0] >= pane_left - 240
                and rect[2] <= pane_right + 40
                and rect[1] >= band_top
                and rect[3] <= band_bottom
            )

        # 4) Attendre les vignettes puis choisir LA bonne fenetre
        target_norm = self._norm_label(zoom_title)
        tokens = self._title_tokens(zoom_title)
        # Ne jamais selectionner l'application elle-meme
        own_markers = ("zoom auto recorder", "zoomautorecorder", "opencode", "kamelrec")

        deadline = time.time() + 15
        seen_labels = []
        while time.time() < deadline:
            time.sleep(0.8)
            candidates = [
                (lbl, r) for lbl, r in self._buttons(window)
                if lbl
                and lbl not in IGNORED_BUTTONS
                and not lbl.startswith(IGNORED_BUTTON_PREFIXES)
                and _is_thumbnail(r)
            ]
            seen_labels = [lbl for lbl, _ in candidates]
            if not candidates:
                continue

            best = None
            for lbl, r in candidates:
                norm = self._norm_label(lbl)
                if not norm or any(marker in norm for marker in own_markers):
                    continue
                score = 0
                if target_norm and norm == target_norm:
                    score = 100
                elif target_norm and min(len(norm), len(target_norm)) >= 8 and (
                    norm.startswith(target_norm) or target_norm.startswith(norm)
                ):
                    score = 90
                elif target_norm and min(len(norm), len(target_norm)) >= 8 and (
                    norm in target_norm or target_norm in norm
                ):
                    score = 80
                else:
                    for token in tokens:
                        if token in norm:
                            score = max(score, 70)
                if score and (best is None or score > best[0]):
                    best = (score, lbl, r)

            if best is not None:
                score, lbl, r = best
                self._click_node(UiNode(r), 0.8)
                logger.info(f"KamelRec: source selectionnee '{lbl}' (score {score}).")
                return True

        logger.warning(
            f"KamelRec: aucune fenetre source pour '{zoom_title}' "
            f"(vignettes vues: {seen_labels})."
        )
        return False

    # ------------------------------------------------------------------
    # Log watching (KamelRec writes session events there)
    # ------------------------------------------------------------------
    @staticmethod
    def _log_size() -> int:
        try:
            return KAMELREC_LOG_PATH.stat().st_size
        except Exception:
            return 0

    @staticmethod
    def _read_log_from(offset: int) -> str:
        try:
            with open(KAMELREC_LOG_PATH, "rb") as f:
                f.seek(offset)
                return f.read().decode("utf-8", errors="ignore")
        except Exception:
            return ""

    def _wait_for_log(self, marker: str, offset: int, timeout: float) -> bool:
        """Wait until ``marker`` appears in the KamelRec log after ``offset``."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if marker in self._read_log_from(offset):
                return True
            time.sleep(0.5)
        return False

    # ------------------------------------------------------------------
    # Record control
    # ------------------------------------------------------------------
    def _find_record_button(self, window) -> Optional[UiNode]:
        node = self._find(window, names=START_LABELS, control_type="Button")
        if node:
            return node
        # Positional fallback: the big button in the right preview panel
        best = None
        for lbl, r in self._buttons(window):
            if r[0] > 950 and 250 < r[1] < 450 and (r[2] - r[0]) > 150:
                if best is None or (r[2] - r[0]) > (best[2] - best[0]):
                    best = r
        return UiNode(best) if best else None

    def _ensure_foreground(self, window) -> None:
        """Bring the KamelRec window to the foreground before clicking it."""
        try:
            import win32con
            import win32gui
            hwnd = window.handle
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.5)
        except Exception as e:
            logger.debug(f"KamelRec: mise au premier plan impossible: {e}")
            try:
                window.set_focus()
            except Exception:
                pass

    def start(self, zoom_title: str, timeout: int = 90) -> bool:
        """Select the Zoom window and start recording.

        KamelRec needs several seconds (countdown + capture init) before the
        session really opens, so we wait for its own log event instead of
        trusting the button state alone.
        """
        self.prepare_settings()
        window = self.ensure_running()
        self._output_dir = self._settings_output_dir()
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # Zoom is usually in front after joining: raise KamelRec to click its UI
        self._ensure_foreground(window)

        if not self._select_windows_source(window, zoom_title):
            logger.warning("KamelRec: sélection de la source Zoom incertaine.")

        window = self._find_window() or window
        self._ensure_foreground(window)
        record_btn = self._find_record_button(window)
        if not record_btn:
            logger.error("KamelRec: bouton d'enregistrement introuvable.")
            return False

        self._record_button_pos = self._center(record_btn.rect)
        log_offset = self._log_size()

        self._click_node(record_btn, 1.0)
        logger.info("KamelRec: enregistrement lancé, attente du démarrage effectif...")

        # Wait for the real capture session to open (countdown + init)
        if self._wait_for_log("recording session opened {", log_offset, timeout):
            logger.info("KamelRec: session d'enregistrement ouverte.")
            time.sleep(1.0)
            return True

        # Fallback: accept if the stop button is visible
        if self._find_stop_button_anywhere():
            logger.info("KamelRec: bouton d'arrêt visible (session non confirmée par le log).")
            return True

        logger.error("KamelRec: la session d'enregistrement n'a pas démarré.")
        return False

    def _recorder_windows(self) -> list:
        """Return KamelRec windows (main window, toolbar, dialogs)."""
        result = []
        try:
            for w in Desktop(backend="uia").windows():
                try:
                    if not w.is_visible():
                        continue
                    title = (w.window_text() or "").strip()
                    if title == "KamelRec" or title.startswith("KamelRec "):
                        result.append((title, w))
                except Exception:
                    continue
        except Exception:
            pass
        return result

    def _find_stop_button_anywhere(self) -> Optional[UiNode]:
        """Find the stop button in the toolbar or main window."""
        # The toolbar is the normal place while recording
        windows = sorted(self._recorder_windows(), key=lambda item: 0 if "toolbar" in item[0].lower() else 1)
        for _title, w in windows:
            node = self._find(w, names=STOP_LABELS, control_type="Button")
            if node:
                return node
        # Do not use a broad "contains إيقاف" here: it would match "إيقاف مؤقت".
        for _title, w in windows:
            for label, rect in self._buttons(w):
                normalized = label.strip()
                if ("إيقاف" in normalized and "مؤقت" not in normalized) or normalized in STOP_LABELS:
                    return UiNode(rect)
        return None

    def _find_button_anywhere(self, labels: set[str], contains: str) -> Optional[UiNode]:
        """Find a toolbar button by exact label, then by safe prefix."""
        windows = sorted(self._recorder_windows(),
                         key=lambda item: 0 if "toolbar" in item[0].lower() else 1)
        for _title, w in windows:
            node = self._find(w, names=labels, control_type="Button")
            if node:
                return node
        for _title, w in windows:
            node = self._find(w, contains=contains, control_type="Button")
            if node:
                return node
        return None

    def pause(self) -> bool:
        """Pause the active KamelRec recording."""
        # Reveal the toolbar before trying to click it.
        for title, w in self._recorder_windows():
            if "toolbar" in title.lower():
                self._ensure_foreground(w)
                break

        btn = self._find_button_anywhere(PAUSE_LABELS, "إيقاف مؤقت")
        if not btn:
            logger.warning("KamelRec: bouton 'إيقاف مؤقت' introuvable.")
            return False
        logger.info("KamelRec: clic sur 'إيقاف مؤقت' (pause).")
        return self._click_node(btn, 0.8)

    def resume(self) -> bool:
        """Resume a paused KamelRec recording."""
        for title, w in self._recorder_windows():
            if "toolbar" in title.lower():
                self._ensure_foreground(w)
                break

        btn = self._find_button_anywhere(RESUME_LABELS, "استئناف")
        if not btn:
            logger.warning("KamelRec: bouton 'استئناف' introuvable.")
            return False
        logger.info("KamelRec: clic sur 'استئناف' (reprise).")
        return self._click_node(btn, 0.8)

    def _handle_confirm_dialog(self) -> bool:
        """Click a confirmation button in a KamelRec dialog (not the main window)."""
        try:
            for title, w in self._recorder_windows():
                if title == "KamelRec":
                    # Never click blindly inside the main window
                    continue
                node = self._find(w, names=CONFIRM_LABELS, control_type="Button")
                if node:
                    self._click_node(node, 0.8)
                    return True
        except Exception as e:
            logger.debug(f"KamelRec: erreur dialogue confirmation: {e}")
        return False

    def stop(self, timeout: int = 90) -> str:
        """Stop the recording and return the path of the produced file."""
        window = self._find_window()
        if not window:
            logger.warning("KamelRec: fenêtre introuvable pour arrêter.")
            return ""

        before = set(self._list_videos())
        log_offset = self._log_size()

        # Make sure the recording toolbar is on top before clicking it
        for title, w in self._recorder_windows():
            if "toolbar" in title.lower():
                self._ensure_foreground(w)
                break

        btn = self._find_stop_button_anywhere()
        if btn:
            self._click_node(btn, 1.0)
        else:
            logger.warning("KamelRec: bouton d'arrêt introuvable.")

        # Possible confirmation dialog(s)
        for _ in range(3):
            time.sleep(1)
            if self._handle_confirm_dialog():
                continue
            # If window disappeared or stop button turned back to start, we're done
            window = self._find_window()
            if not window:
                break
            if self._find_record_button(window):
                break

        # KamelRec finalizes (writes the mp4) after a short delay
        if self._wait_for_log("recording finalized {", log_offset, timeout):
            logger.info("KamelRec: enregistrement finalisé.")

        logger.info("KamelRec: attente de la sauvegarde du fichier...")
        new_file = self._wait_for_new_file(before, timeout=30)
        if new_file:
            logger.info(f"KamelRec: fichier enregistré: {new_file}")
            return new_file
        logger.warning("KamelRec: aucun nouveau fichier détecté.")
        return ""

    def _list_videos(self) -> list[str]:
        try:
            out = self._output_dir or self._settings_output_dir()
            return [str(p) for p in out.glob("*.mp4")]
        except Exception:
            return []

    def _wait_for_new_file(self, before: set[str], timeout: int = 90) -> str:
        out = self._output_dir or self._settings_output_dir()
        deadline = time.time() + timeout
        last_size = -1
        stable_since = None
        while time.time() < deadline:
            time.sleep(1)
            try:
                files = [p for p in out.glob("*.mp4") if str(p) not in before]
            except Exception:
                files = []
            if files:
                newest = max(files, key=lambda p: p.stat().st_mtime)
                size = newest.stat().st_size
                if size == last_size and size > 0:
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= 2:
                        return str(newest)
                else:
                    stable_since = None
                last_size = size
        # Fallback: newest file newer than the stop moment
        try:
            all_files = sorted(out.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
            if all_files and str(all_files[0]) not in before:
                return str(all_files[0])
        except Exception:
            pass
        return ""


# ----------------------------------------------------------------------
# Small subprocess helpers (kept separate for easy mocking)
# ----------------------------------------------------------------------
def subprocess_popen(args):
    import subprocess
    return subprocess.Popen(args)


def subprocess_kill():
    import subprocess
    for name in ("KamelRec.exe", "KamelRec-1.0.0-Portable.exe"):
        try:
            subprocess.run(["taskkill", "/F", "/IM", name],
                           capture_output=True, timeout=10)
        except Exception:
            pass
