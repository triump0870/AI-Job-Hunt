"""Job Hunt OS submits an application only through the guarded path (Phase 6), and this file proves nothing else can.

Guarantees, each tested here:
  1. no code under app/ makes a page receive a click, a key press, a tap, a synthetic event or a mouse/keyboard
     action, except `app/infrastructure/application_browser/guarded_submit.py` (one `click`, inside
     GuardedPage.submit, behind a SubmitTicket); the application modules also never evaluate scripts, and the only
     `.submit(...)` call is the guarded SUBMIT node's `page.submit(ticket)`;
  2. `ApplicationPage`, what Phase 5 application code can do to a page, still has no such method;
  3. an execution can never be moved into a submit stage (SUBMITTING, CONFIRMING, CONFIRMED) without its own
     SubmissionIntent in the right status, by `set_stage` or by setting the column directly;
  4. only app.services.submission may sign a SUBMIT action for the browser-use runner (`signed_submit`, which only
     the guarded SUBMIT node calls, after its CLICKING commit), and the runner clicks a submit-like control only in
     its signed handler and evaluates only its own constant scripts;
  5. while Job Hunt OS drives a page, the application browsers abort every non-GET request except a file upload and
     a ticket's click (tests/test_application_browser.py, tests/test_guarded_submission.py and
     tests/test_browser_use_sidecar.py, with real Chromium).
"""
import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.infrastructure.application_browser import ApplicationPage
from app.infrastructure.application_browser.ephemeral import _Page
from app.models import ApplicationExecutionTask
from app.services import application_execution
from app.services.ats_forms import SubmitControl, greenhouse

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
RUNNER = ROOT / "sidecars" / "browser_use_runner" / "runner.py"
# Methods that act on a page as a person would to submit something. Banned everywhere in app/ ...
INPUT_ACTIONS = {"click", "dblclick", "tap", "press", "press_sequentially", "dispatch_event", "drag_to", "hover",
                 "request_submit", "keyboard", "mouse", "touchscreen"}
# ... except these, each in exactly one module.
ALLOWED = {"click": {"app/infrastructure/application_browser/guarded_submit.py"},
           "submit": {"app/agents/application.py"}}
# In the application modules, also no script evaluation and nothing named submit.
APPLICATION_ONLY = {"evaluate", "evaluate_handle", "eval_on_selector", "eval_on_selector_all", "add_init_script",
                    "expose_function", "submit", "type"}
APPLICATION_MODULES = ("app/infrastructure/application_browser", "app/services/ats_forms",
                       "app/agents/application.py", "app/services/application_execution.py",
                       "app/services/answer_resolver.py", "app/services/submission.py",
                       "app/services/browser_actions.py", "app/services/application_backends.py")


def page_actions(source: str, banned: set[str]) -> list[str]:
    """Calls or attribute reads of a banned name (`page.click(...)`, `page.keyboard.press`)."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr in banned:
            found.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "requestSubmit" in node.value:
            found.append("requestSubmit")
    return found


def _relative(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _application_file(path: Path) -> bool:
    relative = _relative(path)
    return any(relative == m or relative.startswith(m + "/") for m in APPLICATION_MODULES)


def test_only_the_guarded_page_clicks_and_only_the_guarded_node_submits():
    offenders = {}
    for path in sorted(APP.rglob("*.py")):
        if "migrations" in path.parts:
            continue
        banned = INPUT_ACTIONS | (APPLICATION_ONLY if _application_file(path) else set())
        hits = {hit for hit in page_actions(path.read_text(), banned) if _relative(path) not in ALLOWED.get(hit, ())}
        if hits:
            offenders[_relative(path)] = sorted(hits)
    assert offenders == {}, f"These modules can act on a page the way a submit would: {offenders}"


def test_the_allowed_click_and_submit_are_one_call_each():
    guarded = (ROOT / "app/infrastructure/application_browser/guarded_submit.py").read_text()
    assert page_actions(guarded, {"click"}) == ["click"]
    tree = ast.parse(guarded)
    submit = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "submit")
    assert [a for a in ast.walk(submit) if isinstance(a, ast.Attribute) and a.attr == "click"], \
        "the click lives inside GuardedPage.submit"
    assert submit.args.args[1].arg == "ticket"
    graph = (ROOT / "app/agents/application.py").read_text()
    calls = [n for n in ast.walk(ast.parse(graph)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "submit"]
    assert len(calls) == 1 and isinstance(calls[0].args[0], ast.Name) and calls[0].args[0].id == "ticket"


def test_every_application_module_is_scanned():
    for module in APPLICATION_MODULES:
        assert (ROOT / module).exists(), f"{module} is listed as an application module but does not exist"


def test_the_guard_detects_what_it_bans():
    assert page_actions("await page.locator('#submit_app').click()", INPUT_ACTIONS) == ["click"]
    assert page_actions("await page.keyboard.press('Enter')", INPUT_ACTIONS) == ["press", "keyboard"]
    assert page_actions("await page.evaluate('f.requestSubmit()')", APPLICATION_ONLY) == ["evaluate",
                                                                                          "requestSubmit"]
    assert page_actions("await page.locator('#email').fill('a@b.test')", INPUT_ACTIONS | APPLICATION_ONLY) == []


def test_only_the_submission_service_signs_a_submit():
    signers = {}
    for path in sorted(APP.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "sign_action":
                kinds = [a.value for a in node.args if isinstance(a, ast.Constant)]
                signers.setdefault(_relative(path), set()).update(kinds or {"<dynamic>"})
    assert signers == {"app/services/application_backends.py": {"LOGIN"}, "app/services/submission.py": {"SUBMIT"}}
    graph = ast.parse((ROOT / "app/agents/application.py").read_text())
    callers = [n for n in ast.walk(graph) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "signed_submit"]
    assert len(callers) == 1, "one signed submit, in the guarded SUBMIT node"
    source = (ROOT / "app/agents/application.py").read_text()
    body = source[source.index("def _submit_browser_use("):]
    assert body.index("submission.commit_clicking(") < body.index("submission.still_clicking(") \
        < body.index("submission.signed_submit("), "the token is signed only after CLICKING and its re-check"


def test_the_runner_clicks_submit_controls_only_when_signed_and_runs_only_its_own_scripts():
    tree = ast.parse(RUNNER.read_text())
    clicking: set[str] = set()
    for function in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]:
        for node in ast.walk(function):
            if isinstance(node, ast.Attribute) and node.attr in ("_click_element_node", "click"):
                clicking.add(function.name)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "evaluate":
                script = node.args[0] if node.args else None
                assert isinstance(script, ast.Name) and script.id.isupper(), \
                    f"{function.name} evaluates something other than a constant script"
    assert clicking == {"click", "_signed_click", "choose"}
    source = RUNNER.read_text()
    choose = source[source.index("    async def choose("):source.index("    async def scroll(")]
    # `choose` clicks only the dropdown itself (after refusing anything submit-like) and one of its role=option items.
    assert choose.index('element.get("role") != "combobox" or element.get("submit_like")') < choose.index(".click(")
    assert choose.count(".click(") == 2 and "'[role=\"option\"]:visible'" in choose
    click = source[source.index("    async def click("):source.index("    async def scroll(")]
    assert 'element.get("submit_like")' in click and "SUBMIT_REFUSED" in click
    signed = source[source.index("    async def _signed_click("):source.index("    async def submit(")]
    assert signed.index("self._verify(") < signed.index(".click(")
    assert "_click_element_node" not in signed, "browser-use's helper clicks again by script when its wait fails"
    assert signed.count("handle.click(timeout=") == 1 and signed.count("handle.evaluate(CLICK_JS)") == 1
    assert signed.index("reachable = False") < signed.index("handle.click(timeout="), "one or the other, never both"
    (launch,) = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "launch"]
    assert [k.arg for k in launch.keywords] == ["headless"], "plain Chromium: no extra flags, no debugging port"
    assert 'os.environ["ANONYMIZED_TELEMETRY"] = "false"' in source


def test_an_application_page_has_no_way_to_submit():
    public = {name for name, _ in inspect.getmembers(ApplicationPage) if not name.startswith("_")}
    assert public == {"goto", "html", "fill", "select", "check", "upload", "value_of", "is_checked",
                      "blocked_requests"}
    implemented = {name for name in vars(_Page) if not name.startswith("_")}
    assert implemented <= public | {"install"}
    assert not any(word in name for name in public | implemented for word in ("click", "press", "submit", "eval"))


def test_a_form_records_its_submit_control_without_a_locator():
    assert set(SubmitControl.model_fields) == {"present", "label"}
    assert not hasattr(greenhouse, "submit")


@pytest.mark.parametrize("stage", ["SUBMITTING", "CONFIRMING", "CONFIRMED"])
def test_no_execution_reaches_a_submit_stage_without_its_intent(stage):
    task = ApplicationExecutionTask(id=7, stage="FILLING", mode="GUARDED_AUTO")
    with pytest.raises(application_execution.SubmitForbidden):
        application_execution.set_stage(task, stage)
    with pytest.raises(application_execution.SubmitForbidden):
        task.stage = stage  # the column itself refuses it, whoever sets it
    other = SimpleNamespace(execution_task_id=8, status="CLICKING" if stage != "CONFIRMED" else "CONFIRMED")
    with pytest.raises(application_execution.SubmitForbidden):
        application_execution.set_stage(task, stage, intent=other)  # another execution's intent
    wrong = SimpleNamespace(execution_task_id=7, status="ARMED")
    with pytest.raises(application_execution.SubmitForbidden):
        application_execution.set_stage(task, stage, intent=wrong)  # not yet clicking
    assisted = ApplicationExecutionTask(id=7, stage="FILLING", mode="ASSIST")
    own = SimpleNamespace(execution_task_id=7, status="CLICKING" if stage != "CONFIRMED" else "CONFIRMED")
    with pytest.raises(application_execution.SubmitForbidden):
        application_execution.set_stage(assisted, stage, intent=own)  # only a guarded run
    assert task.stage == "FILLING"
    application_execution.set_stage(task, stage, intent=own)
    assert task.stage == stage
    application_execution.set_stage(task, "READY_TO_SUBMIT")
