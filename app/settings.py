from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Job Hunt OS"
    database_url: str = "sqlite:///./jobhunt.db"
    # Connection pool (app.db; server databases only, SQLite keeps its own). A worker holds one connection while its
    # model call runs and takes a second to record the answer, so size + overflow must exceed the workers running at
    # once (the *_CONCURRENCY settings, about 30 by default) with room for web requests — and stay under the database's
    # max_connections across every process. Too small and every thread waits DB_POOL_TIMEOUT_SECONDS for a connection.
    db_pool_size: int = 20
    db_pool_max_overflow: int = 20
    db_pool_timeout_seconds: int = 30
    app_base_url: str = "http://localhost:8000"
    cron_secret: str = "change-me"

    # Accounts (app.auth). SECRET_KEY encrypts the API keys users save; keep it stable and private.
    secret_key: str | None = None
    allow_signup: bool = True
    session_days: int = 30
    # Secure cookie flag. Unset = on when the request arrives over HTTPS.
    session_cookie_secure: bool | None = None

    # LLM provider: litellm | openai | anthropic | openrouter | gemini | mock. `litellm` is gateway mode: every
    # worker's model comes from AI Orchestration → Models and every request goes through the LiteLLM proxy.
    llm_provider: str = "openrouter"
    upload_dir: str = "./data/resumes"
    max_resume_upload_mb: int = 8

    openai_api_key: str | None = None
    openai_model: str = "gpt-5.6-luna"

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"

    # Direct Gemini (Google AI Studio / Gemini API key)
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"

    # OpenRouter is OpenAI-compatible. `openrouter/free` automatically routes
    # among currently available free models that can satisfy the request.
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "openrouter/free"
    openrouter_model_rank: str | None = None
    # Provider-independent model for the rank stage (any provider). A model saved in Settings still wins.
    rank_model: str | None = None
    # The same for the other stages. In LiteLLM gateway mode they seed the matching workers' model assignments
    # the first time an account's AI model configuration is created (app.services.ai_model_config).
    tailor_model: str | None = None
    review_model: str | None = None
    followup_model: str | None = None
    openrouter_model_tailor: str | None = None
    openrouter_model_review: str | None = None
    openrouter_model_followup: str | None = None
    openrouter_fallback_models: str = ""
    openrouter_require_free: bool = True
    openrouter_max_tokens: int = 5000
    openrouter_site_url: str | None = None
    openrouter_app_name: str = "Job Hunt OS"
    # Every provider call: request timeout and SDK-level retries (the workers retry on their own too).
    llm_request_timeout_seconds: int = 120
    llm_max_retries: int = 1

    # LiteLLM gateway (AI Orchestration → Models). These are bootstrap values: an account's saved connection wins.
    # LITELLM_API_KEY is copied (encrypted) into the FIRST account only, like the provider keys above.
    litellm_base_url: str | None = None
    litellm_api_key: str | None = None
    # Used when a worker has no usable assignment yet (for example a tier nobody has mapped to a model).
    litellm_default_model: str | None = None
    # Seed values for the default tiers, as LiteLLM model names (aliases), e.g. tier-1-fast. Blank = unmapped.
    litellm_tier_fast: str | None = None
    litellm_tier_primary: str | None = None
    litellm_tier_deep: str | None = None
    litellm_auto_router_model: str | None = None
    # How Job Hunt OS applies the parts of its configuration that live inside LiteLLM (managed deployments and
    # the auto router): admin_api (needs STORE_MODEL_IN_DB=True on the proxy), config_file, or disabled.
    litellm_sync_mode: str = "disabled"
    # config_file mode: the one file Job Hunt OS owns. The proxy's own config must `include:` it; Job Hunt OS
    # never edits that main config. Operator-level on purpose: a user cannot point the app at another path.
    litellm_managed_config_path: str | None = None
    # Optional: the main proxy config, read (never written) to check that it includes the managed file.
    litellm_config_path: str | None = None
    # Optional command that restarts the proxy after the managed file changes (split like a shell, not run by
    # one). Without it a file change waits for a manual restart and shows as PENDING until verified.
    litellm_restart_command: str | None = None
    litellm_restart_wait_seconds: int = 60
    # The local Ollama server whose models the "Add local Ollama model" form lists. Operator-level: the app only
    # ever fetches this URL, never one a user types in.
    ollama_base_url: str = "http://localhost:11434"
    # Management calls (discovery, sync, verification); inference uses llm_request_timeout_seconds.
    litellm_admin_timeout_seconds: int = 15
    # How long a worker's resolved model configuration is cached in-process. Every applied change clears the
    # cache at once in this process; other processes pick it up within this many seconds.
    ai_config_cache_seconds: int = 30

    # LLM monitoring (app.services.llm_telemetry). Telemetry stores metadata and token counts, never prompt text,
    # unless LLM_PROMPT_DEBUG is on. Raw request rows older than the retention are rolled up into daily totals.
    llm_telemetry_enabled: bool = True
    llm_telemetry_retention_days: int = 90
    llm_prompt_debug: bool = False
    # Full prompts and responses (LLM_PROMPT_DEBUG, or an account's own switch in Monitoring) hold resume and job
    # text, so they are cleared from request rows after this many days; the rest of the row stays until the
    # retention above.
    llm_prompt_debug_retention_days: int = 7
    # Model prices and context windows (app.services.model_catalog). Blank uses app/model_catalog.json.
    model_catalog_file: str | None = None
    # The model market (app.services.model_market, MCP search_models / suggest_models): public model catalogs read
    # for suggestions only, cached in-process. Off: only your gateway, your local Ollama and your own telemetry.
    model_market_enabled: bool = True
    model_market_cache_hours: float = 6
    # Worker model suggestions (AI Orchestration → Workers) may also use how a model did across every account on this
    # server: rates only, per underlying model, and only once enough distinct accounts have used it, so no single
    # account's usage can be read from it. Off: suggestions come from the account's own requests and the catalogs.
    platform_model_stats_enabled: bool = True
    platform_model_stats_min_accounts: int = 3
    # Optional catalog keys, used only to list models (never to call one). Without a key that source is "not
    # configured" (Groq, Artificial Analysis) or tried without one (Ollama Cloud); nothing is guessed in its place.
    artificial_analysis_api_key: str | None = None
    groq_api_key: str | None = None
    ollama_api_key: str | None = None

    # Google ADK / Vertex AI Agent Engine. Authentication and project/location
    # are read by the Google SDK from standard GOOGLE_* environment variables.
    gemini_agent_model: str = "gemini-2.5-flash"

    apify_api_token: str | None = None
    apify_actor_id: str = "openclawai/job-board-scraper"
    apify_dataset_limit: int = 5000

    # Hunter actors (app.services.apify_adapters). Each has its own adapter; IDs are never hard-coded elsewhere.
    apify_primary_job_actor: str = "openclawai/job-board-scraper"
    apify_linkedin_actor: str = "thirdwatch/linkedin-jobs-scraper"
    apify_indeed_actor: str = "crawlerbros/indeed-jobs-scraper"
    apify_glassdoor_actor: str = "crawlerbros/glassdoor-jobs-scraper"
    apify_naukri_actor: str = "crawlerbros/naukri-scraper"
    # Job Insights research actors (app.services.job_insights.apify). Blank = that source is NOT_CONFIGURED: no ID is
    # ever assumed, because Store actors and their input/output schemas change. An admin sets an actor they have
    # checked, and optionally its input as a JSON template with {company}, {role}, {location} and {limit} fields.
    apify_glassdoor_compensation_actor: str = ""
    apify_glassdoor_interview_actor: str = ""
    apify_glassdoor_reviews_actor: str = ""
    apify_payscale_actor: str = ""
    apify_glassdoor_compensation_input: str = ""
    apify_glassdoor_interview_input: str = ""
    apify_glassdoor_reviews_input: str = ""
    apify_payscale_input: str = ""
    # When a multi-board run fails, search its boards again with their dedicated actors (once).
    apify_auto_fallback: bool = True
    apify_request_timeout_seconds: int = 30

    # Rate-limit backoff (app.services.backoff): after a provider says 429, its calls are held off for a while
    # instead of being retried straight into another rejection. Each account can adjust this in Settings.
    rate_limit_backoff_enabled: bool = True
    rate_limit_base_seconds: int = 30
    rate_limit_multiplier: float = 2.0
    rate_limit_max_seconds: int = 900
    # The longest a provider's own Retry-After is obeyed; beyond this the cap above applies instead.
    rate_limit_retry_after_max_seconds: int = 3600

    # External API monitoring (app.services.external_api). Every third-party call is recorded automatically;
    # raw rows are rolled into daily totals once they pass the retention, and operational log lines are deleted.
    external_api_telemetry_enabled: bool = True
    external_api_log_retention_days: int = 90
    system_log_retention_days: int = 30
    # One Monitoring → Logs line per LLM call, successful ones included (INFO), each linked to its request row
    # (and its prompt and response when they were captured). Off keeps Logs to failures and slow calls only.
    system_log_llm_calls: bool = True
    # Off by design: bodies can hold a candidate's resume or an employer's posting. When it is on, extracts are
    # still redacted and capped at external_api_payload_limit bytes.
    external_api_debug_payloads: bool = False
    external_api_payload_limit: int = 2000
    # How much of a request or response body an account that turned on capture (Monitoring → Logs) keeps per call;
    # redacted, and cleared after llm_prompt_debug_retention_days like captured LLM text.
    external_api_capture_limit: int = 32000
    # A call slower than this is logged as a warning in Monitoring → Logs, so slowdowns are visible without a query.
    external_api_slow_call_ms: int = 10_000
    hunter_max_runs_per_search: int = 30
    apify_sync_batch_size: int = 25

    min_fit_score: int = 72
    followup_days: int = 5
    max_jobs_per_batch: int = 30

    # Workflow workers (app.services.work_queue)
    workflow_max_attempts: int = 3
    workflow_lease_seconds: int = 900
    workflow_retry_base_seconds: int = 60
    workflow_retry_max_seconds: int = 3600

    # In-app scheduler (app.scheduler). Intervals are in minutes; 0 disables that job.
    scheduler_enabled: bool = True
    scheduler_tick_seconds: int = 30
    scheduler_lease_minutes: int = 30
    hunter_interval_minutes: int = 240
    apify_sync_interval_minutes: int = 5
    # The scheduler stops checking an Apify run this long after it started: no actor run lasts that long, so a run
    # still "unfinished" by then is one Apify will never report (it is marked failed, with the reason).
    apify_run_max_age_hours: int = 48
    ranking_interval_minutes: int = 2
    compensation_interval_minutes: int = 5
    application_interval_minutes: int = 5
    followup_interval_minutes: int = 60
    analytics_interval_minutes: int = 60
    cleanup_interval_minutes: int = 15
    # Automatic tailoring's safety net (app.services.auto_tailor): queues qualified jobs the ranking hook missed.
    auto_tailor_interval_minutes: int = 5
    # AI Optimization Advisor (app.services.ai_advisor): processes queued and scheduled analyses.
    ai_optimization_interval_minutes: int = 5
    # Imports the auto router's decisions (tier and cause) from LiteLLM's spend logs into Monitoring.
    litellm_telemetry_interval_minutes: int = 10
    # Judges each model's recent performance and rests the ones past the account's failover thresholds.
    model_health_interval_minutes: int = 2
    # A posting with no real description cannot be ranked honestly: the model would be scoring a job title.
    # Postings shorter than this (and with no parsed structure) wait instead, and are ranked if a description
    # arrives later. Several boards — Naukri and Bayt in particular — return listings without one.
    ranking_min_description_chars: int = 200
    ranking_batch_size: int = 20
    ranking_concurrency: int = 5
    # Job Ranker (app.services.ranker)
    min_ranking_confidence: float = 0.6
    ranking_max_attempts: int = 3
    ranking_lease_seconds: int = 900
    ranking_retry_base_seconds: int = 60
    ranking_retry_max_seconds: int = 3600
    # How long a task waits before trying again when the account cannot be ranked yet (missing key or resume).
    ranking_wait_seconds: int = 900
    ranking_enqueue_batch_size: int = 200
    ranking_max_evidence: int = 40
    # Structured job descriptions (app.services.job_description)
    jd_parse_enabled: bool = True
    jd_parse_interval_minutes: int = 3
    jd_parse_batch_size: int = 10
    jd_parse_concurrency: int = 3
    jd_parse_enqueue_batch_size: int = 100
    jd_parse_lease_seconds: int = 600
    jd_parse_max_attempts: int = 3
    jd_parse_retry_seconds: int = 120
    # AI structuring of uploaded resumes (app.services.resume_parsing): the Resume Parser model reads each new version,
    # the result is grounded in the resume's own text, and fills the structured data while nobody has reviewed it.
    resume_parse_enabled: bool = True
    resume_parse_interval_minutes: int = 1
    resume_parse_batch_size: int = 5
    resume_parse_concurrency: int = 2
    resume_parse_lease_seconds: int = 600
    resume_parse_max_attempts: int = 3
    resume_parse_retry_seconds: int = 120
    # RESUME_PARSE_GRAPH_MODE: on (default; each task runs as a LangGraph thread, app.agents.resume_parsing, when
    # AGENT_GRAPHS_ENABLED and PostgreSQL allow it) or off (the same steps run in sequence without a graph).
    resume_parse_graph_mode: str = "on"
    # RESUME_EXTRACTION_V2: read uploaded PDFs and Word documents with app.services.resume_extraction (PDFium, which
    # honours the /ActualText some producers write for ligature glyphs; Word tables and list bullets; section headings
    # and a structure-marked parse view; a quality report) instead of pypdf / paragraphs only. Off by default; off, extraction is exactly as before. The server's default: an account can choose for its
    # own resume uploads (worker setting resume_parsing.extraction_v2, Settings → Resumes). Job description files
    # follow this value.
    resume_extraction_v2: bool = False
    # OCR for resume extraction V2 (app.services.resume_extraction.ocr): only for a PDF the quality report flags, on
    # this machine (Tesseract's own binary; nothing leaves the server), bounded so an upload never waits long. Without
    # the engine installed, corrections fall back to consistent substitution rules alone.
    resume_ocr_enabled: bool = True
    resume_ocr_backend: str = "tesseract"
    resume_ocr_max_pages: int = 4
    resume_ocr_timeout_seconds: int = 20
    # Re-rank qualified jobs once their description has been parsed. Off by default: parsing a backlog would
    # otherwise start a round of ranking calls.
    jd_rerank_after_parse: bool = False
    # Re-rank qualified jobs automatically when the profile, the active resume or the ranking prompt changes.
    ranking_auto_rerank: bool = True
    # Job description recovery (app.services.jd_recovery): a posting that arrived with no usable description is
    # read again from its URL in the background — plain HTTP first, a headless browser only when HTTP finds nothing,
    # and a model only to pick the description out of blocks already fetched. Ingestion never waits for it.
    jd_fetch_enabled: bool = True
    jd_recovery_interval_minutes: int = 2
    # Shorter than this is not a usable posting unless it carries strong job-posting structure (jd_quality).
    jd_description_min_characters: int = 500
    jd_fetch_batch_size: int = 10
    jd_fetch_max_concurrency: int = 2
    jd_fetch_max_attempts: int = 3
    jd_fetch_retry_seconds: int = 300
    jd_fetch_lease_seconds: int = 600
    jd_fetch_http_timeout_seconds: int = 20
    jd_fetch_max_redirects: int = 8
    # Postings discovered before this feature: how many are checked for a missing description per run.
    jd_fetch_enqueue_batch_size: int = 100
    # The model only chooses among blocks of text already fetched; it never browses and never writes the text.
    jd_fetch_llm_enabled: bool = True
    # Local testing only: lets recovery read from localhost and private addresses. Never enable in production.
    jd_fetch_allow_private_hosts: bool = False
    # Job Insights (app.services.job_insights): source-backed company, compensation and interview research for an
    # approved job, built in the background. Approval never waits for it.
    job_insights_enabled: bool = True
    job_insights_interval_minutes: int = 5
    job_insights_batch_size: int = 5
    job_insights_max_concurrency: int = 2
    job_insights_max_attempts: int = 3
    job_insights_retry_seconds: int = 600
    job_insights_lease_seconds: int = 900
    # Sources researched side by side for one job, and the longest one source may take (an Apify run included).
    job_insights_source_concurrency: int = 3
    job_insights_source_timeout_seconds: int = 120
    # Approved jobs from before this feature: how many are queued per run.
    job_insights_enqueue_batch_size: int = 20
    # How long researched observations are reused for another job of the same account (company / role / location).
    company_insight_ttl_days: int = 30
    compensation_insight_ttl_days: int = 14
    interview_insight_ttl_days: int = 30
    # Observations older than this many months are kept but weighted lowest; 0 never drops them.
    insight_max_observation_age_months: int = 0
    # A posting that no longer exists (404/410, "no longer accepting applications") is deleted, and a tombstone stops
    # it being imported again (app.services.job_removal). Approved and applied jobs are never deleted.
    auto_delete_removed_jobs: bool = True
    # Re-check, about daily, that postings up to approval still exist: HTTP first, the shared browser only when HTTP
    # read nothing (never after a login wall, a challenge or a 429), bounded, never a model.
    listing_check_interval_minutes: int = 240
    listing_check_batch_size: int = 60
    listing_check_max_age_days: int = 1
    # Domain filter (app.services.domain_match): jobs outside the candidate's domain are hidden at discovery and never
    # recovered, parsed or ranked. Each account edits its own lists; this switches the feature for the server.
    domain_filter_enabled: bool = True
    # A title term or company learned from the person's dismissals needs this many dismissed jobs behind it.
    learn_min_dismissals: int = 3
    # Hybrid domain classification (app.services.domain_classifier). DOMAIN_CLASSIFIER_MODE: off (default; the
    # deterministic filter alone, exactly as before), shadow (the old filter still decides; verdicts are only recorded
    # for comparison, and model calls need a daily limit above 0) or on (only unambiguous rules hide a job without the
    # model; see scripts/domain_classifier_eval.py before turning this on).
    domain_classifier_mode: str = "off"
    domain_classifier_interval_minutes: int = 2
    domain_classifier_batch_size: int = 40
    domain_classifier_concurrency: int = 4
    domain_classifier_lease_seconds: int = 300
    domain_classifier_max_attempts: int = 3
    domain_classifier_retry_seconds: int = 60
    domain_classifier_cache_days: int = 30
    domain_classifier_shadow_daily_limit: int = 0
    # Model calls per account and day in mode on (0 = no limit). Jobs over it wait as PENDING, unhidden and unranked,
    # and are queued as the day's count falls; a provider outage or a big import can no longer become a ranking surge.
    domain_classifier_daily_limit: int = 2000
    # A job waiting for the person's review longer than this goes on like any other UNCERTAIN job (0 = never).
    domain_classifier_review_days: int = 7
    # Shared headless browser (app.infrastructure.browser). Optional: HTTP recovery works without it.
    playwright_enabled: bool = True
    playwright_browser: str = "chromium"
    # PLAYWRIGHT_HEADLESS=false opens a visible window for local debugging of extraction selectors.
    playwright_headless: bool = True
    # Hosts (comma separated, subdomains included) whose postings JD recovery reads in a visible window, for sites that
    # refuse the headless browser but serve an ordinary one. Plain Chromium: no spoofing, and a challenge stays BLOCKED.
    # Visible pages open one at a time, at least this many seconds apart. Local installs only: it needs a display.
    playwright_visible_hosts: str = ""
    playwright_visible_min_interval_seconds: int = 10
    # Those hosts are read by the browser-use runner. True lets Playwright's visible window read them when the runner
    # could not; False (the default) leaves a runner failure as the result, and Playwright never opens these hosts.
    playwright_visible_window_fallback: bool = False
    playwright_navigation_timeout_seconds: int = 45
    playwright_page_timeout_seconds: int = 60
    playwright_max_concurrency: int = 2
    # Compatibility-first by default. Security routing remains enabled regardless of this performance option.
    playwright_block_nonessential_resources: bool = False
    # Failure-only screenshots and redacted metadata for local diagnosis; never HTML, cookies, or headers.
    playwright_debug_artifacts: bool = False
    # Keep Chromium running between recoveries (faster) or close it after each batch (less memory while idle).
    playwright_keep_browser_running: bool = True
    compensation_batch_size: int = 50
    # Application Inbox (app.services.application_inbox): reads Gmail, read-only, for mail about applications that
    # were actually submitted. Needs a Google OAuth client (Web application) whose redirect URI is
    # GOOGLE_OAUTH_REDIRECT_URI (default {APP_BASE_URL}/api/gmail/oauth/callback); each account connects its own Gmail.
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: str | None = None
    google_oauth_redirect_uri: str | None = None
    application_inbox_enabled: bool = False
    application_inbox_interval_hours: int = 24
    # How far back the first sync of an application looks, and how much each later sync re-reads before the last one.
    gmail_initial_lookback_days: int = 30
    gmail_sync_overlap_hours: int = 24
    gmail_max_messages_per_sync: int = 200
    # Retries of one Gmail request after a server error or a timeout. A 429 ends the sync; the next run resumes.
    gmail_max_retries: int = 2
    # Email → application association: at or above AUTO the email is linked; between REVIEW and AUTO a model may help
    # decide and the link is otherwise left for a person; below REVIEW the email is not linked (and never analysed).
    application_email_auto_match_threshold: float = 0.90
    application_email_review_threshold: float = 0.70
    # A stage change the email implies is applied only at or above this confidence (and when it is explicit and valid).
    application_stage_auto_update_threshold: float = 0.90
    application_inbox_analysis_max_attempts: int = 3
    application_inbox_retry_seconds: int = 900
    # The most text kept per matched email (its relevant part, redacted). Unmatched mail keeps no body at all.
    application_inbox_body_chars: int = 4000
    # Background tailoring worker (app.services.tailor_tasks)
    tailor_worker_enabled: bool = True
    tailor_concurrency: int = 2
    tailor_lease_seconds: int = 900
    # Claude Desktop MCP server (app.mcp): the most jobs one context request or bulk save may cover.
    mcp_bulk_tailor_limit: int = 10
    mcp_bulk_review_limit: int = 20
    # Applying from Claude Desktop: the most jobs one list_jobs_for_application page returns, and where
    # get_job_packet writes the approved resume files for an ATS upload (one folder per account and job).
    mcp_application_page_limit: int = 50
    mcp_packet_dir: str = "./data/packets"
    # The token `python -m app.mcp` authenticates with. The process environment wins over .env.
    jobhunt_mcp_token: str | None = None
    # Required header value (X-Admin-Token) for POST /api/admin/scheduler/{job}/run. Unset disables the triggers.
    admin_token: str | None = None
    # Agent graphs (app.agents, LangGraph): off by default. Off means the /api/agents endpoints answer 404, the
    # agent_graphs scheduler job is skipped and no checkpointer is opened. PostgreSQL only (durable checkpoints).
    agent_graphs_enabled: bool = False
    agent_recovery_minutes: int = 5
    agent_lease_seconds: int = 300
    agent_max_attempts: int = 3
    # A checkpoint bigger than this, or holding anything secret-shaped, is refused before it is written.
    agent_checkpoint_max_bytes: int = 65536
    # Checkpoints of finished threads are deleted after this; a thread waiting for a person expires after the other.
    agent_checkpoint_retention_days: int = 14
    agent_interrupt_expiry_days: int = 30
    # Optional LangSmith export (app.agents.tracing_boundary): off by default, metadata only, never credentials.
    # Own names (not LANGSMITH_*) so the SDK's environment auto-tracing never picks them up on its own.
    agent_langsmith_export_enabled: bool = False
    agent_langsmith_api_key: str | None = None
    agent_langsmith_endpoint: str = "https://api.smith.langchain.com"
    agent_langsmith_project: str = "job-hunt-os"
    agent_langsmith_timeout_ms: int = 3000
    # After LangSmith answers 429 because the plan's trace quota is spent, export stops for this long and then tries
    # once more (a longer Retry-After wins). Graph runs continue untraced meanwhile.
    agent_langsmith_quota_pause_minutes: int = 60
    # The qualification graph (app.agents.qualification, Phase 3). QUALIFICATION_GRAPH_MODE: off (default; jobs are
    # qualified by the services exactly as before), shadow (they still are, and a bounded number of decided jobs per
    # account a day are re-run through the graph read-only and compared — no paid call unless the paid budget below
    # allows one), on (new and waiting jobs are qualified by the graph, which drives the same recovery, parse and
    # ranking tasks). Needs AGENT_GRAPHS_ENABLED and PostgreSQL; otherwise it acts as off.
    qualification_graph_mode: str = "off"
    qualification_minutes: int = 1
    qualification_batch_size: int = 50
    qualification_concurrency: int = 4
    qualification_max_attempts: int = 3
    qualification_retry_seconds: int = 60
    qualification_lease_seconds: int = 300
    qualification_shadow_daily_limit: int = 50  # shadow runs per account per day
    qualification_shadow_paid_daily_limit: int = 0  # shadow re-rankings (model calls) per account per day: none
    qualification_shadow_lookback_hours: int = 48
    # The resume preparation graph (app.agents.preparation, Phase 4). PREPARATION_GRAPH_MODE: off (default; automatic
    # tailoring queues the tailor worker exactly as before), shadow (it still does, and a bounded number of reviewed
    # packets per account a day are re-checked by the graph read-only: never a model call), on (qualified jobs are
    # prepared by the graph: tailor, evidence checks, review decision, a bounded revision loop, a person's approval).
    # Needs AGENT_GRAPHS_ENABLED and PostgreSQL; otherwise it acts as off.
    preparation_graph_mode: str = "off"
    preparation_minutes: int = 1
    preparation_batch_size: int = 50
    preparation_concurrency: int = 4
    preparation_max_attempts: int = 3
    preparation_retry_seconds: int = 60
    preparation_max_revisions: int = 2  # tailoring passes after the first, per run, before a person decides
    preparation_lease_seconds: int = 300
    preparation_shadow_daily_limit: int = 50
    preparation_shadow_lookback_hours: int = 48
    # The Job Insights graph (app.agents.job_insights, Phase 4). JOB_INSIGHTS_GRAPH_MODE: off (default; research runs
    # as before), shadow (it does, and the graph's deterministic steps re-check each finished run read-only), on (each
    # research task runs as a graph thread on the same task row). Needs AGENT_GRAPHS_ENABLED and PostgreSQL.
    job_insights_graph_mode: str = "off"
    # Pre-submit claims (app.services.apply_queue.begin_submission, Phase 5): how long a claimed submission may take
    # before its outcome is treated as unknown (UNCERTAIN, resolved only by a person; never released automatically).
    application_claim_lease_minutes: int = 60
    # The application execution worker (app.agents.application, Phase 5). APPLICATION_EXECUTION_MODE: off (default;
    # nothing runs), observe (read the form and resolve its answers, never fill), assist (also fill it in an ephemeral
    # browser and validate, ending READY_TO_SUBMIT for a person to submit). Nothing ever clicks a final Submit. Needs
    # AGENT_GRAPHS_ENABLED and PostgreSQL; otherwise it acts as off.
    application_execution_mode: str = "off"
    application_execution_minutes: int = 1
    application_execution_batch_size: int = 10
    application_execution_concurrency: int = 2
    application_execution_max_attempts: int = 3
    application_execution_retry_seconds: int = 120
    application_execution_lease_seconds: int = 300
    application_execution_step_timeout_seconds: int = 90
    # Phase 6 (app.services.submission, README §10b). APPLICATION_EXECUTION_MODE also takes guarded_auto (auto
    # submit): a run of an account that turned Auto submit on submits the application itself, on any route a browser
    # can complete, after the pre-submit validator, the shared claim and a SubmissionIntent, and only once; it stops
    # for the person only when something needs them (an unknown outcome included). `autonomous` is refused.
    application_max_actions: int = 60  # browser actions per run (fills, clicks, uploads), whatever proposes them
    application_takeover_ttl_minutes: int = 15  # how long a paused headful session waits for the person
    # A window handed over for a CAPTCHA is asked this often whether the person solved it; the run then continues by
    # itself (a reCAPTCHA answer lasts about two minutes, so this stays short).
    application_challenge_poll_seconds: int = 5
    # A window handed over for fields only the person can fill continues by itself once every one of them holds
    # something that has not changed for this long (they stopped typing). 0: as soon as they are all filled.
    application_fields_settle_seconds: int = 20
    # A form read in a browser-use session is kept this long while the run waits for an answer in the Knowledge bank,
    # so it is filled in place afterwards (the session's own time is extended to match; after it, it is read again).
    application_input_hold_minutes: int = 60
    # Saved ATS logins (app.services.ats_auth): a session a person logged in to, and an encrypted password vault.
    # A password is filled only on a domain listed in APPLICATION_LOGIN_DOMAINS, never anywhere else.
    application_auth_sessions_enabled: bool = False
    application_login_domains: str = ""
    ats_session_max_days: int = 30
    # The browser-use backend (app.infrastructure.application_browser.browser_use_sidecar): a runner Job Hunt OS owns
    # (sidecars/browser_use_runner) started with the browser-use Web UI's own Python (BROWSER_USE_PYTHON), one process
    # per execution. BROWSER_USE_WEBUI_URL is only health-checked; Job Hunt OS never drives the Web UI's own agent.
    browser_use_webui_enabled: bool = False
    browser_use_webui_url: str = ""
    browser_use_webui_path: str = ""
    browser_use_python: str = ""
    browser_use_timeout_seconds: int = 300  # one execution's whole browser-use session
    browser_use_command_timeout_seconds: int = 60  # one command (load, observe, fill)
    browser_use_headless: bool = True  # False shows the window, so a person can take over (local installs only)
    # Tests and local fixtures only: let the application browsers reach 127.0.0.1 / private hosts.
    application_allow_private_hosts: bool = False
    # Candidate knowledge retrieval (RAG, app.services.rag_retrieval). RAG_RETRIEVAL_MODE: off (default; the context
    # builders select evidence exactly as before), shadow (they still do, and the retrieval result is computed beside
    # it and compared in Monitoring → Logs, ids only), on (job contexts take their evidence from retrieval, scoped to
    # the job's own resume). RAG_VECTOR_ENABLED adds embeddings (the configured EMBEDDER model, through LiteLLM) to
    # the keyword search and runs the rag_index job; without it retrieval is full-text only.
    rag_retrieval_mode: str = "off"
    rag_vector_enabled: bool = False
    rag_index_minutes: int = 10
    rag_embed_batch_size: int = 32
    rag_index_max_entries_per_run: int = 500
    rag_index_max_attempts: int = 5
    rag_index_retry_seconds: int = 30  # the first retry wait; doubles per attempt, at most an hour
    rag_index_concurrency: int = 2  # embedding batches in flight at once, across accounts
    rag_max_candidates: int = 50
    rag_context_token_budget: int = 1200
    rag_backfill_min_coverage: float = 1.0
    rag_retired_retention_days: int = 7
    rag_query_cache_size: int = 512
    rag_embed_timeout_seconds: int = 60
    # Vector size of the built-in test embedder used only when LLM_PROVIDER=mock (tests and offline development).
    rag_mock_dimensions: int = 64

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def application_inbox_interval_minutes(self) -> int:
        """The scheduler interval of the inbox job: 0 (off) unless APPLICATION_INBOX_ENABLED."""
        return max(0, self.application_inbox_interval_hours) * 60 if self.application_inbox_enabled else 0

    @property
    def agent_recovery_interval_minutes(self) -> int:
        """The scheduler interval of the agent graph recovery pass: 0 (off) unless AGENT_GRAPHS_ENABLED."""
        return max(0, self.agent_recovery_minutes) if self.agent_graphs_enabled else 0

    @property
    def qualification_mode(self) -> str:
        """off, shadow or on; anything else, or agent graphs being off, is off."""
        mode = (self.qualification_graph_mode or "off").strip().lower()
        return mode if self.agent_graphs_enabled and mode in ("shadow", "on") else "off"

    @property
    def qualification_interval_minutes(self) -> int:
        """The scheduler interval of the qualification graph job: 0 (off) unless the graph's mode is shadow or on.
        In off mode with tasks left over from on, the job still runs to hand them back (qualification.release)."""
        return max(0, self.qualification_minutes) if self.agent_graphs_enabled else 0

    @property
    def preparation_mode(self) -> str:
        """off, shadow or on; anything else, or agent graphs being off, is off."""
        mode = (self.preparation_graph_mode or "off").strip().lower()
        return mode if self.agent_graphs_enabled and mode in ("shadow", "on") else "off"

    @property
    def preparation_interval_minutes(self) -> int:
        """The scheduler interval of the preparation graph job: 0 (off) unless agent graphs are on. In off mode with
        tasks left over from on, the job still runs to hand them back (preparation.release)."""
        return max(0, self.preparation_minutes) if self.agent_graphs_enabled else 0

    @field_validator("application_execution_mode")
    @classmethod
    def _no_autonomous_mode(cls, value: str) -> str:
        if (value or "").strip().lower() == "autonomous":
            raise ValueError("APPLICATION_EXECUTION_MODE=autonomous is not available: auto submit is guarded_auto, "
                             "for accounts that turned Auto submit on")
        return value

    @property
    def application_execution_mode_value(self) -> str:
        """off, observe, assist or guarded_auto; anything else, or agent graphs being off, is off."""
        mode = (self.application_execution_mode or "off").strip().lower()
        return mode if self.agent_graphs_enabled and mode in ("observe", "assist", "guarded_auto") else "off"

    @property
    def login_domains(self) -> frozenset[str]:
        return frozenset(d.strip().lower().strip(".") for d in (self.application_login_domains or "").split(",")
                         if d.strip())

    @property
    def application_execution_interval_minutes(self) -> int:
        """The scheduler interval of the application execution job: 0 (off) unless agent graphs are on. In off mode
        it still runs to end tasks left over (application_execution.release)."""
        return max(0, self.application_execution_minutes) if self.agent_graphs_enabled else 0

    @property
    def resume_parse_graph_mode_value(self) -> str:
        """on or off for resume structuring; anything else, or agent graphs being off, is off."""
        mode = (self.resume_parse_graph_mode or "on").strip().lower()
        return "on" if self.agent_graphs_enabled and mode == "on" else "off"

    @property
    def job_insights_graph_mode_value(self) -> str:
        """off, shadow or on for research runs; anything else, or agent graphs being off, is off."""
        mode = (self.job_insights_graph_mode or "off").strip().lower()
        return mode if self.agent_graphs_enabled and mode in ("shadow", "on") else "off"

    @property
    def rag_index_interval_minutes(self) -> int:
        """The scheduler interval of the embedding index job: 0 (off) unless RAG_VECTOR_ENABLED."""
        return max(0, self.rag_index_minutes) if self.rag_vector_enabled else 0

    @property
    def rag_mode(self) -> str:
        mode = (self.rag_retrieval_mode or "off").strip().lower()
        return mode if mode in ("off", "shadow", "on") else "off"

    @property
    def gmail_oauth_configured(self) -> bool:
        return bool(self.google_oauth_client_id and self.google_oauth_client_secret)

    @property
    def gmail_redirect_uri(self) -> str:
        return self.google_oauth_redirect_uri or f"{self.app_base_url.rstrip('/')}/api/gmail/oauth/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()
