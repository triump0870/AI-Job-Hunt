"""Settings → AI Models: connection, discovery, seeding, draft edits, validation, preview/apply, versions, rollback."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import (
    AIConfigAuditEntry,
    AIConfigVersion,
    AIModel,
    LiteLLMConnection,
    WorkerModelConfig,
)
from app.services import ai_model_config as config
from app.services import model_resolution
from app.services.ai_workers import WORKERS
from app.services.model_resolution import resolve, resolve_model
from app.settings import get_settings
from tests.litellm_fake import MASTER_KEY, Account, deployment, embedding_deployment


def snapshot(**workers):
    return {"models": {"fast": {"enabled": True}, "mid": {"enabled": True}, "deep": {"enabled": True},
                       "off": {"enabled": False}, "auto": {"enabled": True}},
            "tiers": {"FAST": {"model": "fast", "enabled": True}, "PRIMARY_REASONING": {"model": "mid", "enabled": True},
                      "DEEP_REVIEW": {"model": "deep", "enabled": True}, "EMPTY": {"model": None, "enabled": True}},
            "routing": {"auto_router_enabled": True, "auto_router_model_name": "auto", "classifier": "HEURISTIC",
                        "tier_models": {"SIMPLE": "fast"}},
            "workers": workers}


# ───────────── hosting: the Local / Cloud tag ─────────────
@pytest.mark.parametrize(("name", "provider", "underlying", "params", "expected"), [
    ("ollama_chat/qwen2.5:14b", "ollama_chat", "qwen2.5:14b", None, "LOCAL"),
    ("phi4:latest", "ollama", "phi4:latest", None, "LOCAL"),
    ("beam:cloud", "ollama", "beam:cloud", None, "CLOUD"),  # Ollama's provider, but it runs on Ollama's servers
    ("nemotron-3-nano:30b-cloud-Ro", "ollama", "nemotron-3-nano:30b-cloud", None, "CLOUD"),  # an alias of a hosted model
    ("gpt-oss:20b-cloud", None, "gpt-oss:20b-cloud", None, "CLOUD"),
    ("groq/openai/gpt-oss-120b", "groq", "openai/gpt-oss-120b", None, "CLOUD"),
    ("poolside/laguna-s-2.1:free", "poolside", "laguna-s-2.1:free", None, "CLOUD"),
    ("cloudy-local", "ollama", "cloudy:7b", None, "LOCAL"),  # "cloud" in a name is not the hosted-model suffix
    ("Groq", "litellm", "complexity_router", None, None),  # a router's tiers can mix both
    ("gpt-oss:20b", None, "gpt-oss:20b", None, None),  # nobody reported a provider: unknown, not guessed
    ("vllm-box", "hosted_vllm", "m", None, None),  # could be anywhere
    ("managed-here", "ollama_chat", "m", {"api_base": "http://localhost:11434"}, "LOCAL"),
    ("managed-lan", "ollama_chat", "m", {"api_base": "http://192.168.1.20:11434"}, "LOCAL"),
    ("managed-away", "ollama_chat", "m", {"api_base": "https://ollama.example.com"}, "CLOUD"),
    ("managed-relay", "ollama_chat", "m:cloud", {"api_base": "http://localhost:11434"}, "CLOUD"),  # local daemon relays it
    ("local-cloud", "ollama_chat", "qwen2.5:14b", {"api_base": "http://localhost:11434"}, "LOCAL"),  # only the alias says cloud
    ("my-cloud", "ollama", "qwen2.5:14b", None, "LOCAL"),
    ("studio", "lm_studio", "m-cloud", None, "LOCAL"),  # Ollama's naming says nothing about another engine
    ("broken", "ollama_chat", "m", {"api_base": "http://[::1"}, None),  # unreadable: unknown, and it must not raise
    ("hostless", "ollama_chat", "m", {"api_base": "http://"}, None),
])
def test_model_hosting_says_where_a_model_runs(name, provider, underlying, params, expected):
    row = AIModel(litellm_model_name=name, provider=provider, underlying_model=underlying, model_type="CUSTOM",
                  managed=params is not None, deployment_params=params)
    assert config.model_hosting(row) == expected


def test_a_router_type_has_no_hosting_whatever_its_provider():
    assert config.model_hosting(AIModel(litellm_model_name="r", provider="groq", model_type="AUTO_ROUTER")) is None


def test_hosting_is_shown_but_is_not_part_of_the_versioned_snapshot():
    row = AIModel(litellm_model_name="m", provider="ollama", underlying_model="m", model_type="CUSTOM", managed=False)
    assert "hosting" not in config.model_snapshot(row)  # a derived label must never read as a pending config change


def _hosted(name, provider, **fields):
    return AIModel(litellm_model_name=name, provider=provider, underlying_model=name, model_type="CUSTOM", managed=False,
                   **fields)


@pytest.mark.parametrize(("members", "expected"), [
    (["local-a", "local-b"], "LOCAL"),
    (["cloud-a", "groq/b"], "CLOUD"),
    (["local-a", "cloud-a"], "MIXED"),
    (["local-a", "cloud-a", "unknown"], "MIXED"),  # both kinds are known to be in there, whatever the other one is
    (["cloud-a", "unknown"], None),  # could still turn out mixed: not called all-cloud
    (["cloud-a", "not-in-the-catalog"], None),
    (["cloud-a", "groq/not-listed"], "CLOUD"),  # no catalog row, but the name states its provider
    (["local-a", "groq/not-listed"], "MIXED"),
    (["local-a", "ollama_chat/not-listed"], "LOCAL"),
    (None, None),  # not synced since the column arrived
])
def test_a_router_is_judged_by_the_models_it_routes_to(members, expected):
    rows = [_hosted("local-a", "ollama"), _hosted("local-b", "ollama_chat"), _hosted("cloud-a:cloud", "ollama"),
            _hosted("groq/b", "groq"), _hosted("unknown", None),
            AIModel(litellm_model_name="R", provider="litellm", model_type="AUTO_ROUTER", managed=False,
                    router_models=[{"cloud-a": "cloud-a:cloud"}.get(m, m) for m in members] if members else None)]
    assert config.catalog_hosting(rows)["R"] == expected


def test_a_removed_model_still_counts_for_the_router_that_routes_to_it():
    rows = [_hosted("gone", "groq", removed_at=config.utcnow()),
            AIModel(litellm_model_name="R", provider="litellm", model_type="AUTO_ROUTER", router_models=["gone"])]
    assert config.catalog_hosting(rows)["R"] == "CLOUD"


def test_a_cloud_classifier_makes_a_router_of_local_models_mixed(account, litellm):
    litellm.deployments += [
        deployment("Classified", "auto_router/complexity_router", complexity_router_config={
            "tiers": {"SIMPLE": ["plain:20b"]}, "classifier_llm_config": {"model": "groq/judge"}}),
        deployment("plain:20b", "plain:20b", custom_llm_provider="ollama_chat"),
        deployment("groq/judge", "groq/judge")]
    account.call("POST", "/models/sync")
    assert {m["name"]: m for m in account.get("/models")["models"]}["Classified"]["hosting"] == "MIXED"


def test_an_unreadable_api_base_does_not_break_the_configuration_page(account):
    account.call("POST", "/models/sync")
    with SessionLocal() as db:
        db.add(AIModel(user_id=account.user_id, litellm_model_name="broken", display_name="broken", managed=True,
                       provider="ollama_chat", underlying_model="m", model_type="CUSTOM", source="USER",
                       deployment_params={"model": "ollama_chat/m", "api_base": "http://[::1"}))
        db.commit()
    assert {m["name"]: m for m in account.get("/models")["models"]}["broken"]["hosting"] is None


def test_sync_records_router_members_and_the_provider_a_deployment_states(account, litellm):
    litellm.deployments += [
        deployment("Mixed", "auto_router/complexity_router", complexity_router_default_model="plain:20b",
                   complexity_router_config={"tiers": {"SIMPLE": ["plain:20b"], "COMPLEX": "hosted:cloud"}}),
        deployment("plain:20b", "plain:20b", custom_llm_provider="ollama_chat"),
        deployment("hosted:cloud", "hosted:cloud", custom_llm_provider="ollama_chat")]
    account.call("POST", "/models/sync")
    models = {m["name"]: m for m in account.get("/models")["models"]}
    assert models["plain:20b"]["provider"] == "ollama_chat" and models["plain:20b"]["hosting"] == "LOCAL"
    assert models["hosted:cloud"]["hosting"] == "CLOUD"
    assert models["Mixed"]["hosting"] == "MIXED"
    assert "router_models" not in models["Mixed"]  # read for the tag, not part of the versioned snapshot


# ───────────── resolution: FIXED, TIER, AUTO_ROUTER ─────────────

def test_fixed_tier_and_auto_router_modes_resolve_to_litellm_model_names():
    snap = snapshot(RANKER={"mode": "FIXED", "primary_model": "deep"}, TAILOR={"mode": "TIER", "tier": "FAST"},
                    INBOX_CLASSIFIER={"mode": "AUTO_ROUTER", "tier": "FAST"})
    assert resolve_model(snap, "RANKER") == ("deep", "deep", None)
    assert resolve_model(snap, "TAILOR") == ("fast", "FAST", None)
    assert resolve_model(snap, "INBOX_CLASSIFIER") == ("auto", "AUTO_ROUTER", None)
    snap["routing"]["auto_router_enabled"] = False  # the router off: the worker's tier answers instead
    model, configured, note = resolve_model(snap, "INBOX_CLASSIFIER")
    assert (model, configured) == ("fast", "AUTO_ROUTER→FAST") and "auto router is off" in note


def test_unusable_assignments_resolve_to_nothing_with_a_reason(monkeypatch):
    snap = snapshot(RANKER={"mode": "FIXED", "primary_model": "off"}, TAILOR={"mode": "TIER", "tier": "EMPTY"},
                    REVIEWER={"mode": "TIER", "tier": "MISSING"})
    assert resolve_model(snap, "RANKER")[0] is None and "disabled" in resolve_model(snap, "RANKER")[2]
    assert resolve_model(snap, "TAILOR")[2] == "tier EMPTY has no model"
    assert resolve_model(snap, "REVIEWER")[2] == "tier MISSING does not exist"
    monkeypatch.setattr(get_settings(), "litellm_default_model", "env-default")
    worker = resolve(snap, "TAILOR")
    assert worker.model == "env-default" and "LITELLM_DEFAULT_MODEL" in worker.note


# ───────────── connection ─────────────

def test_connection_test_reports_reachability_authentication_discovery_and_latency(account):
    out = account.call("POST", "/test-connection")
    assert out["status"] == "CONNECTED" and out["label"] == "Connected"
    assert out["reachable"] and out["authenticated"] and out["discovery"] and out["models"] == 4
    assert out["litellm_version"] == "1.102.1" and out["latency_ms"] is not None
    assert account.get("")["connection"]["status"] == "CONNECTED"


def test_connection_failures_are_classified(account, litellm):
    account.call("PUT", "/connection", json={"api_key": "sk-wrong-key-000000"})
    assert account.call("POST", "/test-connection")["label"] == "Authentication failed"
    litellm.down = True
    assert account.call("POST", "/test-connection")["label"] == "LiteLLM unavailable"
    account.call("PUT", "/connection", json={"base_url": "not a url"}, expect=422)


def test_the_litellm_key_is_encrypted_and_never_returned(account):
    body = json.dumps(account.get(""))
    assert MASTER_KEY not in body and account.get("")["connection"]["api_key_configured"] is True
    assert account.get("")["connection"]["api_key_hint"] == "…3456"
    with SessionLocal() as db:
        row = db.scalar(select(LiteLLMConnection).where(LiteLLMConnection.user_id == account.user_id))
        assert row.api_key_enc and MASTER_KEY not in row.api_key_enc
        audit = db.scalars(select(AIConfigAuditEntry).where(AIConfigAuditEntry.user_id == account.user_id)).all()
        assert MASTER_KEY not in json.dumps([a.changes for a in audit])


# ───────────── discovery and the registry ─────────────

def test_discovery_records_reported_facts_and_leaves_the_rest_unknown(account):
    out = account.call("POST", "/models/sync")
    assert out["models"] == 4 and set(out["added"]) == {"cloud-mini"}  # the tiers were seeded from .env
    models = {m["name"]: m for m in account.get("/models")["models"]}
    fast = models["tier-1-fast"]
    assert (fast["provider"], fast["underlying_model"]) == ("ollama_chat", "llama3.2:3b")
    assert fast["context_window"] == 8192 and fast["max_context_window"] == 131072  # num_ctx vs Ollama's maximum
    assert fast["supports_tools"] is True and fast["supports_reasoning"] is False  # Ollama reported capabilities
    assert fast["supports_structured_output"] is True  # Ollama constrains decoding to JSON
    assert models["tier-2-reasoning"]["supports_reasoning"] is True
    assert models["tier-3-review"]["max_context_window"] == 32768
    cloud = models["cloud-mini"]
    assert cloud["supports_structured_output"] is True and cloud["context_window"] == 128000
    assert cloud["supports_reasoning"] is None and cloud["supports_vision"] is None  # not reported: unknown
    assert "RANKER" in fast["used_by"] or "INBOX_CLASSIFIER" in fast["used_by"]


def test_a_model_that_leaves_litellm_is_marked_not_present(account, litellm):
    account.call("POST", "/models/sync")
    litellm.deployments = [d for d in litellm.deployments if d["model_name"] != "cloud-mini"]
    out = account.call("POST", "/models/sync")
    assert out["missing"] == ["cloud-mini"]
    assert {m["name"]: m for m in account.get("/models")["models"]}["cloud-mini"]["present_in_litellm"] is False


# ───────────── seeding from existing settings ─────────────

def test_seeding_imports_existing_stage_models_in_gateway_mode(anonymous, litellm, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "litellm")
    monkeypatch.setattr(settings, "rank_model", "legacy-rank")
    monkeypatch.setattr(settings, "review_model", "legacy-review")
    client = TestClient(app)
    user = client.post("/api/auth/signup", json={"email": f"seed-{uuid.uuid4().hex}@example.com",
                                                 "password": "a password for tests",
                                                 "confirm_password": "a password for tests"}).json()["user"]
    data = client.get("/api/settings/ai").json()
    workers = {w["worker_type"]: w["config"] for w in data["workers"]}
    assert workers["RANKER"]["mode"] == "FIXED" and workers["RANKER"]["primary_model"] == "legacy-rank"
    assert workers["REVIEWER"]["primary_model"] == "legacy-review"
    assert workers["TAILOR"]["mode"] == "TIER" and workers["TAILOR"]["tier"] == "PRIMARY_REASONING"
    assert workers["INBOX_CLASSIFIER"]["mode"] == "AUTO_ROUTER" and workers["INBOX_CLASSIFIER"]["tier"] == "FAST"
    assert data["tiers"]["FAST"]["model"] == "tier-1-fast"
    with SessionLocal() as db:
        version = db.scalar(select(AIConfigVersion).where(AIConfigVersion.user_id == user["id"]))
        assert version.version_number == 1 and version.source == "MIGRATION"
        assert {c["path"] for c in version.changes} >= {"workers.RANKER.primary_model", "tiers.FAST.model"}
        assert "legacy-rank" in version.change_summary


def test_seeding_in_direct_mode_keeps_the_existing_stage_models_working(anonymous, litellm, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "openrouter")
    monkeypatch.setattr(get_settings(), "rank_model", "vendor/rank:free")
    client = TestClient(app)
    user = client.post("/api/auth/signup", json={"email": f"direct-{uuid.uuid4().hex}@example.com",
                                                 "password": "a password for tests",
                                                 "confirm_password": "a password for tests"}).json()["user"]
    workers = {w["worker_type"]: w["config"] for w in client.get("/api/settings/ai").json()["workers"]}
    assert workers["RANKER"]["mode"] == "TIER"  # an OpenRouter model name is not a LiteLLM alias
    from app.services.llm import LLMClient

    with SessionLocal() as db:
        db.info["user_id"] = user["id"]
        assert LLMClient.for_user(db).model_for("rank") == ("openrouter", "vendor/rank:free")


def test_seeding_twice_is_harmless(account):
    with SessionLocal() as db:
        assert config.ensure_seeded(db, account.user_id) is True
        assert config.ensure_seeded(db, account.user_id) is False
        count = len(db.scalars(select(WorkerModelConfig).where(WorkerModelConfig.user_id == account.user_id)).all())
        assert count == len(WORKERS)


# ───────────── drafts, validation, preview, apply ─────────────

def test_edits_change_the_draft_but_not_the_active_configuration(account):
    account.call("POST", "/models/sync")
    account.apply()
    before = account.get("")["active_version"]
    account.worker("REVIEWER", model_mode="FIXED", primary_model="tier-2-reasoning")
    data = account.get("")
    assert data["active_version"] == before
    assert data["draft"]["workers"]["REVIEWER"]["primary_model"] == "tier-2-reasoning"
    assert data["active"]["workers"]["REVIEWER"]["mode"] == "TIER"
    assert resolve_model(data["active"], "REVIEWER")[0] == "tier-3-review"  # workers still use the active one


def test_preview_lists_changes_validation_and_the_sync_plan(account):
    account.call("POST", "/models/sync")
    account.apply()
    account.worker("REVIEWER", model_mode="FIXED", primary_model="tier-2-reasoning")
    account.worker("RANKER", context_window=16384)
    preview = account.call("POST", "/preview")
    labels = {c["label"]: c for c in preview["changes"]}
    assert labels["Reviewer · effective model"]["before"] == "tier-3-review"
    assert labels["Reviewer · effective model"]["after"] == "tier-2-reasoning"
    assert labels["Ranker · context window"]["after"] == 16384
    assert preview["can_apply"] and preview["sync_plan"]["needs_sync"] is False
    assert any("Reviewer and Tailor use the same model" in w["message"] for w in preview["validation"]["warnings"])


def test_apply_creates_a_version_audit_entry_and_takes_effect(account):
    account.call("POST", "/models/sync")
    account.apply()
    account.worker("REVIEWER", model_mode="FIXED", primary_model="tier-2-reasoning")
    out = account.apply()
    assert out["status"] == "SYNCED" and out["version"] == 3
    versions = account.get("/versions")["versions"]
    assert [v["version"] for v in versions][:2] == [3, 2]
    assert versions[0]["changed_by"] == account.email and versions[0]["source"] == "USER"
    assert "Reviewer · effective model: tier-3-review → tier-2-reasoning" in versions[0]["summary"]
    assert model_resolution.get(account.user_id, "REVIEWER").model == "tier-2-reasoning"
    actions = [(a["action"], a["result"]) for a in account.get("/audit")["entries"]]
    assert ("APPLY", "SUCCEEDED") in actions
    assert account.apply()["status"] == "UNCHANGED"


@pytest.mark.parametrize("changes, message", [
    ({"model_mode": "FIXED", "primary_model": "no-such-model"}, "does not exist in LiteLLM"),
    ({"fallback_model": "tier-2-reasoning"}, "cannot be the primary model"),
    ({"context_window": 30000}, "exceeds the 24576-token context"),
    ({"model_mode": "FIXED", "primary_model": "tier-3-review", "reasoning_effort": "high"},
     "does not support reasoning effort"),
    ({"escalation": {"tier": "PRIMARY_REASONING", "conditions": ["LOW_CONFIDENCE"], "confidence_threshold": 0.8}},
     "same model"),
    ({"escalation": {"tier": "DEEP_REVIEW", "conditions": ["LOW_CONFIDENCE"]}}, "needs a confidence threshold"),
])
def test_invalid_configurations_are_rejected_before_anything_changes(account, changes, message):
    account.call("POST", "/models/sync")
    account.apply()
    account.worker("RANKER", **changes)
    preview = account.call("POST", "/preview")
    assert not preview["can_apply"]
    assert any(message in e["message"] and e["subject"] == "Ranker" for e in preview["validation"]["errors"]), \
        preview["validation"]["errors"]
    rejected = account.apply(expect=422)
    assert rejected["detail"]["validation"]["errors"]
    assert account.get("")["active_version"] == 2
    assert ("APPLY", "REJECTED") in [(a["action"], a["result"]) for a in account.get("/audit")["entries"]]


def test_a_prompt_token_budget_is_versioned_and_resolved(account):
    account.call("POST", "/models/sync")
    account.apply()
    account.worker("RANKER", prompt_token_budget=7000)
    account.worker("REVIEWER", prompt_token_budget=7000)
    preview = account.call("POST", "/preview")
    labels = {c["label"]: c for c in preview["changes"]}
    assert labels["Ranker · prompt token budget"]["before"] is None
    assert labels["Ranker · prompt token budget"]["after"] == 7000
    assert any("Reviewer does not trim its prompt" in w["message"] for w in preview["validation"]["warnings"])
    assert model_resolution.get(account.user_id, "RANKER").prompt_token_budget is None  # the draft is not live
    assert account.apply()["status"] == "SYNCED"
    assert model_resolution.get(account.user_id, "RANKER").prompt_token_budget == 7000
    workers = {w["worker_type"]: w for w in account.get("")["workers"]}
    assert workers["RANKER"]["trims_prompt"] and not workers["REVIEWER"]["trims_prompt"]
    assert workers["RANKER"]["config"]["prompt_token_budget"] == 7000


@pytest.mark.parametrize("changes", [{"temperature": 3}, {"max_output_tokens": 0}, {"retry_count": 50},
                                     {"prompt_token_budget": 100},
                                     {"model_mode": "SOMETIMES"}, {"reasoning_effort": "extreme"},
                                     {"escalation": {"conditions": ["WHENEVER"]}}])
def test_out_of_range_values_are_refused_at_edit_time(account, changes):
    response = account.client.put("/api/settings/ai/workers/RANKER", json=changes)
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("worker_type", sorted(WORKERS))
def test_a_litellm_model_name_with_spaces_can_be_assigned_to_every_worker(account, litellm, worker_type):
    chat = {"max_input_tokens": 128000, "supports_response_schema": True}
    if WORKERS[worker_type].capability == "EMBEDDING":
        litellm.deployments.append(embedding_deployment("Embed Small v3", "openai/text-embedding-3-small", 16))
        changes = {"primary_model": "Embed Small v3", "enabled": True}
    else:
        for name in ("Groq B4", "Groq B4 backup", "Groq B4 second"):
            litellm.deployments.append(deployment(name, f"groq/{name.replace(' ', '-').lower()}", info=chat))
        changes = {"primary_model": "Groq B4", "fallback_model": "Groq B4 backup",
                   "secondary_models": ["Groq B4 second"]}
    account.call("POST", "/models/sync")
    account.apply()
    account.worker(worker_type, model_mode="FIXED", **changes)
    preview = account.call("POST", "/preview")
    assert preview["can_apply"], preview["validation"]["errors"]
    account.apply()
    resolved = model_resolution.get(account.user_id, worker_type)
    assert resolved.model == changes["primary_model"]
    assert resolved.fallback_model == changes.get("fallback_model")


def test_tiers_model_fallbacks_and_the_auto_router_accept_model_names_with_spaces(account, litellm):
    chat = {"max_input_tokens": 128000, "supports_response_schema": True}
    litellm.deployments += [deployment("Groq B4", "groq/b4", info=chat), deployment("Groq B5", "groq/b5", info=chat),
                            deployment("Auto Router", "auto_router/auto_router", info=chat)]
    account.call("POST", "/models/sync")
    account.apply()
    account.call("PUT", "/tiers/FAST", json={"revision": account.revision(), "litellm_model_name": "Groq B4"})
    account.call("PUT", "/models/Groq B4", json={"revision": account.revision(), "fallback_models": ["Groq B5"]})
    account.call("PUT", "/routing", json={"revision": account.revision(), "auto_router_model_name": "Auto Router",
                                          "tier_models": {"SIMPLE": "Groq B4"}})
    errors = account.call("POST", "/preview")["validation"]["errors"]
    assert not [e for e in errors if "valid" in e["message"] and "name" in e["message"]], errors
    active = account.get("")["draft"]
    assert active["tiers"]["FAST"]["model"] == "Groq B4"
    assert active["models"]["Groq B4"]["fallback_models"] == ["Groq B5"]
    assert active["routing"]["auto_router_model_name"] == "Auto Router"


def test_a_model_name_with_other_characters_is_still_refused(account):
    for bad in ("Groq\nB4", "Groq\tB4", "x;y", "-leading"):
        response = account.client.put("/api/settings/ai/workers/JD_PARSER",
                                      json={"model_mode": "FIXED", "primary_model": bad})
        assert response.status_code == 422, (bad, response.text)


def test_a_circular_fallback_through_litellm_router_settings_is_rejected(account, litellm):
    litellm.router_settings["fallbacks"] = [{"tier-1-fast": ["tier-2-reasoning"]}]
    account.call("POST", "/models/sync")
    account.worker("RANKER", fallback_model="tier-1-fast")  # tier-2 → tier-1 → tier-2
    errors = account.call("POST", "/preview")["validation"]["errors"]
    assert any("Circular fallback: tier-2-reasoning → tier-1-fast → tier-2-reasoning" in e["message"] for e in errors)


def test_structured_output_requirements_warn_or_block(account, litellm):
    litellm.deployments.append(deployment("no-json", "custom/raw-model"))
    account.call("POST", "/models/sync")
    account.worker("REVIEWER", model_mode="FIXED", primary_model="no-json")
    warnings = account.call("POST", "/preview")["validation"]["warnings"]
    assert any("Structured Output Required" in w["message"] and w["subject"] == "Reviewer"
               for w in warnings)  # unknown: warn, allow, and say which worker
    account.call("PUT", "/models/no-json", json={"overrides": {"supports_structured_output": False}})
    errors = account.call("POST", "/preview")["validation"]["errors"]
    assert any("cannot return structured output" in e["message"] for e in errors)  # known lack: block


def test_small_context_models_warn_for_demanding_workers(account, litellm):
    litellm.deployments.append(deployment("tiny", "ollama_chat/llama3.2:3b", api_base="http://ollama.test:11434",
                                          num_ctx=4096))
    account.call("POST", "/models/sync")
    account.worker("REVIEWER", model_mode="FIXED", primary_model="tiny")
    warnings = account.call("POST", "/preview")["validation"]["warnings"]
    assert any("may not satisfy Reviewer requirements" in w["message"] for w in warnings)


# ───────────── concurrency ─────────────

def test_an_edit_based_on_a_stale_revision_is_refused(account):
    revision = account.revision()
    account.worker("RANKER", temperature=0.2)
    response = account.client.put("/api/settings/ai/workers/TAILOR", json={"revision": revision, "temperature": 0.5})
    assert response.status_code == 409 and "changed the AI configuration" in response.json()["detail"]["message"]


def test_apply_refuses_a_draft_edited_after_its_preview(account):
    account.call("POST", "/models/sync")
    preview = account.call("POST", "/preview")
    account.worker("RANKER", temperature=0.3)
    response = account.client.post("/api/settings/ai/apply", json={"revision": preview["draft_revision"],
                                                                   "base_version": preview["base_version"]})
    assert response.status_code == 409


def test_a_second_apply_waits_while_one_is_running(account):
    account.call("POST", "/models/sync")
    with SessionLocal() as db:
        row = db.scalar(select(LiteLLMConnection).where(LiteLLMConnection.user_id == account.user_id))
        row.sync_status, row.applying_since = "APPLYING", config.utcnow()
        db.commit()
    response = account.client.post("/api/settings/ai/apply", json={})
    assert response.status_code == 409 and "being applied" in response.json()["detail"]["message"]


# ───────────── versions and rollback ─────────────

def test_rollback_applies_an_old_snapshot_as_a_new_version(account):
    account.call("POST", "/models/sync")
    account.apply()  # v2
    account.worker("REVIEWER", model_mode="FIXED", primary_model="tier-2-reasoning")
    account.apply()  # v3
    account.worker("RANKER", temperature=0.1)
    account.apply()  # v4
    versions = {v["version"]: v for v in account.get("/versions")["versions"]}
    v2 = account.get(f"/versions/{versions[2]['id']}")
    out = account.call("POST", f"/versions/{versions[2]['id']}/restore", json={"base_version": 4})
    assert out["version"] == 5
    v5 = account.get(f"/versions/{account.get('/versions')['versions'][0]['id']}")
    assert v5["snapshot"]["workers"] == v2["snapshot"]["workers"] and v5["restored_from_version"] == 2
    assert account.get(f"/versions/{versions[3]['id']}")["snapshot"]["workers"]["REVIEWER"]["mode"] == "FIXED"
    assert model_resolution.get(account.user_id, "REVIEWER").model == "tier-3-review"
    assert ("ROLLBACK", "SUCCEEDED") in [(a["action"], a["result"]) for a in account.get("/audit")["entries"]]


def test_a_rollback_that_cannot_apply_keeps_the_draft(account):
    account.call("POST", "/models/sync")
    account.apply()
    account.worker("RANKER", temperature=0.4)
    first = account.get("/versions")["versions"][-1]
    response = account.client.post(f"/api/settings/ai/versions/{first['id']}/restore", json={"base_version": 99})
    assert response.status_code == 409
    assert account.get("")["draft"]["workers"]["RANKER"]["temperature"] == 0.4


def test_one_account_cannot_read_or_restore_anothers_versions(account, litellm):
    other = TestClient(app)
    other.post("/api/auth/signup", json={"email": f"other-{uuid.uuid4().hex}@example.com",
                                         "password": "a password for tests", "confirm_password": "a password for tests"})
    theirs = other.get("/api/settings/ai/versions").json()["versions"][0]["id"]
    assert account.client.get(f"/api/settings/ai/versions/{theirs}").status_code == 404
    assert account.client.post(f"/api/settings/ai/versions/{theirs}/restore", json={}).status_code == 404


# ───────────── the gateway switch and runtime view ─────────────

def test_the_gateway_switch_needs_a_base_url_and_can_be_turned_off(account, monkeypatch):
    assert account.gateway(True)["gateway_enabled"] is True
    assert account.get("")["gateway_enabled"] is True
    assert account.gateway(False)["gateway_enabled"] is False
    monkeypatch.setattr(get_settings(), "litellm_base_url", None)
    account.call("PUT", "/connection", json={"base_url": ""})
    account.call("PUT", "/gateway", json={"enabled": True}, expect=422)


def test_the_overview_switch_names_both_routes_and_returns_to_the_chosen_direct_provider(account, monkeypatch):
    """Overview offers exactly two routes: LiteLLM, or the direct provider (with its model) chosen in Settings."""
    monkeypatch.setattr(get_settings(), "llm_provider", "openrouter")
    body = {"llm_provider": "openrouter", "model": "openrouter/free", "application_mode": "manual_approval",
            "min_review_score": 70}
    assert account.client.put("/api/settings", json=body).status_code == 200
    status = account.get("/gateway")
    assert status["gateway_enabled"] is False and status["direct_provider"] == "openrouter"
    assert status["direct_label"] == "OpenRouter" and status["direct_model"] == "openrouter/free"
    assert status["direct_ready"] is False, "no OpenRouter key saved yet"
    assert status["litellm_ready"] is True

    on = account.gateway(True)
    assert on["gateway_enabled"] is True and on["direct_provider"] == "openrouter", "still knows where off goes back to"
    off = account.gateway(False)
    assert off["gateway_enabled"] is False and off["direct_provider"] == "openrouter"
    assert off["direct_model"] == "openrouter/free", "the direct provider keeps its own model"


def test_saving_the_direct_provider_form_never_turns_the_gateway_off(account, monkeypatch):
    """The form edits the provider that turning the gateway off returns to; only the gateway switch picks a route."""
    monkeypatch.setattr(get_settings(), "llm_provider", "openrouter")
    assert account.gateway(True)["gateway_enabled"] is True
    patch = account.client.patch("/api/settings", json={"llm_provider": "openai", "model": "gpt-5.6-luna"})
    assert patch.status_code == 200, patch.text
    assert patch.json()["ai"]["saved"]["llm_provider"] == "openai", "the form shows the direct provider"
    status = account.get("/gateway")
    assert status["gateway_enabled"] is True, "still on LiteLLM"
    assert status["direct_provider"] == "openai" and status["direct_model"] == "gpt-5.6-luna"
    off = account.gateway(False)
    assert off["gateway_enabled"] is False and off["direct_provider"] == "openai"


def test_runtime_view_distinguishes_desired_active_and_served(account, litellm):
    account.call("POST", "/models/sync")
    account.apply()
    account.worker("REVIEWER", model_mode="FIXED", primary_model="tier-2-reasoning")
    rows = {r["worker_type"]: r for r in account.get("/runtime")["workers"]}
    assert rows["REVIEWER"]["state"] == "PENDING" and rows["REVIEWER"]["desired"] == "tier-2-reasoning"
    assert rows["REVIEWER"]["active"] == "tier-3-review" and rows["RANKER"]["state"] == "SYNCED"
    litellm.deployments = [d for d in litellm.deployments if d["model_name"] != "tier-2-reasoning"]
    rows = {r["worker_type"]: r for r in account.get("/runtime")["workers"]}
    assert rows["RANKER"]["state"] == "OUT_OF_SYNC" and rows["RANKER"]["served_by_litellm"] is False
    assert account.call("POST", "/verify")["status"] == "OUT_OF_SYNC"
    assert account.get("")["connection"]["sync_status"] == "OUT_OF_SYNC"


def test_other_accounts_use_their_own_config_and_keys(account, litellm):
    other = TestClient(app)
    other.post("/api/auth/signup", json={"email": f"x-{uuid.uuid4().hex}@example.com",
                                         "password": "a password for tests", "confirm_password": "a password for tests"})
    assert other.get("/api/settings/ai").json()["connection"]["api_key_configured"] is False
    assert other.post("/api/settings/ai/test-connection").json()["label"] == "Authentication failed"
    account.worker("RANKER", temperature=0.9)
    assert other.get("/api/settings/ai").json()["draft"]["workers"]["RANKER"]["temperature"] is None


def test_prompts_stay_in_app_prompts():
    from pathlib import Path

    from app import prompts

    assert prompts.PROMPT_VERSIONS["model_health"] == prompts.PROMPT_VERSIONS["worker_test"] == "v1"
    root = Path(__file__).resolve().parents[1] / "app" / "services"
    for name in ("ai_model_config.py", "model_resolution.py", "ai_registry.py", "litellm_config_manager.py"):
        assert "You are" not in (root / name).read_text()
    assert Account  # the fixture helper is importable


def test_gateway_settings_never_create_a_row_and_only_the_first_account_gets_the_env_key(litellm, monkeypatch):
    from app.models import User
    from tests.conftest import make_user

    monkeypatch.setattr(get_settings(), "litellm_api_key", "sk-env-key-00000000")
    user_id = make_user(SessionLocal)
    with SessionLocal() as db:
        first = db.scalar(select(User.id).order_by(User.id).limit(1))
        assert config.gateway_settings(db, user_id) == ("http://litellm.test", None)
        assert config.gateway_settings(db, first)[1] == "sk-env-key-00000000"
        assert db.scalar(select(LiteLLMConnection).where(LiteLLMConnection.user_id == user_id)) is None
        assert not db.new and not db.dirty


def test_discard_puts_the_draft_back_to_the_active_version_and_creates_no_version(account, litellm):
    account.call("POST", "/models/sync")
    account.apply()
    active = account.get("")["active_version"]
    account.worker("RANKER", temperature=0.3, secondary_models=["tier-1-fast"])
    assert account.get("")["pending_changes"]
    out = account.call("POST", "/discard")
    assert out["status"] == "DISCARDED" and out["active_version"] == active
    after = account.get("")
    assert after["pending_changes"] == [] and after["active_version"] == active
    ranker = next(w for w in after["workers"] if w["worker_type"] == "RANKER")
    assert ranker["config"]["temperature"] is None and ranker["config"]["secondary_models"] == []


def test_a_model_keeps_its_own_fallbacks_and_rest_time_and_they_are_validated(account, litellm):
    account.call("POST", "/models/sync")
    names = [m["name"] for m in account.get("")["models"]]
    model, other = names[0], names[1]
    account.call("PUT", f"/models/{model}", json={"revision": account.revision(), "fallback_models": [other, other],
                                                   "rest_seconds": 900})
    saved = next(m for m in account.get("")["models"] if m["name"] == model)
    assert saved["fallback_models"] == [other] and saved["rest_seconds"] == 900
    account.apply()
    assert model_resolution.active_snapshot(account.user_id)[1]["models"][model]["fallback_models"] == [other]
    account.call("PUT", f"/models/{model}", expect=422, json={"revision": account.revision(), "fallback_models": [model]})
    account.call("PUT", f"/models/{model}", expect=422, json={"revision": account.revision(), "rest_seconds": 5})
    account.call("PUT", f"/models/{model}", json={"revision": account.revision(), "fallback_models": ["not-a-model"]})
    errors = account.call("POST", "/preview")["validation"]["errors"]
    assert any("not-a-model" in e["message"] for e in errors)


def test_a_model_test_leaves_room_for_reasoning_and_names_an_empty_answer(account, litellm):
    account.call("POST", "/models/sync")
    account.call("PUT", "/models/tier-2-reasoning", json={"revision": account.revision(),
                                                          "overrides": {"supports_reasoning": True}})
    assert account.call("POST", "/models/tier-2-reasoning/test")["success"]
    sent = litellm.chat_bodies[-1]
    assert sent["max_tokens"] >= 512 and sent["reasoning_effort"] == "low"  # room to think before {"ok": true}
    account.call("POST", "/models/tier-1-fast/test")
    assert "reasoning_effort" not in litellm.chat_bodies[-1]  # not known to reason: not sent
    litellm.answer("tier-2-reasoning", "")  # a reasoning model that spent everything thinking
    out = account.call("POST", "/models/tier-2-reasoning/test")
    assert not out["success"] and "returned no output" in out["error"]


def test_removing_a_model_is_refused_while_used_and_a_sync_does_not_bring_it_back(account, litellm):
    account.call("POST", "/models/sync")
    account.apply()
    used = account.call("POST", "/models/tier-2-reasoning/remove", expect=409,
                        json={"revision": account.revision()})["detail"]
    assert "tier PRIMARY_REASONING" in used["references"]
    account.call("PUT", "/models/tier-2-reasoning", json={"revision": account.revision(), "fallback_models": ["cloud-mini"]})
    before = account.get("")["active_version"]
    removed = account.call("POST", "/models/cloud-mini/remove", json={"revision": account.revision()})
    assert removed["dropped"] == ["tier-2-reasoning's fallbacks"]  # a fallback is dropped, not a reason to refuse
    assert account.get("")["draft"]["models"]["tier-2-reasoning"]["fallback_models"] == []
    out = account.apply()
    overview = account.get("")
    assert "cloud-mini" not in [m["name"] for m in overview["models"]]
    assert [r["name"] for r in overview["removed_models"]] == ["cloud-mini"]
    assert out["version"] > before and "cloud-mini" not in model_resolution.active_snapshot(account.user_id)[1]["models"]
    synced = account.call("POST", "/models/sync")
    assert "cloud-mini" not in synced["added"] and "cloud-mini" not in [m["name"] for m in account.get("")["models"]]
    assert any(d["model_name"] == "cloud-mini" for d in litellm.deployments)  # discovered: still in LiteLLM
    account.call("POST", "/models/cloud-mini/restore", json={"revision": account.revision()})
    assert "cloud-mini" in [m["name"] for m in account.get("")["models"]]
    account.call("POST", "/discard")  # Undo of a refused restore: the model is removed again, not left in the draft
    assert "cloud-mini" not in [m["name"] for m in account.get("")["models"]]
    assert [r["name"] for r in account.get("")["removed_models"]] == ["cloud-mini"]
    account.call("POST", "/models/nothing-here/remove", expect=404, json={"revision": account.revision()})


def _backups(data, worker_type):
    config = data["draft"]["workers"][worker_type]
    return config["fallback_model"], config["secondary_models"]


def test_disabling_a_model_drops_it_from_backups_and_model_fallbacks_and_applies(account, litellm):
    account.call("POST", "/models/sync")
    account.worker("RANKER", fallback_model="cloud-mini", secondary_models=["tier-3-review"])
    account.worker("REVIEWER", fallback_model="tier-1-fast", secondary_models=["cloud-mini", "tier-2-reasoning"])
    account.call("PUT", "/models/tier-2-reasoning", json={"revision": account.revision(),
                                                          "fallback_models": ["cloud-mini", "tier-1-fast"]})
    account.apply()
    out = account.call("PUT", "/models/cloud-mini", json={"revision": account.revision(), "enabled": False})
    assert out["dropped"] == ["Ranker (backup 1)", "Reviewer (backup 2)", "tier-2-reasoning's fallbacks"]
    data = account.get("")
    assert _backups(data, "RANKER") == ("tier-3-review", [])  # the next backup moves up
    assert _backups(data, "REVIEWER") == ("tier-1-fast", ["tier-2-reasoning"])  # order kept
    assert data["draft"]["models"]["tier-2-reasoning"]["fallback_models"] == ["tier-1-fast"]
    assert account.apply()["status"] != "FAILED"
    active = model_resolution.active_snapshot(account.user_id)[1]
    assert active["models"]["cloud-mini"]["enabled"] is False and active["workers"]["RANKER"]["fallback_model"] == "tier-3-review"
    with SessionLocal() as db:
        entry = db.scalar(select(AIConfigAuditEntry).where(AIConfigAuditEntry.user_id == account.user_id,
                                                           AIConfigAuditEntry.action == "DISABLE"))
        assert entry.changes == [{"model": "cloud-mini", "dropped_from": out["dropped"]}]
    again = account.call("PUT", "/models/cloud-mini", json={"revision": account.revision(), "enabled": True})
    assert again["dropped"] == [] and _backups(account.get(""), "RANKER") == ("tier-3-review", [])  # not restored


def test_a_main_or_tier_model_is_never_dropped_and_still_blocks_removal(account, litellm):
    account.call("POST", "/models/sync")
    account.worker("REVIEWER", model_mode="FIXED", primary_model="cloud-mini")
    account.worker("RANKER", fallback_model="cloud-mini")
    account.apply()
    out = account.call("PUT", "/models/cloud-mini", json={"revision": account.revision(), "enabled": False})
    assert out["dropped"] == ["Ranker (backup 1)"]
    assert account.get("")["draft"]["workers"]["REVIEWER"]["primary_model"] == "cloud-mini"  # its own model: kept
    applied = account.apply()
    assert any("Reviewer has no usable model" in w["message"] for w in applied["warnings"])
    account.call("PUT", "/models/cloud-mini", json={"revision": account.revision(), "enabled": True})
    account.worker("RANKER", fallback_model="cloud-mini")
    used = account.call("POST", "/models/cloud-mini/remove", expect=409, json={"revision": account.revision()})
    assert used["detail"]["references"] == ["Reviewer (its model)"]
    assert _backups(account.get(""), "RANKER") == ("cloud-mini", [])  # a refused removal drops nothing
    tier = account.call("POST", "/models/tier-1-fast/remove", expect=409, json={"revision": account.revision()})
    assert "tier FAST" in tier["detail"]["references"]


def test_removing_a_managed_model_deletes_its_deployment_and_a_rollback_brings_it_back(account, litellm):
    account.call("POST", "/models/sync")
    account.call("POST", "/models", expect=201, json={"revision": account.revision(), "litellm_model_name": "local-small",
                                          "ollama_model": "llama3.2:3b", "num_ctx": 4096, "model_type": "CUSTOM"})
    added = account.apply()
    assert any(d["model_name"] == "local-small" for d in litellm.managed())
    account.call("POST", "/models/local-small/remove", json={"revision": account.revision()})
    account.apply()
    assert not any(d["model_name"] == "local-small" for d in litellm.managed())  # ours: removed from LiteLLM too
    account.call("POST", "/models/local-small/restore", json={"revision": account.revision()})
    account.apply()  # still ours: restoring redeploys it
    assert any(d["model_name"] == "local-small" for d in litellm.managed())
    account.call("POST", "/models/local-small/remove", json={"revision": account.revision()})
    account.apply()
    versions = account.get("/versions")["versions"]
    with_model = next(v for v in versions if v["version"] == added["version"])
    account.call("POST", f"/versions/{with_model['id']}/restore", json={"base_version": account.get("")["active_version"]})
    assert "local-small" in [m["name"] for m in account.get("")["models"]]


def test_verification_counts_a_primary_models_own_fallbacks():
    from app.services.ai_model_config import _referenced

    snap = snapshot(RANKER={"mode": "FIXED", "primary_model": "mid"})
    snap["models"]["mid"]["fallback_models"] = ["deep"]
    snap["models"]["fast"]["fallback_models"] = ["never-used"]  # not a primary: its fallbacks join no chain
    assert "deep" in _referenced(snap) and "never-used" not in _referenced(snap)


def test_validation_issues_name_what_they_are_about():
    from app.services.ai_model_config import _issue, issue_text
    assert _issue("workers.RANKER.fallback_model", "x")["subject"] == "Ranker"
    assert _issue("tiers.FAST.model", "x")["subject"] == "Tier FAST"
    assert _issue("routing.tier_models.SIMPLE", "x")["subject"] == "Auto Router"
    assert _issue("models.gpt-4.1.fallback_models.0", "x")["subject"] == "Model gpt-4.1"
    assert "subject" not in _issue("somewhere", "x")
    assert issue_text(_issue("workers.RANKER.fallback_model", "The fallback model cannot be the primary model")) == \
        "Ranker: The fallback model cannot be the primary model"
    assert issue_text(_issue("workers.REVIEWER.mode", "Reviewer has no usable model")) == "Reviewer has no usable model"
