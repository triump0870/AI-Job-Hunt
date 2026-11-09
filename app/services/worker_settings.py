"""An account's own worker settings (Running workers → Settings): whether each queue runs on its own, how many attempts a
task gets and how long it waits between them, and which fallbacks JD recovery may use.

Stored in AppSetting.preferences["workers"][kind]. A value the account never set follows the server (app.settings), so
nothing changes for an account that never opens the screen. Concurrency, the ranking cadence and the rate-limit backoff
keep their own homes (app.services.user_settings, app.services.backoff) and are edited on the same screen.

Every reader here is cheap and never raises: a broken or missing value falls back to the server's.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AppSetting
from app.settings import get_settings


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    help: str
    kind: str  # "bool" or "int"
    default: Any  # a value, or the app.settings attribute name (str) the server's value comes from
    low: int = 0
    high: int = 0

    def server_default(self) -> Any:
        if isinstance(self.default, str):
            value = getattr(get_settings(), self.default)
            return bool(value) if self.kind == "bool" else int(value)
        return self.default


AUTO = Field("auto", "Run automatically",
             "Off: tasks wait in the queue until you press Run now. Nothing is dropped.", "bool", True)
FIELDS: dict[str, tuple[Field, ...]] = {
    "parsing": (AUTO,
                Field("max_attempts", "Attempts per task", "How often a posting is tried before it is marked failed.",
                      "int", "jd_parse_max_attempts", 1, 10),
                Field("retry_seconds", "Wait before a retry (seconds)",
                      "Multiplied by the attempt number: the second retry waits twice as long.", "int",
                      "jd_parse_retry_seconds", 10, 86400)),
    "classification": (AUTO,
                       Field("max_attempts", "Attempts per job",
                             "How often a job is sent to the model. After the last one it moves on to the detailed "
                             "match unhidden: a failed check never hides a job.", "int",
                             "domain_classifier_max_attempts", 1, 10),
                       Field("retry_seconds", "Wait before a retry (seconds)",
                             "Multiplied by the attempt number: the second retry waits twice as long.", "int",
                             "domain_classifier_retry_seconds", 10, 86400)),
    "recovery": (AUTO,
                 Field("max_attempts", "Attempts per task", "How often a posting URL is tried.", "int",
                       "jd_fetch_max_attempts", 1, 10),
                 Field("retry_seconds", "First retry wait (seconds)", "Doubles with each attempt.", "int",
                       "jd_fetch_retry_seconds", 10, 86400),
                 Field("use_browser", "Use the browser when HTTP finds nothing",
                       "Renders the page in a headless browser. Login walls and anti-bot checks are never bypassed.",
                       "bool", True),
                 Field("use_model", "Ask a model on ambiguous pages",
                       "Picks the description among a few candidate blocks; uses your AI model.", "bool", True)),
    "ranking": (AUTO,
                Field("max_attempts", "Attempts per job", "How often a ranking is tried before it is marked failed.",
                      "int", "ranking_max_attempts", 1, 10),
                Field("retry_base_seconds", "First retry wait (seconds)", "Doubles with each attempt.", "int",
                      "ranking_retry_base_seconds", 10, 86400),
                Field("retry_max_seconds", "Longest retry wait (seconds)", "The doubling stops here.", "int",
                      "ranking_retry_max_seconds", 10, 86400)),
    "qualification": (AUTO,
                      Field("max_attempts", "Attempts per run",
                            "How often a graph run that failed is tried again before it is marked failed. Waiting for "
                            "recovery, parsing or ranking never uses an attempt.", "int",
                            "qualification_max_attempts", 1, 10),
                      Field("retry_seconds", "Wait before a retry (seconds)",
                            "Multiplied by the attempt number.", "int", "qualification_retry_seconds", 10, 86400)),
    "preparation": (AUTO,
                    Field("max_attempts", "Attempts per run",
                          "How often a graph run that failed is tried again before it is marked failed. Waiting for "
                          "tailoring or for your approval never uses an attempt.", "int",
                          "preparation_max_attempts", 1, 10),
                    Field("retry_seconds", "Wait before a retry (seconds)",
                          "Multiplied by the attempt number.", "int", "preparation_retry_seconds", 10, 86400),
                    Field("max_revisions", "Revisions per packet",
                          "How many more tailoring passes a packet the review or the evidence checks send back may "
                          "get before it waits for you. A packet never passes by running out of revisions.", "int",
                          "preparation_max_revisions", 0, 3)),
    "application": (AUTO,
                    Field("max_attempts", "Attempts per run",
                          "How often a run that failed (the page did not load, the browser broke) is tried again "
                          "before it is marked failed. Waiting for your answers never uses an attempt.", "int",
                          "application_execution_max_attempts", 1, 10),
                    Field("retry_seconds", "Wait before a retry (seconds)",
                          "Multiplied by the attempt number.", "int", "application_execution_retry_seconds", 10,
                          86400)),
    "insights": (AUTO,
                 Field("max_attempts", "Attempts per job", "How often research is tried when every source fails.",
                       "int", "job_insights_max_attempts", 1, 10),
                 Field("retry_seconds", "First retry wait (seconds)", "Doubles with each attempt.", "int",
                       "job_insights_retry_seconds", 60, 86400)),
    "indexing": (AUTO,
                 Field("max_attempts", "Attempts per entry",
                       "How often a fact or answer is sent to the embedding model before it is marked failed.", "int",
                       "rag_index_max_attempts", 1, 10),
                 Field("retry_seconds", "First retry wait (seconds)", "Doubles with each attempt, up to an hour.",
                       "int", "rag_index_retry_seconds", 10, 3600)),
    "resume_parsing": (AUTO,
                       Field("max_attempts", "Attempts per resume",
                             "How often a resume is sent to the Resume Parser model before it is marked failed.",
                             "int", "resume_parse_max_attempts", 1, 10),
                       Field("retry_seconds", "Wait before a retry (seconds)",
                             "Multiplied by the attempt number: the second retry waits twice as long.", "int",
                             "resume_parse_retry_seconds", 10, 86400),
                       Field("ai_only", "Structure uploads with AI only",
                             "On: a new upload goes straight to the Resume Parser model (ahead of the queue, even "
                             "while paused), and its data is shown once the model is done. The quick regex parse is "
                             "kept only as the fallback when the model cannot answer.", "bool", False),
                       Field("extraction_v2", "Read PDF uploads with the new reader",
                             "Reads the text a PDF states for ligatures (\"Kafka\", not \"Kaga\"), joins words a "
                             "line break split, and checks how far the text can be trusted. Applies to new uploads; "
                             "off, PDFs are read as before. Follows the server (RESUME_EXTRACTION_V2) until you "
                             "change it.", "bool", "resume_extraction_v2")),
    "tailoring": (AUTO,
                  Field("max_attempts", "Attempts per packet",
                        "How often an interrupted tailoring run is restarted before it is marked failed.", "int", 2,
                        1, 5)),
}


def _saved(preferences: dict[str, Any] | None, kind: str) -> dict[str, Any]:
    stored = ((preferences or {}).get("workers") or {}).get(kind)
    return stored if isinstance(stored, dict) else {}


def _resolve(kind: str, stored: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for f in FIELDS[kind]:
        raw, fallback = stored.get(f.name), f.server_default()
        if f.kind == "bool":
            out[f.name] = raw if isinstance(raw, bool) else fallback
            continue
        try:
            out[f.name] = max(f.low, min(f.high, int(raw))) if raw is not None else fallback
        except (TypeError, ValueError):
            out[f.name] = fallback
    return out


def _preferences(db: Session, user_id: int) -> dict[str, Any]:
    return db.scalar(select(AppSetting.preferences).where(AppSetting.user_id == user_id)) or {}


def for_user(db: Session, user_id: int, kind: str) -> dict[str, Any]:
    """One worker's settings for one account, defaults filled in."""
    try:
        return _resolve(kind, _saved(_preferences(db, user_id), kind))
    except Exception:  # noqa: BLE001  a settings read never stops a worker
        return _resolve(kind, {})


def get(db: Session, user_id: int, kind: str, name: str) -> Any:
    return for_user(db, user_id, kind)[name]


def paused(db: Session, kind: str, user_ids: Iterable[int] | None = None) -> set[int]:
    """Accounts that switched a worker's automatic runs off. Bounded to the accounts asked about when given."""
    query = select(AppSetting.user_id, AppSetting.preferences)
    if user_ids is not None:
        ids = list(dict.fromkeys(user_ids))
        if not ids:
            return set()
        query = query.where(AppSetting.user_id.in_(ids))
    try:
        rows = db.execute(query.limit(10000)).all()
    except Exception:  # noqa: BLE001
        return set()
    return {uid for uid, prefs in rows if _saved(prefs, kind).get("auto") is False}


def describe(db: Session, user_id: int) -> dict[str, Any]:
    """Every worker's fields: label, help, bounds, the server's default and this account's value."""
    prefs = _preferences(db, user_id)
    return {kind: {"values": _resolve(kind, _saved(prefs, kind)),
                   "fields": [{"name": f.name, "label": f.label, "help": f.help, "type": f.kind, "min": f.low,
                               "max": f.high, "default": f.server_default()} for f in fields]}
            for kind, fields in FIELDS.items()}


class WorkerSettingError(ValueError):
    pass


def update(db: Session, user_id: int, changes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Save an account's changes. A value equal to the server's default is stored as "follow the server". Commits."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.services.user_settings import lock_settings_row, settings_for

    clean: dict[str, dict[str, Any]] = {}
    for kind, values in (changes or {}).items():
        fields = {f.name: f for f in FIELDS.get(kind, ())}
        if not fields:
            raise WorkerSettingError(f"Unknown worker '{kind}'")
        for name, raw in (values or {}).items():
            f = fields.get(name)
            if f is None:
                raise WorkerSettingError(f"'{name}' is not a setting of {kind}")
            if f.kind == "bool":
                if not isinstance(raw, bool):
                    raise WorkerSettingError(f"{f.label}: on or off")
                value: Any = raw
            else:
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    raise WorkerSettingError(f"{f.label}: enter a whole number") from None
                if not f.low <= value <= f.high:
                    raise WorkerSettingError(f"{f.label}: between {f.low} and {f.high}")
            clean.setdefault(kind, {})[name] = None if value == f.server_default() else value
    row = lock_settings_row(db, settings_for(db, user_id))
    prefs = dict(row.preferences or {})
    workers = dict(prefs.get("workers") or {})
    if "ranking" in clean:  # the first retry wait can never be longer than the longest one
        merged = _resolve("ranking", {**_saved(prefs, "ranking"),
                                      **{k: v for k, v in clean["ranking"].items() if v is not None},
                                      **{k: None for k, v in clean["ranking"].items() if v is None}})
        if merged["retry_base_seconds"] > merged["retry_max_seconds"]:
            raise WorkerSettingError("First retry wait: it cannot be longer than the longest retry wait")
    for kind, values in clean.items():
        merged = {**(workers.get(kind) or {}), **values}
        workers[kind] = {k: v for k, v in merged.items() if v is not None}
    prefs["workers"] = workers
    row.preferences = prefs
    flag_modified(row, "preferences")
    db.commit()
    return describe(db, user_id)
