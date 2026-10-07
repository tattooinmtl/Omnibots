"""A17.a.01: read_file turns PDF, Word, Excel and PowerPoint into text.

The Office files are made by the real libraries (python-docx, openpyxl, python-pptx), the PDF by hand with a real
text layer; read_file must return their text inside the untrusted-file wrapper, like any file."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnibots.runtime.core_tools import read_file
from omnibots.runtime.documents import document_text
from omnibots.runtime.tools import ToolContext


def _pdf(path: Path, pages: list[str]) -> None:
    """A minimal valid PDF, one line of Helvetica text per page."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream")
        content = len(objs)
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {content} 0 R >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


def _read(ws: Path, name: str) -> str:
    return asyncio.run(read_file({"path": name}, ToolContext(bot_id="b", workspace=ws)))


def test_pdf_pages_come_back_as_text(tmp_path):
    _pdf(tmp_path / "report.pdf", ["Quarterly report: revenue up 12 percent", "Second page talks about ESP32 boards"])
    out = _read(tmp_path, "report.pdf")
    assert "UNTRUSTED FILE CONTENT" in out
    assert "(PDF, 2 pages)" in out and "--- page 2 ---" in out
    assert "revenue up 12 percent" in out and "ESP32 boards" in out


def test_word_headings_lists_and_tables(tmp_path):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("Launch plan", level=1)
    d.add_paragraph("Ship the bakery site on Friday.")
    d.add_paragraph("Buy the domain", style="List Bullet")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text, t.cell(1, 0).text, t.cell(1, 1).text = "Page", "Owner", "Menu", "Frontend"
    d.save(tmp_path / "plan.docx")
    out = _read(tmp_path, "plan.docx")
    assert "# Launch plan" in out
    assert "Ship the bakery site on Friday." in out
    assert "- Buy the domain" in out
    assert "Menu | Frontend" in out


def test_excel_sheets_numbers_and_shared_strings(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Prices"
    ws.append(["Item", "Price", "In stock"])
    ws.append(["Croissant", 3.25, True])
    ws["E5"] = "far cell"
    wb.create_sheet("Notes")["A1"] = "Second sheet"
    wb.save(tmp_path / "prices.xlsx")
    out = _read(tmp_path, "prices.xlsx")
    assert "--- sheet: Prices ---" in out and "--- sheet: Notes ---" in out
    assert "1: Item | Price | In stock" in out
    assert "2: Croissant | 3.25 | TRUE" in out
    assert "5:  |  |  |  | far cell" in out
    assert "Second sheet" in out


def test_powerpoint_slides_and_notes(tmp_path):
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation()
    for title, body, note in [("OmniBots", "A team of bots", "say hi"), ("Roadmap", "Telegram next", "")]:
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text, s.placeholders[1].text = title, body
        if note:
            s.notes_slide.notes_text_frame.text = note
    prs.save(tmp_path / "deck.pptx")
    out = _read(tmp_path, "deck.pptx")
    assert "(PowerPoint, 2 slides)" in out
    assert "--- slide 1 ---" in out and "A team of bots" in out and "notes: say hi" in out
    assert "--- slide 2 ---" in out and "Telegram next" in out


def test_broken_and_scanned_documents_say_so(tmp_path):
    (tmp_path / "fake.docx").write_text("not a zip")
    assert "ERROR: can't read fake.docx: not a valid DOCX file" in _read(tmp_path, "fake.docx")
    (tmp_path / "fake.pdf").write_text("nope")
    assert "ERROR: can't read fake.pdf" in _read(tmp_path, "fake.pdf")
    _pdf(tmp_path / "scan.pdf", [""])
    assert "no text layer" in document_text(tmp_path / "scan.pdf")


def test_text_files_are_unchanged(tmp_path):
    (tmp_path / "a.txt").write_text("hello\nworld\n")
    out = _read(tmp_path, "a.txt")
    assert "    1  hello" in out and "    2  world" in out
