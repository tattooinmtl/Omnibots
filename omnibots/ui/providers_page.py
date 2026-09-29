"""Settings → Providers. Edits the same settings.json Omni uses, or OmniBots' own copy when Omni isn't installed."""

from __future__ import annotations

from html import escape
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from omnibots.omni.providers_store import (
    ProviderDocumentError, ProviderRow, apply_provider, clear_provider_key, delete_provider, list_providers,
)
from omnibots.ui import theme


def _source_text(row: ProviderRow) -> str:
    suffix = f" ({row.key_mask})" if row.key_mask != "(none)" else ""
    if row.key_source == "settings":
        return f"The key is saved in Omni's settings file{suffix}."
    if row.key_source == "vault":
        return f"The key is stored encrypted in OmniBots' database{suffix}. Only your Windows user can read it."
    if row.key_source == "env":
        return f"The key comes from the environment{suffix}. Leave the box blank and it stays there, not in the file."
    if row.key_source.startswith("account:"):
        return f"The key in use is the active account ({row.key_source.split(':', 1)[1]}){suffix}."
    if row.key_source == "keyless":
        return "This provider does not need a key."
    return "No key saved yet."


class ProvidersPage(QWidget):
    def __init__(self, settings_json: Path | None = None, engine=None, *, autoload: bool = False,
                 keys_db: Path | None = None):
        super().__init__()
        self.engine = engine
        self._rows: dict[str, ProviderRow] = {}
        self._adding = False
        self._loaded = False
        self._discover_error = ""
        self.keys_db = Path(keys_db) if keys_db else None     # set = no Omni: keys go to the encrypted store
        self.path = Path(settings_json) if settings_json else self._discover()

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        title = QLabel("Providers")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        root.addWidget(title)
        if self.keys_db is not None:
            note = QLabel("Omni isn't installed, so OmniBots keeps its own provider list, in Omni's format "
                          "(install Omni later and both use Omni's). Keys you type are stored encrypted in "
                          "OmniBots' database, never in a file. Only your Windows user can read them.")
        else:
            note = QLabel("These are Omni's providers, stored in the same settings file Omni uses. "
                          "Adding, editing, or removing one here changes it for both. "
                          "A key is written only when you type a new one. "
                          "OmniBots' own settings file never holds provider keys.")
        note.setWordWrap(True)
        note.setObjectName("dim")
        root.addWidget(note)

        split = QSplitter()
        split.setChildrenCollapsible(False)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 8, 8, 0)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter providers")
        self.filter.textChanged.connect(self._apply_filter)
        lv.addWidget(self.filter)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(lambda _cur, _prev: self._show_selected())
        lv.addWidget(self.list, 1)
        split.addWidget(left)

        form_host = QWidget()
        form_col = QVBoxLayout(form_host)
        form_col.setContentsMargins(8, 8, 0, 0)
        self.kind = QLabel("")
        self.kind.setObjectName("dim")
        form_col.addWidget(self.kind)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.name = QLineEdit()
        self.label_edit = QLineEdit()
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://api.example.com/v1")
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Leave blank to keep the current key")
        self.show_key = QCheckBox("Show key")
        self.show_key.toggled.connect(
            lambda on: self.key.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        self.keyless = QCheckBox("This provider does not need a key")
        self.keyless.toggled.connect(self._on_keyless)
        self.native = QCheckBox("Native tool calls")
        self.native.setChecked(True)
        self.account = QComboBox()
        self.account_label = QLabel("Active account")
        self.source = QLabel("")
        self.source.setWordWrap(True)
        self.source.setObjectName("dim")
        form.addRow("Name", self.name)
        form.addRow("Label", self.label_edit)
        form.addRow("Base URL", self.url)
        form.addRow("API key", self.key)
        form.addRow("", self.show_key)
        form.addRow("", self.keyless)
        form.addRow("", self.native)
        form.addRow(self.account_label, self.account)
        form_col.addLayout(form)
        form_col.addWidget(self.source)
        self.account_label.setVisible(False)
        self.account.setVisible(False)

        buttons = QHBoxLayout()
        self.new_btn = QPushButton("New")
        self.refresh_btn = QPushButton("Refresh")
        self.save_btn = QPushButton("Save")
        self.save_btn.setObjectName("primary")
        self.save_btn.setDefault(True)
        self.clear_btn = QPushButton("Clear key")
        self.remove_btn = QPushButton("Remove")
        for btn in (self.new_btn, self.refresh_btn, self.save_btn, self.clear_btn, self.remove_btn):
            buttons.addWidget(btn)
        buttons.addStretch(1)
        self.new_btn.clicked.connect(self.begin_add)
        self.refresh_btn.clicked.connect(lambda: self.reload(select=self._current_name()))
        self.save_btn.clicked.connect(self.save)
        self.clear_btn.clicked.connect(self.clear_key)
        self.remove_btn.clicked.connect(self.remove_current)
        form_col.addLayout(buttons)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.RichText)
        form_col.addWidget(self.status)
        form_col.addStretch(1)
        split.addWidget(form_host)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        root.addWidget(split, 1)
        self.path_label = QLabel(str(self.path) if self.path else self._discover_error)
        self.path_label.setObjectName("dim")
        self.path_label.setWordWrap(True)
        root.addWidget(self.path_label)
        if self.path is None:
            self._set_enabled(False)
            self._say(self._discover_error or "Omni was not found.", bad=True)
        elif autoload:
            self.ensure_loaded()

    def confirm(self, title: str, body: str) -> bool:
        answer = QMessageBox.question(self, title, body, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                       QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    def ensure_loaded(self) -> None:
        if self._loaded or self.path is None:
            return
        self._loaded = True
        self.reload()

    def reload(self, select: str | None = None) -> None:
        if self.path is None:
            return
        try:
            rows = list_providers(self.path, keys_db=self.keys_db)
        except ProviderDocumentError as exc:
            self._say(str(exc), bad=True)
            return
        self._rows = {row.name: row for row in rows}
        self.list.blockSignals(True)
        self.list.clear()
        for row in rows:
            if row.key_source == "keyless":
                hint = "no key needed"
            elif row.disk_has_key or row.key_source == "env":
                hint = "key set"
            else:
                hint = "no key"
            item = QListWidgetItem(f"{row.name}    {hint}")
            item.setData(Qt.ItemDataRole.UserRole, row.name)
            tip = row.label
            if row.base_url.startswith(("http://", "https://")):
                tip = f"{row.label}\n{row.base_url}"
            item.setToolTip(tip)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._apply_filter()
        self._loaded = True
        if select and select in self._rows:
            self._select(select)
        elif self.list.count() and not self._adding:
            self.list.setCurrentRow(self._first_visible())
        elif not self._rows:
            self.begin_add()

    def begin_add(self) -> None:
        self._adding = True
        self.list.clearSelection()
        self.name.setReadOnly(False)
        self.name.clear()
        self.label_edit.clear()
        self.url.clear()
        self.key.clear()
        self.keyless.setChecked(False)
        self.native.setChecked(True)
        self.account.clear()
        self._show_accounts(False)
        self.kind.setText("New provider")
        self.source.setText("Type a key, or mark the provider as not needing one (a local server, for example).")
        self.clear_btn.setEnabled(False)
        self.remove_btn.setEnabled(False)
        self.name.setFocus()

    def save(self) -> None:
        if self.path is None:
            return
        name = self.name.text().strip()
        row = None if self._adding else self._rows.get(name)
        if self.keyless.isChecked():
            action = "keyless"
        elif self.key.text().strip():
            action = "set"
        elif row is not None and row.key_source == "keyless":
            action = "clear"
        else:
            action = "keep"
        if self._adding and name in self._rows:
            self._say(f"{name} already exists. Select it in the list to edit it.", bad=True)
            return
        if self._adding and action == "keep":
            self._say("Type a key, or mark the provider as not needing one.", bad=True)
            return
        label = self.label_edit.text()
        if row is not None and not row.saved_label and label.strip() == row.name:
            label = ""
        try:
            apply_provider(
                self.path,
                name=name,
                base_url=self.url.text(),
                label=label,
                native_tools=self.native.isChecked(),
                key_action=action,
                api_key=self.key.text() if action == "set" else "",
                active_account=self.account.currentText() if self.account.isVisible() else None,
                keys_db=self.keys_db,
            )
        except ProviderDocumentError as exc:
            self._say(str(exc), bad=True)
            return
        self.key.clear()
        self._adding = False
        self.reload(select=name)
        self._say(f"Saved {name}. OmniBots uses it within a few seconds." if self.keys_db is not None else
                  f"Saved {name}. Omni picks this up the next time it loads settings, and OmniBots within a few seconds.")
        self._ping_engine()

    def clear_key(self) -> None:
        row = self._current_row()
        if row is None or self.path is None:
            return
        if not row.disk_has_key and row.key_source == "env":
            self._say("Nothing is saved in the file for this key. It still comes from the environment.", bad=True)
            return
        if not self.confirm("Clear key", f"Clear the saved key for {row.name}?"):
            return
        try:
            clear_provider_key(self.path, row.name, keys_db=self.keys_db)
        except ProviderDocumentError as exc:
            self._say(str(exc), bad=True)
            return
        self.reload(select=row.name)
        self._say(f"Cleared the saved key for {row.name}.")
        self._ping_engine()

    def remove_current(self) -> None:
        row = self._current_row()
        if row is None or self.path is None:
            return
        if row.builtin and not row.has_overlay:
            self._say(f"{row.name} is already Omni's built-in entry.")
            return
        if row.builtin:
            body = (f"{row.name} ships with Omni, so removing it drops the key, URL, and label you saved. "
                    "The built-in entry comes back.")
        else:
            body = f"Remove {row.name} from Omni's settings? Omni will no longer have this provider."
        if not self.confirm("Remove provider", body):
            return
        try:
            outcome = delete_provider(self.path, row.name, keys_db=self.keys_db)
        except ProviderDocumentError as exc:
            self._say(str(exc), bad=True)
            return
        self.reload(select=row.name if outcome == "reset" else None)
        if outcome == "reset":
            self._say(f"Reset {row.name} to Omni's built-in entry.")
        else:
            self._say(f"Removed {row.name}.")
        self._ping_engine()

    def _show_selected(self) -> None:
        row = self._current_row()
        if row is None:
            return
        self._adding = False
        self._fill(row)
        self.clear_btn.setEnabled(True)
        self.remove_btn.setEnabled(True)

    def _fill(self, row: ProviderRow) -> None:
        self.name.setReadOnly(True)
        self.name.setText(row.name)
        self.label_edit.setText(row.saved_label or row.label)
        self.url.setText(row.base_url)
        self.key.clear()
        self.key.setPlaceholderText(
            "Leave blank to keep the current key" if row.disk_has_key or row.key_source == "env"
            else "No key saved yet")
        self.keyless.setChecked(row.key_source == "keyless")
        self.native.setChecked(row.native_tools)
        self.account.blockSignals(True)
        self.account.clear()
        if row.accounts:
            self.account.addItems(list(row.accounts))
            if row.active_account:
                self.account.setCurrentText(row.active_account)
            self._show_accounts(True)
        else:
            self._show_accounts(False)
        self.account.blockSignals(False)
        where = "Included with Omni" if row.builtin else "Added by you"
        extra = " · your edits are saved" if row.builtin and row.has_overlay else ""
        self.kind.setText(f"{where}{extra}")
        detail = _source_text(row)
        if row.error:
            detail = f"{detail} {row.error}"
        self.source.setText(detail)

    def _on_keyless(self, checked: bool) -> None:
        self.key.setEnabled(not checked)
        if checked:
            self.key.clear()

    def _show_accounts(self, show: bool) -> None:
        self.account_label.setVisible(show)
        self.account.setVisible(show)

    def _apply_filter(self) -> None:
        query = self.filter.text().strip().lower()
        for i in range(self.list.count()):
            item = self.list.item(i)
            row = self._rows.get(item.data(Qt.ItemDataRole.UserRole))
            blob = f"{item.text()} {(row.label if row else '')}".lower()
            item.setHidden(bool(query) and query not in blob)

    def _current_name(self) -> str | None:
        item = self.list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _current_row(self) -> ProviderRow | None:
        name = self._current_name()
        return self._rows.get(name) if name else None

    def _select(self, name: str) -> None:
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == name:
                self.list.setCurrentItem(item)
                return

    def _first_visible(self) -> int:
        for i in range(self.list.count()):
            if not self.list.item(i).isHidden():
                return i
        return 0

    def _set_enabled(self, on: bool) -> None:
        for widget in (self.filter, self.list, self.name, self.label_edit, self.url, self.key,
                       self.show_key, self.keyless, self.native, self.account, self.new_btn,
                       self.refresh_btn, self.save_btn, self.clear_btn, self.remove_btn):
            widget.setEnabled(on)

    def _say(self, text: str, *, bad: bool = False) -> None:
        color = theme.RED if bad else theme.GREEN
        mark = "✖" if bad else "✔"
        self.status.setText(f"<span style='color:{color}'>{mark} {escape(text)}</span>")

    def _ping_engine(self) -> None:
        reload = getattr(self.engine, "reload_omni", None)
        submit = getattr(self.engine, "submit", None)
        if reload is None or submit is None:
            return
        try:
            submit(reload())
        except Exception:
            return

    def _discover(self) -> Path | None:
        """Omni's settings.json, or OmniBots' own copy (created if missing) when Omni isn't installed.
        Honours `[omni] install_root` from settings.toml, like the engine."""
        try:
            from omnibots.omni.locate import resolve_location
            root = getattr(self.engine, "omni_install_root", None)
            if root is None:
                from omnibots.paths import get_paths
                from omnibots.settings import load_settings
                root = ((load_settings(get_paths().settings_file).get("omni") or {}).get("install_root") or "")
            loc = resolve_location(root)
            if loc.keys_in_db and self.keys_db is None:
                self.keys_db = loc.db_file
            return loc.settings_file
        except Exception as exc:
            self._discover_error = str(exc)
            return None
