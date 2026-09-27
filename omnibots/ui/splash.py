"""The launch intro (user, 2026-09-26): the OmniBots video (7 s), synced to the real startup.

  - the engine reports its stages (engine.signals.startup_stage): the text and a thin progress
    line under the video follow them
  - the engine is ready before the video ends → the video speeds up to finish sooner
  - the video ends before the engine is ready → it holds on the last frame ("OMNIBOTS")
  - click or Esc skips (it still waits for the engine, on the last frame)
  - no video playback available → the poster frame, same behaviour

The frames are painted here (a QVideoSink), so the text and progress sit on top of the video.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

from omnibots.ui import theme

ASSETS = Path(__file__).parent / "assets"
VIDEO, POSTER = ASSETS / "splash.mp4", ASSETS / "splash_poster.jpg"
HURRY_RATE = 2.5                      # once everything is loaded
HOLD_AT = 0.97                        # not loaded yet: wait on the last frame


class Splash(QWidget):
    finished = Signal()

    def __init__(self, video: Path = VIDEO, poster: Path = POSTER, *, width: int = 960, play: bool = True):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        # solid dark from the first instant (no see-through flash); the corners are rounded by a mask
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), QColor("#05070d"))
        self.setPalette(pal)
        self.setWindowOpacity(0.0)                       # faded in once Windows has drawn it (showEvent)
        self.setWindowTitle("OmniBots · Starting")
        self.resize(width, int(width * 9 / 16))
        scr = QGuiApplication.primaryScreen().availableGeometry()
        self.move(scr.center() - self.rect().center())
        self.poster = QImage(str(poster))
        self.frame: QImage | None = None
        self.stage, self.progress = "Starting…", 0.0
        self.ready = self.skipped = self.done = False
        self.error: str | None = None
        self.video_ended = False
        self.player = None
        if play and video.exists():
            try:
                from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
                self.player = QMediaPlayer(self)
                self.sink = QVideoSink(self)
                self.player.setVideoSink(self.sink)
                self.sink.videoFrameChanged.connect(self._on_frame)
                self.player.mediaStatusChanged.connect(self._on_status)
                self.player.errorOccurred.connect(lambda *_: self._no_video())
                self.player.setSource(QUrl.fromLocalFile(str(video)))
            except Exception:
                self.player = None
        if self.player is None:
            self.video_ended = True                      # just the poster
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._check)
        self._tick.start(50)

    # ── inputs ─────────────────────────────────────────────────────────
    def start(self) -> None:
        """Play; the window appears with the first decoded frame (no see-through flash), or after 0.6 s."""
        if self.player is not None:
            self.player.play()
            QTimer.singleShot(600, self.show)
        else:
            self.show()

    def set_stage(self, text: str, done: float) -> None:
        self.stage, self.progress = text, max(self.progress, float(done))
        self.update()

    def set_ready(self, error: str | None = None) -> None:
        self.ready, self.error = True, error
        self.progress = 1.0
        if error:
            self.stage = f"✖ {error}"
        self._check()

    def skip(self) -> None:
        self.skipped = True
        if self.player is not None:
            self.player.pause()
        self.video_ended = True
        self._check()

    # ── video ──────────────────────────────────────────────────────────
    def _on_frame(self, f) -> None:
        img = f.toImage()
        if not img.isNull():
            self.frame = img
            if not self.isVisible() and not self.done:
                self.show()
            self.update()

    def _on_status(self, status) -> None:
        from PySide6.QtMultimedia import QMediaPlayer
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.video_ended = True
            self._check()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._no_video()

    def _no_video(self) -> None:
        self.player = None
        self.video_ended = True
        self._check()

    def video_position(self) -> float:
        if self.player is None or self.player.duration() <= 0:
            return 1.0 if self.video_ended else 0.0
        return self.player.position() / self.player.duration()

    def _check(self) -> None:
        """Keep the video in step with the startup; finish when both are done."""
        if self.done:
            return
        p = self.player
        if p is not None and not self.video_ended:
            if self.ready:
                if p.playbackRate() != HURRY_RATE:
                    p.setPlaybackRate(HURRY_RATE)        # loaded: don't make the user wait
            elif self.video_position() >= HOLD_AT:
                p.pause()                                # not loaded yet: hold on "OMNIBOTS"
                self.video_ended = True
        if self.ready and (self.video_ended or self.error):
            self.done = True
            self._tick.stop()
            if self.player is not None:
                self.player.stop()
            self.hide()
            self.finished.emit()
        self.update()

    # ── looks ──────────────────────────────────────────────────────────
    def showEvent(self, e) -> None:
        """Windows shows a new window ~0.2 s before it's drawn (what's behind shows through), so
        start invisible and fade in after that."""
        super().showEvent(e)
        from PySide6.QtCore import QPropertyAnimation
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setDuration(250)
        QTimer.singleShot(300, self._fade.start)

    def resizeEvent(self, e) -> None:
        from PySide6.QtGui import QRegion
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), 18, 18)
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))
        super().resizeEvent(e)

    def mousePressEvent(self, _e) -> None:
        self.skip()

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.skip()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect())
        clip = QPainterPath()
        clip.addRoundedRect(r, 18, 18)
        p.setClipPath(clip)
        p.fillRect(r, QColor("#05070d"))
        img = self.frame if self.frame is not None else self.poster     # the last frame stays while holding
        if img is not None and not img.isNull():
            p.drawImage(r, img)
        # a soft shade at the bottom, then the stage and the progress line
        shade = QLinearGradient(0, r.height() * 0.72, 0, r.height())
        shade.setColorAt(0, QColor(5, 7, 13, 0))
        shade.setColorAt(1, QColor(5, 7, 13, 210))
        p.fillRect(r, shade)
        f = QFont("Segoe UI")
        f.setPixelSize(15)
        p.setFont(f)
        p.setPen(QColor(theme.RED) if self.error else QColor("#bfefff"))
        p.drawText(QRectF(28, r.height() - 58, r.width() - 56, 24), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self.stage)
        p.setPen(QColor(255, 255, 255, 110))
        f.setPixelSize(12)
        p.setFont(f)
        p.drawText(QRectF(28, r.height() - 58, r.width() - 56, 24), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                   "click to skip" if not self.skipped else "")
        track = QRectF(28, r.height() - 26, r.width() - 56, 3)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 40))
        p.drawRoundedRect(track, 1.5, 1.5)
        p.setBrush(QColor("#6fe3ff"))
        p.drawRoundedRect(QRectF(track.left(), track.top(), track.width() * self.progress, track.height()), 1.5, 1.5)
        p.setClipping(False)
        p.setPen(QColor(111, 227, 255, 90))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 18, 18)
        p.end()
