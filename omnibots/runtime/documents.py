"""Documents as text (PLAN.md A17.a.01): PDF, Word, Excel and PowerPoint.

`read_file` calls `document_text()` for these suffixes instead of decoding the bytes as UTF-8. DOCX, XLSX and PPTX
are zipped XML, read with the standard library; PDF uses pypdf (pure Python). A scanned PDF has no text layer:
the answer says so, so the bot can look at the pages as images instead.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

DOC_SUFFIXES = {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx"}
MAX_PAGES = 300
MAX_ROWS = 2000                      # per sheet

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"


def is_document(path: Path) -> bool:
    return path.suffix.lower() in DOC_SUFFIXES


def document_text(path: Path) -> str:
    """The text of a document, with page / sheet / slide headings. Raises ValueError when it can't be read."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            return _pdf(path)
        if suffix == ".docx":
            return _docx(path)
        if suffix in (".xlsx", ".xlsm"):
            return _xlsx(path)
        if suffix == ".pptx":
            return _pptx(path)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError(f"not a valid {suffix[1:].upper()} file ({exc})") from exc
    raise ValueError(f"not a document type: {suffix}")


def _pdf(path: Path) -> str:
    from pypdf import PdfReader
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise ValueError("the PDF is password-protected")
        pages = reader.pages
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"not a valid PDF ({exc})") from exc
    out, empty = [], 0
    for i, page in enumerate(pages):
        if i >= MAX_PAGES:
            out.append(f"…[{len(pages) - MAX_PAGES} more pages not read]")
            break
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        empty += not text
        out.append(f"--- page {i + 1} ---\n{text}")
    if pages and empty == len(pages):
        return (f"(PDF, {len(pages)} pages, no text layer: it is probably scanned. Convert pages to images "
                "and use describe_image to read them.)")
    return f"(PDF, {len(pages)} pages)\n" + "\n".join(out)


def _docx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{_W}body")
    lines: list[str] = []
    for block in (body if body is not None else []):
        if block.tag == f"{_W}p":
            lines.append(_para(block))
        elif block.tag == f"{_W}tbl":
            for row in block.iter(f"{_W}tr"):
                cells = [" ".join(_para(p) for p in cell.iter(f"{_W}p")).strip() for cell in row.iter(f"{_W}tc")]
                lines.append(" | ".join(cells))
    text = "\n".join(lines)
    return "(Word document)\n" + re.sub(r"\n{3,}", "\n\n", text).strip()


def _para(p) -> str:
    style = p.find(f"{_W}pPr/{_W}pStyle")
    parts = []
    for node in p.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    text = "".join(parts)
    sid = (style.get(f"{_W}val") or "") if style is not None else ""
    m = re.match(r"(?i)heading(\d)", sid)
    if m and text.strip():
        return "#" * int(m.group(1)) + " " + text
    if sid.lower().startswith("list") and text.strip():
        return "- " + text
    return text


def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _xlsx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{_S}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{_S}t")))
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = {r.get("Id"): r.get("Target") for r in rels.iter(f"{_PR}Relationship")}
        out = ["(Excel workbook)"]
        for sheet in wb.iter(f"{_S}sheet"):
            t = target.get(sheet.get(f"{_R}id"), "")
            part = t.lstrip("/") if t.startswith("/") else "xl/" + t
            if part not in names:
                continue
            out.append(f"--- sheet: {sheet.get('name')} ---")
            rows = ET.fromstring(z.read(part)).iter(f"{_S}row")
            for n, row in enumerate(rows):
                if n >= MAX_ROWS:
                    out.append(f"…[more rows not read]")
                    break
                cells: dict[int, str] = {}
                for c in row.iter(f"{_S}c"):
                    kind, v = c.get("t"), c.find(f"{_S}v")
                    if kind == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(f"{_S}t"))
                    elif v is None or v.text is None:
                        continue
                    elif kind == "s":
                        val = shared[int(v.text)] if int(v.text) < len(shared) else ""
                    elif kind == "b":
                        val = "TRUE" if v.text == "1" else "FALSE"
                    else:
                        val = v.text
                    cells[_col_index(c.get("r") or "A")] = val
                if cells:
                    width = max(cells) + 1
                    out.append(f"{row.get('r')}: " + " | ".join(cells.get(i, "") for i in range(width)))
    return "\n".join(out)


def _pptx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
        out = [f"(PowerPoint, {len(slides)} slides)"]
        for i, name in enumerate(slides, 1):
            root = ET.fromstring(z.read(name))
            paras = []
            for p in root.iter(f"{_A}p"):
                t = "".join(r.text or "" for r in p.iter(f"{_A}t")).strip()
                if t:
                    paras.append(t)
            notes = f"ppt/notesSlides/notesSlide{re.search(r'(\d+)', name.rsplit('/', 1)[1]).group(1)}.xml"
            out.append(f"--- slide {i} ---\n" + "\n".join(paras))
            if notes in names:
                nt = [("".join(r.text or "" for r in p.iter(f"{_A}t"))).strip() for p in ET.fromstring(z.read(notes)).iter(f"{_A}p")]
                nt = [x for x in nt if x and not x.isdigit()]
                if nt:
                    out.append("notes: " + " ".join(nt))
    return "\n".join(out)
