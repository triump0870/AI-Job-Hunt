"""[F] The parse view: a resume's text with its structure marked, for structured_resume.parse_deterministic.

    lines + what the file says about them (LineInfo)  +  where sections start (sections.detect)
    → "## <Section>" for every heading, "### Title | Company | Location | Dates" for every role, "- " for every
      bullet, "Name: description" for every project, " | " where the page leaves a gap between two columns

The view is derived, never stored as the resume's text: Resume.raw_text stays what the file says. It holds the
document's own words and nothing else (markers and a section label in place of a heading as written), so grounding
in the raw text still holds for everything parsed from it. Off (RESUME_EXTRACTION_V2), nothing here runs.

Roles are assembled from the layouts resumes use:

    Title | Company | Location | Dates                   one line (kept)
    Company ⟶ Location | Dates  /  Title               the employer's line, then the title
    Company ⟶ Dates  /  Title ⟶ Location | Dates       several roles at one employer (the first line is the group)
    Title  /  Dates                                      another role at the same employer

Bullets written without a character (a drawn marker, Word numbering) are found by where sentences end.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.services.resume_extraction.models import LineInfo, TextLayer
from app.services.resume_extraction.sections import LABELS, Heading, detect
from app.services.structured_resume import DATE_RANGE_RE

_BULLET = re.compile(r"^\s*(?:[-*•●▪‣⁃◦]|\d+[.)])\s+")
_END = re.compile(r"[.!?;:)]$")
SEGMENT_GAP = 2.2  # a gap wider than this many glyph heights between two words splits the line into columns
TITLE_WORDS, TITLE_CHARS = 8, 70


# ───────────── line features from the text layer ─────────────

def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _segments(words, size: float | None) -> list[str]:
    """The line's words in runs the page separates by a wide gap (a column), or [] for one run."""
    runs: list[list[str]] = [[]]
    for prev, word in zip([None, *words], words, strict=False):
        gap = (word.box.x0 - prev.box.x1) if prev is not None and word.box and prev.box else 0
        if size and gap > SEGMENT_GAP * size:
            runs.append([])
        runs[-1].append(word.text)
    return [" ".join(run) for run in runs] if len(runs) > 1 else []


def layer_lines(layer: TextLayer) -> list[LineInfo]:
    """The text layer's lines with their look: glyph height, weight, left edge and columns."""
    out = []
    for page in layer.pages:
        for line in page.lines:
            sizes = sorted(w.size for w in line.words if w.size)
            size = sizes[len(sizes) // 2] if sizes else None
            out.append(LineInfo(text=line.text, size=size, bold=sum(w.bold for w in line.words) * 2 > len(line.words),
                                x0=min((w.box.x0 for w in line.words if w.box), default=None),
                                segments=_segments(line.words, size) or None))
    return out


def line_infos(text: str, layer: TextLayer | None = None, sources: list[LineInfo] | None = None) -> list[LineInfo]:
    """The text's lines, each with the look of the source line it came from (the PDF's text layer, or the Word
    document's paragraphs). Lines are matched by their letters, so a repair, a joined hyphen or normalised spaces do
    not break the match; a line with no match has no look. Columns carry over when the line has the same words."""
    lines = (text or "").split("\n")
    infos = [LineInfo(text=line) for line in lines]
    sources = sources if sources is not None else layer_lines(layer) if layer is not None else None
    if not sources:
        return infos
    a, b = [_key(s.text) for s in sources], [_key(line) for line in lines]
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op in ("delete", "insert"):
            continue
        for offset, j in enumerate(range(j1, j2)):
            src = sources[min(i1 + offset, i2 - 1)] if op == "equal" or i2 - i1 == j2 - j1 else sources[i1]
            info = infos[j]
            info.size, info.bold, info.x0, info.style, info.bullet = src.size, src.bold, src.x0, src.style, src.bullet
            if src.segments:
                info.segments = _resplit(lines[j], src.segments)
    return infos


def _resplit(line: str, segments: list[str]) -> list[str] | None:
    """The line's own words in the source's columns, when they are the same words. A bar is a word inside a PDF
    column ("Remote, US | Oct 2025") and only a separator between Word table cells: both are tried."""
    for keep_bars in (True, False):
        def words(text: str, keep_bars: bool = keep_bars) -> list[str]:
            return text.split() if keep_bars else text.replace(" | ", " ").split()
        counts, own = [len(words(s)) for s in segments], words(line)
        if sum(counts) == len(own):
            out, k = [], 0
            for n in counts:
                out.append(" ".join(own[k:k + n]))
                k += n
            return out
    return None


# ───────────── the view ─────────────

def _text(info: LineInfo) -> str:
    """A line with " | " where the page leaves a column gap."""
    return " | ".join(info.segments) if info.segments else info.text.strip()


def _dated(text: str) -> re.Match[str] | None:
    return DATE_RANGE_RE.search(text) if len(text) < 160 else None


def _split(text: str) -> tuple[str, str, str]:
    """(lead, location, dates) of a dated line: "Flow AI | Remote, US | Oct 2025 - Sep 2026"."""
    match = _dated(text)
    dates = match.group(0) if match else ""
    rest = (text[:match.start()] + text[match.end():]) if match else text
    parts = [p.strip(" ,-–—|\t") for p in re.split(r"\s*\|\s*|\t", rest)]
    parts = [p for p in parts if p]
    return (parts[0] if parts else ""), ", ".join(parts[1:]), dates


def _parts(text: str) -> list[str]:
    match = _dated(text)
    rest = (text[:match.start()] + text[match.end():]) if match else text
    return [p for p in (p.strip(" ,-–—|\t") for p in re.split(r"\s*\|\s*|\t", rest)) if p]


def _complete(text: str) -> bool:
    """A role already written on one line: title, employer and location (three parts besides the dates). With two
    ("Flow AI | Remote, US | Oct 2025 - Sep 2026") the title is on a line of its own."""
    return len(_parts(text)) >= 3


def _titleish(text: str) -> bool:
    words = text.split()
    return (0 < len(words) <= TITLE_WORDS and len(text) <= TITLE_CHARS and not _BULLET.match(text)
            and not re.search(r"[.!?;:,]$", text) and not _dated(text) and "," not in text
            and any(c.isalpha() for c in text))


def _header(title: str, company: str, location: str, dates: str) -> str:
    return "### " + " | ".join(p for p in (title, company, location, dates) if p)


def _bullets(lines: list[str], markers: list[bool]) -> list[str]:
    """Body lines of a role or project as "- " items. With bullet characters in the section, wrapped lines are left
    for the parser to join; without any, an item starts where the previous sentence ended."""
    out: list[str] = []
    explicit = any(_BULLET.match(line) for line in lines)
    for line, marked in zip(lines, markers, strict=True):
        if _BULLET.match(line):
            out.append("- " + _BULLET.sub("", line).strip())
        elif explicit and not marked:
            out.append(line)
        elif marked or not out or _END.search(out[-1]):
            out.append("- " + line)
        else:
            out[-1] = f"{out[-1]} {line}"
    return out


_DATE_TAIL = re.compile(r"^[–—-]\s*(?:[A-Za-z]{3,9}\.?\s+)?(?:\d{4}|present|current|now)\b", re.IGNORECASE)


def _unwrap_headers(rows: list[LineInfo]) -> tuple[list[LineInfo], list[str]]:
    """A role line a narrow column wrapped is one line again: after a bar ("Data Scientist | Litware |" /
    "Dallas, TX | 2017 – 2020") or inside its dates ("Boulder, CO | Sep 2014" / "– Mar 2019")."""
    kept, texts = [], []
    for info in rows:
        text = _text(info)
        if texts and (texts[-1].rstrip().endswith(("|", "–", "—", " -")) or _DATE_TAIL.match(text)):
            texts[-1] = f"{texts[-1].rstrip()} {text}"
            continue
        kept.append(info)
        texts.append(text)
    return kept, texts


_MD_HEADING = re.compile(r"^#{1,6}\s+")


def _experience(infos: list[LineInfo]) -> list[str]:
    out: list[str] = []
    rows, texts = _unwrap_headers([i for i in infos if i.text.strip()])
    # A text resume already written with "### " role headers (this app's own export, a Markdown file) is marked once.
    texts = [_MD_HEADING.sub("", t) for t in texts]
    company = location = ""
    body: list[tuple[str, bool]] = []

    def flush() -> None:
        if body:
            out.extend(_bullets([t for t, _ in body], [m for _, m in body]))
            body.clear()

    j = 0
    while j < len(rows):
        text, info = texts[j], rows[j]
        nxt = texts[j + 1] if j + 1 < len(rows) else ""
        # A short line is a title only beside a date line; otherwise it is the start of a wrapped sentence.
        if info.bullet or _BULLET.match(text) or not (_dated(text) or (_titleish(text) and _dated(nxt))):
            body.append((text, info.bullet))
            j += 1
            continue
        if _dated(text):
            lead, loc, dates = _split(text)
            if _complete(text):
                flush()
                out.append("### " + text)
                company, location = "", ""
                j += 1
                continue
            if lead and nxt and _titleish(nxt):  # Company | Location | Dates, then the title
                flush()
                company, location = lead, loc
                k = j + 2
                if k < len(rows) and _dated(texts[k]) and not _split(texts[k])[0]:
                    dates = _split(texts[k])[2]  # the role's own dates under the title
                    k += 1
                out.append(_header(nxt, company, location, dates))
                j = k
                continue
            if lead and nxt and _dated(nxt) and _split(nxt)[0] and not _complete(nxt):
                company, location = lead, loc  # an employer's overall dates above its roles
                j += 1
                continue
            if lead and company:  # another role at the same employer: Title | Location | Dates
                flush()
                out.append(_header(lead, company, loc or location, dates))
                j += 1
                continue
            flush()
            out.append("### " + text)  # a role line the layouts above do not explain: as the parser always read it
            j += 1
            continue
        # A short line: a title over its own dates at the current employer, or role meta (a location line).
        if company and nxt and _dated(nxt) and not _split(nxt)[0]:
            flush()
            out.append(_header(text, company, location, _split(nxt)[2]))
            j += 2
            continue
        flush()
        out.append(text)
        j += 1
    flush()
    return out


def _projects(infos: list[LineInfo]) -> list[str]:
    """A short name line followed by its description becomes "Name: description"; anything else stays."""
    out: list[str] = []
    rows = [_text(i) for i in infos if i.text.strip()]
    j = 0
    while j < len(rows):
        line = rows[j]
        if out and not _BULLET.match(line) and not _CATEGORY.match(line) and not _titleish(line) \
                and not re.search(r"[.!?]$", out[-1]):
            out[-1] = f"{out[-1]} {line}"  # a description a narrow column wrapped
            j += 1
            continue
        if _titleish(line) and ":" not in line and j + 1 < len(rows) and not _titleish(rows[j + 1]) \
                and not _BULLET.match(rows[j + 1]):
            k, description = j + 1, []
            while k < len(rows) and not _titleish(rows[k]) and not _BULLET.match(rows[k]):
                description.append(rows[k])
                k += 1
            out.append(f"{line}: {' '.join(description)}")
            j = k
            continue
        out.append(line)
        j += 1
    return out


_CATEGORY = re.compile(r"^[^,:]{1,40}:\s")


WRAP_FILL = 0.8  # a wrapped line fills its column: at least this share of the section's widest line


def _wrapped(previous: LineInfo, widest: int) -> bool:
    """Evidence that `previous` ran on into the next line: it ends with a list separator, or (a PDF line, never a
    Word paragraph, which never wraps in the text) it fills the column. A comma inside a line is no such evidence:
    "Python, Java" and "SQL, Go" are two rows."""
    text = previous.text.rstrip()
    if text.endswith(","):
        return True
    return previous.style is None and previous.x0 is not None and len(text) >= WRAP_FILL * widest


def _skills(infos: list[LineInfo]) -> list[str]:
    """A table row "Category  ⟶  a, b, c" reads "Category: a, b, c"; a comma list a narrow column wrapped
    ("Platform Strategy, Incident" / "Management, API Gateway") is one line again, when the layout shows the wrap
    (`_wrapped`). A list with one skill per line has no commas and stays as it is."""
    out: list[str] = []
    widest = max((len(i.text.rstrip()) for i in infos), default=0)
    previous: LineInfo | None = None
    for info in infos:
        if not info.text.strip():
            continue
        if info.segments and len(info.segments) == 2 and ":" not in info.segments[0] \
                and len(info.segments[0].split()) <= 3 and "," not in info.segments[0]:
            line = f"{info.segments[0]}: {info.segments[1]}"
        else:
            line = _text(info)
        if out and previous is not None and "," in out[-1] and not out[-1].rstrip().endswith(".") \
                and not _CATEGORY.match(line) and not _BULLET.match(line) and not previous.segments \
                and _wrapped(previous, widest):
            out[-1] = f"{out[-1].rstrip()} {line}"
        else:
            out.append(line)
        previous = info
    return out


def view(infos: list[LineInfo], headings: list[Heading] | None = None) -> str:
    """The parse view of a document (see the module docstring)."""
    headings = detect(infos) if headings is None else headings
    starts = {h.line: h for h in headings}
    blocks: list[tuple[Heading | None, list[LineInfo]]] = [(None, [])]
    for i, info in enumerate(infos):
        if i in starts:
            blocks.append((starts[i], []))
        else:
            blocks[-1][1].append(info)
    out: list[str] = []
    for heading, lines in blocks:
        if heading is None:
            out += [i.text for i in lines]
            continue
        out += ["", f"## {LABELS[heading.kind]}"]
        if heading.kind == "experience":
            out += _experience(lines)
        elif heading.kind == "projects":
            out += _projects(lines)
        elif heading.kind == "skills":
            out += _skills(lines)
        elif heading.kind in ("education", "certifications", "awards"):
            out += _unwrap_headers([i for i in lines if i.text.strip()])[1]
        else:
            out += [("- " + _text(i)) if i.bullet else _text(i) for i in lines if i.text.strip()]
    return "\n".join(out).strip() + "\n"
