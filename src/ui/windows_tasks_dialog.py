"""Dialog for viewing and cancelling Zoom Auto Recorder Windows tasks."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHeaderView, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from src.windows_tasks import (
    cancel_managed_task,
    get_managed_tasks,
    parse_managed_meeting_id,
    sync_windows_tasks,
)


def _format_datetime(value: str) -> str:
    if not value:
        return "—"
    # PowerShell returns ISO-like strings; try common formats.
    from datetime import datetime
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.strftime("%d/%m/%Y %H:%M")
        except ValueError:
            continue
    return value


class WindowsTasksDialog(QDialog):
    """Show all planned Zoom Auto Recorder Windows tasks."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Tâches Windows planifiées")
        self.resize(950, 520)

        layout = QVBoxLayout(self)
        title = QLabel("🗓️ Tâches du Planificateur Windows")
        title.setStyleSheet("font-size:16px;font-weight:bold;color:#1e293b;")
        layout.addWidget(title)
        subtitle = QLabel(
            "Ces tâches lancent automatiquement Zoom et l'enregistrement, même si l'application est fermée."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color:#64748b;font-size:11px;")
        layout.addWidget(subtitle)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels([
            "Tâche", "Réunion", "Début prévue", "État Windows", "Réunion activée", "Action",
        ])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        layout.addWidget(self.table)

        self.empty_label = QLabel("Aucune tâche planifiée.")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet("color:#94a3b8;padding:18px;")
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)

        buttons = QHBoxLayout()
        self.refresh_btn = QPushButton("🔄 Actualiser")
        self.refresh_btn.setStyleSheet("padding:6px 10px;background:#007acc;color:white;border:none;border-radius:4px;")
        self.refresh_btn.clicked.connect(self.refresh)
        buttons.addWidget(self.refresh_btn)

        self.cancel_selected_btn = QPushButton("❌ Annuler la sélection")
        self.cancel_selected_btn.setStyleSheet("padding:6px 10px;background:#dc3545;color:white;border:none;border-radius:4px;")
        self.cancel_selected_btn.clicked.connect(self.cancel_selected)
        buttons.addWidget(self.cancel_selected_btn)

        self.cancel_all_btn = QPushButton("🗑 Tout annuler")
        self.cancel_all_btn.setStyleSheet("padding:6px 10px;background:#7f1d1d;color:white;border:none;border-radius:4px;")
        self.cancel_all_btn.clicked.connect(self.cancel_all)
        buttons.addWidget(self.cancel_all_btn)

        buttons.addStretch()
        close_box = QDialogButtonBox(QDialogButtonBox.Close)
        close_box.rejected.connect(self.reject)
        close_box.accepted.connect(self.accept)
        buttons.addWidget(close_box)

        layout.addLayout(buttons)
        self.refresh()

    def refresh(self):
        tasks = get_managed_tasks()
        # Load meeting names in one pass.
        meetings = {m.id: m for m in self.db.get_all_meetings()}

        self.table.setRowCount(len(tasks))
        self.empty_label.setVisible(not tasks)
        self.table.setVisible(bool(tasks))

        for row, task in enumerate(tasks):
            meeting_id = parse_managed_meeting_id(task["name"])
            meeting = meetings.get(meeting_id) if meeting_id is not None else None
            values = [
                task["name"],
                meeting.name if meeting else "Tâche orpheline",
                _format_datetime(task["start_boundary"]),
                task["state"] or "—",
                "✅ Oui" if (meeting and meeting.is_enabled) else "⏸️ Non",
                task["action"] or "—",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, task["name"])
                item.setToolTip(value)
                self.table.setItem(row, col, item)

        self.cancel_selected_btn.setEnabled(bool(tasks))
        self.cancel_all_btn.setEnabled(bool(tasks))

    def _selected_task_names(self) -> list[str]:
        names = []
        for index in self.table.selectedIndexes():
            if index.column() != 0:
                continue
            item = self.table.item(index.row(), 0)
            if item:
                names.append(item.data(Qt.UserRole))
        return list(dict.fromkeys(names))

    def _cancel_tasks(self, task_names: list[str]):
        cancelled = 0
        orphans = 0
        for name in task_names:
            result = cancel_managed_task(self.db, name)
            cancelled += 1
            if result == "supprimée":
                orphans += 1
        # Re-sync everything so Windows stays consistent with the database.
        sync_windows_tasks(self.db)
        self.refresh()
        return cancelled, orphans

    def cancel_selected(self):
        names = self._selected_task_names()
        if not names:
            QMessageBox.information(self, "Tâches Windows", "Sélectionnez au moins une tâche.")
            return

        reply = QMessageBox.question(
            self,
            "Annuler les tâches",
            f"Annuler {len(names)} tâche(s) Windows ?\n\n"
            "Les réunions correspondantes seront désactivées afin qu'elles ne soient pas reprogrammées automatiquement.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        cancelled, _ = self._cancel_tasks(names)
        QMessageBox.information(self, "Tâches Windows", f"{cancelled} tâche(s) annulée(s).")

    def cancel_all(self):
        count = self.table.rowCount()
        if count == 0:
            return

        reply = QMessageBox.question(
            self,
            "Tout annuler",
            f"Annuler les {count} tâche(s) Windows ?\n\n"
            "Les réunions correspondantes seront désactivées afin qu'elles ne soient pas reprogrammées automatiquement.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        names = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item:
                names.append(item.data(Qt.UserRole))
        cancelled, _ = self._cancel_tasks(names)
        QMessageBox.information(self, "Tâches Windows", f"{cancelled} tâche(s) annulée(s).")
