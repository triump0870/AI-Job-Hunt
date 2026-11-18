"""Mechanical defects in a rendered resume that a recruiter would see. Deterministic, no model.

    lint(markdown) → [{code, line, text, message}]

Only defects with no reading in which they are intended are reported:

* REPEATED_LINE      a line that repeats an earlier line, or the end of the line just above it ("…teams." then
                     "- resulting in … teams.");
* STRAY_PUNCTUATION  a bullet whose text starts with another bullet mark or a dash ("-Built", "- • Led");
* SPACE_BEFORE_PUNCT a space before a comma, period, semicolon or colon ("MCP , and").

Shown on the Approvals checklist so the person can fix the packet in Tailor before approving. Nothing here edits
the resume.
"""
from __future__ import annotations

import re
from typing import Any

from app.services.resume_format import body_text

MIN_REPEAT_WORDS = 4
STRAY_RE = re.compile(r"^(?:-\s*[-–—•*·]|-(?=[A-Za-z]))")
SPACE_PUNCT_RE = re.compile(r"\w \s*[,.;:](?:\s|$)")
MESSAGES = {
    "REPEATED_LINE": "repeats text that is already on the resume",
    "STRAY_PUNCTUATION": "starts with a stray dash or bullet mark",
    "SPACE_BEFORE_PUNCT": "has a space before punctuation",
}


def _words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9%]+", (text or "").lower()))


def _content(line: str) -> str:
    return line.strip().removeprefix("- ").strip()


def lint(markdown: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous = ""
    for number, raw in enumerate(body_text(markdown).splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            previous = ""
            continue
        text = _content(line)
        words = _words(text)

        found = []
        if len(words.split()) >= MIN_REPEAT_WORDS and (words in seen or (previous and words in previous)):
            found.append("REPEATED_LINE")
        if STRAY_RE.match(line):
            found.append("STRAY_PUNCTUATION")
        if SPACE_PUNCT_RE.search(text) and "http" not in text:
            found.append("SPACE_BEFORE_PUNCT")
        issues += [{"code": code, "line": number, "text": text[:160], "message": MESSAGES[code]} for code in found]
        if words:
            seen.add(words)
        previous = words
    return issues
