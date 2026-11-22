"""
Add Meeting Dialog UI component.
"""
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QVBoxLayout, QLineEdit, QDateTimeEdit,
    QSpinBox, QCheckBox, QComboBox, QPushButton, QDialogButtonBox,
    QStackedWidget, QWidget, QLabel
)
from PySide6.QtCore import QDateTime, Qt


class AddMeetingDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ajouter une réunion")
        self.resize(480, 600)

        layout = QVBoxLayout(self)

        # Sélecteur de plateforme
        self.platform_combo = QComboBox()
        self.platform_combo.addItem("Zoom", "zoom")
        self.platform_combo.addItem("Google Meet", "google_meet")
        self.platform_combo.currentIndexChanged.connect(self._on_platform_changed)

        # Inputs communs
        self.name_input = QLineEdit()
        self.meeting_id_input = QLineEdit()
        self.meeting_id_input.setPlaceholderText("ID de réunion ou lien Google Meet")
        self.passcode_input = QLineEdit()
        self.passcode_input.setPlaceholderText("Code secret (Zoom uniquement)")
        self.display_name_input = QLineEdit()
        self.display_name_input.setPlaceholderText("Nom affiché dans la réunion")

        self.date_time_input = QDateTimeEdit(QDateTime.currentDateTime())
        self.date_time_input.setCalendarPopup(True)

        self.duration_input = QSpinBox()
        self.duration_input.setRange(5, 480)
        self.duration_input.setValue(60)

        # Formulaire commun
        form = QFormLayout()
        form.addRow("Plateforme:", self.platform_combo)
        form.addRow("Nom de la réunion:", self.name_input)
        form.addRow("Meeting ID / Lien:", self.meeting_id_input)
        form.addRow("Mot de passe:", self.passcode_input)
        form.addRow("Nom affiché:", self.display_name_input)
        form.addRow("Date et Heure:", self.date_time_input)
        form.addRow("Durée (min):", self.duration_input)
        layout.addLayout(form)

        # Stack pour l'aide spécifique à la plateforme
        self.stack = QStackedWidget()

        # Page Zoom
        zoom_page = QWidget()
        zoom_lay = QVBoxLayout(zoom_page)
        zoom_lay.addWidget(QLabel("<i>ID de réunion Zoom et code secret requis.</i>"))
        self.stack.addWidget(zoom_page)

        # Page Google Meet
        meet_page = QWidget()
        meet_lay = QVBoxLayout(meet_page)
        meet_lay.addWidget(QLabel("<i>Collez le lien Google Meet complet.</i>"))
        meet_hint = QLabel("Exemple : https://meet.google.com/abc-defg-hij")
        meet_hint.setStyleSheet("color:#666; font-size:11px;")
        meet_lay.addWidget(meet_hint)
        meet_lay.addWidget(QLabel("<i>Le micro et la caméra seront coupés avant de rejoindre.</i>"))
        self.stack.addWidget(meet_page)

        layout.addWidget(self.stack)

        # Options
        self.mic_checkbox = QCheckBox("Activer le micro au démarrage")
        self.cam_checkbox = QCheckBox("Activer la caméra au démarrage")
        self.chat_checkbox = QCheckBox("Ouvrir le Chat")
        self.chat_checkbox.setChecked(True)
        layout.addWidget(self.mic_checkbox)
        layout.addWidget(self.cam_checkbox)
        layout.addWidget(self.chat_checkbox)

        # Boutons
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._on_platform_changed(0)

    def _on_platform_changed(self, index):
        plateforme = self.platform_combo.itemData(index)
        is_meet = plateforme == "google_meet"
        self.passcode_input.setVisible(not is_meet)
        self.stack.setCurrentIndex(1 if is_meet else 0)

    def get_data(self):
        return {
            "name": self.name_input.text().strip(),
            "meeting_id": self.meeting_id_input.text().strip(),
            "passcode": self.passcode_input.text().strip(),
            "display_name": self.display_name_input.text().strip(),
            "start_time": self.date_time_input.dateTime().toString(Qt.ISODate),
            "duration_minutes": self.duration_input.value(),
            "mic_on_start": self.mic_checkbox.isChecked(),
            "camera_on_start": self.cam_checkbox.isChecked(),
            "chat_open": self.chat_checkbox.isChecked(),
            "platform": self.platform_combo.currentData(),
        }
