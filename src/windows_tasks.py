"""Windows Task Scheduler integration for Zoom Auto Recorder.

A meeting created in the app can therefore start even when the app itself is
closed: Windows launches the app with ``--run-meeting <id>`` at the scheduled
time. Tasks are per-user and run interactively, which is required for Zoom UI
automation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from src.database import DatabaseManager, Meeting

logger = logging.getLogger("ZoomAutoRecorder")

TASK_PREFIX = "ZoomAutoRecorder-Meeting-"
REMOTE_TASK_NAME = "ZoomAutoRecorder-RemoteServer"
REMOTE_AUTOSTART_NAME = "ZoomAutoRecorder-RemoteServer"
MISSED_WINDOW_MINUTES = 5


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _python_executable() -> str:
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    if pythonw.exists():
        return str(pythonw)
    return str(python)


def _task_name(meeting_id: int | None) -> str:
    if meeting_id is None:
        raise ValueError("meeting_id is required")
    return f"{TASK_PREFIX}{meeting_id}"


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _run_powershell(script: str) -> subprocess.CompletedProcess:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=30,
    )


def _meeting_command(meeting: Meeting) -> tuple[str, str]:
    """Return (executable, arguments) for the scheduled task."""
    if is_frozen():
        return sys.executable, f"--run-meeting {meeting.id}"

    script = _project_root() / "main.py"
    return _python_executable(), f'"{script}" --run-meeting {meeting.id}'


def _register_task(meeting: Meeting, start: datetime) -> bool:
    name = _task_name(meeting.id)
    executable, arguments = _meeting_command(meeting)

    script = f"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$trigger = New-ScheduledTaskTrigger -Once -At ([datetime]'{start.isoformat(sep=' ')}')
$action = New-ScheduledTaskAction -Execute {_ps_quote(executable)} -Argument {_ps_quote(arguments)}
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName {_ps_quote(name)} `
    -Description 'Zoom Auto Recorder - Réunion planifiée' `
    -Trigger $trigger `
    -Action $action `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null
"""
    result = _run_powershell(script)
    if result.returncode != 0:
        logger.warning("Planificateur Windows: création de la tâche %s impossible: %s",
                       name, (result.stderr or result.stdout).strip())
        return False
    logger.info("🗓️ Tâche Windows créée/mise à jour : %s (%s)", name, start.strftime("%d/%m/%Y %H:%M"))
    return True


def _unregister_task(task_name: str) -> bool:
    script = f"""
$ErrorActionPreference = 'Stop'
Unregister-ScheduledTask -TaskName {_ps_quote(task_name)} -Confirm:$false
"""
    result = _run_powershell(script)
    if result.returncode != 0:
        # It may simply not exist; that's fine.
        logger.debug("Planificateur Windows: suppression %s: %s",
                     task_name, (result.stderr or result.stdout).strip())
        return False
    logger.info("🗑️ Tâche Windows supprimée : %s", task_name)
    return True


def _managed_task_names() -> set[str]:
    script = f"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$tasks = Get-ScheduledTask -TaskName '{TASK_PREFIX}*' -ErrorAction SilentlyContinue
$items = @()
foreach ($task in $tasks) {{
    $items += $task.TaskName
}}
ConvertTo-Json -InputObject $items -Compress
"""
    result = _run_powershell(script)
    if result.returncode != 0:
        logger.warning("Planificateur Windows: lecture des tâches impossible: %s",
                       (result.stderr or result.stdout).strip())
        return set()

    text = (result.stdout or "").strip()
    if not text:
        return set()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Planificateur Windows: réponse illisible (%s)", text[:200])
        return set()

    if isinstance(parsed, str):
        return {parsed} if parsed.startswith(TASK_PREFIX) else set()
    if isinstance(parsed, list):
        return {item for item in parsed if isinstance(item, str) and item.startswith(TASK_PREFIX)}
    return set()


def _parse_task_list(text: str) -> list[dict]:
    text = (text or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Planificateur Windows: réponse des tâches illisible (%s)", text[:200])
        return []

    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []

    tasks: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "")
        if not name.startswith(TASK_PREFIX):
            continue
        tasks.append({
            "name": name,
            "state": str(item.get("State") or ""),
            "start_boundary": str(item.get("StartBoundary") or ""),
            "action": str(item.get("Action") or ""),
        })
    return tasks


def get_managed_tasks() -> list[dict]:
    """Return all managed Windows tasks (Zoom Auto Recorder only)."""
    script = f"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$tasks = Get-ScheduledTask -TaskName '{TASK_PREFIX}*' -ErrorAction SilentlyContinue
$items = @()
foreach ($task in $tasks) {{
    $start = $null
    if ($task.Triggers -and $task.Triggers.Count -gt 0) {{
        $start = $task.Triggers[0].StartBoundary
    }}
    $actionText = ($task.Actions | ForEach-Object {{ ($_.Execute + ' ' + $_.Arguments) }}) -join ' ; '
    $items += [PSCustomObject]@{{
        Name = $task.TaskName
        State = [string]$task.State
        StartBoundary = if ($start) {{ [string]$start }} else {{ '' }}
        Action = $actionText
    }}
}}
ConvertTo-Json -InputObject $items -Depth 3 -Compress
"""
    result = _run_powershell(script)
    if result.returncode != 0:
        logger.warning("Planificateur Windows: lecture détaillée impossible: %s",
                       (result.stderr or result.stdout).strip())
        return []
    return _parse_task_list(result.stdout)


def parse_managed_meeting_id(task_name: str) -> int | None:
    match = re.fullmatch(rf"{re.escape(TASK_PREFIX)}(\d+)", task_name)
    return int(match.group(1)) if match else None


def cancel_managed_task(db: DatabaseManager, task_name: str) -> str:
    """Cancel a managed task.

    For a meeting that is still waiting, the meeting is disabled first so it is
    not recreated at the next synchronization. Orphan tasks are simply deleted.
    Returns a short result label used by the UI.
    """
    meeting_id = parse_managed_meeting_id(task_name)
    result = "supprimée"

    if meeting_id is not None:
        try:
            meeting = db.get_meeting_by_id(meeting_id)
        except Exception:
            meeting = None
        if meeting:
            if meeting.is_enabled:
                meeting.is_enabled = False
                db.update_meeting(meeting)
                result = "réunion désactivée"
            logger.info("Planificateur Windows: tâche annulée pour la réunion %s", meeting.name)
        else:
            logger.info("Planificateur Windows: tâche orpheline %s", task_name)

    _unregister_task(task_name)
    return result


def _is_due(m: Meeting, now: datetime) -> bool:
    if not m.is_enabled or m.status != "Waiting":
        return False
    try:
        start = datetime.fromisoformat(m.start_time)
    except (TypeError, ValueError):
        return False
    return start >= now - timedelta(minutes=MISSED_WINDOW_MINUTES)


def sync_windows_tasks(db: DatabaseManager | None = None) -> int:
    """Create/update/delete Windows tasks to match enabled meetings.

    Returns the number of desired tasks after synchronization.
    """
    db = db or DatabaseManager()
    now = datetime.now()
    desired: dict[str, tuple[Meeting, datetime]] = {}

    try:
        meetings = db.get_all_meetings()
    except Exception as e:
        logger.warning("Planificateur Windows: lecture des réunions impossible: %s", e)
        return 0

    for meeting in meetings:
        if not _is_due(meeting, now):
            continue
        try:
            start = datetime.fromisoformat(meeting.start_time)
        except (TypeError, ValueError):
            logger.warning("Planificateur Windows: date invalide pour '%s'", meeting.name)
            continue
        desired[_task_name(meeting.id)] = (meeting, start)

    existing = _managed_task_names()

    # Remove stale managed tasks (disabled, completed, deleted or past meetings).
    for name in existing - set(desired):
        _unregister_task(name)

    # Create/update desired tasks.
    created = 0
    for name, (meeting, start) in desired.items():
        if _register_task(meeting, start):
            created += 1

    logger.info("Planificateur Windows: %d tâche(s) active(s), %d enregistrement(s) créé(s)/mis à jour.",
                len(desired), created)
    return len(desired)


def remove_windows_task(meeting_id: int | None) -> bool:
    if meeting_id is None:
        return False
    return _unregister_task(_task_name(meeting_id))


def remove_all_windows_tasks() -> None:
    for name in _managed_task_names():
        _unregister_task(name)


# ═══════════════════════════════════════════════════════════════
# DÉMARRAGE AUTOMATIQUE DU CONTRÔLE À DISTANCE (ouverture de session)
# ═══════════════════════════════════════════════════════════════
def _remote_command() -> tuple[str, str, str]:
    """Commande (exécutable, arguments, dossier) du mode contrôle à distance."""
    if is_frozen():
        executable = sys.executable
        return executable, "--remote-server", str(Path(executable).resolve().parent)
    script = _project_root() / "main.py"
    return _python_executable(), f'"{script}" --remote-server', str(_project_root())


def _startup_shortcut_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / f"{REMOTE_AUTOSTART_NAME}.lnk"
    )


def register_remote_autostart() -> bool:
    """Crée un raccourci dans le dossier Démarrage de l'utilisateur.

    Le Planificateur Windows exige des droits administrateur pour un
    déclencheur « à l'ouverture de session ». Le dossier Démarrage, lui, est
    accessible à l'utilisateur courant et lance l'application minimisée à
    chaque connexion, sans élévation.
    """
    executable, arguments, working_dir = _remote_command()
    shortcut = _startup_shortcut_path()
    script = f"""
$ErrorActionPreference = 'Stop'
$startup = [Environment]::GetFolderPath('Startup')
if (-not (Test-Path $startup)) {{ New-Item -ItemType Directory -Path $startup -Force | Out-Null }}
$path = Join-Path $startup {_ps_quote(shortcut.name)}
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($path)
$link.TargetPath = {_ps_quote(executable)}
$link.Arguments = {_ps_quote(arguments)}
$link.WorkingDirectory = {_ps_quote(working_dir)}
$link.Description = 'Zoom Auto Recorder - Controle a distance'
$link.WindowStyle = 7
$link.Save()
Write-Output $path
"""
    result = _run_powershell(script)
    if result.returncode != 0:
        logger.warning(
            "Contrôle à distance : démarrage automatique impossible : %s",
            (result.stderr or result.stdout).strip(),
        )
        return False
    logger.info(
        "Contrôle à distance : démarrage automatique activé à l'ouverture de session Windows."
    )
    return True


def unregister_remote_autostart() -> bool:
    """Supprime le raccourci de démarrage automatique du contrôle à distance."""
    shortcut = _startup_shortcut_path()
    try:
        if shortcut.exists():
            shortcut.unlink()
            logger.info("Contrôle à distance : démarrage automatique désactivé.")
            return True
    except OSError as exc:
        logger.warning("Contrôle à distance : suppression du raccourci impossible : %s", exc)
    return False


def is_remote_autostart_enabled() -> bool:
    return _startup_shortcut_path().exists()
