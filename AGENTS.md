# Working on Job Hunt OS

Conventions that are not obvious from the code, and the rules a change has to keep. Read the README for what
the system does; this file is about how to add to it.

## Third-party APIs: use the instrumentation layer

**Every production call to a third party goes through `app/services/external_api.py`. Do not create an
unmanaged HTTP client in business-service code.**

```
Job Hunt OS
  → external_client(...) / external_operation(...)      ← app/services/external_api.py
  → the third-party API
  → ExternalAPIRequestLog (+ a log line when it fails)  ← written automatically
  → Monitoring → External APIs
```

Logging is infrastructure, not something a service remembers to do. There is no `log_external_call(...)` to
call before a request, because a developer can forget one; the client *is* the telemetry.

### Adding a new provider

1. **HTTP APIs** — use the shared client. That is the whole integration:

   ```python
   from app.services.external_api import external_client

   def fetch_listings(token: str, query: str) -> list[dict]:
       with external_client("SOME_BOARD", service="listings_api", operation="search",
                            headers={"Authorization": f"Bearer {token}"}, timeout=20) as client:
           response = client.get("https://api.example.test/v1/listings", params={"q": query})
           response.raise_for_status()
           return response.json()["items"]
   ```

   Latency, HTTP status, byte sizes, retries, rate-limit headers, failure classification, the trace ID and the
   account all come with it. Name a different operation per request with
   `client.get(url, extensions={"operation": "listing_detail", "metadata": {"listing_id": 7}})`.

2. **Vendor SDKs that own their transport** (OpenAI, Anthropic, Gemini) — wrap the boundary once, in the
   provider adapter, never in each method:

   ```python
   with external_operation("SOME_SDK", service="v1", operation="generate", user_id=user_id) as call:
       result = sdk.generate(...)
       call.note(model=result.model).record_usage(cost=result.cost, source="provider_reported")
   ```

   or decorate the integration method with `@track_external_call("SOME_SDK", service="v1")`.

3. `provider` is a free string, upper-cased. Adding one needs no migration and no enum change. Use an existing
   name when the call belongs to an existing service (`APIFY`, `OPENROUTER`, `OPENAI`, `ANTHROPIC`, `GEMINI`,
   `LITELLM`, `OLLAMA`, `EMAIL`, `SEARCH`, `JOB_BOARD`, `WEB`).

4. `tests/test_external_api_guard.py` fails the build when an unmanaged `httpx.Client`, `requests` or
   `aiohttp` appears anywhere under `app/`. Only `app/services/external_api.py` is allowlisted. Tests may use
   whatever they like.

### Rules for what gets stored

* **Never store credentials.** Redaction lives in `app/services/redaction.py` and runs inside the
  instrumentation layer, so a caller cannot forget it. Authorization, cookies and API-key headers are replaced
  wholesale; secret-shaped strings are replaced anywhere they appear.
* **Never store request or response bodies by default.** Sizes are recorded instead. Bodies are kept only on request:
  an account's own capture switch (Monitoring → Logs, the same one as LLM prompts) keeps the redacted request and
  response of its external calls in `external_api_request_logs.payload` (`external_api.capture_payload`, capped at
  `EXTERNAL_API_CAPTURE_LIMIT`), cleared after `LLM_PROMPT_DEBUG_RETENTION_DAYS` by the cleanup job
  (`external_monitoring.clear_captured`); `EXTERNAL_API_DEBUG_PAYLOADS=true` does the same, briefer, for every account.
  Bodies go through `redaction.redact_body` (credential-named JSON and form fields replaced, then secret shapes), and
  are redacted again on read. Never add another path that stores a body.
* **LLM prompts and responses are captured only on request**: `LLM_PROMPT_DEBUG` (every account) or an account's own
  switch in Monitoring → LLM requests (`AppSetting.preferences["monitoring"]["capture_llm_payloads"]`, off by default).
  The text is redacted on write and on read, and cleared after `LLM_PROMPT_DEBUG_RETENTION_DAYS` (7).
* **Never guess a cost.** `record_usage(cost=...)` takes a figure the provider reported. Unknown cost stays
  NULL; NULL means unknown, not free.
* **Telemetry must not change an outcome.** Every failure inside `external_api` and `system_log` is caught and
  logged. If the monitoring database is unreachable, the API call still succeeds.

## LLM calls and model configuration

These rules keep the model a worker uses a matter of configuration, never of code:

* **All production LLM calls use the centralized LLM gateway abstraction**: `LLMClient.generate_prompt(prompt,
  LLMCall(...))` in `app/services/llm.py`. Never call a provider SDK, LiteLLM or Ollama from a service.
* **Workers must not hard-code provider model names.** No `model="gpt-oss:20b"` anywhere in a worker. A worker is
  identified by its prompt's name (`app/services/ai_workers.py`), and the LLM layer resolves its model.
* **Worker model selection comes from `WorkerModelConfig`**, read at request time through
  `model_resolution.get(user_id, "RANKER")`, which returns the model and generation settings of the account's
  *active* `AIConfigVersion` (cached for `AI_CONFIG_CACHE_SECONDS`, cleared on Apply).
* **When LiteLLM gateway mode is enabled (provider `litellm`), workers must not bypass LiteLLM.** Every request goes
  to the proxy; there is no direct-provider shortcut in that mode.
* **Model configuration changes go through the AI configuration service** (`app/services/ai_model_config.py`):
  edit the draft → preview → validate → apply → version. Do not write `WorkerModelConfig`, `ModelTier`,
  `AIRoutingConfig` or `AIModel` rows anywhere else, and never mutate an `AIConfigVersion`; roll back by applying an
  old snapshot as a new version. The Models & routing screen saves by writing the draft and applying it at once; when
  the apply is refused it offers Undo (`discard_draft`, which puts the draft back to the active version and never
  creates a version). Keep that pairing: a refused save must never leave edits silently waiting in the draft.
* **Deleting a model is a soft removal** (`ai_model_config.remove_model`, `AIModel.removed_at`): it leaves the draft,
  every picker and the next version, and `ai_registry.discover` never brings it back; Restore (or a rollback to a version
  that had it) does. Disabling or removing a model first drops it from worker backups and model fallbacks
  (`drop_backup_uses`), never from a main model, a tier or the router; it is refused while anything still uses it
  (`model_references`). Only a deployment Job Hunt OS owns
  leaves LiteLLM (through the normal apply; `managed` is kept, so Restore redeploys it); a discovered model stays in
  LiteLLM. Undo (`discard_draft`) removes again a model restored since the active version.
* **Prompts remain under `app/prompts/`.** Prompts say *what* a model is asked to do; worker configuration decides
  *which* model, its generation settings and routing. Never put model selection in a prompt, or a prompt in the
  configuration.
* **Adding a worker**: add a `Worker` to `app/services/ai_workers.py` with the prompt names it sends, whether it
  requires structured output, its minimum context and its default assignment. Nothing else needs to change; its
  config row is seeded, and it appears in Settings and Monitoring.
* **Routing policy is central.** Quality escalation (`escalation_reason`), request metadata and parameter filtering
  (`request_params`) live in `app/services/model_resolution.py`. A worker never decides to retry on a bigger model.
  Infrastructure fallback (the worker's `fallback_model`, sent to LiteLLM) and quality escalation stay separate.
* **A prompt token budget trims; a context window refuses.** `WorkerModelConfig.prompt_token_budget` (versioned, draft →
  apply) is read through `prompt_budget.budget_for` (LiteLLM gateway mode only). Over it, `context_builder.fit_prompt`
  (Ranker, Tailor) and `ai_advisor.fit_advisor_prompt` (AI Optimization) render, measure and drop one item at a time in
  their documented order (`RANKING_TRIM_ORDER`, `TAILORING_TRIM_ORDER`, `ADVISOR_TRIM_ORDER`) — deterministic, no
  model. Trim before anything is computed from the context (ranking hashes, citable ids), so a validator never accepts
  an id the prompt no longer shows. Never drop a REQUIRED requirement, the employer's sentences for it, the fit
  analysis or rule findings. Unset, the builders' prompts stay byte-identical (`tests/test_prompt_budget.py`). A worker
  joins `prompt_budget.TRIMMED_WORKERS` only with trim steps of its own.
* **Unsupported parameters are never sent blindly**: a parameter goes out only when the model is not known to lack
  it; reasoning effort needs a model known to reason. Unknown capability stays unknown (NULL), never guessed.
* **Job Hunt OS never destroys LiteLLM configuration it does not own.** `LiteLLMConfigManager` creates and removes
  only deployments marked `jobhunt_managed` with this install's owner tag, and in `config_file` mode writes only
  `LITELLM_MANAGED_CONFIG_PATH`. Do not add code that edits LiteLLM's own config file or `router_settings`.
* **LiteLLM changes are atomic where possible**: validate first, apply, verify, and undo everything on failure so the
  previous version stays active. Do not use a LiteLLM admin endpoint the installed version does not have; check its
  `/openapi.json` first.
* **No secret in configuration**: a managed deployment's credentials are `os.environ/NAME` references; the LiteLLM
  key is encrypted like the provider keys and never returned, logged or snapshotted.

## Model failover (route health)

`app/services/model_health.py` (health), `app/services/failover.py` (choice) and `app/services/failover_settings.py`
(thresholds) move a worker's requests off a model that is rate limited or failing, and back when it recovers. The
rules a change has to keep:

* **Failover is a runtime overlay, never a configuration edit.** It writes `model_route_states` and `failover_events`
  only. It never writes `WorkerModelConfig`, `ModelTier`, `AIRoutingConfig` or an `AIConfigVersion`; the models a worker
  may fail over to (`fallback_model`, `secondary_models`) are configuration and change through draft → preview → apply.
* **Health is judged from what happened, deterministically.** Request outcomes (`llm_request_logs`) and the account's
  thresholds decide; no model is called. Too few samples (`min_samples`) is never a breach: unknown stays healthy.
  Configuration errors and context overflows say nothing about a model and are not counted.
* **PostgreSQL is the only shared state.** No Redis, no cross-process in-memory counters: a trip is a database row,
  readers cache it for a couple of seconds (`model_health.states`), like `backoff.cooling_until`.
* **Fails open.** Any error in health tracking or ordering leaves the configured worker untouched and never changes a
  request's outcome (`record_outcome` and `failover.apply` catch everything).
* **A chain is the worker's backups, then its model's own fallbacks** (`AIModel.fallback_models`, `ResolvedWorker.chain`).
  Only the primary model's fallbacks join it, never a fallback's fallbacks, so two models may name each other without a
  loop. A model's `rest_seconds` is exactly how long it rests after a trip, every time; without it a 429 rests it for the
  account's `rate_limit_rest_minutes` (also fixed) when set, and otherwise the account's cooldown doubles per trip. The
  model's fallbacks and rest time are configuration (versioned, draft → apply); the account's are failover settings.
* **A resting model is a last resort, not removed**, so a request is never left with nothing to call. After its
  cooldown a route is half-open: traffic probes it, `recovery_successes` successes restore it, one failure rests it
  again for twice as long.
* **A 429 rests the model for as long as the provider said** (`app/services/rate_limits.py`, read from the full 429
  response before its text is cut): never before a reset it named (a header, `X-RateLimit-Reset` in the body, "try
  again in 8.4s"); a usage cap with no reset (Ollama's 5-hour / weekly limits) for the account's
  `usage_limit_rest_minutes`. "Request too large" refused one request, not the model: it never rests it, starts no
  request cooldown (`external_api` marks the call `oversized_request`) and is not counted in the health window. A reset is a provider fact; never invent one.
* **Requests too big for a model are shown, not rerouted** (`app/services/model_capacity.py`, Workers tab). From the
  account's own refusals (the provider's stated limit and request size) and successes, it offers an answer cap only
  when this worker's answers fit under it, and models with room: no refusal of a request that size, and a known context
  window large enough or an answer to one (credited to the model that answered: `model_group`, never the requested
  model of a fallback). A fix replaces the model in the role it has (primary, backup, escalation). It recommends; a
  person applies a fix through draft → apply.
* **A 429 rests the model, not the gateway.** The LiteLLM cooldown is kept per model (`external_api.cooldown_provider`,
  `LITELLM:<model>`), so a rate-limited model never blocks the model failover sends the request to. Other providers
  still cool down as a whole.
* **A spent minute is waited out, not sent into a 429** (`app/services/token_pacing.py`). Each gateway response's
  token headers (`x-ratelimit-*-tokens`, bare or LiteLLM's `llm_provider-` form; resets are durations such as
  `2m59.56s`) are kept per `LITELLM:<model>` in `provider_rate_windows`; `external_api.hold_off` reserves the request's
  `tokens_needed` (estimated prompt + `max_tokens`) with one guarded UPDATE and holds it with `RateLimitedCooldown`
  until the token reset when the window cannot take it. A reservation stays on the window (`tokens_reserved`) until its
  request's answer, so a provider figure that has not seen requests in flight never frees their tokens; a failure gives
  it back. A window belongs to the model that answered (`x-litellm-model-group`), never to the requested one when a
  fallback answered. Failover moves a waiting model behind its backups (an overlay, never a health trip); a request
  that was never sent counts against a model's health nowhere (`record_outcome`, `window_stats`); a held ranking gives
  its attempt back and comes back at the reset (`LLMRateLimitError.held_seconds`). Only what the provider said paces:
  no row, an expired reset or a request larger than the whole limit is sent. Fails open.
  Groq's request counter is per DAY: never report it as the figure that runs out first.
* **A request larger than the whole per-minute limit fails once.** Besides the rules above (no cooldown, no rest), its
  `LLMRateLimitError` (`rate_limit.kind == TOO_LARGE`) is never retried by the ranker, gives back its token
  reservation and leaves the window alone. Its row stays a `RATE_LIMIT` with the provider's figures: Model capacity
  reads it.
* **Quality escalation and infrastructure failover stay separate.** Failover never decides on answer quality, and an
  escalated request gets no failover.
* **Notifications are metadata only** (`/api/failover/events`: model names, cause, counts) and use the existing browser
  Notification helpers in `app/static/app.js`; permission is asked only on a click, with a toast fallback.
* **Log lines after the commit.** `model_health._emit` queues the Monitoring line and `_flush_logs` writes it once the
  transaction is committed; `system_log` uses its own connection.

## Job description recovery and the shared browser

`app/services/jd_recovery.py` reads a posting's description from its URL when the board sent none (README §13).
The rules a change has to keep:

* **JD recovery runs asynchronously.** Ingestion (`sources.upsert_job`), API requests and other workers only write a
  `JobDescriptionFetchTask`; the `jd_recovery` scheduler job does the fetching. Never fetch a posting inside a
  request or an import. PostgreSQL (the task table) is the only queue: no Celery, Redis or in-memory queues.
* **One active task per job** (`uq_jd_fetch_tasks_active_job`), claimed with SKIP LOCKED plus a guarded UPDATE, with
  bounded attempts. A failure is one task's failure; it never raises out of the scheduled job.
* **HTTP before the browser.** HTTP goes through `external_client` (`app/services/external_api.py`) with redirects
  followed one by one and each hop checked by `app/services/url_safety.py`. The browser is used only when HTTP found
  no usable description. A generic HTTP `ACCESS_DENIED` may receive one normal browser rendering attempt; an explicit
  login wall, anti-bot challenge or 429 never does.
* **Playwright access goes through the shared `BrowserService`** (`app.infrastructure.browser.get_browser_service()`).
  Workers and features must not import Playwright or launch Chromium themselves. Pages get an isolated context each,
  are closed in `finally`, and never receive API keys, credentials, cookies or environment values.
* **CAPTCHA and anti-bot protections are never bypassed.** No solvers, stealth plugins, fingerprint spoofing or
  challenge retries: a blocked page is recorded as `BLOCKED` with its reason, and a person pastes the text.
* **Recovered descriptions require deterministic validation before persistence** (`jd_quality.validate_recovered`).
  Detection (`jd_quality.description_problem`) never decides by length alone.
* **Original scraped descriptions are never silently destroyed.** The board's text moves to
  `original_description`; a usable description is replaced only by a fuller version of the same posting
  (`jd_quality.better_than`); text a person saved (`description_source = USER`) is never replaced. Record provenance
  (`description_source`, `description_fetch_method`, URLs, `description_fetched_at`, task id) — never invent it.
* **LLM extraction is a fallback, not the retrieval mechanism.** It runs only on an ambiguous page, sees at most a few
  already-fetched candidate blocks (never the whole page), and returns block ids (`JobDescriptionRecoveryResult`);
  the saved text is the page's own. Its prompt lives in `app/prompts/job_description_recovery.py` (this repository's
  prompt package is `app/prompts/`), treats page text as untrusted data, and has a version constant.
* **Successful recovery feeds the existing Structured JD pipeline**: `job_description.queue_parse`, then
  `ranker.reopen_for_description` / `ranker.queue_job`. Recovery never parses or ranks by itself, and there is no
  second parser or a second workflow state model — recovery status lives on the task.
* **Site extractors are small.** Add a site to `app/services/jd_extractors.py` only for a documented public endpoint
  or the one container its postings use; the Generic extractor must keep working for everything else.

## Discovery quality: domain filter, feedback, removed postings, resume per job

* **The domain filter is deterministic** (`app/services/domain_match.py`): never a model. It runs where jobs are
  created (`app/services/discovery.admit`, called by `sources.upsert_job` and `hunter._save`) before description
  recovery, parsing or ranking are queued, and in the ranking and recovery sweeps for older jobs. A hidden job
  (`domain_match = NOT_MATCHING`) must never get a recovery, parse or ranking task; `ranker.hard_filter_reason` refuses
  it whatever path queued it. When unsure, keep the job. A person's own terms (roles, skills, include list) always win,
  jobs added by hand are never hidden, and KEPT jobs are never re-assessed.
* **Learning is counting, not inference** (`app/services/feedback_learning.py`): record feedback with
  `feedback_learning.record(db, job, action)` *before* deleting a job, then `after_feedback`, which learns inside a
  savepoint so a failure never undoes the person's action. Automatic deletions (dead postings) are never feedback.
* **Dead postings go through `job_removal.remove_posting`**: tombstone, log line, `delete_job` (which also removes task
  rows, for SQLite). Approved or applied jobs are never deleted (an approved one gets `posting_closed_at` instead), and
  only a page that says the posting is gone counts. The daily listing check (`job_removal.check_listings`) covers jobs
  up to approval, skips jobs a worker or tailoring task holds, and may render a page once in the shared browser after
  HTTP — never after a login wall, a challenge or a 429, and never a model.
  Import paths check `job_removal.is_removed` before creating a job.
* **One resume selection: the active version (`Resume.is_active`).** Only `resume_verification.set_active` changes it,
  and only because the person asked (Make active). Uploads, saved resume text, structured edits, file deletes and
  re-parses never activate a version; the one exception is a profile's very first resume. `CandidateProfile.master_resume`
  and `ResumeDocument.active` are mirrors that `set_active` keeps in step — never write them anywhere else. There is no
  "default" resume.
* **Resolve the resume through `resume_verification.for_job(db, profile, job)`** (`selected(db, profile)` without a job)
  in every workflow (ranking, tailoring, review, follow-ups, MCP). It returns a view whose `chosen_resume` is the job's own
  resume, else the active one, and whose `master_resume` is that resume in the form the candidate policy's
  `resume_source` names: `structured` renders its structured data (`structured_resume.render_document`, rejected units
  left out), `master_text` is its raw text as written. Never read `profile.master_resume` of a bare profile in a
  workflow, and never parse the raw text in structured mode. Ranking identity (snapshot, hashes) uses
  `resume_verification.raw_text(view)`, so verifying facts does not re-rank every job. The view only changes what is read;
  it never activates a resume or writes the master text.
* **Setups pin resumes, and their jobs follow.** Changing a setup's resume re-applies it to the jobs it found
  (`discovery.follow_setup`) — never to jobs the person pinned (`USER`, `ACTIVE`) or approved/applied ones.
* **Resume choice is deterministic and pins win** (`app/services/resume_matching.py`): tags live on `Resume`
  (`domains`: one or more domains, each with its roles; a resume scores as its best-fitting domain). `domain` /
  `roles` are mirrors; only `resume_matching.set_tags` writes the three, and `groups(resume)` reads them; `discovery.choose_resume` applies the setup's pin, else `auto_assign`. Only `resume_choice`
  NULL or `AUTO` may ever be re-matched; `SETUP`, `USER` and `ACTIVE` are the person's decisions. Changing tags never
  re-ranks by itself — re-matching is an explicit action, because it can queue model calls. A job whose stored match
  differs from what today's tags pick shows it (`workspace.stale_match`, read-only) and offers Re-match on a click.
  A person moving a tailored job to another resume (`discovery.repoint_jobs`) marks it `RESUME_CHANGED`: automatic
  tailoring (when on) re-tailors it once the new ranking qualifies it — never from the old resume's analysis, and only
  when its ranking was queued.

## Resume extraction V2 (reading uploaded files)

`app/services/resume_extraction/` (README §11 "Reading the file", `docs/resume-extraction-plan.md`) reads an uploaded
PDF when `RESUME_EXTRACTION_V2` is on. The rules a change has to keep:

* **Off is the old path, byte for byte.** `document_text.extract` calls the pipeline only for a PDF or DOCX with V2
  on; TXT reads through `document_text.read_plain` either way. A version read without V2 has no `ResumeExtraction` row
  and is parsed from its raw text exactly as before (`resume_verification.parse_text`).
* **The switch is per account, defaulting to the server.** A resume upload passes `resume_parsing.extraction_v2`
  (worker setting `resume_parsing.extraction_v2`, Settings → Resumes); a value the account never set follows
  `RESUME_EXTRACTION_V2`. Callers without an account (job description files) pass nothing and follow the server.
* **The text the file states comes first.** PDFium (`text_layer.read_pdfium`) honours `/ActualText`; pypdf is only the
  fallback for a file PDFium cannot open. Every word keeps its page, box and font: never drop provenance.
* **Normalise artefacts, never meaning.** `normalize.TABLE` lists the characters that change; no NFKC. A line-break
  hyphen is removed only when the joined word is known and its halves are not; otherwise the hyphen stays.
* **Deterministic, local, bounded.** No model and no network in this step: it may run in the upload request. OCR
  (`ocr.read_pdf`, Tesseract on this machine; a VLM backend would be a setting, off) runs only for a PDF the quality
  report does not route `clean`, within `RESUME_OCR_MAX_PAGES` / `RESUME_OCR_TIMEOUT_SECONDS`; a missing, slow or
  failing engine means no OCR, never a failed upload. The quality report and the log line hold counts, routes and
  flags, never resume words.
* **A correction needs evidence and is never silent** (`correction.correct`). Only an unknown or broken-looking word
  is touched — never one the dictionary or the resume vocabulary knows. Evidence is the page reading a known word at
  the same place, at most two glyphs apart (`_glyph_edits`: a ligature glyph reads as up to three letters), or a substitution the file uses on at least `MIN_RULE_WORDS` different
  unknown words (seed `lexicons/confusions.yaml`, or learnt from the page). Every change is a Correction on the record
  with its evidence; `Resume.raw_text` keeps the file's word and `store.clean_text` applies the corrections nobody
  undid (the view and grounding read it). A person keeps or undoes each; undo re-reads the resume.
* **Structure is marked, never written into the resume.** `sections.detect` (synonyms, keywords, the document's own
  heading style; never a model) and `structure.view` produce the parse view the regex parser reads. `Resume.raw_text`
  stays what the file says; the view, stored on `ResumeExtraction`, holds only its words plus markers ("## Section",
  "### Title | Company | …", "- "). An unknown heading starts its own section: lines never run on into a neighbour
  and are never dropped. Only `resume_extraction.store` writes `resume_extractions`; a person's decisions go to
  `resume_extraction_feedback`.
* **A person's section choice wins, and is learnt by counting.** `store.assign` records the choice (feedback and the
  record's `overrides`), rebuilds the view from the stored text and its recorded look (never from the file again) and
  re-parses: the data is filled only while nothing is reviewed or edited (`resume_parsing.untouched`), otherwise the
  new parse is the usual comparison the person resolves. `store.learned` is the account's own choices, counted per
  heading as written, applied before the synonym table on its next uploads; never another account's. A view is parsed
  with `parse_deterministic(..., marked=True)` so headings are not guessed again. A heading no table could place
  (`unknown`) blocks `verify_resume` until it is assigned.
* **The AI parse of a V2 version reads its parse view (prompt v7) and grounding never loses silently.** A version
  read without V2 keeps prompt v6 byte for byte. A normalized skill stays only when its written form (`raw`) is in the
  text and `resume_parsing.normalizes` (containment, a known alias, initials) checks it is the same skill; the unit
  keeps `raw`. What grounding drops is computed by `held_back` and stored on the record's review list — values only —
  for the person to add (through `rv.apply_edit`: their change, unverified) or discard. Nothing on the list reaches a
  workflow unasked; never loosen grounding to keep it.
* **Confidence is computed, explained and never verifies by itself** (`confidence.assess`, on read, never stored: it
  cannot go stale). It is rules over checkable signals — the value in the clean text word for word, the correction
  behind a word and its evidence, a normalized or alias match, a fuzzy passage, a misread-looking word, the quality
  route — each with a human-readable reason. "Accept high-confidence" (`resume_verification.verify_confident`) is the
  person's action and verifies only facts still UNVERIFIED that are rated high; tailoring stays verified-only. The
  `resume_extraction` gate `high_confidence_precision` (≥ 0.99) checks that a high fact is right.
* **Unverified means high confidence, or held back.** With `allow_unverified_resume_facts` on, generation and review
  use an UNVERIFIED fact of a V2 version only when it is rated high (`context_builder.hold_back_unconfident`, in
  `_base`, on a copy marked `HELD_BACK`; a held role holds its bullets). The rest go to `TaskContext.held_back_facts`
  (the reviewer's prompt shows them only when there are some; MCP `get_tailoring_context` / `get_review_context`
  list them with `HELD_BACK_NOTE`). Ranking is not gated; VERIFIED, EDITED and REJECTED facts are never touched; a
  version without confidence is used as before. Never let a held fact back in through another path.
* **Measured, not assumed.** Run `python scripts/agent_eval.py --suite resume_extraction --resume-pipeline v2`; its
  hard gates (`valid_words_altered`, `hallucinated_values`) stay at 0. Fixtures are synthetic
  (`tests/fixtures/resume_extraction/`, rebuilt by `scripts/build_resume_fixtures.py`); a real resume goes only in the
  gitignored `real/` folder. Bump the dataset's `version` when a case changes.

## Resume structuring (AI parse of uploads)

`app/services/resume_parsing.py` (queue and steps), `app/agents/resume_parsing.py` (the graph) and
`app/services/resume_parsing_graph.py` (graph or plain path) structure each uploaded resume with the Resume Parser
model (README §11). The rules a change has to keep:

* **No model in a request.** `record_version` and "Re-parse with AI" only write a `ResumeParseTask`; the
  `resume_parse` scheduler job runs it. The regex parse (`structured_resume.parse_deterministic`) is written at once
  and stays when the model is unavailable.
* **Grounding drops, never rewrites** (`resume_parsing.ground`, deterministic): a value is kept only when the resume's
  own text contains it; dates are recomputed from their raw text with `normalize_date`. Never loosen it to make a
  model's paraphrase pass; improve the prompt instead.
* **A person's work wins.** The result replaces `structured_data` only while `untouched` holds (no reviewed unit, no
  USER change, the revision the task was queued against); otherwise it is `resume_verification.compare`, the same
  comparison a re-parse makes. Everything it writes is UNVERIFIED. It never activates a resume.
* **The graph conducts the service's steps.** Its one model call goes through `GatewayChatModel` bound to the
  `resume_parser` prompt; every write is `effects.within` plus the task's lease (`held`). The model output lives on
  `resume_parse_tasks.staged` (cleared when the task ends), never in a checkpoint; graph state is ids and counts.
* **Extracted text is repaired, never rewritten.** Uploads are read by `document_text.extract`; PDF text goes through
  `text_repair.repair_glyphs` (deterministic: a broken ligature glyph is replaced only where the inferred ligature makes
  a known word) before any parser sees it, so grounding checks the repaired text. Never add a model to that step.
* **AI only is a queue order, not a missing fallback.** With the account's `resume_parsing.ai_only` worker setting an
  upload is queued run-now and the screen hides the regex data while the model reads; the regex parse is still written
  and stays when the model cannot answer.
* **Kill stops; an upload never revives it.** A stopped task's text is not queued again by an upload; Retry, Run now
  or Re-parse with AI do.

## Job Insights (company, compensation and interview research)

`app/services/job_insights/` builds a source-backed research package for an approved job (README §14a). The rules a
change has to keep:

* **Never present generated interview questions as reported questions.** REPORTED questions come only from
  INTERVIEW_QUESTION observations and cite them; ROLE_DERIVED and CANDIDATE_SPECIFIC questions carry a rationale and
  requirement / evidence ids, never source attribution. Interview stages are REPORTED or INFERRED, never equivalent.
* **Never present a market estimate as an employer-approved salary band**, and **never replace the employer's salary
  with a lower-confidence estimate.** The posting's own salary (`compensation.extract_posting_salary`) is priority 1,
  a recruiter budget the person entered is priority 2; a board's *estimate* is market data. Compensation is computed in
  code (`compensation.compensation_insight`): no model output ever contains a figure, currencies are never converted
  without a configured FX source, base / total cash / total compensation are never compared, and conflicting
  estimates are listed, not averaged.
* **Every external factual insight has provenance**: an `InsightObservation` (source type, provider, actor, URL or
  run/dataset reference, observed and fetched dates, match scope and status). Model output that cites an id it was not
  shown is rejected (`synthesis.validate_company` / `validate_interview`); items are labelled FACT, DERIVED, GENERATED
  or UNKNOWN in storage and in the UI.
* **External content is untrusted.** Pages, review text, actor output and job descriptions reach a model only through
  `untrusted()` in `app/prompts/company_insights.py` / `interview_insights.py`. The model never browses.
* **A refresh never erases last known good data because one provider failed**: a failed source keeps its previous
  observations, a run without the model keeps the previous synthesized sections (`service.SYNTHESIZED`), and a run
  where everything failed changes nothing and is retried. Recruiter input is never written by research.
* **Do not invent Apify actor IDs or provider schemas.** Actors are settings (`APIFY_*_ACTOR`) and their input is an
  admin template (`APIFY_*_INPUT`); a source missing either is NOT_CONFIGURED, never run with a guessed input, their output is read through aliases in `apify.py`, and an
  unrecognized shape is FAILED ("schema not recognized").
* **Add a source as an adapter** (`base.InsightSource`: `name`, `label`, `source_type`, `company_level`,
  `configured()`, `fetch(snapshot)`) in `service.SOURCES`; it never raises, never touches the database and calls out
  only through `external_client` / `apify_client`. Matching to the job's company, role and location goes through
  `matching.assess` — deterministic, never a model; an ambiguous company is NEEDS_REVIEW and unused.
* **Never guess a company website** (no `name.com` probing); it comes from the posting's data or the person.
* **The cache is per account** (`service.cached`): observations are reused only within the same `user_id`.
* **Approval never waits for research**: `workflow.approve_job` calls `tasks.on_approved` in a savepoint; research
  runs in the `job_insights` scheduler job with the same claim / lease / run-now / kill rules as JD recovery.

## Application Inbox (Gmail)

`app/services/application_inbox/` reads Gmail, read-only, for updates on submitted applications (README: Application
Inbox Agent). The rules a change has to keep:

* **Gmail processing is asynchronous.** The scheduler's `inbox` job and `POST /api/application-inbox/sync` (a background
  task) run `sync.sync_account`; no request reads Gmail or calls a model while it waits. An account's sync lease on
  `gmail_connections` keeps runs from overlapping.
* **Gmail is read-only for this worker.** The only scope is `gmail.readonly`, and `app/services/gmail.py` issues GET
  requests only, through `external_client` (providers `GMAIL`, `GOOGLE_OAUTH`). Never add a send, modify or label call.
* **Only submitted, tracked applications are monitored**, and searches are targeted (`queries.py`): a sender, a domain,
  ATS senders *with* the company name, or a reference — bounded by `after:`. Never list or download the mailbox.
* **Only emails relevant to tracked applications may be analysed.** A body is fetched only when the headers already
  point at an application; only MATCHED (or review-band) messages reach a model; unmatched ones keep ids only.
* **Gmail content is untrusted external data**: it goes through `untrusted()` in `app/prompts/application_inbox.py`, and
  instructions in it are never followed. Reusable prompts remain under `app/prompts/` (this repository's name for the
  prompt package).
* **Application-email matching must be confidence-aware, and never guesses** (`matching.py`): deterministic signals
  first; `APPLICATION_EMAIL_AUTO_MATCH_THRESHOLD` links, the review band waits for a person (the model may only confirm
  the deterministic favourite among the candidates it was shown), ambiguity or another tracked company → NEEDS_REVIEW.
* **LLM output never directly mutates tracker state.** The model recommends `recommended_tracker_action`;
  `stage_policy.decide` validates it and only `tracker.set_stage` changes a stage. Low confidence, non-explicit wording
  or an invalid transition → a stored suggestion, never a change. No response never means rejection.
* **Duplicate emails are never reprocessed**: `(user_id, gmail_message_id)` is unique on messages and events.
* **User corrections override automated analysis**: corrections are recorded per field on the event
  (`corrections`, `user_corrected`) and `analysis.save_event` never overwrites a corrected field.
* **Gmail credentials must never appear in logs**: only the encrypted refresh token is stored; access tokens stay in
  memory; `redaction.py` knows Google token shapes (`ya29.`, `1//`, `GOCSPX-`).
* **Follow-Up must check inbox events before drafting or sending**: `create_due_followups` calls
  `application_inbox.service.blocks_followup`; a recruiter reply cancels the pending follow-up.
* **Gmail failures do not affect unrelated workers**: Gmail, OAuth and rate-limit errors end that account's sync with a
  status (`failed`, `connection_required`, `rate_limited`); a model failure leaves the message PENDING for retry and
  never loses it.

## Follow-ups (Tracker and MCP)

`app/services/followups.py` backs the MCP follow-up tools and the Tracker's Mark sent / Skip (README §10, Follow-up
workflow). The rules a change has to keep:

* **Job Hunt OS never sends a message.** The service records what a person did; there is no send path, and Gmail stays
  read-only.
* **One writer for a person's follow-up decisions.** Saving a person's (or Claude's) draft, Mark sent and Skip go through
  `followups.save_draft` / `mark_sent` / `skip`, from MCP and from the web alike, so both write the same rows. The other
  writers stay what they are: the scheduler's AI draft (`workflow.draft_followup`) and the Application Inbox's
  cancellation (`analysis.suppress_followup`).
* **No model in the follow-up tools.** `followups.due` only reads; the scheduler alone drafts with a model, and only for
  what `followups.needs_draft` says (no open draft, fewer than `MAX_SENT` sent).
* **Recording a send is idempotent** (`ALREADY_SENT`), schedules at most one more follow-up (never more than `MAX_SENT`
  per application, none once `blocks_followup`), and its timeline event holds the channel and recipient, never the text.
* **Audit rows hold IDs, counts and statuses only**: message text and reasons are reduced to their length
  (`conversation_actions.summarize_arguments`).

## AI Orchestration

`app/services/orchestration.py` (runtime) and `app/services/orchestration_topology.py` (the stage registry) back the
AI Orchestration workspace (README §9a). It is a read-only projection; the rules a change has to keep:

* **Orchestration owns no state.** It reads business tables, Monitoring telemetry and the active AI configuration. Do
  not add a packet, workflow-event or orchestration table while those sources can answer the question; if one truly
  cannot, add the fact at the shared layer that owns it (a transition in `work_queue.transition`, a task column), not
  a parallel log.
* **Every `/api/orchestration/*` endpoint is a GET, and the graph changes nothing.** Its Models & routing workspace
  (`app/static/ai_models.js`, `#orchestration/models`) is the one place models are managed, over `/api/settings/ai/*`;
  model changes keep going through draft → preview → apply → version.
* **Deterministic and never inferred.** Node health follows `HealthPolicy` (thresholds in one place, shown in the UI).
  The actual model comes only from `llm_request_logs.model`; unknown stays unknown. Edge timing is shown only when
  both timestamps live on the same row. No model is ever called to render or describe anything.
* **Metadata only.** No prompt or response text, resume, cover letter, email subject/sender/body, application answer
  or credential in any orchestration payload; error text goes through `redaction.redact_text`. The every-account view
  (admin) returns aggregates only — titles, packets and traces stay the viewer's own.
* **Bounded.** Every query has a limit; the graph is cached briefly; the live poll reads active task rows only;
  history and node detail load lazily.

**Any new AI workflow stage must:**

1. declare a stable worker identifier (`app/services/ai_workers.py`) and stage identifier (a `Node.id` in
   `orchestration_topology.NODES`; never rename one),
2. call models only through the centralized LLM gateway (`LLMClient.generate_prompt`), so its requests are recorded
   with their worker, stage and models,
3. move or record workflow state through the shared layer (`work_queue.transition`, its task table, the tracker) so
   orchestration can place a job there,
4. be registered with the topology: its `types` (honestly — a deterministic step is not `AI`), `workers`,
   `llm_stages`, `external` matches, `states`, and the edges it takes part in, each with the stored fact it counts
   (`measure`); `orchestration_topology.validate()` must stay empty,
5. never expose secrets or prompt bodies through orchestration APIs.

The frontend (`app/static/orchestration.js`) renders whatever the registry returns; do not hard-code stage names in
other static files.

## Running workers

`app/services/workers.py` and `app/workers_api.py` back Running workers (README §9b). The rules a change has to keep:

* **Every queued worker registers a `WorkerKind`** (task model, statuses, max attempts, scheduler job, orchestration
  node). The screen renders whatever the registry returns.
* **A queue without a job is still a `WorkerKind`.** The retrieval indexer's entries (`rag_index_entries`, kind
  `indexing`) belong to an account and a source row: the kind sets `subject` (a row's label — ids and the index version,
  never the source's text — and the embedding model), `search`, `requeue` (Retry and Run now put the same row back;
  there is never a newer task), `stopped_status` and `shown` (its finished statuses in the shared words). Its rows carry
  `job_id` None, so never key a control on `task.job_id` without checking `WorkerKind.job_bound`. Edits and sweeps
  leave a stopped entry stopped; un-verifying or deleting its source still deactivates or removes it.
* **A claim may be a batch.** The indexer leases one embedding request's entries under one token; `Claimed.task_id` is
  that token and `rag_index.renew` is its `run_live` heartbeat. Run-now entries are claimed in a batch of their own, so
  only they skip the cooldown.
* **A worker's claim honours `run_now_at`:** order run-now tasks first, clear the column on claim, carry `run_now` on the
  claim, and run that task inside `backoff.bypass(claim.run_now)`. Never bypass a cooldown any other way.
* **Every write a worker makes after its model or network call stays lease-guarded** (a lock token or a per-claim holder
  name), so a kill is always safe; a run that commits business data from its own session checks its holder inside that
  same transaction (tailoring: a `before_commit` guard), never only at a checkpoint before it.
* **Kill stops, it does not requeue.** A stopped task ends with `lifecycle.STOPPED_BY_USER` and every sweep must leave
  its job alone until a person asks again (Retry, Run now). A new automatic sweep has to honour that.
* **Kill and run-now only move the account's own task rows** (and, for kill-all, clear its cooldowns). They never
  change configuration, never delete history, and never move a job past a state the workflow would not.
* **Worker settings have two scopes.** An account's own (`app/services/worker_settings.py`: run automatically, attempts,
  retry waits, recovery's fallbacks) are read per task with `worker_settings.get/for_user`, never cached across accounts,
  and a value never set follows the server. Server-wide ones (scheduler intervals, batch sizes, concurrency) are read
  through `server_settings.value(name)` — never `get_settings()` directly for an editable name — and only an admin
  changes them. Adding a setting means adding it to `FIELDS` / `EDITABLE`; the screen renders whatever they describe.
* **A queued worker's scheduler pass goes through `scheduled_jobs.run_live`**: it claims tasks as slots free up,
  re-reads its server limits every few seconds and passes a `heartbeat` (`scheduled_jobs.renew`) that renews held
  leases and lets the pass drop a stopped task. Never claim a whole batch up front behind a fixed semaphore.
* **Long blocking work runs on `threads.run_work`, scheduler bookkeeping on `threads.run_io`** — never on the default
  `asyncio.to_thread` pool, which a few slow model calls can fill and stall every scheduled job.
* **Bulk retry is many single retries** (`workers.retry`): each task goes through `_requeue_finished`, is skipped with a
  reason rather than failing the batch, and never gets `run_now_at` — a bulk retry never bypasses a provider cooldown.
* **A task row's model is read, never decided.** `workers.attach_models` (matched through the kind's `llm_link`: the
  job, or the resume for resume parsing) adds `model` (route, model, how it was chosen;
  `source` is `used` from the task's own `llm_request_logs` rows, or `next` from `LLMClient.route_for`) and
  `model_change` (FAILOVER, RESTING, FALLBACK, ESCALATED, CONFIG) to active rows. One bounded query, metadata only, and
  it never raises. Finished rows keep the model their own last request used (`source` `used`, or `none` when the task
  never called a model) and only that request's change (FAILOVER, FALLBACK, ESCALATED); a failover's origin is the
  request's own `llm_request_logs.failed_over_from`, never today's configuration.
* **A paused worker holds its queue, never drops it.** Its claim skips the account's tasks unless `run_now_at` is set
  (ranking: unless the task is forced).

## Agent graphs (LangGraph)

`app/agents/` is a small LangGraph runtime (README: Agent graphs; `docs/architecture/agentic-roadmap.md` Phase 1).
Only a no-side-effect pilot graph uses it so far, and everything is off unless `AGENT_GRAPHS_ENABLED`. The rules a
change has to keep:

* **A checkpoint is computation, never business state.** Job states, approvals, applications and candidate facts stay
  in their own tables and change only through their services (`work_queue.transition`, `workflow`, `apply_queue`). A
  graph node calls those services; it never writes a lifecycle column itself.
* **Graph state is small and secret-free.** Keep it to ids, enums, counts and short codes; no document, resume, page,
  email body, prompt or answer. `checkpoint.GuardedSerializer` refuses a credential-named field, anything
  secret-shaped and anything over `AGENT_CHECKPOINT_MAX_BYTES` before it is written. Do not widen that check to make a
  graph fit.
* **Thread ids are built on the server** (`threads.thread_id_for`: `graph:u<user>:<subject>:v<n>`) and never taken
  from a caller. A caller names a run by its `agent_threads.id`. **Ownership is checked before every checkpointer
  access, on start and on every resume** (`threads.owned`: the account's session, plus the owner in the thread id).
  Listing reads `agent_threads` only, never the checkpointer.
* **Every side effect goes through `effects.once(db, run, key, summary)`.** LangGraph replays a node whose checkpoint
  was not written, so a node never just "does" something. `once` checks the lease in the same transaction and records
  the effect under a key that is unique per thread. A run that lost its lease (it was stopped, or taken over after its
  lease expired) raises `LeaseLost` and writes nothing.
* **The existing scheduler dispatches.** API starts and resumes claim the thread's lease (`runtime.claim`, a guarded
  UPDATE on `lock_token`) and run as background tasks. The `agent_graphs` job continues runs whose lease expired from
  their last checkpoint. No other queue. Before a graph does production work it registers its `WorkerKind` and
  orchestration node like any queued worker. The pilot is exempt (ADR-020).
* **LLM calls go only through `app.agents.llm.GatewayChatModel`**, which is `LLMClient.generate_prompt` behind
  LangChain's interface. It is bound to one registered `app/prompts` prompt and refuses any other messages. It has no
  `model` argument and records nothing itself, so routing, failover, escalation, validation and telemetry are exactly
  the gateway's. Never put a provider chat model (ChatOpenAI, ChatAnthropic, …) in a graph.
* **LangSmith goes only through `app/agents/tracing_boundary.py`** (`tests/test_external_api_guard.py` enforces it).
  Its client sends through `external_api.requests_session` and applies `app/agents/export_policy.py`:
  * allowlist hooks for inputs, outputs and metadata;
  * error class names only;
  * a final allowlist of run fields;
  * a fail-closed wire check on the bytes sent.

  Thread ids are exported as keyed pseudonyms. **No setting widens the export**, including `LLM_PROMPT_DEBUG`, the
  capture switches and `EXTERNAL_API_DEBUG_PAYLOADS`. Passwords, OTPs, cookies, tokens, authorization headers,
  browser profiles and session storage never leave the process. A graph's `EXPORT_FIELDS` names the state keys a trace
  may show; only add plain ids, enums and counts. Each run happens inside `tracing_boundary.export(...)`, so
  environment tracing variables (`LANGSMITH_TRACING`) cannot add an unpoliced tracer. An export failure never changes
  a run.
* **Retention:** the cleanup job deletes checkpoints of finished threads after `AGENT_CHECKPOINT_RETENTION_DAYS`,
  expires threads waiting for a person after `AGENT_INTERRUPT_EXPIRY_DAYS`, and removes checkpoints whose account is
  gone. LangGraph owns its checkpoint tables (`PostgresSaver.setup()`); Alembic ignores them (`app/migrations/env.py`).
  Never hold a database transaction open while opening a saver: its setup builds indexes concurrently.

## Qualification graph (Phase 3)

`app/agents/qualification.py` (the graph) and `app/services/qualification.py` (its queue, shadow comparison and
hand-back) qualify a job when `QUALIFICATION_GRAPH_MODE=on` (README §9c). The rules a change has to keep:

* **The graph conducts; it does not reimplement.**
  * Every node calls the service that owns the step: `domain_match.screen`, `search_scope.screen`,
    `jd_recovery.enqueue`, `job_description.queue_parse`, `ranker.prepare`, `rag_retrieval.scope`, `ranker.queue_job`.
  * The existing workers do the HTTP, browser and model work under their own task rows and leases. The ranker still
    decides (`ranker.qualify` through `work_queue.transition`).
  * A graph node never calls a model, a browser or Apify, and never writes a lifecycle column. The one exception is
    a shadow run's budgeted re-ranking, through the ranker's own `LLMClient` and never saved.
* **Admission first, for every job.** The admission node runs before recovery, parsing, the browser or any model,
  including old jobs from `ranker.sweep`. A hidden job (`NOT_MATCHING`) ends the run with no task created. In mode
  `on`, `discovery.admit` dispatches to the graph instead of queueing recovery or a parse. The backlog sweeps
  (`jd_recovery.sweep`, `job_description.sweep`, `ranker.sweep`) leave pre-rank jobs to the graph, and also jobs it
  has run (`qualification.untouched`). `jd_recovery.apply_recovered` leaves a job the graph holds
  (`qualification.owns`) to the graph.
* **Waiting is an interrupt, not a poll.** A node that queued work returns `waiting_for`; the `wait` node
  interrupts, and the task goes back to PENDING without using an attempt. The claim skips a job with an active
  recovery, parse or ranking task (`qualification._busy`). The recovery, parse and ranking passes nudge
  `qualification` when they end.
* **Every graph-side write is lease-guarded and recorded once.** `effects.within` locks the thread row, records
  the effect key and makes the business write in one transaction (`recovery.enqueue:<job>`, `parse.enqueue:<job>:<hash>`,
  `rank.enqueue:<job>`). The task is settled under its own lock token. A stopped or taken-over run writes nothing.
* **Worker-owned threads.** The qualification graph is `api_resumable=False`. `/api/agents/runs/{id}/resume` refuses
  it, `runtime.claim_stale` never continues it and `runtime.prune` never expires it while it waits for work: only its
  task's worker does (`runtime.WORKER_OWNED`). A thread key
  carries the task's id, creation time and attempt.
* **Shadow never writes and never pays by default.** A shadow session refuses to commit and is rolled back.
  Predictions use the deterministic functions and the latest stored ranking. A paid re-ranking needs
  `qualification_shadow_paid_daily_limit > 0`, reserved per account under a transaction lock.
* **Kill stops; nothing revives it.** Kill ends the task with `STOPPED_BY_USER` and stops its thread and the job's
  active ranking (as Kill on Ranking would), so no model decides a job a person stopped. `dispatch`
  never creates a new run for a job whose latest run a person stopped, or that failed its attempts, unless they ask
  (Retry and Run now create a new task); otherwise every `ranker.sweep` would start another run and the attempt limit
  would bound nothing.
* **Off is the old path.** With mode `off` every hook calls exactly what it called before. Leftover ON tasks are
  ended `ROUTED_TO_LEGACY` by the next pass.
* Re-ranks (`resume_changed`, `_queue_stale`, `_recheck_salary`, `follow_setup`) stay on the existing services in
  Phase 3.

## Preparation and insights graphs (Phase 4)

`app/agents/preparation.py` / `app/services/preparation.py` (resume preparation, `PREPARATION_GRAPH_MODE`),
`app/agents/job_insights.py` / `app/services/job_insights/graph.py` (research, `JOB_INSIGHTS_GRAPH_MODE`) and
`app/services/approval.py` (the approved packet) (README §9c). The rules a change has to keep:

* **The preparation graph conducts; it never tailors, reviews or ranks itself.** TAILOR queues `tailor_tasks.enqueue`
  (option `conducted`, and `revise_from` on a revision) and waits; the tailor worker makes the model calls, drops
  unsupported units and saves a new version. A graph node never calls a model, never queues a ranking (no analysis,
  or one made from another resume, ends the run) and never writes `Resume`.
* **An evidence problem always beats the reviewer.** VALIDATE_FACTS re-runs `tailoring_results.validate_tailored_resume`
  on the saved version and checks its lineage (`lineage_matches`). Any unsupported unit or a broken lineage is
  RETAILOR whatever the reviewer recommended (`overrule`, recorded as disagreement). The decision itself is
  `review_results.decide_review` / `apply_review`, the same rules Claude Desktop's reviews go through.
* **The revision loop is bounded and never passes by exhaustion** (`next_step`): RETAILOR goes back to TAILOR while
  `revision < max_revisions` (per account, `worker_settings` `preparation`), then waits for a person as NEEDS_HUMAN. A
  person's send-back counts toward the same bound. A revision pass gets the previous review
  (`review_results.retailor_feedback`), never new facts.
* **A person decides at AWAIT_APPROVAL, in the existing screens.** `approve_job`, `reject_one` and `send_back_one` call
  `preparation.on_decision` (savepoint, never fails the person's action); the claim skips a run waiting for a person
  until then. The graph is worker-owned (`api_resumable=False`). Auto-approval in automatic mode happens only on a
  PASS, in DECIDE; a conducted tailor task never auto-approves.
* **Approval moves the canonical state, or is refused.** `approval.reconcile_to_ready` walks only transitions a
  stored fact backs (a saved version → TAILORING, TAILORED; its review → REVIEWED; then READY_TO_APPLY), recorded as
  `reconcile` with the fact. No such path → `ApprovalRefused` (409) with nothing written. Never forge a stage without
  its fact, and never set `Job.state` around `work_queue.transition`.
* **The approved packet is immutable.** `approval.snapshot` stores the content and its digest (`canonical`,
  `SERIALIZER_VERSION`; bump the version for any format change) inside `approve_job`. An edit of an approved packet
  goes through `approval.guard_edit` in the same transaction (revoke: READY_TO_APPLY → REVIEWED, back to the approval
  queue); a submitted packet cannot be edited. Export, the apply queue and MCP packet issuance use `approval.verify` /
  `check_export`: a mismatch needs a new approval, never a new digest. A new in-place edit path must call
  `guard_edit`.
* **The insights graph runs the research service's own steps** (`service.load`, `collect`, `store`, `synthesis.run`,
  `settle`) on the same task row and claim (`InsightClaim`); every write stays lease-guarded by the claim's token.
  Research starts only from `tasks.on_approved`. Step records commit with the step's write (`store` / `settle`
  `within=`), the validated synthesis waits on `job_insight_tasks.staged`, and nothing but counts enters the
  checkpoint (the snapshot holds the Apify token).
* **VALIDATE_PROVENANCE drops, never relabels** (`job_insights.provenance.enforce`). Its rules: employer pay only from
  EMPLOYER / JOB_DESCRIPTION, market only from `MARKET_SOURCES`, official values only from OFFICIAL_COMPANY, culture
  only from employees, REPORTED questions only cite INTERVIEW_QUESTION observations, GENERATED items carry no source.
  Off mode is `service.process` exactly; `settle`'s new arguments default to the old behaviour.
* **Shadow never writes and never pays.** Preparation shadow runs only the deterministic nodes in a session that
  refuses to commit; insights shadow runs on an in-memory checkpoint after the current path finished.
* **Evaluations are hand-labelled and run on mock providers** (`app/services/preparation_eval.py`,
  `tests/fixtures/preparation_eval/dataset.json`). Targets call the graphs' own pure rules and the real checks, never a
  stand-in. Nothing uploads to LangSmith; `--jsonl` is for a manual import.

## Application execution and pre-submit claims (Phase 5)

`app/services/apply_queue.py` (readiness, posting identity, claims, the one result recorder),
`app/services/application_execution.py` / `app/agents/application.py` (the execution worker and its graph),
`app/services/answer_resolver.py`, `app/services/ats_forms/` and `app/infrastructure/application_browser/` (README
§10a). The rules a change has to keep:

* **Only the guarded path clicks a final Submit (Phase 6, next section).** OBSERVE and ASSIST fill a form at most and
  end at READY_TO_SUBMIT; a person submits. The execution stages SUBMITTING, CONFIRMING and CONFIRMED are refused by
  `application_execution.set_stage` (and the column) unless a GUARDED_AUTO execution passes its own SubmissionIntent.
  `ApplicationPage` has no click, press, submit, tap or evaluate, and while Job Hunt OS drives a page the application
  browsers abort every non-GET request except a file upload and a SubmitTicket's click. `tests/test_no_automated_submit.py`
  allows exactly one `click` (GuardedPage.submit), one `.submit(ticket)` and one `signed_submit` (the SUBMIT node);
  never add another.
* **`apply_queue` stays the one domain boundary.** Readiness (`not_ready_reason`), duplicates (`applied_duplicate`,
  over `posting_identity`), blockers and recording (`record_submission`, `record_blocker`) are shared by MCP, the Apply
  kit, `POST /api/jobs/{id}/applied` and the execution worker. Never add a second readiness, duplicate or result path.
* **A claim before any external submit.** `apply_queue.begin_submission` takes the account lock, re-checks readiness
  (including the approved packet's digest) and reserves every alias key of the posting (`application_claim_keys`,
  unique per account while held), returning a token bound to the job and the approved digest. `lock_application`
  serialises *recording*; it is not a pre-submit reservation. Never hold a transaction across a browser.
* **An expired claim is UNCERTAIN, never free.** `expire_claims` moves a CLAIMED claim past its lease to UNCERTAIN; only
  a person resolves it (`resolve_uncertain`: submitted → recorded through `record_submission`, not submitted →
  released). A release is only ever a client saying nothing was submitted, and only before its lease runs out (whether
  or not the sweep has run). A RECORDED claim keeps its keys forever. Every claim transition re-checks the status it
  starts from under the lock (expiry is a conditional UPDATE), so a later writer never undoes a finished claim.
* **Tokenless reports are checked, not trusted.** A result reported without a claim token (older MCP clients, the in-app
  "I applied") goes through the same lock, digest, duplicate and held-key checks, is refused while another claim holds
  the posting, and is recorded with `claim: "unclaimed"`. Submissions made entirely outside Job Hunt OS cannot be
  serialised; say so, and reconcile instead of promising exactly-once.
* **The packet is verified at every use.** Enqueue captures the approved digest; FILL re-verifies it
  (`approval.verify`, and the digest equal to the one captured) before touching a form; a mismatch is BLOCKED
  `PACKET_CHANGED` and needs a new approval. Never re-hash a packet to make it current.
* **Answers come from verified sources only** (`answer_resolver`): profile contact fields, the approved packet's files
  and letter, a verified KnowledgeAnswer for the same question (`knowledge.same_question`, never a similar one, for
  sensitive answers too). A sensitive answer needs the person's confirmation for this execution, given after the Apply
  kit showed the question and the saved value; anything else a *required* field needs is saved with `knowledge.request_answer` and the execution
  waits (NEEDS_INPUT). Optional unknown fields stay empty. No model writes an answer in Phase 5. Answer values are
  resolved at fill time and never stored on the execution: its `plan` holds sources and statuses only.
* **The application browser is not the JD-recovery browser.** Application pages come only from
  `app.infrastructure.application_browser` (`ApplicationSessionManager`). Its only implementation is ephemeral: a fresh
  context per step, no storage state, cookies, credentials or profile, closed in `finally`. It never passes anything
  to `BrowserService.fetch_rendered_page` or JD recovery, and JD recovery never receives an application context.
  It never holds a saved login: saved ATS sessions exist only for the browser-use runner, per account and domain
  (Phase 6, ADR-035). A login wall, OTP or CAPTCHA pauses as NEEDS_HUMAN; it is never solved or retried.
* **The execution worker is a normal queued worker.** `application_execution_tasks` (one active per job), WorkerKind
  `application`, scheduler job `application_execution`, orchestration node `application_execution`, worker-owned graph
  `application` (`api_resumable=False`; thread key `application:<job>:<task>:<created>:a<attempt>`). Kill stops the
  task and its thread; Retry and Run now create a new task. Everything is off unless `APPLICATION_EXECUTION_MODE` is
  `observe` or `assist`, `AGENT_GRAPHS_ENABLED` is on and the database is PostgreSQL (screens show the mode only where
  it can run: `available_mode`); while it is off, the always-on `applications` pass ends the runs left over. Graph state
  holds ids, enums and counts only — never form values, page HTML or answers. ASSIST fills a page that is closed when
  the step ends: READY_TO_SUBMIT means every required field could be filled from verified sources, not that a filled
  form is waiting anywhere. Never present it as one; a person fills and submits on the site.

## Guarded submission, the browser-use backend and ATS logins (Phase 6)

`app/services/submission.py` (PreSubmitValidator, SubmissionIntent, ConfirmationVerifier, reconcile),
`app/infrastructure/application_browser/guarded_submit.py` (the one page that clicks),
`app/services/application_backends.py`, `app/services/browser_actions.py`, `app/prompts/browser_navigation.py`,
`app/infrastructure/application_browser/browser_use_sidecar.py` + `sidecars/browser_use_runner/runner.py`,
`app/services/ats_map.py`, `app/services/ats_auth.py`, `app/services/browser_use_webui.py` (README §10b, ADR-031 –
ADR-035). The rules a change has to keep:

* **Auto submit is per account, and stops only for the person.** `guarded_auto` is auto submit:
  * it needs the server mode (`APPLICATION_EXECUTION_MODE=guarded_auto`) and the account's switch (Settings → Auto
    submit, `submission.consent`, re-read at every check). `autonomous` is refused when settings load;
  * with both on, approving a job queues its run (`application_execution.on_approved`, in a savepoint: approval never
    fails because of it), and a run named by no one gets GUARDED_AUTO (`default_mode`);
  * it covers every route a browser can complete (`AtsMatch.auto_submit`): the Greenhouse adapter on Playwright, or the
    browser-use backend for any other form. LinkedIn, Indeed and Ashby are never automated;
  * anything only the person can settle (a question with no verified answer, an attestation they have not approved, a
    sensitive answer, a login, a code, a CAPTCHA, no single Submit control) hands them the window with the reason on
    the page; Continue checks everything again and submits.
* **The order is fixed and every step is re-checked.**
  1. `PreSubmitValidator.check`.
  2. `apply_queue.begin_submission(client="worker", execution_id=…)` — the shared claim, never a second mechanism.
  3. A SubmissionIntent ARMED.
  4. The page read again.
  5. `PreSubmitValidator.check` again (with the claim).
  6. `commit_clicking`: CLICKING + SUBMITTING in one commit, under the run's lease.
  7. The click: `GuardedPage.submit(ticket)` (Playwright), or the runner's `submit` with `submission.signed_submit`
     (browser-use) after `still_clicking`. Either way the intent is re-read first.
  8. `after_click`: CONFIRMING, READY_TO_APPLY → APPLYING.
  9. `ConfirmationVerifier`.
  10. `confirm` (through `record_submission`) or `uncertain`.

  No transaction is held while the browser works.
* **After CLICKING, the outcome is a fact to establish, never a retry.** A crash, a timeout, a Kill, a lease running out
  or an ambiguous page makes the intent and the claim UNCERTAIN (`submission.reconcile`, `recover_expired`,
  `on_stopped`). A replayed SUBMIT node never clicks again (`submission.clicked`). Only an ARMED intent (nothing
  clicked) is aborted and its claim released. Only a person resolves an uncertain submission, and only a person moves
  APPLYING back to READY_TO_APPLY (`lifecycle.PERSON_ONLY`, enforced in `work_queue.transition`).
* **Confirmation needs the site's evidence.** It needs all three:
  * a 2xx/3xx non-GET response from the application's own site after the click (`same_site`; uploads never count);
  * the form gone;
  * a confirmation path or the site's confirmation wording.

  A click, a navigation or a success-looking page alone is never enough. The evidence stored is metadata (statuses,
  hosts, flags, a confirmation id), never page text.
* **One recorder, two provenances.** Every submission is recorded by `apply_queue.record_submission`, which moves
  READY_TO_APPLY → APPLYING → APPLIED (`workflow.advance_to_applied`, never a forged step) and labels the event
  `evidence: verified_browser` only when the worker passes its own claim token and the verifier's facts. MCP, the Apply
  kit and `POST /api/jobs/{id}/applied` are always `reported`.
* **Route exclusivity.** While a GUARDED_AUTO task is active for a posting (any alias), `begin_submission` refuses every
  other client (`ROUTE_HELD`) and MCP `get_job_packet` refuses the packet. A guarded enqueue is refused while any claim
  on the posting is open or uncertain.
* **The browser-use backend never decides, and submits only with a signed token.**
  * The runner is a child process per execution, started with `BROWSER_USE_PYTHON`. It speaks JSON lines and has no
    port.
  * Its environment holds only PATH and HOME. Telemetry is off. Chromium is launched plainly: no debugging port, no
    anti-detection flags (browser-use's own launcher is not used).
  * It reuses browser-use's DOM indexing and element actions only: no browser-use Agent, no LLM.
  * While Job Hunt OS drives, it aborts every non-GET except:
    * the upload a file selection starts: an XHR/fetch PUT or multipart POST, for the seconds after `upload`
      (Greenhouse sends the file to its storage bucket);
    * during a signed SUBMIT click, whatever the page sends (its CAPTCHA script included);
    * during a signed LOGIN click, the login domain only.

    It refuses a submit-like click without a token (a `<button>` with no type inside a form is a submit), refuses
    navigation off the allowed hosts, uploads only from the run's packet folder and fills password fields only through
    `login_fill`. An upload counts only once the site took it (`UPLOAD_FAILED` otherwise); a board that replaces the
    input with the file's name is read from `observe`'s `attached`.
  * Like the shared browser, it never reaches internal services: every request is checked by host name and literal
    address, and every navigation (a frame's too) by the addresses its host resolves to (`_resolves_private`).
  * A LOGIN token works only on a control of the form that holds the password field, and `_login_parts` takes the
    username, password and button from that one form.
  * A saved session restores its cookies and its origins' local storage.
  * The only scripts it evaluates are its own constants.
  * Only `submission` signs SUBMIT (`signed_submit`, called once, by the SUBMIT node after CLICKING), for the form's
    own submit control (`_submit_control`: one submit-like element of the form that holds the fields). Only
    `application_backends` signs LOGIN.
* **Every navigator step is checked by Python** (`browser_actions.check`):
  * the element is in the latest observation;
  * an answer goes only into the element it was resolved for (the field's locator is that element's key), and only a
    RESOLVED answer;
  * a select value is one of the element's own options;
  * an upload is a packet file of the matching role;
  * a click never lands on a submit-like control, a link off the route, or an accept/agree/consent button
    (declining non-essential cookies is allowed);
  * three refusals end the run.

  The model's contract (`BrowserActionProposal`) has no field for a value and no submit action. Deterministic filling
  comes first; the navigator only reaches what it cannot.
* **Dropdown widgets (`role=combobox`, the newer Greenhouse board) are set only by the runner's `choose`.** It opens
  the widget, types the verified answer and clicks the option whose text matches it exactly; anything else is
  refused (`NO_SUCH_OPTION`). The Playwright path never clicks, so a form with a required dropdown goes to browser-use
  (auto submit included), or waits for the person (`DROPDOWN_FIELDS`) when browser-use is off. A widget's hidden helper
  inputs (`aria-hidden`, `tabindex=-1`, no id or name) are never fields. Each ephemeral-page field action waits at most
  `FIELD_TIMEOUT_MS`.
* **A person may send a run to browser-use** (`enqueue(backend="browser_use_webui")`, the Apply kit and the history
  panel): fill only (ASSIST) unless a mode is named, refused while another run of the job is active or when the runner is
  not configured; Retry and Run now keep that choice.
* **The Web UI's own HTTP interface is not an automation API.** `browser_use_webui.probe` GETs its `/config` for
  health and compatibility, nothing else. Never drive its Gradio endpoints (positional, auto-named, unauthenticated,
  one global session, no pre-action veto).
* **Logins are the person's, scoped to one domain** (`ats_auth`).
  * Passwords and sessions are encrypted (`encrypt_secret`) in their own owned tables, never in settings or
    preferences.
  * No API returns a password, except a signup's generated one, once. A password is decrypted only to fill that very
    domain's login form, on a domain in `APPLICATION_LOGIN_DOMAINS`, inside `redaction.holding`.
  * A saved session keeps only its own domain's cookies and origins and is used only by that account's runner for that
    domain (`APPLICATION_AUTH_SESSIONS_ENABLED`). A login is attempted once per run.
  * OTP, CAPTCHA, a failed login and every signup wait for the person. No run ever creates an account or accepts terms.
* **A code the site emails after Submit is the person's to enter** (`application_inbox/codes.py`). A click that ends on
  such a page (`ats_forms.asks_email_code`) is UNCERTAIN with `email_code` on the intent, and the window stays with
  the person. Only when they press "Get code from Gmail" does one targeted read-only search (the ATS's sender, mail
  after the click) find it, and only that response returns it, `NO_CAPTURE`. It is never stored, logged or put in graph
  state. The run never types a code, never imitates human timing, and never solves a CAPTCHA.
* **A window handed to the person is a normal browser.**
  * A headful session is parked for `APPLICATION_TAKEOVER_TTL_MINUTES` and then sends what the person sends (logins,
    uploads, their own submission); there is nothing to start first, and no Start submission in the Apply kit.
  * `watch_windows` (every `applications` pass) reads those windows' evidence and records a submission the site
    confirmed (`ConfirmationVerifier`): the run's own open claim is resolved as submitted, anything else is a
    `reported` submission; the run ends `SUBMITTED_IN_WINDOW`. It works as the window's own account.
  * The task waits PENDING at NEEDS_HUMAN, not claimable, until the person continues
    (`continue_after_takeover`), on the same thread: every step, SUBMIT included, pauses through `wait` and resumes at
    its `resume_at`. A run that hands over a window never ends and starts again.
  * Then the run picks the page up where the person left it (`resume_in_place`, set by `_park_or_close`; no reload, so
    a solved CAPTCHA, a sign-in and typed fields survive), observes it afresh and re-validates everything. It loads
    the apply URL again only when the planned form is no longer on that page or the window is gone. It never replays
    an old action.
  * **A CAPTCHA is the person's, and the run continues once it is answered.** The runner reports a challenge widget as
    booleans only (`captcha: {present, solved, kind}`, every frame, constant JavaScript; never the token; invisible
    v3 widgets are not a challenge), and `ats_forms.page_problem(captcha=…)` lets it decide over page text. While a
    window waits at a CAPTCHA, `watch_challenge` asks it every `APPLICATION_CHALLENGE_POLL_SECONDS` (a scheduler pass
    does the same as a fallback, `watch_challenges`) and continues the run when it is answered (`continued_by:
    window`). Nothing ever solves, clicks or retries a challenge.
  * A run waiting for an answer in the Knowledge bank (NEEDS_INPUT, browser-use) keeps the form it read for
    `APPLICATION_INPUT_HOLD_MINUTES` (`_keep_for_input`: a hold, never a takeover; non-GET stays blocked) and fills it
    in place after the answer. The ephemeral Playwright page is still read again.
  * An unclaimed takeover ends TAKEOVER_EXPIRED with the shared blocker. Kill closes the window.
* **Fields the run could not fill are pointed out, not guessed** (`FIELDS_NEED_YOU`, headful browser-use).
  * The runner outlines them with a banner (`point`, constant JavaScript, no click). The interrupt holds field ids and
    labels only.
  * Continue resumes in the same page (`session.resume_in_place`, no reload): the run removes the marks, fills what was
    answered meanwhile, and reads back every required field.
  * A required field with no verified source passes only when it now holds a value, and becomes `PERSON`. Auto submit
    accepts a `PERSON` field (and an attestation the person ticked) only after the person pressed Continue.
  * The person's values are never stored on the task, the checkpoint or a log. They are read from the window only when
    the person asks to save them to the Knowledge bank (`save-window-answers`, as their verified answers).
  * Headless runs end NEEDS_HUMAN and list the fields instead.
  * The run also continues by itself once every pointed-out field holds something unchanged for
    `APPLICATION_FIELDS_SETTLE_SECONDS` (`watch_fields`, with `watch_filled_windows` as the scheduler fallback). Value
    digests stay in the watcher's memory. Continue is also offered on the task's Running workers row.
  * Questions a page grows after ANALYZE_FORM (`application._plan_grown`) go through `answer_resolver.plan` like planned
    ones: verified saved answers are used, and unanswered required ones are saved to the Knowledge bank. They live in
    `plan["extra_fields"]`, never in `task.form` (its fingerprint stays). PreSubmitValidator and `save-window-answers`
    check and read them too.
* **The ATS map is facts, per account** (`ats_routes`, `ats_map.observe`): from posting URLs and run outcomes, never a
  model, never a guessed employer site. A login belongs to a route's `login_domain`: per tenant where the ATS has
  per-employer accounts (Workday, iCIMS, Taleo, SuccessFactors). Where employers share a host (SuccessFactors,
  `tenant_in_host=False`), a host cannot keep their logins apart: `login_domain` is None, nothing is saved or reused
  there, and a person signs in each time. Ashby (its form loads through POST), LinkedIn and
  Indeed are `unsupported`.

## Candidate knowledge retrieval (RAG)

`app/services/embeddings.py`, `evidence_tags.py`, `rag_index.py`, `rag_retrieval.py`, `vector_store.py` and
`rag_eval.py` back retrieval (README §9d). The rules a change has to keep:

* **Embeddings go through `EmbeddingService`, never `LLMClient`.** `LLMClient.generate_prompt` is for generation. An
  embedding call goes through `EmbeddingService.embed_documents/embed_query`, which uses the LiteLLM gateway's
  `/v1/embeddings` through the instrumented client. Never call a provider's embedding API from a service.
* **The embedding model is configuration.** It is the EMBEDDER worker's FIXED model (`ai_workers`, capability
  `EMBEDDING`, seeded off), changed through draft → apply.
  * Apply refuses an embedding model of unknown size, a chat model on the Embedder, and an embedding model on a chat
    worker.
  * An embedder has no fallbacks: vectors of another model cannot be compared. The other way round, no chat slot
    (primary, `fallback_model`, `secondary_models`, a model's own `fallback_models`) may hold an embedding model.
  * Never hard-code a model or a vector size outside tests.
* **Never guess an embedding cost.** The cost is what LiteLLM reports (`x-litellm-response-cost`), recorded on the
  external call row; when it reports none, the cost stays NULL. Embedding calls do not write `llm_request_logs`.
  Text and vectors are never logged: the request is sent with `external_api.NO_CAPTURE`, so neither an account's
  payload capture nor `EXTERNAL_API_DEBUG_PAYLOADS` keeps its bodies.
* **Never compare incompatible vectors.** An index version is one model × vector size × document format
  (`EmbeddingModel.identity_hash`; bump `RAG_DOC_FORMAT_VERSION` when `rag_index.evidence_text/answer_text` change).
  * A new identity builds a new version beside the SERVING one and replaces it only at full coverage, counted
    against the rows as they are when it is promoted (a re-sweep), never a count taken before the embedding calls.
  * Queries are embedded with the serving version's own model (`EmbeddingService.for_version`).
  * Every vector is checked against the configured size before it is stored.
* **Scope before scoring, for every job.** Resolve the job's resume with `resume_verification.for_job`, then take
  `evidence_tags.allowed_evidence_ids` for it, before any keyword or vector work.
  * A fact verified only on another resume is never a candidate, however similar it is.
  * A person's pin and a person's tags beat similarity.
  * Do not add a retrieval path that starts from the account's whole evidence bank.
  * In `on` mode, citations are checked against the same scope (`tailoring_results.verified_evidence`), and so is
    the evidence the verified-facts gate counts (`context_builder.scoped_evidence`).
* **Resume tags: the person's choice wins.** Automation writes AUTO links only (`sync_evidence`, extraction) and never
  recreates a link a person excluded. Only `evidence_tags` writes `evidence_resume_links`.
* **The index is derived and never authoritative.**
  * Retrieval reads the live Evidence row every time, and uses a vector only while its `content_hash` equals the live
    text's.
  * The flush listener in `rag_index` keeps entries in step with edits, verification changes and deletions. It does
    metadata-only writes and never calls a model in a request; a sweep repairs anything missed. Un-verifying a fact
    deletes its vector at once: a withdrawn fact keeps no vector.
  * Index writes after the model call are lease-guarded.
* **Explicit `user_id` in raw SQL.** Vector and full-text queries are `text()` SQL, which tenancy does not filter, so
  every one names the account. The rag tables cascade on account deletion.
* **Sensitive answers.** Semantic matches are suggestions, never answers: `knowledge.search` suggestions are
  `authoritative: false`, never change its status, and show no answer text for work-authorization, visa or
  sponsorship questions. A sensitive saved answer is embedded by its question only.
* **The deterministic builder is the fallback.** With `RAG_RETRIEVAL_MODE=off` the context builders must stay
  byte-identical to the old builders (a test checks this). In `on` mode a retrieval error falls back to them.
* **`rag_vectors` is created at runtime, not by Alembic** (`vector_store.setup`). A server without pgvector must still
  start; retrieval then stays full-text. There is no ANN index until a measured account needs one.

## Evaluation, feedback and the journey (Phase 7)

`app/services/journey.py` (the application journey), `app/services/agent_eval/` (datasets, suites, gates, report,
live feedback), `scripts/agent_eval.py` (README §9e). The rules a change has to keep:

* **No second pipeline.** The journey and the feedback rates read the rows each stage already writes. A new fact goes
  where its stage owns it (a nullable column written by `llm_telemetry.build_entry`, as `retrieval_ms` is), never a
  journey, packet-event or graph-state table.
* **Cost is counted once.** An LLM call's cost lives on its `llm_request_logs` row only; an external row adds its own
  cost only when no LLM row points at it (`external_request_id`). Unknown cost is None everywhere, never `0.0`.
* **Unknown is UNKNOWN.** A stage with no stored fact is `UNKNOWN`; nothing is inferred from a neighbour. The actual
  model is `llm_request_logs.model` / `model_group`; "off configuration" is computed only where both sides are known
  and never for the auto router.
* **The recruiter's decision is not a model error.** `recruiter_outcome` is reported beside the journey and the
  correction rates with `attributed_to_model: False`; no metric reads a rejection as a fault of a stage.
* **Gmail stays confidence-aware.** An ambiguous message is NEEDS_REVIEW and never an application's outcome
  (`journey.review_messages` finds it by its candidates); a person's correction wins and is labelled `person`.
* **Every suite runs production code over a versioned dataset**, and every suite has gates (`gates.GATES`). Bump a
  dataset's `version` when a case is added, removed or relabelled. A skipped suite is BLOCKED. Ground truth is
  written by a person; cases are synthetic. Add a gate before relaxing one.
* **The LangGraph view draws the code** (`app/services/graph_topology.py`): structure from each compiled graph,
  descriptions from each node function's docstring, pauses from `interrupt(`, the stage it conducts from
  `GraphSpec.orchestration_node`. A new graph appears there once it is in `runtime.GRAPHS`; give every node function a
  docstring (a test fails without one) and never draw or name a step in `app/static/`.
* **Nothing is uploaded.** `--jsonl` is for a manual LangSmith import; the only export path stays
  `app/agents/tracing_boundary.py`, which also refuses OTP- and token-shaped strings, cookie text and credentials in
  URLs or headers (`export_policy.request_check`). `tests/test_external_api_guard.py` checks `job_hunt_agent/`,
  `scripts/` and `sidecars/` for exporters too.

## Traces

`app/services/tracing.py` holds an ambient correlation ID. Scheduler jobs and HTTP requests open one; nested
work narrows it (`trace("ranking", job_id=...)`) while keeping the same ID. Services never pass it around, and
a new integration inherits it. Annotate what becomes known mid-flight with `tracing.annotate(job_id=...)`.

## Operational logging

`app/services/system_log.py` writes the Monitoring → Logs table: failed or slow external calls, every LLM call
(`llm_telemetry.log_call`, INFO on success, linked to its request row; `SYSTEM_LOG_LLM_CALLS=false` keeps only
failures), scheduler runs and workflow milestones. It is for events a person would want to see, not a debug
stream — ordinary successful non-LLM calls are already one row each in the request table.

## Prompts and LLM workers

Checklist and examples: `docs/prompts.md`. The rules a change must keep:

* **All reusable production prompts live under `app/prompts/`** — including retry and correction wording.
  `tests/test_prompts.py` fails the build on an inline prompt elsewhere. Prompt modules import no provider SDK.
* **Each LLM worker has a single responsibility.** The Ranker produces the FitAnalysis; the Tailor only presents
  verified experience and never scores, reranks, reclassifies requirements or fills gaps; the Reviewer checks and
  recommends, never rewrites. Say in the prompt what the worker is *not*.
* **Never pass an empty output template belonging to an upstream worker into a downstream prompt.** Missing upstream
  data raises `ContextError` (`RANKING_REQUIRED`, `TAILOR_CONTEXT_INCOMPLETE`, `REVIEW_CONTEXT_INCOMPLETE`, ...)
  before any model call.
* **Machine-facing workers use typed output schemas** (`app/prompts/contracts.py`, `Prompt.output_schema`). Valid JSON
  is not enough: contracts forbid unknown fields, do not share field names across workers, and the gateway diagnoses
  another worker's shape as `POSSIBLE_CROSS_WORKER_OUTPUT`. Services use `result.validated`, never the raw dict.
* **External text is untrusted data**: job descriptions, emails, uploaded resumes, form questions, web text and another
  model's output under review go through `untrusted()`; instructions inside them are never followed.
* **Generated factual claims are evidence-backed**: each unit carries `evidence_ids`, and
  `app.services.tailoring_results` drops units whose evidence does not support them. A requirement without evidence
  stays missing.
* **Context builders remove irrelevant and disallowed information before prompt construction**: task projections,
  no search/scraper settings, unverified and rejected facts filtered by CandidatePolicy, provenance conflicts held
  back (`app.services.evidence_provenance`, which only diagnoses and never edits candidate history).
* **Workflow state transitions are deterministic Python.** A model may recommend (PASS / RETAILOR / SKIP, QUALIFY /
  ARCHIVE); code decides and moves the job.
* **Secrets are centrally redacted before logging** (`app/services/redaction.py`): telemetry, debug text, system logs
  and Monitoring payloads. Never add a logging path that bypasses it.
* Bump a prompt's `*_PROMPT_VERSION` when its behaviour materially changes; outputs store prompt version, provider,
  model and config version. Add a golden fixture (`tests/fixtures/prompt_golden/`) for a new worker.

## Model performance

`app/services/model_performance.py` turns raw `llm_request_logs` rows into the comparison Monitoring → Model
performance and the AI Orchestration nodes show. The rules a change has to keep:

* **Aggregates only ever come from the raw request rows.** No second telemetry table, no counters, no call to a model
  or to LiteLLM, and the browser never talks to LiteLLM. A new metric starts as a nullable column written by
  `llm_telemetry.build_entry` (from `Attempt`/`Usage`), then an aggregate in `model_performance.block`.
* **Unknown is None, never 0.** A figure a provider did not report (TTFT, generation time, reasoning tokens, cost)
  stays NULL and is left out of its own average; add the sample count next to any new rate. Never derive a number from
  something that is not the same quantity (no tokens/sec from wall-clock latency alone, no guessed price).
* **Six dimensions, no composite score**: reliability, latency, throughput, cost, quality, context. Do not add a
  single "AI quality score". Quality uses signals the product really stores; list a missing one in
  `model_performance.UNSUPPORTED` instead of showing it as zero.
* **Timing facts are best effort.** Reading gateway headers (`llm._gateway_timings`) and recording them must never
  fail or slow a request; it catches its own errors like the rest of telemetry.
* **Distinguish configured from actual**: the model shown on a node or in a table is `llm_request_logs.model` (what
  answered), with `configured_model` / `requested_model` kept beside it.
* Aggregation stays in Python over a bounded row set (`monitoring.MAX_ROWS`, `orchestration.LLM_ROW_LIMIT`), so
  PostgreSQL and SQLite agree; the column lists are `model_performance.COLUMNS` and never include prompt/response text.

## Telemetry tables and retention

| Table | Written by | Retention |
| --- | --- | --- |
| `external_api_request_logs` | `app/services/external_api.py` | `EXTERNAL_API_LOG_RETENTION_DAYS` (90), rolled into `external_api_usage_daily` |
| `llm_request_logs` | `app/services/llm_telemetry.py` (routing columns: worker, requested and actual model, tier, fallback, escalation; structured output: schema enforcement, validation failure reason, context builder version) | `LLM_TELEMETRY_RETENTION_DAYS` (90), rolled into `llm_usage_daily` |
| `system_logs` | `app/services/system_log.py` | `SYSTEM_LOG_RETENTION_DAYS` (30), deleted |
| `job_description_fetch_tasks` | `app/services/jd_recovery.py` (owned, in `OWNED_MODELS`) | kept: a job's recovery history and the JD recovery metrics |

An LLM call writes one row in each of the first two: the generic row is the provider call, the LLM row is the
token and context detail, and `llm_request_logs.external_request_id` joins them. Do not record an LLM call
twice by wrapping it again at a call site.

## The AI Optimization Advisor recommends, never acts

`app/services/ai_advisor.py` may analyse, compare and recommend. It must never disable a provider, change an
actor, a scheduler interval, a model, a worker's model assignment, a tier, the auto router, or any credential or
configuration. Keep new rules deterministic and
grounded in the aggregates; add advisor instructions to `app/prompts/ai_optimization.py`, not to a service.

## Tenancy

Every user-owned table carries `user_id` and is registered in `app/tenancy.py::OWNED_MODELS`, which filters
every ORM query by the session's account. The AI model configuration tables (`litellm_connections`, `ai_models`, `model_tiers`, `worker_model_configs`,
`ai_routing_configs`, `ai_config_versions`, `ai_config_audit`) are owned and in that list, and so are the retrieval tables
(`evidence_resume_links`, `rag_index_versions`, `rag_index_entries`; the runtime `rag_vectors` table carries `user_id`
with an ON DELETE CASCADE and is filtered explicitly). The two telemetry tables
are deliberately not in that list — a call
can belong to no account — so `app/services/monitoring.py` and `app/services/external_monitoring.py` filter by
`user_id` explicitly in every query.

## Tests

`pytest` for the backend, `npx vitest run` for `app/static/*.js`. Ranking and cleanup tests need PostgreSQL
(`TEST_DATABASE_URL`) and skip on SQLite. Run both suites before finishing.
