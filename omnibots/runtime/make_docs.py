"""Slides and documents (PLAN.md A17.b.05): `make_document` writes real Office files and PDFs from Markdown.

The file's extension picks the format; the content is always Markdown, the one format every model writes well:
  .docx  headings (#), paragraphs, bullet (-) and numbered (1.) lists, tables (| a | b |), **bold**, *italic*
  .pptx  one slide per "# Title" (the first can be a title slide: "# Title" + one line of subtitle); "- " bullets
         (indent 2 spaces for a sub-bullet); "Notes: ..." lines become the speaker notes
  .xlsx  each "## Sheet name" starts a sheet; its Markdown table (or CSV lines) become the rows; numbers stay numbers
  .pdf   the Markdown laid out as a clean page with the bots' browser (Chromium prints it)
R1: it only writes inside the bot's workspace.
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import re
from pathlib import Path
from typing import Any

from omnibots.runtime.tools import Tool, ToolContext

FORMATS = (".docx", ".pptx", ".xlsx", ".pdf")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_rule(line: str) -> bool:
    return bool(re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", line.strip()))


def blocks(md: str) -> list[tuple[str, Any]]:
    """Markdown → [(kind, data)]: h1-h6, p, ul, ol (lists of (level, text)), table (rows), notes."""
    out: list[tuple[str, Any]] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
            continue
        m = re.match(r"(#{1,6})\s+(.*)", s)
        if m:
            out.append((f"h{len(m.group(1))}", m.group(2).strip()))
            i += 1
        elif s.lower().startswith("notes:"):
            out.append(("notes", s[6:].strip()))
            i += 1
        elif s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not _is_rule(lines[i]):
                    rows.append(_cells(lines[i]))
                i += 1
            out.append(("table", rows))
        elif re.match(r"\s*([-*+]|\d+[.)])\s+", line):
            kind = "ol" if re.match(r"\s*\d+[.)]\s+", line) else "ul"
            items = []
            while i < len(lines) and re.match(r"\s*([-*+]|\d+[.)])\s+", lines[i]):
                indent = len(lines[i]) - len(lines[i].lstrip())
                items.append((min(indent // 2, 4), re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", lines[i]).strip()))
                i += 1
            out.append((kind, items))
        else:
            para = [s]
            i += 1
            while i < len(lines) and lines[i].strip() and not re.match(r"\s*(#|\||[-*+]\s|\d+[.)]\s)", lines[i]) \
                    and not lines[i].strip().lower().startswith("notes:"):
                para.append(lines[i].strip())
                i += 1
            out.append(("p", " ".join(para)))
    return out


_INLINE = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)")


def _runs(text: str) -> list[tuple[str, str]]:
    """'a **b** *c*' → [('', 'a '), ('b', 'b'), ('i', 'c')]"""
    out = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            out.append(("b", part[2:-2]))
        elif part.startswith("`") and part.endswith("`"):
            out.append(("c", part[1:-1]))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            out.append(("i", part[1:-1]))
        else:
            out.append(("", part))
    return out


def _plain(text: str) -> str:
    return "".join(t for _, t in _runs(text))


def write_docx(md: str, path: Path) -> str:
    import docx
    d = docx.Document()

    def para(text, style=None):
        p = d.add_paragraph(style=style)
        for kind, t in _runs(text):
            r = p.add_run(t)
            r.bold, r.italic = kind == "b", kind == "i"
            if kind == "c":
                r.font.name = "Consolas"
        return p
    n = 0
    for kind, data in blocks(md):
        if kind.startswith("h"):
            d.add_heading(_plain(data), level=min(int(kind[1]), 4))
        elif kind == "p":
            para(data)
        elif kind in ("ul", "ol"):
            for level, t in data:
                base = "List Bullet" if kind == "ul" else "List Number"
                para(t, base if level == 0 else f"{base} {min(level + 1, 3)}")
        elif kind == "table" and data:
            width = max(len(r) for r in data)
            t = d.add_table(rows=len(data), cols=width)
            t.style = "Table Grid"
            for r, row in enumerate(data):
                for c in range(width):
                    cell = t.cell(r, c)
                    cell.text = _plain(row[c]) if c < len(row) else ""
                    if r == 0:
                        for run in cell.paragraphs[0].runs:
                            run.bold = True
        elif kind == "notes":
            para(data)
        n += 1
    d.save(str(path))
    return f"{n} blocks"


def write_pptx(md: str, path: Path) -> str:
    from pptx import Presentation
    from pptx.util import Pt
    prs = Presentation()
    prs.slide_width, prs.slide_height = 12192000, 6858000               # 16:9
    slides: list[dict[str, Any]] = []
    for kind, data in blocks(md):
        if kind in ("h1", "h2") or not slides:
            slides.append({"title": _plain(data) if kind in ("h1", "h2") else "", "items": [], "notes": [], "text": [], "table": None})
            if kind in ("h1", "h2"):
                continue
        cur = slides[-1]
        if kind in ("ul", "ol"):
            cur["items"] += data
        elif kind == "p" or kind.startswith("h"):
            cur["text"].append(_plain(data))
        elif kind == "notes":
            cur["notes"].append(data)
        elif kind == "table":
            cur["table"] = data
    for i, sd in enumerate(slides):
        title_only = i == 0 and not sd["items"] and not sd["table"] and len(sd["text"]) <= 1
        slide = prs.slides.add_slide(prs.slide_layouts[0 if title_only else 5 if sd["table"] else 1])
        slide.shapes.title.text = sd["title"]
        if title_only:
            if sd["text"] and len(slide.placeholders) > 1:
                slide.placeholders[1].text = sd["text"][0]
        elif sd["table"]:
            rows, width = sd["table"], max(len(r) for r in sd["table"])
            shape = slide.shapes.add_table(len(rows), width, 457200, 1600200, prs.slide_width - 914400, 400000 * len(rows))
            for r, row in enumerate(rows):
                for c in range(width):
                    shape.table.cell(r, c).text = _plain(row[c]) if c < len(row) else ""
        else:
            body = slide.placeholders[1].text_frame
            body.clear()
            entries = [(0, t) for t in sd["text"]] + list(sd["items"])
            for j, (level, t) in enumerate(entries):
                p = body.paragraphs[0] if j == 0 else body.add_paragraph()
                p.text, p.level = _plain(t), level
                if len(entries) > 7:
                    p.font.size = Pt(18)
        if sd["notes"]:
            slide.notes_slide.notes_text_frame.text = "\n".join(sd["notes"])
    prs.save(str(path))
    return f"{len(slides)} slides"


def _number(v: str):
    t = v.strip().replace(",", "") if re.fullmatch(r"-?[\d,]+(\.\d+)?", v.strip()) else v.strip()
    try:
        return int(t) if re.fullmatch(r"-?\d+", t) else float(t) if re.fullmatch(r"-?\d+\.\d+", t) else v.strip()
    except ValueError:
        return v.strip()


def write_xlsx(md: str, path: Path) -> str:
    import openpyxl
    from openpyxl.styles import Font
    sheets: list[tuple[str, list[list[str]]]] = []
    cur: list[list[str]] | None = None
    for line in md.replace("\r\n", "\n").split("\n"):
        s = line.strip()
        m = re.match(r"#{1,6}\s+(.*)", s)
        if m:
            cur = []
            sheets.append((m.group(1).strip()[:31] or f"Sheet{len(sheets) + 1}", cur))
            continue
        if not s or _is_rule(s):
            continue
        if cur is None:
            cur = []
            sheets.append(("Sheet1", cur))
        cur.append(_cells(s) if s.startswith("|") else next(csv.reader(io.StringIO(s))))
    if not sheets:
        raise ValueError("no rows: give a Markdown table or CSV lines")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets:
        ws = wb.create_sheet(re.sub(r"[\[\]:*?/\\]", "_", name))
        for r in rows:
            ws.append([_number(_plain(c)) for c in r])
        for cell in ws[1] if rows else []:
            cell.font = Font(bold=True)
        for col in ws.columns:
            ws.column_dimensions[col[0].column_letter].width = min(60, max(10, *(len(str(c.value or "")) + 2 for c in col)))
    wb.save(str(path))
    return f"{len(sheets)} sheet(s), {sum(len(r) for _, r in sheets)} rows"


def md_html(md: str, title: str = "") -> str:
    def inline(t: str) -> str:
        return "".join({"b": f"<b>{html.escape(x)}</b>", "i": f"<i>{html.escape(x)}</i>", "c": f"<code>{html.escape(x)}</code>"}
                       .get(k, html.escape(x)) for k, x in _runs(t))
    body = []
    for kind, data in blocks(md):
        if kind.startswith("h"):
            body.append(f"<{kind}>{inline(data)}</{kind}>")
        elif kind in ("p", "notes"):
            body.append(f"<p>{inline(data)}</p>")
        elif kind in ("ul", "ol"):
            body.append(f"<{kind}>" + "".join(f'<li style="margin-left:{lvl * 1.4}em">{inline(t)}</li>' for lvl, t in data) + f"</{kind}>")
        elif kind == "table" and data:
            head, *rest = data
            body.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                        + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rest) + "</tbody></table>")
    css = ("body{font:11pt/1.5 'Segoe UI',Arial,sans-serif;color:#1b1f24;margin:0}h1{font-size:22pt;margin:0 0 8pt}"
           "h2{font-size:16pt;margin:16pt 0 6pt}h3{font-size:13pt}table{border-collapse:collapse;margin:8pt 0;width:100%}"
           "th,td{border:1px solid #c9d1db;padding:4pt 7pt;text-align:left}th{background:#eef2f7}code{font-family:Consolas,monospace}")
    return f"<!doctype html><meta charset='utf-8'><title>{html.escape(title)}</title><style>{css}</style>" + "\n".join(body)


async def write_pdf(md: str, path: Path) -> str:
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(md_html(md, path.stem), wait_until="load")
            await page.pdf(path=str(path), format="A4", margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"},
                           print_background=True)
        finally:
            await browser.close()
    from pypdf import PdfReader
    return f"{len(PdfReader(str(path)).pages)} page(s)"


async def make_document(args: dict[str, Any], ctx: ToolContext) -> str:
    rel = str(args.get("path") or "")
    p = (ctx.workspace / rel).resolve()
    if not p.is_relative_to(ctx.workspace.resolve()):
        return "ERROR: path is outside your workspace"
    if p.suffix.lower() not in FORMATS:
        return f"ERROR: the path must end in one of {', '.join(FORMATS)}"
    md = str(args.get("content") or "")
    if not md.strip():
        return "ERROR: content (Markdown) is required"
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    try:
        if p.suffix.lower() == ".pdf":
            info = await write_pdf(md, p)
        else:
            fn = {".docx": write_docx, ".pptx": write_pptx, ".xlsx": write_xlsx}[p.suffix.lower()]
            info = await asyncio.to_thread(fn, md, p)
    except Exception as exc:                                  # a library error is the model's to fix, not a crash
        return f"ERROR: couldn't make {rel}: {type(exc).__name__}: {exc}"
    await ctx.event("console", f"  📄 {rel} ({info})")
    return f"{'overwrote' if existed else 'created'} {rel} ({info}, {p.stat().st_size:,} bytes). read_file shows its text back."


def doc_tools() -> list[Tool]:
    s = {"type": "string"}
    return [Tool("make_document", "Make a Word (.docx), PowerPoint (.pptx), Excel (.xlsx) or PDF (.pdf) file from Markdown; the "
                 "path's extension picks the format. Slides: one '# Title' per slide, '- ' bullets, 'Notes: ...' for speaker "
                 "notes. Excel: '## Sheet' then a Markdown table or CSV lines. Word/PDF: headings, lists, tables, **bold**.",
                 {"type": "object", "properties": {"path": s, "content": s}, "required": ["path", "content"]},
                 "R1", make_document, timeout=180, summary=lambda a: f"make_document {a.get('path')}")]
