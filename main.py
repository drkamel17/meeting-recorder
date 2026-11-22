"""
Zoom Auto Recorder - Point d'entrée principal.
"""
import os
import sys

# Add src to path
SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

# Handle du mutex Windows du serveur de contrôle à distance (gardé vivant
# pendant toute la durée du processus).
_REMOTE_MUTEX = None


def _acquire_remote_mutex() -> bool:
    """Empêche deux serveurs de contrôle à distance de tourner en même temps."""
    global _REMOTE_MUTEX
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "Local\\ZoomAutoRecorderRemoteServer")
        if not handle:
            return True
        # ERROR_ALREADY_EXISTS
        if kernel32.GetLastError() == 183:
            kernel32.CloseHandle(handle)
            return False
        _REMOTE_MUTEX = handle
        return True
    except Exception:
        # En cas de doute, laisser démarrer (le port occupé sera journalisé).
        return True


def main():
    """Entry point for the application."""
    # Mode serveur de contrôle à distance (démarrage automatique à l'ouverture
    # de session). L'interface est réduite dans la barre des tâches et reste
    # ouverte : elle se restaure à la réception d'un ordre distant.
    if "--remote-server" in sys.argv[1:]:
        if not _acquire_remote_mutex():
            return 0
        from src.app import ZoomAutoRecorderApp

        app = ZoomAutoRecorderApp(remote_server=True)
        app.run()
        return 0

    # Windows Task Scheduler launches this mode for a specific meeting.
    # L'interface graphique est affichée (réduite dans la barre des tâches)
    # afin que l'utilisateur puisse suivre et arrêter l'application.
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-meeting":
        try:
            meeting_id = int(sys.argv[2])
        except ValueError:
            print(f"Invalid meeting id: {sys.argv[2]!r}", file=sys.stderr)
            return 2

        from src.app import ZoomAutoRecorderApp

        app = ZoomAutoRecorderApp(auto_meeting_id=meeting_id)
        app.run()
        return 0

    # Ancien mode sans interface (conservé pour un lancement manuel/avancé).
    if len(sys.argv) >= 3 and sys.argv[1] == "--run-meeting-headless":
        try:
            meeting_id = int(sys.argv[2])
        except ValueError:
            print(f"Invalid meeting id: {sys.argv[2]!r}", file=sys.stderr)
            return 2

        from src.scheduled_runner import run_scheduled_meeting
        return run_scheduled_meeting(meeting_id)

    from src.app import ZoomAutoRecorderApp

    app = ZoomAutoRecorderApp()
    app.run()


if __name__ == "__main__":
    sys.exit(main())
