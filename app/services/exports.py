from __future__ import annotations

import re
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from app.services.resume_format import body_text, flatten_links, to_plain, unbold

MARGIN = 48
BODY, BOLD = "Helvetica", "Helvetica-Bold"
INK, MUTED, RULE = (0.07, 0.08, 0.15), (0.35, 0.38, 0.45), (0.8, 0.82, 0.87)


def _runs(text: str) -> list[tuple[str, bool]]:
    """Split Markdown emphasis into (text, bold) runs: "**Data:** Kafka" -> [("Data:", True), (" Kafka", False)]."""
    return [(part, index % 2 == 1) for index, part in enumerate((text or "").split("**")) if part] or [("", False)]


def _segments(row: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Words of one wrapped line grouped into the fewest possible (text, font) runs."""
    out: list[tuple[str, str]] = []
    for word, word_font in row:
        if out and out[-1][1] == word_font:
            out[-1] = (f"{out[-1][0]} {word}", word_font)
        else:
            out.append((word, word_font))
    return out


def markdown_to_simple_pdf(markdown: str) -> bytes:
    """Render the resume Markdown dialect (# / ## / ### Title | Company | Location | Dates / - bullets)."""
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    usable = width - 2 * MARGIN
    y = height - MARGIN
    after_name = False

    def room(needed: float) -> None:
        nonlocal y
        if y - needed < MARGIN:
            c.showPage()
            y = height - MARGIN

    def paragraph(text: str, font: str = BODY, size: float = 9.5, indent: float = 0, color=INK,
                  leading: float | None = None, bullet: bool = False) -> None:
        """One wrapped paragraph. **bold** spans are drawn bold, so skill groups and labels keep their emphasis."""
        nonlocal y
        leading = leading or size * 1.35
        bold = BOLD if font == BODY else font
        space = stringWidth(" ", font, size)
        limit = usable - indent
        rows: list[list[tuple[str, str]]] = [[]]
        used = 0.0
        for chunk, is_bold in _runs(text):
            for word in chunk.split():
                word_font = bold if is_bold else font
                word_width = stringWidth(word, word_font, size)
                if rows[-1] and used + space + word_width > limit:
                    rows.append([])
                    used = 0.0
                used += (space if rows[-1] else 0) + word_width
                rows[-1].append((word, word_font))
        for i, row in enumerate(rows):
            room(leading)
            c.setFillColorRGB(*color)
            if bullet and i == 0:
                c.setFont(font, size)
                c.drawString(MARGIN + indent - 9, y - size, "\u2022")
            x = MARGIN + indent
            # Consecutive words in the same font are drawn as one string, so the PDF still extracts as real
            # sentences (ATS parsers and pypdf read it the way a person does).
            for segment, segment_font in _segments(row):
                c.setFont(segment_font, size)
                c.drawString(x, y - size, segment)
                x += stringWidth(segment, segment_font, size) + space
            y -= leading

    for raw in body_text(markdown).splitlines():
        line = flatten_links(to_plain(raw.rstrip()))
        stripped = line.strip()
        if not stripped:
            y -= 4
            continue
        if stripped.startswith("### "):
            parts = [p.strip() for p in unbold(stripped[4:]).split("|")]
            dates = parts[-1] if len(parts) > 2 else ""
            title = parts[0]
            rest = " | ".join(parts[1:-1] if dates else parts[1:])
            y -= 3
            size = 10.5
            left_width = stringWidth(title, BOLD, size) + (stringWidth(f"  {rest}", BODY, 9.5) if rest else 0)
            dates_width = stringWidth(dates, BODY, 9.5) if dates else 0
            if left_width + dates_width + 12 <= usable:
                room(size * 1.4)
                c.setFillColorRGB(*INK)
                c.setFont(BOLD, size)
                c.drawString(MARGIN, y - size, title)
                if rest:
                    c.setFont(BODY, 9.5)
                    c.setFillColorRGB(*MUTED)
                    c.drawString(MARGIN + stringWidth(title, BOLD, size), y - size, f"  {rest}")
                if dates:
                    c.setFont(BODY, 9.5)
                    c.setFillColorRGB(*MUTED)
                    c.drawRightString(width - MARGIN, y - size, dates)
                y -= size * 1.4
            else:
                paragraph(stripped[4:], BOLD, size)
            continue
        if stripped.startswith("## "):
            y -= 8
            room(26)
            paragraph(unbold(stripped[3:]).upper(), BOLD, 10.5, color=INK, leading=14)
            c.setStrokeColorRGB(*RULE)
            c.setLineWidth(0.6)
            c.line(MARGIN, y + 2, width - MARGIN, y + 2)
            y -= 4
            after_name = False
            continue
        if stripped.startswith("# "):
            paragraph(unbold(stripped[2:]), BOLD, 18, leading=24)
            after_name = True
            continue
        if stripped.startswith("- "):
            paragraph(stripped[2:], indent=12, bullet=True)
            continue
        paragraph(stripped, color=MUTED if after_name else INK)
    c.save()
    buffer.seek(0)
    return buffer.read()


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def markdown_to_docx(markdown: str) -> bytes:
    """The same resume Markdown as a Word document, for portals that reject PDF."""
    from docx import Document

    document = Document()

    def write(paragraph, text: str) -> None:
        """Markdown emphasis becomes real bold runs instead of literal ** markers."""
        for index, part in enumerate(text.split("**")):
            if part:
                paragraph.add_run(part).bold = index % 2 == 1

    for line in (markdown or "").split("## Source Resume")[0].splitlines():
        line = flatten_links(line)
        if line.startswith("# "):
            document.add_heading(unbold(line[2:]), level=0)
        elif line.startswith("### "):
            parts = [p.strip() for p in unbold(line[4:]).split("|")]
            paragraph = document.add_paragraph()
            paragraph.add_run(parts[0]).bold = True
            if len(parts) > 1:
                paragraph.add_run("  |  " + "  |  ".join(parts[1:]))
        elif line.startswith("## "):
            document.add_heading(unbold(line[3:]), level=1)
        elif line.startswith("- "):
            write(document.add_paragraph(style="List Bullet"), line[2:])
        elif line.strip():
            write(document.add_paragraph(), line)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# What a downloaded packet file is called. The apply kit shows this name and the download headers use the same
# one, so what lands in the Downloads folder is what the screen said it would be.
MAX_SLUG = 80


def packet_slug(name: str, company: str) -> str:
    """`rohan-roy-straive` — the candidate and the employer, safe for a filename on any platform."""
    slug = re.sub(r"[^a-z0-9]+", "-", f"{name or ''} {company or ''}".lower()).strip("-")[:MAX_SLUG].strip("-")
    return slug or "resume"


COVER_LETTER_KINDS = {"cover_letter": "txt", "cover_letter_pdf": "pdf", "cover_letter_docx": "docx"}


def packet_filename(slug: str, kind: str, version: int | None = None, *, draft: bool = False) -> str:
    """The file name for one part of a packet: resume PDF and DOCX (`pdf`, `docx`), or the cover letter as text
    (`cover_letter`, the Apply screen's download), PDF (`cover_letter_pdf`) or DOCX (`cover_letter_docx`). A `draft`
    (a version nobody approved yet) says so in its name."""
    suffix = "-draft" if draft else ""
    if kind in COVER_LETTER_KINDS:
        return f"cover-letter-{slug}{suffix}.{COVER_LETTER_KINDS[kind]}"
    return f"{slug}-v{version or 1}{suffix}.{kind}"
