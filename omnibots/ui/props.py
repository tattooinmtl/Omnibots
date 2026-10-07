"""Omi's extras (PLAN.md A11.d.05, user 2026-09-26):

  ACTION PROPS: what Omi is doing right now, animated — a keyboard when coding, a book
  when reading, pen & paper when writing, a magnifier when searching... Hand-held props
  sit in the bottom band (below the face screen, over the chin); "idea" props sit in the
  top-right corner. None of them cover the face screen (x 58-182, y 88-184).

  JOB BADGES: a round badge in the TOP-LEFT corner saying what kind of bot this is
  (crown = the boss, </> = coder, quill = writer...). That corner is empty in Omi's
  drawing, so the badge never blocks the face.

Everything is drawn in Omi's 240-unit box; `t` (seconds) drives the animation.
Settings shows a legend of both (ui/settings_window.py).
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPolygonF

INK = QColor("#141c3a")
PAPER = QColor("#f7f9ff")
WOOD = QColor("#c98a4b")


def _pen(color, w=4.0):
    p = QPen(QColor(color), w)
    p.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return p


def _c(color, a):
    c = QColor(color)
    c.setAlpha(a)
    return c


# ══ action props ═══════════════════════════════════════════════════════════
# action -> (label shown in the legend, which tools/events trigger it)
ACTIONS: dict[str, tuple[str, str]] = {
    "coding":    ("Coding", "write_file on code, git_commit, create_tool"),
    "reading":   ("Reading", "read_file, invoke_skill"),
    "writing":   ("Writing", "write_file on .md/.txt/docs, reports"),
    "searching": ("Searching", "grep, find_files, find_skill, web_search"),
    "browsing":  ("Browsing", "web_fetch, browser_*"),
    "running":   ("Running code", "run_python, run_shell"),
    "thinking":  ("Thinking", "model reasoning, planning"),
    "deploying": ("Deploying", "git_push, hosting deploys"),
    "reviewing": ("Reviewing", "review_work, forge review, checking claims"),
    "chatting":  ("Talking", "send_message, ask_help, wait_for_mention"),
    "waiting":   ("Waiting for you", "an approval card is open"),
    "shopping":  ("Buying (R4)", "a paid action waiting for your click"),
    "speaking":  ("Speaking", "text_to_speech"),
    "looking":   ("Looking", "describe_image, browser_screenshot, watch_video, camera_snapshot"),
    "filming":   ("Making media", "generate_video, generate_image, edit_image, generate_music"),
}

CODE_EXT = {"py", "js", "ts", "tsx", "jsx", "mjs", "java", "c", "cpp", "h", "cs", "go", "rs", "rb", "php", "sh",
            "ps1", "html", "css", "sql", "json", "toml", "yaml", "yml"}

TOOL_ACTIONS = {
    "git_commit": "coding", "git_diff": "coding", "create_tool": "coding",
    "read_file": "reading", "invoke_skill": "reading", "list_dir": "reading",
    "grep": "searching", "find_files": "searching", "find_skill": "searching", "web_search": "searching",
    "web_fetch": "browsing", "browser_navigate": "browsing", "browser_read": "browsing", "browser_click": "browsing",
    "browser_type": "browsing", "browser_press": "browsing", "browser_fill_secret": "browsing",
    "run_python": "running", "run_shell": "running", "git_push": "deploying",
    "review_work": "reviewing", "accept_claim": "reviewing", "reject_claim": "reviewing",
    "send_message": "chatting", "ask_help": "chatting", "wait_for_mention": "chatting", "ask_user": "chatting",
    "text_to_speech": "speaking", "describe_image": "looking", "browser_screenshot": "looking",
    "generate_video": "filming", "plan_goal": "thinking", "council": "thinking",
    "computer_run": "running", "computer_upload": "running", "computer_download": "running",
    "computer_click": "browsing", "computer_type": "browsing", "computer_key": "browsing",
    "computer_screenshot": "looking", "watch_video": "looking", "camera_snapshot": "looking",
    "generate_image": "filming", "edit_image": "filming", "generate_music": "filming", "clone_voice": "speaking",
    "make_document": "writing", "research": "searching",
}


def action_for_tool(name: str, args: dict | None = None) -> str | None:
    if name == "write_file":
        ext = str((args or {}).get("path", "")).rsplit(".", 1)[-1].lower()
        return "coding" if ext in CODE_EXT else "writing"
    return TOOL_ACTIONS.get(name)


def draw_prop(p: QPainter, action: str, t: float, accent: str = "#2f7dff") -> None:
    fn = _PROPS.get(action)
    if fn:
        p.save()
        fn(p, t, QColor(accent))
        p.restore()


def _keyboard(p, t, acc):                       # bottom band, keys pressing
    base = QPolygonF([QPointF(40, 202), QPointF(200, 202), QPointF(214, 236), QPointF(26, 236)])
    g = QLinearGradient(QPointF(0, 202), QPointF(0, 236))
    g.setColorAt(0, QColor("#2a3766"))
    g.setColorAt(1, QColor("#161f40"))
    p.setPen(_pen(INK, 4))
    p.setBrush(QBrush(g))
    p.drawPolygon(base)
    pressed = int(t * 9) % 14
    k = 0
    for row, (y, x0, x1, n) in enumerate(((207, 48, 192, 7), (216, 42, 198, 7), (226, 36, 204, 6))):
        w = (x1 - x0) / n
        for i in range(n):
            down = k == pressed or k == (pressed * 5 + 3) % 20
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(acc.lighter(150) if down else QColor("#c9d6f5"))
            p.drawRoundedRect(QRectF(x0 + i * w + 2, y + (1.5 if down else 0), w - 4, 6.5), 1.5, 1.5)
            k += 1
    p.setBrush(QColor("#c9d6f5"))                  # space bar
    p.drawRoundedRect(QRectF(80, 233 - 1, 80, 3), 1.5, 1.5)
    # floating code glyphs (from Omni's coding state), top-right
    f = QFont("Cascadia Mono")
    f.setBold(True)
    f.setPixelSize(16)
    p.setFont(f)
    for i, gl in enumerate(("{ }", "</>", ";")):
        ph = (t * 0.45 + i / 3) % 1
        p.setPen(_c(acc.lighter(140), int(255 * (1 - ph))))
        p.drawText(QPointF(190 + i * 6, 70 - ph * 50), gl)


def _book(p, t, acc):                           # open book, a page turning
    cx, top, bot = 120, 196, 238
    for side in (-1, 1):
        page = QPainterPath(QPointF(cx, top + 6))
        page.quadTo(QPointF(cx + side * 30, top - 3), QPointF(cx + side * 62, top + 2))
        page.lineTo(QPointF(cx + side * 62, bot - 4))
        page.quadTo(QPointF(cx + side * 30, bot - 10), QPointF(cx, bot))
        page.closeSubpath()
        p.setPen(_pen(INK, 4))
        p.setBrush(PAPER)
        p.drawPath(page)
        p.setPen(_pen("#9aa6c8", 2))
        for i in range(4):
            y = top + 11 + i * 6
            p.drawLine(QPointF(cx + side * 12, y), QPointF(cx + side * 50, y + 1))
    ph = (t * 0.6) % 1                             # the turning page sweeps right -> left
    ang = math.pi * ph
    x = cx + 60 * math.cos(ang)
    turning = QPainterPath(QPointF(cx, top + 6))
    turning.quadTo(QPointF((cx + x) / 2, top - 3 - 5 * math.sin(ang)), QPointF(x, top + 2 - 4 * math.sin(ang)))
    turning.lineTo(QPointF(x, bot - 6 - 6 * math.sin(ang)))
    turning.quadTo(QPointF((cx + x) / 2, bot - 14), QPointF(cx, bot))
    turning.closeSubpath()
    p.setPen(_pen(INK, 3))
    p.setBrush(QColor("#e3e9fb"))
    p.drawPath(turning)
    p.setPen(_pen(acc, 5))                          # spine / bookmark
    p.drawLine(QPointF(cx, top + 6), QPointF(cx, bot))


def _writing(p, t, acc):                        # sheet of paper + pen writing
    p.save()
    p.translate(160, 214)
    p.rotate(-6)
    p.setPen(_pen(INK, 4))
    p.setBrush(PAPER)
    p.drawRoundedRect(QRectF(-40, -22, 80, 46), 4, 4)
    prog = (t * 0.5) % 1
    p.setPen(_pen("#6b78a8", 2.5))
    for i in range(4):
        y = -13 + i * 9
        full = 64 if i < int(prog * 4) else (64 * ((prog * 4) % 1) if i == int(prog * 4) else 0)
        if full > 1:
            path = QPainterPath(QPointF(-32, y))
            steps = max(2, int(full / 5))
            for s in range(1, steps + 1):
                xx = -32 + full * s / steps
                path.lineTo(QPointF(xx, y + 1.6 * math.sin(xx * 0.8)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)
    row = min(3, int(prog * 4))
    tip = QPointF(-32 + 64 * ((prog * 4) % 1), -13 + row * 9)
    p.translate(tip)
    p.rotate(62 + 5 * math.sin(t * 14))                    # leans out to the right, clear of the face
    p.setPen(_pen(INK, 3))
    p.setBrush(acc)
    p.drawRoundedRect(QRectF(-3, -30, 7, 24), 2, 2)          # pen body
    p.setBrush(QColor("#f5c24b"))
    nib = QPolygonF([QPointF(-3, -6), QPointF(4, -6), QPointF(0.5, 2)])
    p.drawPolygon(nib)
    p.restore()


def _magnifier(p, t, acc):                      # bottom-right, sweeping
    sway = 8 * math.sin(t * 2.4)
    c = QPointF(170 + sway, 208)
    p.setPen(_pen(INK, 7))
    p.drawLine(c + QPointF(15, 15), c + QPointF(32, 32))
    p.setPen(_pen(WOOD, 5))
    p.drawLine(c + QPointF(17, 17), c + QPointF(30, 30))
    p.setPen(_pen(INK, 5))
    g = QLinearGradient(c + QPointF(-18, -18), c + QPointF(18, 18))
    g.setColorAt(0, _c("#ffffff", 200))
    g.setColorAt(1, _c(acc, 110))
    p.setBrush(QBrush(g))
    p.drawEllipse(c, 20, 20)
    p.setPen(_pen("#ffffff", 3))
    p.drawArc(QRectF(c.x() - 13, c.y() - 13, 26, 26), 100 * 16, 60 * 16)


def _globe(p, t, acc):                          # bottom-right, meridians rotating
    c, r = QPointF(176, 208), 24
    p.setPen(_pen(INK, 4))
    g = QLinearGradient(c + QPointF(-r, -r), c + QPointF(r, r))
    g.setColorAt(0, acc.lighter(160))
    g.setColorAt(1, acc.darker(110))
    p.setBrush(QBrush(g))
    p.drawEllipse(c, r, r)
    p.setPen(_pen(_c("#ffffff", 200), 2))
    p.setBrush(Qt.BrushStyle.NoBrush)
    for k in range(3):
        w = r * abs(math.cos(t * 1.5 + k * math.pi / 3))
        p.drawEllipse(c, w, r)
    p.drawLine(c + QPointF(-r, 0), c + QPointF(r, 0))
    p.drawEllipse(c, r * 0.95, r * 0.45)


def _terminal(p, t, acc):                       # a little terminal window + spinning gear
    r = QRectF(128, 186, 84, 50)
    p.setPen(_pen(INK, 4))
    p.setBrush(QColor("#0b1024"))
    p.drawRoundedRect(r, 6, 6)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#1d2a55"))
    p.drawRoundedRect(QRectF(r.left() + 2, r.top() + 2, r.width() - 4, 9), 4, 4)
    for i, col in enumerate(("#ff6b8b", "#f5c24b", "#5ef0a6")):
        p.setBrush(QColor(col))
        p.drawEllipse(QPointF(r.left() + 9 + i * 8, r.top() + 6.5), 2.5, 2.5)
    f = QFont("Cascadia Mono")
    f.setPixelSize(12)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#5ef0a6"))
    p.drawText(QPointF(r.left() + 7, r.top() + 27), "$ run")
    if int(t * 3) % 2 == 0:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#5ef0a6"))
        p.drawRect(QRectF(r.left() + 46, r.top() + 18, 6, 11))
    # gear, bottom-left
    p.save()
    p.translate(64, 214)
    p.rotate(t * 90)
    p.setPen(_pen(INK, 3))
    p.setBrush(acc.lighter(130))
    teeth = QPainterPath()
    for i in range(16):
        a = i * math.pi / 8
        rr = 20 if i % 2 == 0 else 15
        pt = QPointF(rr * math.cos(a), rr * math.sin(a))
        teeth.moveTo(pt) if i == 0 else teeth.lineTo(pt)
    teeth.closeSubpath()
    p.drawPath(teeth)
    p.setBrush(QColor("#0b1024"))
    p.drawEllipse(QPointF(0, 0), 6, 6)
    p.restore()


def _bulb(p, t, acc):                           # top-right idea bulb, glowing
    c = QPointF(206, 38)
    glow = 0.5 + 0.5 * math.sin(t * 4)
    halo = QLinearGradient(c, c)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(_c("#ffe27a", int(60 + 80 * glow)))
    p.drawEllipse(c, 30, 30)
    p.setPen(_pen(INK, 4))
    p.setBrush(QColor("#ffe27a"))
    p.drawEllipse(c, 17, 17)
    p.setBrush(QColor("#9aa6c8"))
    p.drawRoundedRect(QRectF(c.x() - 8, c.y() + 14, 16, 10), 3, 3)
    p.setPen(_pen("#f5a524", 2.5))
    p.drawLine(c + QPointF(-5, 6), c + QPointF(0, -3))
    p.drawLine(c + QPointF(0, -3), c + QPointF(5, 6))
    for k in range(5):                              # rays
        a = -math.pi / 2 + (k - 2) * 0.55
        p.setPen(_pen(_c("#ffe27a", int(150 + 100 * glow)), 3))
        p.drawLine(c + QPointF(math.cos(a) * 24, math.sin(a) * 24), c + QPointF(math.cos(a) * 32, math.sin(a) * 32))
    del halo


def _rocket(p, t, acc):                         # bottom-right, flame flickering
    bob = 3 * math.sin(t * 5)
    p.save()
    p.translate(196, 214 + bob)
    p.rotate(-35)
    flame = QPainterPath(QPointF(-6, 22))
    flame.quadTo(QPointF(0, 40 + 6 * math.sin(t * 25)), QPointF(6, 22))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#ff9f43"))
    p.drawPath(flame)
    body = QPainterPath(QPointF(0, -28))
    body.quadTo(QPointF(14, -10), QPointF(10, 22))
    body.lineTo(QPointF(-10, 22))
    body.quadTo(QPointF(-14, -10), QPointF(0, -28))
    p.setPen(_pen(INK, 4))
    p.setBrush(PAPER)
    p.drawPath(body)
    p.setBrush(acc)
    p.drawEllipse(QPointF(0, -4), 6, 6)
    for s in (-1, 1):
        fin = QPolygonF([QPointF(s * 9, 8), QPointF(s * 18, 24), QPointF(s * 8, 22)])
        p.drawPolygon(fin)
    p.restore()


def _clipboard(p, t, acc):                      # reviewing: clipboard with ticks appearing
    r = QRectF(140, 190, 60, 46)
    p.setPen(_pen(INK, 4))
    p.setBrush(WOOD)
    p.drawRoundedRect(r, 5, 5)
    p.setBrush(PAPER)
    p.drawRect(r.adjusted(6, 8, -6, -5))
    p.setBrush(QColor("#9aa6c8"))
    p.drawRoundedRect(QRectF(r.center().x() - 10, r.top() - 4, 20, 9), 3, 3)
    shown = int(t * 1.5) % 4
    for i in range(3):
        y = r.top() + 16 + i * 9
        if i < shown:
            p.setPen(_pen("#27b36a", 3))
            p.drawLine(QPointF(r.left() + 11, y), QPointF(r.left() + 14, y + 3))
            p.drawLine(QPointF(r.left() + 14, y + 3), QPointF(r.left() + 20, y - 3))
        p.setPen(_pen("#9aa6c8", 2))
        p.drawLine(QPointF(r.left() + 25, y), QPointF(r.right() - 11, y))


def _speech(p, t, acc):                         # top-right speech bubble with dots
    r = QRectF(176, 14, 58, 36)
    p.setPen(_pen(INK, 4))
    p.setBrush(PAPER)
    path = QPainterPath()
    path.addRoundedRect(r, 12, 12)
    tail = QPainterPath(QPointF(186, 46))
    tail.lineTo(QPointF(180, 60))
    tail.lineTo(QPointF(198, 48))
    p.drawPath(path.united(tail))
    for i in range(3):
        up = 3 * math.sin(t * 6 - i * 0.9)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(acc)
        p.drawEllipse(QPointF(192 + i * 13, 32 - max(0.0, up)), 4, 4)


def _hourglass(p, t, acc):                      # top-right, sand flowing, flips
    p.save()
    p.translate(208, 36)
    flip = (t % 4) > 3.6
    p.rotate(180 * min(1.0, ((t % 4) - 3.6) / 0.4) if flip else 0)
    p.setPen(_pen(INK, 4))
    p.setBrush(WOOD)
    p.drawRoundedRect(QRectF(-18, -26, 36, 6), 2, 2)
    p.drawRoundedRect(QRectF(-18, 20, 36, 6), 2, 2)
    glass = QPainterPath(QPointF(-13, -20))
    glass.lineTo(QPointF(13, -20))
    glass.lineTo(QPointF(2, 0))
    glass.lineTo(QPointF(13, 20))
    glass.lineTo(QPointF(-13, 20))
    glass.lineTo(QPointF(-2, 0))
    glass.closeSubpath()
    p.setBrush(_c("#ffffff", 170))
    p.drawPath(glass)
    lvl = (t % 3.6) / 3.6
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#f5c24b"))
    top_h = 16 * (1 - lvl)
    p.drawPolygon(QPolygonF([QPointF(-11 * top_h / 16, -2 - top_h), QPointF(11 * top_h / 16, -2 - top_h), QPointF(0, -1)]))
    p.drawPolygon(QPolygonF([QPointF(-12, 19), QPointF(12, 19), QPointF(0, 19 - 16 * lvl)]))
    p.drawRect(QRectF(-0.8, -1, 1.6, 20))
    p.restore()


def _cart(p, t, acc):                           # bottom-right shopping cart, wheels turning
    x = 146 + 4 * math.sin(t * 2)
    p.setPen(_pen(INK, 4))
    p.setBrush(PAPER)
    basket = QPolygonF([QPointF(x, 194), QPointF(x + 58, 194), QPointF(x + 50, 220), QPointF(x + 8, 220)])
    p.drawPolygon(basket)
    p.drawLine(QPointF(x - 10, 186), QPointF(x, 194))
    p.setPen(_pen("#9aa6c8", 2))
    for i in range(1, 4):
        p.drawLine(QPointF(x + i * 14, 196), QPointF(x + 4 + i * 11, 218))
    p.setPen(_pen(INK, 3))
    p.setBrush(acc)
    for wx in (x + 14, x + 44):
        p.drawEllipse(QPointF(wx, 228), 6, 6)
    p.setBrush(QColor("#f5c24b"))                   # a coin hopping in
    ph = (t * 0.8) % 1
    p.drawEllipse(QPointF(x + 56 - 18 * ph, 176 + 22 * ph ** 2), 7, 7)


def _mic(p, t, acc):                            # speaking: microphone + sound waves
    c = QPointF(198, 206)
    p.setPen(_pen(INK, 4))
    p.setBrush(QColor("#c9d6f5"))
    p.drawRoundedRect(QRectF(c.x() - 11, c.y() - 26, 22, 34), 11, 11)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(c.x() - 17, c.y() - 14, 34, 30), 180 * 16, 180 * 16)
    p.drawLine(c + QPointF(0, 16), c + QPointF(0, 28))
    p.drawLine(c + QPointF(-10, 28), c + QPointF(10, 28))
    for k in range(3):
        ph = (t * 1.2 + k / 3) % 1
        p.setPen(_pen(_c(acc, int(255 * (1 - ph))), 3))
        rr = 22 + 18 * ph
        p.drawArc(QRectF(c.x() - rr, c.y() - 10 - rr, 2 * rr, 2 * rr), -40 * 16, 80 * 16)


def _camera(p, t, acc):                         # looking: camera, flash now and then
    r = QRectF(136, 192, 68, 42)
    p.setPen(_pen(INK, 4))
    p.setBrush(QColor("#2a3766"))
    p.drawRoundedRect(r, 8, 8)
    p.drawRoundedRect(QRectF(r.left() + 8, r.top() - 7, 18, 9), 3, 3)
    p.setBrush(QColor("#0b1024"))
    p.drawEllipse(r.center(), 14, 14)
    p.setBrush(acc.lighter(140))
    p.drawEllipse(r.center(), 7, 7)
    if (t % 2.5) < 0.12:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_c("#ffffff", 200))
        p.drawEllipse(QPointF(r.right() - 10, r.top() + 8), 16, 16)


def _clapper(p, t, acc):                        # making video: clapperboard snapping
    p.save()
    p.translate(180, 220)
    p.setPen(_pen(INK, 4))
    p.setBrush(QColor("#1d2440"))
    p.drawRect(QRectF(-26, -10, 52, 24))
    ang = -18 * max(0.0, math.sin(t * 3))
    p.save()
    p.translate(-26, -10)
    p.rotate(ang)
    p.setBrush(PAPER)
    p.drawRect(QRectF(0, -8, 52, 8))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#1d2440"))
    for i in range(4):
        p.drawPolygon(QPolygonF([QPointF(3 + i * 13, -8), QPointF(10 + i * 13, -8), QPointF(7 + i * 13, 0), QPointF(0 + i * 13, 0)]))
    p.restore()
    p.restore()


_PROPS = {
    "coding": _keyboard, "reading": _book, "writing": _writing, "searching": _magnifier, "browsing": _globe,
    "running": _terminal, "thinking": _bulb, "deploying": _rocket, "reviewing": _clipboard, "chatting": _speech,
    "waiting": _hourglass, "shopping": _cart, "speaking": _mic, "looking": _camera, "filming": _clapper,
}
TOP_RIGHT_PROPS = {"thinking", "chatting", "waiting"}      # these use the corner the mood extras use


# ══ job badges ═════════════════════════════════════════════════════════════
# job key -> (title shown in the legend, role keywords that select it)
JOBS: dict[str, tuple[str, tuple[str, ...]]] = {
    "boss":       ("Boss / orchestrator (Omi)", ("boss", "orchestrator", "lead", "manager")),
    "coder":      ("Coder / developer", ("coder", "developer", "programmer", "engineer", "dev")),
    "researcher": ("Researcher", ("research", "scout", "investigat")),
    "writer":     ("Writer / editor", ("writer", "editor", "copy", "author", "blog", "docs")),
    "reviewer":   ("Reviewer / critic", ("review", "critic", "qa", "auditor")),
    "tester":     ("Tester", ("test", "bug")),
    "devops":     ("DevOps / deployer", ("devops", "deploy", "ops", "sysadmin", "hosting", "infra")),
    "designer":   ("Designer", ("design", "artist", "ui", "ux", "graphic")),
    "analyst":    ("Data analyst", ("analyst", "analysis", "data", "stats")),
    "shopper":    ("Shopper / seller", ("shop", "buyer", "seller", "commerce", "sales", "purchas")),
    "support":    ("Support / assistant", ("support", "assistant", "helper", "concierge")),
    "security":   ("Security", ("security", "secur", "pentest", "guard")),
    "translator": ("Translator", ("translat", "locali", "language")),
    "media":      ("Media maker (voice / video)", ("media", "video", "voice", "audio", "podcast")),
    "night":      ("Night shift", ("night",)),
    "toolsmith":  ("Toolsmith (forges tools)", ("toolsmith", "forge", "tool")),
    "worker":     ("General worker", ()),
}


def job_for_role(role: str) -> str:
    r = (role or "").lower()
    for key, (_title, words) in JOBS.items():
        if any(w in r for w in words):
            return key
    return "worker"


BADGE_CENTER, BADGE_R = QPointF(30, 30), 25.0


def draw_badge(p: QPainter, job: str, accent: str = "#2f7dff") -> None:
    """The job badge in the top-left corner (outside the head, never over the face)."""
    acc = QColor(accent)
    c, r = BADGE_CENTER, BADGE_R
    p.save()
    p.setPen(_pen(INK, 4))
    g = QLinearGradient(c + QPointF(0, -r), c + QPointF(0, r))
    g.setColorAt(0, QColor("#1f2c5c"))
    g.setColorAt(1, QColor("#0d1430"))
    p.setBrush(QBrush(g))
    p.drawEllipse(c, r, r)
    p.setPen(_pen(acc, 3))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(c, r - 4, r - 4)
    p.translate(c)
    _BADGES.get(job, _b_worker)(p, acc)
    p.restore()


def badge_image(job: str, accent: str, size: int = 48):
    """Just the badge, centered in a size x size image (for the legend and small lists)."""
    from PySide6.QtGui import QImage
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 60.0, size / 60.0)
    draw_badge(p, job, accent)
    p.end()
    return img


def _b_boss(p, acc):
    crown = QPolygonF([QPointF(-13, 8), QPointF(-14, -8), QPointF(-6, 0), QPointF(0, -11), QPointF(6, 0),
                       QPointF(14, -8), QPointF(13, 8)])
    p.setPen(_pen(INK, 2))
    p.setBrush(QColor("#f5c24b"))
    p.drawPolygon(crown)
    p.setBrush(QColor("#ff6b8b"))
    p.drawEllipse(QPointF(0, 3), 2.5, 2.5)


def _b_coder(p, acc):
    p.setPen(_pen("#e7edfb", 3))
    p.drawPolyline(QPolygonF([QPointF(-7, -7), QPointF(-13, 0), QPointF(-7, 7)]))
    p.drawPolyline(QPolygonF([QPointF(7, -7), QPointF(13, 0), QPointF(7, 7)]))
    p.setPen(_pen(acc.lighter(150), 3))
    p.drawLine(QPointF(3, -9), QPointF(-3, 9))


def _b_researcher(p, acc):
    p.setPen(_pen("#e7edfb", 3))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QPointF(-3, -3), 8, 8)
    p.drawLine(QPointF(3, 3), QPointF(11, 11))


def _b_writer(p, acc):
    quill = QPainterPath(QPointF(10, -12))
    quill.quadTo(QPointF(-6, -6), QPointF(-9, 10))
    quill.quadTo(QPointF(4, 2), QPointF(10, -12))
    p.setPen(_pen(INK, 1.5))
    p.setBrush(QColor("#e7edfb"))
    p.drawPath(quill)
    p.setPen(_pen(acc.lighter(140), 2))
    p.drawLine(QPointF(-9, 10), QPointF(-12, 13))


def _b_reviewer(p, acc):
    shield = QPainterPath(QPointF(0, -12))
    shield.lineTo(QPointF(11, -7))
    shield.quadTo(QPointF(10, 8), QPointF(0, 13))
    shield.quadTo(QPointF(-10, 8), QPointF(-11, -7))
    shield.closeSubpath()
    p.setPen(_pen("#e7edfb", 2))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(shield)
    p.setPen(_pen("#5ef0a6", 3))
    p.drawPolyline(QPolygonF([QPointF(-5, 0), QPointF(-1, 4), QPointF(6, -4)]))


def _b_tester(p, acc):
    p.setPen(_pen("#e7edfb", 2))
    for s in (-1, 1):
        for dy in (-4, 1, 6):
            p.drawLine(QPointF(s * 5, dy), QPointF(s * 11, dy - 2))
    p.setBrush(QColor("#ff6b8b"))
    p.setPen(_pen(INK, 1.5))
    p.drawEllipse(QPointF(0, 2), 6, 8)
    p.setBrush(QColor("#e7edfb"))
    p.drawEllipse(QPointF(0, -8), 4, 3.5)


def _b_devops(p, acc):
    p.save()
    p.rotate(45)
    body = QPainterPath(QPointF(0, -13))
    body.quadTo(QPointF(7, -4), QPointF(5, 8))
    body.lineTo(QPointF(-5, 8))
    body.quadTo(QPointF(-7, -4), QPointF(0, -13))
    p.setPen(_pen(INK, 1.5))
    p.setBrush(QColor("#e7edfb"))
    p.drawPath(body)
    p.setBrush(QColor("#ff9f43"))
    p.drawPolygon(QPolygonF([QPointF(-3, 8), QPointF(3, 8), QPointF(0, 14)]))
    p.setBrush(acc)
    p.drawEllipse(QPointF(0, -3), 2.5, 2.5)
    p.restore()


def _b_designer(p, acc):
    pal = QPainterPath()
    pal.addEllipse(QPointF(0, 0), 12, 10)
    hole = QPainterPath()
    hole.addEllipse(QPointF(5, 4), 3, 3)
    p.setPen(_pen(INK, 1.5))
    p.setBrush(QColor("#e7edfb"))
    p.drawPath(pal.subtracted(hole))
    for (x, y), col in zip(((-6, -3), (-1, -6), (5, -4), (-6, 3)), ("#ff6b8b", "#f5c24b", "#5ef0a6", "#7fb4ff")):
        p.setBrush(QColor(col))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(x, y), 2.5, 2.5)


def _b_analyst(p, acc):
    p.setPen(Qt.PenStyle.NoPen)
    for i, h in enumerate((8, 14, 11, 19)):
        p.setBrush(QColor("#e7edfb") if i != 3 else acc.lighter(150))
        p.drawRoundedRect(QRectF(-12 + i * 6.5, 10 - h, 5, h), 1, 1)


def _b_shopper(p, acc):
    p.setPen(_pen("#e7edfb", 2))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPolyline(QPolygonF([QPointF(-13, -9), QPointF(-9, -9), QPointF(-5, 5), QPointF(9, 5), QPointF(12, -5), QPointF(-7, -5)]))
    p.setBrush(QColor("#e7edfb"))
    p.drawEllipse(QPointF(-3, 10), 2, 2)
    p.drawEllipse(QPointF(7, 10), 2, 2)


def _b_support(p, acc):
    p.setPen(_pen("#e7edfb", 2.5))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(-10, -11, 20, 20), 0, 180 * 16)
    p.setBrush(acc.lighter(140))
    p.drawRoundedRect(QRectF(-13, -2, 6, 10), 2, 2)
    p.drawRoundedRect(QRectF(7, -2, 6, 10), 2, 2)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(-4, 0, 14, 14), 270 * 16, 90 * 16)


def _b_security(p, acc):
    p.setPen(_pen("#e7edfb", 2.5))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(-7, -13, 14, 16), 0, 180 * 16)
    p.setBrush(QColor("#e7edfb"))
    p.setPen(_pen(INK, 1.5))
    p.drawRoundedRect(QRectF(-10, -4, 20, 15), 3, 3)
    p.setBrush(acc)
    p.drawEllipse(QPointF(0, 3), 2.5, 2.5)


def _b_translator(p, acc):
    f = QFont("Segoe UI")
    f.setBold(True)
    f.setPixelSize(13)
    p.setFont(f)
    p.setPen(QColor("#e7edfb"))
    p.drawText(QRectF(-14, -13, 16, 16), Qt.AlignmentFlag.AlignCenter, "A")
    p.setPen(acc.lighter(150))
    p.drawText(QRectF(-2, -3, 16, 16), Qt.AlignmentFlag.AlignCenter, "文")


def _b_media(p, acc):
    p.setPen(_pen(INK, 1.5))
    p.setBrush(QColor("#e7edfb"))
    p.drawRoundedRect(QRectF(-13, -7, 18, 14), 3, 3)
    p.drawPolygon(QPolygonF([QPointF(5, -2), QPointF(13, -7), QPointF(13, 7), QPointF(5, 2)]))
    p.setBrush(QColor("#ff6b8b"))
    p.drawEllipse(QPointF(-8, -2), 2, 2)


def _b_night(p, acc):
    moon = QPainterPath()
    moon.addEllipse(QPointF(0, 0), 11, 11)
    bite = QPainterPath()
    bite.addEllipse(QPointF(6, -4), 9, 9)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#ffe27a"))
    p.drawPath(moon.subtracted(bite))
    p.setBrush(QColor("#e7edfb"))
    p.drawEllipse(QPointF(9, 7), 1.6, 1.6)


def _b_toolsmith(p, acc):
    p.save()
    p.rotate(-40)
    p.setPen(_pen(INK, 1.5))
    p.setBrush(QColor("#c98a4b"))
    p.drawRoundedRect(QRectF(-2, -4, 4, 18), 1.5, 1.5)
    p.setBrush(QColor("#e7edfb"))
    p.drawRoundedRect(QRectF(-9, -12, 18, 8), 2, 2)
    p.restore()


def _b_worker(p, acc):
    teeth = QPainterPath()
    for i in range(16):
        a = i * math.pi / 8
        rr = 12 if i % 2 == 0 else 9
        pt = QPointF(rr * math.cos(a), rr * math.sin(a))
        teeth.moveTo(pt) if i == 0 else teeth.lineTo(pt)
    teeth.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#e7edfb"))
    hole = QPainterPath()
    hole.addEllipse(QPointF(0, 0), 4, 4)
    p.drawPath(teeth.subtracted(hole))


_BADGES = {
    "boss": _b_boss, "coder": _b_coder, "researcher": _b_researcher, "writer": _b_writer, "reviewer": _b_reviewer,
    "tester": _b_tester, "devops": _b_devops, "designer": _b_designer, "analyst": _b_analyst, "shopper": _b_shopper,
    "support": _b_support, "security": _b_security, "translator": _b_translator, "media": _b_media,
    "night": _b_night, "toolsmith": _b_toolsmith, "worker": _b_worker,
}
