"""Headless entry point used by Windows Task Scheduler.

This runner is intentionally independent from the Qt UI so a meeting can be
joined and recorded even when the main application is closed.
"""

from datetime import datetime, timedelta
import logging
import time

from src.database import DatabaseManager
from src.automation import ZoomAutomation
from src.config import STATE_COMPLETED, STATE_ERROR, STATE_IN_MEETING, STATE_RECORDING, STATE_WAITING_ROOM
from src.windows_tasks import remove_windows_task
from src.logger import setup_logging

logger = setup_logging()

MISSED_WINDOW_MINUTES = 5


def run_scheduled_meeting(meeting_id: int) -> int:
    db = DatabaseManager()
    try:
        db.init_db()
    except Exception as e:
        logger.exception("Planificateur Windows: initialisation DB impossible: %s", e)
        return 2

    try:
        meeting = db.get_meeting_by_id(meeting_id)
    except Exception as e:
        logger.exception("Planificateur Windows: lecture de la réunion %s impossible: %s", meeting_id, e)
        return 2
    if not meeting:
        logger.error("Planificateur Windows: réunion %s introuvable.", meeting_id)
        return 2

    if not meeting.is_enabled:
        logger.info("Planificateur Windows: réunion désactivée (%s).", meeting.name)
        remove_windows_task(meeting.id)
        return 3

    try:
        start = datetime.fromisoformat(meeting.start_time)
    except (TypeError, ValueError):
        logger.error("Planificateur Windows: date invalide pour '%s'.", meeting.name)
        remove_windows_task(meeting.id)
        return 4

    now = datetime.now()
    if now < start - timedelta(minutes=2):
        # Should be rare, but tasks may be edited manually. Wait until it's time.
        while datetime.now() < start:
            time.sleep(2)
    elif now > start + timedelta(minutes=MISSED_WINDOW_MINUTES):
        logger.warning("Planificateur Windows: fenêtre de démarrage dépassée pour '%s'.", meeting.name)
        meeting.status = STATE_ERROR
        db.update_meeting(meeting)
        remove_windows_task(meeting.id)
        return 5

    # Claim the meeting atomically so the running app and this task cannot
    # both start it at the same time.
    if not db.try_claim_meeting(meeting.id):
        logger.info("Planificateur Windows: réunion déjà prise en charge (%s).", meeting.name)
        return 0

    # Remove the one-shot task once the meeting has been claimed.
    remove_windows_task(meeting.id)

    meeting = db.get_meeting_by_id(meeting.id)
    if not meeting:
        logger.error("Planificateur Windows: la réunion a disparu après revendication.")
        return 6

    engine = ZoomAutomation()
    try:
        logger.info("=== Démarrage planifié : %s ===", meeting.name)
        platform = getattr(meeting, "platform", "zoom") or "zoom"

        if platform == "google_meet":
            if not engine.launch_google_meet(meeting.meeting_id):
                meeting.status = STATE_ERROR
                db.update_meeting(meeting)
                return 7
            time.sleep(3)
            display_name = (meeting.display_name or meeting.name or "").strip() or "AutoRecorder"
            result = engine.join_google_meet(
                meeting.meeting_id,
                display_name=display_name,
                mic_on_start=bool(meeting.mic_on_start),
                camera_on_start=bool(meeting.camera_on_start),
            )
        else:
            if not engine.launch_zoom():
                meeting.status = STATE_ERROR
                db.update_meeting(meeting)
                return 7
            time.sleep(3)
            display_name = (meeting.display_name or meeting.name or "").strip() or "AutoRecorder"
            result = engine.join_meeting(
                meeting.meeting_id,
                meeting.passcode,
                wait_for_join=True,
                display_name=display_name,
            )

        if result == STATE_WAITING_ROOM:
            meeting.status = STATE_WAITING_ROOM
            db.update_meeting(meeting)
            logger.warning("Salle d'attente : %s", meeting.name)
            return 8

        if result != STATE_IN_MEETING:
            meeting.status = STATE_ERROR
            db.update_meeting(meeting)
            logger.error("Échec de jointure : %s", meeting.name)
            return 9

        meeting.status = STATE_RECORDING
        db.update_meeting(meeting)
        logger.info("Démarrage enregistrement : %s", meeting.name)

        if not engine.start_recording():
            meeting.status = STATE_IN_MEETING
            db.update_meeting(meeting)
            logger.error("Enregistrement non démarré.")
            return 10

        # Stay alive and stop after the configured duration.
        duration = max(1, int(meeting.duration_minutes or 60))
        deadline = datetime.now() + timedelta(minutes=duration)
        while datetime.now() < deadline:
            time.sleep(5)
            try:
                engine.keep_meeting_fullscreen()
            except Exception:
                pass

        engine.stop_recording()
        meeting.status = STATE_COMPLETED
        db.update_meeting(meeting)
        logger.info("✅ Réunion terminée : %s", meeting.name)
        return 0

    except Exception as e:
        logger.exception("Planificateur Windows: erreur sur '%s': %s", meeting.name, e)
        meeting.status = STATE_ERROR
        try:
            db.update_meeting(meeting)
        except Exception:
            pass
        remove_windows_task(meeting.id)
        return 11
