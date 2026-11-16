import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'render', 'monState', 'MON_TABS', 'renderMonExternal', 'renderMonApify', 'renderMonLogs',
  'renderMonRequestModal', 'renderMonExtRequestModal', 'monCaptured'];
let ui, context, fetchMock;

const response = (data, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(data) });
const health = (n, extra = {}) => ({ requests: n, failed: 1, success_rate: 0.93, failure_rate: 0.07, rate_limited: 1,
  rate_limit_rate: 0.02, server_errors: 1, server_error_rate: 0.01, client_errors: 0, timeouts: 1, timeout_rate: 0.01,
  connection_errors: 0, retries: 2, retried_requests: 2, retry_rate: 0.04, avg_latency_ms: 820, p50_latency_ms: 640,
  p95_latency_ms: 2400, p99_latency_ms: 3900, max_latency_ms: 4200, estimated_cost: 0.42, unpriced_requests: 3,
  request_bytes: 100, response_bytes: 2000, ...extra });
const externalDash = () => ({
  filter: {}, summary: { ...health(1284), providers: 2, operations: 3 },
  // Options for the filter lists: everything in the range, not just what the current filter left behind.
  facets: { providers: ['APIFY', 'OPENROUTER'], services: ['apify_api', 'llm'],
            operations: ['actor_run_status', 'chat_completion', 'dataset_fetch'],
            workflows: ['apify_sync', 'hunter', 'ranking'], error_types: ['RATE_LIMIT', 'SERVER_ERROR'],
            status_codes: [200, 429, 503] },
  providers: [{ provider: 'APIFY', ...health(640) }, { provider: 'OPENROUTER', ...health(644) }],
  operations: [{ provider: 'APIFY', service: 'apify_api', operation: 'dataset_fetch', ...health(300) }],
  workflows: [{ workflow: 'hunter', ...health(700) }],
  errors: { failed: 17, failure_rate: 0.013, by_type: [{ error_type: 'RATE_LIMIT', requests: 9, share: 0.5 }], by_status_code: [], by_provider: [] },
  timeseries: [{ at: '2026-09-20T00:00:00', requests: 120, failed: 2, avg_latency_ms: 700, estimated_cost: 0.1 }],
  latency: { overall: health(1284) },
  apify: { runs_today: 6, runs_this_month: 48, runs: 22, succeeded: 19, failed: 2, running: 1, success_rate: 0.9,
    compute_units: 1.284, runs_with_reported_usage: 19, reported_cost: 0.42, runs_with_reported_cost: 19,
    dataset_items: 1840, jobs_discovered: 1840, jobs_normalized: 1700, jobs_deduplicated: 1512,
    jobs_after_deduplication: 328, api: health(640),
    by_actor: [{ actor_id: 'x/y', runs: 12, succeeded: 11, failed: 1, jobs_discovered: 1200, jobs_created: 210, compute_units: 0.9, reported_cost: 0.28 }],
    by_saved_search: [{ saved_search: 'Remote AI', runs: 12, succeeded: 11, failed: 1, jobs_discovered: 1200, jobs_created: 210, compute_units: null, reported_cost: null }],
    by_location: [], by_site: [{ site: 'linkedin', jobs: 420 }], by_day: [],
    recent_runs: [{ id: 21, actor_id: 'x/y', status: 'SUCCEEDED', created_at: '2026-09-20T20:59:00', finished_at: '2026-09-20T21:02:00',
      duration_ms: 180000, location: 'Remote', saved_search: 'Remote AI', jobs_discovered: 120, jobs_deduplicated: 98,
      jobs_created: 22, compute_units: 0.042, reported_cost: 0.0137 }] } });
const logsPage = () => ({ total: 3, page: 1, pages: 1, per_page: 50, levels: ['INFO', 'ERROR'],
  all_categories: ['EXTERNAL_API', 'SCHEDULER'], categories: [], items: [
    { id: 3, created_at: '2026-09-20T21:06:00', level: 'ERROR', category: 'EXTERNAL_API', provider: 'OPENROUTER',
      operation: 'chat_completion', workflow: 'ranking', trace_id: '8f2c1a9b', entity_type: 'job', entity_id: 412,
      message: 'OPENROUTER chat_completion failed: RATE_LIMIT', status: 'RATE_LIMIT', duration_ms: 210 }] });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c","is_admin":true}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('scrollTo', vi.fn());
  // Actions re-load the screen; answer every endpoint with a shape the renderer can draw.
  fetchMock = vi.fn(async url =>
    response(url.includes('/workspace/summary') ? ui.D.summary
      : url.includes('/external/dashboard') ? externalDash()
      : url.includes('/monitoring/logs') ? logsPage()
      : url.includes('/external/requests') ? ui.D.monExtRequests
      : url.includes('/recommendations') ? ui.D.monRec : {}));
  vi.stubGlobal('fetch', fetchMock);
  if (!ui) {
    context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock; context.confirm = confirm;
  ui.S.forms = {}; ui.S.busy = {}; ui.S.screen = 'monitoring'; ui.S.mon = null;
  ui.monState();
  // The shell renders the sidebar too, so the workspace summary has to be present.
  Object.assign(ui.D, {
    summary: { profile: {}, counts: {}, application_mode: 'manual_approval', model: 'm', total_jobs: 1,
      auto_search: { schedule: 'manual', setups: 0, roles: 0, locations: 0, next_run: null, last_run: null } },
    monExt: externalDash(), monLogs: logsPage(), monRec: { report: null, tasks: [], active: false, schedule: 'off', notice: 'Recommendations only.' },
    monExtRequests: { total: 2, page: 1, pages: 1, per_page: 25, items: [
      { id: 9, created_at: '2026-09-20T21:03:00', provider: 'APIFY', operation: 'dataset_fetch', workflow: 'hunter', status: 'SUCCEEDED', http_status_code: 200, latency_ms: 840, retry_count: 0, estimated_cost: null },
      { id: 8, created_at: '2026-09-20T21:04:00', provider: 'OPENROUTER', operation: 'chat_completion', workflow: 'ranking', status: 'FAILED', http_status_code: 429, error_type: 'RATE_LIMIT', latency_ms: 210, retry_count: 2, estimated_cost: null }] } });
});

const show = tab => { ui.monState().tab = tab; ui.render(); return document.getElementById('view'); };

describe('Monitoring · external APIs', () => {
  it('offers every section', () => {
    show('external');
    expect(ui.MON_TABS.map(([id]) => id)).toEqual(['overview', 'llm', 'models', 'external', 'limits', 'apify', 'inbox', 'logs', 'advisor']);
    expect([...document.querySelectorAll('[data-act="mon-tab"]')].map(b => b.textContent.trim()))
      .toEqual(['Overview', 'LLM usage', 'Model performance', 'External APIs', 'Rate limits', 'Apify', 'Application inbox', 'Logs', 'AI optimization']);
  });

  it('shows the headline numbers the dashboard is for', () => {
    const view = show('external');
    const text = view.textContent;
    expect(text).toContain('1,284');            // calls
    expect(text).toContain('93.0%');            // success rate
    expect(text).toContain('820ms');            // average latency
    expect(text).toContain('2.4s');             // p95
    expect(text).toContain('$0.42');            // estimated cost
    expect(text).toContain('APIFY');
    expect(text).toContain('OPENROUTER');
  });

  it('shows rolling provider usage and dated quota readings with distinct request and token windows', () => {
    ui.D.monLimits = { as_of: '2026-11-15T03:00:00', providers: [{ provider: 'GROQ', calls_minute: 2,
      calls_day: 150, http_429_day: 1, models: [{ model: 'alias', calls_minute: 2, calls_day: 150 }] }],
      snapshots: [{ provider: 'GROQ', model: 'alias', requests_window: 'day', requests_limit: 1000,
        requests_remaining: 0, tokens_per_minute_limit: 8000, tokens_per_minute_remaining: 420,
        tokens_reset_seconds: 7.66, retry_after_seconds: 2, observed_at: '2026-11-15T03:00:00' }] };
    const view = show('limits');
    expect(view.querySelector('[data-act="mon-range"]')).toBeNull();
    const rows = [...view.querySelectorAll('tbody tr')].map(tr => [...tr.cells].map(td => td.textContent));
    expect(rows).toContainEqual(['GROQ', '2', '150', '1']);
    expect(rows).toContainEqual(['GROQ', 'alias', 'Requests / day', '1,000', '0', '—', expect.any(String)]);
    expect(rows).toContainEqual(['GROQ', 'alias', 'Tokens / minute', '8,000', '420', '7.66 s', expect.any(String)]);
    expect(view.textContent).toContain('Retry after');
    expect(view.textContent).toContain('whisper-large-v3');
    expect(view.querySelector('[data-act="mon-openrouter-quota"]')).toBeTruthy();
  });

  it('keeps plan references available when nothing has been recorded', () => {
    ui.D.monLimits = { as_of: '2026-11-15T03:00:00', providers: [], snapshots: [] };
    const view = show('limits');
    expect(view.textContent).toContain('No provider calls recorded');
    expect(view.textContent).toContain('No quota headers recorded');
    expect(view.textContent).toContain('OpenRouter free models');
  });

  it('lists calls with provider, operation, workflow, status, latency, retries and cost', () => {
    const view = show('external');
    const headers = [...view.querySelectorAll('table th')].map(h => h.textContent);
    expect(headers).toEqual(expect.arrayContaining(['Time', 'Provider', 'Operation', 'Workflow', 'Status', 'Latency', 'Retries', 'Cost']));
    const failed = [...view.querySelectorAll('tbody tr')].find(r => r.textContent.includes('OPENROUTER'));
    expect(failed.textContent).toContain('429');
    expect(failed.textContent).toContain('210ms');
  });

  it('filters by provider, operation, workflow, status, failure type, HTTP code and errors only', async () => {
    show('external');
    const keys = [...document.querySelectorAll('[data-act-change="mon-ext-filter"]')].map(s => s.dataset.key);
    expect(keys).toEqual(['provider', 'operation', 'workflow', 'status', 'error_type', 'http_status_code']);
    ui.A['mon-ext-filter']({ dataset: { key: 'provider' }, value: 'APIFY' });
    expect(ui.monState().ext.provider).toBe('APIFY');
    ui.A['mon-ext-filter']({ dataset: { key: 'error_type' }, value: 'RATE_LIMIT' });
    ui.A['mon-ext-errors']({ checked: true });
    expect(ui.monState().ext.error_only).toBe(true);
    const asked = fetchMock.mock.calls.map(([url]) => url).join(' ');
    expect(asked).toContain('error_only=true');
    expect(asked).toContain('error_type=RATE_LIMIT');
  });

  it('keeps every choice in the lists after one is picked', () => {
    // The bug this guards: options built from the filtered rows made the chosen value the only one left,
    // so a successful Apify call could not be reached again without resetting.
    ui.monState().ext.provider = 'OPENROUTER';
    ui.monState().ext.status = 'FAILED';
    show('external');
    const providers = [...document.querySelector('[data-key="provider"]').options].map(o => o.value);
    expect(providers).toEqual(['', 'APIFY', 'OPENROUTER']);
    expect(document.querySelector('[data-key="provider"]').value).toBe('OPENROUTER');
    const operations = [...document.querySelector('[data-key="operation"]').options].map(o => o.value);
    expect(operations).toContain('dataset_fetch');
  });

  it('shows what is filtered and can drop one filter at a time', () => {
    Object.assign(ui.monState().ext, { provider: 'APIFY', error_type: 'RATE_LIMIT' });
    show('external');
    const chips = [...document.querySelectorAll('[data-act="mon-ext-clear"]')];
    expect(chips.map(b => b.textContent.trim())).toEqual(['Provider: APIFY ×', 'Failure: Rate limits (429) ×']);
    ui.A['mon-ext-clear']({ dataset: { key: 'error_type' } });
    expect(ui.monState().ext.error_type).toBe('');
    expect(ui.monState().ext.provider).toBe('APIFY');
  });

  it('filters by a value clicked in the table', () => {
    show('external');
    const provider = document.querySelector('[data-act="mon-ext-pick"][data-key="provider"]');
    expect(provider.textContent).toBe('APIFY');
    ui.A['mon-ext-pick'](provider, { stopPropagation() {} });
    expect(ui.monState().ext.provider).toBe('APIFY');
    expect(ui.monState().ext.page).toBe(1);
  });

  it('explains an empty table when filters are the reason', () => {
    ui.monState().ext.provider = 'APIFY';
    ui.D.monExtRequests = { total: 0, page: 1, pages: 1, per_page: 25, items: [] };
    expect(show('external').textContent).toContain('No calls match these filters');
    ui.monState().ext.provider = '';
    expect(show('external').textContent).toContain('No external API calls');
  });

  it('shows Apify usage, and a dash wherever Apify reported nothing', () => {
    const view = show('apify');
    const text = view.textContent;
    expect(text).toContain('1.284');    // compute units
    expect(text).toContain('328');      // jobs after deduplication
    expect(text).toContain('$0.42');    // reported cost
    expect(text).toContain('19 runs priced by Apify');
    ui.D.monExt.apify.compute_units = null;
    ui.D.monExt.apify.reported_cost = null;
    expect(show('apify').textContent).toContain('Apify reported none');
  });

  it('shows logs with level, category, trace and entity', () => {
    const view = show('logs');
    const headers = [...view.querySelectorAll('table th')].map(h => h.textContent);
    expect(headers).toEqual(expect.arrayContaining(['Time', 'Level', 'Category', 'Provider', 'Workflow', 'Trace', 'Entity', 'Message', 'Duration', 'Status']));
    expect(view.textContent).toContain('OPENROUTER chat_completion failed: RATE_LIMIT');
    expect(view.textContent).toContain('job #412');
  });

  it('pages through calls and logs', () => {
    show('external');
    ui.A['mon-ext-page']({ dataset: { dir: '1' } });
    expect(ui.monState().ext.page).toBe(2);
    show('logs');
    ui.A['mon-log-page']({ dataset: { dir: '1' } });
    expect(ui.monState().logs.page).toBe(2);
    ui.A['mon-log-page']({ dataset: { dir: '-1' } });
    ui.A['mon-log-page']({ dataset: { dir: '-1' } });
    expect(ui.monState().logs.page).toBe(1);
  });
});

describe('Monitoring · LLM request structured output', () => {
  it('shows the captured request and response of a call, pretty-printed, or says they were not captured', () => {
    const call = { id: 7, request_id: 'r-7', created_at: '2026-10-10T11:05:34', provider: 'LITELLM', operation: 'model_test',
      status: 'FAILED', error_type: 'CLIENT_ERROR', http_status_code: 400, http_method: 'POST', endpoint_host: 'localhost:4000',
      endpoint_path: '/v1/chat/completions', latency_ms: 812, attempt: 1, retry_count: 0, cost_source: 'unknown',
      error_message: 'HTTP 400', metadata: null,
      payload: { request: { method: 'POST', url: 'http://localhost:4000/v1/chat/completions', headers: { authorization: '[redacted]' },
                            body: '{"model":"groq/openai/gpt-oss-20b","max_tokens":512}' },
                 response: { status: 400, headers: {}, body: '{"error":{"code":"json_validate_failed","failed_generation":""}}' } } };
    ui.D.monExtRequest = call;
    ui.monState().ext.request = 7;
    const html = ui.renderMonExtRequestModal();
    expect(html).toContain('REQUEST AND RESPONSE');
    expect(html).toContain('POST http://localhost:4000/v1/chat/completions');
    expect(html).toContain('authorization: [redacted]');
    expect(html).toContain('HTTP 400');
    expect(html).toContain('&quot;code&quot;: &quot;json_validate_failed&quot;');  // pretty-printed JSON
    expect(ui.monCaptured(null)).toContain('were not captured');
  });

  it('shows how the contract was enforced and why an answer failed it', () => {
    const request = { id: 41, request_id: 'r-41', created_at: '2026-09-25T10:00:00', stage: 'TAILOR', status: 'FAILED',
      error_type: 'SCHEMA_VALIDATION', structured_output_valid: false, structured_output_retry_count: 0,
      validation_failure_reason: 'POSSIBLE_CROSS_WORKER_OUTPUT', schema_enforcement: 'json_mode',
      context_builder_version: 'v2', provider: 'litellm', model: 'm', retry_count: 0, context_breakdown: {},
      cost_source: 'unknown', usage_source: 'provider_reported', latency_ms: 900, estimated_cost: null };
    ui.D.monRequest = request;
    ui.monState().request = 41;
    const html = ui.renderMonRequestModal();
    expect(html).toMatch(/invalid \(another worker(&#39;|&#x27;|')s output shape\)/);
    expect(html).toContain('JSON mode; contract checked by Job Hunt OS');
    expect(html).toContain('Context builder');
    expect(html).toContain('v2');
  });
});
