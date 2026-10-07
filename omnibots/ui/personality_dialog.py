"""Layout → Personality → New personality… (PLAN.md A17.d.01): make your own character for a bot. Style only."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QPlainTextEdit

from omnibots.ui import theme

LEVELS = ("None", "A little", "Lots")


class PersonalityDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New personality")
        self.setStyleSheet(theme.stylesheet())
        self.setMinimumWidth(460)
        form = QFormLayout(self)
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Grumpy chef")
        self.tone = QLineEdit()
        self.tone.setPlaceholderText("e.g. a gruff but kind head chef")
        self.style = QLineEdit()
        self.style.setPlaceholderText("e.g. kitchen metaphors, short sentences")
        self.backstory = QPlainTextEdit()
        self.backstory.setPlaceholderText("Optional: who this character is")
        self.backstory.setFixedHeight(70)
        self.language = QLineEdit()
        self.language.setPlaceholderText("Optional: e.g. French")
        self.emoji, self.humour = QComboBox(), QComboBox()
        self.emoji.addItems(LEVELS)
        self.humour.addItems(LEVELS)
        self.humour.setCurrentIndex(1)
        for label, w in (("Name", self.name), ("Tone", self.tone), ("Style", self.style), ("Backstory", self.backstory),
                         ("Language", self.language), ("Emoji", self.emoji), ("Humour", self.humour)):
            form.addRow(label, w)
        note = QLabel("A personality changes how the bot talks, never its rules or the quality of its work.")
        note.setObjectName("dim")
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> dict:
        return {"label": self.name.text(), "tone": self.tone.text(), "style": self.style.text(),
                "backstory": self.backstory.toPlainText(), "language": self.language.text(),
                "emoji": self.emoji.currentIndex(), "humour": self.humour.currentIndex()}
