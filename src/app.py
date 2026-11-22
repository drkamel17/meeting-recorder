"""
Zoom Auto Recorder - Application principale
============================================
Application professionnelle pour l'automatisation de la prise de rendez-vous
et l'enregistrement de réunions Zoom sur Windows.
"""
import sys
import os
import time
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QVBoxLayout, QHBoxLayout,
    QWidget, QLabel, QPushButton, QMessageBox, QTextEdit, QScrollArea,
    QTableWidget, QTableWidgetItem, QListWidget, QFrame, QGroupBox,
    QGridLayout, QProgressBar, QComboBox, QCheckBox, QLineEdit, QSpinBox,
    QSystemTrayIcon, QMenu
)
from PySide6.QtCore import (
    QTimer, Qt, QDateTime, QEvent, QObject, Signal, QUrl, QRectF
)
from PySide6.QtGui import (
    QColor, QDesktopServices, QIcon, QPixmap, QPainter, QPen, QBrush
)

from src.logger import setup_logging
from src.database import DatabaseManager, Meeting, Recording
from src.ui.add_meeting_dialog import AddMeetingDialog
from src.ui.windows_tasks_dialog import WindowsTasksDialog
from src.automation import ZoomAutomation
from src.windows_tasks import (
    sync_windows_tasks, remove_windows_task, MISSED_WINDOW_MINUTES,
    register_remote_autostart, unregister_remote_autostart,
    is_remote_autostart_enabled, is_frozen,
)
from src.scheduler import MeetingScheduler
from src.settings import (
    get_recording_engine, set_recording_engine,
    get_close_popups_during_recording, set_close_popups_during_recording,
    ENGINE_KAMELREC, ENGINE_WINDOWS_GAMEBAR, ENGINE_LABELS,
    get_remote_config, get_remote_web_token, regenerate_remote_web_token,
    set_remote_web_enabled, set_remote_web_port, set_remote_web_token,
    set_remote_email_enabled, set_remote_email_address, set_remote_email_password,
    set_remote_email_keyword, set_remote_email_senders, set_remote_email_poll_seconds,
    get_remote_autostart, set_remote_autostart,
    get_remote_autostart_default_done, set_remote_autostart_default_done,
)
from src.remote_control import (
    RemoteControlService, test_email_connection,
)
from src.clipboard import copy_text
from src.config import (
    STATE_WAITING, STATE_LAUNCHING_ZOOM, STATE_JOINING_MEETING,
    STATE_WAITING_ROOM, STATE_IN_MEETING, STATE_RECORDING,
    STATE_COMPLETED, STATE_ERROR, STATE_PAUSED, STATE_CANCELLED,
    DEFAULT_RECORDINGS_DIR, DATA_DIR, LOG_DIR, APP_VERSION, DB_PATH
)

STATUS_COLORS = {
    STATE_WAITING: "#e9ecef",
    STATE_LAUNCHING_ZOOM: "#fff3cd",
    STATE_JOINING_MEETING: "#d1ecf1",
    STATE_WAITING_ROOM: "#fff3cd",
    STATE_IN_MEETING: "#d4edda",
    STATE_RECORDING: "#f8d7da",
    STATE_COMPLETED: "#d4edda",
    STATE_PAUSED: "#fff3cd",
    STATE_ERROR: "#f8d7da",
    STATE_CANCELLED: "#e2e3e5",
}


# ═══════════════════════════════════════════════════════════════
# THREAD BRIDGE (worker -> GUI thread)
# ═══════════════════════════════════════════════════════════════
class AppBridge(QObject):
    log_message = Signal(str)
    meeting_started = Signal(object, str)
    recording_stopped = Signal()
    manual_recording_started = Signal(bool)
    guard_hotkey = Signal()


class GlobalHotkey(threading.Thread):
    """Raccourci clavier global : fonctionne même sans le focus sur l'application.

    Windows envoie WM_HOTKEY au thread qui a enregistré le raccourci ; le
    callback est donc exécuté hors du thread Qt (l'appelant doit repasser par
    un signal).
    """

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_NOREPEAT = 0x4000
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    VK_S = 0x53

    def __init__(self, callback, name="raccourci"):
        super().__init__(daemon=True, name=f"hotkey-{name}")
        self.callback = callback
        self.name = name
        self._thread_id = None
        self._stop = threading.Event()

    def run(self):
        import ctypes
        from ctypes import wintypes

        logger = logging.getLogger("ZoomAutoRecorder")
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()

        if not user32.RegisterHotKey(
            None, 1, self.MOD_CONTROL | self.MOD_NOREPEAT, self.VK_S
        ):
            logger.warning(
                "Raccourci %s indisponible : déjà utilisé par une autre application.",
                self.name,
            )
            return
        logger.info("Raccourci %s activé pendant l'enregistrement.", self.name)

        try:
            msg = wintypes.MSG()
            while not self._stop.is_set():
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):
                    break
                if msg.message == self.WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception:
                        pass
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            user32.UnregisterHotKey(None, 1)
            logger.info("Raccourci %s désactivé.", self.name)

    def stop(self):
        self._stop.set()
        try:
            import ctypes
            if self._thread_id:
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread_id, self.WM_QUIT, 0, 0
                )
        except Exception:
            pass



# ═══════════════════════════════════════════════════════════════
# MEETING ROW WIDGET
# ═══════════════════════════════════════════════════════════════
class MeetingRowWidget(QFrame):
    def __init__(self, meeting: Meeting, db: DatabaseManager, app=None, parent=None):
        super().__init__(parent)
        self.meeting = meeting
        self.db = db
        self.app = app
        self.setFrameStyle(QFrame.StyledPanel | QFrame.Raised)
        self.setMinimumHeight(90)
        self.setStyleSheet("""
            QFrame { background-color:#fafafa; border:1px solid #ddd;
                     border-radius:6px; margin:4px; }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(16)

        # Status badge
        self.status_lbl = QLabel(meeting.status)
        self.status_lbl.setStyleSheet(
            f"background:{STATUS_COLORS.get(meeting.status,'#eee')}; "
            f"padding:6px 12px; border-radius:4px; font-weight:bold; color:#333;"
        )
        layout.addWidget(self.status_lbl)

        # Info
        info = QVBoxLayout()
        name_lbl = QLabel(f"<b>{meeting.name}</b>")
        name_lbl.setStyleSheet("font-size:13px; color:#222;")
        info.addWidget(name_lbl)

        dt_str = ""
        if meeting.start_time:
            dt = QDateTime.fromString(meeting.start_time, Qt.ISODate)
            dt_str = dt.toString("dd/MM/yyyy HH:mm")
        dur_lbl = QLabel(f"📅 {dt_str}  •  ⏱ {meeting.duration_minutes} min  •  👤 {meeting.display_name or '—'}  •  🖥 {meeting.platform.upper() if meeting.platform else 'ZOOM'}")
        dur_lbl.setStyleSheet("font-size:11px; color:#666;")
        info.addWidget(dur_lbl)

        icon = "✅" if meeting.is_enabled else "⏸️"
        recur_lbl = QLabel(f"{icon} {meeting.recurrence if meeting.recurrence != 'none' else 'ponctuel'}")
        recur_lbl.setStyleSheet("font-size:11px; color:#888;")
        info.addWidget(recur_lbl)
        layout.addLayout(info)
        layout.addStretch()

        # Buttons
        btns = QVBoxLayout()
        btns.setSpacing(4)
        actions = [
            ("▶ Démarrer", "#28a745", self.manual_start),
            ("✏️ Modifier", "#007bff", self.edit_meeting),
            ("🗑 Supprimer", "#dc3545", self.delete_meeting),
            ("⏯ Activer/Désactiver", "#6c757d", self.toggle_enable),
        ]
        for txt, style, cb in actions:
            b = QPushButton(txt)
            b.setStyleSheet(f"background:{style};color:white;border:none;border-radius:4px;padding:5px 10px;font-size:11px;")
            b.clicked.connect(cb)
            btns.addWidget(b)
        layout.addLayout(btns)

    def manual_start(self):
        if self.app:
            self.app.start_meeting(self.meeting)

    def edit_meeting(self):
        dialog = AddMeetingDialog(self.app.main_window if self.app else None)
        dialog.name_input.setText(self.meeting.name)
        dialog.meeting_id_input.setText(self.meeting.meeting_id)
        dialog.passcode_input.setText(self.meeting.passcode)
        dialog.display_name_input.setText(self.meeting.display_name)
        if self.meeting.start_time:
            dialog.date_time_input.setDateTime(QDateTime.fromString(self.meeting.start_time, Qt.ISODate))
        dialog.duration_input.setValue(self.meeting.duration_minutes)
        dialog.mic_checkbox.setChecked(self.meeting.mic_on_start)
        dialog.cam_checkbox.setChecked(self.meeting.camera_on_start)
        dialog.chat_checkbox.setChecked(self.meeting.chat_open)
        # Sélectionner la plateforme enregistrée
        idx = dialog.platform_combo.findData(getattr(self.meeting, "platform", "zoom"))
        if idx >= 0:
            dialog.platform_combo.setCurrentIndex(idx)

        if dialog.exec():
            data = dialog.get_data()
            self.meeting.name = data["name"]
            self.meeting.meeting_id = data["meeting_id"]
            self.meeting.passcode = data["passcode"]
            self.meeting.display_name = data["display_name"]
            self.meeting.start_time = data["start_time"]
            self.meeting.duration_minutes = data["duration_minutes"]
            self.meeting.mic_on_start = data["mic_on_start"]
            self.meeting.camera_on_start = data["camera_on_start"]
            self.meeting.chat_open = data["chat_open"]
            self.meeting.platform = data["platform"]
            self.db.update_meeting(self.meeting)
            if self.app:
                self.app._sync_windows_tasks()
                self.app.load_meetings()

    def delete_meeting(self):
        ok = QMessageBox.question(self, "Supprimer",
                                  f"Supprimer '{self.meeting.name}' ?",
                                  QMessageBox.Yes | QMessageBox.No)
        if ok == QMessageBox.Yes:
            self.db.delete_meeting(self.meeting.id)
            if self.app:
                self.app._sync_windows_tasks()
                self.app.load_meetings()

    def toggle_enable(self):
        self.meeting.is_enabled = not self.meeting.is_enabled
        self.db.update_meeting(self.meeting)
        if self.app:
            self.app._sync_windows_tasks()
            self.app.load_meetings()


# ═══════════════════════════════════════════════════════════════
# RECORDING CONTROL WIDGET
# ═══════════════════════════════════════════════════════════════
class RecordingControlWidget(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setStyleSheet("background:#fdf2f2; border:2px solid #f5c6cb; border-radius:8px; padding:10px;")
        outer = QVBoxLayout(self)
        outer.setSpacing(8)

        # Ligne 1 : état + chrono
        top = QHBoxLayout()
        top.setSpacing(12)

        self.rec_status_lbl = QLabel("🔴 Enregistrement : Arrêté")
        self.rec_status_lbl.setStyleSheet("font-size:13px; font-weight:bold; color:#721c24;")
        top.addWidget(self.rec_status_lbl)

        self.rec_timer_lbl = QLabel("⏱ 00:00:00")
        self.rec_timer_lbl.setStyleSheet("font-size:13px; font-family:Consolas,monospace; color:#721c24;")
        top.addWidget(self.rec_timer_lbl)

        top.addStretch()

        # Ligne 2 : commandes de l'enregistrement
        bottom = QHBoxLayout()
        bottom.setSpacing(12)

        record_btn = QPushButton("▶ Enregistrer")
        pause_btn = QPushButton("⏸ Pause")
        resume_btn = QPushButton("▶ Reprendre")
        stop_btn = QPushButton("⏹ Arrêter")
        folder_btn = QPushButton("📁 Ouvrir dossier")

        self.record_btn = record_btn
        self.pause_btn = pause_btn
        self.resume_btn = resume_btn
        self.stop_btn = stop_btn
        self.folder_btn = folder_btn

        self._engine = get_recording_engine()
        self.app.rec_control = self

        button_specs = [
            (record_btn, "#dc3545", self.app.manual_start_recording, bottom),
            (pause_btn, "#ffc107", self.app.manual_pause_recording, bottom),
            (resume_btn, "#28a745", self.app.manual_resume_recording, bottom),
            (stop_btn, "#6c757d", self.app.stop_current_recording, bottom),
            (folder_btn, "#007acc", self.app.auto_engine.open_recordings_folder, bottom),
        ]
        for button, color, callback, row in button_specs:
            button.setStyleSheet(
                f"QPushButton {{ background:{color}; color:white; border:none; border-radius:4px;"
                " padding:5px 10px; font-size:11px; font-weight:bold; }}"
                " QPushButton:disabled { background:#d6c8c8; color:#7a6f6f; }"
            )
            button.clicked.connect(callback)
            row.addWidget(button)

        outer.addLayout(top)
        outer.addLayout(bottom)

        self.set_control_state("stopped")

        # Timer update
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_timer)
        self.timer.start(1000)

    def refresh_engine(self) -> None:
        """Reload the selected engine (called after saving the setting)."""
        self._engine = get_recording_engine()
        self.set_control_state(self.app.auto_engine.get_recording_status())

    def set_control_state(self, state: str) -> None:
        """Enable only the actions that make sense for the current state."""
        recording = state == "recording"
        paused = state == "paused"
        active = recording or paused
        gamebar = self._engine == ENGINE_WINDOWS_GAMEBAR

        self.record_btn.setEnabled(not active)
        self.pause_btn.setEnabled(recording and not gamebar)
        self.resume_btn.setEnabled(paused and not gamebar)
        self.stop_btn.setEnabled(active)

        self.record_btn.setToolTip("Démarrer l'enregistrement" if not active else "Un enregistrement est déjà en cours")
        if gamebar:
            self.pause_btn.setToolTip("La pause n'est pas disponible avec Windows Game Bar")
            self.resume_btn.setToolTip("La reprise n'est pas disponible avec Windows Game Bar")
        else:
            self.pause_btn.setToolTip("Mettre l'enregistrement en pause" if recording else "Disponible uniquement pendant l'enregistrement")
            self.resume_btn.setToolTip("Reprendre l'enregistrement" if paused else "Disponible uniquement lorsque l'enregistrement est en pause")
        self.stop_btn.setToolTip("Arrêter et sauvegarder l'enregistrement" if active else "Aucun enregistrement actif")

        # Garde-fou des fenêtres : boutons permanents (voir la barre dédiée).
        self.app.update_guard_controls()

    def _update_timer(self):
        elapsed = self.app.auto_engine.get_recording_elapsed()
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        self.rec_timer_lbl.setText(f"⏱ {h:02d}:{m:02d}:{s:02d}")
        state = self.app.auto_engine.get_recording_status()
        labels = {
            "recording": "🔴 En cours",
            "paused": "⏸ En pause",
            "stopped": "⏹ Arrêté",
        }
        self.rec_status_lbl.setText(f"Enregistrement : {labels.get(state, state)}")
        self.set_control_state(state)


# ═══════════════════════════════════════════════════════════════
# MAIN APPLICATION
# ═══════════════════════════════════════════════════════════════
class _TrayMainWindow(QMainWindow):
    """Fenêtre principale qui peut rester active dans la zone de notification.

    Quand le contrôle à distance est actif, fermer la fenêtre ne doit pas
    arrêter le serveur web : la fenêtre est masquée et l'application continue
    en arrière-plan (icône près de l'horloge).
    """

    def __init__(self, on_close):
        super().__init__()
        self._on_close = on_close

    def closeEvent(self, event):  # noqa: N802 (API Qt)
        try:
            allow = self._on_close(event)
        except Exception:
            allow = True
        if allow:
            event.accept()
        else:
            event.ignore()


class ZoomAutoRecorderApp:
    def __init__(self, auto_meeting_id=None, remote_server=False):
        # Identifiant de la réunion à démarrer automatiquement (lancement par
        # le Planificateur Windows). None = lancement normal.
        self.auto_meeting_id = auto_meeting_id
        # Mode « contrôle à distance » : démarré à l'ouverture de session,
        # fenêtre réduite, prêt à recevoir un ordre web ou e-mail.
        self.remote_server_mode = bool(remote_server)
        self.remote_service = None
        self.remote_last_order = None
        # Zone de notification (icône près de l'horloge) : permet de fermer la
        # fenêtre tout en gardant la page web active.
        self._tray = None
        self._force_quit = False
        self._tray_notice_shown = False
        self.logger = setup_logging()
        self.db = DatabaseManager()
        self.db.init_db()
        # Remettre en attente les reunions coincées (crash precedent, etc.)
        self.db.reset_stuck_meetings()
        # Make sure Windows tasks match the meetings in the database.
        self._sync_windows_tasks()
        self.auto_engine = ZoomAutomation()
        self.current_meeting = None
        self.timer = None
        self.rec_mon_timer = None

        self.app = QApplication(sys.argv)

        # Qt timers must be created after QApplication exists.
        self.timer = QTimer(self.app)
        self.timer.timeout.connect(self.check_schedule)
        self.timer.start(10000)
        self.rec_mon_timer = QTimer(self.app)
        self.rec_mon_timer.timeout.connect(self._monitor_recording)
        self.rec_mon_timer.start(5000)

        # Worker -> GUI thread bridge
        self.bridge = AppBridge()
        self.bridge.meeting_started.connect(self._on_meeting_started)
        self.bridge.recording_stopped.connect(self._on_recording_stopped)
        self.bridge.manual_recording_started.connect(self._on_manual_recording_started)
        self.bridge.guard_hotkey.connect(self.toggle_popup_guard)
        self._start_thread = None
        self._stop_thread = None
        self._manual_thread = None
        self._recording_deadline = None
        # Occurrences deja lancees (pour ne pas les relancer automatiquement).
        self._finished_meetings = set()

        # Raccourci global Ctrl+S (actif seulement pendant l'enregistrement).
        self._hotkey = None
        self._hotkey_unavailable = False
        self.hotkey_timer = QTimer(self.app)
        self.hotkey_timer.timeout.connect(self._sync_guard_hotkey)
        self.hotkey_timer.start(1000)

        # Une fois la page web activée, le serveur doit aussi démarrer avec
        # Windows pour rester joignable après un redémarrage du PC.
        self._ensure_remote_autostart()

        self.main_window = _TrayMainWindow(self._on_window_close)
        self.main_window.setWindowTitle("Zoom Auto Recorder & Meeting Assistant")
        self.main_window.resize(1100, 720)
        self.main_window.setWindowIcon(self._build_tray_icon())
        self.setup_ui()

        # Contrôle à distance : la file d'actions est consommée dans le thread
        # graphique (les handlers du serveur HTTP n'ont pas le droit de toucher
        # aux widgets Qt).
        self.remote_timer = QTimer(self.app)
        self.remote_timer.timeout.connect(self._poll_remote_actions)
        self.remote_timer.start(1000)
        self._start_remote_service()

    def _sync_windows_tasks(self):
        """Synchronize the per-meeting Windows Task Scheduler entries."""
        try:
            sync_windows_tasks(self.db)
        except Exception as e:
            self.logger.warning(f"Planificateur Windows: synchronisation impossible: {e}")

    def _remove_auto_task(self, meeting_id):
        """Supprime la tâche Windows ponctuelle d'une réunion planifiée."""
        try:
            remove_windows_task(meeting_id)
        except Exception as e:
            self.logger.debug(f"Suppression de la tâche Windows impossible : {e}")

    # ──────────────────────────────────────────────────────────
    # CONTRÔLE À DISTANCE (page web + e-mail Gmail)
    # ──────────────────────────────────────────────────────────
    def _start_remote_service(self):
        """Démarre la page web et/ou la surveillance e-mail selon les paramètres."""
        self._stop_remote_service()
        config = get_remote_config()
        if not (config.get("web_enabled") or config.get("email_enabled")):
            if self.remote_server_mode:
                self.logger.warning(
                    "Mode contrôle à distance démarré, mais la page web et l'e-mail "
                    "sont désactivés dans ⚙️ Paramètres."
                )
            try:
                self._refresh_remote_urls()
                self._refresh_remote_info()
            except Exception:
                pass
            self._update_tray()
            return
        service = RemoteControlService(
            web_enabled=config.get("web_enabled", False),
            web_port=config.get("web_port", 8080),
            web_token=config.get("web_token", ""),
            email_config={
                "enabled": config.get("email_enabled", False),
                "address": config.get("email_address", ""),
                "password": config.get("email_password", ""),
                "keyword": config.get("email_keyword", "record"),
                "senders": config.get("email_senders", []),
                "poll_seconds": config.get("email_poll_seconds", 60),
            },
        )
        service.status_provider = self._remote_status
        service.recordings_provider = self._remote_recordings
        service.download_provider = self._remote_download_path
        # Le jeton est relu depuis les paramètres à chaque requête : si le
        # jeton est régénéré (ou modifié par une autre instance), la page
        # accepte immédiatement le nouveau jeton.
        service.token_provider = lambda: get_remote_web_token(create=True)
        service.start()
        self.remote_service = service
        try:
            self._refresh_remote_urls()
            self._refresh_remote_info()
        except Exception:
            pass
        self._update_tray()

    def _stop_remote_service(self):
        service = getattr(self, "remote_service", None)
        if service is not None:
            try:
                service.stop()
            except Exception as e:
                self.logger.debug(f"Arrêt du contrôle à distance : {e}")
        self.remote_service = None

    # ──────────────────────────────────────────────────────────
    # ZONE DE NOTIFICATION (fermeture sans couper le serveur)
    # ──────────────────────────────────────────────────────────
    def _ensure_remote_autostart(self):
        """Active une fois le démarrage avec Windows quand la page web sert.

        Sans ce réglage, le lien cesse de fonctionner après un redémarrage du
        PC tant que l'application n'est pas relancée à la main. Le choix de
        l'utilisateur reste prioritaire : s'il décoche ensuite la case, le
        raccourci n'est pas recréé.
        """
        try:
            if not is_frozen():
                # Mode source : ne pas créer de raccourci vers Python.
                return
            config = get_remote_config()
            if not (config.get("web_enabled") or config.get("email_enabled")):
                return
            if get_remote_autostart() and is_remote_autostart_enabled():
                return
            if not get_remote_autostart_default_done():
                set_remote_autostart_default_done(True)
                if register_remote_autostart():
                    set_remote_autostart(True)
                    self.logger.info(
                        "Contrôle à distance : démarrage automatique activé "
                        "(la page reste joignable après fermeture/redémarrage)."
                    )
            elif get_remote_autostart():
                # Réglage actif mais raccourci disparu : le recréer.
                if register_remote_autostart():
                    self.logger.info(
                        "Contrôle à distance : raccourci de démarrage recréé."
                    )
        except Exception as e:
            self.logger.debug(f"Démarrage automatique du contrôle à distance : {e}")

    def _build_tray_icon(self) -> QIcon:
        """Petite icône dessinée : aucun fichier externe n'est nécessaire."""
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#1f2937")))
        painter.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
        painter.setBrush(QBrush(QColor("#e11d48")))
        painter.drawEllipse(QRectF(13, 13, 38, 38))
        painter.setPen(QPen(QColor("#ffffff")))
        font = painter.font()
        font.setBold(True)
        font.setPixelSize(30)
        painter.setFont(font)
        painter.drawText(QRectF(13, 13, 38, 38), Qt.AlignCenter, "R")
        painter.end()
        return QIcon(pixmap)

    def _create_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.logger.warning(
                "Zone de notification indisponible : la fenêtre sera réduite "
                "dans la barre des tâches au lieu d'être masquée."
            )
            return
        tray = QSystemTrayIcon(self._build_tray_icon(), self.app)
        tray.setToolTip(
            "Zoom Auto Recorder — contrôle à distance actif "
            "(la page reste accessible)"
        )
        menu = QMenu()
        open_action = menu.addAction("Ouvrir Zoom Auto Recorder")
        open_action.triggered.connect(self._restore_window)
        if get_remote_config().get("web_enabled"):
            page_action = menu.addAction("Ouvrir la page de contrôle")
            page_action.triggered.connect(self.open_remote_page)
        menu.addSeparator()
        quit_action = menu.addAction(
            "Quitter complètement (arrête le contrôle à distance)"
        )
        quit_action.triggered.connect(self._quit_from_tray)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray
        self.app.setQuitOnLastWindowClosed(False)
        self.logger.info(
            "Icône de notification active : fermer la fenêtre garde le "
            "contrôle à distance en marche."
        )

    def _destroy_tray(self):
        tray = getattr(self, "_tray", None)
        if tray is not None:
            try:
                tray.hide()
                tray.setContextMenu(None)
                tray.deleteLater()
            except Exception:
                pass
        self._tray = None
        try:
            self.app.setQuitOnLastWindowClosed(True)
        except Exception:
            pass

    def _update_tray(self, recreate: bool = False):
        """Affiche l'icône seulement quand un serveur de contrôle tourne."""
        active = getattr(self, "remote_service", None) is not None
        if recreate:
            self._destroy_tray()
        if active and self._tray is None:
            self._create_tray()
        elif not active and self._tray is not None:
            self._destroy_tray()

    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._restore_window()

    def _restore_window(self):
        try:
            self.main_window.showNormal()
            self.main_window.raise_()
            self.main_window.activateWindow()
        except Exception:
            pass

    def _recording_in_progress(self) -> bool:
        try:
            status = self.auto_engine.get_recording_status()
        except Exception:
            status = ""
        if status in ("recording", "paused"):
            return True
        thread = getattr(self, "_start_thread", None)
        return bool(thread is not None and thread.is_alive())

    def _on_window_close(self, event) -> bool:
        """Autorise (True) ou refuse (False) la fermeture de la fenêtre."""
        if self._force_quit:
            return True
        if getattr(self, "remote_service", None) is None:
            return True
        if self._tray is not None:
            self.main_window.hide()
            if not self._tray_notice_shown:
                self._tray_notice_shown = True
                self._tray.showMessage(
                    "Zoom Auto Recorder",
                    "L'application continue en arrière-plan : la page de "
                    "contrôle reste accessible. Clic droit sur l'icône pour "
                    "quitter complètement.",
                    QSystemTrayIcon.Information,
                    8000,
                )
            self.logger.info(
                "Fenêtre fermée : contrôle à distance maintenu en arrière-plan."
            )
            return False
        # Pas de zone de notification : réduire au lieu de quitter.
        self.main_window.showMinimized()
        self.logger.warning(
            "Zone de notification indisponible : fenêtre réduite pour garder "
            "le contrôle à distance actif."
        )
        return False

    def _quit_from_tray(self):
        message = (
            "Quitter complètement l'application ?\n\n"
            "La page de contrôle à distance ne sera plus accessible tant que "
            "l'application n'est pas relancée."
        )
        if self._recording_in_progress():
            message = (
                "Un enregistrement est en cours.\n\n"
                "Quitter complètement l'application arrêtera cet "
                "enregistrement et coupera la page de contrôle à distance."
            )
        answer = QMessageBox.question(
            self.main_window, "Quitter Zoom Auto Recorder", message,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._force_quit = True
        self.logger.info("Arrêt complet demandé depuis la zone de notification.")
        self._stop_remote_service()
        self._destroy_tray()
        try:
            self.main_window.close()
        except Exception:
            pass
        self.app.quit()

    def _poll_remote_actions(self):
        """Traite les ordres reçus (thread graphique uniquement)."""
        service = getattr(self, "remote_service", None)
        if service is None:
            return
        try:
            actions = service.poll()
        except Exception as e:
            self.logger.debug(f"File de contrôle à distance : {e}")
            return
        for action in actions:
            kind = action.get("type")
            if kind == "order":
                self._handle_remote_order(action.get("order"))
            elif kind == "stop":
                self.logger.info("⏹ Arrêt demandé à distance.")
                self.stop_current_recording()

    def _remote_busy(self) -> bool:
        status = ""
        try:
            status = self.auto_engine.get_recording_status()
        except Exception:
            pass
        if status in ("recording", "paused"):
            return True
        if self._start_thread and self._start_thread.is_alive():
            return True
        if self._stop_thread and self._stop_thread.is_alive():
            return True
        meeting = getattr(self, "current_meeting", None)
        if meeting is not None:
            active_states = (
                STATE_LAUNCHING_ZOOM, STATE_JOINING_MEETING, STATE_WAITING_ROOM,
                STATE_IN_MEETING, STATE_RECORDING, STATE_PAUSED,
            )
            if meeting.status in active_states:
                return True
        return False

    def _handle_remote_order(self, order):
        """Crée la réunion demandée à distance puis la lance (ou la planifie)."""
        if order is None:
            return
        if self._remote_busy():
            self.logger.warning(
                "Ordre distant ignoré : une réunion est déjà en cours (%s).",
                order.meeting_id,
            )
            self.remote_last_order = {
                "status": "ignored",
                "meeting_id": order.meeting_id,
                "reason": "une réunion est déjà en cours",
            }
            return

        now = datetime.now()
        start = order.start_at or now
        title = (order.title or order.display_name or "Réunion à distance").strip()
        display_name = (order.display_name or title or "AutoRecorder").strip()

        meeting = Meeting(
            name=title,
            display_name=display_name,
            meeting_id=order.meeting_id,
            passcode=order.passcode,
            start_time=start.isoformat(timespec="minutes"),
            duration_minutes=max(1, int(order.duration_minutes or 60)),
            platform=order.platform or "zoom",
            is_enabled=True,
            status=STATE_WAITING,
            mic_on_start=False,
            camera_on_start=False,
            chat_open=True,
        )
        try:
            meeting.id = self.db.add_meeting(meeting)
        except Exception as e:
            self.logger.error(f"Ordre distant : création de la réunion impossible : {e}")
            self.remote_last_order = {
                "status": "error",
                "meeting_id": order.meeting_id,
                "reason": str(e),
            }
            return

        self.remote_last_order = {
            "status": "scheduled" if order.start_at else "accepted",
            "meeting_id": order.meeting_id,
            "title": title,
            "platform": order.platform,
            "source": order.source,
            "start_at": start.isoformat(timespec="minutes"),
        }
        self.logger.info(
            "🌐 Ordre distant (%s) : « %s » — %s %s",
            order.source, title, order.platform, order.meeting_id,
        )

        # « Ouvrir l'application » : la fenêtre réduite est restaurée dès qu'un
        # ordre arrive (le mode --remote-server démarre minimisé).
        try:
            self.main_window.showNormal()
            self.main_window.raise_()
            self.main_window.activateWindow()
        except Exception:
            pass

        self.load_meetings()
        self.refresh_dashboard()
        self._sync_windows_tasks()

        if order.start_at and order.start_at > now + timedelta(seconds=60):
            self.logger.info(
                "Ordre distant planifié : « %s » le %s.",
                title, order.start_at.strftime("%d/%m/%Y à %H:%M"),
            )
            return
        self.start_meeting(meeting)

    def _remote_status(self) -> dict:
        status = ""
        elapsed = 0
        try:
            status = self.auto_engine.get_recording_status()
            elapsed = self.auto_engine.get_recording_elapsed()
        except Exception:
            pass
        meeting = getattr(self, "current_meeting", None)
        return {
            "state": meeting.status if meeting else "En veille",
            "meeting": meeting.name if meeting else "",
            "platform": (getattr(meeting, "platform", "") if meeting else ""),
            "recording": status in ("recording", "paused"),
            "paused": status == "paused",
            "elapsed": elapsed,
            "busy": self._remote_busy(),
            "last_order": self.remote_last_order,
        }

    def _remote_recordings(self) -> list:
        try:
            recordings = self.db.get_all_recordings()
        except Exception:
            return []
        items = []
        for recording in recordings:
            path = Path(recording.file_path) if recording.file_path else None
            items.append({
                "id": recording.id,
                "meeting_name": recording.meeting_name,
                "start_time": recording.start_time,
                "duration_seconds": recording.duration_seconds,
                "status": recording.status,
                "exists": bool(path and path.is_file()),
            })
        return items

    def _remote_download_path(self, recording_id: int):
        try:
            recordings = self.db.get_all_recordings()
        except Exception:
            return None
        for recording in recordings:
            if recording.id == recording_id and recording.file_path:
                path = Path(recording.file_path)
                if path.is_file():
                    return str(path)
        return None

    def _start_auto_meeting(self):
        """Démarre la réunion demandée par le Planificateur Windows.

        Les contrôles sont les mêmes que dans le lanceur sans interface
        (`src/scheduled_runner.py`), mais le démarrage passe par l'interface
        graphique : la fenêtre est réduite dans la barre des tâches et
        l'utilisateur peut arrêter l'application à la fin.
        """
        meeting_id = self.auto_meeting_id
        if meeting_id is None:
            return
        self.auto_meeting_id = None

        try:
            meeting = self.db.get_meeting_by_id(meeting_id)
        except Exception as e:
            self.logger.error(f"Réunion planifiée {meeting_id} illisible : {e}")
            self._remove_auto_task(meeting_id)
            return

        if not meeting:
            self.logger.error(f"Réunion planifiée {meeting_id} introuvable en base.")
            self._remove_auto_task(meeting_id)
            return

        if not meeting.is_enabled:
            self.logger.warning(f"Réunion planifiée désactivée : {meeting.name}.")
            self._remove_auto_task(meeting_id)
            return

        try:
            start = datetime.fromisoformat(meeting.start_time)
        except (TypeError, ValueError):
            self.logger.error(f"Date invalide pour la réunion planifiée '{meeting.name}'.")
            self._remove_auto_task(meeting_id)
            return

        now = datetime.now()
        if now > start + timedelta(minutes=MISSED_WINDOW_MINUTES):
            self.logger.warning(
                f"Fenêtre de démarrage dépassée pour '{meeting.name}' "
                f"(prévue à {start:%d/%m %H:%M})."
            )
            meeting.status = STATE_ERROR
            try:
                self.db.update_meeting(meeting)
            except Exception:
                pass
            self._remove_auto_task(meeting_id)
            self._update_status()
            self.refresh_dashboard()
            return

        self.logger.info(f"Lancement planifié : {meeting.name}")
        self.start_meeting(meeting)

    # ──────────────────────────────────────────────────────────
    def setup_ui(self):
        central = QWidget()
        self.main_window.setCentralWidget(central)
        main = QVBoxLayout(central)

        # Header
        hdr = QHBoxLayout()
        title_lbl = QLabel("🎥 Zoom Auto Recorder & Meeting Assistant")
        title_lbl.setStyleSheet("font-size:18px; font-weight:bold; color:#222; padding:10px;")
        hdr.addWidget(title_lbl)
        hdr.addStretch()
        self.status_label = QLabel("État : En veille")
        self.status_label.setStyleSheet("font-size:13px; color:#555; padding:8px 14px; background:#e9ecef; border-radius:4px;")
        hdr.addWidget(self.status_label)
        main.addLayout(hdr)

        # Barre de garde-fou permanente : les deux boutons restent visibles
        # en permanence, même hors enregistrement, pour pouvoir intervenir
        # à n'importe quel moment.
        self.guard_bar = QFrame()
        self.guard_bar.setStyleSheet(
            "QFrame { background:#eef4f8; border:1px solid #c9dbe8; border-radius:6px; }"
            " QLabel { border:none; background:transparent; }"
            " QPushButton { color:white; border:none; border-radius:4px;"
            " padding:5px 12px; font-size:12px; font-weight:bold; }"
        )
        guard_lay = QHBoxLayout(self.guard_bar)
        guard_lay.setContentsMargins(10, 6, 10, 6)
        guard_lay.setSpacing(10)

        self.guard_state_lbl = QLabel()
        self.guard_state_lbl.setStyleSheet("font-size:12px; font-weight:bold; color:#555;")
        guard_lay.addWidget(self.guard_state_lbl)
        guard_lay.addStretch()

        self.guard_pause_btn = QPushButton("⏸ Suspendre la garde")
        self.guard_pause_btn.setStyleSheet("background:#e67e22;")
        self.guard_pause_btn.clicked.connect(self.suspend_popup_guard)
        guard_lay.addWidget(self.guard_pause_btn)

        self.guard_resume_btn = QPushButton("🛡 Réactiver la garde")
        self.guard_resume_btn.setStyleSheet("background:#17a2b8;")
        self.guard_resume_btn.clicked.connect(self.resume_popup_guard)
        guard_lay.addWidget(self.guard_resume_btn)

        main.addWidget(self.guard_bar)

        # Recording control bar
        self.rec_control = RecordingControlWidget(self)
        self.rec_control.setVisible(False)
        main.addWidget(self.rec_control)

        # Tabs
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_dashboard(), "📊 Tableau de bord")
        self.tabs.addTab(self._build_meetings(), "📅 Réunions")
        self.tabs.addTab(self._build_recordings(), "🎬 Enregistrements")
        self.tabs.addTab(self._build_settings(), "⚙️ Paramètres")
        main.addWidget(self.tabs)

        # Etat initial de la barre de garde (boutons toujours visibles).
        self.update_guard_controls()

    # ──────────────────────────────────────────────────────────
    def _build_dashboard(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("📌 Prochaines réunions"))
        self.dash_list = QListWidget()
        self.dash_list.setStyleSheet("font-size:12px;")
        lay.addWidget(self.dash_list)

        ctrl = QHBoxLayout()
        b = QPushButton("🔄 Rafraîchir")
        b.clicked.connect(self.refresh_dashboard)
        ctrl.addWidget(b)
        ctrl.addStretch()
        b2 = QPushButton("➕ Ajouter une réunion")
        b2.setStyleSheet("background:#28a745;color:white;font-size:13px;padding:8px 16px;border:none;border-radius:6px;")
        b2.clicked.connect(self.open_add_meeting)
        ctrl.addWidget(b2)
        lay.addLayout(ctrl)

        lay.addWidget(QLabel("📝 Journal d'activité"))
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("font-family:Consolas,monospace;font-size:11px;background:#1e1e1e;color:#d4d4d4;border:1px solid #444;")
        lay.addWidget(self.log_text)
        self.refresh_dashboard()
        return w

    def _build_meetings(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        b = QPushButton("➕ Ajouter une réunion")
        b.setStyleSheet("background:#28a745;color:white;font-size:13px;padding:8px 16px;border:none;border-radius:6px;")
        b.clicked.connect(self.open_add_meeting)
        top.addWidget(b)
        tasks_btn = QPushButton("🗓️ Tâches Windows")
        tasks_btn.setStyleSheet("background:#6c757d;color:white;font-size:13px;padding:8px 16px;border:none;border-radius:6px;")
        tasks_btn.clicked.connect(self.open_windows_tasks)
        top.addWidget(tasks_btn)
        top.addStretch()
        b2 = QPushButton("🔄 Rafraîchir")
        b2.clicked.connect(self.load_meetings)
        top.addWidget(b2)
        lay.addLayout(top)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self.meeting_layout = QVBoxLayout(container)
        self.meeting_layout.setAlignment(Qt.AlignTop)
        scroll.setWidget(container)
        lay.addWidget(scroll)
        self.load_meetings()
        return w

    def _build_recordings(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("🎬 Enregistrements"), alignment=Qt.AlignLeft)

        self.rec_table = QTableWidget()
        self.rec_table.setColumnCount(7)
        self.rec_table.setHorizontalHeaderLabels(["# ID", "Réunion", "Date", "Début", "Durée", "Fichier", "Actions"])
        self.rec_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.rec_table.setAlternatingRowColors(True)
        self.rec_table.setStyleSheet("font-size:11px;")
        lay.addWidget(self.rec_table)

        b = QPushButton("🔄 Rafraîchir")
        b.clicked.connect(self.load_recordings)
        lay.addWidget(b)
        self.load_recordings()
        return w

    def _build_settings(self):
        w = QWidget()
        outer = QVBoxLayout(w)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        lay = QVBoxLayout(container)
        lay.addWidget(QLabel("⚙️ Paramètres généraux"))

        engine = get_recording_engine()

        # ── Enregistreur ──────────────────────────────────────
        engine_box = QGroupBox("🎙️ Enregistreur vidéo")
        engine_lay = QVBoxLayout(engine_box)

        row = QHBoxLayout()
        row.addWidget(QLabel("Enregistreur utilisé pour les réunions :"))
        self.engine_combo = QComboBox()
        self.engine_combo.addItem(ENGINE_LABELS[ENGINE_KAMELREC], ENGINE_KAMELREC)
        self.engine_combo.addItem(ENGINE_LABELS[ENGINE_WINDOWS_GAMEBAR], ENGINE_WINDOWS_GAMEBAR)
        idx = self.engine_combo.findData(engine)
        self.engine_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.engine_combo.setMinimumWidth(240)
        row.addWidget(self.engine_combo)

        save_btn = QPushButton("💾 Appliquer")
        save_btn.setStyleSheet(
            "background:#007acc;color:white;font-size:12px;padding:6px 14px;"
            "border:none;border-radius:4px;"
        )
        save_btn.clicked.connect(self.save_recording_engine)
        row.addWidget(save_btn)
        row.addStretch()
        engine_lay.addLayout(row)

        self.engine_info_lbl = QLabel()
        self.engine_info_lbl.setWordWrap(True)
        self.engine_info_lbl.setStyleSheet("color:#555;font-size:11px;")
        engine_lay.addWidget(self.engine_info_lbl)
        lay.addWidget(engine_box)

        # ── Fenêtres pendant l'enregistrement ─────────────────
        guard_box = QGroupBox("🪟 Fenêtres pendant l'enregistrement")
        guard_lay = QVBoxLayout(guard_box)
        self.close_popups_cb = QCheckBox(
            "Fermer automatiquement les fenêtres qui s'ouvrent au-dessus de la réunion"
        )
        self.close_popups_cb.setChecked(get_close_popups_during_recording())
        self.close_popups_cb.toggled.connect(self.save_close_popups)
        guard_lay.addWidget(self.close_popups_cb)
        guard_info = QLabel(
            "Les boîtes d'alerte (messages d'enregistrement, notifications) sont fermées "
            "dès leur apparition et les fenêtres normales sont réduites dans la barre des "
            "tâches. La fenêtre de la réunion, l'application et KamelRec ne sont jamais touchés."
        )
        guard_info.setWordWrap(True)
        guard_info.setStyleSheet("color:#555;font-size:11px;")
        guard_lay.addWidget(guard_info)
        lay.addWidget(guard_box)

        self.settings_text = QTextEdit()
        self.settings_text.setReadOnly(True)
        self.settings_text.setMinimumHeight(130)
        self.settings_text.setMaximumHeight(240)
        self.settings_text.setStyleSheet(
            "font-size:12px;background:#f9f9f9;border:1px solid #ddd;"
            "border-radius:6px;padding:12px;"
        )
        self.settings_text.setHtml(self._settings_help_html(engine))
        lay.addWidget(self.settings_text)

        # ── Contrôle à distance ───────────────────────────────
        self._build_remote_settings(lay)

        self._refresh_engine_info(engine)

        scroll.setWidget(container)
        outer.addWidget(scroll)
        return w

    # ── Contrôle à distance : interface des paramètres ───────
    def _build_remote_settings(self, parent_layout):
        config = get_remote_config()
        token = get_remote_web_token(create=True)

        box = QGroupBox("🌐 Contrôle à distance (page web + e-mail Gmail)")
        lay = QVBoxLayout(box)

        intro = QLabel(
            "Recevez un lien Zoom / Google Meet depuis un téléphone ou un e-mail : "
            "l'application s'ouvre, rejoint la réunion et démarre l'enregistrement."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color:#555;font-size:11px;")
        lay.addWidget(intro)

        # ── Page web ──────────────────────────────────────────
        self.remote_web_cb = QCheckBox(
            "Activer la page web de contrôle à distance (accessible via Tailscale ou le réseau local)"
        )
        self.remote_web_cb.setChecked(config.get("web_enabled", False))
        lay.addWidget(self.remote_web_cb)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Port :"))
        self.remote_port_spin = QSpinBox()
        self.remote_port_spin.setRange(1024, 65535)
        self.remote_port_spin.setValue(config.get("web_port", 8080))
        self.remote_port_spin.setMinimumWidth(90)
        port_row.addWidget(self.remote_port_spin)

        port_row.addWidget(QLabel("Jeton d'accès :"))
        self.remote_token_edit = QLineEdit(token)
        self.remote_token_edit.setReadOnly(True)
        self.remote_token_edit.setMinimumWidth(230)
        # Police à chasse fixe : I (i majuscule) et l (L minuscule) restent
        # distinguables à l'écran.
        self.remote_token_edit.setStyleSheet(
            "font-family: Consolas, 'Courier New', monospace; font-size:12px;"
        )
        self.remote_token_edit.setToolTip(
            "Jeton d'accès. Utilisez « Copier le jeton » pour éviter toute erreur de lecture."
        )
        port_row.addWidget(self.remote_token_edit)

        copy_btn = QPushButton("📋 Copier le jeton")
        copy_btn.clicked.connect(self.copy_remote_token)
        port_row.addWidget(copy_btn)

        regen_btn = QPushButton("🔄 Régénérer")
        regen_btn.clicked.connect(self.regenerate_remote_token)
        port_row.addWidget(regen_btn)
        port_row.addStretch()
        lay.addLayout(port_row)

        link_row = QHBoxLayout()
        copy_url_btn = QPushButton("📋 Copier le lien complet")
        copy_url_btn.setToolTip("Copie l'adresse complète, jeton inclus.")
        copy_url_btn.clicked.connect(self.copy_remote_url)
        link_row.addWidget(copy_url_btn)
        open_page_btn = QPushButton("🌐 Ouvrir la page de contrôle")
        open_page_btn.clicked.connect(self.open_remote_page)
        link_row.addWidget(open_page_btn)
        link_row.addStretch()
        lay.addLayout(link_row)

        self.remote_urls_lbl = QLabel()
        self.remote_urls_lbl.setWordWrap(True)
        # Sélectionnable à la souris + chasse fixe : on peut copier l'adresse
        # directement depuis le libellé si le bouton n'est pas utilisable.
        self.remote_urls_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.remote_urls_lbl.setStyleSheet(
            "font-size:11px;color:#005a9e;font-family: Consolas, 'Courier New', monospace;"
        )
        lay.addWidget(self.remote_urls_lbl)
        self._refresh_remote_urls()

        # ── E-mail Gmail ──────────────────────────────────────
        self.remote_email_cb = QCheckBox(
            "Activer le déclenchement par e-mail (Gmail IMAP)"
        )
        self.remote_email_cb.setChecked(config.get("email_enabled", False))
        lay.addWidget(self.remote_email_cb)

        email_grid = QGridLayout()
        email_grid.setHorizontalSpacing(8)
        email_grid.setVerticalSpacing(6)

        email_grid.addWidget(QLabel("Adresse Gmail :"), 0, 0)
        self.remote_email_edit = QLineEdit(config.get("email_address", ""))
        self.remote_email_edit.setPlaceholderText("moncompte@gmail.com")
        email_grid.addWidget(self.remote_email_edit, 0, 1, 1, 3)

        email_grid.addWidget(QLabel("Mot de passe d'application :"), 1, 0)
        self.remote_email_password_edit = QLineEdit()
        self.remote_email_password_edit.setEchoMode(QLineEdit.Password)
        if config.get("email_password"):
            self.remote_email_password_edit.setPlaceholderText(
                "•••••••• (déjà enregistré — laissez vide pour conserver)"
            )
        else:
            self.remote_email_password_edit.setPlaceholderText("16 caractères, sans espaces")
        email_grid.addWidget(self.remote_email_password_edit, 1, 1, 1, 3)

        email_grid.addWidget(QLabel("Mot-clé :"), 2, 0)
        self.remote_keyword_edit = QLineEdit(config.get("email_keyword", "record"))
        self.remote_keyword_edit.setToolTip(
            "L'e-mail doit contenir ce mot (objet ou corps). Exemple : record, zoom, تسجيل"
        )
        email_grid.addWidget(self.remote_keyword_edit, 2, 1)

        email_grid.addWidget(QLabel("Expéditeurs autorisés :"), 2, 2)
        senders = config.get("email_senders", [])
        self.remote_senders_edit = QLineEdit(", ".join(senders))
        self.remote_senders_edit.setPlaceholderText("(vide = tous) — séparés par des virgules")
        email_grid.addWidget(self.remote_senders_edit, 2, 3)

        email_grid.addWidget(QLabel("Vérification toutes les :"), 3, 0)
        self.remote_poll_spin = QSpinBox()
        self.remote_poll_spin.setRange(15, 3600)
        self.remote_poll_spin.setSuffix(" s")
        self.remote_poll_spin.setValue(config.get("email_poll_seconds", 60))
        email_grid.addWidget(self.remote_poll_spin, 3, 1)

        test_btn = QPushButton("✉️ Tester la connexion Gmail")
        test_btn.clicked.connect(self.test_remote_email)
        email_grid.addWidget(test_btn, 3, 2, 1, 2)
        lay.addLayout(email_grid)

        email_help = QLabel(
            "Gmail exige un <b>mot de passe d'application</b> (compte avec validation en "
            "deux étapes) : Compte Google → Sécurité → Validation en deux étapes → "
            "Mots de passe des applications. L'e-mail doit contenir le mot-clé et un "
            "lien Zoom / Meet (ou un ID). Exemple de corps : "
            "<code>record https://zoom.us/j/1234567890?pwd=abc123 nom: Kamel</code>"
        )
        email_help.setWordWrap(True)
        email_help.setStyleSheet("color:#555;font-size:11px;")
        lay.addWidget(email_help)

        # ── Démarrage automatique ─────────────────────────────
        self.remote_autostart_cb = QCheckBox(
            "Démarrer le contrôle à distance à l'ouverture de session Windows "
            "(l'application démarre en arrière-plan, icône près de l'horloge)"
        )
        self.remote_autostart_cb.setChecked(get_remote_autostart())
        lay.addWidget(self.remote_autostart_cb)

        close_help = QLabel(
            "Fermer la fenêtre (croix) ne coupe pas la page web : l'application "
            "reste active dans la zone de notification. Pour tout arrêter : clic "
            "droit sur l'icône → « Quitter complètement »."
        )
        close_help.setWordWrap(True)
        close_help.setStyleSheet("color:#555;font-size:11px;")
        lay.addWidget(close_help)

        save_row = QHBoxLayout()
        save_btn = QPushButton("💾 Enregistrer le contrôle à distance")
        save_btn.setStyleSheet(
            "background:#2f855a;color:white;font-size:12px;padding:7px 16px;"
            "border:none;border-radius:4px;font-weight:bold;"
        )
        save_btn.clicked.connect(self.save_remote_settings)
        save_row.addWidget(save_btn)
        save_row.addStretch()
        lay.addLayout(save_row)

        self.remote_info_lbl = QLabel()
        self.remote_info_lbl.setWordWrap(True)
        self.remote_info_lbl.setStyleSheet("font-size:11px;color:#555;")
        lay.addWidget(self.remote_info_lbl)

        parent_layout.addWidget(box)
        self._refresh_remote_info()

    def _refresh_remote_urls(self):
        if not hasattr(self, "remote_urls_lbl"):
            return
        port = self.remote_port_spin.value() if hasattr(self, "remote_port_spin") else 8080
        token = self.remote_token_edit.text() if hasattr(self, "remote_token_edit") else ""
        service = getattr(self, "remote_service", None)
        if service is not None and service.is_web_running():
            urls = service.web_urls(include_token=True)
        else:
            from src.remote_control import _local_hosts
            urls = [f"http://{host}:{port}/?token={token}" for host in _local_hosts()]
        # Adresse principale : celle qui fonctionne depuis un autre appareil
        # (première adresse réseau, pas 127.0.0.1).
        primary = ""
        for url in urls:
            if "127.0.0.1" not in url and "localhost" not in url:
                primary = url
                break
        if not primary and urls:
            primary = urls[0]
        self._remote_primary_url = primary
        if urls:
            self.remote_urls_lbl.setText(
                "Adresse à ouvrir depuis le téléphone :\n" + "\n".join(urls)
            )
        else:
            self.remote_urls_lbl.setText(
                "Adresse : http://<nom-du-PC>:%d/?token=%s" % (port, token)
            )

    def _refresh_remote_info(self):
        if not hasattr(self, "remote_info_lbl"):
            return
        service = getattr(self, "remote_service", None)
        if service is None:
            self.remote_info_lbl.setText(
                "État : arrêté. Cochez la page web et/ou l'e-mail puis enregistrez."
            )
            return
        parts = []
        if getattr(service, "web_external", False):
            parts.append("Page web : servie par une autre instance (même jeton)")
        else:
            parts.append("Page web : " + ("active" if service.is_web_running() else "arrêtée"))
        parts.append("E-mail : " + ("actif" if service.is_email_running() else "arrêté"))
        if service.last_error:
            parts.append("Erreur : " + service.last_error)
        self.remote_info_lbl.setText("État : " + " • ".join(parts))

    def copy_remote_token(self):
        token = self.remote_token_edit.text().strip()
        if not token:
            QMessageBox.warning(
                self.main_window, "Aucun jeton",
                "Aucun jeton d'accès n'est disponible. Cliquez sur « 🔄 Régénérer »."
            )
            return
        if copy_text(token):
            self.logger.info("Jeton de contrôle à distance copié dans le presse-papiers.")
            QMessageBox.information(
                self.main_window, "Jeton copié",
                "Le jeton d'accès a bien été copié dans le presse-papiers.\n\n"
                "Collez-le dans le champ « Jeton d'accès » de la page web "
                "(une seule fois : le navigateur le mémorise ensuite)."
            )
        else:
            self.logger.warning("Échec de la copie du jeton dans le presse-papiers.")
            QMessageBox.warning(
                self.main_window, "Copie impossible",
                "Le presse-papiers Windows est indisponible (verrouillé par un autre "
                "programme).\n\nJeton à recopier manuellement :\n\n" + token
            )

    def copy_remote_url(self):
        url = getattr(self, "_remote_primary_url", "") or ""
        if not url:
            self._refresh_remote_urls()
            url = getattr(self, "_remote_primary_url", "") or ""
        if not url:
            QMessageBox.warning(
                self.main_window, "Aucune adresse",
                "Aucune adresse web n'est disponible. Activez la page web puis "
                "enregistrez les paramètres."
            )
            return
        if copy_text(url):
            self.logger.info("Lien de contrôle à distance copié : %s", url)
            QMessageBox.information(
                self.main_window, "Lien copié",
                "Le lien complet (jeton inclus) a été copié :\n\n" + url +
                "\n\nOuvrez-le depuis votre téléphone (même réseau Wi-Fi ou Tailscale)."
            )
        else:
            self.logger.warning("Échec de la copie du lien de contrôle à distance.")
            QMessageBox.warning(
                self.main_window, "Copie impossible",
                "Le presse-papiers Windows est indisponible.\n\nLien à recopier "
                "manuellement :\n\n" + url
            )

    def open_remote_page(self):
        url = getattr(self, "_remote_primary_url", "") or ""
        if not url:
            self._refresh_remote_urls()
            url = getattr(self, "_remote_primary_url", "") or ""
        if not url:
            QMessageBox.warning(
                self.main_window, "Aucune adresse",
                "Aucune adresse web n'est disponible. Activez la page web puis "
                "enregistrez les paramètres."
            )
            return
        QDesktopServices.openUrl(QUrl(url))
        self.logger.info("Ouverture de la page de contrôle : %s", url)

    def regenerate_remote_token(self):
        answer = QMessageBox.question(
            self.main_window, "Régénérer le jeton",
            "Régénérer le jeton ? L'ancien lien ne fonctionnera plus.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        token = regenerate_remote_web_token()
        self.remote_token_edit.setText(token)
        self._refresh_remote_urls()
        self._start_remote_service()
        self._refresh_remote_info()
        self.logger.info("Nouveau jeton de contrôle à distance généré.")

    def test_remote_email(self):
        address = self.remote_email_edit.text().strip()
        password = self.remote_email_password_edit.text().strip()
        if not password:
            password = get_remote_config().get("email_password", "")
        self.logger.info("Test de la connexion Gmail en cours…")
        ok, message = test_email_connection(address, password)
        if ok:
            self.logger.info("Contrôle par e-mail : connexion Gmail réussie.")
            QMessageBox.information(self.main_window, "Gmail", message)
        else:
            self.logger.warning("Contrôle par e-mail : %s", message)
            QMessageBox.warning(self.main_window, "Gmail", message)

    def save_remote_settings(self):
        web_enabled = self.remote_web_cb.isChecked()
        port = self.remote_port_spin.value()
        token = self.remote_token_edit.text().strip() or get_remote_web_token(create=True)
        email_enabled = self.remote_email_cb.isChecked()
        address = self.remote_email_edit.text().strip()
        password = self.remote_email_password_edit.text().strip()
        keyword = self.remote_keyword_edit.text().strip() or "record"
        senders = self.remote_senders_edit.text().strip()
        poll = self.remote_poll_spin.value()
        autostart = self.remote_autostart_cb.isChecked()

        # Première activation du contrôle à distance : activer aussi le
        # démarrage avec Windows, sinon le lien ne répond plus après un
        # redémarrage du PC. Le choix de l'utilisateur reste prioritaire
        # ensuite (s'il décoche la case, elle n'est plus réactivée).
        if ((web_enabled or email_enabled) and not autostart
                and not get_remote_autostart_default_done()):
            autostart = True
            self.remote_autostart_cb.setChecked(True)
            self.logger.info(
                "Contrôle à distance : démarrage automatique activé pour que "
                "la page reste joignable après un redémarrage."
            )
        if web_enabled or email_enabled:
            set_remote_autostart_default_done(True)

        if email_enabled and (not address or (not password and not get_remote_config().get("email_password"))):
            QMessageBox.warning(
                self.main_window, "Contrôle à distance",
                "Pour activer l'e-mail, renseignez l'adresse Gmail et le mot de passe "
                "d'application (ou testez d'abord la connexion).",
            )
            return

        set_remote_web_enabled(web_enabled)
        set_remote_web_port(port)
        set_remote_web_token(token)
        set_remote_email_enabled(email_enabled)
        set_remote_email_address(address)
        if password:
            set_remote_email_password(password)
        set_remote_email_keyword(keyword)
        set_remote_email_senders(senders)
        set_remote_email_poll_seconds(poll)
        set_remote_autostart(autostart)

        # Démarrage automatique à l'ouverture de session.
        if autostart:
            if not register_remote_autostart():
                QMessageBox.warning(
                    self.main_window, "Contrôle à distance",
                    "Impossible de créer le raccourci de démarrage automatique. "
                    "Le reste des paramètres a été enregistré.",
                )
        else:
            unregister_remote_autostart()

        self.remote_email_password_edit.clear()
        if password:
            self.remote_email_password_edit.setPlaceholderText(
                "•••••••• (déjà enregistré — laissez vide pour conserver)"
            )

        self._start_remote_service()
        self._update_tray(recreate=True)
        self._refresh_remote_urls()
        self._refresh_remote_info()
        self.logger.info(
            "Contrôle à distance enregistré : page web %s, e-mail %s, autostart %s.",
            "activée" if web_enabled else "désactivée",
            "activé" if email_enabled else "désactivé",
            "activé" if autostart else "désactivé",
        )
        QMessageBox.information(
            self.main_window, "Contrôle à distance",
            "Paramètres enregistrés.\n\n"
            + ("Page web : " + ("active" if (self.remote_service and self.remote_service.is_web_running()) else "arrêtée") + "\n")
            + ("E-mail : " + ("actif" if (self.remote_service and self.remote_service.is_email_running()) else "arrêté") + "\n")
            + ("Démarrage automatique : " + ("activé" if autostart else "désactivé"))
        )

    def _refresh_engine_info(self, engine: str) -> None:
        if not hasattr(self, "engine_info_lbl"):
            return
        if engine == ENGINE_WINDOWS_GAMEBAR:
            self.engine_info_lbl.setText(
                "Windows Game Bar : enregistre la fenêtre Zoom active (Win+Alt+R) avec l'audio. "
                "La pause n'est pas disponible. Le fichier MP4 est déplacé vers le dossier des "
                "enregistrements après l'arrêt. La Game Bar doit être activée dans Windows."
            )
        else:
            self.engine_info_lbl.setText(
                "KamelRec : enregistre la fenêtre Zoom avec l'audio système. "
                "Pause et reprise disponibles. KamelRec-1.0.0-Portable.exe doit être placé "
                "à côté de l'application (ou défini via ZOOM_AUTORECORDER_KAMELREC_EXE)."
            )

    def save_recording_engine(self):
        engine = self.engine_combo.currentData()
        if engine == get_recording_engine():
            QMessageBox.information(
                self.main_window, "Paramètres",
                "Cet enregistreur est déjà sélectionné."
            )
            return

        if set_recording_engine(engine):
            self._refresh_engine_info(engine)
            self.settings_text.setHtml(self._settings_help_html(engine))
            if getattr(self, "rec_control", None):
                self.rec_control.refresh_engine()
            self.logger.info(f"Enregistreur sélectionné : {ENGINE_LABELS.get(engine, engine)}")
            QMessageBox.information(
                self.main_window, "Paramètres",
                f"Enregistreur appliqué : {ENGINE_LABELS.get(engine, engine)}.\n"
                "Le prochain enregistrement utilisera ce moteur."
            )
        else:
            QMessageBox.critical(
                self.main_window, "Erreur",
                "Impossible d'enregistrer le paramètre."
            )

    def save_close_popups(self, enabled: bool):
        """Enregistre le choix de fermer les fenêtres pendant l'enregistrement."""
        if set_close_popups_during_recording(enabled):
            # Appliquer immediatement si un enregistrement est en cours.
            engine = getattr(self, "auto_engine", None)
            if engine is not None:
                try:
                    if enabled and engine.get_recording_status() in ("recording", "paused"):
                        engine.start_popup_guard()
                    elif not enabled:
                        engine.stop_popup_guard()
                except Exception:
                    pass
            self.logger.info(
                "Fermeture automatique des fenêtres pendant l'enregistrement : %s",
                "activée" if enabled else "désactivée",
            )
        else:
            QMessageBox.critical(
                self.main_window, "Erreur",
                "Impossible d'enregistrer le paramètre."
            )

    def _settings_help_html(self, engine: str) -> str:
        engine_label = ENGINE_LABELS.get(engine, engine)
        if engine == ENGINE_WINDOWS_GAMEBAR:
            recorder_help = (
                "<li><b>Game Bar</b> : Zoom est mis au premier plan puis <b>Win+Alt+R</b> démarre "
                "l'enregistrement. À l'arrêt, le MP4 est récupéré depuis <code>Vidéos\\Captures</code> "
                "et déplacé vers le dossier des enregistrements. La <b>pause n'est pas disponible</b>.</li>"
            )
            recorder_trouble = (
                "<li><b>Game Bar ne démarre pas</b> : ouvrez <b>Paramètres Windows → Jeux → Xbox Game Bar</b>, "
                "activez la Game Bar et « Enregistrer les clips de jeu ». Vérifiez aussi que la capture "
                "audio est autorisée et que Zoom est la fenêtre active.</li>"
            )
        else:
            recorder_help = (
                "<li><b>KamelRec</b> : enregistre la fenêtre Zoom avec l'audio système. "
                "<b>Pause</b> et <b>Reprendre</b> utilisent les boutons إيقاف مؤقت / استئناف de KamelRec.</li>"
            )
            recorder_trouble = (
                "<li><b>KamelRec</b> : le fichier <code>KamelRec-1.0.0-Portable.exe</code> doit être "
                "à côté de l'application ou accessible via <code>ZOOM_AUTORECORDER_KAMELREC_EXE</code>.</li>"
            )

        return f"""
            <h3>📋 Configuration par défaut</h3>
            <table cellpadding="4">
                <tr><td><b>Version</b></td><td>{APP_VERSION}</td></tr>
                <tr><td><b>Enregistreur sélectionné</b></td><td>{engine_label}</td></tr>
                <tr><td><b>Répertoire des enregistrements</b></td><td>{DEFAULT_RECORDINGS_DIR}</td></tr>
                <tr><td><b>Base de données</b></td><td>{DB_PATH}</td></tr>
                <tr><td><b>Journal d'activité</b></td><td>{LOG_DIR / 'app.log'}</td></tr>
                <tr><td><b>Qualité KamelRec</b></td><td>720p, 30 fps, qualité medium, MP4/AAC</td></tr>
                <tr><td><b>Micro</b></td><td>Désactivé par défaut</td></tr>
                <tr><td><b>Haut-parleur Zoom</b></td><td>Identique au système (forcé automatiquement)</td></tr>
                <tr><td><b>Audio de la réunion</b></td><td>Rejoint automatiquement via l'audio de l'ordinateur</td></tr>
                <tr><td><b>Planification</b></td><td>Tâches Windows créées pour chaque réunion activée (application pouvant être fermée)</td></tr>
                <tr><td><b>Fenêtres pendant l'enregistrement</b></td><td>Fermées automatiquement (alertes) ou réduites (fenêtres normales)</td></tr>
                <tr><td><b>Vérification en cours d'exécution</b></td><td>Toutes les 10 secondes</td></tr>
                <tr><td><b>Fenêtre de démarrage acceptée</b></td><td>5 minutes après l'heure prévue</td></tr>
                <tr><td><b>Nom affiché par défaut</b></td><td>AutoRecorder</td></tr>
                <tr><td><b>Durée par défaut</b></td><td>60 minutes</td></tr>
            </table>
            <hr/>
            <h3>ℹ️ Comment utiliser l'application</h3>
            <ol>
                <li><b>Choisissez l'enregistreur</b> : menu ci-dessus → <b>KamelRec</b> ou <b>Windows Game Bar</b>, puis <b>💾 Appliquer</b>.</li>
                <li><b>Ajoutez une réunion</b> : onglet <b>Réunions</b> → <b>➕ Ajouter une réunion</b>.</li>
                <li><b>Renseignez</b> : Meeting ID/lien, mot de passe, nom affiché Zoom, date et heure, durée.</li>
                <li><b>Activez la réunion</b> : bouton <b>⏯ Activer/Désactiver</b> (✅ visible). Sans activation, aucune tâche Windows n'est créée.</li>
                <li><b>Gardez l'application ouverte</b> : le planificateur vérifie toutes les 10 secondes. Les tâches Windows permettent aussi de lancer la réunion avec l'application fermée.</li>
                <li><b>Lancement automatique</b> : Zoom démarre, le haut-parleur est réglé sur <b>Identique au système</b>, la réunion est rejointe, l'audio de l'ordinateur est activé puis l'enregistrement démarre.</li>
                {recorder_help}
                <li><b>Arrêt</b> : après la durée configurée, ou via <b>⏹ Arrêter</b> dans la barre d'enregistrement.</li>
                <li><b>Tâches Windows</b> : utilisez <b>🗓️ Tâches Windows</b> pour afficher ou annuler les tâches planifiées.</li>
                <li><b>Résultats</b> : onglet <b>Enregistrements</b> pour ouvrir ou supprimer les vidéos.</li>
            </ol>
            <hr/>
            <h3>🛠️ Dépannage</h3>
            <ul>
                <li><b>Réunion non lancée</b> : vérifiez que la réunion est activée (✅) et que le statut est <b>Waiting</b>.</li>
                <li><b>Application fermée</b> : une tâche Windows intervient si elle existe ; vérifiez-la dans <b>🗓️ Tâches Windows</b>.</li>
                <li><b>PC verrouillé/en veille</b> : Zoom ne peut pas être piloté ; la session Windows doit être ouverte et déverrouillée.</li>
                {recorder_trouble}
                <li><b>Zoom introuvable</b> : définissez <code>ZOOM_AUTORECORDER_ZOOM_EXE</code> avec le chemin complet vers <code>Zoom.exe</code>.</li>
                <li><b>Enregistrement sans audio</b> : le haut-parleur Zoom est forcé sur <b>Identique au système</b> et l'audio de l'ordinateur est rejoint automatiquement avant l'enregistrement.</li>
                <li><b>Journal</b> : <code>{LOG_DIR / 'app.log'}</code></li>
                <li><b>Base de données</b> : {DB_PATH}</li>
            </ul>
        """

    # ──────────────────────────────────────────────────────────
    def refresh_dashboard(self):
        meetings = self.db.get_all_meetings()
        self.dash_list.clear()
        for m in meetings:
            dt = QDateTime.fromString(m.start_time, Qt.ISODate)
            icon = "🟢" if m.is_enabled else "⏸️"
            self.dash_list.addItem(f"{icon} {m.name} — {dt.toString('dd/MM/yyyy HH:mm')} [{m.status}]")

    def load_meetings(self):
        while self.meeting_layout.count():
            item = self.meeting_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        meetings = self.db.get_all_meetings()
        if not meetings:
            lbl = QLabel("Aucune réunion programmée. Cliquez sur 'Ajouter' pour commencer.")
            lbl.setStyleSheet("color:#888;font-style:italic;padding:20px;")
            self.meeting_layout.addWidget(lbl)
        else:
            for m in meetings:
                self.meeting_layout.addWidget(MeetingRowWidget(m, self.db, self))
        self.refresh_dashboard()

    def open_windows_tasks(self):
        dialog = WindowsTasksDialog(self.db, self.main_window)
        dialog.exec()
        # Catch any change made by the dialog (meetings disabled/cancelled).
        self._sync_windows_tasks()
        self.load_meetings()

    def open_add_meeting(self):
        dialog = AddMeetingDialog(self.main_window)
        if dialog.exec():
            data = dialog.get_data()
            try:
                new_m = Meeting(**data)
                self.db.add_meeting(new_m)
                self._sync_windows_tasks()
                self.logger.info(f"Réunion ajoutée : {data['name']}")
                QMessageBox.information(self.main_window, "Succès",
                                        f"La réunion '{data['name']}' a été ajoutée.")
                self.load_meetings()
            except Exception as e:
                self.logger.error(f"Erreur ajout : {e}")
                QMessageBox.critical(self.main_window, "Erreur", str(e))

    def load_recordings(self):
        # First scan the folder for any recordings not in the database
        self.db.scan_recordings_folder()

        # Then load from database
        recordings = self.db.get_all_recordings()
        self.rec_table.setRowCount(len(recordings))
        for i, r in enumerate(recordings):
            self.rec_table.setItem(i, 0, QTableWidgetItem(str(r.id or "")))
            self.rec_table.setItem(i, 1, QTableWidgetItem(r.meeting_name))
            self.rec_table.setItem(i, 2, QTableWidgetItem(r.start_time[:10] if r.start_time else ""))
            self.rec_table.setItem(i, 3, QTableWidgetItem(r.start_time[11:16] if r.start_time else ""))
            dur = r.duration_seconds or 0
            h, rem = divmod(dur, 3600)
            m, s = divmod(rem, 60)
            self.rec_table.setItem(i, 4, QTableWidgetItem(f"{h:02d}:{m:02d}:{s:02d}"))
            fname = os.path.basename(r.file_path) if r.file_path else "—"
            self.rec_table.setItem(i, 5, QTableWidgetItem(fname))

            # Container for buttons
            btn_container = QWidget()
            btn_layout = QHBoxLayout(btn_container)
            btn_layout.setContentsMargins(0, 0, 0, 0)
            btn_layout.setSpacing(5)

            # Open button
            btn_open = QPushButton("📂")
            btn_open.setStyleSheet("font-size:10px;padding:3px 8px;background:#17a2b8;color:white;border:none;border-radius:3px;")
            btn_open.clicked.connect(lambda _, p=r.file_path: self._open_file(p))
            btn_layout.addWidget(btn_open)

            # Delete button
            btn_del = QPushButton("🗑️")
            btn_del.setStyleSheet("font-size:10px;padding:3px 8px;background:#dc3545;color:white;border:none;border-radius:3px;")
            btn_del.clicked.connect(lambda _, rid=r.id, fp=r.file_path: self._delete_recording(rid, fp))
            btn_layout.addWidget(btn_del)

            self.rec_table.setCellWidget(i, 6, btn_container)

    def _open_file(self, path):
        if path and os.path.exists(path):
            os.startfile(path)

    def _delete_recording(self, recording_id: int, file_path: str):
        """Delete a recording from database and disk."""
        try:
            from PySide6.QtWidgets import QMessageBox

            reply = QMessageBox.question(
                self.main_window,
                "Supprimer l'enregistrement",
                f"Êtes-vous sûr de vouloir supprimer cet enregistrement ?\n\n{os.path.basename(file_path) if file_path else ''}",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )

            if reply == QMessageBox.Yes:
                # Delete from database
                self.db.delete_recording(recording_id)

                # Delete file from disk
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)
                    logger.info(f"🗑️ Fichier supprimé: {file_path}")

                # Refresh table
                self.load_recordings()
                logger.info(f"✅ Enregistrement #{recording_id} supprimé")

        except Exception as e:
            logger.error(f"Erreur suppression enregistrement: {e}")
            QMessageBox.critical(self.main_window, "Erreur", f"Impossible de supprimer l'enregistrement:\n{e}")

    # ──────────────────────────────────────────────────────────
    @property
    def scheduler(self):
        return MeetingScheduler(self.db)

    def check_schedule(self):
        meeting = self.scheduler.get_due_meeting()
        if meeting:
            if self._meeting_key(meeting) in self._finished_meetings:
                # Deja lancee pour cette occurrence : ne pas la relancer en
                # boucle apres un arret manuel ou un echec.
                return
            self.start_meeting(meeting)

    @staticmethod
    def _meeting_key(meeting):
        """Identifie une occurrence precise d'une reunion (id + heure prevue)."""
        return (meeting.id, (meeting.start_time or "").strip())

    def _mark_meeting_finished(self, meeting):
        try:
            self._finished_meetings.add(self._meeting_key(meeting))
        except Exception:
            pass

    def start_meeting(self, meeting):
        """Start a meeting in a worker thread (join + record can take minutes)."""
        if self._start_thread and self._start_thread.is_alive():
            self.logger.info("Une réunion est déjà en cours de démarrage.")
            return

        # Claim the meeting atomically so a Windows Task Scheduler instance
        # cannot start the same meeting at the same time.
        if not self.db.try_claim_meeting(meeting.id):
            # Si la reunion est dans un statut terminal (Error, Completed, etc.)
            # ou n'importe quel statut sauf vraiment en cours, on la remet en
            # attente pour autoriser un relancement manuel.
            current = self.db.get_meeting_by_id(meeting.id)
            if current and current.is_enabled:
                blocked_statuses = (STATE_LAUNCHING_ZOOM, "Starting")
                if current.status not in blocked_statuses:
                    self.logger.info(
                        f"Reinitialisation en attente pour relancement : {meeting.name} (etait {current.status})"
                    )
                    current.status = STATE_WAITING
                    self.db.update_meeting(current)
                    if self.db.try_claim_meeting(meeting.id):
                        self.logger.info(f"Reclamation reussie après reinitialisation : {meeting.name}")
                    else:
                        self.logger.info("Réunion déjà prise en charge : %s", meeting.name)
                        return
                else:
                    self.logger.info("Réunion déjà prise en charge : %s", meeting.name)
                    return
            else:
                self.logger.info("Réunion déjà prise en charge : %s", meeting.name)
                return

        # The one-shot Windows task is no longer needed.
        self._remove_auto_task(meeting.id)

        self.logger.info(f"=== Démarrage : {meeting.name} ===")
        meeting.status = STATE_LAUNCHING_ZOOM
        self.db.update_meeting(meeting)
        self.current_meeting = meeting
        # Auto-stop relative to the real start, not the scheduled time
        duration = getattr(meeting, "duration_minutes", 0) or 0
        self._recording_deadline = (
            datetime.now() + timedelta(minutes=duration) if duration > 0 else None
        )
        self._update_status()
        self.rec_control.setVisible(True)

        self._start_thread = threading.Thread(
            target=self._start_meeting_worker, args=(meeting,), daemon=True
        )
        self._start_thread.start()

    def _start_meeting_worker(self, meeting):
        status = STATE_ERROR
        try:
            platform = getattr(meeting, "platform", "zoom") or "zoom"
            if platform == "google_meet":
                # ══════════════════════════════════════════════════════
                # GOOGLE MEET (navigateur)
                # ═════════════════════════════════════════════════════
                meeting.status = STATE_LAUNCHING_ZOOM
                self.db.update_meeting(meeting)
                self.logger.info(f"=== Démarrage Google Meet : {meeting.name} ===")

                if self.auto_engine.launch_google_meet(meeting.meeting_id):
                    time.sleep(3)
                    meet_display_name = (meeting.display_name or meeting.name or "").strip() or "AutoRecorder"
                    result = self.auto_engine.join_google_meet(
                        meeting.meeting_id,
                        display_name=meet_display_name,
                        mic_on_start=meeting.mic_on_start,
                        camera_on_start=meeting.camera_on_start,
                    )
                    if result == STATE_IN_MEETING:
                        status = STATE_RECORDING
                        self.logger.info(f"🎬 Démarrage enregistrement : {meeting.name}")
                        if not self.auto_engine.start_recording():
                            status = STATE_IN_MEETING
                            self.logger.error("❌ Enregistrement non démarré.")
                    else:
                        self.logger.error(f"❌ Échec jointure Google Meet : {meeting.name}")
                else:
                    self.logger.error(f"❌ Navigateur non démarré : {meeting.name}")
            else:
                # ═══════════════════════════════════════════════════════
                # ZOOM (application bureau)
                # ═══════════════════════════════════════════════════════
                meeting.status = STATE_LAUNCHING_ZOOM
                self.db.update_meeting(meeting)
                self.logger.info(f"=== Démarrage : {meeting.name} ===")

                if self.auto_engine.launch_zoom():
                    time.sleep(3)
                    zoom_display_name = (meeting.display_name or meeting.name or "").strip() or "AutoRecorder"
                    result = self.auto_engine.join_meeting(
                        meeting.meeting_id,
                        meeting.passcode,
                        wait_for_join=True,
                        display_name=zoom_display_name,
                    )
                    if result == STATE_WAITING_ROOM:
                        status = STATE_WAITING_ROOM
                        self.logger.warning(f"⏳ Salle d'attente : {meeting.name}")
                    elif result == STATE_IN_MEETING:
                        status = STATE_RECORDING
                        self.logger.info(f"🎬 Démarrage enregistrement : {meeting.name}")
                        if not self.auto_engine.start_recording():
                            status = STATE_IN_MEETING
                            self.logger.error("❌ Enregistrement non démarré.")
                    else:
                        self.logger.error(f"❌ Échec jointure : {meeting.name}")
                else:
                    self.logger.error(f"❌ Zoom non démarré : {meeting.name}")
        except Exception as e:
            self.logger.error(f"Erreur démarrage réunion : {e}")
            status = STATE_ERROR

        meeting.status = status
        try:
            self.db.update_meeting(meeting)
        except Exception as e:
            self.logger.warning(f"Mise à jour réunion impossible : {e}")

        # En cas d'echec, remettre en attente pour permettre un relancement
        # (automatique dans la fenetre de 5 min, ou manuel).
        if status == STATE_ERROR:
            meeting.status = STATE_WAITING
            try:
                self.db.update_meeting(meeting)
            except Exception:
                pass
            self.logger.info(f"Réunion réinitialisée en attente : {meeting.name}")

        self.bridge.meeting_started.emit(meeting, status)

    def _on_meeting_started(self, meeting, status):
        self._update_status()
        self.refresh_dashboard()
        if status in (STATE_RECORDING, STATE_IN_MEETING):
            self.rec_control.setVisible(True)

    def _update_status(self):
        if self.current_meeting:
            self.status_label.setText(
                f"État : {self.current_meeting.status} — {self.current_meeting.name}"
            )
        else:
            self.status_label.setText("État : En veille")

    def _monitor_recording(self):
        """Stop the recording when the planned duration has elapsed."""
        if not self.current_meeting:
            return
        m = self.current_meeting
        if m.status not in (STATE_RECORDING, STATE_IN_MEETING):
            return
        # Google Meet : garder le navigateur en plein ecran pendant tout
        # l'enregistrement (Chrome peut en sortir apres une invite).
        if (getattr(m, "platform", "zoom") or "zoom") == "google_meet":
            try:
                self.auto_engine.keep_meeting_fullscreen()
            except Exception as e:
                self.logger.debug(f"Plein ecran Meet non verifie : {e}")
        deadline = getattr(self, "_recording_deadline", None)
        if deadline and datetime.now() >= deadline:
            self.logger.info(f"⏰ Durée atteinte — Arrêt de {m.name}")
            self.stop_current_recording()

    def stop_current_recording(self):
        """Stop recording in a worker thread (KamelRec needs time to finalize)."""
        if self._stop_thread and self._stop_thread.is_alive():
            return
        if self.auto_engine.get_recording_status() in ("recording", "paused"):
            self.logger.info("Arrêt de l'enregistrement demandé...")
            self._stop_thread = threading.Thread(target=self._stop_worker, daemon=True)
            self._stop_thread.start()
        else:
            self._finish_meeting()

    def _stop_worker(self):
        try:
            self.auto_engine.stop_recording()
        except Exception as e:
            self.logger.error(f"Erreur arrêt enregistrement : {e}")
        self.bridge.recording_stopped.emit()

    def _on_recording_stopped(self):
        self.load_recordings()
        self._finish_meeting()

    def _finish_meeting(self):
        self._recording_deadline = None
        if self.current_meeting:
            # Ne plus relancer automatiquement cette occurrence (l'utilisateur
            # peut toujours la relancer manuellement).
            self._mark_meeting_finished(self.current_meeting)
            # Remettre en attente pour permettre un nouveau lancement manuel
            self.current_meeting.status = STATE_WAITING
            self.db.update_meeting(self.current_meeting)
            self._sync_windows_tasks()
            self._update_status()
            self.rec_control.set_control_state("stopped")
            self.rec_control.setVisible(False)
            self.logger.info(f"✅ Réunion terminée : {self.current_meeting.name}")
            self.refresh_dashboard()

    def manual_pause_recording(self):
        """Pause the active recording (KamelRec or Zoom fallback)."""
        ok = self.auto_engine.pause_recording()
        self.rec_control.set_control_state(
            "paused" if ok else self.auto_engine.get_recording_status()
        )
        if not ok:
            self.logger.warning("La pause n'a pas pu être effectuée.")

    def manual_resume_recording(self):
        """Resume a paused recording (KamelRec or Zoom fallback)."""
        ok = self.auto_engine.resume_recording()
        self.rec_control.set_control_state(
            "recording" if ok else self.auto_engine.get_recording_status()
        )
        if not ok:
            self.logger.warning("La reprise n'a pas pu être effectuée.")

    def _sync_guard_hotkey(self):
        """Enregistre Ctrl+S pendant l'enregistrement, le libère ensuite."""
        try:
            recording = self.auto_engine.get_recording_status() in ("recording", "paused")
            if not recording:
                if self._hotkey is not None:
                    self._hotkey.stop()
                    self._hotkey = None
                self._hotkey_unavailable = False
                return
            if self._hotkey is not None:
                if self._hotkey.is_alive():
                    return
                # Le thread s'est arrêté : raccourci déjà pris par une autre app.
                self._hotkey = None
                self._hotkey_unavailable = True
            if self._hotkey_unavailable:
                return
            self._hotkey = GlobalHotkey(self.bridge.guard_hotkey.emit, name="Ctrl+S")
            self._hotkey.start()
        except Exception as e:
            self.logger.debug(f"Raccourci Ctrl+S: {e}")

    def toggle_popup_guard(self):
        """Ctrl+S : suspend la garde, ou la réactive si elle est déjà suspendue."""
        if self.auto_engine.get_recording_status() not in ("recording", "paused"):
            return
        if self.auto_engine.is_popup_guard_active():
            self.suspend_popup_guard()
        else:
            self.resume_popup_guard()

    def update_guard_controls(self):
        """Met à jour la barre permanente de la garde des fenêtres."""
        if not hasattr(self, "guard_state_lbl"):
            return
        try:
            status = self.auto_engine.get_recording_status()
            active = status in ("recording", "paused")
            guard_active = self.auto_engine.is_popup_guard_active()
        except Exception:
            active = False
            guard_active = False

        # Les deux boutons restent toujours visibles et cliquables, pour
        # pouvoir intervenir à n'importe quel moment.
        self.guard_pause_btn.setEnabled(True)
        self.guard_resume_btn.setEnabled(True)

        if not active:
            self.guard_state_lbl.setText(
                "🛡 Garde : inactive (aucun enregistrement)"
            )
            self.guard_state_lbl.setStyleSheet(
                "font-size:12px; font-weight:bold; color:#6c757d;"
            )
            self.guard_pause_btn.setToolTip(
                "Suspendre la garde pendant l'enregistrement (raccourci Ctrl+S)"
            )
            self.guard_resume_btn.setToolTip(
                "Réactiver la garde pendant l'enregistrement (raccourci Ctrl+S)"
            )
        elif guard_active:
            self.guard_state_lbl.setText(
                "🛡 Garde : ACTIVE — fenêtres fermées"
            )
            self.guard_state_lbl.setStyleSheet(
                "font-size:12px; font-weight:bold; color:#1e7e34;"
            )
            self.guard_pause_btn.setToolTip(
                "Suspendre la garde pour intervenir librement : plus aucune fenêtre "
                "ne sera fermée (Ctrl+S)"
            )
            self.guard_resume_btn.setToolTip("La garde est déjà active")
        else:
            self.guard_state_lbl.setText(
                "⏸ Garde : SUSPENDUE — fenêtres ouvertes"
            )
            self.guard_state_lbl.setStyleSheet(
                "font-size:12px; font-weight:bold; color:#b35c00;"
            )
            self.guard_pause_btn.setToolTip("La garde est déjà suspendue")
            self.guard_resume_btn.setToolTip(
                "Réactiver la fermeture automatique des fenêtres qui s'ouvrent "
                "au-dessus de la réunion (Ctrl+S)"
            )

    def suspend_popup_guard(self):
        """Suspend la fermeture automatique des fenêtres pendant l'enregistrement."""
        if self.auto_engine.get_recording_status() not in ("recording", "paused"):
            self.logger.info(
                "Garde des fenêtres : aucun enregistrement en cours, rien à suspendre."
            )
            self.update_guard_controls()
            return
        if not self.auto_engine.is_popup_guard_active():
            self.logger.info("Garde des fenêtres : déjà suspendue.")
            self.update_guard_controls()
            return
        self.auto_engine.stop_popup_guard()
        self.logger.info(
            "Garde des fenêtres suspendue par l'utilisateur : aucune fenêtre ne sera fermée."
        )
        self.update_guard_controls()

    def resume_popup_guard(self):
        """Réactive la fermeture automatique des fenêtres pendant l'enregistrement."""
        if self.auto_engine.get_recording_status() not in ("recording", "paused"):
            self.logger.info(
                "Garde des fenêtres : aucun enregistrement en cours, rien à réactiver."
            )
            self.update_guard_controls()
            return
        if self.auto_engine.is_popup_guard_active():
            self.logger.info("Garde des fenêtres : déjà active.")
            self.update_guard_controls()
            return
        self.auto_engine.start_popup_guard(force=True)
        self.logger.info(
            "Garde des fenêtres réactivée : les fenêtres surgissantes seront de nouveau fermées."
        )
        self.update_guard_controls()

    def manual_start_recording(self):
        """Start recording from the toolbar (background thread)."""
        if self._manual_thread and self._manual_thread.is_alive():
            return
        if self.auto_engine.get_recording_status() == "recording":
            return
        self._manual_thread = threading.Thread(
            target=self._manual_start_worker, daemon=True
        )
        self._manual_thread.start()

    def _manual_start_worker(self):
        ok = False
        try:
            ok = self.auto_engine.start_recording()
        except Exception as e:
            self.logger.error(f"Erreur démarrage enregistrement : {e}")
        self.bridge.manual_recording_started.emit(ok)

    def _on_manual_recording_started(self, ok: bool):
        self.rec_control.setVisible(True)
        if ok:
            self.rec_control.set_control_state("recording")
        else:
            self.rec_control.set_control_state("stopped")
            self.logger.warning("L'enregistrement n'a pas pu démarrer.")

    def run(self):
        class TextHandler(logging.Handler):
            """Send log records to the GUI through a queued signal (thread-safe)."""
            def __init__(self, widget, bridge):
                super().__init__()
                self.w = widget
                self.bridge = bridge
                bridge.log_message.connect(widget.append)

            def emit(self, record):
                try:
                    self.bridge.log_message.emit(self.format(record))
                except Exception:
                    pass

        if not any(isinstance(h, TextHandler) for h in self.logger.handlers):
            self.logger.addHandler(TextHandler(self.log_text, self.bridge))

        if self.remote_server_mode:
            # Mode contrôle à distance : aucune fenêtre visible, seulement
            # l'icône près de l'horloge. La fenêtre se restaure à la réception
            # d'un ordre distant.
            if self._tray is not None:
                self.main_window.hide()
                self.logger.info(
                    "Application démarrée en mode contrôle à distance : "
                    "icône active dans la zone de notification."
                )
            else:
                self.main_window.showMinimized()
                self.logger.info(
                    "Application démarrée en mode contrôle à distance : "
                    "fenêtre réduite dans la barre des tâches."
                )
        elif self.auto_meeting_id is not None:
            # Lancement par le Planificateur Windows : l'interface graphique
            # est bien présente, mais réduite dans la barre des tâches. Elle
            # reste ouverte après l'enregistrement, pour que l'utilisateur
            # puisse voir le résultat et fermer l'application lui-même.
            self.main_window.showMinimized()
            self.logger.info(
                "Application démarrée (lancement planifié) : "
                "fenêtre réduite dans la barre des tâches."
            )
            QTimer.singleShot(0, self._start_auto_meeting)
        else:
            self.main_window.show()
            self.logger.info("Application démarrée.")
        sys.exit(self.app.exec())


def main():
    """Entry point."""
    app = ZoomAutoRecorderApp()
    app.run()


if __name__ == "__main__":
    main()
