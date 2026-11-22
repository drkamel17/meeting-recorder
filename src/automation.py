"""
Zoom automation engine - Complete implementation based on testing.
Handles: Launch → Join → Passcode → Meeting → Record
"""
import time
import os
import subprocess
import logging
import threading
from datetime import datetime
from pathlib import Path
from pywinauto import Desktop
from pyautogui import click, typewrite, hotkey, moveRel, moveTo

from src.config import (
    ZOOM_EXE_CANDIDATES, STATE_WAITING_ROOM, STATE_IN_MEETING,
    STATE_RECORDING, STATE_COMPLETED, STATE_ERROR, STATE_PAUSED
)
from src.debug_logger import take_zoom_window_screenshot, dump_zoom_ui_tree
from src.settings import (
    get_recording_engine,
    get_close_popups_during_recording,
    ENGINE_KAMELREC,
    ENGINE_WINDOWS_GAMEBAR,
)

logger = logging.getLogger("ZoomAutoRecorder")

# Zoom window classes discovered during testing
ZOOM_MAIN_CLASS = "ZPPTEnhanceLoginMainWindowClassEx"
ZOOM_JOIN_CLASS = "zWaitingMeetingIDWndClass"
ZOOM_MEETING_CLASS = "ConfMultiTabContentWndClass"
ZOOM_VIDEO_PREVIEW_CLASS = "VideoPreviewWndClass"
ZOOM_SETTINGS_CLASS = "ZPSettingWndClass"
ZOOM_JOIN_AUDIO_CLASS = "zJoinAudioWndClass"

# ── Google Meet : libelles des boutons (arabe / francais / anglais) ──
MEET_JOIN_TEXTS = (
    "انضم الآن", "انضم الان", "الانضمام الآن", "انضمام الان",
    "الانضمام هنا أيضًا", "التبديل إلى هنا",
    # Francais : page de pre-jointure et page d'accueil
    "Participer à la réunion", "Participer a la reunion",
    "Rejoindre maintenant", "Rejoindre la réunion", "Rejoindre la reunion",
    "Participer maintenant", "Demander à participer",
    "Rejoindre ici aussi", "Basculer ici",
    # Anglais
    "Join now", "Join a meeting", "Join the meeting", "Ask to join",
    "Join here too", "Switch here",
)
MEET_LEAVE_TEXTS = (
    "مغادرة المكالمة", "مغادرة", "Quitter l'appel", "Quitter l’appel",
    "Quitter", "Leave call", "Leave",
)
MEET_POPUP_TEXTS = (
    "ليس الآن", "Pas maintenant", "Not now", "Plus tard", "Later",
)
# Invite "Voulez-vous que les participants vous entendent ?" (micro coupe)
MEET_MIC_PROMPT_TEXTS = (
    "استخدام الميكروفون", "Utiliser le microphone", "Use microphone",
)
MEET_CLOSE_DIALOG_TEXTS = (
    "إغلاق مربّع الحوار", "Fermer la boîte de dialogue",
    "Fermer le dialogue", "Close dialog",
)
MEET_MIC_OFF_TEXTS = (
    "إيقاف الميكروفون", "كتم الميكروفون",
    "Couper le microphone", "Désactiver le microphone",
    "Mettre le micro en sourdine", "Turn off microphone", "Mute microphone",
)
MEET_MIC_ON_TEXTS = (
    "تشغيل الميكروفون",
    "Activer le microphone", "Activer le micro",
    "Turn on microphone", "Unmute microphone",
)
MEET_CAM_OFF_TEXTS = (
    "إيقاف الكاميرا", "Couper la caméra", "Désactiver la caméra",
    "Turn off camera",
)
MEET_CAM_ON_TEXTS = (
    "تشغيل الكاميرا", "Activer la caméra", "Turn on camera",
)

# Caracteres invisibles (marques RTL/LTR, ZWJ...) ajoutes par Meet aux libelles
_MEET_INVISIBLE = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u200e\u200f\u202a\u202b\u202c\u202d\u202e\ufeff"),
    None,
)
# Variantes de lettres arabes (alef, ya, ta marbuta) et diacritiques
_MEET_AR = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ة": "ه",
    "ً": "", "ٌ": "", "ٍ": "", "َ": "", "ُ": "", "ِ": "", "ّ": "", "ْ": "",
})


def _meet_norm(text: str) -> str:
    """Normalise un libelle Meet pour une comparaison fiable."""
    cleaned = (text or "").translate(_MEET_INVISIBLE).translate(_MEET_AR)
    return " ".join(cleaned.split()).lower()


class ZoomAutomation:
    def __init__(self):
        self.desktop = Desktop(backend="uia")
        self.recording_state = "stopped"
        self.recording_start_time = None
        self.screen_recorder = None
        self.screen_recording_path = None
        self._zoom_path = None
        self.kamelrec = None
        self._kamelrec_active = False
        self._windows_gamebar_active = False
        self._windows_gamebar_started_at = None
        self._audio_joined = False
        self._recorded_seconds = 0.0
        self._recording_original_start = None
        self._current_meeting_window = None
        self._current_platform = "zoom"
        self._browser_fallback = None
        # Surveillance des fenetres qui surgissent pendant l'enregistrement
        self._popup_guard_active = False
        self._popup_guard_stop = threading.Event()
        self._popup_guard_thread = None
        self._popup_guard_known = set()
        self._guard_meeting_proc = ""
        self._guard_tick_count = 0

    # ═══════════════════════════════════════════════════════════════
    # ZOOM DISCOVERY & LAUNCH
    # ═══════════════════════════════════════════════════════════════

    def find_zoom_path(self) -> str | None:
        """Find Zoom executable path."""
        if self._zoom_path:
            return self._zoom_path

        # Try the dynamically resolved candidates from config.py.
        for path in ZOOM_EXE_CANDIDATES:
            if path and os.path.exists(path):
                self._zoom_path = path
                return path

        # Try registry
        try:
            import winreg
            for key_path in [
                r"SOFTWARE\ZoomAudioVideo",
                r"SOFTWARE\WOW6432Node\ZoomAudioVideo",
            ]:
                try:
                    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ)
                    install_dir = winreg.QueryValueEx(key, "Install Location")[0]
                    winreg.CloseKey(key)
                    exe = os.path.join(install_dir, "Zoom.exe")
                    if os.path.exists(exe):
                        self._zoom_path = exe
                        return exe
                except FileNotFoundError:
                    continue
        except Exception as e:
            logger.debug(f"Registry search failed: {e}")

        return None

    def is_zoom_running(self) -> bool:
        """Check if Zoom is running."""
        try:
            self._get_zoom_window()
            return True
        except Exception:
            return False

    def launch_zoom(self) -> bool:
        """Launch Zoom and wait for main window."""
        path = self.find_zoom_path()
        if not path:
            logger.error("Zoom executable introuvable.")
            return False

        logger.info("Lancement de Zoom...")
        subprocess.Popen([path])

        # Wait for Zoom to initialize
        for _ in range(20):
            time.sleep(1)
            if self.is_zoom_running():
                logger.info("Zoom est maintenant ouvert.")
                take_zoom_window_screenshot("Zoom_Main")
                return True

        logger.error("Timeout : Zoom n'a pas répondu.")
        return False

    # ═══════════════════════════════════════════════════════════════
    # WINDOW DETECTION
    # ═══════════════════════════════════════════════════════════════

    def _get_zoom_window(self):
        """Get the main Zoom window."""
        windows = self.desktop.windows()
        for w in windows:
            if w.is_visible():
                try:
                    cls = w.class_name()
                    title = w.window_text()
                    if cls == ZOOM_MAIN_CLASS or title == "Zoom Workplace":
                        return w
                except Exception:
                    continue
        raise RuntimeError("Fenêtre Zoom principale non trouvée")

    def _get_join_dialog(self) -> object | None:
        """Get the join meeting dialog."""
        for w in self.desktop.windows():
            if w.is_visible():
                try:
                    if w.class_name() == ZOOM_JOIN_CLASS:
                        return w
                except Exception:
                    continue
        return None

    def _get_meeting_window(self) -> object | None:
        """Get the active meeting window."""
        # Fenêtre stockée (Google Meet / navigateur)
        if self._current_meeting_window is not None:
            try:
                if self._current_meeting_window.is_visible():
                    return self._current_meeting_window
            except Exception:
                self._current_meeting_window = None
        # Fenêtre Zoom (classe connue)
        for w in self.desktop.windows():
            if w.is_visible():
                try:
                    cls = w.class_name()
                    if cls in (ZOOM_MEETING_CLASS, ZOOM_VIDEO_PREVIEW_CLASS):
                        return w
                except Exception:
                    continue
        return None

    def _set_meeting_window(self, window, platform: str = "zoom"):
        """Stocke la fenêtre de réunion courante (Zoom ou navigateur)."""
        self._current_meeting_window = window
        self._current_platform = platform

    # ═══════════════════════════════════════════════════════════════
    # ZOOM AUDIO OUTPUT (must follow the Windows default device so the
    # recorder's system-audio loopback captures the meeting sound)
    # ═══════════════════════════════════════════════════════════════

    def _find_settings_window(self, timeout: float = 15.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for w in self.desktop.windows():
                try:
                    if w.is_visible() and w.class_name() == ZOOM_SETTINGS_CLASS:
                        return w
                except Exception:
                    continue
            time.sleep(0.5)
        return None

    @staticmethod
    def _find_control(window, *, control_type=None, name=None, name_prefix=None,
                      name_contains=None, automation_id=None):
        """Return the element_info of the first matching descendant."""
        if isinstance(name_prefix, str):
            prefixes = (name_prefix,)
        else:
            prefixes = tuple(name_prefix or ())
        prefixes = tuple(p.lower() for p in prefixes)
        contains = (name_contains or "").lower()
        try:
            descendants = window.descendants()
        except Exception:
            return None
        for node in descendants:
            try:
                info = node.element_info
                ctype = info.control_type
                label = (info.name or "").strip()
                if control_type and ctype != control_type:
                    continue
                if automation_id and (info.automation_id or "") != automation_id:
                    continue
                if name and label != name:
                    continue
                if prefixes and not any(label.lower().startswith(p) for p in prefixes):
                    continue
                if contains and contains not in label.lower():
                    continue
                rect = info.rectangle
                if rect.right <= rect.left or rect.bottom <= rect.top:
                    continue
                return info
            except Exception:
                continue
        return None

    @classmethod
    def _find_control_retry(cls, window, timeout: float = 10.0, **kwargs):
        """Search for a control, retrying while Zoom builds its UI."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = cls._find_control(window, **kwargs)
            if found:
                return found
            time.sleep(0.5)
        return None

    @staticmethod
    def _click_info(info, pause: float = 1.0) -> None:
        r = info.rectangle
        click((r.left + r.right) // 2, (r.top + r.bottom) // 2)
        time.sleep(pause)

    def _close_settings_window(self, settings) -> None:
        try:
            btn = self._find_control(settings, control_type="Button",
                                     name_prefix=("Fermer", "Close"))
            if btn:
                self._click_info(btn, 0.5)
                return
        except Exception:
            pass
        try:
            settings.set_focus()
            hotkey('alt', 'f4')
            time.sleep(0.5)
        except Exception:
            pass

    def ensure_zoom_system_speaker(self) -> bool:
        """Force Zoom's speaker to "Identique au système" (Windows default).

        This guarantees that the meeting sound is played on the same device as
        the system, which is the device captured by the recorder's loopback.
        """
        try:
            zoom_win = self._get_zoom_window()
            if not zoom_win:
                logger.warning("Zoom: fenêtre principale introuvable pour les réglages audio.")
                return False

            settings = self._find_settings_window(timeout=1)
            if not settings:
                gear = self._find_control_retry(
                    zoom_win, timeout=10, control_type="Button",
                    name_prefix=("Paramètres", "Settings"))
                if not gear:
                    gear = self._find_control_retry(
                        zoom_win, timeout=5, automation_id="SettingsButton")
                if not gear:
                    logger.warning("Zoom: bouton Paramètres introuvable.")
                    return False
                logger.info("Zoom: ouverture des paramètres audio...")
                self._click_info(gear, 1.5)
                settings = self._find_settings_window(timeout=15)
                if not settings:
                    logger.warning("Zoom: fenêtre des paramètres non ouverte.")
                    return False

            # Open the "Audio" category (the settings tree can take a while)
            audio_item = self._find_control_retry(
                settings, timeout=15, control_type="ListItem",
                name_prefix=("Audio",))
            if not audio_item:
                logger.warning("Zoom: catégorie Audio introuvable.")
                self._close_settings_window(settings)
                return False
            self._click_info(audio_item, 1.5)

            # Speaker combo
            combo = self._find_control_retry(
                settings, timeout=10, control_type="ComboBox",
                name_prefix=("Haut-parleur", "Speaker"))

            if not combo:
                logger.warning("Zoom: réglage 'Haut-parleur' introuvable.")
                self._close_settings_window(settings)
                return False

            combo_name = (combo.name or "")
            logger.info(f"Zoom: haut-parleur actuel → {combo_name}")
            if ("identique au système" in combo_name.lower()
                    or "same as system" in combo_name.lower()):
                logger.info("✅ Zoom: haut-parleur déjà réglé sur 'Identique au système'.")
                self._close_settings_window(settings)
                return True

            # Open the dropdown and choose "Identique au système"
            self._click_info(combo, 1.5)
            target = None
            deadline = time.time() + 6
            while time.time() < deadline and not target:
                for w in self.desktop.windows():
                    try:
                        if not w.is_visible():
                            continue
                    except Exception:
                        continue
                    target = self._find_control(
                        w, control_type="ListItem",
                        name_prefix=("Identique au système", "Same as system"),
                    )
                    if target:
                        break
                if not target:
                    time.sleep(0.5)

            if target:
                self._click_info(target, 1.5)
                logger.info("✅ Zoom: haut-parleur réglé sur 'Identique au système'.")
                self._close_settings_window(settings)
                return True

            logger.warning("Zoom: option 'Identique au système' introuvable dans la liste.")
            try:
                hotkey('escape')
            except Exception:
                pass
            self._close_settings_window(settings)
            return False

        except Exception as e:
            logger.error(f"Zoom: erreur réglage haut-parleur: {e}")
            return False

    @staticmethod
    def _reveal_meeting_toolbar(meeting_win) -> None:
        """Zoom auto-hides its meeting toolbar; wiggle the mouse to show it."""
        try:
            r = meeting_win.rectangle()
            cx = (r.left + r.right) // 2
            by = min(r.bottom - 25, 1040)
            for dx in (0, 40, -40, 0):
                moveTo(cx + dx, by)
                time.sleep(0.2)
            time.sleep(0.7)
        except Exception:
            pass

    def ensure_computer_audio(self, timeout: float = 6.0) -> bool:
        """Join the meeting audio through the computer before recording.

        Clicks the meeting toolbar's "Participer à l’audio" button, then the
        "Participer par l’audio de l’ordinateur" option in the popup. Without
        this step Zoom may stay silent for the participant and the recording
        would contain no meeting sound.
        """
        try:
            meeting_win = self._get_meeting_window()
            if not meeting_win:
                logger.warning("Audio réunion : fenêtre de réunion introuvable.")
                return False

            # Zoom hides the toolbar when idle: reveal it before searching.
            self._reveal_meeting_toolbar(meeting_win)

            # If the join-audio button is gone, audio is already connected.
            audio_btn = self._find_control_retry(
                meeting_win, timeout=timeout, control_type="Button",
                name_prefix=("Participer à l’audio", "Participer à l'audio",
                             "Join audio", "Join Audio"))

            if not audio_btn:
                # Confirm by looking for the mute/unmute button instead.
                muted = self._find_control(
                    meeting_win, control_type="Button",
                    name_prefix=("Couper le son", "Réactiver le son",
                                 "Rétablir le son", "Mute", "Unmute"))
                if muted:
                    logger.info("✅ Audio réunion : déjà connecté (audio de l'ordinateur).")
                    return True
                # The toolbar may have auto-hidden during the search: retry once.
                self._reveal_meeting_toolbar(meeting_win)
                audio_btn = self._find_control(
                    meeting_win, control_type="Button",
                    name_prefix=("Participer à l’audio", "Participer à l'audio",
                                 "Join audio", "Join Audio"))
                if not audio_btn:
                    logger.info("Audio réunion : état déjà connecté (ou barre masquée).")
                    return True

            logger.info("Audio réunion : clic sur 'Participer à l’audio'...")
            self._click_info(audio_btn, 1.5)

            # Find the "computer audio" option in the popup window.
            computer_btn = None
            deadline = time.time() + 10
            while time.time() < deadline and not computer_btn:
                for w in self.desktop.windows():
                    try:
                        if not w.is_visible():
                            continue
                    except Exception:
                        continue
                    computer_btn = self._find_control(
                        w, control_type="Button",
                        name_prefix=("Participer par l’audio de l’ordinateur",
                                     "Participer par l'audio de l'ordinateur",
                                     "Join with Computer Audio",
                                     "Join with computer audio",
                                     "Join audio by computer"))
                    if computer_btn:
                        break
                if not computer_btn:
                    time.sleep(0.5)

            if not computer_btn:
                logger.warning("Audio réunion : option 'Participer par l’audio de l’ordinateur' introuvable.")
                try:
                    hotkey('escape')
                except Exception:
                    pass
                return False

            logger.info("Audio réunion : choix 'Participer par l’audio de l’ordinateur'...")
            self._click_info(computer_btn, 2.0)

            # Confirm: the mute/unmute button should now be present.
            self._reveal_meeting_toolbar(meeting_win)
            muted = self._find_control_retry(
                meeting_win, timeout=6, control_type="Button",
                name_prefix=("Couper le son", "Réactiver le son",
                             "Rétablir le son", "Mute", "Unmute"))
            still_there = self._find_control(
                meeting_win, control_type="Button",
                name_prefix=("Participer à l’audio", "Participer à l'audio",
                             "Join audio", "Join Audio"))

            if muted or not still_there:
                logger.info("✅ Audio réunion : connecté via l'audio de l'ordinateur.")
                return True

            logger.warning("Audio réunion : connexion audio non confirmée.")
            return False

        except Exception as e:
            logger.error(f"Audio réunion : erreur connexion audio: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # ==================================================================
    # GOOGLE MEET (via navigateur Chrome/Edge)
    # ==================================================================

    def _find_browser_path(self) -> str | None:
        candidates = []
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local_appdata = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        candidates += [
            os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(local_appdata, "Google", "Chrome", "Application", "chrome.exe"),
            os.environ.get("ZOOM_AUTORECORDER_CHROME_EXE", ""),
            os.path.join(program_files, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(program_files_x86, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(local_appdata, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.environ.get("ZOOM_AUTORECORDER_EDGE_EXE", ""),
        ]
        for path in candidates:
            if path and os.path.exists(path):
                return path
        return None

    def launch_google_meet(self, meet_link: str) -> bool:
        try:
            self._browser_fallback = None
            browser = self._find_browser_path()
            if not browser:
                logger.error("Navigateur Chrome/Edge introuvable pour Google Meet.")
                return False
            logger.info(f"Lancement navigateur vers : {meet_link}")
            # --new-window evite de detourner une fenetre Chrome deja ouverte
            subprocess.Popen([browser, "--new-window", meet_link], shell=False)
            time.sleep(4)

            # Attendre la fenetre dont le titre correspond a Meet (le titre
            # peut etre generique pendant le chargement).
            win = None
            deadline = time.time() + 25
            while time.time() < deadline:
                candidate = self._get_browser_window()
                if candidate is not None:
                    win = candidate
                    if "meet" in (win.window_text() or "").lower():
                        break
                time.sleep(1)

            if win is None:
                logger.warning("Fenetre navigateur non detectee apres lancement.")
                return False

            self._prepare_meet_window(win)
            self._set_meeting_window(win, platform="google_meet")
            logger.info("Navigateur Google Meet ouvert.")
            return True
        except Exception as e:
            logger.error(f"Erreur lancement Google Meet: {e}")
            return False

    # ── Geometrie ecran / plein ecran du navigateur ────────────────
    @staticmethod
    def _monitor_rect(hwnd=None) -> tuple[int, int, int, int] | None:
        """Bornes du moniteur (ecran principal par defaut) en pixels ecran."""
        try:
            import ctypes
            from ctypes import wintypes

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                            ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

            user32 = ctypes.windll.user32
            if hwnd:
                hmon = user32.MonitorFromWindow(hwnd, 2)   # NEAREST
            else:
                class POINT(ctypes.Structure):
                    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
                hmon = user32.MonitorFromPoint(POINT(0, 0), 1)  # PRIMARY
            if not hmon:
                return None
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                return None
            r = mi.rcMonitor
            return (r.left, r.top, r.right, r.bottom)
        except Exception:
            return None

    def _meet_is_fullscreen(self, win) -> bool:
        """Vrai si la fenetre couvre entierement son moniteur (F11)."""
        try:
            r = win.rectangle()
            bounds = self._monitor_rect(win.handle)
            if bounds is None:
                return False
            left, top, right, bottom = bounds
            return (
                r.left <= left + 2 and r.top <= top + 2
                and r.right >= right - 2 and r.bottom >= bottom - 2
            )
        except Exception:
            return False

    def _meet_enter_fullscreen(self, win) -> bool:
        """Force le navigateur en plein ecran (F11) et verifie le resultat.

        Le plein ecran evite que la barre des taches, les onglets ou une
        fenetre non maximisee apparaissent dans l'enregistrement.
        """
        if self._meet_is_fullscreen(win):
            return True
        for attempt in range(3):
            try:
                win.set_focus()
            except Exception:
                pass
            time.sleep(0.4)
            try:
                hotkey("f11")
            except Exception as e:
                logger.debug(f"Meet: envoi de F11 impossible: {e}")
                break
            time.sleep(1.5)
            if self._meet_is_fullscreen(win):
                logger.info("Google Meet: navigateur en plein ecran.")
                return True
        logger.warning("Google Meet: le navigateur n'a pas pu passer en plein ecran.")
        return False

    def keep_meeting_fullscreen(self) -> bool:
        """Maintient le navigateur en plein ecran pendant l'enregistrement.

        Appele periodiquement : Chrome peut sortir du plein ecran (invite de
        permission, Esc, changement de fenetre). On le remet en plein ecran
        uniquement si necessaire, pour eviter toute barre des taches dans la
        video.
        """
        if getattr(self, "_current_platform", "") != "google_meet":
            return False
        win = self._current_meeting_window
        if win is None:
            return False
        try:
            if not win.is_visible():
                return False
        except Exception:
            return False
        if self._meet_is_fullscreen(win):
            return True
        logger.info("Google Meet: navigateur sorti du plein ecran, restauration...")
        return self._meet_enter_fullscreen(win)

    def _prepare_meet_window(self, win) -> None:
        """Place la fenetre du navigateur sur l'ecran principal, maximise puis
        passe en plein ecran (F11).

        Indispensable quand Chrome s'ouvre sur un second ecran (coordonnees
        negatives) : l'enregistreur et KamelRec capturent l'ecran principal.
        """
        try:
            win.restore()
            time.sleep(0.3)
        except Exception:
            pass
        try:
            # UIAWrapper n'expose pas move_window : on passe par l'API Windows.
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = win.handle
            bounds = self._monitor_rect() or (0, 0, 1920, 1080)
            left, top, right, bottom = bounds
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE
            time.sleep(0.3)
            # Deplacer sur l'ecran principal, a la taille du moniteur
            user32.SetWindowPos(
                hwnd, 0, left, top, right - left, bottom - top,
                0x0004 | 0x0040,                # NOZORDER | SHOWWINDOW
            )
            time.sleep(0.4)
            user32.ShowWindow(hwnd, 3)          # SW_MAXIMIZE
            time.sleep(0.4)
        except Exception as e:
            logger.debug(f"Meet: deplacement de la fenetre impossible: {e}")
        try:
            win.set_focus()
        except Exception:
            pass
        time.sleep(0.3)
        # Plein ecran navigateur : aucun onglet/barre d'adresse/barre des taches
        self._meet_enter_fullscreen(win)

    def _get_browser_window(self) -> object | None:
        """Retourne la premiere fenetre Chrome/Edge visible."""
        for w in self.desktop.windows():
            if not w.is_visible():
                continue
            try:
                cls = w.class_name() or ""
                title = w.window_text() or ""
                # Chrome et Edge utilisent tous deux Chrome_WidgetWin_1
                if cls == "Chrome_WidgetWin_1":
                    # Preferer une fenetre dont le titre mentionne Meet, sinon prendre la premiere Chrome
                    title_low = title.lower()
                    if any(k in title_low for k in ("meet.google.com", "google meet", "meet -")):
                        return w
                    # Si aucune fenetre MeetSpecifique, retourner la premiere Chrome visible
                    if not hasattr(self, '_browser_fallback') or self._browser_fallback is None:
                        self._browser_fallback = w
            except Exception:
                continue
        # Retourner le fallback si aucune fenetre MeetSpecifique n'a ete trouvee
        if getattr(self, '_browser_fallback', None) is not None:
            fb = self._browser_fallback
            self._browser_fallback = None
            return fb
        return None

    # ── Google Meet : detection des vrais boutons ──────────────────
    def _meet_buttons(self, window):
        """Retourne [(nom_normalise, nom, ctrl)] des controles de type Button.

        On ignore volontairement les Text : le titre "مستعد للانضمام؟"
        contient le mot "انضمام" et etait clique a la place du bouton.
        """
        result = []
        try:
            descendants = window.descendants()
        except Exception as e:
            logger.debug(f"Meet: enumeration UIA impossible: {e}")
            return result
        for ctrl in descendants:
            try:
                info = ctrl.element_info
                if info.control_type != "Button":
                    continue
                name = (info.name or "").strip()
                if not name:
                    try:
                        name = (ctrl.window_text() or "").strip()
                    except Exception:
                        name = ""
                if not name:
                    continue
                result.append((_meet_norm(name), name, ctrl))
            except Exception:
                continue
        return result

    @staticmethod
    def _meet_pick(buttons, texts, allow_class=False):
        """Choisit un bouton : nom exact d'abord, puis prefixe."""
        wanted = [_meet_norm(t) for t in texts]
        wanted = [w for w in wanted if w]
        for norm, orig, ctrl in buttons:
            if norm in wanted:
                return ctrl, orig
        for norm, orig, ctrl in buttons:
            for w in wanted:
                if norm.startswith(w):
                    return ctrl, orig
        if allow_class:
            for norm, orig, ctrl in buttons:
                try:
                    if "UywwFc-RLmnJb" in (ctrl.element_info.class_name or ""):
                        return ctrl, orig or "UywwFc-RLmnJb"
                except Exception:
                    continue
        return None, None

    def _meet_find(self, window, texts, allow_class=False):
        return self._meet_pick(self._meet_buttons(window), texts, allow_class)

    def _meet_click(self, window, ctrl, label: str) -> bool:
        """Clique au centre reel d'un controle (fonctionne aussi en multi-ecran)."""
        try:
            rect = ctrl.rectangle()
        except Exception:
            return False
        if rect.width() <= 0 or rect.height() <= 0:
            return False
        cx = rect.left + rect.width() // 2
        cy = rect.top + rect.height() // 2
        try:
            window.set_focus()
            time.sleep(0.2)
        except Exception:
            pass
        try:
            moveTo(cx, cy, duration=0.2)
            time.sleep(0.1)
            click(cx, cy)
        except Exception as e:
            logger.warning(f"Meet: clic impossible sur '{label}' ({cx},{cy}): {e}")
            return False
        logger.info(f"Meet: clic sur '{label}' (x={cx}, y={cy}).")
        return True

    def _meet_is_in_call(self, window) -> bool:
        """Vrai si la page Meet affiche les controles d'une reunion en cours."""
        ctrl, _ = self._meet_find(window, MEET_LEAVE_TEXTS)
        return ctrl is not None

    def _meet_wait_ready(self, window, timeout: float = 60.0):
        """Attend la page de pre-jointure. Retourne 'joined', 'ready' ou None."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._meet_is_in_call(window):
                return "joined"
            ctrl, _ = self._meet_find(window, MEET_JOIN_TEXTS, allow_class=True)
            if ctrl is not None:
                return "ready"
            time.sleep(1.0)
        return None

    def _meet_wait_in_call(self, window, timeout: float = 15.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._meet_is_in_call(window):
                return True
            time.sleep(1.0)
        return False

    def _meet_dismiss_popups(self, window) -> None:
        """Ferme l'invite 'Recevoir des notifications' qui peut gener le clic."""
        for _ in range(2):
            ctrl, name = self._meet_find(window, MEET_POPUP_TEXTS)
            if ctrl is None:
                return
            self._meet_click(window, ctrl, name)
            time.sleep(0.6)

    def _meet_dismiss_mic_prompt(self, window) -> bool:
        """Ferme l'invite 'Voulez-vous que les participants vous entendent ?'.

        Meet l'affiche quand on rejoint avec le micro coupe. Les deux boutons
        proposes activent le micro/camera : on ferme donc la boite de dialogue
        pour rester muet et sans camera.
        """
        ctrl, _name = self._meet_find(window, MEET_MIC_PROMPT_TEXTS)
        if ctrl is None:
            return False
        logger.info("Google Meet: fermeture de l'invite micro (reste muet).")
        close, close_name = self._meet_find(window, MEET_CLOSE_DIALOG_TEXTS)
        if close is not None:
            self._meet_click(window, close, close_name)
        else:
            try:
                window.set_focus()
                hotkey("esc")
            except Exception:
                pass
        time.sleep(1)
        return True

    def _meet_media_off(self, window, label, off_texts, on_texts) -> None:
        """Desactive micro/camera sans jamais les activer par erreur.

        Le libelle du bouton decrit l'action : 'Couper le microphone' signifie
        que le micro est actuellement ACTIF. S'il affiche 'Activer le
        microphone', il est deja coupe et on ne touche a rien.
        """
        ctrl, name = self._meet_find(window, off_texts)
        if ctrl is not None:
            if self._meet_click(window, ctrl, name):
                logger.info(f"Meet: {label} desactive ('{name}').")
                time.sleep(0.7)
            return
        ctrl, name = self._meet_find(window, on_texts)
        if ctrl is not None:
            logger.info(f"Meet: {label} deja desactive ('{name}').")
            return
        logger.info(f"Meet: etat {label} indetermine (aucune bascule pour eviter de l'activer).")

    def _meet_disable_media(self, window) -> None:
        """Force micro et camera sur 'desactive' avant de rejoindre."""
        self._meet_media_off(window, "micro", MEET_MIC_OFF_TEXTS, MEET_MIC_ON_TEXTS)
        self._meet_media_off(window, "camera", MEET_CAM_OFF_TEXTS, MEET_CAM_ON_TEXTS)

    def join_google_meet(
        self,
        meet_link: str,
        display_name: str = "",
        mic_on_start: bool = False,
        camera_on_start: bool = False,
    ) -> str:
        """Rejoint une reunion Google Meet et VERIFIE que la jointure a eu lieu.

        Le micro et la camera sont toujours desactives avant le clic, quelles
        que soient les options demandees.
        """
        try:
            win = self._current_meeting_window
            if win is None:
                win = self._get_browser_window()
            if win is None:
                logger.error("Fenetre navigateur Google Meet introuvable.")
                return STATE_ERROR
            self._set_meeting_window(win, platform="google_meet")
            try:
                win.set_focus()
            except Exception:
                pass
            # Toujours en plein ecran (aucune barre des taches/onglet enregistree)
            self._meet_enter_fullscreen(win)

            # 1. Attendre que la page de pre-jointure soit prete
            logger.info("Google Meet: attente de la page de pre-jointure...")
            state = self._meet_wait_ready(win, timeout=60)
            if state == "joined":
                logger.info("Google Meet: deja present dans la reunion.")
                self._meet_disable_media(win)
                self._meet_enter_fullscreen(win)
                return STATE_IN_MEETING
            if state is None:
                logger.error("Google Meet: bouton 'Rejoindre' introuvable apres 60 s.")
                return STATE_ERROR

            # 2. Fermer les invites qui pourraient intercepter le clic
            self._meet_dismiss_popups(win)

            # 3. Micro et camera TOUJOURS desactives avant de rejoindre
            self._meet_disable_media(win)

            # 4. Cliquer sur Rejoindre, puis verifier la jointure reelle
            for attempt in range(1, 4):
                ctrl, name = self._meet_find(win, MEET_JOIN_TEXTS, allow_class=True)
                if ctrl is None:
                    logger.warning("Google Meet: bouton 'Rejoindre' absent (tentative %s/3).", attempt)
                    time.sleep(2)
                    continue
                self._meet_click(win, ctrl, name)
                # Meet affiche "Voulez-vous que les participants vous
                # entendent ?" quand on rejoint micro coupe : on ferme cette
                # invite pour rester muet, puis on attend la vraie reunion.
                deadline = time.time() + 20
                while time.time() < deadline:
                    if self._meet_is_in_call(win):
                        break
                    if not self._meet_dismiss_mic_prompt(win):
                        time.sleep(1)
                if self._meet_wait_in_call(win, timeout=10):
                    logger.info("Reunion Google Meet jointe (verifiee).")
                    self._meet_disable_media(win)
                    self._meet_enter_fullscreen(win)
                    return STATE_IN_MEETING
                logger.warning("Google Meet: toujours pas dans la reunion apres le clic (%s/3).", attempt)
                time.sleep(2)
                self._meet_dismiss_popups(win)
                self._meet_disable_media(win)

            logger.error("Google Meet: impossible de rejoindre la reunion.")
            return STATE_ERROR
        except Exception as e:
            logger.error(f"Erreur jointure Google Meet: {e}")
            return STATE_ERROR

    # JOIN MEETING FLOW
    # ═══════════════════════════════════════════════════════════════

    def join_meeting(
        self,
        meeting_id: str,
        passcode: str = "",
        wait_for_join: bool = True,
        display_name: str = "",
    ) -> str:
        """
        Complete join flow:
        1. Click 'Participer à une réunion' on main window
        2. Fill Meeting ID
        3. Fill Display Name
        4. Handle checkboxes (mic, camera)
        5. Click 'Participer' in join dialog
        6. Handle preview window (click 'Participer' in preview)
        7. Handle passcode dialog if needed
        8. Handle permission dialog
        9. Handle final preview window (click 'Participer')
        10. Wait for meeting to start
        """
        self._audio_joined = False
        zoom_display_name = (display_name or "").strip() or "AutoRecorder"
        try:
            # Step 1: Get main Zoom window
            zoom_win = self._get_zoom_window()
            if not zoom_win:
                logger.error("Fenêtre Zoom principale non trouvée.")
                return STATE_ERROR

            # Step 1b: Make sure Zoom plays sound on the Windows default device
            # (same device as the system audio captured by the recorder)
            logger.info("Vérification du haut-parleur Zoom (identique au système)...")
            self.ensure_zoom_system_speaker()
            try:
                zoom_win.set_focus()
            except Exception:
                pass
            time.sleep(0.5)

            # Step 2: Click "Participer à une réunion"
            logger.info("Clic sur 'Participer à une réunion'...")
            if not self._click_button_by_text(zoom_win, "Participer à une réunion"):
                logger.error("Bouton 'Participer à une réunion' non trouvé.")
                return STATE_ERROR
            time.sleep(3)

            # Step 3: Wait for join dialog
            logger.info("Attente du dialogue de jonction...")
            join_win = self._wait_for_window(ZOOM_JOIN_CLASS, timeout=15)
            if not join_win:
                logger.error("Dialogue de jonction non trouvé.")
                return STATE_ERROR
            logger.info(f"Dialogue trouvé: {join_win.window_text()}")

            # Step 4: Fill Meeting ID
            logger.info(f"Remplissage Meeting ID: {meeting_id}")
            self._fill_field_by_hint(join_win, "ID de réunion", meeting_id)
            time.sleep(0.5)

            # Step 5: Fill Display Name (clear first to avoid appending)
            logger.info("Remplissage Display Name (après nettoyage)...")
            self._fill_field_by_hint(join_win, "Saisir votre nom", zoom_display_name, clear_first=True)
            time.sleep(0.5)

            # Step 6: Handle checkboxes
            self._handle_checkboxes(join_win)

            # Step 7: Click Participate in join dialog
            logger.info("Clic sur 'Participer'...")
            if not self._click_button_by_text(join_win, "Participer", retries=5):
                logger.warning("Bouton 'Participer' non trouvé, tentative fallback...")
                # Fallback: press Enter
                hotkey('enter')
                time.sleep(0.5)
                if not self._click_button_by_text(join_win, "Participer", retries=3):
                    logger.error("Bouton 'Participer' non trouvé après retries.")
                    return STATE_ERROR
            time.sleep(3)

            # Step 8: Wait for the passcode dialog or the video preview.
            # Zoom shows the passcode dialog right after "Participer", before
            # the preview. The join window itself is excluded so it is never
            # mistaken for the passcode prompt.
            logger.info("Vérification du code secret et de la fenêtre de preview...")
            join_hwnds = set()
            try:
                join_hwnds.add(join_win.handle)
            except Exception:
                pass

            self._wait_for_passcode_or_preview(passcode, join_hwnds, timeout=20)

            # Step 9: Handle preview window (appears after the passcode)
            self._click_preview_when_ready(timeout=15)

            # The passcode dialog may also appear after the preview step.
            if self._handle_passcode_dialog(passcode, join_hwnds, timeout=15):
                time.sleep(2)

            # Step 10: Handle microphone/camera permission dialog
            logger.info("Vérification des permissions audio/vidéo...")
            time.sleep(2)
            self._handle_permission_dialog()

            # Step 11: Handle FINAL preview window (after permission, before joining)
            logger.info("Vérification de la fenêtre preview finale...")
            time.sleep(1)
            final_preview = None
            for w in self.desktop.windows():
                if w.is_visible():
                    try:
                        if w.class_name() == ZOOM_VIDEO_PREVIEW_CLASS:
                            title = w.window_text()
                            # Only handle if it's a join preview (has Participer button)
                            for ctrl in w.descendants():
                                try:
                                    if ctrl.window_text() == "Participer":
                                        final_preview = w
                                        break
                                except:
                                    continue
                            if final_preview:
                                break
                    except:
                        continue

            if final_preview:
                logger.info(f"Fenêtre preview finale détectée: {final_preview.window_text()}")
                # Click "Participer" in final preview window
                if not self._click_button_by_text(final_preview, "Participer"):
                    logger.warning("Bouton 'Participer' non trouvé dans la preview finale.")
                time.sleep(3)

            # Step 12: Wait for meeting
            if wait_for_join:
                result = self._wait_for_meeting_ready(timeout=30)
                if result == STATE_IN_MEETING:
                    # Join the meeting audio through the computer so that the
                    # meeting sound is actually played (and recorded).
                    try:
                        self._audio_joined = self.ensure_computer_audio(timeout=8)
                    except Exception as e:
                        self._audio_joined = False
                        logger.warning(f"Audio réunion : étape ignorée ({e}).")
                    # Pass to fullscreen for better recording
                    self._set_fullscreen()
                else:
                    take_zoom_window_screenshot("Join_Failed")
                    try:
                        dump_zoom_ui_tree()
                    except Exception as e:
                        logger.debug(f"Dump UI impossible: {e}")

                    # Last resort: some Zoom versions do not expose the
                    # passcode field through UI Automation. Rejoin with the
                    # zoommtg:// protocol, which carries the passcode in the URL.
                    if passcode:
                        logger.info("Nouvelle tentative via le lien Zoom avec le code secret...")
                        try:
                            hotkey('esc')
                            time.sleep(1)
                        except Exception:
                            pass
                        if self._join_via_uri(meeting_id, passcode, zoom_display_name):
                            time.sleep(6)
                            self._handle_preview_windows()
                            self._handle_permission_dialog()
                            self._handle_preview_windows()
                            result = self._wait_for_meeting_ready(timeout=40)
                            if result == STATE_IN_MEETING:
                                try:
                                    self._audio_joined = self.ensure_computer_audio(timeout=8)
                                except Exception as e:
                                    self._audio_joined = False
                                    logger.warning(f"Audio réunion : étape ignorée ({e}).")
                                self._set_fullscreen()
                return result

            return STATE_IN_MEETING

        except Exception as e:
            logger.error(f"Erreur lors de la jonction: {e}")
            take_zoom_window_screenshot("Zoom_Error")
            return STATE_ERROR

    def _wait_for_window(self, class_name: str, timeout: int = 10):
        """Wait for a window with specific class name."""
        end = time.time() + timeout
        while time.time() < end:
            win = self._get_window_by_class(class_name)
            if win:
                return win
            time.sleep(0.5)
        return None

    def _get_window_by_class(self, class_name: str):
        """Get window by class name."""
        for w in self.desktop.windows():
            if w.is_visible():
                try:
                    if w.class_name() == class_name:
                        return w
                except Exception:
                    continue
        return None

    # ═══════════════════════════════════════════════════════════════
    # PASSCODE HANDLING
    # ═══════════════════════════════════════════════════════════════

    PASSCODE_TITLE_KEYWORDS = (
        "code secret", "code d'accès", "code d’accès", "mot de passe",
        "passcode", "secret code", "code de la réunion",
    )
    PASSCODE_FIELD_HINTS = (
        "Code secret", "code secret", "Mot de passe", "mot de passe",
        "Saisir le code", "saisir le code", "Code d'accès", "code d’accès",
        "Passcode", "passcode", "Entrez le code", "code de la réunion",
    )
    PASSCODE_BUTTON_TEXTS = (
        "Participer", "Participer à la réunion", "Rejoindre", "Valider",
        "Confirmer", "OK", "Join",
    )

    def _iter_edit_controls(self, window):
        """Return the editable controls of a window (UIA-compatible helper)."""
        edits = []
        try:
            for ctrl in window.descendants():
                try:
                    control_type = ""
                    try:
                        control_type = ctrl.element_info.control_type or ""
                    except Exception:
                        pass
                    class_name = ""
                    try:
                        class_name = ctrl.class_name() or ""
                    except Exception:
                        pass
                    if control_type in ("Edit", "TextEdit") or class_name == "Edit":
                        edits.append(ctrl)
                except Exception:
                    continue
        except Exception:
            pass
        return edits

    @staticmethod
    def _control_hint(ctrl) -> str:
        """Return the visible text, name and help text of a control, lowercased."""
        texts = []
        try:
            texts.append(ctrl.window_text() or "")
        except Exception:
            pass
        for attr in ("name", "help_text", "automation_id"):
            try:
                texts.append(str(getattr(ctrl.element_info, attr, "") or ""))
            except Exception:
                pass
        return " ".join(texts).lower()

    def _find_passcode_window(self, exclude_hwnds=()):
        """Return the visible passcode prompt, ignoring the join dialog itself.

        Zoom may either open a second window or transform the join dialog in
        place, so detection is based on the content: the regular join dialog
        has at least two edit fields (meeting ID + display name), the passcode
        prompt has only one, and its title usually mentions the secret code.
        """
        for w in self.desktop.windows():
            try:
                if not w.is_visible():
                    continue

                title = (w.window_text() or "").strip()
                low = title.lower()
                if any(kw in low for kw in self.PASSCODE_TITLE_KEYWORDS):
                    return w

                try:
                    if w.class_name() != ZOOM_JOIN_CLASS:
                        continue
                except Exception:
                    continue

                edits = self._iter_edit_controls(w)
                if len(edits) >= 2:
                    # Meeting ID + display name: this is the join dialog.
                    continue

                if len(edits) == 1:
                    hint = self._control_hint(edits[0])
                    if any(k in hint for k in ("id de réunion", "meeting id", "votre nom", "your name")):
                        continue
                return w
            except Exception:
                continue
        return None

    def _find_passcode_window_by_hint(self):
        """Content-based passcode detection (handles dialogs transformed in place)."""
        markers = ("code secret", "passcode", "mot de passe", "code d'accès", "code d’accès", "secret code")
        for w in self.desktop.windows():
            try:
                if not w.is_visible():
                    continue
                try:
                    cls = w.class_name() or ""
                except Exception:
                    cls = ""
                if not (cls[:1].lower() == "z" or cls.startswith(("Conf", "Video", "ZP"))):
                    continue
                for ctrl in self._iter_edit_controls(w):
                    hint = self._control_hint(ctrl)
                    if any(marker in hint for marker in markers):
                        return w
            except Exception:
                continue
        return None

    def _fill_passcode_field(self, window, passcode: str) -> bool:
        """Fill the passcode input of the dialog."""
        edits = self._iter_edit_controls(window)
        if not edits:
            return False

        # Prefer the edit whose label, hint or automation id mentions a code.
        for ctrl in edits:
            joined = self._control_hint(ctrl)
            if any(kw in joined for kw in ("code", "pass", "mot de passe", "secret")):
                if self._type_into(ctrl, passcode):
                    return True

        # A passcode prompt contains a single field: use it directly.
        if len(edits) == 1 and self._type_into(edits[0], passcode):
            return True
        return False

    def _type_into(self, ctrl, value: str) -> bool:
        try:
            rect = ctrl.rectangle()
            cx = rect.left + rect.width() // 2
            cy = rect.top + rect.height() // 2
            click(cx, cy)
            time.sleep(0.3)
            self._clear_field(cx, cy)
            typewrite(value)
            logger.info("Code secret saisi.")
            return True
        except Exception as e:
            logger.debug(f"Erreur saisie code secret: {e}")
            return False

    def _submit_passcode(self, window) -> bool:
        """Click the validation button of the passcode dialog."""
        for text in self.PASSCODE_BUTTON_TEXTS:
            if self._click_button_by_text(window, text, retries=2):
                return True
        try:
            hotkey('enter')
            return True
        except Exception:
            return False

    def _fill_and_submit_passcode(self, window, passcode: str) -> bool:
        logger.info(f"Dialogue passcode détecté: {window.window_text()}")
        if not passcode:
            logger.warning("Passcode requis mais non fourni.")
            return False
        if not self._fill_passcode_field(window, passcode):
            logger.warning("Champ 'Code secret' introuvable dans le dialogue.")
            return False
        time.sleep(0.5)
        self._submit_passcode(window)
        time.sleep(2)
        logger.info("✅ Code secret saisi, validation demandée.")
        return True

    def _handle_passcode_dialog(self, passcode: str, exclude_hwnds=(), timeout: int = 10) -> bool:
        """Wait for the passcode dialog and submit the code. Returns True if handled."""
        if not passcode:
            return False
        deadline = time.time() + timeout
        started = time.time()
        while time.time() < deadline:
            win = self._find_passcode_window(exclude_hwnds)
            if not win and time.time() - started >= 3:
                win = self._find_passcode_window_by_hint()
            if win:
                return self._fill_and_submit_passcode(win, passcode)
            if self._get_meeting_window():
                return False
            time.sleep(0.4)
        return False

    def _wait_for_passcode_or_preview(self, passcode: str, exclude_hwnds=(), timeout: int = 20):
        """Watch the join transition, filling the passcode as soon as it appears.

        Returns "passcode", "preview", "meeting" or None.
        """
        deadline = time.time() + timeout
        last_hint_scan = 0.0
        preview_seen_at = None
        while time.time() < deadline:
            win = self._find_passcode_window(exclude_hwnds)
            if not win and time.time() - last_hint_scan >= 3:
                last_hint_scan = time.time()
                win = self._find_passcode_window_by_hint()
            if win:
                self._fill_and_submit_passcode(win, passcode)
                return "passcode"

            if self._get_meeting_window():
                return "meeting"

            if self._get_window_by_class(ZOOM_VIDEO_PREVIEW_CLASS):
                # Give the passcode dialog a few seconds to appear on top of
                # the preview before interacting with the preview.
                if preview_seen_at is None:
                    preview_seen_at = time.time()
                elif time.time() - preview_seen_at >= 4:
                    return "preview"
            else:
                preview_seen_at = None
            time.sleep(0.4)
        logger.warning("Aucun dialogue de code secret ou aperçu détecté après la jonction.")
        try:
            candidates = []
            for w in self.desktop.windows():
                try:
                    if not w.is_visible():
                        continue
                    cls = w.class_name()
                    if cls.startswith(("z", "Z", "Conf", "Video")):
                        candidates.append(f"{cls}::{(w.window_text() or '')[:60]}")
                except Exception:
                    continue
            logger.debug("Fenêtres Zoom visibles: %s", " | ".join(candidates))
        except Exception:
            pass
        return None

    def _join_via_uri(self, meeting_id: str, passcode: str, display_name: str) -> bool:
        """Fallback join using Zoom's zoommtg:// protocol (passcode included).

        This bypasses the passcode dialog entirely when Zoom's UIA tree does
        not expose the input field.
        """
        try:
            import re
            from urllib.parse import quote

            confno = re.sub(r"\D", "", str(meeting_id or ""))
            if not confno:
                logger.error("Jonction via lien: Meeting ID invalide.")
                return False

            uri = f"zoommtg://zoom.us/join?action=join&confno={confno}"
            if passcode:
                uri += f"&pwd={quote(str(passcode))}"
            if display_name:
                uri += f"&uname={quote(str(display_name))}"

            logger.info("Jonction via le lien Zoom (code secret inclus)...")
            os.startfile(uri)
            return True
        except Exception as e:
            logger.error(f"Erreur jonction via lien Zoom: {e}")
            return False

    def _click_preview_when_ready(self, timeout: int = 15) -> bool:
        """Wait for a Zoom preview window and click its 'Participer' button."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            preview_win = self._get_window_by_class(ZOOM_VIDEO_PREVIEW_CLASS)
            if preview_win:
                logger.info(f"Fenêtre preview détectée: {preview_win.window_text()}")
                if not self._click_button_by_text(preview_win, "Participer"):
                    logger.warning("Bouton 'Participer' non trouvé dans la preview.")
                    return False
                time.sleep(2)
                return True
            if self._get_meeting_window():
                return False
            time.sleep(0.5)
        return False

    def _handle_preview_windows(self) -> None:
        """Click 'Participer' in any visible Zoom preview window."""
        try:
            preview_win = self._get_window_by_class(ZOOM_VIDEO_PREVIEW_CLASS)
            if preview_win:
                logger.info(f"Fenêtre preview détectée: {preview_win.window_text()}")
                self._click_button_by_text(preview_win, "Participer")
                time.sleep(3)
        except Exception as e:
            logger.debug(f"Erreur preview: {e}")

    def _handle_permission_dialog(self):
        """Handle microphone/camera permission dialog that appears after joining."""
        try:
            # Wait for permission dialog
            time.sleep(2)
            desktop = Desktop(backend="uia")

            # Look for permission-related windows
            for w in desktop.windows():
                if w.is_visible():
                    try:
                        title = w.window_text()
                        cls = w.class_name()

                        # Check if this is a permission dialog
                        if any(kw in title.lower() for kw in ["micro", "camera", "caméra", "audio", "video", "permission", "autoris", "join audio"]):
                            logger.info(f"Permission dialog detected: {title}")

                            # Look for "Oui" or "OK" or "Join" button
                            for ctrl in w.descendants():
                                try:
                                    txt = ctrl.window_text()
                                    if any(kw in txt.lower() for kw in ["oui", "ok", "join", "participer", "yes", "accept"]):
                                        rect = ctrl.rectangle()
                                        cx = rect.left + rect.width() // 2
                                        cy = rect.top + rect.height() // 2
                                        logger.info(f"Clicking '{txt}' at ({cx}, {cy})")
                                        click(cx, cy)
                                        time.sleep(2)
                                        return True
                                except Exception:
                                    continue
                    except Exception:
                        continue
        except Exception as e:
            logger.debug(f"Permission dialog handler error: {e}")

        return False

    def _wait_for_meeting_ready(self, timeout: int = 30) -> str:
        """Wait for meeting window to appear."""
        end = time.time() + timeout
        while time.time() < end:
            meeting_win = self._get_meeting_window()
            if meeting_win:
                title = meeting_win.window_text()
                # Check if meeting has ended
                if "terminée" in title.lower() or "ended" in title.lower():
                    logger.warning("La réunion s'est terminée.")
                    return STATE_COMPLETED
                logger.info(f"Réunion rejointe: {title}")
                return STATE_IN_MEETING
            time.sleep(2)
        logger.warning("Timeout en attente de la réunion.")
        return STATE_ERROR

    # ═══════════════════════════════════════════════════════════════
    # UI INTERACTION HELPERS
    # ═══════════════════════════════════════════════════════════════

    def _click_button_by_text(self, window, text: str, retries: int = 3) -> bool:
        """Find and click a button by its text with retry."""
        for attempt in range(retries):
            try:
                for ctrl in window.descendants():
                    try:
                        ctrl_text = ctrl.window_text().strip()
                        if ctrl_text == text:
                            rect = ctrl.rectangle()
                            cx = rect.left + rect.width() // 2
                            cy = rect.top + rect.height() // 2
                            click(cx, cy)
                            logger.info(f"Clic sur: {text}")
                            return True
                    except Exception:
                        continue
            except Exception as e:
                logger.debug(f"Erreur recherche bouton (tentative {attempt+1}): {e}")
            if attempt < retries - 1:
                time.sleep(0.5)
        return False

    def _fill_field_by_hint(self, window, hint: str, value: str, clear_first: bool = False):
        """Find an edit field by hint text and fill it."""
        try:
            for ctrl in window.descendants():
                try:
                    txt = ctrl.window_text()
                    if hint in txt:
                        rect = ctrl.rectangle()
                        cx = rect.left + rect.width() // 2
                        cy = rect.top + rect.height() // 2
                        click(cx, cy)
                        time.sleep(0.3)
                        # Clear existing text if requested
                        if clear_first:
                            self._clear_field(cx, cy)
                        typewrite(value)
                        logger.info(f"Rempli '{hint}': {value[:8]}...")
                        return True
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"Erreur remplissage: {e}")
        return False

    def _clear_field(self, x: int, y: int):
        """Clear a text field by selecting all and deleting."""
        try:
            click(x, y)
            time.sleep(0.2)
            hotkey('ctrl', 'a')
            time.sleep(0.2)
            hotkey('delete')
            time.sleep(0.2)
            logger.debug("Champ vidé.")
        except Exception as e:
            logger.debug(f"Erreur effacement champ: {e}")

    def _set_fullscreen(self) -> bool:
        """Maximize the Zoom meeting window to fullscreen."""
        try:
            import ctypes
            meeting_win = self._get_meeting_window()
            if not meeting_win:
                logger.warning("Fenêtre de réunion non trouvée pour le plein écran.")
                return False

            # Get screen dimensions
            user32 = ctypes.windll.user32
            width = user32.GetSystemMetrics(0)
            height = user32.GetSystemMetrics(1)

            # Get window handle using win32gui
            import win32gui
            hwnd = win32gui.FindWindow(None, meeting_win.window_text())

            logger.info(f"Résolution écran: {width}x{height}")
            rect_before = meeting_win.rectangle()
            logger.info(f"Position avant: ({rect_before.left}, {rect_before.top}) taille: ({rect_before.width()}x{rect_before.height()})")

            # Move and resize window to fullscreen using Win32 API
            SWP_SHOWWINDOW = 0x0040
            user32.SetWindowPos(
                hwnd,
                0,  # HWND_TOP
                0,  # x=0
                0,  # y=0
                width,  # width
                height - 50,  # height (leave room for taskbar)
                SWP_SHOWWINDOW
            )
            time.sleep(1)

            rect = meeting_win.rectangle()
            logger.info(f"Position après: ({rect.left}, {rect.top}) taille: ({rect.width()}x{rect.height()})")

            # Try F11 to minimize Zoom controls
            hotkey('f11')
            time.sleep(1)

            logger.info("✅ Plein écran activé.")
            take_zoom_window_screenshot("Fullscreen")
            return True

        except Exception as e:
            logger.error(f"Erreur plein écran: {e}")
            return False

    def _handle_checkboxes(self, window):
        """Handle audio/video checkboxes."""
        try:
            for ctrl in window.descendants():
                try:
                    txt = ctrl.window_text()
                    rect = ctrl.rectangle()
                    cx = rect.left + rect.width() // 2
                    cy = rect.top + rect.height() // 2

                    if "Ne pas se connecter" in txt:
                        click(cx, cy)
                        time.sleep(0.2)
                        logger.info("Microphone activé")

                    if "Arrêter" in txt and "vidéo" in txt:
                        click(cx, cy)
                        time.sleep(0.2)
                        logger.info("Caméra désactivée")
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"Erreur checkboxes: {e}")

    # ═══════════════════════════════════════════════════════════════
    # WINDOWS GAME BAR RECORDER
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _gamebar_capture_dirs() -> list[Path]:
        home = Path.home()
        candidates = [
            home / "Videos" / "Captures",
            home / "Vidéos" / "Captures",
            home / "OneDrive" / "Videos" / "Captures",
            home / "OneDrive" / "Vidéos" / "Captures",
        ]
        return [p for p in candidates if p.is_dir()]

    @staticmethod
    def _capture_files_before() -> set[Path]:
        before = set()
        for d in ZoomAutomation._gamebar_capture_dirs():
            before.update(d.glob("*.mp4"))
        return before

    def _wait_for_gamebar_file(self, before: set[Path], timeout: int = 45) -> Path | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1)
            for d in self._gamebar_capture_dirs():
                for p in d.glob("*.mp4"):
                    if p not in before and p.stat().st_mtime >= (self._windows_gamebar_started_at or datetime.now()).timestamp() - 5:
                        return p
        return None

    def _start_windows_gamebar_recording(self, meeting_win) -> bool:
        """Start recording with Windows Game Bar (Win+Alt+R)."""
        try:
            # Game Bar records the focused capture; bring Zoom to the front.
            try:
                meeting_win.set_focus()
                time.sleep(0.5)
            except Exception:
                self._bring_to_front(meeting_win)

            self._windows_gamebar_started_at = datetime.now()
            before = self._capture_files_before()
            self._windows_gamebar_active = True
            self._gamebar_files_before = before
            hotkey('win', 'alt', 'r')
            time.sleep(2)
            logger.info("🎮 Enregistrement Game Bar démarré (Win+Alt+R).")
            logger.info("ℹ️ Si rien ne démarre, activez 'Record game clips...' dans les paramètres Xbox Game Bar.")
            return True
        except Exception as e:
            logger.error(f"Erreur démarrage Game Bar: {e}")
            return False

    def _stop_windows_gamebar_recording(self) -> str:
        """Stop Game Bar recording and move the captured MP4 into the app folder."""
        try:
            # Focus Zoom before toggling recording off.
            meeting_win = self._get_meeting_window()
            if meeting_win:
                try:
                    meeting_win.set_focus()
                    time.sleep(0.5)
                except Exception:
                    self._bring_to_front(meeting_win)

            hotkey('win', 'alt', 'r')
            time.sleep(3)

            before = getattr(self, "_gamebar_files_before", set())
            started = self._windows_gamebar_started_at or datetime.now()
            new_file = self._wait_for_gamebar_file(before, timeout=45)

            if not new_file:
                logger.warning("Game Bar: aucun nouveau fichier MP4 détecté dans Vidéos\\Captures.")
                return ""

            # Move into the application's recordings folder when possible.
            try:
                from src.config import DEFAULT_RECORDINGS_DIR
                DEFAULT_RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                dest = DEFAULT_RECORDINGS_DIR / f"enregistrement_gamebar_{timestamp}.mp4"
                try:
                    import shutil
                    shutil.move(str(new_file), str(dest))
                except Exception:
                    import shutil
                    shutil.copy2(str(new_file), str(dest))
                logger.info(f"📁 Enregistrement Game Bar sauvegardé: {dest}")
                return str(dest)
            except Exception as e:
                logger.warning(f"Game Bar: déplacement vers le dossier des enregistrements impossible: {e}")
                return str(new_file)
        except Exception as e:
            logger.error(f"Erreur arrêt Game Bar: {e}")
            return ""

    # ═══════════════════════════════════════════════════════════════
    # RECORDING CONTROL
    # ═══════════════════════════════════════════════════════════════

    def start_recording(self) -> bool:
        """Start recording using the user's preferred engine.

        Engines: KamelRec (default), Windows Game Bar (Win+Alt+R).
        KamelRec fallback: built-in screen recorder (mss + sounddevice + ffmpeg merge).
        """
        try:
            meeting_win = self._get_meeting_window()
            if not meeting_win:
                logger.warning("Fenêtre de réunion non trouvée pour l'enregistrement.")
                return False

            # Memoriser la fenetre filmee pour ne jamais la fermer.
            if self._current_meeting_window is None:
                self._set_meeting_window(
                    meeting_win,
                    platform=getattr(self, "_current_platform", "zoom") or "zoom",
                )

            # Google Meet : le navigateur doit etre en plein ecran AVANT de
            # lancer l'enregistreur, sinon la barre des taches ou une fenetre
            # non maximisee apparait dans la video.
            if getattr(self, "_current_platform", "") == "google_meet":
                self._meet_enter_fullscreen(meeting_win)

            # Safety net: make sure the meeting audio goes through the computer
            # (the user may have started the recording manually).
            if not getattr(self, "_audio_joined", False):
                try:
                    self._audio_joined = self.ensure_computer_audio(timeout=3)
                except Exception as e:
                    logger.warning(f"Audio réunion : étape ignorée ({e}).")

            engine_choice = get_recording_engine()

            # Toute fenetre qui surgit pendant l'enregistrement (alerte
            # d'enregistrement, notification...) est fermee immediatement
            # pour ne pas apparaitre dans la video.
            self.start_popup_guard()

            if engine_choice == ENGINE_WINDOWS_GAMEBAR:
                if self._start_windows_gamebar_recording(meeting_win):
                    self.recording_state = "recording"
                    self.recording_start_time = datetime.now()
                    self._recorded_seconds = 0.0
                    self._recording_original_start = self.recording_start_time
                    logger.info("🎮 Enregistrement démarré (Windows Game Bar).")
                    take_zoom_window_screenshot("Recording_Started")
                    return True
                logger.error("Windows Game Bar: enregistrement non démarré.")
                self.stop_popup_guard()
                return False

            # --- Primary: KamelRec ---
            try:
                from src.config import USE_KAMELREC
            except Exception:
                USE_KAMELREC = False

            if USE_KAMELREC:
                if self._start_kamelrec_recording(meeting_win):
                    self.recording_state = "recording"
                    self.recording_start_time = datetime.now()
                    self._recorded_seconds = 0.0
                    self._recording_original_start = self.recording_start_time
                    logger.info("🎬 Enregistrement démarré (KamelRec - fenêtre Zoom + audio système).")
                    take_zoom_window_screenshot("Recording_Started")
                    return True
                logger.warning("KamelRec indisponible, bascule sur l'enregistreur intégré...")

            # --- Fallback: built-in recorder ---
            logger.info("Démarrage enregistrement écran + audio (intégré)...")
            if self._start_screen_recording():
                self.recording_state = "recording"
                self.recording_start_time = datetime.now()
                self._recorded_seconds = 0.0
                self._recording_original_start = self.recording_start_time
                logger.info("🎬 Enregistrement démarré (écran + audio).")
                take_zoom_window_screenshot("Recording_Started")
                return True

            logger.error("Échec de l'enregistrement.")
            self.stop_popup_guard()
            return False

        except Exception as e:
            logger.error(f"Erreur démarrage enregistrement: {e}")
            self.stop_popup_guard()
            return False

    def _start_kamelrec_recording(self, meeting_win) -> bool:
        """Start recording through KamelRec, targeting the Zoom window."""
        try:
            from src.kamelrec import KamelRecController

            if self.kamelrec is None:
                self.kamelrec = KamelRecController()
            if not self.kamelrec.available:
                logger.warning("KamelRec introuvable.")
                return False

            # Window title shown in KamelRec's window list
            try:
                zoom_title = meeting_win.window_text()
            except Exception:
                zoom_title = "Zoom"
            logger.info(f"KamelRec: fenêtre cible = '{zoom_title}'")

            if not self.kamelrec.start(zoom_title):
                return False

            # Keep Zoom visible in front while recording
            time.sleep(1)
            self._bring_to_front(meeting_win)
            if getattr(self, "_current_platform", "") == "google_meet":
                # KamelRec peut voler le focus : on re-verifie le plein ecran.
                self._meet_enter_fullscreen(meeting_win)

            self._kamelrec_active = True
            return True
        except Exception as e:
            logger.error(f"Erreur KamelRec: {e}")
            self._kamelrec_active = False
            return False

    def _finalize_recorded_elapsed(self) -> int:
        """Accumulate the current segment and return total elapsed seconds."""
        if self.recording_start_time:
            self._recorded_seconds += (datetime.now() - self.recording_start_time).total_seconds()
            self.recording_start_time = None
        return max(0, int(self._recorded_seconds))

    def _bring_to_front(self, window) -> None:
        """Bring a pywinauto window to the foreground without resizing it."""
        try:
            import win32gui
            import win32con
            hwnd = window.handle
            # Ne restaurer QUE si la fenetre est minimisee : SW_RESTORE sur une
            # fenetre maximisee/plein ecran la remettrait en 900x700.
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                win32gui.BringWindowToTop(hwnd)
        except Exception:
            try:
                window.set_focus()
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════════
    # SURVEILLANCE DES FENETRES PENDANT L'ENREGISTREMENT
    # ═══════════════════════════════════════════════════════════════
    # Zoom, KamelRec ou Windows peuvent afficher des alertes/boites de
    # dialogue au-dessus de la fenetre filmee. Un thread les detecte et
    # les ferme (ou les reduit) des leur apparition afin qu'elles
    # n'apparaissent jamais dans la video.

    _GUARD_PROTECTED_TITLES = (
        "zoom auto recorder",
        "kamelrec",
        "opencode",
        "program manager",
        # La Game Bar exclut sa propre interface de la capture : inutile de
        # la fermer (et risqué pour l'enregistrement en cours).
        "game bar",
        "xbox game bar",
    )
    _GUARD_PROTECTED_CLASSES = (
        "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
        "Windows.UI.Core.CoreWindow", "ApplicationManager_DesktopShellWindow",
        "MultitaskingViewFrame", "ForegroundStaging", "TaskListThumbnailWnd",
        "XamlExplorerHostIslandWindow", "Windows.Internal.Shell.TabProxyWindow",
    )
    # Fenetres techniques sans titre : jamais touchees.
    _GUARD_IGNORED_CLASSES = (
        "tooltips_class32", "IME", "MSCTFIME UI", "GDI+ Window", "Default IME",
        "SysShadow", "Chrome_SystemMessageWindow", "MediaContextNotificationWindow",
        "TaskListThumbnailWnd", "ForegroundStaging",
    )
    # Libelles de validation utilises pour fermer un message.
    _GUARD_OK_LABELS = (
        "ok", "d'accord", "oui", "yes", "got it", "compris", "j'ai compris",
        "continuer", "continue",
    )

    @staticmethod
    def _enum_visible_windows(include_untitled: bool = False) -> dict:
        """Retourne {hwnd: (titre, classe)} des fenetres visibles de premier niveau.

        `include_untitled=True` inclut aussi les fenetres sans titre : Zoom
        affiche certains messages (« Cette réunion est enregistrée ») dans
        une fenêtre dont le titre est vide.
        """
        try:
            import win32gui
        except Exception:
            return {}
        result = {}

        def _cb(hwnd, _param):
            try:
                if win32gui.IsWindowVisible(hwnd):
                    title = (win32gui.GetWindowText(hwnd) or "").strip()
                    if title:
                        result[hwnd] = (title, win32gui.GetClassName(hwnd))
                    elif include_untitled:
                        l, t, r, b = win32gui.GetWindowRect(hwnd)
                        if (r - l) >= 40 and (b - t) >= 20:
                            result[hwnd] = ("", win32gui.GetClassName(hwnd))
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(_cb, None)
        except Exception:
            pass
        return result

    @staticmethod
    def _process_name(hwnd) -> str:
        """Nom de l'executable proprietaire d'une fenetre (en minuscules)."""
        try:
            import ctypes
            import win32process
            _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = ctypes.c_ulong(1024)
                if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    return os.path.basename(buf.value).lower()
            finally:
                k32.CloseHandle(handle)
        except Exception:
            pass
        return ""

    def _meeting_process_name(self) -> str:
        name = getattr(self, "_guard_meeting_proc", "")
        if not name:
            hwnd = self._meeting_hwnd()
            if hwnd:
                name = self._process_name(hwnd)
                self._guard_meeting_proc = name
        return name

    def _guard_should_close_untitled(self, hwnd, cls: str) -> bool:
        """Vrai si une fenetre sans titre est un message a fermer."""
        if cls in self._GUARD_IGNORED_CLASSES or cls in self._GUARD_PROTECTED_CLASSES:
            return False
        try:
            import win32gui
            import win32con
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            if (r - l) < 80 or (b - t) < 40:
                return False
            if cls == "#32770":
                return True
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if exstyle & win32con.WS_EX_DLGMODALFRAME:
                return True
            if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
                return True
            if (style & win32con.WS_POPUP) and (exstyle & win32con.WS_EX_TOPMOST):
                return True
            # Petite fenetre du meme processus que la reunion (message Zoom).
            meeting = self._meeting_hwnd()
            meeting_proc = self._meeting_process_name()
            if meeting and meeting_proc and self._process_name(hwnd) == meeting_proc:
                ml, mt, mr, mb = win32gui.GetWindowRect(meeting)
                meeting_area = max(1, (mr - ml) * (mb - mt))
                if (r - l) * (b - t) <= 0.6 * meeting_area:
                    return True
        except Exception:
            return False
        # Dernier recours : un bouton de validation dans la fenetre.
        return self._guard_has_ok_button(hwnd)

    @classmethod
    def _guard_button_is_validation(cls, btn) -> bool:
        """Vrai si le bouton UIA est un bouton de validation (pas la barre de titre)."""
        try:
            name = (btn.window_text() or "").strip().lower()
        except Exception:
            return False
        if name not in cls._GUARD_OK_LABELS:
            return False
        try:
            if btn.parent().element_info.control_type == "TitleBar":
                return False
        except Exception:
            pass
        return True

    def _guard_has_ok_button(self, hwnd) -> bool:
        """Vrai si la fenetre contient un bouton de validation (OK, D'accord...)."""
        try:
            from pywinauto import Desktop
            win = Desktop(backend="uia").window(handle=hwnd)
            for ctrl in win.descendants(control_type="Button"):
                if self._guard_button_is_validation(ctrl):
                    return True
        except Exception:
            pass
        return False

    def _guard_dismiss_ok_dialogs(self) -> None:
        """Ferme les messages affiches *dans* la fenetre de reunion (avis Zoom)."""
        # Uniquement Zoom : l'arbre UIA d'un navigateur (Google Meet) est
        # beaucoup trop lourd pour etre parcouru en continu.
        if getattr(self, "_current_platform", "zoom") != "zoom":
            return
        win = self._current_meeting_window
        if win is None:
            return
        try:
            buttons = win.descendants(control_type="Button")
        except Exception:
            return
        for btn in buttons:
            try:
                if not btn.is_visible() or not btn.is_enabled():
                    continue
            except Exception:
                continue
            if not self._guard_button_is_validation(btn):
                continue
            try:
                btn.invoke()
            except Exception:
                try:
                    btn.click_input()
                except Exception:
                    continue
            logger.info("Message fermé dans la fenêtre de réunion (bouton de validation).")
            return

    def _meeting_hwnd(self) -> int:
        win = self._current_meeting_window
        if win is None:
            return 0
        try:
            return int(win.handle)
        except Exception:
            return 0

    @staticmethod
    def _belongs_to_own_process(hwnd) -> bool:
        try:
            import win32process
            _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
            return pid == os.getpid()
        except Exception:
            return False

    def _guard_is_protected(self, hwnd, title: str, cls: str) -> bool:
        """Vrai si la fenetre ne doit jamais etre touchee."""
        if hwnd == self._meeting_hwnd():
            return True
        # Toutes les fenetres de l'application elle-meme (fenetre principale,
        # boites de dialogue, etc.).
        if self._belongs_to_own_process(hwnd):
            return True
        low = (title or "").lower()
        for token in self._GUARD_PROTECTED_TITLES:
            if token in low:
                return True
        return cls in self._GUARD_PROTECTED_CLASSES

    @staticmethod
    def _guard_action(hwnd) -> str:
        """'close' pour une alerte/dialogue, 'minimize' pour une vraie fenetre."""
        try:
            import win32gui
            import win32con
        except Exception:
            return "close"
        try:
            if win32gui.GetClassName(hwnd) == "#32770":
                return "close"
        except Exception:
            pass
        try:
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            exstyle = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if style & win32con.WS_POPUP:
                return "close"
            if exstyle & win32con.WS_EX_TOPMOST:
                return "close"
            if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
                return "close"
        except Exception:
            pass
        return "minimize"

    def _guard_handle_window(self, hwnd, title: str, force_close: bool = False) -> None:
        """Ferme ou reduit une fenetre apparue pendant l'enregistrement."""
        try:
            import win32gui
            import win32con
        except Exception:
            return
        action = "close" if force_close else self._guard_action(hwnd)
        label = title or "message sans titre"
        try:
            if action == "minimize":
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
                logger.info("Fenêtre réduite pendant l'enregistrement : '%s'.", label)
            else:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                logger.info("Fenêtre fermée pendant l'enregistrement : '%s'.", label)
        except Exception as e:
            logger.debug("Fenêtre '%s' non traitée: %s", label, e)
            return
        # La fenetre de reunion doit rester au premier plan.
        self._guard_refocus_meeting()

    def _guard_refocus_meeting(self) -> None:
        """Remet la fenetre de reunion au premier plan apres fermeture."""
        hwnd = self._meeting_hwnd()
        if not hwnd:
            return
        try:
            import win32gui
            import win32con
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                win32gui.BringWindowToTop(hwnd)
        except Exception:
            pass

    def start_popup_guard(self, interval: float = 1.0, force: bool = False) -> None:
        """Surveille et ferme les fenetres qui apparaissent pendant l'enregistrement.

        `force=True` ignore le réglage global : utilisé par le bouton
        « Réactiver la garde » pour reprendre la surveillance en cours
        d'enregistrement.
        """
        if getattr(self, "_popup_guard_active", False):
            return
        if not force:
            try:
                if not get_close_popups_during_recording():
                    logger.info("Surveillance des fenêtres désactivée dans les paramètres.")
                    return
            except Exception:
                pass
        self._popup_guard_known = set(
            self._enum_visible_windows(include_untitled=True).keys()
        )
        self._guard_meeting_proc = ""
        self._guard_tick_count = 0
        self._popup_guard_stop = threading.Event()
        self._popup_guard_active = True
        self._popup_guard_thread = threading.Thread(
            target=self._popup_guard_loop,
            args=(interval,),
            daemon=True,
            name="popup-guard",
        )
        self._popup_guard_thread.start()
        logger.info(
            "Surveillance des fenêtres activée : toute fenêtre surgissant "
            "pendant l'enregistrement sera fermée ou réduite."
        )

    def stop_popup_guard(self) -> None:
        """Arrete la surveillance des fenetres."""
        if not getattr(self, "_popup_guard_active", False):
            return
        self._popup_guard_active = False
        try:
            self._popup_guard_stop.set()
        except Exception:
            pass
        thread = getattr(self, "_popup_guard_thread", None)
        if thread is not None and thread.is_alive():
            try:
                thread.join(timeout=2)
            except Exception:
                pass
        self._popup_guard_thread = None
        logger.info("Surveillance des fenêtres arrêtée.")

    def is_popup_guard_active(self) -> bool:
        """Vrai si la surveillance des fenêtres est en cours."""
        return bool(getattr(self, "_popup_guard_active", False))

    def _popup_guard_loop(self, interval: float) -> None:
        stop_event = self._popup_guard_stop
        while not stop_event.wait(interval):
            try:
                self._popup_guard_tick()
            except Exception as e:
                logger.debug("Surveillance des fenêtres: %s", e)

    def _popup_guard_tick(self) -> None:
        windows = self._enum_visible_windows(include_untitled=True)
        for hwnd, (title, cls) in windows.items():
            if hwnd in self._popup_guard_known:
                continue
            self._popup_guard_known.add(hwnd)
            if self._guard_is_protected(hwnd, title, cls):
                continue
            if not title:
                # Message sans titre (avis Zoom, notification...) : on ne le
                # ferme que s'il ressemble vraiment a un dialogue.
                if self._guard_should_close_untitled(hwnd, cls):
                    self._guard_handle_window(hwnd, "message sans titre", force_close=True)
                else:
                    meeting_proc = self._meeting_process_name()
                    if meeting_proc and self._process_name(hwnd) == meeting_proc:
                        logger.info(
                            "Fenêtre sans titre de la réunion laissée ouverte (classe=%s).", cls
                        )
                    else:
                        logger.debug("Fenêtre sans titre ignorée (classe=%s).", cls)
                continue
            self._guard_handle_window(hwnd, title)
        # Oublier les fenetres disparues (les identifiants peuvent etre reutilises).
        alive = set(windows.keys())
        self._popup_guard_known = {h for h in self._popup_guard_known if h in alive}

        # Messages dessines *dans* la fenetre de reunion (avis d'enregistrement
        # Zoom avec un bouton OK) : on clique sur OK. L'arbre UIA d'un
        # navigateur etant plus lourd, on le parcourt moins souvent.
        try:
            self._guard_tick_count = getattr(self, "_guard_tick_count", 0) + 1
            browsers = ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
                        "opera.exe", "vivaldi.exe")
            every = 10 if self._meeting_process_name() in browsers else 2
            if self._guard_tick_count % every == 0:
                self._guard_dismiss_ok_dialogs()
        except Exception as e:
            logger.debug("Fermeture des messages de réunion: %s", e)

    def stop_recording(self) -> bool:
        """Stop recording."""
        try:
            # Ne plus fermer de fenetres : KamelRec peut afficher une
            # confirmation de sauvegarde a la fin de l'enregistrement.
            self.stop_popup_guard()

            if self.recording_state not in ("recording", "paused"):
                logger.info("Aucun enregistrement actif.")
                return False

            logger.info("Arrêt de l'enregistrement...")

            # --- Windows Game Bar engine ---
            if getattr(self, "_windows_gamebar_active", False):
                path = self._stop_windows_gamebar_recording()
                self._windows_gamebar_active = False
                elapsed = self._finalize_recorded_elapsed()
                self.recording_state = "stopped"
                logger.info(f"🛑 Enregistrement Game Bar arrêté après {int(elapsed)}s")
                if path:
                    self._register_recording(path)
                else:
                    logger.warning("⚠️ Fichier Game Bar non trouvé.")
                take_zoom_window_screenshot("Recording_Stopped")
                return bool(path)

            # --- KamelRec engine ---
            if getattr(self, "_kamelrec_active", False):
                path = ""
                try:
                    path = self.kamelrec.stop() if self.kamelrec else ""
                except Exception as e:
                    logger.error(f"Erreur arrêt KamelRec: {e}")
                self._kamelrec_active = False
                elapsed = self._finalize_recorded_elapsed()
                self.recording_state = "stopped"
                logger.info(f"🛑 Enregistrement arrêté après {int(elapsed)}s")

                if path:
                    logger.info(f"📁 Enregistrement sauvegardé: {path}")
                    self._register_recording(path)
                else:
                    logger.warning("⚠️ Fichier KamelRec non trouvé.")
                take_zoom_window_screenshot("Recording_Stopped")
                return bool(path)

            # --- Built-in screen recorder ---
            if self.screen_recorder:
                recording_path = self._stop_screen_recording()
                if recording_path:
                    logger.info(f"📁 Enregistrement sauvegardé: {recording_path}")
                else:
                    logger.warning("⚠️ Fichier d'enregistrement non trouvé.")
                return recording_path != ""

            # --- Zoom local recording ---
            hotkey('ctrl', 'shift', 's')
            time.sleep(5)  # Let Zoom process

            elapsed = self._finalize_recorded_elapsed()
            self.recording_state = "stopped"
            logger.info(f"🛑 Enregistrement arrêté après {int(elapsed)}s")
            take_zoom_window_screenshot("Recording_Stopped")

            # Find and copy the recording file (retry multiple times)
            recording_path = self._find_and_copy_recording_with_retries()
            if recording_path:
                logger.info(f"📁 Enregistrement sauvegardé: {recording_path}")
                self._register_recording(recording_path)
            else:
                logger.warning("⚠️ Fichier d'enregistrement non trouvé.")

            return bool(recording_path)

        except Exception as e:
            logger.error(f"Erreur arrêt enregistrement: {e}")
            return False

    def _register_recording(self, recording_path: str) -> None:
        """Add a finished recording to the database."""
        try:
            from src.database import DatabaseManager, Recording
            db = DatabaseManager()
            r = Recording(
                meeting_name="Zoom Réunion",
                start_time=self._recording_original_start.isoformat() if self._recording_original_start else datetime.now().isoformat(),
                end_time=datetime.now().isoformat(),
                duration_seconds=max(0, int(getattr(self, "_recorded_seconds", 0))),
                file_path=recording_path,
                status="Completed",
            )
            db.add_recording(r)
            logger.info("✅ Enregistrement ajouté à la base de données.")
        except Exception as e:
            logger.warning(f"Erreur ajout DB: {e}")

    def _find_and_copy_recording(self) -> str:
        """Find the recorded video file and copy it to the configured directory."""
        return self._find_and_copy_recording_with_retries()

    def _find_and_copy_recording_with_retries(self, max_retries: int = 3) -> str:
        """Find and copy recording with retry on file lock."""
        import time
        for attempt in range(max_retries):
            try:
                result = self._do_find_and_copy_recording()
                if result:
                    return result
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.info(f"  Tentative {attempt+1}/{max_retries}: fichier verrouillé, réessai...")
                    time.sleep(3)
                else:
                    logger.warning(f"Échec après {max_retries} tentatives: {e}")
        return ""

    def _do_find_and_copy_recording(self) -> str:
        """Core logic to find and copy recording."""
        from src.config import DEFAULT_RECORDINGS_DIR
        import shutil

        # Wait for Zoom to finish processing
        time.sleep(5)

        # Common Zoom recording locations (resolved dynamically)
        home = Path(os.environ.get("USERPROFILE", str(Path.home())))
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        zoom_record_dirs = [
            home / "OneDrive" / "Documents" / "Zoom",
            home / "Documents" / "Zoom",
            home / "Documents" / "Zoom Auto Recorder",
            home / "Videos" / "Zoom",
            program_files / "Zoom" / "bin" / "Record",
        ]

        # Also check any OneDrive Business/personal folders, without assuming
        # a fixed number of tenants.
        try:
            one_drive = home / "OneDrive"
            if one_drive.is_dir():
                for d in one_drive.iterdir():
                    candidate = d / "Documents" / "Zoom"
                    if d.is_dir() and candidate.is_dir():
                        zoom_record_dirs.insert(0, candidate)
        except Exception as e:
            logger.debug(f"Recherche des dossiers OneDrive impossible: {e}")

        # Find the most recently modified video file
        newest_file = None
        newest_time = 0

        for record_dir in zoom_record_dirs:
            if not os.path.exists(record_dir):
                continue

            for root, dirs, files in os.walk(record_dir):
                for f in files:
                    if f.endswith((".mp4", ".m4v")):
                        fp = os.path.join(root, f)
                        try:
                            mtime = os.path.getmtime(fp)
                            if mtime > newest_time:
                                newest_time = mtime
                                newest_file = fp
                        except:
                            continue

        if not newest_file:
            logger.warning("⚠️ Aucun fichier vidéo trouvé!")
            logger.warning("   L'enregistrement local peut être désactivé dans les paramètres Zoom.")
            logger.warning("   Allez dans Zoom → Paramètres → Enregistrement pour vérifier.")
            return ""

        # Create destination directory
        dest_dir = DEFAULT_RECORDINGS_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Generate destination filename
        timestamp = datetime.fromtimestamp(newest_time).strftime("%Y%m%d_%H%M%S")
        dest_filename = f"enregistrement_{timestamp}.mp4"
        dest_path = dest_dir / dest_filename

        # Copy the file
        shutil.copy2(newest_file, dest_path)
        logger.info(f"✅ Enregistrement sauvegardé: {dest_path}")
        logger.info(f"   Ouvrez le dossier: {dest_dir}")

        return str(dest_path)

    def open_recordings_folder(self) -> bool:
        """Open the recordings folder in Explorer."""
        try:
            from src.config import DEFAULT_RECORDINGS_DIR
            DEFAULT_RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["explorer", str(DEFAULT_RECORDINGS_DIR)])
            logger.info(f"📁 Dossier des enregistrements ouvert: {DEFAULT_RECORDINGS_DIR}")
            return True
        except Exception as e:
            logger.error(f"Erreur ouverture dossier: {e}")
            return False

    def pause_recording(self) -> bool:
        """Pause recording."""
        try:
            if self.recording_state != "recording":
                return False

            if getattr(self, "_windows_gamebar_active", False):
                logger.warning("Windows Game Bar ne prend pas en charge la pause. Utilisez Arrêter.")
                return False

            ok = False
            if getattr(self, "_kamelrec_active", False) and self.kamelrec:
                ok = self.kamelrec.pause()
                if not ok:
                    logger.error("KamelRec: la mise en pause a échoué.")
            else:
                try:
                    hotkey('ctrl', 'shift', 'p')
                    time.sleep(1)
                    ok = True
                except Exception as e:
                    logger.error(f"Erreur raccourci pause: {e}")

            if not ok:
                return False

            # Freeze the elapsed timer while paused.
            if self.recording_start_time:
                self._recorded_seconds += (datetime.now() - self.recording_start_time).total_seconds()
            self.recording_start_time = None
            self.recording_state = "paused"
            logger.info("⏸ Enregistrement en pause")
            return True
        except Exception as e:
            logger.error(f"Erreur pause: {e}")
            return False

    def resume_recording(self) -> bool:
        """Resume recording."""
        try:
            if self.recording_state != "paused":
                return False

            if getattr(self, "_windows_gamebar_active", False):
                logger.warning("Windows Game Bar ne prend pas en charge la reprise.")
                return False

            ok = False
            if getattr(self, "_kamelrec_active", False) and self.kamelrec:
                ok = self.kamelrec.resume()
                if not ok:
                    logger.error("KamelRec: la reprise a échoué.")
            else:
                try:
                    hotkey('ctrl', 'shift', 'p')
                    time.sleep(1)
                    ok = True
                except Exception as e:
                    logger.error(f"Erreur raccourci reprise: {e}")

            if not ok:
                return False

            self.recording_start_time = datetime.now()
            self.recording_state = "recording"
            logger.info("▶️ Enregistrement repris")
            return True
        except Exception as e:
            logger.error(f"Erreur reprise: {e}")
            return False

    def get_recording_status(self) -> str:
        """Get current recording state."""
        return self.recording_state

    def get_recording_elapsed(self) -> int:
        """Get elapsed recording time in seconds."""
        total = float(getattr(self, "_recorded_seconds", 0.0))
        if self.recording_state == "recording" and self.recording_start_time:
            total += (datetime.now() - self.recording_start_time).total_seconds()
        return max(0, int(total))

    # ═══════════════════════════════════════════════════════════════
    # SCREEN RECORDING (Fallback when Zoom local recording is unavailable)
    # ═══════════════════════════════════════════════════════════════

    def _start_screen_recording(self) -> bool:
        """Start screen recording with audio using mss + sounddevice."""
        try:
            import mss
            import cv2
            import numpy as np
            import sounddevice as sd
            import wave
            import threading
            from src.config import DEFAULT_RECORDINGS_DIR

            # Get Zoom meeting window
            meeting_win = self._get_meeting_window()
            if not meeting_win:
                logger.warning("Fenêtre de réunion non trouvée pour l'enregistrement écran.")
                return False

            rect = meeting_win.rectangle()
            width = rect.width()
            height = rect.height()

            # Create output paths
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.screen_recording_path = DEFAULT_RECORDINGS_DIR / f"enregistrement_ecran_{timestamp}.mp4"
            self.audio_temp_path = DEFAULT_RECORDINGS_DIR / f"_audio_temp_{timestamp}.wav"
            DEFAULT_RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

            # Find audio loopback device (Stereo Mix)
            loopback_id = self._find_audio_loopback()

            # Initialize recorder
            self.screen_recorder = {
                'mss': mss.mss(),
                'video': None,
                'fps': 15,
                'left': int(rect.left),
                'top': int(rect.top),
                'width': int(width),
                'height': int(height),
                'stop_event': threading.Event(),
                'audio_device': loopback_id,
                'audio_sample_rate': 44100,
                'audio_channels': 2,
            }

            # OpenCV video writer
            fourcc = cv2.VideoWriter_fourcc(*'mp4v')
            frame_size = (int(width), int(height))
            self.screen_recorder['video'] = cv2.VideoWriter(
                str(self.screen_recording_path), fourcc, float(self.screen_recorder['fps']),
                frame_size
            )

            # Start audio recording thread
            if loopback_id is not None:
                self._audio_thread = threading.Thread(
                    target=self._record_audio, daemon=True
                )
                self._audio_thread.start()
                logger.info(f"🎙️ Audio capturé depuis: {self._get_device_name(loopback_id)}")
            else:
                logger.warning("⚠️ Appareil audio loopback non trouvé, enregistrement vidéo sans audio.")

            # Start video recording thread
            self._recording_thread = threading.Thread(
                target=self._screen_record_loop, daemon=True
            )
            self._recording_thread.start()

            logger.info(f"🎬 Enregistrement écran démarré: {self.screen_recording_path}")
            return True

        except Exception as e:
            logger.error(f"Erreur démarrage enregistrement écran: {e}")
            return False

    def _find_audio_loopback(self) -> int:
        """Find the stereo mix / loopback audio device."""
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            for i, d in enumerate(devices):
                name = d['name'].lower()
                if d['max_input_channels'] > 0 and any(kw in name for kw in ['mixage', 'stereo mix', 'loopback', 'hier']):
                    logger.info(f"Appareil audio loopback trouvé: [{i}] {d['name']}")
                    return i
            # Fallback: try first input device
            for i, d in enumerate(devices):
                if d['max_input_channels'] > 0:
                    logger.info(f"Appareil audio fallback: [{i}] {d['name']}")
                    return i
            return None
        except Exception as e:
            logger.warning(f"Erreur recherche appareil audio: {e}")
            return None

    def _get_device_name(self, device_id: int) -> str:
        """Get device name by ID."""
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            if device_id is not None and 0 <= device_id < len(devices):
                return devices[device_id]['name']
            return f"Device {device_id}"
        except:
            return f"Device {device_id}"

    def _record_audio(self):
        """Record audio from loopback device using callback approach."""
        try:
            import sounddevice as sd
            import numpy as np
            import wave
            import threading

            device_id = self.screen_recorder.get('audio_device')
            sample_rate = self.screen_recorder.get('audio_sample_rate', 44100)
            channels = self.screen_recorder.get('audio_channels', 2)

            if device_id is None:
                return

            audio_data = []
            lock = threading.Lock()

            def audio_callback(indata, frames, time_info, status):
                """Callback for audio input."""
                if status:
                    logger.debug(f"Audio status: {status}")
                if not self.screen_recorder['stop_event'].is_set():
                    with lock:
                        audio_data.append(indata.copy())

            # Start recording with callback
            with sd.InputStream(
                device=device_id,
                channels=channels,
                samplerate=sample_rate,
                callback=audio_callback,
                blocksize=int(sample_rate * 0.1)  # 100ms blocks
            ):
                # Wait until stop event is set
                while not self.screen_recorder['stop_event'].is_set():
                    time.sleep(0.1)

            # Save to WAV
            if audio_data:
                audio_array = np.concatenate(audio_data, axis=0)
                with wave.open(str(self.audio_temp_path), 'wb') as wf:
                    wf.setnchannels(channels)
                    wf.setsampwidth(2)
                    wf.setframerate(sample_rate)
                    wf.writeframes(audio_array.tobytes())
                duration = len(audio_array) / sample_rate
                logger.info(f"🎵 Audio enregistré: {self.audio_temp_path} ({duration:.1f}s)")
            else:
                logger.warning("⚠️ Aucun audio capturé.")

        except Exception as e:
            logger.warning(f"Erreur enregistrement audio: {e}")

    def _screen_record_loop(self):
        """Main loop for screen recording (video only)."""
        try:
            import cv2
            import numpy as np

            while not self.screen_recorder['stop_event'].is_set():
                try:
                    # Capture la fenetre de reunion (multi-ecran: left/top reels)
                    screenshot = self.screen_recorder['mss'].grab({
                        'left': self.screen_recorder.get('left', 0),
                        'top': self.screen_recorder.get('top', 0),
                        'width': self.screen_recorder['width'],
                        'height': self.screen_recorder['height'],
                    })

                    # Convert to OpenCV format
                    frame = cv2.cvtColor(np.array(screenshot), cv2.COLOR_BGRA2BGR)

                    # Write frame
                    if self.screen_recorder.get('video'):
                        self.screen_recorder['video'].write(frame)

                except Exception as e:
                    logger.debug(f"Erreur capture frame: {e}")
                    time.sleep(0.05)

        finally:
            # Release video writer
            if self.screen_recorder.get('video'):
                self.screen_recorder['video'].release()

    def _merge_audio_video(self) -> str:
        """Merge the recorded video and audio into a single MP4 with ffmpeg."""
        import os

        video_path = str(self.screen_recording_path)
        audio_path = str(self.audio_temp_path)
        output_path = str(self.screen_recording_path.with_suffix('.mp4'))

        # If the video file is already the final mp4 target, write to a temp name
        if os.path.abspath(video_path) == os.path.abspath(output_path):
            merge_target = str(self.screen_recording_path.with_name(
                self.screen_recording_path.stem + "_merged.mp4"
            ))
        else:
            merge_target = output_path

        ffmpeg_exe = self._find_ffmpeg()
        if ffmpeg_exe and os.path.exists(video_path) and os.path.exists(audio_path):
            try:
                import subprocess
                cmd = [
                    ffmpeg_exe, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", video_path,
                    "-i", audio_path,
                    "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k",
                    "-shortest",
                    "-movflags", "+faststart",
                    merge_target,
                ]
                proc = subprocess.run(cmd, capture_output=True, timeout=300)
                if proc.returncode == 0 and os.path.exists(merge_target) and os.path.getsize(merge_target) > 0:
                    if os.path.abspath(merge_target) != os.path.abspath(video_path):
                        try:
                            os.remove(video_path)
                        except Exception:
                            pass
                    try:
                        os.remove(audio_path)
                    except Exception:
                        pass
                    logger.info(f"✅ Vidéo + audio fusionnés (ffmpeg): {merge_target}")
                    return merge_target
                logger.warning(f"ffmpeg a échoué: {proc.stderr.decode(errors='ignore')[:200]}")
            except Exception as e:
                logger.warning(f"Erreur fusion ffmpeg: {e}")
        else:
            logger.warning("ffmpeg ou fichiers source indisponibles, pas de fusion audio/vidéo.")

        # Fallback: try to keep the audio next to the video so nothing is lost
        try:
            if os.path.exists(audio_path):
                keep_audio = str(Path(video_path).with_suffix(".wav"))
                if os.path.abspath(audio_path) != os.path.abspath(keep_audio):
                    import shutil
                    shutil.copy2(audio_path, keep_audio)
                    logger.info(f"💾 Audio conservé séparément: {keep_audio}")
        except Exception as e:
            logger.debug(f"Impossible de conserver l'audio: {e}")

        return output_path

    @staticmethod
    def _find_ffmpeg() -> str | None:
        """Return a usable ffmpeg executable path."""
        candidates = []
        try:
            import imageio_ffmpeg
            candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
        except Exception:
            pass
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86", r"C:\Program Files (x86)")
        candidates += [
            str(Path(program_files) / "ffmpeg" / "bin" / "ffmpeg.exe"),
            str(Path(program_files_x86) / "ffmpeg" / "bin" / "ffmpeg.exe"),
            "ffmpeg",
        ]
        import shutil as _shutil
        import subprocess
        for candidate in candidates:
            try:
                found = _shutil.which(candidate) if not os.path.isabs(candidate) else candidate
                if not found or not os.path.exists(found):
                    continue
                probe = subprocess.run([found, "-version"], capture_output=True, timeout=10)
                if probe.returncode == 0:
                    return found
            except Exception:
                continue
        return None

    def _stop_screen_recording(self) -> str:
        """Stop screen recording and merge audio+video. Returns the output path."""
        try:
            if not self.screen_recorder:
                return ""

            # Signal stop
            self.screen_recorder['stop_event'].set()

            # Wait for threads to finish
            if hasattr(self, '_recording_thread'):
                self._recording_thread.join(timeout=10)
            if hasattr(self, '_audio_thread'):
                self._audio_thread.join(timeout=10)

            # Release resources
            if self.screen_recorder.get('video'):
                self.screen_recorder['video'].release()
            self.screen_recorder['mss'].close()

            elapsed = self._finalize_recorded_elapsed()
            self.recording_state = "stopped"
            logger.info(f"🛑 Enregistrement écran arrêté après {int(elapsed)}s")

            # Merge audio and video if audio was recorded
            final_path = self._merge_audio_video()

            if os.path.exists(final_path):
                size = os.path.getsize(final_path)
                logger.info(f"✅ Fichier sauvegardé: {final_path} ({size // 1024} KB)")
                from src.config import DEFAULT_RECORDINGS_DIR
                logger.info(f"   Ouvrez le dossier: {DEFAULT_RECORDINGS_DIR}")
                # Add to database
                try:
                    from src.database import DatabaseManager, Recording
                    db = DatabaseManager()
                    r = Recording(
                        meeting_name="Zoom Réunion",
                        start_time=self._recording_original_start.isoformat() if self._recording_original_start else "",
                        end_time=datetime.now().isoformat(),
                        duration_seconds=max(0, int(getattr(self, "_recorded_seconds", 0))),
                        file_path=final_path,
                        status="Completed"
                    )
                    db.add_recording(r)
                    logger.info("✅ Enregistrement ajouté à la base de données.")
                except Exception as e:
                    logger.warning(f"Erreur ajout DB: {e}")
                return final_path
            else:
                logger.warning("⚠️ Fichier d'enregistrement non trouvé.")
                return ""

        except Exception as e:
            logger.error(f"Erreur arrêt enregistrement écran: {e}")
            return ""

    # ═══════════════════════════════════════════════════════════════
    # UTILITY
    # ═══════════════════════════════════════════════════════════════

    def kill_zoom(self):
        """Kill all Zoom processes."""
        try:
            subprocess.run(['taskkill', '/F', '/IM', 'Zoom.exe'], capture_output=True, timeout=5)
            logger.info("Zoom arrêté.")
        except Exception as e:
            logger.debug(f"Erreur arrêt Zoom: {e}")
