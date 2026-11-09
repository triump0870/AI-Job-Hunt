"""Helpers for the guarded-submission tests (Phase 6): a local fake ATS that answers a submitted form the way a real
one might (a confirmation, an ambiguous page, an error), the routing that points the Greenhouse route at it, and a
guarded account. Nothing here reaches the internet, and no real application is ever submitted.
"""
from __future__ import annotations

import dataclasses
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self

from app.services import ats_map
from tests import application_support as aps

CONFIRMATION = b"""<!doctype html><html><head><title>Thank you</title></head><body>
<h1>Thank you for applying</h1><p>Your application has been submitted. Confirmation number: GH-48213</p></body></html>"""
AMBIGUOUS = b"""<!doctype html><html><head><title>Processing</title></head><body>
<p>One moment please.</p></body></html>"""
ERROR = b"""<!doctype html><html><head><title>Error</title></head><body><p>Something went wrong.</p></body></html>"""


class FakeATS:
    """Serves the fixture forms, counts every request, and answers POST /apply/submit with `answer`:
    "confirm" (303 to a confirmation page), "ambiguous" (200, no form and no confirmation), "off_site" (307, repeating
    the POST on `localhost`, another host of this same server) or "error" (500)."""

    def __init__(self, answer: str = "confirm", pages: dict[str, bytes] | None = None) -> None:
        self.answer = answer
        self.pages = pages or {}
        self.requests: list[tuple[str, str]] = []
        ats = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                ats.requests.append(("GET", self.path))
                name = self.path.lstrip("/").split("?")[0]
                if name == "confirmation.html":
                    return self._send(200, CONFIRMATION)
                if name in ats.pages:
                    return self._send(200, ats.pages[name])
                path = aps.FORMS / name
                if name.endswith(".html") and path.exists():
                    return self._send(200, path.read_bytes())
                return self._send(404, b"<html><body>nothing here</body></html>")

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                ats.requests.append(("POST", self.path))
                if self.path.startswith("/apply/submit"):
                    if ats.answer == "confirm":
                        return self._send(303, b"", {"Location": "/confirmation.html"})
                    if ats.answer == "ambiguous":
                        return self._send(200, AMBIGUOUS)
                    if ats.answer == "off_site":
                        port = self.server.server_address[1]
                        return self._send(307, b"", {"Location": f"http://localhost:{port}/elsewhere"})
                    return self._send(500, ERROR)
                return self._send(200, b"ok")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def url(self, name: str = "greenhouse_form") -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/{name}.html"

    @property
    def submits(self) -> list[str]:
        return [p for m, p in self.requests if m == "POST" and p.startswith("/apply/submit")]

    @property
    def posts(self) -> list[str]:
        return [p for m, p in self.requests if m != "GET"]

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def route_greenhouse_here(monkeypatch) -> None:
    """Point the Greenhouse route at the local fake ATS (127.0.0.1), as if it were a Greenhouse host."""
    patched = tuple(dataclasses.replace(a, hosts=(*a.hosts, "127.0.0.1")) if a.name == "greenhouse" else a
                    for a in ats_map.ATSES)
    monkeypatch.setattr(ats_map, "ATSES", patched)


def configure(monkeypatch) -> None:
    aps.configure(monkeypatch, "guarded_auto")
    settings = aps.get_settings()
    monkeypatch.setattr(settings, "application_allow_private_hosts", True)
    route_greenhouse_here(monkeypatch)


def consent(sessions, user_id: int, *, enabled: bool = True, attestations: list[str] | None = None) -> None:
    from app.services import submission

    with sessions() as db:
        db.info["user_id"] = user_id
        submission.set_consent(db, user_id, enabled=enabled, attestations=attestations or [])
        db.commit()


def guarded_account(sessions, *, sensitive: bool = False) -> int:
    user = aps.owner(sessions)
    aps.answer(sessions, user, aps.PYTHON_QUESTION, "8")
    aps.answer(sessions, user, aps.AUTH_QUESTION, "Yes", sensitive=sensitive)
    consent(sessions, user)
    return user
