"""Reusable pieces of the bot window (PLAN.md A11.c): glass panels, the animated face,
the ID card, console/thinking panels, the chat, the prompt box, the file explorer and
the team strip. Styled by ui/theme.py (the LayoutPlan look)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QDir, QFileInfo, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QFont, QIcon, QPainter, QPen, QPixmap, QSyntaxHighlighter, QTextCharFormat,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QFileIconProvider, QFileSystemModel, QFrame, QHBoxLayout, QLabel, QLayout,
    QLineEdit, QMenu,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QToolButton, QTreeView, QVBoxLayout, QWidget,
)

from omnibots.runtime.approvals import MORE_TOKENS
from omnibots.ui import theme
from omnibots.ui.animator import FaceAnimator
from omnibots.ui.omi_face import render_face
from omnibots.ui.props import job_for_role


# ── glass panel ─────────────────────────────────────────────────────────────
class GlassPanel(QFrame):
    """A rounded navy panel with a thin blue border (objectName drives the QSS)."""

    def __init__(self, accent: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("glassAccent" if accent else "glass")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)


class PanelHeader(QWidget):
    def __init__(self, title: str, dot: str | None = None, buttons: bool = True, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 10, 6)
        t = QLabel(title)
        t.setObjectName("panelTitle")
        row.addWidget(t)
        if dot:
            d = QLabel("●")
            d.setStyleSheet(f"color: {dot}; font-size: 10px;")
            row.addWidget(d)
        row.addStretch(1)
        if buttons:
            for glyph, tip in (("–", "minimize"), ("⤢", "expand"), ("✕", "close")):
                b = QToolButton()
                b.setText(glyph)
                b.setToolTip(tip)
                b.setStyleSheet(f"QToolButton {{ border: none; color: {theme.TEXT_DIM}; font-size: 13px; padding: 2px 6px; }}"
                                f"QToolButton:hover {{ color: {theme.TEXT}; }}")
                row.addWidget(b)


# ── the animated face ───────────────────────────────────────────────────────
class FaceWidget(QWidget):
    """Omi's face + thought bubble, animated at ~30 fps by a FaceAnimator."""

    def __init__(self, accent: str = theme.ACCENT, face_px: int = 150, bubble: bool = True, parent=None,
                 badge: str | None = None):
        super().__init__(parent)
        self.anim = FaceAnimator(accent=accent, badge=badge)
        self.face_px, self.bubble = face_px, bubble
        self.setMinimumSize(QSize(face_px, face_px))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)

    def _tick(self) -> None:
        self.anim.advance(0.033)
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self.face_px
        top = self.height() - s
        self.anim.paint_face(p, QRectF(0, top, s, s))
        if self.bubble:
            self.anim.paint_bubble(p, QPointF(s * 1.02, top + s * 0.14), QPointF(s * 0.80, top + s * 0.30),
                                   max_w=max(140.0, self.width() - s * 1.05))
        p.end()


def mini_face(accent: str, mood: str = "happy", px: int = 44, badge: str | None = None) -> QPixmap:
    """A small face for lists. Under 80 px the badge would be unreadable inside the face, so the
    face shrinks to the bottom-right and a bigger badge sits in the free top-left corner."""
    if not badge or px >= 80:
        return QPixmap.fromImage(render_face(px, mood, accent, ring=False, badge=badge))
    from omnibots.ui.props import badge_image
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    fs = int(px * 0.86)
    p.drawImage(QPointF(px - fs, px - fs), render_face(fs, mood, accent, ring=False))
    p.drawImage(QPointF(0, 0), badge_image(badge, accent, int(px * 0.46)))
    p.end()
    return pm


def user_avatar(px: int = 34) -> QPixmap:
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(theme.ACCENT), 2))
    p.setBrush(QColor("#13306e"))
    p.drawEllipse(QRectF(1, 1, px - 2, px - 2))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(theme.TEXT))
    p.drawEllipse(QPointF(px / 2, px * 0.38), px * 0.15, px * 0.15)
    p.drawChord(QRectF(px * 0.24, px * 0.56, px * 0.52, px * 0.5), 0, 180 * 16)
    p.end()
    return pm


# ── ID card ─────────────────────────────────────────────────────────────────
@dataclass
class BotCard:
    name: str = "Omi"
    role: str = "boss"
    status: str = "Online"
    tagline: str = "Always curious. Always helpful."
    seat: str = "MiniMax seat 1"
    model: str = "minimax.io/m3"
    usage_pct: float = 0.0
    accent: str = theme.ACCENT


class IDCard(GlassPanel):
    def __init__(self, card: BotCard, parent=None):
        super().__init__(accent=False, parent=parent)
        self.card = card
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 12, 14, 12)
        self.face = FaceWidget(card.accent, face_px=150, bubble=False, badge=job_for_role(card.role))
        self.face.setFixedSize(150, 150)
        lay.addWidget(self.face)
        info = QVBoxLayout()
        info.setSpacing(4)
        self.name = QLabel(card.name)
        self.name.setStyleSheet("font-size: 22px; font-weight: 600;")
        self.status = QLabel()
        self.tagline = QLabel(card.tagline)
        self.tagline.setObjectName("dim")
        self.tagline.setWordWrap(True)
        info.addStretch(1)
        for w in (self.name, self.status, self.tagline):
            info.addWidget(w)
        info.addSpacing(8)
        self.facts = QLabel()
        self.facts.setObjectName("faint")
        info.addWidget(self.facts)
        self.usage = UsageBar(card.accent)
        info.addWidget(self.usage)
        info.addSpacing(6)
        self.computer_btn = QPushButton("🖥  Open computer")           # A10.f.05: watch the bot's own computer
        self.computer_btn.setObjectName("primary")
        self.computer_btn.setToolTip("See this bot's computer on the server live; take over the mouse if needed")
        self.computer_btn.setStyleSheet(f"QPushButton#primary {{ background: {theme.ACCENT}; border-radius: 10px; "
                                        f"padding: 6px 12px; font-weight: 600; }}")
        info.addWidget(self.computer_btn)
        info.addStretch(1)
        lay.addLayout(info, 1)
        self.overlay = BubbleOverlay(self.face.anim, self.face, self)   # Omi's thoughts float over the card
        self.refresh()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.overlay.setGeometry(self.rect())
        self.overlay.raise_()

    def refresh(self) -> None:
        c = self.card
        col = {"Online": theme.GREEN, "Working": theme.ACCENT_CYAN, "Paused": theme.AMBER,
               "Waiting": theme.AMBER, "Stopped": theme.TEXT_DIM, "Needs approval": theme.RED}.get(c.status, theme.GREEN)
        self.status.setText(f"<span style='color:{col}'>●</span>&nbsp; {c.status}")
        self.facts.setText(f"{c.role} · {c.seat}<br>{c.model}")
        self.usage.set_value(c.usage_pct)


class BubbleOverlay(QWidget):
    """Paints the face's thought bubble on top of the ID card, rising from Omi's head.
    Transparent to the mouse; the face widget's animator drives it."""

    def __init__(self, anim: FaceAnimator, face: QWidget, parent: QWidget):
        super().__init__(parent)
        self.anim, self.face = anim, face
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._t = QTimer(self)
        self._t.timeout.connect(self.update)
        self._t.start(33)

    def paintEvent(self, _ev) -> None:
        if not self.anim.bubble:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = self.face.geometry()
        # the cloud floats beside the head (over the name, for a few seconds); puffs lead to the head
        head = QPointF(g.left() + g.width() * 0.82, g.top() + g.height() * 0.30)
        anchor = QPointF(g.right() + 8, g.top() + g.height() * 0.66)
        self.anim.paint_bubble(p, anchor, head, max_w=max(160.0, self.width() - anchor.x() - 10))
        p.end()


class UsageBar(QWidget):
    def __init__(self, accent: str, parent=None):
        super().__init__(parent)
        self.accent, self.value = accent, 0.0
        self.setFixedHeight(18)

    def set_value(self, pct: float) -> None:
        self.value = max(0.0, min(100.0, pct))
        self.update()

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0, 7, self.width() - 44, 5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.BG3))
        p.drawRoundedRect(r, 2.5, 2.5)
        p.setBrush(QColor(self.accent))
        p.drawRoundedRect(QRectF(r.left(), r.top(), r.width() * self.value / 100, r.height()), 2.5, 2.5)
        p.setPen(QColor(theme.TEXT_DIM))
        f = QFont(theme.FONT_UI)
        f.setPixelSize(11)
        p.setFont(f)
        p.drawText(QRectF(r.right() + 6, 0, 40, 18), Qt.AlignmentFlag.AlignVCenter, f"{self.value:.0f}%")
        p.end()


# ── console / thinking ──────────────────────────────────────────────────────
class TerminalPanel(GlassPanel):
    """A mono, dark log. kind='console' colors prompts/commands; 'thinking' is dim prose."""

    def __init__(self, title: str, kind: str = "console", dot: str | None = None, parent=None):
        super().__init__(parent=parent)
        self.kind = kind
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 10)
        lay.addWidget(PanelHeader(title, dot))
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        mono = QFont("Cascadia Mono")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPixelSize(12)
        self.view.setFont(mono)
        fg = theme.TERM_TEXT if kind == "console" else theme.TEXT_DIM
        self.view.setStyleSheet(f"QPlainTextEdit {{ background: transparent; border: none; color: {fg}; padding: 4px 14px; }}")
        if kind == "console":
            ConsoleHighlighter(self.view.document())
        lay.addWidget(self.view, 1)

    def append(self, text: str) -> None:
        self.view.appendPlainText(text)
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def write(self, text: str) -> None:
        """Streamed pieces (tokens): continue the current line instead of starting a new one."""
        from PySide6.QtGui import QTextCursor
        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertText(text)
        self.view.setTextCursor(cur)
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())
        if self.view.blockCount() > 5000:                        # keep long sessions light
            c = QTextCursor(self.view.document())
            c.movePosition(QTextCursor.MoveOperation.Start)
            c.movePosition(QTextCursor.MoveOperation.Down, QTextCursor.MoveMode.KeepAnchor, 500)
            c.removeSelectedText()


class ConsoleHighlighter(QSyntaxHighlighter):
    def highlightBlock(self, text: str) -> None:
        fmt = QTextCharFormat()
        if text.startswith("$ ") or text.startswith("user@"):
            fmt.setForeground(QColor(theme.TERM_GREEN))
            self.setFormat(0, len(text), fmt)
        elif text.startswith("▸") or text.startswith(">"):
            fmt.setForeground(QColor(theme.TERM_BLUE))
            self.setFormat(0, len(text), fmt)
        elif text.startswith("  ↳") or text.startswith("["):
            fmt.setForeground(QColor(theme.TERM_DIM))
            self.setFormat(0, len(text), fmt)
        elif text.startswith("✖") or "ERROR" in text:
            fmt.setForeground(QColor(theme.RED))
            self.setFormat(0, len(text), fmt)


# ── chat ────────────────────────────────────────────────────────────────────
class PyHighlighter(QSyntaxHighlighter):
    KW = r"\b(import|from|as|def|class|return|if|elif|else|for|while|in|with|try|except|finally|print|True|False|None)\b"

    def highlightBlock(self, text: str) -> None:
        kw = QTextCharFormat()
        kw.setForeground(QColor("#7fb4ff"))
        for m in re.finditer(self.KW, text):
            self.setFormat(m.start(), m.end() - m.start(), kw)
        s = QTextCharFormat()
        s.setForeground(QColor("#5ef0a6"))
        for m in re.finditer(r"(['\"]).*?\1", text):
            self.setFormat(m.start(), m.end() - m.start(), s)
        c = QTextCharFormat()
        c.setForeground(QColor(theme.TERM_DIM))
        i = text.find("#")
        if i >= 0:
            self.setFormat(i, len(text) - i, c)


class CodeBlock(QFrame):
    def __init__(self, filename: str, code: str, parent=None):
        super().__init__(parent)
        self.setObjectName("glass")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"QFrame#glass {{ background: {theme.TERM_BG}; border: 1px solid {theme.BORDER}; border-radius: 10px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 6)
        head = QHBoxLayout()
        head.setContentsMargins(12, 8, 8, 4)
        name = QLabel(f"🗎  {filename}")
        name.setStyleSheet(f"color: {theme.ACCENT_CYAN};")
        head.addWidget(name)
        head.addStretch(1)
        copy = QPushButton("⧉ Copy")
        copy.setStyleSheet("padding: 3px 10px; border-radius: 8px; font-size: 11px;")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(code))
        head.addWidget(copy)
        lay.addLayout(head)
        view = QPlainTextEdit(code)
        view.setReadOnly(True)
        mono = QFont("Cascadia Mono")
        mono.setPixelSize(12)
        view.setFont(mono)
        view.setStyleSheet(f"QPlainTextEdit {{ background: transparent; border: none; color: {theme.TERM_TEXT}; padding: 2px 12px; }}")
        view.setFixedHeight(min(260, 22 + 17 * (code.count(chr(10)) + 1)))
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)       # long lines scroll inside the block
        view.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        view.setMinimumWidth(0)
        PyHighlighter(view.document())
        lay.addWidget(view)


@dataclass
class ChatMessage:
    who: str                      # "bot" | "user"
    text: str
    chips: list[str] = field(default_factory=list)
    code: tuple[str, str] | None = None     # (filename, code)
    title: str = ""


class AttachButton(QToolButton):
    """The 📎: attaches files. It wiggles when you point at it and shows how many files are attached."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtCore import QPropertyAnimation
        self._angle, self.count = 0.0, 0
        self.setFixedSize(36, 36)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Attach files (the bots can open them)")
        self.setStyleSheet("QToolButton { border: none; background: transparent; }")
        self._anim = QPropertyAnimation(self, b"angle", self)
        self._anim.setDuration(520)
        for t, a in ((0, 0.0), (0.2, -20.0), (0.45, 15.0), (0.7, -8.0), (1.0, 0.0)):
            self._anim.setKeyValueAt(t, a)

    def _get_angle(self) -> float:
        return self._angle

    def _set_angle(self, a: float) -> None:
        self._angle = a
        self.update()

    from PySide6.QtCore import Property as _Property
    angle = _Property(float, _get_angle, _set_angle)          # animated by the wiggle

    def wiggle(self) -> None:
        self._anim.stop()
        self._anim.start()

    def enterEvent(self, e) -> None:
        self.wiggle()
        super().enterEvent(e)

    def set_count(self, n: int) -> None:
        self.count = n
        if n:
            self.wiggle()
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.translate(self.width() / 2, self.height() / 2)
        p.rotate(self._angle)
        f = QFont("Segoe UI Emoji")
        f.setPixelSize(23)
        p.setFont(f)
        p.drawText(QRectF(-16, -16, 32, 32), Qt.AlignmentFlag.AlignCenter, "📎")
        p.resetTransform()
        if self.count:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.ACCENT))
            p.drawEllipse(QRectF(self.width() - 16, 1, 15, 15))
            p.setPen(QColor("white"))
            f.setFamily("Segoe UI")
            f.setPixelSize(10)
            f.setBold(True)
            p.setFont(f)
            p.drawText(QRectF(self.width() - 16, 1, 15, 15), Qt.AlignmentFlag.AlignCenter, str(self.count))
        p.end()


class ChatPanel(GlassPanel):
    prompt_sent = Signal(str)
    added = Signal(str, str)              # (who, text): every message shown, for the session (ui/sessions.py)
    action = Signal(str)                  # "new_session" | "clear" from the ＋ menu

    def __init__(self, accent: str, parent=None, badge: str | None = None):
        super().__init__(parent=parent)
        self.accent, self.badge = accent, badge
        self.attachments: list[Path] = []
        self._sending: list[Path] = []
        self.replaying = False            # showing a saved session: don't save it again
        self.pick_files = lambda: QFileDialog.getOpenFileNames(self, "Attach files")[0]
        self.pick_folder = lambda: QFileDialog.getExistingDirectory(self, "Attach a folder") or None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)   # messages wrap, never scroll sideways
        self.scroll.setStyleSheet("QScrollArea { background: transparent; } QScrollArea > QWidget > QWidget { background: transparent; }")
        self.body = QWidget()
        self.col = QVBoxLayout(self.body)
        self.col.setSpacing(16)
        self.col.addStretch(1)
        self.scroll.setWidget(self.body)
        lay.addWidget(self.scroll, 1)
        lay.addWidget(self._prompt_box())

    def _prompt_box(self) -> QWidget:
        box = QFrame()
        box.setObjectName("glassAccent")
        box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        box.setStyleSheet(f"QFrame#glassAccent {{ background: {theme.BG1}; border: 1px solid {theme.ACCENT}; border-radius: 16px; }}")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(10, 6, 10, 8)
        outer.setSpacing(4)
        self.chips_box = QWidget()                              # the attached files, each with ×
        self.chips = FlowLayout(spacing=6)
        self.chips_box.setLayout(self.chips)
        self.chips_box.hide()
        outer.addWidget(self.chips_box)
        row = QHBoxLayout()
        outer.addLayout(row)
        plus = QPushButton("+")
        plus.setFixedSize(34, 34)
        plus.setToolTip("Attach files or a folder, new session, clear chat")
        plus.setStyleSheet("border-radius: 17px; font-size: 18px; padding: 0;")
        self.plus_menu = QMenu(self)
        self.plus_menu.addAction("📎  Attach files…", self.attach_files)
        self.plus_menu.addAction("🗂  Attach a folder…", self.attach_folder)
        self.plus_menu.addSeparator()
        self.plus_menu.addAction("🆕  New session", lambda: self.action.emit("new_session"))
        self.plus_menu.addAction("🧹  Clear chat…", lambda: self.action.emit("clear"))
        plus.clicked.connect(lambda: self.plus_menu.popup(plus.mapToGlobal(plus.rect().topLeft()) - QPoint(0, self.plus_menu.sizeHint().height())))
        self.plus = plus
        self.input = QLineEdit()
        self.input.setPlaceholderText("Type your prompt here… (steers this bot while it works)")
        self.input.setStyleSheet("QLineEdit { background: transparent; border: none; font-size: 14px; }")
        clip = AttachButton()
        clip.clicked.connect(self.attach_files)
        self.clip = clip
        send = QPushButton("➤")
        send.setObjectName("primary")
        send.setFixedSize(40, 40)
        send.setStyleSheet(f"QPushButton#primary {{ border-radius: 20px; font-size: 16px; background: {theme.ACCENT}; }}")
        send.clicked.connect(self._send)
        self.input.returnPressed.connect(self._send)
        for w in (plus, self.input, clip, send):
            row.addWidget(w)
        row.setStretch(1, 1)
        return box

    # ── attachments ────────────────────────────────────────────────────
    def attach_files(self) -> None:
        for p in self.pick_files() or []:
            self.attach(Path(p))

    def attach_folder(self) -> None:
        p = self.pick_folder()
        if p:
            self.attach(Path(p))

    def attach(self, path: Path) -> None:
        path = Path(path)
        if path.exists() and path not in self.attachments:
            self.attachments.append(path)
            self._refresh_chips()

    def _refresh_chips(self) -> None:
        while self.chips.count():
            item = self.chips.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        for p in self.attachments:
            b = QPushButton(("🗂 " if p.is_dir() else "📄 ") + p.name + "   ✕")
            b.setObjectName("chip")
            b.setToolTip(f"{p}\n(click to remove)")
            b.clicked.connect(lambda _=False, p=p: self.detach(p))
            self.chips.addWidget(b)
        self.chips_box.setVisible(bool(self.attachments))
        self.clip.set_count(len(self.attachments))

    def detach(self, path: Path) -> None:
        self.attachments = [p for p in self.attachments if p != path]
        self._refresh_chips()

    def take_attachments(self) -> list[Path]:
        """The files sent with the message being sent right now (LiveUI copies them for the bots)."""
        return list(self._sending)

    def _send(self) -> None:
        text = self.input.text().strip()
        if not text and not self.attachments:
            return
        self._sending = list(self.attachments)
        names = ", ".join(p.name for p in self._sending)
        shown = (text or "(see the attached files)") + (f"\n📎 {names}" if names else "")
        self.add(ChatMessage("user", shown))
        self.input.clear()
        self.prompt_sent.emit(text or "Please look at the attached files.")
        self._sending, self.attachments = [], []
        self._refresh_chips()

    def clear(self) -> None:
        """Remove every message from the view."""
        while self.col.count() > 1:                          # the last item is the stretch
            item = self.col.takeAt(0)
            lay = item.layout() if item else None
            if lay is not None:
                while lay.count():
                    it = lay.takeAt(0)
                    if it.widget():
                        it.widget().setParent(None)             # gone from the view right away
                        it.widget().deleteLater()
                lay.deleteLater()
            elif item and item.widget():
                item.widget().setParent(None)
                item.widget().deleteLater()

    def add(self, m: ChatMessage) -> None:
        if not self.replaying:
            self.added.emit(m.who, m.text)
        row = QHBoxLayout()
        row.setSpacing(12)
        bubble = QFrame()
        bubble.setObjectName("msg")
        bubble.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        inner = QVBoxLayout(bubble)
        inner.setContentsMargins(16, 12, 16, 12)
        inner.setSpacing(10)
        if m.title:
            t = QLabel(m.title)
            t.setStyleSheet("font-size: 18px; font-weight: 600; background: transparent;")
            inner.addWidget(t)
        lab = QLabel(m.text)
        lab.setWordWrap(True)
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lab.setStyleSheet("font-size: 14px; background: transparent; line-height: 140%;")
        inner.addWidget(lab)
        if m.chips:
            chips = FlowLayout(spacing=8)                        # chips wrap onto new lines
            for c in m.chips:
                b = QPushButton(c)
                b.setObjectName("chip")
                chips.addWidget(b)
            inner.addLayout(chips)
        if m.code:
            inner.addWidget(CodeBlock(*m.code))
        if m.who == "bot":
            bubble.setStyleSheet(f"QFrame#msg {{ background: {theme.BG2}; border: 1px solid {theme.BORDER}; border-radius: 14px; }}")
            face = QLabel()
            face.setPixmap(mini_face(self.accent, px=52, badge=self.badge))
            face.setAlignment(Qt.AlignmentFlag.AlignTop)
            row.addWidget(face, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(bubble, 1)
            row.addSpacing(60)
        else:
            bubble.setStyleSheet(f"QFrame#msg {{ background: #13306e; border: 1px solid {theme.ACCENT}; border-radius: 14px; }}")
            bubble.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
            bubble.setMaximumWidth(460)
            row.addStretch(1)
            row.addWidget(bubble)
            you = QLabel("👤")
            you.setStyleSheet(f"font-size: 20px; color: {theme.TEXT_DIM};")
            row.addWidget(you, 0, Qt.AlignmentFlag.AlignTop)
        self.col.insertLayout(self.col.count() - 1, row)

    def add_verdict(self, data: dict, on_rate) -> "VerdictCard":
        """A16.b: after a goal, your 👍 / 👎 on it and on each bot's accepted work."""
        card = VerdictCard(data, on_rate)
        row = QHBoxLayout()
        row.setSpacing(12)
        face = QLabel()
        face.setPixmap(mini_face(self.accent, mood="happy", px=52, badge=self.badge))
        face.setAlignment(Qt.AlignmentFlag.AlignTop)
        row.addWidget(face, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(card, 1)
        row.addSpacing(60)
        self.col.insertLayout(self.col.count() - 1, row)
        return card

    def add_approval(self, info: dict, on_decide) -> "ApprovalCard":
        """A bot is waiting for your OK (A11.e.01): a card in its chat with Approve / Deny."""
        card = ApprovalCard(info, on_decide)
        row = QHBoxLayout()
        row.setSpacing(12)
        face = QLabel()
        face.setPixmap(mini_face(self.accent, mood="surprised", px=52, badge=self.badge))
        face.setAlignment(Qt.AlignmentFlag.AlignTop)
        row.addWidget(face, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(card, 1)
        row.addSpacing(60)
        self.col.insertLayout(self.col.count() - 1, row)
        return card


class VerdictCard(QFrame):
    """How did it go? 👍 / 👎 on the goal and on each bot's accepted claim, with one optional note box:
    a thumb sends its row with the note (then the box clears). `on_rate(project_id, claim_id, verdict,
    note, done)`; `done(ok, text)` marks the row."""

    def __init__(self, data: dict, on_rate, parent=None):
        super().__init__(parent)
        self.data, self.on_rate = data, on_rate
        self.rows: dict = {}                              # claim_id (None = the goal) -> (up, down, state label)
        self.setObjectName("verdict")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"QFrame#verdict {{ background: {theme.BG2}; border: 1px solid {theme.BORDER_GLOW}; border-radius: 14px; }}")
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(8)
        head = QLabel(f"<b>How did it go?</b>  <span style='color:{theme.TEXT_DIM}'>{str(data.get('goal', ''))[:90]}</span>")
        head.setWordWrap(True)
        head.setStyleSheet("font-size: 14px; background: transparent;")
        v.addWidget(head)
        hint = QLabel("Your rating counts more than the bots' own: 👎 marks this recipe as failed, and a note becomes a lesson "
                      "in the bot's memory.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px; background: transparent;")
        v.addWidget(hint)
        v.addLayout(self._row(None, "The whole goal", data.get("verdict")))
        for c in data.get("claims", []):
            v.addLayout(self._row(c["id"], f"<b>{c['bot_name']}</b>: {c['text'][:110]}", c.get("verdict")))
        self.note = QLineEdit()
        self.note.setPlaceholderText("Optional note (what to repeat, what to do differently)…")
        v.addWidget(self.note)

    def _row(self, claim_id, label: str, given) -> QHBoxLayout:
        h = QHBoxLayout()
        text = QLabel(label)
        text.setWordWrap(True)
        text.setStyleSheet("font-size: 13px; background: transparent;")
        h.addWidget(text, 1)
        state = QLabel("")
        state.setStyleSheet(f"color: {theme.TEXT_DIM}; background: transparent;")
        h.addWidget(state)
        up, down = QPushButton("👍"), QPushButton("👎")
        for b, val in ((up, 1), (down, -1)):
            b.setFixedWidth(46)
            b.setToolTip("Good work" if val > 0 else "Not good (say why in the note)")
            b.clicked.connect(lambda _=False, cid=claim_id, vv=val: self.rate(cid, vv))
            h.addWidget(b)
        self.rows[claim_id] = (up, down, state)
        if given in (1, -1):
            self.mark(claim_id, given, "rated")
        return h

    def rate(self, claim_id, verdict: int) -> None:
        up, down, state = self.rows[claim_id]
        up.setEnabled(False)
        down.setEnabled(False)
        state.setText("saving…")
        note = self.note.text().strip()
        self.note.clear()
        self.on_rate(self.data["project_id"], claim_id, verdict, note,
                     lambda ok, text: self.mark(claim_id, verdict, text) if ok else self._failed(claim_id, text))

    def mark(self, claim_id, verdict: int, text: str = "") -> None:
        up, down, state = self.rows[claim_id]
        up.setEnabled(False)
        down.setEnabled(False)
        chosen = up if verdict > 0 else down
        chosen.setStyleSheet(f"QPushButton {{ background: {theme.GREEN if verdict > 0 else theme.AMBER}; border-radius: 8px; }}")
        state.setText(text or "rated")

    def _failed(self, claim_id, text: str) -> None:
        up, down, state = self.rows[claim_id]
        up.setEnabled(True)
        down.setEnabled(True)
        state.setText(f"✖ {text}"[:80])


class ApprovalCard(QFrame):
    """What the bot wants to do, why it asks (destructive / money / a secret place), and the buttons.
    Decided elsewhere (tray, pipe) → mark_decided() updates it."""

    def __init__(self, info: dict, on_decide, parent=None):
        super().__init__(parent)
        self.info, self.on_decide, self.decided = info, on_decide, None
        self.setObjectName("approval")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        tokens = info.get("tool") == MORE_TOKENS
        money = info.get("risk") == "R4"
        edge = theme.AMBER if (money or tokens) else theme.RED
        self.setStyleSheet(f"QFrame#approval {{ background: {theme.BG2}; border: 1px solid {edge}; border-radius: 14px; }}")
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 12, 16, 12)
        v.setSpacing(8)
        why = {"R4": "costs money", "R5": "can't be undone"}.get(str(info.get("risk")), "needs your OK")
        who = (info.get("rehearsal") or {}).get("bot_name") or (info.get("rehearsal") or {}).get("bot") or "a bot"
        head = QLabel(f"⏸  <b>Omi asks: more tokens for {who}?</b> (background work, today)" if tokens else
                      f"⚠  Needs your OK: <b>{info.get('tool')}</b> ({info.get('risk')}, {why})")
        head.setStyleSheet(f"color: {edge}; font-size: 14px; background: transparent;")
        v.addWidget(head)
        body = QLabel(str(info.get("summary") or "") + (f"<br><span style='color:{theme.TEXT_DIM}'>on {info.get('host')}</span>"
                                                          if info.get("host") else ""))
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setStyleSheet("font-size: 14px; background: transparent;")
        v.addWidget(body)
        if info.get("rehearsal"):
            import json as _json
            r = info["rehearsal"]
            details = QLabel(f"Used today {int(r.get('used_today', 0)):,}  ·  allocated {int(r.get('allocated_today', 0)):,}  ·  "
                             f"asking for {int(r.get('asking_for', 0)):,}" if tokens else
                             _json.dumps(r, indent=1, ensure_ascii=False)[:600])
            details.setWordWrap(True)
            details.setStyleSheet(f"color: {theme.TEXT_DIM}; font-family: Consolas, monospace; font-size: 12px; background: transparent;")
            v.addWidget(details)
        row = QHBoxLayout()
        self.approve = QPushButton("✔  Approve")
        self.deny = QPushButton("✖  Deny")
        self.approve.setStyleSheet(f"QPushButton {{ background: {theme.GREEN}; color: #04140b; border-radius: 10px; padding: 6px 16px; font-weight: 600; }}")
        self.deny.setStyleSheet(f"QPushButton {{ background: {theme.BG3}; border: 1px solid {theme.BORDER}; border-radius: 10px; padding: 6px 16px; }}")
        self.approve.clicked.connect(lambda: self._choose(True))
        self.deny.clicked.connect(lambda: self._choose(False))
        row.addWidget(self.approve)
        row.addWidget(self.deny)
        row.addStretch(1)
        self.state = QLabel("")
        self.state.setStyleSheet("background: transparent;")
        row.addWidget(self.state)
        v.addLayout(row)

    def _choose(self, ok: bool) -> None:
        if self.decided is None:
            self.on_decide(self.info["id"], ok)
            self.mark_decided(ok)

    def mark_decided(self, ok: bool, by: str = "") -> None:
        self.decided = ok
        self.approve.setEnabled(False)
        self.deny.setEnabled(False)
        col = theme.GREEN if ok else theme.TEXT_DIM
        self.state.setText(f"<span style='color:{col}'>{'✔ Approved' if ok else '✖ Denied'}{(' ' + by) if by else ''}</span>")


class FlowLayout(QLayout):
    """Lays widgets out left to right and wraps onto new lines (Qt's flow layout example)."""

    def __init__(self, parent=None, spacing: int = 8):
        super().__init__(parent)
        self._items: list = []
        self._space = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._layout(QRect(0, 0, width, 0), test=True)

    def setGeometry(self, rect) -> None:
        super().setGeometry(rect)
        self._layout(rect, test=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        return s

    def _layout(self, rect, test: bool) -> int:
        x, y, line_h = rect.x(), rect.y(), 0
        for it in self._items:
            w = it.sizeHint()
            if x + w.width() > rect.right() + 1 and line_h > 0:
                x, y, line_h = rect.x(), y + line_h + self._space, 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), w))
            x += w.width() + self._space
            line_h = max(line_h, w.height())
        return y + line_h - rect.y()


class FileIcons(QFileIconProvider):
    """Drawn icons like LayoutPlan's: blue folders, a Python badge, docs, git, config."""

    def __init__(self):
        super().__init__()
        self._cache: dict[str, QIcon] = {}

    def icon(self, info):
        if not isinstance(info, QFileInfo):
            return super().icon(info)
        if info.isDir():
            key = "dir"
        else:
            ext = info.suffix().lower()
            name = info.fileName().lower()
            key = ("git" if name.startswith(".git") else "py" if ext == "py" else
                   "doc" if ext in ("md", "txt", "rst") else "cfg" if ext in ("toml", "json", "yaml", "yml", "ini", "cfg") else "file")
        if key not in self._cache:
            self._cache[key] = QIcon(_draw_file_icon(key))
        return self._cache[key]


def _draw_file_icon(kind: str, s: int = 32) -> QPixmap:
    pm = QPixmap(s, s)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if kind == "dir":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#3b7bff"))
        p.drawRoundedRect(QRectF(3, 7, 12, 6), 2, 2)
        p.setBrush(QColor("#5b95ff"))
        p.drawRoundedRect(QRectF(3, 10, 26, 17), 3, 3)
    elif kind == "py":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#3776ab"))
        p.drawRoundedRect(QRectF(6, 4, 14, 14), 4, 4)
        p.setBrush(QColor("#ffd43b"))
        p.drawRoundedRect(QRectF(12, 14, 14, 14), 4, 4)
    elif kind == "git":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#f05033"))
        p.translate(16, 16)
        p.rotate(45)
        p.drawRoundedRect(QRectF(-10, -10, 20, 20), 3, 3)
    else:
        col = {"doc": "#a78bfa", "cfg": "#8b97b8"}.get(kind, "#8b97b8")
        p.setPen(QPen(QColor(col), 2))
        p.setBrush(QColor(theme.BG2))
        p.drawRoundedRect(QRectF(7, 3, 18, 26), 3, 3)
        for yy in (11, 16, 21):
            p.drawLine(QPointF(11, yy), QPointF(21, yy))
    p.end()
    return pm


class ElidedLabel(QLabel):
    """A one-line label that shortens itself with … in the middle instead of forcing its width."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)
        super().setText(text)

    def setText(self, text: str) -> None:
        self._full = text
        self._elide()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._elide()

    def _elide(self) -> None:
        fm = self.fontMetrics()
        m = self.contentsMargins()
        super().setText(fm.elidedText(self._full, Qt.TextElideMode.ElideMiddle, max(20, self.width() - m.left() - m.right() - 30)))


# ── file explorer ───────────────────────────────────────────────────────────
class FilesPanel(GlassPanel):
    """The File Explorer (A11.k.01, A11.m): the project's files, live. Double-click opens a file in
    the editor; right-click asks the window for its file menu (the window owns the actions)."""

    file_opened = Signal(str)
    context_menu = Signal(object)                      # a QPoint in tree-viewport coordinates

    def __init__(self, root: Path, parent=None):
        super().__init__(parent=parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 8)
        lay.addWidget(PanelHeader("📁  File Explorer"))
        path = ElidedLabel(f"🗀  {root}")                     # shortened with … : never widens the column
        path.setToolTip(str(root))
        path.setObjectName("dim")
        path.setStyleSheet(f"padding: 6px 14px; border-bottom: 1px solid {theme.BORDER}; color: {theme.TEXT_DIM};")
        lay.addWidget(path)
        self.path_label = path
        self.model = QFileSystemModel()
        self.model.setIconProvider(FileIcons())
        self.model.setRootPath(str(root))
        self.model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Hidden)
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setRootIndex(self.model.index(str(root)))
        for c in (1, 2, 3):
            self.tree.hideColumn(c)
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(18)
        self.tree.setAnimated(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.context_menu.emit)
        self.tree.doubleClicked.connect(self._double_clicked)
        lay.addWidget(self.tree, 1)
        self.root = Path(root)
        self.working = False

    def set_root(self, root: Path, *, working: bool | None = None) -> None:
        """Show another folder (e.g. the project a new goal works in). working=True marks the folder
        the user opened for the bots to work in (📌)."""
        if working is not None:
            self.working = working
        self.root = Path(root)
        self.model.setRootPath(str(root))
        self.tree.setRootIndex(self.model.index(str(root)))
        self.path_label.setText(("📌  " if self.working else "🗀  ") + str(root))
        self.path_label.setToolTip(str(root) + ("\nThe bots work in this folder (File → Open folder)." if self.working else ""))

    def _double_clicked(self, index) -> None:
        p = Path(self.model.filePath(index))
        if p.is_file():
            self.file_opened.emit(str(p))

    def selected_paths(self) -> list[Path]:
        rows = {self.model.filePath(i) for i in self.tree.selectionModel().selectedRows(0)}
        return [Path(r) for r in sorted(rows)]

    def target_folder(self) -> Path:
        """Where New file / New folder / Paste go: the selected folder, a selected file's folder, or the root."""
        sel = self.selected_paths()
        if sel:
            return sel[0] if sel[0].is_dir() else sel[0].parent
        return self.root

    def select(self, path: Path) -> None:
        idx = self.model.index(str(path))
        if idx.isValid():
            self.tree.setCurrentIndex(idx)
            self.tree.scrollTo(idx)


# ── team strip ──────────────────────────────────────────────────────────────
class TeamStrip(QWidget):
    bot_chosen = Signal(str)

    def __init__(self, bots: list[tuple[str, str, str, str]], current: str, parent=None):
        """bots: (bot_id, name, role, mood)."""
        super().__init__(parent)
        self.current = current
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 0, 4, 0)
        row.setSpacing(6)
        self._row = row
        self.set_team(bots)

    def set_team(self, bots: list[tuple[str, str, str, str]]) -> None:
        """(Re)build the strip, e.g. after the boss creates a new bot."""
        row, current = self._row, self.current
        while row.count():
            item = row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.bot_ids = [b[0] for b in bots]
        for bid, name, role, mood in bots:
            b = QToolButton()
            b.setIcon(mini_face(theme.role_color(role), mood, 48, badge=job_for_role(role)))
            b.setIconSize(QSize(48, 48))
            b.setText(name)
            b.setToolTip(f"{name} · {role}")
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            sel = bid == current
            ring = theme.role_color(role)
            b.setStyleSheet(f"QToolButton {{ border: 1px solid {ring if sel else 'transparent'}; border-radius: 12px; "
                            f"padding: 4px 6px; color: {theme.TEXT if sel else theme.TEXT_DIM}; font-size: 11px; "
                            f"background: {theme.BG2 if sel else 'transparent'}; }}"
                            f"QToolButton:hover {{ background: {theme.BG2}; }}")
            b.clicked.connect(lambda _=False, x=bid: self.bot_chosen.emit(x))
            row.addWidget(b)
        row.addStretch(1)


# ── message board (the bots' conversation, A11.e) ──────────────────────────────
MSG_COLORS = {
    "CLAIM_SUBMITTED": "#7fb4ff", "CLAIM_ACCEPTED": "#3ad07a", "CLAIM_REJECTED": "#ff6b8b",
    "APPROVAL_REQUEST": "#f5a524", "APPROVAL_DECISION": "#f5a524", "TASK_ASSIGNED": "#6fe3ff",
    "TASK_COMPLETED": "#3ad07a", "TASK_FAILED": "#ff6b8b", "A2A_MESSAGE": "#b99cff", "HELP_REQUEST": "#ff9f43",
    "REVIEW_RESULT": "#ff8fab", "COUNCIL_VERDICT": "#b99cff", "SEAT_WAITING": "#8b97b8", "TOOL_CREATED": "#4de3c4",
    "PLAYBOOK_UPDATED": "#4de3c4", "USER_STEER": "#2f7dff", "BOT_CREATED": "#6fe3ff", "ARTIFACT_READY": "#3ad07a",
}
FILTERS = {
    "All": lambda e, bot: True,
    "This bot": lambda e, bot: bot in (e.sender_id, e.recipient_id),
    "Claims": lambda e, bot: e.mtype.startswith("CLAIM") or e.mtype == "REVIEW_RESULT",
    "Approvals": lambda e, bot: e.mtype.startswith("APPROVAL"),
    "Problems": lambda e, bot: e.mtype in ("TASK_FAILED", "CLAIM_REJECTED", "HELP_REQUEST", "BLOCKED"),
}


@dataclass
class BoardEntry:
    time: str
    sender_id: str
    sender: str
    sender_role: str
    recipient_id: str | None
    recipient: str | None
    mtype: str
    text: str


class BoardPanel(GlassPanel):
    """The message board: every message between the bots (and you), live, filterable."""

    def __init__(self, bot_id: str = "omi", parent=None):
        super().__init__(parent=parent)
        self.bot_id = bot_id
        self.entries: list[tuple[BoardEntry, QWidget]] = []
        self.filter = "All"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 8)
        lay.addWidget(PanelHeader("💬  Message Board", dot=theme.GREEN))
        self.activity = ActivityList()
        lay.addWidget(self.activity)
        chips = QHBoxLayout()
        chips.setContentsMargins(12, 0, 12, 6)
        chips.setSpacing(6)
        self._chips: dict[str, QPushButton] = {}
        for name in FILTERS:
            b = QPushButton(name)
            b.setCheckable(True)
            b.setChecked(name == "All")
            b.clicked.connect(lambda _=False, n=name: self.set_filter(n))
            self._chips[name] = b
            chips.addWidget(b)
        chips.addStretch(1)
        lay.addLayout(chips)
        self._style_chips()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; } QScrollArea > QWidget > QWidget { background: transparent; }")
        body = QWidget()
        self.col = QVBoxLayout(body)
        self.col.setContentsMargins(10, 0, 10, 0)
        self.col.setSpacing(6)
        self.col.addStretch(1)
        self.scroll.setWidget(body)
        lay.addWidget(self.scroll, 1)

    def set_activity(self, bot_id: str, name: str, role: str, status: str, action: str) -> None:
        """One live line per bot: ● Name – what it's doing now."""
        self.activity.set(bot_id, name, role, status, action)

    def _style_chips(self) -> None:
        for name, b in self._chips.items():
            on = name == self.filter
            b.setStyleSheet(f"QPushButton {{ padding: 3px 10px; border-radius: 10px; font-size: 11px; "
                            f"background: {theme.ACCENT if on else theme.BG2}; border: 1px solid {theme.ACCENT if on else theme.BORDER}; }}")

    def set_filter(self, name: str) -> None:
        self.filter = name
        for n, b in self._chips.items():
            b.setChecked(n == name)
        self._style_chips()
        for e, w in self.entries:
            w.setVisible(FILTERS[name](e, self.bot_id))

    def add(self, e: BoardEntry) -> None:
        row = QFrame()
        row.setObjectName("boardRow")
        row.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row.setStyleSheet(f"QFrame#boardRow {{ background: {theme.BG2}; border: 1px solid {theme.BORDER}; border-radius: 10px; }}")
        g = QHBoxLayout(row)
        g.setContentsMargins(8, 7, 10, 7)
        g.setSpacing(8)
        face = QLabel()
        face.setPixmap(user_avatar(38) if e.sender_role == "user" else
                       mini_face(theme.role_color(e.sender_role), "happy", 40, badge=job_for_role(e.sender_role)))
        face.setAlignment(Qt.AlignmentFlag.AlignTop)
        g.addWidget(face, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(2)
        top = QHBoxLayout()
        who = QLabel(f"<b>{e.sender}</b>" + (f" <span style='color:{theme.TEXT_DIM}'>→</span> {e.recipient}" if e.recipient else ""))
        who.setStyleSheet("font-size: 12px; background: transparent;")
        top.addWidget(who)
        col_hex = MSG_COLORS.get(e.mtype, theme.TEXT_DIM)
        tag = QLabel(e.mtype.replace("_", " ").lower())
        tag.setStyleSheet(f"color: {col_hex}; border: 1px solid {col_hex}; border-radius: 7px; padding: 0 6px; "
                          f"font-size: 10px; background: transparent;")
        top.addWidget(tag)
        top.addStretch(1)
        when = QLabel(e.time)
        when.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 10px; background: transparent;")
        top.addWidget(when)
        col.addLayout(top)
        txt = QLabel(e.text if len(e.text) <= 240 else e.text[:237] + "…")
        txt.setWordWrap(True)
        txt.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        txt.setStyleSheet(f"color: {theme.TEXT}; font-size: 12px; background: transparent;")
        col.addWidget(txt)
        g.addLayout(col, 1)
        self.col.insertWidget(self.col.count() - 1, row)
        self.entries.append((e, row))
        row.setVisible(FILTERS[self.filter](e, self.bot_id))
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))


STATUS_DOT = {"working": theme.ACCENT_CYAN, "thinking": theme.PURPLE, "waiting": theme.AMBER, "waiting_approval": theme.AMBER,
              "paused": theme.AMBER, "idle": theme.TEXT_DIM, "done": theme.GREEN, "error": theme.RED, "blocked": theme.RED,
              "stopped": theme.TEXT_DIM}


class ActivityList(QFrame):
    """The top of the message board: who is doing what right now, one line per bot."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("activity")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"QFrame#activity {{ background: {theme.BG2}; border: 1px solid {theme.BORDER}; "
                           f"border-radius: 10px; margin: 0 12px 8px 12px; }}")
        self.col = QVBoxLayout(self)
        self.col.setContentsMargins(12, 8, 12, 8)
        self.col.setSpacing(3)
        self.rows: dict[str, QLabel] = {}
        self.setVisible(False)

    def set(self, bot_id: str, name: str, role: str, status: str, action: str) -> None:
        dot = STATUS_DOT.get(status, theme.TEXT_DIM)
        text = (f"<span style='color:{dot}; font-size:14px'>●</span>&nbsp; "
                f"<b style='color:{theme.role_color(role)}'>{name}</b>"
                f"<span style='color:{theme.TEXT_DIM}'> – </span>{action}")
        if bot_id not in self.rows:
            lab = QLabel()
            lab.setStyleSheet("background: transparent; font-size: 12px;")
            lab.setTextFormat(Qt.TextFormat.RichText)
            self.rows[bot_id] = lab
            self.col.addWidget(lab)
        self.rows[bot_id].setText(text)
        self.rows[bot_id].setToolTip(f"{name} ({role}) · {status}")
        self.setVisible(True)

    def remove(self, bot_id: str) -> None:
        lab = self.rows.pop(bot_id, None)
        if lab:
            lab.deleteLater()
        self.setVisible(bool(self.rows))
