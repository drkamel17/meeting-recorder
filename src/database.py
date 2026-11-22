"""
Database management for Zoom Auto Recorder using SQLite.
"""
import sqlite3
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from src.config import DB_PATH, DATA_DIR, DEFAULT_RECORDINGS_DIR
from src.security import encrypt_password, decrypt_password

logger = logging.getLogger("ZoomAutoRecorder")


@dataclass
class Meeting:
    id: Optional[int] = None
    name: str = ""
    display_name: str = ""
    meeting_id: str = ""
    passcode: str = ""
    start_time: str = ""  # ISO format
    duration_minutes: int = 60
    recordings_dir: str = ""
    notes: str = ""
    platform: str = "zoom"  # "zoom" ou "google_meet"
    
    # Control settings
    mic_on_start: bool = False
    camera_on_start: bool = False
    chat_open: bool = True
    full_screen: bool = True
    
    # Status and scheduling
    is_enabled: bool = True
    recurrence: str = "none"  # none, daily, weekly
    last_run_time: Optional[str] = None
    status: str = "Waiting"


logger = logging.getLogger("ZoomAutoRecorder")


@dataclass
class Recording:
    id: Optional[int] = None
    meeting_id: Optional[int] = None
    meeting_name: str = ""
    start_time: str = ""
    end_time: str = ""
    duration_seconds: int = 0
    file_path: str = ""
    status: str = "Completed"


class DatabaseManager:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db_path = DB_PATH

    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self.get_connection() as conn:
            # Meetings table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meetings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    display_name TEXT,
                    meeting_id TEXT NOT NULL,
                    passcode TEXT,
                    start_time TEXT NOT NULL,
                    duration_minutes INTEGER DEFAULT 60,
                    recordings_dir TEXT,
                    notes TEXT,
                    mic_on_start BOOLEAN DEFAULT 0,
                    camera_on_start BOOLEAN DEFAULT 0,
                    chat_open BOOLEAN DEFAULT 1,
                    full_screen BOOLEAN DEFAULT 1,
                    is_enabled BOOLEAN DEFAULT 1,
                    recurrence TEXT DEFAULT 'none',
                    last_run_time TEXT,
                    status TEXT DEFAULT 'Waiting',
                    platform TEXT DEFAULT 'zoom'
                )
            """)
            # Recordings table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS recordings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    meeting_id INTEGER,
                    meeting_name TEXT,
                    start_time TEXT,
                    end_time TEXT,
                    duration_seconds INTEGER,
                    file_path TEXT,
                    status TEXT,
                    FOREIGN KEY(meeting_id) REFERENCES meetings(id)
                )
            """)
            conn.commit()
        self._migrate()

    def _migrate(self):
        """Ajoute les colonnes manquantes aux bases de données existantes."""
        with self.get_connection() as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(meetings)")}
            if "platform" not in cols:
                conn.execute("ALTER TABLE meetings ADD COLUMN platform TEXT DEFAULT 'zoom'")
                conn.commit()
                logger.info("Migration DB: colonne 'platform' ajoutée.")

    # --- Meeting Operations ---

    def add_meeting(self, m: Meeting) -> int:
        with self.get_connection() as conn:
            cursor = conn.execute(
                """INSERT INTO meetings (
                    name, display_name, meeting_id, passcode, start_time, duration_minutes,
                    recordings_dir, notes, mic_on_start, camera_on_start, chat_open,
                    full_screen, is_enabled, recurrence, status, platform
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (m.name, m.display_name, m.meeting_id, encrypt_password(m.passcode), m.start_time,
                 m.duration_minutes, m.recordings_dir, m.notes, m.mic_on_start,
                 m.camera_on_start, m.chat_open, m.full_screen, m.is_enabled,
                 m.recurrence, m.status, m.platform)
            )
            m.id = cursor.lastrowid
            return m.id

    def update_meeting(self, m: Meeting):
        with self.get_connection() as conn:
            conn.execute(
                """UPDATE meetings SET 
                    name=?, display_name=?, meeting_id=?, passcode=?, start_time=?, 
                    duration_minutes=?, recordings_dir=?, notes=?, mic_on_start=?, 
                    camera_on_start=?, chat_open=?, full_screen=?, is_enabled=?, 
                    recurrence=?, last_run_time=?, status=?, platform=?
                    WHERE id=?""",
                (m.name, m.display_name, m.meeting_id, m.passcode, m.start_time, 
                 m.duration_minutes, m.recordings_dir, m.notes, m.mic_on_start, 
                 m.camera_on_start, m.chat_open, m.full_screen, m.is_enabled, 
                 m.recurrence, m.last_run_time, m.status, m.platform, m.id)
            )

    def reset_stuck_meetings(self):
        """Remet en attente les reunions coincées (lancement interrompu, crash precedent)."""
        with self.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE meetings SET status=? WHERE status IN ('Launching Zoom', 'Starting')",
                ("Waiting",),
            )
            if cursor.rowcount > 0:
                logger.info(f"Réunions réinitialisées en attente : {cursor.rowcount}")
                conn.commit()

    def try_claim_meeting(self, meeting_id: int) -> bool:
        """Atomically claim a waiting meeting for execution.

        This prevents both the running app and a Windows Task Scheduler
        instance from starting the same meeting twice.
        """
        with self.get_connection() as conn:
            cursor = conn.execute(
                "UPDATE meetings SET status=? WHERE id=? AND status=? AND is_enabled=1",
                ("Starting", meeting_id, "Waiting"),
            )
            return cursor.rowcount > 0

    def delete_meeting(self, meeting_id: int):
        with self.get_connection() as conn:
            conn.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))

    def get_all_meetings(self) -> List[Meeting]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM meetings ORDER BY start_time ASC").fetchall()
            meetings = []
            for row in rows:
                d = dict(row)
                d["passcode"] = decrypt_password(d.get("passcode", ""))
                meetings.append(Meeting(**d))
            return meetings

    def get_meeting_by_id(self, meeting_id: int) -> Optional[Meeting]:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
            if row:
                d = dict(row)
                d["passcode"] = decrypt_password(d.get("passcode", ""))
                return Meeting(**d)
            return None

    # --- Recording Operations ---

    def add_recording(self, r: Recording) -> int:
        with self.get_connection() as conn:
            cursor = conn.execute(
                """INSERT INTO recordings (
                    meeting_id, meeting_name, start_time, end_time, 
                    duration_seconds, file_path, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (r.meeting_id, r.meeting_name, r.start_time, r.end_time, 
                 r.duration_seconds, r.file_path, r.status)
            )
            return cursor.lastrowid

    def get_all_recordings(self) -> List[Recording]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT * FROM recordings ORDER BY start_time DESC").fetchall()
            return [Recording(**dict(row)) for row in rows]

    def scan_recordings_folder(self) -> int:
        """Scan the recordings folder and add any new files to the database."""
        try:
            import os
            from datetime import datetime

            dir_path = DEFAULT_RECORDINGS_DIR
            if not dir_path.exists():
                logger.warning(f"Dossier non trouvé: {dir_path}")
                return 0

            with self.get_connection() as conn:
                # Get existing file paths from database
                existing = conn.execute("SELECT file_path FROM recordings").fetchall()
                existing_paths = {row[0] for row in existing}
                logger.info(f"Enregistrements existants en DB: {len(existing_paths)}")

                added = 0
                files = list(dir_path.glob("*.mp4"))
                logger.info(f"Fichiers MP4 trouvés: {len(files)}")

                for f in files:
                    file_path = str(f)
                    logger.info(f"  Traitement: {f.name}")

                    if file_path not in existing_paths:
                        stem = f.stem
                        dt = None
                        try:
                            if "enregistrement_ecran_" in stem:
                                ts_str = stem.split("enregistrement_ecran_", 1)[1]
                                dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
                            elif "enregistrement_gamebar_" in stem:
                                ts_str = stem.split("enregistrement_gamebar_", 1)[1]
                                dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
                            elif "enregistrement_" in stem:
                                ts_str = stem.split("enregistrement_", 1)[1]
                                dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
                            elif stem.lower().startswith("kamelrec_"):
                                ts_str = stem[len("KamelRec_"):]
                                dt = datetime.strptime(ts_str, "%Y-%m-%d_%H-%M-%S")
                        except ValueError:
                            dt = None

                        # Unknown or malformed name (e.g. manual export):
                        # fall back to the file modification time instead of
                        # dropping the recording.
                        if dt is None:
                            dt = datetime.fromtimestamp(f.stat().st_mtime)

                        start_time = dt.strftime("%Y-%m-%dT%H:%M:%S")
                        end_time = start_time
                        duration_seconds = 0

                        conn.execute(
                            """INSERT INTO recordings (
                                meeting_id, meeting_name, start_time, end_time,
                                duration_seconds, file_path, status
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (None, "Enregistrement", start_time, end_time,
                             duration_seconds, file_path, "Completed")
                        )
                        existing_paths.add(file_path)
                        added += 1
                        logger.info(f"    ✅ Ajouté: {f.name}")

                conn.commit()
                logger.info(f"Total ajouté: {added}")
                return added

        except Exception as e:
            logger.error(f"Erreur scan dossier enregistrements: {e}")
            return 0

    def delete_recording(self, recording_id: int):
        with self.get_connection() as conn:
            conn.execute("DELETE FROM recordings WHERE id=?", (recording_id,))
