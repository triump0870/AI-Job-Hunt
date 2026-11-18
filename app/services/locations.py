"""Location filters: every list that can be filtered by location groups postings by city the same way.

Used by Discovery, Tailor, Approvals and the ranking tracker on the server, and mirrored in app/static/app.js for
the Tracker, which filters in the browser.
"""
from __future__ import annotations

import re

# "Hybrid - Bengaluru", "On-site: Pune": the work mode is its own filter, so the place is what is grouped.
MODE_PREFIX = re.compile(r"^(?:hybrid|on[- ]?site|in[- ]office|office)\s*[-–—:|]\s*", re.IGNORECASE)


def location_key(location: str | None) -> str:
    """The city a location filter groups by: "Bengaluru, Karnataka, India" → "Bengaluru"; anything remote → "Remote";
    a work-mode prefix is dropped ("Hybrid - Bengaluru" → "Bengaluru").
    Mirrors locationKey in app/static/app.js (the Tracker filters in the browser)."""
    text = MODE_PREFIX.sub("", (location or "").strip()).strip()
    if not text:
        return "Not listed"
    if "remote" in text.lower():
        return "Remote"
    return text.split(",")[0].strip() or "Not listed"


def location_facets(locations: list[str | None]) -> list[list]:
    """[city, count] pairs, most common first, for a location filter's options."""
    counts: dict[str, int] = {}
    for location in locations:
        key = location_key(location)
        counts[key] = counts.get(key, 0) + 1
    return [[key, n] for key, n in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))]
