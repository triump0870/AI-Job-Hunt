import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// The production scripts, in page order, without app.js's automatic network boot.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'AIM', 'render', 'setF', 'orchState', 'readHash', 'renderAIModels', 'aimWorkerPayload', 'aimRoutingPayload', 'renderMonRouting'];
let ui, context, fetchMock, routes;
const response = (data, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(data) });

const worker = (type, label, config, model, extra = {}) => ({ worker_type: type, label, description: `${label} work`, structured_output_required: true, min_context_tokens: 8192, has_prompt: true, config: { mode: 'TIER', tier: 'PRIMARY_REASONING', use_structured_output: true, enabled: true, escalation: null, ...config }, resolved: { model, assignment: config.tier || config.primary_model, note: null }, ...extra });
const model = (name, extra = {}) => ({ name, display_name: name, enabled: true, model_type: 'CUSTOM', provider: 'ollama_chat', underlying_model: name, supports_structured_output: true, supports_tools: true, supports_reasoning: null, supports_vision: null, context_window: 24576, max_context_window: 131072, max_output_tokens: null, managed: false, present_in_litellm: true, health_status: 'UNKNOWN', used_by: [], ...extra });
function overview() {
  return {
    gateway_enabled: true, llm_provider: 'litellm', active_version: 4,
    gateway: { gateway_enabled: true, direct_provider: 'openrouter', direct_label: 'OpenRouter', direct_model: 'openrouter/free', direct_ready: true, litellm_ready: true, litellm_base_url: 'http://localhost:4000' },
    connection: { base_url: 'http://localhost:4000', base_url_saved: 'http://localhost:4000', base_url_env: null, api_key_configured: true, api_key_hint: '…3456', sync_mode: 'admin_api', sync_modes: ['disabled', 'admin_api'], status: 'CONNECTED', litellm_version: '1.102.1', last_successful_sync_at: '2026-09-25T10:00:00', sync_status: 'SYNCED', draft_revision: 7, secret_storage: true },
    draft: { workers: {} }, active: { workers: {} },
    pending_changes: [{ path: 'effective.REVIEWER', label: 'Reviewer · effective model', before: 'local-reasoning', after: 'local-deep', kind: 'effective' }],
    workers: [worker('RANKER', 'Ranker', { tier: 'PRIMARY_REASONING' }, 'local-reasoning'), worker('REVIEWER', 'Reviewer', { mode: 'FIXED', primary_model: 'local-deep' }, 'local-deep', { min_context_tokens: 16384 }), worker('INBOX_CLASSIFIER', 'Inbox Classifier', { mode: 'AUTO_ROUTER', tier: 'FAST' }, 'jobhunt-auto')],
    models: [model('local-fast', { context_window: 4096, supports_reasoning: false }), model('local-reasoning', { supports_reasoning: true, used_by: ['RANKER'] }), model('local-deep', { supports_reasoning: false }), model('mystery', { provider: null, supports_structured_output: null, context_window: null, max_context_window: null })],
    tiers: { FAST: { model: 'local-fast', enabled: true, description: 'quick' }, PRIMARY_REASONING: { model: 'local-reasoning', enabled: true, description: '' }, DEEP_REVIEW: { model: 'local-deep', enabled: true, description: '' } },
    routing: { auto_router_enabled: true, auto_router_model_name: 'jobhunt-auto', manage_auto_router: true, classifier: 'HEURISTIC', classifier_model: null, classifier_plugin: null, tier_models: { SIMPLE: 'local-fast', MEDIUM: 'local-reasoning', COMPLEX: 'local-reasoning', REASONING: 'local-deep' }, default_model: null, send_metadata: true },
    options: { model_types: ['FAST', 'CUSTOM', 'AUTO_ROUTER'], routing_tiers: ['SIMPLE', 'MEDIUM', 'COMPLEX', 'REASONING'] },
  };
}
const flush = () => new Promise(r => setTimeout(r, 0));
async function open(tab = 'workers') {
  ui.S.screen = 'orchestration'; ui.orchState().workspace = 'models'; ui.AIM.tab = tab; ui.AIM.data = undefined; ui.render();
  await flush(); await flush();
}
const text = () => document.getElementById('view').textContent;
const call = (method, url) => fetchMock.mock.calls.filter(([u, o]) => u === url && (o?.method || 'GET') === method);
const body = (method, url) => JSON.parse(call(method, url).at(-1)[1].body);

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"me@example.com"},"max_upload_mb":10}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('confirm', vi.fn(() => true));
  routes = { 'GET /api/settings/ai': () => overview() };
  fetchMock = vi.fn(async (url, opt = {}) => {
    const handler = routes[`${opt.method || 'GET'} ${url.split('?')[0]}`];
    if (!handler) return response({});
    const out = handler(opt.body ? JSON.parse(opt.body) : undefined);
    return out instanceof Array && out.length === 2 && typeof out[1] === 'number' ? response(out[0], out[1]) : response(out);
  });
  if (!ui) {
    context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, setTimeout, clearTimeout, setInterval, clearInterval, console });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'ai_models.js', 'failover.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock; context.confirm = confirm;
  ui.S.forms = {}; ui.S.busy = {};
  Object.assign(ui.AIM, { tab: 'overview', data: undefined, loading: false, error: '', drawer: null, preview: null, runtime: null, versions: null, audit: null, tests: {}, ollama: null, adding: false, more: {}, refs: {}, fbEdit: null, failed: null });
  Object.assign(ui.D, { profile: { name: 'A', search_preferences: {} }, prefs: {}, settings: { ai: { saved: { stage_models: {} }, env: { llm_provider: 'openrouter', models: {}, stage_models: {} }, effective: {} }, configured: {}, keys: {} }, summary: { model: 'local-reasoning', application_mode: 'manual_approval', counts: {} } });
});

describe('AI Orchestration → Models & routing', () => {
  it('keeps the direct provider form in Settings, open when the route is direct and folded away otherwise', async () => {
    await open('settings');
    expect(document.querySelector('#view details[data-settings-details="aim-direct"] summary').textContent)
      .toContain('Direct provider · OpenRouter (used when the route is direct)');
    expect(document.querySelector('#view .aim-direct-models')).toBeNull();
    routes['GET /api/settings/ai'] = () => ({ ...overview(), gateway_enabled: false, llm_provider: 'openrouter',
      gateway: { ...overview().gateway, gateway_enabled: false } });
    await open('settings');
    const direct = document.querySelector('#view .aim-direct-models');
    expect(direct.textContent).toContain('Model defaults');
    expect(direct.querySelector('#ai-llm_provider')).not.toBeNull();
    expect(document.querySelector('#view details[data-settings-details="aim-litellm"]')).not.toBeNull();  // LiteLLM kept for switching back
    await open('workers');
    expect(text()).toContain('Workers call OpenRouter directly right now');
  });
  it('keeps provider settings off the Workers, Models and Reliability tabs', async () => {
    for (const tab of ['workers', 'models', 'reliability']) {
      await open(tab);
      expect(document.querySelector('#view #ai-llm_provider'), tab).toBeNull();
    }
  });
  it('chooses between LiteLLM and the direct provider in Settings → Route', async () => {
    routes['PUT /api/settings/ai/gateway'] = sent => ({ ...overview().gateway, gateway_enabled: sent.enabled });
    await open('settings');
    const buttons = [...document.querySelectorAll('#view .seg[aria-label="AI gateway"] button')];
    expect(buttons.map(b => b.textContent.trim())).toEqual(['LiteLLM gateway', 'OpenRouter (direct)']);
    expect(buttons[0].getAttribute('aria-pressed')).toBe('true');
    expect(buttons.every(b => b.type === 'button')).toBe(true);  // inside the settings form: never a submit
    confirm.mockReturnValueOnce(false);
    buttons[1].click();  // cancelled: the route stays as it is
    await flush();
    expect(call('PUT', '/api/settings/ai/gateway')).toHaveLength(0);
    expect(confirm.mock.calls.at(-1)[0]).toContain('Switch every AI worker to OpenRouter directly');
    buttons[1].click();
    await flush(); await flush(); await flush();
    expect(body('PUT', '/api/settings/ai/gateway')).toEqual({ enabled: false });
    expect(call('GET', '/api/settings/ai').length).toBeGreaterThan(1);  // the tab reloads to show the new route
  });
  it('shows the route, connection, version and each worker’s model and backups in one place', async () => {
    routes['GET /api/settings/ai'] = () => ({ ...overview(), workers: [worker('RANKER', 'Ranker', { tier: 'PRIMARY_REASONING', fallback_model: 'local-fast', secondary_models: ['local-deep'] }, 'local-reasoning'), ...overview().workers.slice(1)] });
    await open();
    expect(call('GET', '/api/settings/ai').length).toBe(1);
    expect(ui.AIM.tab).toBe('workers');
    expect(text()).toContain('LiteLLM'); expect(text()).toContain('Connected'); expect(text()).toContain('Active v4');
    expect(text()).toContain('1 change is saved but not applied yet');  // a draft left from before: Apply now or Undo
    const ranker = document.querySelector('select[data-worker="RANKER"]');
    expect(ranker.value).toBe('tier:PRIMARY_REASONING');
    expect(document.querySelector('select[data-worker="REVIEWER"]').value).toBe('model:local-deep');
    expect(document.querySelector('select[data-worker="INBOX_CLASSIFIER"]').value).toBe('auto');
    expect([...ranker.closest('tr').querySelectorAll('.aim-chain li')].map(li => li.textContent)).toEqual(['1local-fast', '2local-deep']);
    expect(ranker.closest('tr').querySelector('.aim-health').textContent).toBe('Healthy');
    expect(ranker.closest('tr').querySelector('.aim-wk-mono').textContent).toBe('RA');
    expect(document.querySelector('select[data-worker="REVIEWER"]').closest('tr').querySelector('.aim-wk-none').textContent).toBe('No backup');
    expect(text()).not.toContain('How a request finds its model');  // no overview diagram any more
  });
  it('labels every form control on every tab', async () => {
    for (const tab of ['workers', 'models', 'reliability', 'settings']) {
      await open(tab);
      for (const el of document.querySelectorAll('#settings-editor input:not([type="radio"]), #settings-editor select, #settings-editor textarea')) {
        expect(el.getAttribute('aria-label') || el.labels?.length, `${tab}: ${el.id || el.outerHTML}`).toBeTruthy();
      }
    }
  });

  it('opens a worker drawer that only offers options the model supports', async () => {
    await open('workers');
    ui.A['aim-worker']({ dataset: { worker: 'RANKER' } });
    expect(document.getElementById('aiw-reasoning_effort')).not.toBeNull(); // local-reasoning reasons
    ui.setF('aiw', 'target', 'model:local-deep'); ui.render();
    expect(document.getElementById('aiw-reasoning_effort')).toBeNull(); // local-deep does not
    expect(text()).toContain('does not support it');
    ui.setF('aiw', 'target', 'model:mystery'); ui.render();
    expect(text()).toContain('Structured Output Required');
    ui.setF('aiw', 'target', 'model:local-fast'); ui.render();
    expect(text()).toContain('may not satisfy Ranker requirements');
    // By default the drawer shows the model, the backups and on/off; the rest is folded away.
    const drawer = document.querySelector('.aim-drawer');
    const visible = [...drawer.querySelectorAll('select, input')].filter(el => !el.closest('details'));
    expect(visible.map(el => el.id)).toEqual(['aiw-target', 'aiw-backup_0', 'aiw-enabled']);
  });
  it('saves a worker and applies it straight away', async () => {
    routes['PUT /api/settings/ai/workers/RANKER'] = () => ({ worker: {}, draft_revision: 8 });
    routes['POST /api/settings/ai/apply'] = () => ({ status: 'SYNCED', version: 5 });
    await open('workers');
    ui.A['aim-worker']({ dataset: { worker: 'RANKER' } });
    ui.setF('aiw', 'temperature', '0.2'); ui.setF('aiw', 'context_window', '16384');
    ui.setF('aiw', 'esc_target', 'tier:DEEP_REVIEW'); ui.setF('aiw', 'esc_LOW_CONFIDENCE', true); ui.setF('aiw', 'esc_threshold', '0.85');
    await ui.A['aim-worker-save']({ dataset: { worker: 'RANKER' } });
    const sent = body('PUT', '/api/settings/ai/workers/RANKER');
    expect(sent).toMatchObject({ revision: 7, model_mode: 'TIER', tier_name: 'PRIMARY_REASONING', temperature: 0.2, context_window: 16384, max_output_tokens: null,
      escalation: { tier: 'DEEP_REVIEW', model: null, conditions: ['LOW_CONFIDENCE'], confidence_threshold: 0.85 } });
    expect(body('POST', '/api/settings/ai/apply')).toEqual({ revision: 7, base_version: 4 });
    expect(ui.AIM.drawer).toBeNull();
    expect(document.getElementById('toast').textContent).toBe('Ranker saved · v5');
  });

  it('offers a prompt token budget only to a worker that trims its prompt, and saves it', async () => {
    routes['GET /api/settings/ai'] = () => ({ ...overview(), workers: [
      worker('RANKER', 'Ranker', { tier: 'PRIMARY_REASONING', prompt_token_budget: 6000 }, 'local-reasoning', { trims_prompt: true }),
      ...overview().workers.slice(1)] });
    routes['PUT /api/settings/ai/workers/RANKER'] = () => ({ worker: {}, draft_revision: 8 });
    routes['POST /api/settings/ai/apply'] = () => ({ status: 'SYNCED', version: 5 });
    await open('workers');
    ui.A['aim-worker']({ dataset: { worker: 'REVIEWER' } });
    expect(document.getElementById('aiw-prompt_token_budget')).toBeNull();
    ui.A['aim-worker']({ dataset: { worker: 'RANKER' } });
    expect(document.getElementById('aiw-prompt_token_budget').value).toBe('6000');
    expect(document.querySelector('.aim-drawer').textContent).toContain('Required requirements are never left out');
    ui.setF('aiw', 'prompt_token_budget', '7000');
    await ui.A['aim-worker-save']({ dataset: { worker: 'RANKER' } });
    expect(body('PUT', '/api/settings/ai/workers/RANKER')).toMatchObject({ prompt_token_budget: 7000 });
  });

  it('gives the Embedder its own dialog: embedding models only, no tiers, backups or generation settings', async () => {
    const embedder = worker('EMBEDDER', 'Embedder', { mode: 'FIXED', primary_model: null, tier: null, enabled: false }, null,
      { capability: 'EMBEDDING', optional: true, structured_output_required: false, min_context_tokens: 0 });
    routes['GET /api/settings/ai'] = () => ({ ...overview(), workers: [...overview().workers, embedder],
      models: [...overview().models, model('nomic-embed-text', { model_type: 'EMBEDDING', embedding_dimensions: 768 }),
        model('qwen3-embedding:0.6b', { model_type: 'EMBEDDING', embedding_dimensions: 1024 })] });
    routes['PUT /api/settings/ai/workers/EMBEDDER'] = () => ({ worker: {}, draft_revision: 8 });
    routes['POST /api/settings/ai/apply'] = () => ({ status: 'SYNCED', version: 5 });
    await open('workers');
    ui.A['aim-worker']({ dataset: { worker: 'EMBEDDER' } });
    const options = [...document.querySelectorAll('#aiw-target option')].map(o => o.textContent);
    expect(options).toEqual(['Off (full-text retrieval only)', 'nomic-embed-text · 768 dims', 'qwen3-embedding:0.6b · 1024 dims']);
    for (const id of ['aiw-backup_0', 'aiw-temperature', 'aiw-esc_target', 'aiw-use_structured_output', 'aiw-enabled']) {
      expect(document.getElementById(id)).toBeNull();
    }
    ui.setF('aiw', 'target', 'model:qwen3-embedding:0.6b'); ui.render();
    expect(text()).toContain('Vector size: 1024 dimensions');
    await ui.A['aim-worker-save']({ dataset: { worker: 'EMBEDDER' } });
    expect(body('PUT', '/api/settings/ai/workers/EMBEDDER')).toEqual({ revision: 7, model_mode: 'FIXED', primary_model: 'qwen3-embedding:0.6b',
      tier_name: null, fallback_model: null, secondary_models: [], escalation: null, enabled: true });
  });

  it('changes a worker’s model from its row, mapping the choice to mode, model and tier', async () => {
    routes['PUT /api/settings/ai/workers/REVIEWER'] = () => ({});
    routes['PUT /api/settings/ai/workers/RANKER'] = () => ({});
    routes['PUT /api/settings/ai/workers/INBOX_CLASSIFIER'] = () => ({});
    routes['POST /api/settings/ai/apply'] = () => ({ status: 'PENDING', version: 5 });
    await open('workers');
    await ui.A['aim-worker-target']({ dataset: { worker: 'REVIEWER' }, value: 'tier:DEEP_REVIEW' });
    expect(body('PUT', '/api/settings/ai/workers/REVIEWER')).toEqual({ revision: 7, model_mode: 'TIER', tier_name: 'DEEP_REVIEW', primary_model: 'local-deep' });
    expect(document.getElementById('toast').textContent).toContain('restart LiteLLM');
    await ui.A['aim-worker-target']({ dataset: { worker: 'RANKER' }, value: 'model:local-fast' });
    expect(body('PUT', '/api/settings/ai/workers/RANKER')).toEqual({ revision: 7, model_mode: 'FIXED', primary_model: 'local-fast', tier_name: 'PRIMARY_REASONING' });
    await ui.A['aim-worker-target']({ dataset: { worker: 'INBOX_CLASSIFIER' }, value: 'auto' });
    expect(body('PUT', '/api/settings/ai/workers/INBOX_CLASSIFIER')).toMatchObject({ model_mode: 'AUTO_ROUTER', tier_name: 'FAST' });
  });

  it('sets a model for a worker added after the account was configured (no config row yet)', async () => {
    routes['GET /api/settings/ai'] = () => ({ ...overview(), workers: [...overview().workers, { worker_type: 'BROWSER_NAVIGATOR', label: 'Browser Navigator', description: 'Next step', structured_output_required: true, min_context_tokens: 16384, has_prompt: true, config: null, resolved: { model: null, assignment: null, note: 'nothing assigned' } }] });
    routes['PUT /api/settings/ai/workers/BROWSER_NAVIGATOR'] = () => ({});
    routes['POST /api/settings/ai/apply'] = () => ({ status: 'PENDING', version: 5 });
    await open('workers');
    expect(text()).toContain('Browser Navigator');
    await ui.A['aim-worker-target']({ dataset: { worker: 'BROWSER_NAVIGATOR' }, value: 'model:local-reasoning' });
    expect(body('PUT', '/api/settings/ai/workers/BROWSER_NAVIGATOR')).toEqual({ revision: 7, model_mode: 'FIXED', primary_model: 'local-reasoning', tier_name: null });
    expect(document.getElementById('toast').textContent).not.toContain('Cannot read properties');
  });

  it('shows a refused save with its reasons, keeps the drawer, and undoes it on request', async () => {
    routes['PUT /api/settings/ai/workers/RANKER'] = () => ({});
    routes['POST /api/settings/ai/apply'] = () => [{ detail: { message: 'invalid', validation: { errors: [{ message: 'A model can appear only once', subject: 'Ranker' }], warnings: [{ message: 'unknown JSON support', subject: 'Ranker' }, { message: 'unknown JSON support', subject: 'Ranker' }, { message: 'Reviewer has no usable model', subject: 'Reviewer' }] } } }, 422];
    routes['POST /api/settings/ai/discard'] = () => ({ status: 'DISCARDED' });
    await open('workers');
    ui.A['aim-worker']({ dataset: { worker: 'RANKER' } });
    ui.setF('aiw', 'backup_0', 'local-reasoning');
    await ui.A['aim-worker-save']({ dataset: { worker: 'RANKER' } });
    expect(ui.AIM.drawer).toBe('RANKER'); expect(ui.S.forms.aiw.backup_0).toBe('local-reasoning');
    expect(document.getElementById('toast').textContent).toContain('Not saved: Ranker: A model can appear only once');
    expect(text()).toContain('Not saved.');
    expect(ui.AIM.failed.warnings.map(w => w.text)).toEqual(['Ranker: unknown JSON support', 'Reviewer has no usable model']);
    await ui.A['aim-discard']();
    expect(call('POST', '/api/settings/ai/discard').length).toBe(1);
    expect(ui.AIM.failed).toBeNull(); expect(ui.S.forms.aiw).toBeUndefined();
    routes['POST /api/settings/ai/apply'] = () => [{ detail: 'LiteLLM refused the deployment' }, 502];
    await ui.A['aim-apply']();
    expect(ui.AIM.failed.errors.map(e => e.text)).toEqual(['LiteLLM refused the deployment']);
  });
  it('links each refused-save problem to where it is fixed, and names every worker a repeated warning is about', async () => {
    const issue = (path, subject, message) => ({ path, subject, message });
    routes['POST /api/settings/ai/apply'] = () => [{ detail: { message: 'invalid', validation: {
      errors: [issue('workers.RANKER.fallback_model', 'Ranker', 'The fallback model cannot be the primary model'), issue('workers.REVIEWER.escalation', 'Reviewer', 'Escalation model local-fast is disabled'), issue('models.local-deep.fallback_models.0', 'Model local-deep', 'Fallback model mystery is disabled'), issue('routing.tier_models.SIMPLE', 'Auto Router', 'Map the SIMPLE routing tier to a model'), issue('somewhere', undefined, 'Odd')],
      warnings: [issue('workers.RANKER.primary_model', 'Ranker', 'unknown JSON'), issue('workers.REVIEWER.primary_model', 'Reviewer', 'unknown JSON')] } } }, 422];
    await open('workers');
    await ui.A['aim-apply']();
    const view = document.getElementById('view');
    expect(view.querySelectorAll('.aim-failed [data-act="aim-fix"]').length).toBe(6);  // 4 errors with a place, 2 workers
    expect(text()).toContain('unknown JSON — Ranker, Reviewer');
    const fix = path => view.querySelector(`[data-act="aim-fix"][data-path="${path}"]`);
    ui.A['aim-fix'](fix('workers.RANKER.fallback_model'));
    expect(ui.AIM.drawer).toBe('RANKER'); expect(document.activeElement.id).toBe('aiw-backup_0');
    ui.A['aim-fix'](fix('workers.REVIEWER.escalation'));
    expect(ui.AIM.drawer).toBe('REVIEWER'); expect(document.activeElement.id).toBe('aiw-esc_target');
    ui.A['aim-fix'](fix('models.local-deep.fallback_models.0'));
    expect(ui.AIM.tab).toBe('models'); expect(ui.AIM.drawer).toBeNull(); expect(ui.AIM.fbEdit).toBe('local-deep');
    expect(document.activeElement.dataset.model).toBe('local-deep');
    ui.A['aim-fix'](fix('routing.tier_models.SIMPLE'));
    expect(document.activeElement.id).toBe('aim-route-tier_SIMPLE');
    expect(view.querySelector('[data-act="aim-fix"][data-path="somewhere"]')).toBeNull();
  });
  it('shows requests too big for their model with the fixes that fit, and applies the one a person picks', async () => {
    const size = (extra = {}) => ({ worker_type: 'RANKER', label: 'Ranker', model: 'local-reasoning', count: 36, last_at: '2026-10-25T22:37:42',
      limits: [{ deployment: 'openai/gpt-oss-120b', kind: 'TPM', counts: 'total', limit: 8000, requested: 10159 }, { deployment: 'qwen/qwen3.8-27b', kind: 'OTPM', counts: 'output', limit: 1000, requested: 1667 }],
      typical_input: 6327, typical_output: 800, input_over: false, output_cap: { value: 1000, typical_output: 800, known_safe: true, applied: false, current: null },
      models: [{ name: 'local-deep', context_window: 131072, proven: true, in_chain: false }], has_backup: false, ...extra });
    routes['GET /api/settings/ai'] = () => ({ ...overview(), request_size: [size(), size({ worker_type: 'REVIEWER', label: 'Reviewer', input_over: true, output_cap: null, models: [] })] });
    routes['PUT /api/settings/ai/workers/RANKER'] = () => ({});
    routes['POST /api/settings/ai/apply'] = () => ({ status: 'APPLIED', version: 5 });
    await open('workers');
    expect(text()).toContain('Requests too big for their model');
    expect(text()).toContain('36 requests refused as too large');
    expect(text()).toContain('8K prompt + answer tokens/min (TPM)');
    expect(text()).toContain('they are not cut short');
    expect(text()).toContain("The prompt alone (~6.2K) is over local-reasoning's limit");  // the Reviewer: no cap is offered
    expect(text()).toContain('No model in your catalog is known to have room');
    expect(text()).toContain('Last 7 days · 2 workers · 72 requests refused');
    const view = document.getElementById('view');
    const [rankerCard, reviewerCard] = view.querySelectorAll('.aim-size');
    // The meter draws the limit these requests overrun most: 6.3K + 800 against the 8K prompt + answer limit.
    expect(rankerCard.querySelector('.aim-size-meter').getAttribute('aria-label')).toContain('against a limit of 7.8K prompt + answer');
    expect(rankerCard.querySelector('.aim-size-verdict.brand [data-act="aim-size-cap"]')).not.toBeNull();
    expect(reviewerCard.querySelector('.aim-size-verdict.warn').textContent).toContain('Switch to a model with more room');
    expect(reviewerCard.querySelector('[data-act="aim-tab"][data-tab="models"]')).not.toBeNull();
    const switchBtn = rankerCard.querySelector('[data-act="aim-size-use"]');
    expect(switchBtn.getAttribute('aria-label')).toBe('Use local-deep instead of local-reasoning for Ranker');
    expect(switchBtn.classList.contains('primary')).toBe(false);  // the cap is this card's primary action
    expect(rankerCard.textContent).toContain('Recommended');
    await ui.A['aim-size-cap'](view.querySelector('[data-act="aim-size-cap"]'));
    expect(body('PUT', '/api/settings/ai/workers/RANKER')).toEqual({ revision: 7, max_output_tokens: 1000 });
    await ui.A['aim-size-backup'](view.querySelector('[data-act="aim-size-backup"]'));
    expect(body('PUT', '/api/settings/ai/workers/RANKER')).toEqual({ revision: 7, fallback_model: 'local-deep', secondary_models: [] });
    await ui.A['aim-size-use'](view.querySelector('[data-act="aim-size-use"]'));
    expect(body('PUT', '/api/settings/ai/workers/RANKER')).toMatchObject({ model_mode: 'FIXED', primary_model: 'local-deep', fallback_model: null, secondary_models: [] });
    // A refusing backup or escalation model is replaced where it is; the worker's own model stays.
    const use = (role, replace) => ({ dataset: { worker: 'RANKER', model: 'local-deep', replace, role } });
    const ranker = overview().workers.find(w => w.worker_type === 'RANKER');
    ui.AIM.data.workers = ui.AIM.data.workers.map(w => w.worker_type === 'RANKER' ? { ...ranker, config: { ...ranker.config, fallback_model: 'local-fast', secondary_models: ['mystery'], escalation: { model: 'mystery', tier: null, conditions: ['LOW_CONFIDENCE'], confidence_threshold: 0.8 } } } : w);
    await ui.A['aim-size-use'](use('backup', 'local-fast'));
    expect(body('PUT', '/api/settings/ai/workers/RANKER')).toEqual({ revision: 7, fallback_model: 'local-deep', secondary_models: ['mystery'] });
    ui.AIM.data.workers = ui.AIM.data.workers.map(w => w.worker_type === 'RANKER' ? { ...ranker, config: { ...ranker.config, escalation: { model: 'mystery', tier: null, conditions: ['LOW_CONFIDENCE'], confidence_threshold: 0.8 } } } : w);
    await ui.A['aim-size-use'](use('escalation', 'mystery'));
    expect(body('PUT', '/api/settings/ai/workers/RANKER')).toEqual({ revision: 7, escalation: { model: 'local-deep', tier: null, conditions: ['LOW_CONFIDENCE'], confidence_threshold: 0.8 } });
  });
  it('makes switching the primary fix when no cap helps, folds extra models away and marks existing backups', async () => {
    const m = (name, extra = {}) => ({ name, context_window: 131072, proven: false, in_chain: false, ...extra });
    routes['GET /api/settings/ai'] = () => ({ ...overview(), request_size: [{ worker_type: 'RANKER', label: 'Ranker', model: 'local-reasoning', role: 'primary', count: 1, last_at: '2026-10-25T22:37:42',
      limits: [{ deployment: 'openai/gpt-oss-120b', kind: 'ITPM', counts: 'input', limit: 6000, requested: 7000 }], typical_input: 7000, typical_output: null, input_over: true, output_cap: null,
      models: [m('local-deep', { proven: true, in_chain: true }), m('local-fast'), m('mystery', { context_window: null })], has_backup: true }] });
    await open('workers');
    const card = document.querySelector('#view .aim-size');
    expect(card.textContent).toContain('1 request refused as too large');
    const [first, second] = card.querySelectorAll('.aim-size-model');
    expect(first.querySelector('[data-act="aim-size-use"]').classList.contains('primary')).toBe(true);
    expect(first.querySelector('[data-act="aim-size-backup"]')).toBeNull();
    expect(first.textContent).toContain('Backup');
    expect(first.textContent).toContain('Proven for this worker');
    expect(second.querySelector('[data-act="aim-size-backup"]')).not.toBeNull();
    const more = card.querySelector('details[data-settings-details="aim-size-more:RANKER:local-reasoning"]');
    expect(more.textContent).toContain('1 more model');
    expect(more.textContent).toContain('context unknown');
    expect(card.querySelector('details[data-settings-details="aim-size-limits:RANKER:local-reasoning"]').textContent).toContain('prompt tokens/min (ITPM)');
  });
  it('saves backups in order as the fallback and secondary models, growing the list as it fills', async () => {
    routes['PUT /api/settings/ai/workers/RANKER'] = () => ({ worker: {}, draft_revision: 8 });
    await open('workers');
    ui.A['aim-worker']({ dataset: { worker: 'RANKER' } });
    expect(document.querySelectorAll('#view select[data-field^="backup_"]')).toHaveLength(1);
    ui.setF('aiw', 'backup_0', 'local-fast'); ui.render();
    expect(document.querySelectorAll('#view select[data-field^="backup_"]')).toHaveLength(2);
    ui.setF('aiw', 'backup_2', 'local-deep'); ui.setF('aiw', 'backup_3', 'local-fast');  // a gap and a repeat
    ui.render();
    expect(text()).not.toContain('A backup cannot be the model itself');
    await ui.A['aim-worker-save']({ dataset: { worker: 'RANKER' } });
    expect(body('PUT', '/api/settings/ai/workers/RANKER')).toMatchObject({ fallback_model: 'local-fast', secondary_models: ['local-deep'] });
  });
  it('reloads and keeps the drawer when someone else changed the draft', async () => {
    routes['PUT /api/settings/ai/workers/RANKER'] = () => [{ detail: { message: 'Someone else changed the AI configuration' } }, 409];
    await open('workers');
    ui.A['aim-worker']({ dataset: { worker: 'RANKER' } });
    await ui.A['aim-worker-save']({ dataset: { worker: 'RANKER' } });
    expect(ui.AIM.drawer).toBe('RANKER');
    expect(call('GET', '/api/settings/ai').length).toBe(2);
    expect(document.getElementById('toast').textContent).toContain('Someone else changed');
  });

  it('applies a waiting draft from the strip, or undoes it', async () => {
    routes['POST /api/settings/ai/apply'] = () => ({ status: 'SYNCED', version: 5 });
    routes['POST /api/settings/ai/discard'] = () => ({ status: 'DISCARDED' });
    await open();
    expect(document.querySelector('[data-act="aim-apply"]')).not.toBeNull();
    await ui.A['aim-apply']();
    expect(body('POST', '/api/settings/ai/apply')).toEqual({ revision: 7, base_version: 4 });
    expect(document.getElementById('toast').textContent).toBe('Applied · v5');
    await ui.A['aim-discard']();
    expect(document.getElementById('toast').textContent).toBe('Changes undone');
    routes['GET /api/settings/ai'] = () => ({ ...overview(), pending_changes: [] });
    await open();
    expect(document.querySelector('[data-act="aim-apply"]')).toBeNull();  // nothing waiting: no notice
  });
  it('reports a conflicting save and reloads without leaving an error notice', async () => {
    routes['PUT /api/settings/ai/tiers/FAST'] = () => ({});
    routes['POST /api/settings/ai/apply'] = () => [{ detail: { message: 'The active configuration is now v5' } }, 409];
    await open('models');
    await ui.A['aim-tier-model']({ dataset: { tier: 'FAST' }, value: 'local-deep' });
    expect(document.getElementById('toast').textContent).toContain('now v5');
    expect(ui.AIM.failed).toBeNull();
  });
  it('tags each model Local or Cloud on its card, in the pickers and in the backup chain, and leaves unknown untagged', async () => {
    const models = [model('local-fast', { hosting: 'LOCAL' }), model('beam:cloud', { hosting: 'CLOUD' }), model('Groq', { provider: 'litellm', hosting: null })];
    routes['GET /api/settings/ai'] = () => ({ ...overview(), models, workers: [worker('RANKER', 'Ranker', { mode: 'FIXED', primary_model: 'beam:cloud', fallback_model: 'local-fast', secondary_models: ['Groq'] }, 'beam:cloud')] });
    await open();
    const ranker = document.querySelector('select[data-worker="RANKER"]');
    const labels = [...ranker.querySelectorAll('optgroup[label="Models"] option')].map(o => o.textContent);
    expect(labels).toEqual(['local-fast · Local', 'beam:cloud · Cloud', 'Groq']);
    expect(ranker.value).toBe('model:beam:cloud');  // the label changed, the value saved did not
    const chain = [...ranker.closest('tr').querySelectorAll('.aim-chain li')];
    expect(chain.map(li => li.querySelector('.aim-host')?.textContent ?? null)).toEqual(['Local', null]);
    await open('models');
    const tag = name => document.querySelector(`.aim-mcard[data-model="${name}"] .aim-mcard-tags .aim-host`);
    expect(tag('local-fast').textContent).toBe('Local'); expect(tag('local-fast').classList.contains('local')).toBe(true);
    expect(tag('beam:cloud').textContent).toBe('Cloud'); expect(tag('beam:cloud').classList.contains('cloud')).toBe(true);
    expect(tag('Groq')).toBeNull();
  });
  it('tags a router with both kinds behind it as Local + Cloud', async () => {
    routes['GET /api/settings/ai'] = () => ({ ...overview(), models: [model('Ollama Ro', { provider: 'litellm', hosting: 'MIXED' })] });
    await open('models');
    const tag = document.querySelector('.aim-mcard[data-model="Ollama Ro"] .aim-host');
    expect(tag.textContent).toBe('Local + Cloud'); expect(tag.classList.contains('mixed')).toBe(true);
  });
  it('shows each model as a card: monogram, health, stats and the first workers that use it', async () => {
    routes['GET /api/settings/ai'] = () => ({ ...overview(), models: [model('local-reasoning', { health_status: 'HEALTHY', health_latency_ms: 90, used_by: ['RANKER', 'REVIEWER', 'INBOX_CLASSIFIER', 'TAILOR'] }), model('ollama_chat/qwen2.5', { enabled: false })] });
    await open('models');
    const [first, second] = document.querySelectorAll('.aim-mcard');
    expect(first.querySelector('.aim-mcard-mono').textContent).toBe('LR');
    expect(second.querySelector('.aim-mcard-mono').textContent).toBe('QW');  // letters only, and the provider prefix is not part of it
    expect(second.querySelector('.aim-mcard-name').textContent).toBe('ollama_chat/qwen2.5');  // the whole name, never cut
    expect(second.querySelector('.aim-mcard-path').textContent).toBe('ollama_chat/');
    expect(second.querySelector('.aim-mcard-name').classList.contains('ellipsis')).toBe(false);
    expect(first.querySelector('.aim-mcard-path')).toBeNull();  // no provider path: nothing dimmed
    expect(first.querySelector('.aim-health').textContent).toBe('Healthy');
    expect(first.querySelector('.aim-mcard-stats').textContent).toContain('24K'); expect(first.querySelector('.aim-mcard-stats').textContent).toContain('90ms');
    expect([...first.querySelectorAll('.aim-user')].map(x => x.textContent)).toEqual(['Ranker', 'Reviewer', 'Inbox Classifier', '+1 more']);
    expect(first.querySelector('.aim-mcard-users').title).toContain('TAILOR');
    expect(second.classList.contains('off')).toBe(true); expect(second.textContent).toContain('Not used by any worker');
    expect(first.getAttribute('style')).toMatch(/--h:\d+/);
  });

  it('sets a model’s own fallbacks and rest time on its card, applied at once', async () => {
    routes['GET /api/settings/ai'] = () => ({ ...overview(), models: [model('local-reasoning', { fallback_models: ['local-fast'], rest_seconds: 900, health_latency_ms: 147510 }), model('local-fast', { health_latency_ms: 840 }), model('local-deep')] });
    routes['PUT /api/settings/ai/models/local-reasoning'] = () => ({});
    routes['POST /api/settings/ai/apply'] = () => ({ status: 'SYNCED', version: 5 });
    await open('models');
    const [card, fast] = document.querySelectorAll('.aim-mcard');
    expect(card.querySelector('.aim-mcard-fb .aim-chain').textContent).toBe('1local-fast');
    expect(card.querySelector('.aim-mcard-fb').textContent).toContain('Rests 15 minutes after a rate limit or failures');
    expect(fast.querySelector('.aim-mcard-fb').textContent).toContain('No fallback');
    expect(fast.querySelector('.aim-mcard-fb').textContent).toContain('Rests as set in Reliability');
    expect(card.querySelector('.aim-mcard-stats').textContent).toContain('147.5s');  // long latencies in seconds
    expect(fast.querySelector('.aim-mcard-stats').textContent).toContain('840ms');
    ui.A['aim-fb-edit']({ dataset: { name: 'local-reasoning' } });
    const options = [...document.querySelectorAll('#fb-local-reasoning-m0 option')].map(o => o.value);
    expect(options).not.toContain('local-reasoning');  // never itself
    expect(document.querySelectorAll('select[data-form="fb-local-reasoning"][data-field^="m"]')).toHaveLength(2);
    ui.setF('fb-local-reasoning', 'm1', 'local-deep'); ui.setF('fb-local-reasoning', 'rest', '3600');
    await ui.A['aim-fb-save']({ dataset: { name: 'local-reasoning' } });
    expect(body('PUT', '/api/settings/ai/models/local-reasoning')).toEqual({ revision: 7, fallback_models: ['local-fast', 'local-deep'], rest_seconds: 3600 });
    expect(document.getElementById('toast').textContent).toBe('Fallbacks for local-reasoning saved · v5');
    expect(ui.AIM.fbEdit).toBeNull();
    ui.A['aim-fb-edit']({ dataset: { name: 'local-reasoning' } });
    ui.setF('fb-local-reasoning', 'm0', ''); ui.setF('fb-local-reasoning', 'rest', '');
    await ui.A['aim-fb-save']({ dataset: { name: 'local-reasoning' } });
    expect(body('PUT', '/api/settings/ai/models/local-reasoning')).toEqual({ revision: 7, fallback_models: [], rest_seconds: null });
  });

  it('deletes a model after asking, lists where it is still used, and restores a removed one', async () => {
    routes['GET /api/settings/ai'] = () => ({ ...overview(), models: [model('local-fast', { managed: true }), model('whisper')],
      removed_models: [{ name: 'old-model', removed_at: '2026-10-10T11:00:00' }] });
    routes['POST /api/settings/ai/models/local-fast/remove'] = () => [{ detail: { message: 'local-fast is still used by: tier FAST', references: ['tier FAST', 'Ranker (backup 1)'] } }, 409];
    routes['POST /api/settings/ai/models/whisper/remove'] = () => ({ removed: 'whisper', dropped: ['Tailor (backup 1)'] });
    routes['POST /api/settings/ai/models/old-model/restore'] = () => ({ restored: 'old-model' });
    routes['POST /api/settings/ai/apply'] = () => ({ status: 'SYNCED', version: 5 });
    await open('models');
    expect(document.querySelector('details[data-settings-details="aim-removed"] summary').textContent).toContain('Removed models (1)');
    ui.A['aim-model-more']({ dataset: { name: 'local-fast' } });
    expect(document.querySelector('[data-act="aim-model-remove"][data-name="local-fast"]').textContent).toBe('Delete model');
    await ui.A['aim-model-remove']({ dataset: { name: 'local-fast', managed: '1' } });
    expect(confirm.mock.calls.at(-1)[0]).toContain('Its deployment is also removed from LiteLLM');
    expect(call('POST', '/api/settings/ai/apply')).toHaveLength(0);  // refused: nothing applied
    const card = [...document.querySelectorAll('.aim-mcard')].find(c => c.textContent.includes('local-fast'));
    expect(card.querySelector('[role="alert"]').textContent).toContain('Still used by: tier FAST, Ranker (backup 1)');
    await ui.A['aim-model-remove']({ dataset: { name: 'whisper', managed: '' } });
    expect(confirm.mock.calls.at(-1)[0]).toContain('It stays in LiteLLM');
    expect(body('POST', '/api/settings/ai/models/whisper/remove')).toEqual({ revision: 7 });
    expect(document.getElementById('toast').textContent).toBe('whisper deleted · no longer a backup for Tailor · v5');
    confirm.mockReturnValueOnce(false);
    ui.A['aim-model-remove']({ dataset: { name: 'whisper', managed: '' } });
    expect(call('POST', '/api/settings/ai/models/whisper/remove')).toHaveLength(1);  // cancelled: not sent again
    await ui.A['aim-model-restore']({ dataset: { name: 'old-model' } });
    expect(document.getElementById('toast').textContent).toBe('old-model restored · v5');
  });

  it('shows known, unknown and missing capabilities, and tests a model', async () => {
    routes['POST /api/settings/ai/models/mystery/test'] = () => ({ success: true, latency_ms: 420, model_group: 'mystery', underlying_model: 'ollama_chat/x', structured_output: true });
    await open('models');
    expect(document.querySelector('[title="reasoning: unknown"]')).not.toBeNull();
    expect(document.querySelector('[title="reasoning: no"]')).not.toBeNull();
    expect(text()).toContain('provider unknown');
    await ui.A['aim-model-test']({ dataset: { name: 'mystery' } });
    expect(ui.AIM.tests['model:mystery'].latency_ms).toBe(420);
    expect(text()).toContain('420 ms');
  });

  it('adds a local Ollama model from the server Ollama list', async () => {
    routes['GET /api/settings/ai/ollama'] = () => ({ api_base: 'http://localhost:11434', reachable: true, models: [{ name: 'llama3.2:3b', context_length: 131072 }] });
    routes['POST /api/settings/ai/models'] = () => ({ model: {} });
    await open('models');
    await ui.A['aim-add-toggle'](); await flush();
    expect(document.getElementById('aim-add-ollama_model').textContent).toContain('128K ctx');
    ui.setF('aim-add', 'litellm_model_name', 'local-small'); ui.setF('aim-add', 'ollama_model', 'llama3.2:3b'); ui.setF('aim-add', 'num_ctx', '4096');
    await ui.A['aim-add']();
    expect(body('POST', '/api/settings/ai/models')).toMatchObject({ revision: 7, litellm_model_name: 'local-small', ollama_model: 'llama3.2:3b', num_ctx: 4096 });
  });

  it('turns the auto router off and on through its classifier', async () => {
    await open('models');
    expect(document.querySelector('details[data-settings-details="aim-router"] summary').textContent).toContain('Auto router · Heuristic');
    expect(document.getElementById('aim-route-tier_SIMPLE').value).toBe('local-fast');
    ui.setF('aim-route', 'classifier', 'DISABLED'); ui.render();
    expect(document.getElementById('aim-route-tier_SIMPLE')).toBeNull();
    expect(ui.aimRoutingPayload()).toEqual({ revision: 7, auto_router_enabled: false });
    ui.setF('aim-route', 'classifier', 'LLM'); ui.setF('aim-route', 'tier_REASONING', 'local-reasoning'); ui.render();
    expect(document.getElementById('aim-route-classifier_model')).not.toBeNull();
    expect(ui.aimRoutingPayload()).toMatchObject({ auto_router_enabled: true, classifier: 'LLM', tier_models: { SIMPLE: 'local-fast', REASONING: 'local-reasoning' } });
  });

  it('never shows the saved key and sends a key only when one was typed', async () => {
    routes['PUT /api/settings/ai/connection'] = () => ({});
    routes['POST /api/settings/ai/test-connection'] = () => ({ reachable: true, authenticated: false, discovery: false, models: 0, latency_ms: 12, status: 'AUTH_FAILED', label: 'Authentication failed', error: 'LiteLLM refused the API key' });
    await open('settings');
    expect(document.getElementById('aim-conn-api_key').value).toBe('');
    expect(document.getElementById('view').innerHTML).not.toContain('sk-');
    ui.setF('aim-conn', 'base_url', 'http://litellm:4000');
    await ui.A['aim-conn-save']();
    expect(body('PUT', '/api/settings/ai/connection')).toEqual({ base_url: 'http://litellm:4000' });
    await ui.A['aim-conn-test']();
    expect(text()).toContain('✓ LiteLLM reachable'); expect(text()).toContain('✗ Authentication');
  });

  it('lists version history and rolls back as a new version', async () => {
    routes['GET /api/settings/ai/versions'] = () => ({ versions: [{ id: 14, version: 4, changed_by: 'me', source: 'USER', summary: 'Reviewer', created_at: '2026-09-25T10:00:00' }, { id: 12, version: 2, changed_by: 'system', source: 'MIGRATION', summary: 'Initial', created_at: '2026-09-24T10:00:00' }] });
    routes['GET /api/settings/ai/audit'] = () => ({ entries: [{ action: 'APPLY', result: 'REJECTED', actor: 'me', source: 'USER', error: 'bad', created_at: '2026-09-25T10:00:00' }] });
    routes['POST /api/settings/ai/versions/12/restore'] = () => ({ version: 5 });
    await open('settings'); await flush(); await flush();
    expect(text()).toContain('v4'); expect(text()).toContain('Initial'); expect(text()).toContain('REJECTED');
    expect(document.querySelectorAll('[data-act="aim-restore"]').length).toBe(1); // not on the active version
    await ui.A['aim-restore']({ dataset: { id: 12, version: 2 } });
    expect(body('POST', '/api/settings/ai/versions/12/restore')).toEqual({ base_version: 4 });
    expect(document.getElementById('toast').textContent).toBe('Restored as v5');
  });

  it('shows an error with a retry when the overview cannot load', async () => {
    routes['GET /api/settings/ai'] = () => [{ detail: 'boom' }, 500];
    await open();
    expect(text()).toContain('could not be loaded: boom');
    routes['GET /api/settings/ai'] = () => overview();
    ui.A['aim-reload'](); await flush(); await flush();
    expect(text()).toContain('Active v4');
  });
});

describe('Monitoring → model routing', () => {
  it('shows per-worker inference share, escalation and auto router tiers', () => {
    const html = ui.renderMonRouting({ requests: 30, workers: [{ worker_type: 'REVIEWER', requests: 10, inference_ms: 9000, inference_share: 0.6, escalation_rate: 0.2, fallback_rate: 0, schema_failure_rate: 0, p95_latency_ms: 5000, configured: [['DEEP_REVIEW', 10]], models: [['ollama_chat/qwen2.5:32b', 10]] }],
      models: [{ model: 'ollama_chat/qwen2.5:32b', requests: 10, inference_ms: 9000, inference_share: 0.6, schema_failure_rate: 0.1, failure_rate: 0, p95_latency_ms: 5000, avg_confidence: 0.8 }],
      auto_router: { requests: 20, tiers: [{ tier: 'SIMPLE', requests: 15, share: 0.75 }] }, escalations: [{ worker_type: 'REVIEWER', reason: 'LOW_CONFIDENCE', requests: 2 }], slowest_model: 'ollama_chat/qwen2.5:32b', most_schema_failures: null });
    expect(html).toContain('Model routing'); expect(html).toContain('SIMPLE 75%'); expect(html).toContain('Reviewer 2× (LOW_CONFIDENCE)');
    expect(ui.renderMonRouting({ requests: 0 })).toBe('');
  });
});

describe('Settings → AI Models actions', () => {
  const ok = () => ({});
  it('switches tabs, follows old tab names and checks what workers run', async () => {
    routes['GET /api/settings/ai/runtime'] = () => ({ live_error: null, workers: [
      { label: 'Ranker', desired: 'a', active: 'a', state: 'SYNCED' }, { label: 'Reviewer', desired: 'b', active: 'c', state: 'PENDING' },
      { label: 'Tailor', desired: 'd', active: 'd', state: 'OUT_OF_SYNC' }] });
    await open();
    expect([...document.querySelectorAll('#view .aim-ws-nav button b')].map(b => b.textContent)).toEqual(['Workers', 'Models', 'Reliability', 'Settings']);
    ui.A['aim-tab']({ dataset: { tab: 'models' } }); expect(ui.AIM.tab).toBe('models');
    for (const [old, now] of [['overview', 'Workers'], ['routing', 'Models'], ['litellm', 'Settings'], ['advanced', 'Settings']]) {
      ui.A['aim-tab']({ dataset: { tab: old } });
      expect(document.querySelector('#view .aim-ws-nav [aria-selected="true"]').textContent).toContain(now);
    }
    await ui.A['aim-runtime']();
    expect(text()).toContain('Synced ✓'); expect(text()).toContain('Out of sync'); expect(text()).toContain('LiteLLM was read just now');
    routes['GET /api/settings/ai'] = () => ({ ...overview(), gateway_enabled: false, llm_provider: 'openrouter', pending_changes: [], connection: { ...overview().connection, status: 'UNKNOWN', base_url: null } });
    await open();
    expect(text()).toContain('Direct · OpenRouter'); expect(text()).not.toContain('Not tested');  // no LiteLLM status on the direct route
  });
  it('opens the drawer from a row, shows the auto router state and closes it', async () => {
    await open();
    ui.A['aim-worker']({ dataset: { worker: 'INBOX_CLASSIFIER' } });
    expect(ui.AIM.tab).toBe('workers');
    expect(text()).toContain('requests go to jobhunt-auto');
    ui.setF('aiw', 'context_window', '999999'); ui.setF('aiw', 'target', 'model:local-reasoning'); ui.setF('aiw', 'backup_0', 'local-reasoning'); ui.render();
    expect(text()).toContain('A backup cannot be the model itself'); expect(text()).toContain('exceeds the 24K');
    ui.A['aim-drawer-close']();
    expect(ui.AIM.drawer).toBeNull(); expect(ui.S.forms.aiw).toBeUndefined();
    ui.A['aim-worker']({ dataset: { worker: 'NOPE' } });
    expect(document.querySelector('.aim-drawer')).toBeNull();
  });
  it('runs a worker test and shows success and failure', async () => {
    let answer = { success: true, answered_by: 'ollama_chat/gpt-oss:20b', latency_ms: 800, input_tokens: 40, output_tokens: 12, schema_ok: true, routing: { model_group: 'local-reasoning', routing_tier: 'MEDIUM', escalated: true, escalation_reason: 'LOW_CONFIDENCE' } };
    routes['POST /api/settings/ai/workers/RANKER/test'] = () => answer;
    await open('workers');
    await ui.A['aim-worker-test']({ dataset: { worker: 'RANKER' } });
    expect(body('POST', '/api/settings/ai/workers/RANKER/test')).toEqual({ configuration: 'draft' });
    expect(text()).toContain('group local-reasoning · tier MEDIUM · 800 ms · 40+12 tokens · schema ✓ · escalated (LOW_CONFIDENCE)');
    answer = { success: false, error: 'LiteLLM is unreachable', schema_ok: null };
    await ui.A['aim-worker-test']({ dataset: { worker: 'RANKER' } });
    expect(text()).toContain('LiteLLM is unreachable');
    answer = { success: false, schema_ok: false, model: 'local-reasoning' };
    await ui.A['aim-worker-test']({ dataset: { worker: 'RANKER' } });
    expect(text()).toContain('✗ model local-reasoning · schema ✗');
  });

  it('changes models: enable, type, stop managing, sync — each applied at once', async () => {
    routes['PUT /api/settings/ai/models/local-fast'] = ok;
    routes['DELETE /api/settings/ai/models/local-fast'] = ok;
    routes['POST /api/settings/ai/models/sync'] = () => ({ models: 5, added: ['new-one'] });
    routes['POST /api/settings/ai/apply'] = () => ({ status: 'SYNCED', version: 5 });
    routes['GET /api/settings/ai'] = () => ({ ...overview(), models: [model('local-fast', { managed: true, present_in_litellm: false, health_status: 'HEALTHY', health_latency_ms: 90 }), model('bad', { enabled: false, health_status: 'UNHEALTHY' })] });
    await open('models');
    expect(text()).toContain('Managed'); expect(text()).toContain('Not in LiteLLM'); expect(text()).toContain('Healthy'); expect(text()).toContain('Unhealthy');
    expect(document.querySelector('[data-act-change="aim-model-type"]')).toBeNull();  // type and management sit behind ⋯
    ui.A['aim-model-more']({ dataset: { name: 'local-fast' } });
    expect(document.querySelector('[data-act-change="aim-model-type"]').dataset.name).toBe('local-fast');
    expect(text()).toContain('Stop managing');
    ui.A['aim-model-more']({ dataset: { name: 'local-fast' } });
    expect(document.querySelector('[data-act-change="aim-model-type"]')).toBeNull();
    await ui.A['aim-model-toggle']({ dataset: { name: 'local-fast', on: '' } });
    expect(body('PUT', '/api/settings/ai/models/local-fast')).toEqual({ revision: 7, enabled: false });
    expect(document.getElementById('toast').textContent).toBe('local-fast disabled · v5');
    routes['PUT /api/settings/ai/models/local-fast'] = () => ({ dropped: ['Ranker (backup 1)', 'Reviewer (backup 2)', 'Ranker (backup 3)', "local-deep's fallbacks"] });
    await ui.A['aim-model-toggle']({ dataset: { name: 'local-fast', on: '' } });
    expect(document.getElementById('toast').textContent).toBe("local-fast disabled · no longer a backup for Ranker, Reviewer, local-deep's fallbacks · v5");
    routes['PUT /api/settings/ai/models/local-fast'] = ok;
    await ui.A['aim-model-type']({ dataset: { name: 'local-fast' }, value: 'FAST' });
    expect(body('PUT', '/api/settings/ai/models/local-fast')).toEqual({ revision: 7, model_type: 'FAST' });
    await ui.A['aim-model-unmanage']({ dataset: { name: 'local-fast' } });
    expect(call('DELETE', '/api/settings/ai/models/local-fast?revision=7').length).toBe(1);
    confirm.mockReturnValueOnce(false);
    ui.A['aim-model-unmanage']({ dataset: { name: 'local-fast' } });
    await ui.A['aim-sync']();
    expect(document.getElementById('toast').textContent).toBe('5 models discovered, 1 new · v5');
    expect(call('POST', '/api/settings/ai/apply').length).toBe(5);
    routes['GET /api/settings/ai'] = () => ({ ...overview(), models: [] });
    await open('models');
    expect(text()).toContain('No models yet');
  });
  it('adds a model even when Ollama is unreachable, and requires a name', async () => {
    routes['GET /api/settings/ai/ollama'] = () => [{ detail: 'down' }, 500];
    await open('models');
    await ui.A['aim-add-toggle'](); await flush();
    expect(text()).toContain('did not answer'); expect(document.getElementById('aim-add-ollama_model').tagName).toBe('INPUT');
    ui.A['aim-add']();
    expect(document.getElementById('toast').textContent).toContain('Give the model');
    await ui.A['aim-add-toggle']();
    expect(ui.AIM.adding).toBe(false);
  });

  it('edits tiers and saves routing', async () => {
    routes['PUT /api/settings/ai/tiers/FAST'] = ok;
    routes['PUT /api/settings/ai/tiers/CHEAP'] = ok;
    routes['PUT /api/settings/ai/routing'] = ok;
    await open('models');
    await ui.A['aim-tier-model']({ dataset: { tier: 'FAST' }, value: 'local-reasoning' });
    expect(body('PUT', '/api/settings/ai/tiers/FAST')).toEqual({ revision: 7, litellm_model_name: 'local-reasoning' });
    await ui.A['aim-tier-toggle']({ dataset: { tier: 'FAST', on: '' } });
    expect(body('PUT', '/api/settings/ai/tiers/FAST')).toEqual({ revision: 7, enabled: false });
    ui.A['aim-tier-add']();
    expect(call('PUT', '/api/settings/ai/tiers/CHEAP').length).toBe(0);
    ui.setF('aim-tier', 'new', 'cheap');
    await ui.A['aim-tier-add']();
    expect(body('PUT', '/api/settings/ai/tiers/CHEAP')).toEqual({ revision: 7, enabled: true });
    ui.setF('aim-route', 'classifier', 'CUSTOM'); ui.setF('aim-route', 'classifier_plugin', 'jobhunt_classifier.router');
    ui.setF('aim-route', 'manage_auto_router', false); ui.setF('aim-route', 'default_model', ''); ui.render();
    expect(text()).toContain('integrations/litellm/jobhunt_classifier.py');
    await ui.A['aim-route-save']();
    expect(body('PUT', '/api/settings/ai/routing')).toEqual({ revision: 7, auto_router_enabled: true, classifier: 'CUSTOM', classifier_plugin: 'jobhunt_classifier.router', manage_auto_router: false, default_model: null });
    expect(ui.S.forms['aim-route']).toBeUndefined();
  });

  it('saves a key, removes it, verifies sync and explains file mode', async () => {
    routes['PUT /api/settings/ai/connection'] = ok;
    routes['POST /api/settings/ai/verify'] = () => ({ status: 'OUT_OF_SYNC', differences: ['missing local-small'], missing_models: ['tier-x'] });
    routes['GET /api/settings/ai'] = () => ({ ...overview(), connection: { ...overview().connection, secret_storage: false, base_url_saved: '', base_url_env: 'http://env:4000', managed_config_path: '/srv/jobhunt_models.yaml', restart_command_configured: false, last_error: 'old error', status: 'AUTH_FAILED', sync_status: 'PENDING', last_sync_at: '2026-09-25T10:00:00', last_sync_error: 'restart needed' } });
    await open('settings');
    expect(text()).toContain('Key storage is unavailable'); expect(text()).toContain('Using the server default http://env:4000');
    expect(text()).toContain('/srv/jobhunt_models.yaml'); expect(text()).toContain('restart LiteLLM after saving'); expect(text()).toContain('old error');
    ui.setF('aim-conn', 'api_key', 'sk-new-key-1234'); ui.setF('aim-conn', 'sync_mode', 'disabled');
    await ui.A['aim-conn-save']();
    expect(body('PUT', '/api/settings/ai/connection')).toEqual({ api_key: 'sk-new-key-1234', sync_mode: 'disabled' });
    expect(ui.S.forms['aim-conn']).toBeUndefined();
    await ui.A['aim-conn-key-remove']();
    expect(body('PUT', '/api/settings/ai/connection')).toEqual({ api_key: '' });
    await ui.A['aim-verify']();
    expect(text()).toContain('OUT_OF_SYNC — missing local-small — not served: tier-x');
  });

  it('says when a save needs a LiteLLM restart or changed nothing', async () => {
    routes['PUT /api/settings/ai/tiers/FAST'] = () => ({});
    routes['POST /api/settings/ai/apply'] = () => ({ status: 'PENDING', version: 5 });
    await open('models');
    await ui.A['aim-tier-model']({ dataset: { tier: 'FAST' }, value: 'local-deep' });
    expect(document.getElementById('toast').textContent).toBe('Tier FAST saved · v5 — restart LiteLLM to load it');
    routes['POST /api/settings/ai/apply'] = () => ({ status: 'UNCHANGED', version: 4 });
    await ui.A['aim-tier-model']({ dataset: { tier: 'FAST' }, value: 'local-fast' });
    expect(document.getElementById('toast').textContent).toBe('Tier FAST saved · nothing changed');
  });
  it('handles an empty history and a failed history load', async () => {
    routes['GET /api/settings/ai/versions'] = () => [{ detail: 'nope' }, 500];
    await open('settings'); await flush(); await flush();
    expect(ui.AIM.versions).toEqual([]); expect(text()).toContain('Nothing recorded yet');
    confirm.mockReturnValueOnce(false);
    ui.A['aim-restore']({ dataset: { id: 1, version: 1 } });
    expect(call('POST', '/api/settings/ai/versions/1/restore').length).toBe(0);
  });

  it('lives in AI Orchestration: a node opens its worker’s editor and the workspace switch keeps the URL', async () => {
    window.scrollTo = () => {};
    ui.A['orch-settings-ai']({ dataset: { worker: 'RANKER' } });
    await flush(); await flush();
    expect(location.hash).toBe('#orchestration/models');
    expect(ui.AIM.tab).toBe('workers'); expect(ui.AIM.drawer).toBe('RANKER');
    expect(document.querySelector('#view .aim-drawer h3').textContent).toBe('Ranker');
    expect(document.querySelector('#view .aim-ws-nav [aria-selected="true"]').textContent).toContain('Workers');
    expect(document.querySelector('#view select[data-worker="RANKER"]').closest('tr').textContent).toContain('local-reasoning');
    ui.A['orch-settings-ai']({ dataset: {} });
    await flush(); await flush();
    expect(ui.AIM.tab).toBe('settings');
    ui.A['orch-workspace']({ dataset: { id: 'pipeline' } });
    expect(location.hash).toBe('#orchestration');
    history.replaceState(null, '', '#orchestration/models'); ui.readHash();
    expect(ui.orchState().workspace).toBe('models');
  });
});
