import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'render', 'go', 'wkState', 'loadWorkers', 'wkPoll', 'wkPollDelay', 'scheduleWkPoll',
  'stopWkPoll', 'readHash', 'WK_KINDS', 'fmtDateTime', 'wkLastRun'];
let ui, context, fetchMock, confirmMock, routes;

const response = (data, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(data) });
const counts = (running = 0, queued = 0, retrying = 0) => ({ running, queued, retrying, active: running + queued + retrying });
const summary = (over = {}) => ({
  cooldowns: [], active: 4, running: 1,
  workers: [
    { kind: 'parsing', label: 'JD parsing', description: 'Parses postings', counts: counts(1, 1, 1), scheduler: { enabled: true, interval_minutes: 2, running: false, next_run_at: null } },
    { kind: 'recovery', label: 'JD recovery', description: 'Reads postings', counts: counts(), scheduler: { enabled: true, interval_minutes: 2 } },
    { kind: 'ranking', label: 'Ranking', description: 'Scores fit', counts: counts(0, 1), scheduler: { enabled: true, interval_minutes: 2 } },
    { kind: 'tailoring', label: 'Tailoring', description: 'Tailors packets', counts: counts(), scheduler: { enabled: true, poll_seconds: 2, concurrency: 2 } },
    { kind: 'reviewing', label: 'Reviewing', description: 'Checks packets', view_of: 'tailoring', counts: counts(1), scheduler: { enabled: true, poll_seconds: 2, concurrency: 2 } },
    { kind: 'indexing', label: 'Indexing', description: 'Embeds verified evidence', counts: counts(), scheduler: { enabled: true, interval_minutes: 10 } }],
  ...over });
const row = (over = {}) => ({ task_id: 11, job_id: 7, title: 'Staff SWE', company: 'Canva', kind: 'parsing', status: 'RUNNING',
  stage: 'running', attempt: 1, max_attempts: 3, started_at: '2026-10-04T10:00:00', next_attempt_at: null, last_error: null,
  error_category: null, run_now: false, can_kill: true, can_run_now: false, can_remove: false, ...over });
const parsing = () => ({ kind: 'parsing', metrics: { tasks_created: 92, completed: 56, failed: 6, retrying: 22, success_rate: 0.903,
  average_duration_ms: 88300, jobs_parsed: 64, failures_by_category: {}, top_errors: [],
  queue: { pending: 22, processing: 8, oldest_pending_at: null },
  llm: { requests: 182, failed: 95, input_tokens: 260611, output_tokens: 1132032, estimated_cost: 0, average_latency_ms: 139500, models: {} },
  scheduler: null },
  rows: [row(), row({ task_id: 12, status: 'RETRYING', can_kill: true, can_run_now: true, attempt: 2, next_attempt_at: '2099-01-01T00:00:00', last_error: 'LLMRateLimitError: timed out' }),
    row({ task_id: 13, status: 'FAILED', can_kill: false, can_run_now: true, last_error: 'ValidationError' })] });

const performance = kind => ({ kind, tasks_created: 12, completed: 9, failed: 1, stopped: 1, skipped: 1, active: 0, retried: 2,
  by_status: { COMPLETED: 9, FAILED: 1 }, success_rate: 0.9, average_duration_ms: 42000, median_wait_seconds: 30,
  failures_by_category: {}, top_errors: [{ error: 'Model timed out', tasks: 1 }],
  llm: { requests: 14, failed: 1, input_tokens: 5000, output_tokens: 900, estimated_cost: null, average_latency_ms: 3100, models: { 'gpt-oss:20b': 14 } },
  ...(kind === 'ranking' ? { ranking: { rankings: 9, decisions: { QUALIFIED: 6, ARCHIVED: 3 }, qualified: 6, archived: 3, average_fit: 71.5, median_fit: 74 } } : {}),
  ...(kind === 'tailoring' || kind === 'reviewing' ? { review: { scored: 9, median_score: 78, lowest_score: 55, highest_score: 91, auto_approved: 4, untailored: 1, skip_mode: 1 } } : {}) });
const hours = () => [...Array(24)].map((_, i) => ({ hour: `2026-10-04T${String(i).padStart(2, '0')}:00:00`, queued: i === 23 ? 4 : 0, done: i === 22 ? 6 : 0, failed: i === 23 ? 2 : 0 }));
const activity = () => ({ kind: 'parsing', hours: hours(), done: 6, failed: 2, queued: 4, success_rate: 0.75, median_run_seconds: 95, median_wait_seconds: 30 });
const settings = () => ({ application_mode: 'manual_approval', min_review_score: 70, ranking_concurrency: 3, tailoring_concurrency: 2,
  ranking_interval_minutes: 5, processing_maximums: { ranking_concurrency: 5, tailoring_concurrency: 2 }, ranking_interval_server_minutes: 2,
  rate_limits: { enabled: true, base_seconds: 30, multiplier: 2, max_seconds: 900 }, auto_tailor: { enabled: false } });

const field = (name, type, def, min = 1, max = 10) => ({ name, label: name.replace(/_/g, ' '), help: '', type, min, max, default: def });
const workerSettings = (editable = true) => ({
  account: {
    parsing: { values: { auto: true, max_attempts: 3, retry_seconds: 120 }, fields: [field('auto', 'bool', true), field('max_attempts', 'int', 3), field('retry_seconds', 'int', 120, 10, 86400)] },
    recovery: { values: { auto: true, max_attempts: 3, retry_seconds: 300, use_browser: true, use_model: true }, fields: [field('auto', 'bool', true), field('max_attempts', 'int', 3), field('retry_seconds', 'int', 300, 10, 86400), field('use_browser', 'bool', true), field('use_model', 'bool', true)] },
    ranking: { values: { auto: false, max_attempts: 3, retry_base_seconds: 60, retry_max_seconds: 3600 }, fields: [field('auto', 'bool', true), field('max_attempts', 'int', 3), field('retry_base_seconds', 'int', 60, 10, 86400), field('retry_max_seconds', 'int', 3600, 10, 86400)] },
    tailoring: { values: { auto: true, max_attempts: 2 }, fields: [field('auto', 'bool', true), field('max_attempts', 'int', 2, 1, 5)] } },
  server: { jd_parse_batch_size: { worker: 'parsing', label: 'Tasks per run', help: '', min: 1, max: 200, value: 10, default: 10, overridden: false },
    ranking_interval_minutes: { worker: 'ranking', label: 'Run every (minutes)', help: '', min: 0, max: 1440, value: 2, default: 2, overridden: false } },
  server_editable: editable, schedules: {} });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c","is_admin":false}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('scrollTo', vi.fn());
  confirmMock = vi.fn(() => true);
  vi.stubGlobal('confirm', confirmMock);
  routes = { summary: summary(), parsing: parsing(), activity: activity(), settings: settings(), workerSettings: workerSettings(), fail: false };
  fetchMock = vi.fn(async (url, opt = {}) => {
    if (routes.fail && url.startsWith('/api/workers')) return response({ detail: 'Workers are unavailable' }, 503);
    if (opt.method === 'POST' && url.endsWith('/run-queued')) { routes.runQueued = url; return response({ kind: 'ranking', released: 3, task_ids: [1, 2, 3] }); }
    if (opt.method === 'POST' && url.endsWith('/nudge')) { routes.nudged = url; routes.nudgeBody = opt.body ? JSON.parse(opt.body) : null; return response({ kind: 'parsing', to: 'Ranking', moved: [4, 5], skipped: [{ job_id: 6, reason: 'Dismissed in triage' }], message: null }); }
    if (opt.method === 'POST' && url.includes('/run-now')) return response(row({ status: 'QUEUED', run_now: true }));
    if (opt.method === 'POST' && url.includes('/cooldowns/reset')) return response({ cleared: ['OPENROUTER'] });
    if (opt.method === 'POST' && url.includes('kill')) return response({ stopped: 2, running: 1, cooldowns_cleared: url.endsWith('kill') ? [] : ['OPENROUTER'] });
    if (url.endsWith('/clear')) { routes.clears = [...(routes.clears || []), `${opt.method} ${url}`]; return response({ kind: 'parsing', cleared_at: opt.method === 'POST' ? '2026-10-05T10:00:00' : null }); }
    if (opt.method === 'POST' && url.endsWith('/retry')) { routes.retryPost = { url, body: JSON.parse(opt.body) }; return response({ kind: 'parsing', retried: routes.retryPost.body.task_ids.slice(1).map(id => ({ task_id: id, new_task_id: id + 100, job_id: 1 })), skipped: [{ task_id: routes.retryPost.body.task_ids[0], reason: 'Already run again as task #99' }] }); }
    if (opt.method === 'PUT' && url === '/api/workers/settings') { routes.accountPut = JSON.parse(opt.body); return response({ account: routes.workerSettings.account }); }
    if (opt.method === 'PUT' && url === '/api/workers/server-settings') { routes.serverPut = JSON.parse(opt.body); return response({ server: routes.workerSettings.server }); }
    if (opt.method === 'PATCH' && url === '/api/settings') { routes.patched = JSON.parse(opt.body); return response({ ...routes.settings, ...routes.patched }); }
    return response(url.includes('/workspace/summary') ? ui.D.summary
      : url === '/api/workers' ? routes.summary
      : url === '/api/workers/settings' ? routes.workerSettings
      : url.endsWith('/activity') ? routes.activity
      : url.includes('/performance?') ? (routes.perfCalls = [...(routes.perfCalls || []), url], performance(url.split('/')[3]))
      : url === '/api/settings' ? routes.settings
      : url.includes('/history') ? (routes.historyUrl = url, routes.history || { task: row(), events: [] })
      : url.startsWith('/api/workers/parsing') ? routes.parsing
      : url.startsWith('/api/workers/reviewing') ? { kind: 'reviewing', rows: [
        row({ task_id: 51, job_id: 8, kind: 'reviewing', stage: 'reviewing', title: 'Backend Lead', company: 'Stripe' }),
        row({ task_id: 52, job_id: 9, kind: 'reviewing', status: 'SUCCEEDED', stage: 'done', can_kill: false, reviewer_score: 82, finished_at: '2026-10-04T11:00:00' })] }
      : url.startsWith('/api/workers/indexing') ? (routes.indexingUrl = url, { kind: 'indexing', status_counts: { all: 2 }, rows: [
        row({ task_id: 61, job_id: null, kind: 'indexing', title: 'Evidence #12', company: 'index v3 (building)', subject: { type: 'evidence', id: 12, index_version: 3, sensitive: false },
          model: { model: 'text-embedding-3-small', route: 'LITELLM', route_label: 'LiteLLM', source: 'next', backups: [] } }),
        row({ task_id: 62, job_id: null, kind: 'indexing', status: 'STOPPED', title: 'Saved answer #4', company: 'index v3 (building)', can_kill: false, can_run_now: true,
          last_error: 'Stopped by you', subject: { type: 'knowledge_answer', id: 4, index_version: 3, sensitive: true } })] })
      : url.startsWith('/api/workers/resume_parsing') ? (routes.resumeUrl = url, { kind: 'resume_parsing', status_counts: { all: 1 }, rows: [
        row({ task_id: 71, job_id: null, kind: 'resume_parsing', title: 'cv.pdf', company: 'v6', subject: { type: 'resume', id: 3 } })] })
      : url.startsWith('/api/workers/recovery') ? { kind: 'recovery', rows: [], metrics: { by_result: {} }, diagnostics: { http: {}, playwright: {}, llm: {}, settings: {} } }
      : url.startsWith('/api/ranking/tracker') ? { counts: { filtering: 0, queued: 1, processing: 0, retry_scheduled: 0, waiting: 0, completed: 0, failed: 0, skipped: 0, qualified: 0, archived: 0 },
        progress: { finished: 0, total: 1 }, concurrency: 2, interval_minutes: 5, active: [{ id: 31, job_id: 9, title: 'AI Engineer', company: 'Grab', status: 'PENDING', attempt_count: 0, max_attempts: 3, reason: 'new' }],
        recent: [], locations: [], worker: { enabled: true, interval_minutes: 2 }, auto_rank: true }
      : url.startsWith('/api/tailor-tasks') ? [{ id: 41, job_id: 5, title: 'Backend Lead', company: 'Stripe', mode: 'tailor', status: 'running', stage: 'tailoring', progress: 25, started_at: '2026-10-04T10:00:00' }]
      : {});
  });
  vi.stubGlobal('fetch', fetchMock);
  if (!ui) {
    context = createContext({ document, window, location, history: window.history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock; context.confirm = confirmMock;
  ui.stopWkPoll();
  window.history.replaceState(null, '', '#workers/parsing');  // a hash left by an earlier test must not switch tabs
  Object.assign(ui.S, { forms: {}, busy: {}, screen: 'workers', wk: null, rf: { q: '', location: 'all' } });
  for (const key of ['wk', 'wkData', 'wkAct', 'wkPerf', 'ranking', 'tasks', 'settings', 'wkSettings']) delete ui.D[key];
  ui.D.summary = { profile: {}, counts: {}, application_mode: 'manual_approval', model: 'm', total_jobs: 1,
    auto_search: { schedule: 'manual', setups: 0, roles: 0, locations: 0, next_run: null, last_run: null } };
});

const view = () => document.getElementById('view');
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise(r => setTimeout(r, 0)); };
async function open(tab = 'parsing') { ui.wkState().tab = tab; await ui.loadWorkers(); ui.render(); return view(); }
const posts = () => fetchMock.mock.calls.filter(([, opt]) => opt?.method === 'POST').map(([u]) => u);

describe('Running workers', () => {
  it('sits in the SYSTEM group between AI Orchestration and Monitoring, with a live count', async () => {
    await open();
    const system = [...document.querySelectorAll('#nav .nav-group')].find(g => g.querySelector('.nav-caps').textContent === 'SYSTEM');
    const items = [...system.querySelectorAll('.nav-btn')].map(a => a.textContent.trim());
    expect(items).toEqual(['AI Orchestration—', 'Running workers4', 'Monitoring—']);
  });

  it('has a tab per worker with its queue size, kept in the URL', async () => {
    await open();
    const tabs = [...view().querySelectorAll('[data-act="wk-tab"]')].map(b => b.textContent.trim());
    expect(tabs).toEqual(['Domain check', 'JD parsing 3', 'JD recovery', 'Ranking 1', 'Job insights', 'Tailoring', 'Reviewing 1', 'Indexing', 'Resume parsing', '⚙ Settings']);
    ui.A['wk-tab']({ dataset: { id: 'recovery' } });
    expect(location.hash).toBe('#workers/recovery');
    await flush();
    expect(view().textContent).toContain('JD recovery');
  });

  it('shows the qualification graph only once it is scheduled or has work', async () => {
    const extra = { kind: 'qualification', label: 'Qualification', description: 'LangGraph qualification',
      counts: { running: 0, queued: 2, retrying: 0, active: 2 }, scheduler: { enabled: true, interval_minutes: 1 } };
    routes.summary.workers.push(extra);
    await open();
    const tabs = [...view().querySelectorAll('[data-act="wk-tab"]')].map(b => b.textContent.trim());
    expect(tabs).toContain('Qualification 2');
  });

  it('shows the tailoring tasks at the review step on a Reviewing tab, with their scores', async () => {
    const v = await open('reviewing');
    expect(v.textContent).toContain('Median reviewer score');
    expect(v.textContent).toContain('78');
    expect(v.textContent).toContain('runs as the last step of each Tailoring task');
    const rows = [...v.querySelectorAll('.wk-row')];
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain('reviewing');
    expect(rows[0].querySelector('[data-act="wk-kill"]').dataset.kind).toBe('reviewing');
    expect(rows[1].textContent).toContain('reviewer score 82');
  });

  it('gives Ranking, Job insights, Tailoring and Reviewing a Performance card from the server, kept through Clear finished', async () => {
    for (const [tab, extra] of [['ranking', 'Qualified'], ['insights', 'Model calls'], ['tailoring', 'Auto-approved'], ['reviewing', 'Master resume reviewed']]) {
      const v = await open(tab);
      const card = [...v.querySelectorAll('.card')].find(c => c.querySelector('.card-title')?.textContent === 'Performance');
      expect(card, tab).toBeTruthy();
      expect(card.textContent).toContain(extra);
      expect(card.textContent).toContain('90%');
      expect(v.textContent).toContain('gpt-oss:20b');
      expect(routes.perfCalls.at(-1)).toBe(`/api/workers/${tab}/performance?range=7d`);
    }
    // Clearing hides the list only: the card is read again from the server, not from the rows.
    await ui.A['wk-clear']({ dataset: { tab: 'reviewing', n: 1 } });
    await flush();
    expect(view().textContent).toContain('Median reviewer score');
    ui.A['wk-range']({ dataset: { id: '30d' } });
    await flush();
    expect(routes.perfCalls.at(-1)).toBe('/api/workers/reviewing/performance?range=30d');
  });

  it('shows the JD parsing metrics that moved from Monitoring, and its tasks with their controls', async () => {
    const v = await open();
    expect(v.textContent).toContain('Parse tasks');
    expect(v.textContent).toContain('90.3%');
    const rows = [...v.querySelectorAll('.wk-row')];
    expect(rows.map(r => r.querySelector('.orch-chip').textContent.trim())).toEqual(['● Running', '↻ Retrying', '✕ Failed']);
    expect(rows[0].querySelector('[data-act="wk-kill"]')).not.toBeNull();
    expect(rows[0].querySelector('[data-act="wk-run-now"]')).toBeNull();  // a running task cannot be run "now"
    expect(rows[1].textContent).toContain('attempt 2 of 3');
    expect(rows[1].textContent).toContain('timed out');
    expect(rows[2].querySelector('[data-act="wk-kill"]')).toBeNull();
    expect(rows[2].querySelector('[data-act="wk-run-now"]')).not.toBeNull();
  });

  it('shows each active task’s model and route, and what moved it off its model', async () => {
    const base = parsing();
    base.rows = [
      row({ model: { model: 'nemotron-3-super:cloud', route: 'LITELLM', route_label: 'LiteLLM', via: 'tier DEEP_REVIEW', source: 'used', backups: ['Openrouter'] },
        model_change: { kind: 'FAILOVER', from: 'nemotron-3-super:cloud', to: 'Openrouter', reason: 'its model was resting' } }),
      row({ task_id: 12, status: 'QUEUED', can_kill: false, can_run_now: true, attempt: 0,
        model: { model: 'openrouter/free', route: 'OPENROUTER', route_label: 'OpenRouter', via: null, source: 'next', backups: [] }, model_change: null }),
      row({ task_id: 13, status: 'FAILED', can_kill: false, can_run_now: true })];
    routes.parsing = base;
    const w = await open();
    const rows = [...w.querySelectorAll('.wk-row')];
    expect(w.querySelector('.wk-head').textContent).toContain('Model & route');
    expect(rows[0].querySelector('.wk-route').textContent).toBe('LiteLLM');
    expect(rows[0].querySelector('.wk-model').textContent).toContain('nemotron-3-super:cloud');
    expect(rows[0].querySelector('.wk-model small').textContent).toBe('in use · tier DEEP_REVIEW');
    expect(rows[0].querySelector('.wk-change').classList.contains('t-bad')).toBe(true);
    expect(rows[0].querySelector('.wk-change').textContent).toContain('Failed over');
    expect(rows[0].querySelector('.wk-change s').textContent).toBe('nemotron-3-super:cloud');
    expect(rows[0].querySelector('.wk-change').title).toContain('its model was resting');
    expect(rows[1].querySelector('.wk-route.direct').textContent).toBe('OpenRouter');
    expect(rows[1].querySelector('.wk-model small').textContent).toBe('next request');
    expect(rows[1].querySelector('.wk-change').textContent).toContain('No change');
    expect(rows[2].querySelector('.wk-model.empty')).not.toBeNull();  // a finished task shows no model
    base.rows = [row({ task_id: 14, status: 'COMPLETED', model: { model: 'old-model', route: 'LITELLM', route_label: 'LiteLLM', via: null, source: 'used', backups: [] }, model_change: null }),
      row({ task_id: 15, status: 'FAILED', model: { model: null, source: 'none' }, model_change: null })];
    const done = [...(await open()).querySelectorAll('.wk-row')];
    expect(done[0].querySelector('.wk-model small').textContent).toBe('used');
    expect(done[0].querySelector('.wk-change').textContent).toContain('No change');
    expect(done[1].querySelector('.wk-model.nocall').textContent).toBe('No model call');
  });

  it('kills one task, and asks before killing a whole worker or every worker', async () => {
    await open();
    await ui.A['wk-kill']({ dataset: { kind: 'parsing', id: '11' } });
    expect(posts()).toContain('/api/workers/parsing/tasks/11/kill');
    expect(document.getElementById('toast').textContent).toContain('Stopped 2 tasks (1 mid-run; anything they return is discarded). They stay out of the queue until you retry them');
    confirmMock.mockReturnValueOnce(false);
    await ui.A['wk-kill-all']({ dataset: { kind: 'parsing' } });
    expect(posts()).not.toContain('/api/workers/parsing/kill-all');
    await ui.A['wk-kill-all']({ dataset: { kind: 'parsing' } });
    expect(confirmMock.mock.calls.at(-1)[0]).toContain('rate-limit backoff');
    expect(posts()).toContain('/api/workers/parsing/kill-all');
    expect(document.getElementById('toast').textContent).toContain('backoff cleared for openrouter');
    await ui.A['wk-kill-everything']();
    expect(posts()).toContain('/api/workers/kill-all');
  });

  it('lists the retrieval indexer’s entries, which have no job, with the same controls', async () => {
    const v = await open('indexing');
    const rows = [...v.querySelectorAll('.wk-row')];
    expect(rows.map(r => r.querySelector('.small.b').textContent.trim())).toEqual(['Evidence #12 · index v3 (building)', 'Saved answer #4 · index v3 (building) question only']);
    expect(rows.every(r => !r.querySelector('[data-act="open-job"]'))).toBe(true);
    expect(rows[0].querySelector('[data-act="wk-kill"]')).not.toBeNull();
    expect(rows[0].textContent).toContain('text-embedding-3-small');
    expect(rows[1].querySelector('[data-act="wk-run-now"]')).not.toBeNull();
    expect(rows[1].querySelector('[data-wk-select="indexing"]')).not.toBeNull();  // a stopped entry can be retried in bulk
    expect(v.querySelector('#wk-q-indexing').getAttribute('placeholder')).toContain('#source id');
    expect(routes.indexingUrl).toMatch(/^\/api\/workers\/indexing\?/);
    await ui.A['wk-kill']({ dataset: { kind: 'indexing', id: '61' } });
    expect(posts()).toContain('/api/workers/indexing/tasks/61/kill');
  });

  it('has a Resume parsing tab listing each resume version, with the same controls', async () => {
    routes.summary = summary({ workers: [...summary().workers,
      { kind: 'resume_parsing', label: 'Resume parsing', description: 'Structures resumes', counts: counts(1), scheduler: { enabled: true, interval_minutes: 1 } }] });
    const v = await open('resume_parsing');
    expect([...v.querySelectorAll('[data-act="wk-tab"]')].map(b => b.textContent.trim())).toContain('Resume parsing 1');
    expect(routes.resumeUrl).toMatch(/^\/api\/workers\/resume_parsing\?/);
    expect(v.textContent).toContain('cv.pdf');
    expect(v.querySelector('#wk-q-resume_parsing').getAttribute('placeholder')).toContain('#resume id');
    await ui.A['wk-kill']({ dataset: { kind: 'resume_parsing', id: '71' } });
    expect(posts()).toContain('/api/workers/resume_parsing/tasks/71/kill');
  });
  it('runs a task now and says it is first in line', async () => {
    await open();
    await ui.A['wk-run-now']({ dataset: { kind: 'parsing', id: '12' } });
    expect(posts()).toContain('/api/workers/parsing/tasks/12/run-now');
    expect(document.getElementById('toast').textContent).toContain('runs next');
    routes.parsing = { ...parsing(), rows: [row({ status: 'RETRYING', run_now: true, can_run_now: true, attempt: 1 })] };
    await open();
    expect(view().querySelector('.wk-row').textContent).toContain('First in line');
  });

  it('shows an application run waiting for the person as such, never as first in line, and with no model', async () => {
    routes.parsing = { ...parsing(), rows: [row({ kind: 'application', status: 'QUEUED', run_now: true, can_run_now: false, waiting_for: 'YOU', backend: 'playwright', waiting_detail: { kind: 'NEEDS_INPUT', questions: 11, confirmations: 0 }, model: { model: null, source: 'none' }, model_change: null })] };
    const text = (await open()).querySelector('.wk-row').textContent;
    expect(text).toContain('Waiting for you');
    expect(text).toContain('answer 11 questions in the Knowledge bank');
    expect(text).not.toContain('First in line');
    expect(text).toContain('No model call');
    expect(view().querySelector('.wk-row [data-act="wk-run-now"]')).toBeNull();
  });

  it('points the person at the browser window when fields are highlighted there', async () => {
    routes.parsing = { ...parsing(), rows: [row({ kind: 'application', status: 'QUEUED', can_run_now: false, waiting_for: 'YOU', backend: 'browser_use_webui', waiting_detail: { kind: 'NEEDS_HUMAN', reason: 'FIELDS_NEED_YOU', fields: 2, takeover: true, continued: false } })] };
    const text = (await open()).querySelector('.wk-row').textContent;
    expect(text).toContain('Waiting for you in the browser window: fill 2 highlighted fields: it carries on by itself when you finish, or press Continue');
    // Done in the window: say so from here, without opening the Apply kit.
    const done = view().querySelector('.wk-row [data-act="wk-continue"]');
    expect(done.textContent).toContain('I’m done — continue');
    expect(view().querySelector('.wk-row [data-act="open-kit"]')).not.toBeNull();
    await ui.A['wk-continue']({ dataset: { id: done.dataset.id } });
    expect(posts()).toContain('/api/application-executions/11/continue');
  });

  it('offers no Continue for a run that already continued or waits for answers, not the window', async () => {
    routes.parsing = { ...parsing(), rows: [row({ kind: 'application', status: 'QUEUED', can_run_now: false, waiting_for: 'YOU', waiting_detail: { kind: 'NEEDS_HUMAN', reason: 'FIELDS_NEED_YOU', fields: 2, takeover: true, continued: true } })] };
    expect((await open()).querySelector('.wk-row [data-act="wk-continue"]')).toBeNull();
    routes.parsing = { ...parsing(), rows: [row({ kind: 'application', status: 'QUEUED', can_run_now: false, waiting_for: 'YOU', waiting_detail: { kind: 'NEEDS_INPUT', questions: 2 } })] };
    expect((await open()).querySelector('.wk-row [data-act="wk-continue"]')).toBeNull();
  });

  it('opens a task’s history from its name, newest first, with the browser-use option for an application', async () => {
    routes.history = { task: row({ kind: 'application', status: 'FAILED', job_id: 7 }), browser_use_ready: true,
      trace_ids: ['abcdef0123456789', '0123456789abcdef'], log_count: 3, events: [
      { at: '2026-10-25T22:38:15', title: 'Attempt 3 failed', detail: 'reached filling · OTHER', tone: 'bad', source: 'attempt' },
      { at: '2026-10-25T22:36:45', title: 'Opened job-boards.greenhouse.io', detail: 'HTTP 200 · 754 ms', tone: 'muted', source: 'browser' }] };
    await open();
    const name = view().querySelector('.wk-row [data-act="wk-history"]');
    expect(name).not.toBeNull();
    await ui.A['wk-history']({ dataset: { kind: name.dataset.kind, id: name.dataset.id } });
    ui.render();
    expect(routes.historyUrl).toBe(`/api/workers/${name.dataset.kind}/tasks/${name.dataset.id}/history`);
    const panel = view().querySelector('.wk-history');
    expect(panel.textContent).toContain('Attempt 3 failed');
    expect(panel.textContent.indexOf('Attempt 3 failed')).toBeLessThan(panel.textContent.indexOf('Opened job-boards'));
    // Each run's Monitoring → Logs lines, newest first, one click away.
    const logs = [...panel.querySelectorAll('[data-act="wk-logs"]')];
    expect(panel.textContent).toContain('3 log lines in Monitoring → Logs');
    expect(logs.map(b => b.dataset.id)).toEqual(['abcdef0123456789', '0123456789abcdef']);
    expect(logs[0].textContent).toContain('Latest run');
    ui.A['wk-logs'](logs[0]);
    expect([ui.S.screen, ui.S.mon.tab, ui.S.mon.logs.trace_id]).toEqual(['monitoring', 'logs', 'abcdef0123456789']);
    ui.S.screen = 'workers';
    await ui.A['wk-history-close']();
    ui.render();
    expect(view().querySelector('.wk-history')).toBeNull();
  });

  it('nudges finished jobs on to the next stage, and only offers it when some are waiting', async () => {
    routes.summary.workers.find(w => w.kind === 'parsing').next_stage = { to: 'Ranking', waiting: 2 };
    routes.summary.workers.find(w => w.kind === 'recovery').next_stage = { to: 'Parsing', waiting: 0 };
    let v = await open();
    const button = v.querySelector('[data-act="wk-nudge"]');
    expect(button.textContent.trim()).toBe('Nudge to Ranking · 2');
    expect(button.disabled).toBe(false);
    await ui.A['wk-nudge']({ dataset: { kind: 'parsing' } });
    expect(routes.nudged).toBe('/api/workers/parsing/nudge');
    expect(document.getElementById('toast').textContent).toBe('Moved 2 jobs to Ranking · 1 skipped: Dismissed in triage');

    v = await open('recovery');
    expect(v.querySelector('[data-act="wk-nudge"]').disabled).toBe(true);  // nothing between recovery and parsing
    v = await open('insights');
    expect(v.querySelector('[data-act="wk-nudge"]')).toBeNull();  // the last stage has no next one
  });

  it('nudges one finished task, or every finished task shown, when its job is waiting for the next stage', async () => {
    routes.summary.workers.find(w => w.kind === 'parsing').next_stage = { to: 'Ranking', waiting: 2, job_ids: [4, 5] };
    routes.parsing = { ...parsing(), rows: [row({ task_id: 21, job_id: 4, status: 'COMPLETED', can_kill: false }),
      row({ task_id: 22, job_id: 5, status: 'COMPLETED', can_kill: false }), row({ task_id: 23, job_id: 6, status: 'COMPLETED', can_kill: false }),
      row({ task_id: 24, job_id: 7, status: 'FAILED', can_kill: false })] };
    const v = await open();
    const rows = [...v.querySelectorAll('.wk-row')];
    expect(rows[0].querySelector('[data-act="wk-nudge"]').textContent.trim()).toBe('→ Ranking');
    expect(rows[2].querySelector('[data-act="wk-nudge"]')).toBeNull();  // job 6 is already past it
    expect(rows[2].textContent).toContain('moved on');
    expect(rows[3].querySelector('[data-act="wk-nudge"]')).toBeNull();  // a failed task is retried, not nudged
    expect(v.textContent).toContain('Nudge 2 shown to Ranking');
    await ui.A['wk-nudge']({ dataset: { kind: 'parsing', job: '4' } });
    expect(routes.nudgeBody).toEqual({ job_ids: [4] });
    await ui.A['wk-nudge']({ dataset: { kind: 'parsing', jobs: '4,5' } });
    expect(routes.nudgeBody).toEqual({ job_ids: [4, 5] });
    await ui.A['wk-nudge']({ dataset: { kind: 'parsing' } });
    expect(routes.nudgeBody).toBeNull();
  });

  it('ranks everything that is queued now, in bulk, after asking', async () => {
    let v = await open('ranking');
    const button = v.querySelector('[data-act="wk-run-queued"]');
    expect(button.textContent.trim()).toBe('Rank 1 queued now');
    confirmMock.mockClear();
    await ui.A['wk-run-queued']({ dataset: { kind: 'ranking', n: '1' } });
    expect(confirmMock).not.toHaveBeenCalled();  // one task needs no question
    expect(routes.runQueued).toBe('/api/workers/ranking/run-queued');
    await ui.A['wk-run-queued']({ dataset: { kind: 'ranking', n: '3' } });
    expect(confirmMock).toHaveBeenCalledTimes(1);
    expect(document.getElementById('toast').textContent).toBe('3 queued tasks released to run now');
    v = await open('tailoring');
    expect(v.querySelector('[data-act="wk-run-queued"]')).toBeNull();  // tailoring has no wait to skip
  });

  it('shows the rate-limit backoff and resets it', async () => {
    routes.summary = summary({ cooldowns: [{ provider: 'OPENROUTER', seconds_left: 31, calls_skipped: 4 }] });
    await open();
    expect(view().textContent).toContain('openrouter 31s left');
    await ui.A['wk-reset-cooldowns']();
    expect(posts()).toContain('/api/workers/cooldowns/reset');
    expect(document.getElementById('toast').textContent).toContain('Backoff cleared for openrouter');
  });

  it('keeps the ranking tracker in the Ranking tab, with Run now on waiting jobs', async () => {
    const v = await open('ranking');
    expect(v.textContent).toContain('Queue unranked jobs');
    expect(v.textContent).toContain('AI Engineer');
    const rowEl = [...v.querySelectorAll('.lrow')].find(r => r.textContent.includes('AI Engineer'));
    expect(rowEl.querySelector('[data-act="wk-run-now"]').dataset.id).toBe('31');
    expect(rowEl.querySelector('[data-act="wk-kill"]')).toBeNull();  // nothing to stop yet
    expect(rowEl.querySelector('.wk-last').textContent).toBe('Not run yet');
  });

  it('keeps the tailoring task list in the Tailoring tab, with Kill on a running task', async () => {
    const v = await open('tailoring');
    expect(v.textContent).toContain('Backend Lead');
    expect(v.querySelector('[data-act="wk-kill"][data-kind="tailoring"]').dataset.id).toBe('41');
    const last = v.querySelector('.lrow .wk-last');
    expect(last.querySelector('b').textContent).toBe(ui.fmtDateTime('2026-10-04T10:00:00'));
    expect(last.querySelector('small').textContent).toMatch(/ago$/);
  });

  it('shows when each task last ran, preferring a ranking’s last attempt, and says when one never has', async () => {
    const recent = new Date(Date.now() - 5 * 60000).toISOString().slice(0, 19);
    const base = parsing();
    base.rows = [row({ started_at: recent }), row({ task_id: 12, status: 'QUEUED', started_at: null, can_kill: false, can_run_now: true, attempt: 0 })];
    routes.parsing = base;
    const v = await open();
    expect(v.querySelector('.wk-head').textContent).toContain('Last run');
    const [ran, never] = [...v.querySelectorAll('.wk-row')];
    expect(ran.querySelector('.wk-last small').textContent).toBe('5m ago');
    expect(ran.querySelector('.wk-last').title).toContain('Last run');
    expect(never.querySelector('.wk-last').textContent).toBe('Not run yet');
    expect(ui.wkLastRun({ started_at: null, last_attempt_at: recent }).includes('5m ago')).toBe(true);
  });

  it('sends old links to the ranking tracker here', () => {
    location.hash = '#discovery/ranking';
    ui.readHash();
    expect(ui.S.screen).toBe('workers');
    expect(ui.wkState().tab).toBe('ranking');
  });

  it('shows an empty queue, and an error when the workers cannot be read', async () => {
    routes.parsing = { ...parsing(), rows: [] };
    await open();
    expect(view().textContent).toContain('Nothing in this queue');
    routes.fail = true;
    await expect(ui.loadWorkers()).rejects.toThrow('Workers are unavailable');
  });

  it('polls fast while work runs, slowly when idle, and stops on another screen or in a hidden tab', async () => {
    await open();
    expect(ui.wkPollDelay()).toBe(3000);
    ui.D.wk = { ...ui.D.wk, active: 0 };
    expect(ui.wkPollDelay()).toBe(20000);
    ui.scheduleWkPoll();
    expect(runInContext('wkTimer', context)).not.toBeNull();
    ui.S.screen = 'overview';
    expect(ui.wkPollDelay()).toBeNull();
    await ui.wkPoll();
    expect(runInContext('wkTimer', context)).toBeNull();
    ui.S.screen = 'workers';
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    expect(ui.wkPollDelay()).toBeNull();
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
  });

  it('filters each tab by status and name, asking the server for parsing and recovery', async () => {
    routes.parsing = { ...parsing(), status_counts: { all: 3, active: 2, running: 1, queued: 0, retrying: 1, failed: 1, done: 0 } };
    const v = await open();
    const options = [...v.querySelectorAll('#wk-status-parsing option')].map(o => o.textContent);
    expect(options).toEqual(['All statuses (3)', 'Active (running, queued, retrying) (2)', 'Running (1)', 'Queued (0)', 'Retrying (1)', 'Failed, blocked or stopped (1)', 'Done (0)']);
    const status = v.querySelector('#wk-status-parsing');
    status.value = 'failed';
    status.dispatchEvent(new Event('change', { bubbles: true }));
    await flush();
    expect(fetchMock.mock.calls.some(([u]) => u.startsWith('/api/workers/parsing?') && u.includes('status=failed'))).toBe(true);
    const search = view().querySelector('#wk-q-parsing');
    search.value = 'canva';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    await new Promise(r => setTimeout(r, 300)); await flush();
    expect(fetchMock.mock.calls.some(([u]) => u.includes('q=canva') && u.includes('status=failed'))).toBe(true);
    expect(view().querySelector('[data-act="wk-clear-filters"]')).not.toBeNull();
    routes.parsing = { ...parsing(), rows: [] };
    await ui.loadWorkers(); ui.render();
    expect(view().textContent).toContain('No tasks match these filters');
    ui.A['wk-clear-filters']({ dataset: { tab: 'parsing' } });
    await flush();
    expect(ui.wkState().f.parsing).toEqual({ q: '', status: 'all' });
  });

  it('filters the ranking tracker by status and keeps its server search and location', async () => {
    const v = await open('ranking');
    expect(v.querySelector('#rf-q')).not.toBeNull();
    expect(v.querySelector('#rf-location')).not.toBeNull();
    expect([...v.querySelectorAll('#wk-status-ranking option')].map(o => o.textContent)).toContain('Queued (1)');
    ui.wkState().f.ranking = { q: '', status: 'running' };
    ui.render();
    expect(view().textContent).not.toContain('AI Engineer');
    expect(view().textContent).toContain('0 in progress');
    ui.wkState().f.ranking.status = 'queued';
    ui.render();
    expect(view().textContent).toContain('AI Engineer');
  });

  it('filters the tailoring list in place by name and status, keeping the totals', async () => {
    await open('tailoring');
    ui.wkState().f.tailoring = { q: 'stripe', status: 'running' };
    ui.render();
    expect(view().textContent).toContain('Backend Lead');
    ui.wkState().f.tailoring = { q: 'canva', status: 'all' };
    ui.render();
    expect(view().textContent).toContain('No tailoring tasks match these filters');
    expect(view().textContent).toContain('1 running');  // the header still counts every task
  });

  it('escapes job titles and errors', async () => {
    routes.parsing = { ...parsing(), rows: [row({ title: '<img src=x onerror=alert(1)>', last_error: '<b>x</b>', status: 'FAILED' })] };
    const v = await open();
    expect(v.querySelector('.wk-row img')).toBeNull();
    expect(v.querySelector('.wk-row').textContent).toContain('<img src=x onerror=alert(1)>');
  });

  it('shows the queue at a glance and the last 24 hours hour by hour', async () => {
    const v = await open();
    const kpis = [...v.querySelectorAll('.wk-kpi')].map(k => k.textContent.replace(/\s+/g, ' ').trim());
    expect(kpis[0]).toContain('Running 1');
    expect(kpis[3]).toContain('Finished · 24h 8');
    expect(kpis[3]).toContain('6 done · 2 failed');
    expect(v.querySelector('.wk-stack').getAttribute('aria-label')).toBe('1 running, 1 queued, 1 retrying');
    expect(v.querySelectorAll('.wk-col')).toHaveLength(24);
    expect(v.querySelectorAll('.wk-col')[23].dataset.tip).toContain('2 failed · 4 queued');
    const facts = v.querySelector('.wk-facts').textContent;
    for (const bit of ['75%', '1m 35s', '30s']) expect(facts).toContain(bit);
    expect(fetchMock.mock.calls.map(([u]) => u)).toContain('/api/workers/parsing/activity');
  });

  it('changes the metrics range without touching the task filters', async () => {
    await open();
    ui.A['wk-range']({ dataset: { id: '30d' } });
    await flush();
    expect(fetchMock.mock.calls.map(([u]) => u).some(u => u.startsWith('/api/workers/parsing?') && u.includes('range=30d'))).toBe(true);
    expect(view().querySelector('[data-act="wk-range"][data-id="30d"]').classList.contains('on')).toBe(true);
  });

  it('edits every worker setting in its Settings tab: limits, per-worker settings and the server (admin)', async () => {
    const v = await open('settings');
    expect(v.querySelector('#workers-ranking_concurrency').value).toBe('3');
    expect(v.querySelector('#workers-a__parsing__max_attempts').value).toBe('3');
    expect(v.querySelector('#workers-a__recovery__use_browser').checked).toBe(true);
    expect(v.querySelector('#workers-s__jd_parse_batch_size').value).toBe('10');
    expect(v.querySelector('#workers-a__ranking__auto').checked).toBe(false);
    expect(v.querySelector('#wk-set-ranking').textContent).toContain('Paused');
    expect([...v.querySelectorAll('.wk-ladder span')].map(x => x.textContent)).toEqual(['130s', '21m', '32m', '44m', '5+8m']);
    const set = (id, value) => { const el = v.querySelector(id); if (el.type === 'checkbox') { el.checked = value; el.dispatchEvent(new Event('change', { bubbles: true })); } else { el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })); } };
    set('#workers-ranking_concurrency', '4');
    set('#workers-a__parsing__max_attempts', '5');
    set('#workers-s__jd_parse_batch_size', '25');
    set('#workers-a__recovery__use_model', false);
    expect(document.querySelector('[data-settings-save="workers"]').disabled).toBe(false);
    // A changed value says it is custom and offers its default back.
    ui.render();
    const row = view().querySelector('#workers-a__parsing__max_attempts').closest('.wk-srow');
    expect(row.classList.contains('custom')).toBe(true);
    expect(row.querySelector('[data-act="wk-set-reset"]').textContent).toBe('reset to 3');
    expect(view().querySelector('#workers-a__parsing__retry_seconds').closest('.wk-srow').textContent).toContain('Default · 2m');
    await ui.A['settings-save']({ dataset: { form: 'workers' } });
    await flush();
    expect(routes.patched).toEqual({ ranking_concurrency: 4 });
    expect(routes.accountPut).toEqual({ workers: { parsing: { max_attempts: 5 }, recovery: { use_model: false } } });
    expect(routes.serverPut).toEqual({ values: { jd_parse_batch_size: 25 } });
    expect(document.getElementById('toast').textContent).toContain('Changes saved');
    expect(ui.wkPollDelay()).toBeNull();  // no polling under a form
    window.history.replaceState(null, '', '#workers/settings');
    ui.readHash();
    expect(ui.wkState().tab).toBe('settings');
    // Approvals, who tailors, the resume length and automatic tailoring moved here from Settings → Workflow & cadence.
    expect(view().querySelector('#wk-set-approvals [data-field="application_mode"][value="automatic"]')).not.toBeNull();
    expect(view().querySelector('#workers-min_review_score')).not.toBeNull();
    expect(view().querySelector('#wk-set-tailoring [data-field="execution_mode"][value="claude_desktop"]')).not.toBeNull();
    expect(view().querySelector('#wk-set-tailoring [data-field="tailor_pages"][value="2"]')).not.toBeNull();
    expect(view().querySelector('#wk-set-tailoring [data-field="auto_tailor"]')).not.toBeNull();
    expect(view().querySelector('#wk-notify')).not.toBeNull();
    expect(view().textContent).not.toContain('Workflow & cadence');
    // The old Settings section is gone: its link lands on this tab.
    window.history.replaceState(null, '', '#settings/workflow');
    ui.readHash();
    expect(ui.S.screen).toBe('workers'); expect(ui.wkState().tab).toBe('settings');
  });

  it('shows the server settings read-only to an account that is not an admin', async () => {
    routes.workerSettings = workerSettings(false);
    const v = await open('settings');
    expect(v.querySelector('#workers-s__jd_parse_batch_size')).toBeNull();
    const server = v.querySelector('#wk-set-parsing .wk-scol.server');
    expect(server.textContent).toContain('Tasks per run');
    expect(server.querySelector('.wk-ro-val').textContent).toContain('10');
    expect(v.textContent).toContain('set by your admin');
  });

  it('never redraws the status dropdown while it is open, and catches up when it closes', async () => {
    const v = await open();
    const select = v.querySelector('#wk-status-parsing');
    select.focus();
    expect(document.activeElement).toBe(select);
    routes.summary = summary({ active: 9 });
    await ui.wkPoll();
    expect(v.querySelector('#wk-status-parsing')).toBe(select);  // the same element: its open list stays open
    select.value = 'failed';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    await flush();
    expect(fetchMock.mock.calls.map(([u]) => u).some(u => u.startsWith('/api/workers/parsing?') && u.includes('status=failed'))).toBe(true);
    expect(view().querySelector('#wk-status-parsing').value).toBe('failed');
    view().querySelector('#wk-status-parsing').focus();
    await ui.wkPoll();
    const held = view().querySelector('#wk-status-parsing');
    held.blur();
    await flush();
    expect(view().querySelector('#wk-status-parsing')).not.toBe(held);  // the held refresh ran after the dropdown closed
  });

  it('shows a failed task that was already run again without a second Run now', async () => {
    routes.parsing = { ...parsing(), rows: [row({ task_id: 13, status: 'FAILED', can_kill: false, can_run_now: false, last_error: '502', finished_at: '2026-10-04T16:04:00', retried_by: { task_id: 20, status: 'QUEUED' } }),
      row({ task_id: 14, job_id: 8, status: 'FAILED', can_kill: false, can_run_now: true, last_error: 'boom' })] };
    const v = await open();
    const [retried, fresh] = v.querySelectorAll('.wk-row');
    expect(retried.querySelector('[data-act="wk-run-now"]')).toBeNull();
    expect(retried.textContent).toContain('↻ Run again');
    expect(retried.textContent).toContain('run again as task #20 (queued)');
    expect(fresh.querySelector('[data-act="wk-run-now"]')).not.toBeNull();
  });

  it('selects failed tasks and retries them in bulk', async () => {
    const failed = (id, extra = {}) => row({ task_id: id, job_id: id, title: `Job ${id}`, status: 'FAILED', can_kill: false, can_run_now: true, last_error: 'ollama.com:443', ...extra });
    routes.parsing = { ...parsing(), rows: [row(), failed(21), failed(22), failed(23), failed(24, { retried_by: { task_id: 40, status: 'QUEUED' }, can_run_now: false })] };
    const v = await open();
    const boxes = () => [...view().querySelectorAll('[data-wk-select="parsing"]')];
    expect(boxes().map(b => b.dataset.id)).toEqual(['21', '22', '23']);  // never on a running or already-retried row
    expect(view().querySelector('.wk-bulk').textContent).toContain('Select all 3 failed tasks shown');
    expect(view().querySelector('[data-act="wk-retry-selected"]').disabled).toBe(true);
    const tick = el => { el.checked = !el.checked; el.dispatchEvent(new Event('change', { bubbles: true })); };
    tick(boxes()[1]);
    expect(view().querySelector('.wk-bulk').textContent).toContain('1 selected of 3 failed shown');
    expect(view().querySelector('.wk-bulk .wk-check').classList.contains('partial')).toBe(true);
    tick(view().querySelector('[data-wk-select-all="parsing"]'));
    expect(boxes().every(b => b.checked)).toBe(true);
    expect(view().querySelector('[data-act="wk-retry-selected"]').textContent).toContain('Retry 3');
    // A poll that no longer lists one of them drops it from the selection.
    routes.parsing = { ...routes.parsing, rows: routes.parsing.rows.filter(r => r.task_id !== 23) };
    await ui.loadWorkers(); ui.render();
    expect(view().querySelector('[data-act="wk-retry-selected"]').textContent).toContain('Retry 2');
    await ui.A['wk-retry-selected']({ dataset: { tab: 'parsing' } });
    expect(confirmMock.mock.calls.at(-1)[0]).toContain('Retry 2 failed tasks?');
    expect(routes.retryPost).toEqual({ url: '/api/workers/parsing/retry', body: { task_ids: [21, 22] } });
    expect(document.getElementById('toast').textContent).toContain('Queued 1 task again · 1 skipped: already run again');
    expect(ui.wkState().sel.parsing.size).toBe(0);
  });

  it('offers the same selection on the tailoring and ranking tabs', async () => {
    fetchMock.mockImplementation(async (url, opt = {}) => response(
      url === '/api/workers' ? routes.summary : url.endsWith('/activity') ? routes.activity
      : url.startsWith('/api/tailor-tasks') ? [{ id: 41, job_id: 5, title: 'Backend Lead', company: 'Stripe', mode: 'tailor', status: 'failed', stage: 'done', progress: 0, error: 'x' },
        { id: 42, job_id: 6, title: 'Staff', company: 'Grab', mode: 'tailor', status: 'failed', stage: 'done', progress: 0, error: 'x' }, { id: 43, job_id: 6, title: 'Staff', company: 'Grab', mode: 'tailor', status: 'queued', stage: 'queued', progress: 0 }]
      : url.startsWith('/api/ranking/tracker') ? { counts: { filtering: 0, queued: 0, processing: 0, retry_scheduled: 0, waiting: 0, completed: 0, failed: 1, skipped: 0, qualified: 0, archived: 0 },
        progress: { finished: 1, total: 1 }, concurrency: 2, interval_minutes: 5, active: [], recent: [{ id: 51, job_id: 9, title: 'AI Engineer', company: 'Grab', status: 'FAILED', attempt_count: 3, max_attempts: 3, reason: 'new' }],
        locations: [], worker: { enabled: true, interval_minutes: 2 }, auto_rank: true } : {}));
    let v = await open('tailoring');
    expect([...v.querySelectorAll('[data-wk-select="tailoring"]')].map(b => b.dataset.id)).toEqual(['41']);  // 42 was retried as 43
    v = await open('ranking');
    expect([...v.querySelectorAll('[data-wk-select="ranking"]')].map(b => b.dataset.id)).toEqual(['51']);
    expect(v.querySelector('.wk-bulk')).not.toBeNull();
  });

  it('explains a waiting or retrying task in full behind an ⓘ', async () => {
    const long = 'LLMRateLimitError: LiteLLM request failed for model=Auto-Router: litellm.RateLimitError: Ollama_chatException - timed out waiting for a concurrent request';
    routes.parsing = { ...parsing(), rows: [row({ task_id: 31, status: 'RETRYING', attempt: 2, max_attempts: 3, last_error: long, error_category: 'RATE_LIMITED', next_attempt_at: '2099-01-01T00:00:00', reason: 'description_changed', can_run_now: true }),
      row({ task_id: 32, status: 'QUEUED', attempt: 0, last_error: null, can_kill: false, can_run_now: true })] };
    const v = await open();
    const [retrying, queued] = v.querySelectorAll('.wk-row');
    const tip = retrying.querySelector('.wk-info').dataset.tip;
    expect(tip.split('\n')).toEqual(['Why it is retrying', long, 'Kind: Rate limited', 'Attempt 2 of 3 used',
      expect.stringMatching(/^Next try /), 'Queued because: Description changed']);
    expect(retrying.querySelector('.wk-info').getAttribute('aria-label')).toBe(tip);
    expect(queued.querySelector('.wk-info')).toBeNull();  // nothing to explain
    // The ranking tracker's "Waiting to retry" line gets the same ⓘ.
    expect(context.wkRankWhy({ status: 'PENDING', attempt_count: 0, last_error: 'The job description is still being parsed', next_attempt_at: '2099-01-01T00:00:00', reason: 'resume_changed', max_attempts: 3 }))
      .toContain('Why it is waiting\nThe job description is still being parsed');
  });

  it('clears finished tasks from a tab without deleting them, and shows them again', async () => {
    routes.parsing = { ...parsing(), rows: [row(), row({ task_id: 13, status: 'FAILED', can_kill: false }), row({ task_id: 14, status: 'COMPLETED', can_kill: false })] };
    let v = await open();
    const clear = v.querySelector('[data-act="wk-clear"]');
    expect(clear.textContent).toContain('Clear finished (2)');
    await ui.A['wk-clear'](clear);
    expect(confirmMock.mock.calls.at(-1)[0]).toContain('Nothing is deleted');
    expect(routes.clears).toEqual(['POST /api/workers/parsing/clear']);
    routes.summary = summary({ workers: summary().workers.map(w => w.kind === 'parsing' ? { ...w, cleared_at: '2026-10-05T10:00:00' } : w) });
    routes.parsing = { ...parsing(), rows: [row()], cleared_at: '2026-10-05T10:00:00', cleared_hidden: 2 };
    v = await open();
    expect(v.querySelector('[data-act="wk-clear"]')).toBeNull();
    expect(v.querySelector('[data-act="wk-unclear"]').textContent).toContain('Show 2 cleared');
    await ui.A['wk-unclear'](v.querySelector('[data-act="wk-unclear"]'));
    expect(routes.clears.at(-1)).toBe('DELETE /api/workers/parsing/clear');
  });

  it('hides tailoring tasks finished before the tab was cleared', async () => {
    routes.summary = summary({ workers: summary().workers.map(w => w.kind === 'tailoring' ? { ...w, cleared_at: '2026-10-05T10:00:00' } : w) });
    fetchMock.mockImplementation(async url => response(url === '/api/workers' ? routes.summary : url.endsWith('/activity') ? routes.activity
      : url.startsWith('/api/tailor-tasks') ? [{ id: 41, job_id: 5, title: 'Old', company: 'A', mode: 'tailor', status: 'failed', stage: 'done', progress: 0, finished_at: '2026-10-05T09:00:00' },
        { id: 42, job_id: 6, title: 'New', company: 'B', mode: 'tailor', status: 'failed', stage: 'done', progress: 0, finished_at: '2026-10-05T11:00:00' },
        { id: 43, job_id: 7, title: 'Waiting', company: 'C', mode: 'tailor', status: 'queued', stage: 'queued', progress: 0 }] : {}));
    const v = await open('tailoring');
    expect(v.textContent).not.toContain('Old'); expect(v.textContent).toContain('New'); expect(v.textContent).toContain('Waiting');
    expect(v.querySelector('[data-act="wk-clear"]').textContent).toContain('Clear finished (1)');
    expect(v.querySelector('[data-act="tasks-clear"]')).toBeNull();  // the old button deleted rows
  });

  it('asks before leaving unsaved worker settings, but not between its own tabs', async () => {
    window.scrollTo = () => {};
    const v = await open('settings');
    const el = v.querySelector('#workers-a__parsing__max_attempts');
    el.value = '5'; el.dispatchEvent(new Event('input', { bubbles: true }));
    confirmMock.mockReturnValueOnce(false);
    ui.go('overview');
    expect(ui.S.screen).toBe('workers');  // stayed: the draft is kept
    expect(ui.S.forms.workers.a__parsing__max_attempts).toBe('5');
    ui.A['wk-tab']({ dataset: { id: 'parsing' } });
    expect(ui.S.screen).toBe('workers'); expect(ui.S.forms.workers).toBeDefined();  // own tabs: no question
    confirmMock.mockReturnValueOnce(true);
    ui.go('overview');
    expect(ui.S.screen).toBe('overview'); expect(ui.S.forms.workers).toBeUndefined();
  });
});

