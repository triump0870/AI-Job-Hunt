"""Server-wide settings an admin can change without a restart, stored in `server_settings`.

Two kinds, both overriding the environment (app.settings), which stays the default; saving the environment's value
again removes the override:

  * EDITABLE — worker limits (Running workers → Settings): how often a scheduler job runs, how many tasks one run
    claims, how many run side by side. Read with `value(name)`.
  * CONFIG — the agent and application configuration (Settings → AI agents & automation): the LangGraph switch and
    graph modes, the application worker's mode, the browser-use options, checkpoint retention and LangSmith export.
    Each is a choice with a description of every option (or a checked number or text). These are laid over the settings
    object itself (`apply`), so every reader — its derived properties included (`qualification_mode`,
    `application_execution_mode_value`, the scheduler intervals that follow AGENT_GRAPHS_ENABLED) — sees the admin's
    choice. Machine paths (BROWSER_USE_PYTHON) and the server's own secrets stay in the environment. The one key that
    may be saved here is the LangSmith API key (kind "secret"): encrypted with SECRET_KEY (app.security), never
    returned by any API (only whether one is set), and replaced or removed, never read back.

Reads are cached for a few seconds and never raise: if the table cannot be read, the environment value applies.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import SchedulerJob, ServerSetting
from app.security import SecretsUnavailable, decrypt_secret, encrypt_secret
from app.settings import get_settings

log = logging.getLogger(__name__)
CACHE_SECONDS = 10.0


@dataclass(frozen=True)
class Editable:
    name: str  # the app.settings attribute it overrides
    worker: str  # the Running workers tab it belongs to
    label: str
    help: str
    low: int
    high: int
    scheduler_job: str | None = None  # an interval: the scheduler job it paces


EDITABLE: dict[str, Editable] = {e.name: e for e in (
    Editable("domain_classifier_interval_minutes", "classification", "Run every (minutes)",
             "How often the domain classifier looks for work. 0 turns it off for every account.", 0, 1440,
             "domain_classifier"),
    Editable("domain_classifier_batch_size", "classification", "Tasks per run",
             "How many jobs one run claims. Each is its own small model call.", 1, 500),
    Editable("domain_classifier_concurrency", "classification", "Side by side",
             "How many jobs are read at once.", 1, 32),
    Editable("jd_parse_interval_minutes", "parsing", "Run every (minutes)",
             "How often the JD parsing job looks for work. 0 turns it off for every account.", 0, 1440, "jd_parsing"),
    Editable("jd_parse_batch_size", "parsing", "Tasks per run", "How many parse tasks one run claims.", 1, 200),
    Editable("jd_parse_concurrency", "parsing", "Side by side", "How many postings are parsed at once.", 1, 32),
    Editable("resume_parse_interval_minutes", "resume_parsing", "Run every (minutes)",
             "How often resume parsing looks for work. 0 turns it off for every account.", 0, 1440, "resume_parse"),
    Editable("resume_parse_batch_size", "resume_parsing", "Tasks per run", "How many resumes one run claims.", 1, 50),
    Editable("resume_parse_concurrency", "resume_parsing", "Side by side", "How many resumes are parsed at once.",
             1, 8),
    Editable("jd_recovery_interval_minutes", "recovery", "Run every (minutes)",
             "How often JD recovery looks for work. 0 turns it off for every account.", 0, 1440, "jd_recovery"),
    Editable("jd_fetch_batch_size", "recovery", "Tasks per run", "How many recovery tasks one run claims.", 1, 200),
    Editable("jd_fetch_max_concurrency", "recovery", "Side by side", "How many postings are fetched at once.", 1, 16),
    Editable("job_insights_interval_minutes", "insights", "Run every (minutes)",
             "How often Job insights looks for work. 0 turns it off for every account.", 0, 1440, "job_insights"),
    Editable("job_insights_batch_size", "insights", "Tasks per run", "How many jobs one run researches.", 1, 100),
    Editable("job_insights_max_concurrency", "insights", "Side by side", "How many jobs are researched at once.", 1, 8),
    Editable("ranking_interval_minutes", "ranking", "Run every (minutes)",
             "How often the ranking job looks for work. 0 turns it off for every account.", 0, 1440, "ranking"),
    Editable("ranking_batch_size", "ranking", "Tasks per run", "How many ranking tasks one run claims.", 1, 500),
    Editable("ranking_concurrency", "ranking", "Most per account",
             "The most rankings one account may run at once (each account picks up to this).", 1, 64),
    Editable("qualification_interval_minutes", "qualification", "Run every (minutes)",
             "How often the qualification graph looks for work. 0 turns it off for every account; it never runs while "
             "AGENT_GRAPHS_ENABLED is off.", 0, 1440, "qualification"),
    Editable("qualification_batch_size", "qualification", "Tasks per run", "How many graph runs one pass claims.",
             1, 500),
    Editable("qualification_concurrency", "qualification", "Side by side",
             "How many graph runs advance at once. A run only hands work to the other workers, so this is cheap.",
             1, 32),
    Editable("qualification_shadow_daily_limit", "qualification", "Shadow runs per account per day",
             "In shadow mode: how many decided jobs per account a day are compared with the graph (read-only).",
             0, 5000),
    Editable("qualification_shadow_paid_daily_limit", "qualification", "Shadow model calls per account per day",
             "In shadow mode: how many jobs without a ranking may be ranked again by the model, only to compare. "
             "0 (the default) never spends a model call on shadow runs.", 0, 500),
    Editable("preparation_interval_minutes", "preparation", "Run every (minutes)",
             "How often the resume preparation graph looks for work. 0 turns it off for every account; it never runs "
             "while AGENT_GRAPHS_ENABLED is off.", 0, 1440, "preparation"),
    Editable("preparation_batch_size", "preparation", "Tasks per run", "How many graph runs one pass claims.",
             1, 500),
    Editable("preparation_concurrency", "preparation", "Side by side",
             "How many graph runs advance at once. A run hands tailoring to the tailor worker, so this is cheap.",
             1, 32),
    Editable("preparation_shadow_daily_limit", "preparation", "Shadow runs per account per day",
             "In shadow mode: how many reviewed packets per account a day are re-checked by the graph (read-only, "
             "never a model call).", 0, 5000),
    Editable("application_execution_interval_minutes", "application", "Run every (minutes)",
             "How often the application worker looks for work. 0 turns it off for every account; it never runs while "
             "AGENT_GRAPHS_ENABLED is off or APPLICATION_EXECUTION_MODE is off.", 0, 1440, "application_execution"),
    Editable("application_execution_batch_size", "application", "Tasks per run",
             "How many application runs one pass claims.", 1, 100),
    Editable("application_execution_concurrency", "application", "Side by side",
             "How many application runs advance at once. Each holds a browser page while it reads or fills a form.",
             1, 8),
    Editable("tailor_concurrency", "tailoring", "Side by side",
             "How many packets are tailored at once across every account, and the most one account may ask for "
             "(each account picks up to this).", 1, 16),
    Editable("rag_index_interval_minutes", "indexing", "Run every (minutes)",
             "How often the retrieval indexer looks for work. 0 turns it off for every account; it never runs while "
             "RAG_VECTOR_ENABLED is off.", 0, 1440, "rag_index"),
    Editable("rag_embed_batch_size", "indexing", "Entries per batch",
             "How many facts or answers go to the embedding model in one request.", 1, 256),
    Editable("rag_index_max_entries_per_run", "indexing", "Entries per account per run",
             "The most entries one run embeds for one account.", 1, 5000),
    Editable("rag_index_concurrency", "indexing", "Side by side",
             "How many embedding batches are in flight at once, across accounts.", 1, 16),
)}

@dataclass(frozen=True)
class Choice:
    value: str
    label: str
    help: str


@dataclass(frozen=True)
class Config:
    name: str  # the app.settings attribute it overrides (its environment variable is the name upper-cased)
    group: str
    label: str
    help: str
    kind: str  # "choice" (a word), "bool", "int", "text" or "secret" (stored encrypted, never returned)
    choices: tuple[Choice, ...] = ()
    low: int = 0
    high: int = 0
    needs: str = ""  # what else must be set for this to do anything, in words


GROUPS = (("graphs", "Agent graphs (LangGraph)",
           ("LangGraph runs a workflow as a checkpointed graph that can pause for you and resume. Nothing here calls "
            "a model by itself: each graph drives the same workers and models as before.")),
          ("applications", "Applications", "What the application worker does with an approved job."),
          ("browser", "Browser", "The browser-use window the application worker fills forms in."),
          ("retention", "Checkpoints and tracing",
           "How long graph checkpoints are kept, and whether run metadata goes to LangSmith."))
CONFIG: dict[str, Config] = {c.name: c for c in (
    Config("agent_graphs_enabled", "graphs", "LangGraph agent graphs",
           "The switch every graph below needs. Needs PostgreSQL.", "bool",
           (Choice("false", "Off", "Every workflow runs the classic way; the graph modes and the application worker "
                                   "below do nothing."),
            Choice("true", "On", "Graph runs are allowed: the modes below decide which workflows use them."))),
    Config("qualification_graph_mode", "graphs", "Qualification",
           "Screening new jobs, recovering missing descriptions, parsing and ranking them.",
           "choice", (Choice("off", "Off", "Jobs are screened, recovered, parsed and ranked by the services, as before."),
                      Choice("shadow", "Shadow", "As before, and a few decided jobs a day are re-run through the graph "
                                                 "read-only and compared. No model call unless a paid budget is set."),
                      Choice("on", "On", "New jobs are qualified by the graph, which drives the same recovery, parse "
                                         "and ranking tasks.")),
           needs="LangGraph agent graphs on"),
    Config("preparation_graph_mode", "graphs", "Resume preparation",
           "Tailoring a qualified job's resume, checking it against your evidence and reviewing it.", "choice",
           (Choice("off", "Off", "Automatic tailoring queues the tailor worker, as before."),
            Choice("shadow", "Shadow", "As before, and a few reviewed packets a day are re-checked by the graph "
                                       "read-only. Never a model call."),
            Choice("on", "On", "Qualified jobs are prepared by the graph: tailoring, evidence checks, the review "
                               "decision, a bounded revision loop, then your approval.")),
           needs="LangGraph agent graphs on"),
    Config("resume_parse_graph_mode", "graphs", "Resume structuring",
           "Reading each uploaded resume into structured data with the Resume Parser model.", "choice",
           (Choice("off", "Off", "The same steps (model, grounding in the resume's text, the fill or comparison) run "
                                 "without a graph."),
            Choice("on", "On", "Each resume is structured by the graph, one thread per attempt.")),
           needs="LangGraph agent graphs on"),
    Config("job_insights_graph_mode", "graphs", "Job insights research",
           "Company, salary and interview research for approved jobs.", "choice",
           (Choice("off", "Off", "Company, salary and interview research runs as before."),
            Choice("shadow", "Shadow", "As before, and the graph's own checks re-run each finished research read-only."),
            Choice("on", "On", "Each research task runs as a graph on the same task.")),
           needs="LangGraph agent graphs on"),
    Config("application_execution_mode", "applications", "Application worker",
           "What happens with a job you approved. Auto submit also needs the account's switch in Settings → Auto "
           "submit.", "choice",
           (Choice("off", "Off", "Nothing runs. You apply yourself with the Apply kit."),
            Choice("observe", "Read forms", "Reads the application form and finds a verified source for every answer. "
                                            "Fills nothing: you see which questions still need an answer."),
            Choice("assist", "Fill forms, I submit", "Also fills the form. On browser-use the window stays open for "
                                                     "you to check and submit. Never submits."),
            Choice("guarded_auto", "Auto submit", "For accounts with Auto submit on: approving a job fills and submits "
                                                  "it, recorded as applied only on the site's confirmation. It stops "
                                                  "for you only when something needs you.")),
           needs="LangGraph agent graphs on, and PostgreSQL"),
    Config("application_auth_sessions_enabled", "applications", "Reuse saved sign-ins",
           "Whether a run starts signed in with a session you saved for that site (Settings → Auto submit → logins).",
           "bool", (Choice("false", "Off", "Every run starts signed out; a login wall waits for you."),
                    Choice("true", "On", "A run starts with your saved session for that site, if there is one."))),
    Config("application_login_domains", "applications", "Sites where a saved password may be filled",
           "Exact host names, separated by commas (e.g. acme.wd5.myworkdayjobs.com). A saved password is never "
           "filled anywhere else. Empty: never.", "text"),
    Config("application_takeover_ttl_minutes", "applications", "How long a window waits for you",
           "When a run needs you, the browser window stays open this long; then the run ends and waits for a retry.",
           "int", (Choice("5", "5 minutes", ""), Choice("15", "15 minutes", ""), Choice("30", "30 minutes", ""),
                   Choice("60", "1 hour", ""), Choice("120", "2 hours", "")), low=1, high=240),
    Config("application_challenge_poll_seconds", "applications", "How often a CAPTCHA window is checked",
           "After you solve a CAPTCHA in the window, the application continues by itself; the window is checked this "
           "often (a solved CAPTCHA lasts about two minutes).", "int",
           (Choice("3", "3 seconds", ""), Choice("5", "5 seconds", ""), Choice("10", "10 seconds", "")), low=2, high=30),
    Config("application_fields_settle_seconds", "applications", "When a window you filled continues by itself",
           "After you fill the highlighted fields in the window, the application continues once all of them have "
           "stopped changing for this long. You can always press Continue sooner.", "int",
           (Choice("10", "10 seconds", ""), Choice("20", "20 seconds", ""), Choice("60", "1 minute", "")),
           low=0, high=600),
    Config("application_input_hold_minutes", "applications", "How long a read form waits for your answer",
           "While a run waits for an answer you give in the Knowledge bank, its browser-use window keeps the form so "
           "it is filled in place afterwards. Past this, the form is read again.", "int",
           (Choice("15", "15 minutes", ""), Choice("60", "1 hour", ""), Choice("120", "2 hours", "")), low=1, high=240),
    Config("application_max_actions", "applications", "Most browser actions per run",
           "Fills, uploads and clicks one run may make, whoever proposes them. A run that needs more stops.", "int",
           low=10, high=300),
    Config("browser_use_webui_enabled", "browser", "browser-use backend",
           "Forms no built-in adapter knows (most boards other than Greenhouse) are filled through browser-use.",
           "bool", (Choice("false", "Off", "Only forms with a built-in adapter (Greenhouse) are filled; others wait "
                                           "for you."),
                    Choice("true", "On", "Other forms are filled through browser-use.")),
           needs="BROWSER_USE_PYTHON set in the environment"),
    Config("browser_use_headless", "browser", "Browser window",
           "Whether you can see, and take over, the window a run fills.", "bool",
           (Choice("false", "Visible", "A window opens on this computer: when a run needs you, it stays open with the "
                                       "fields highlighted for you. For local installs."),
            Choice("true", "Hidden", "No window (servers). When a run needs you, it stops and the Apply kit lists "
                                     "what is missing."))),
    Config("agent_checkpoint_retention_days", "retention", "Keep finished graph checkpoints",
           "Checkpoints are the graphs' saved progress (ids and counts, never your documents).", "int",
           (Choice("1", "1 day", ""), Choice("7", "7 days", ""), Choice("14", "14 days", ""),
            Choice("30", "30 days", ""), Choice("90", "90 days", "")), low=0, high=365),
    Config("agent_interrupt_expiry_days", "retention", "A graph waiting for you expires after",
           "A graph paused for your input or approval is ended after this long.", "int",
           (Choice("7", "7 days", ""), Choice("14", "14 days", ""), Choice("30", "30 days", ""),
            Choice("90", "90 days", "")), low=1, high=365),
    Config("agent_langsmith_export_enabled", "retention", "Send traces to LangSmith",
           "Run metadata only (ids, enums, counts, error class names); never a prompt, document or credential.", "bool",
           (Choice("false", "Off", "Nothing leaves this server."),
            Choice("true", "On", "Graph runs are traced to the LangSmith project below.")),
           needs="a LangSmith API key, below or in .env"),
    Config("agent_langsmith_api_key", "retention", "LangSmith API key",
           "Saved encrypted on this server and never shown again. Leave it empty to keep the saved key.", "secret"),
    Config("agent_langsmith_project", "retention", "LangSmith project", "The project traces go to.", "text"),
    # A text setting with choices: the regions are offered, and any other https address (self-hosted) is still allowed.
    Config("agent_langsmith_endpoint", "retention", "LangSmith region",
           "Where your LangSmith organization lives. A key works only in its own region (another region answers "
           "403 Forbidden).", "text",
           (Choice("https://api.smith.langchain.com", "US", "smith.langchain.com · api.smith.langchain.com"),
            Choice("https://eu.api.smith.langchain.com", "EU", "eu.smith.langchain.com · eu.api.smith.langchain.com"),
            Choice("https://apac.api.smith.langchain.com", "APAC",
                   "apac.smith.langchain.com · apac.api.smith.langchain.com"))),
)}

_lock = threading.Lock()
_cache: dict[str, int] = {}
_config: dict[str, Any] = {}
_cached_at = 0.0
_env: dict[str, Any] = {}  # the environment's value of each setting an override is laid over now
_overlaid: int | None = None  # id() of the settings object the overrides were laid over


def _session() -> Session:
    from app.db import SessionLocal

    return SessionLocal()


def _whole(text: Any) -> int | None:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _typed(spec: Config, text: str) -> Any:
    if spec.kind == "bool":
        return str(text).strip().lower() == "true"
    if spec.kind == "int":
        return _whole(text)
    if spec.kind == "secret":
        return decrypt_secret(text)  # None (the environment applies) when SECRET_KEY changed or is missing
    return str(text)


def _load() -> None:
    """Read the table (at most every CACHE_SECONDS) and lay the configuration over the settings object."""
    global _cache, _config, _cached_at
    with _lock:
        fresh = time.monotonic() - _cached_at < CACHE_SECONDS
    if not fresh:
        try:
            with _session() as db:
                rows = dict(db.execute(select(ServerSetting.name, ServerSetting.value)).all())
        except Exception:
            log.debug("server settings unavailable", exc_info=True)
            rows = {}
        with _lock:
            _cache = {k: n for k, v in rows.items() if k in EDITABLE and (n := _whole(v)) is not None}
            _config = {k: t for k, v in rows.items() if k in CONFIG and (t := _typed(CONFIG[k], v)) is not None}
            _cached_at = time.monotonic()
    _overlay()


def _overlay() -> None:
    """Lay the saved configuration over the settings object, and give a setting whose override was removed its
    environment value back. Only touches settings that have (or just lost) an override."""
    global _overlaid
    settings = get_settings()
    with _lock:
        if _overlaid != id(settings):  # a new settings object (a cache clear): its values are the environment's
            _env.clear()
            _overlaid = id(settings)
        for name in [n for n in _env if n not in _config]:
            setattr(settings, name, _env.pop(name))
        for name, value in _config.items():
            _env.setdefault(name, getattr(settings, name))
            if getattr(settings, name) != value:
                setattr(settings, name, value)


def apply() -> None:
    """Read the saved configuration now and lay it over the settings object (startup, after a save)."""
    reset_cache()
    _load()


def overrides() -> dict[str, int]:
    """Every saved worker-limit override, cached briefly (and the configuration laid over the settings). Never
    raises."""
    _load()
    with _lock:
        return dict(_cache)


def environment(name: str) -> Any:
    """The environment's own value of a CONFIG setting, whether or not an override is laid over it."""
    with _lock:
        return _env[name] if name in _env else getattr(get_settings(), name)


def reset_cache() -> None:
    global _cached_at
    with _lock:
        _cached_at = 0.0


def value(name: str) -> int:
    """The setting in force: the admin's override, else the environment's."""
    saved = overrides().get(name)
    return saved if saved is not None else int(getattr(get_settings(), name))


def interval_override(setting_name: str) -> int | None:
    """A scheduler interval an admin set, or None (the scheduler keeps its own)."""
    return overrides().get(setting_name) if setting_name in EDITABLE else None


def describe() -> dict[str, dict[str, Any]]:
    saved = overrides()
    settings = get_settings()
    return {name: {"worker": e.worker, "label": e.label, "help": e.help, "min": e.low, "max": e.high,
                   "value": saved.get(name, int(getattr(settings, name))), "default": int(getattr(settings, name)),
                   "overridden": name in saved}
            for name, e in EDITABLE.items()}


class ServerSettingError(ValueError):
    pass


def update_values(db: Session, changes: dict[str, Any], actor: str) -> dict[str, dict[str, Any]]:
    """Save an admin's changes. A value equal to the environment's removes the override. Commits."""
    settings = get_settings()
    clean: dict[str, int] = {}
    for name, raw in changes.items():
        spec = EDITABLE.get(name)
        if spec is None:
            raise ServerSettingError(f"'{name}' cannot be changed here")
        try:
            number = int(raw)
        except (TypeError, ValueError):
            raise ServerSettingError(f"{spec.label}: enter a whole number") from None
        if not spec.low <= number <= spec.high:
            raise ServerSettingError(f"{spec.label}: between {spec.low} and {spec.high}")
        clean[name] = number
    now = datetime.now(UTC).replace(tzinfo=None)
    for name, number in clean.items():
        db.execute(delete(ServerSetting).where(ServerSetting.name == name))
        if number != int(getattr(settings, name)):
            db.add(ServerSetting(name=name, value=str(number), updated_by=actor[:200], updated_at=now))
        job = EDITABLE[name].scheduler_job
        if job:  # a new interval applies from now, not after the old one has run out
            db.execute(update(SchedulerJob).where(SchedulerJob.name == job)
                       .values(next_run_at=None if number <= 0 else now))
    db.commit()
    reset_cache()
    return describe()


# ───────────── the agent and application configuration (Settings → AI agents & automation) ─────────────

HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
PROJECT = re.compile(r"^[\w .:-]{1,100}$")
PRESETS = (
    {"id": "manual", "label": "I apply myself", "help": "Nothing runs on its own: use the Apply kit.",
     "values": {"agent_graphs_enabled": "false", "application_execution_mode": "off"}},
    {"id": "read", "label": "Read forms and find answers",
     "help": "The worker reads each approved job's form and tells you which questions still need an answer.",
     "values": {"agent_graphs_enabled": "true", "application_execution_mode": "observe"}},
    {"id": "fill", "label": "Fill forms; I submit",
     "help": "The worker fills each form in a visible window and leaves it open for you to check and submit.",
     "values": {"agent_graphs_enabled": "true", "application_execution_mode": "assist",
                "browser_use_webui_enabled": "true", "browser_use_headless": "false"}},
    {"id": "auto", "label": "Submit applications for me",
     "help": "Approving a job submits it (with Auto submit on for your account). It stops for you only when "
             "something needs you, in a visible window.",
     "values": {"agent_graphs_enabled": "true", "application_execution_mode": "guarded_auto",
                "browser_use_webui_enabled": "true", "browser_use_headless": "false"}},
)


def _text(value: Any) -> str:
    """A configuration value in its stored form."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def describe_config() -> dict[str, Any]:
    """Every configuration setting with its choices, the value in force, the environment's and whether it is
    overridden; grouped, with the presets and what the environment alone decides (never a secret's value)."""
    from app.infrastructure.application_browser.browser_use_sidecar import configured

    _load()
    settings = get_settings()
    with _lock:
        saved = dict(_config)
    items = []
    for name, spec in CONFIG.items():
        if spec.kind == "secret":  # whether a key is set, and where; never the key
            items.append({"name": name, "env": name.upper(), "group": spec.group, "label": spec.label,
                          "help": spec.help, "kind": spec.kind, "needs": None, "min": 0, "max": 0, "choices": [],
                          "value": "", "default": "", "overridden": name in saved,
                          "set": bool(getattr(settings, name)), "env_set": bool(environment(name))})
            continue
        items.append({"name": name, "env": name.upper(), "group": spec.group, "label": spec.label, "help": spec.help,
                      "kind": spec.kind, "needs": spec.needs or None, "min": spec.low, "max": spec.high,
                      "choices": [{"value": c.value, "label": c.label, "help": c.help} for c in spec.choices],
                      "value": _text(getattr(settings, name)), "default": _text(environment(name)),
                      "overridden": name in saved})
    return {"groups": [{"id": g, "label": label, "help": text} for g, label, text in GROUPS], "settings": items,
            "presets": list(PRESETS),
            "environment": {"browser_use_python": bool(settings.browser_use_python),
                            "browser_use_ready": configured(),
                            "langsmith_api_key": bool(settings.agent_langsmith_api_key)}}


SECRET = re.compile(r"^[\x21-\x7e]{8,200}$")  # one printable token, no spaces


def _clean(spec: Config, raw: Any) -> str:
    text = _text(raw).strip()
    if spec.kind == "secret":
        if not text:
            return ""  # removes the saved key: the environment's applies
        if not SECRET.match(text):
            raise ServerSettingError(f"{spec.label}: paste the key as it is (8 to 200 characters, no spaces)")
        try:
            return encrypt_secret(text)
        except SecretsUnavailable:
            raise ServerSettingError(f"{spec.label}: the server has no SECRET_KEY, so a key cannot be stored; "
                                     f"set {spec.name.upper()} in .env instead") from None
    if spec.kind in ("choice", "bool"):
        if text.lower() not in {c.value for c in spec.choices}:
            raise ServerSettingError(f"{spec.label}: choose one of {', '.join(c.label for c in spec.choices)}")
        return text.lower()
    if spec.kind == "int":
        number = _whole(text)
        if number is None or not spec.low <= number <= spec.high:
            raise ServerSettingError(f"{spec.label}: a whole number between {spec.low} and {spec.high}")
        return str(number)
    if spec.name == "application_login_domains":
        hosts = [h.strip().lower().strip(".") for h in text.split(",") if h.strip()]
        wrong = [h for h in hosts if not HOST.match(h)]
        if wrong:
            raise ServerSettingError(f"{spec.label}: not a host name: {', '.join(wrong[:3])}")
        return ",".join(dict.fromkeys(hosts))
    if spec.name == "agent_langsmith_endpoint":
        if not re.match(r"^https://[a-z0-9.-]+(:[0-9]+)?(/[\w./-]*)?$", text, re.IGNORECASE) or len(text) > 300:
            raise ServerSettingError(f"{spec.label}: an https:// address")
        return text.rstrip("/")
    if not PROJECT.match(text):
        raise ServerSettingError(f"{spec.label}: letters, numbers, spaces, dots, dashes (up to 100)")
    return text


def update_config(db: Session, changes: dict[str, Any], actor: str) -> dict[str, Any]:
    """Save an admin's configuration changes, all or none. A value equal to the environment's removes the override;
    for a secret, an empty value removes the saved key. Takes effect at once in this process (`apply`); other
    processes pick it up within CACHE_SECONDS. Commits."""
    clean: dict[str, str] = {}
    for name, raw in changes.items():
        spec = CONFIG.get(name)
        if spec is None:
            raise ServerSettingError(f"'{name}' cannot be changed here")
        clean[name] = _clean(spec, raw)
    now = datetime.now(UTC).replace(tzinfo=None)
    for name, text in clean.items():
        db.execute(delete(ServerSetting).where(ServerSetting.name == name))
        secret = CONFIG[name].kind == "secret"
        if text if secret else text != _text(environment(name)):
            db.add(ServerSetting(name=name, value=text, updated_by=actor[:200], updated_at=now))
    db.commit()
    apply()
    return describe_config()
