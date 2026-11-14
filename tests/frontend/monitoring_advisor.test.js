import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// AI optimization: every recommendation shows a decision brief; a configuration change is staged with one click and
// shown as a validated diff before Apply; a code change offers a prompt for a coding assistant; applied changes can be
// undone; recommendations can be dismissed.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'render', 'monState'];
let ui, context, fetchMock;

const response = (data, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(data) });
const rec = (extra = {}) => ({ title: 'Remove failing models from RANKER', category: 'RELIABILITY', priority: 'HIGH',
  finding: 'Ranker fails often.', evidence: '758 of 1490 failed.', recommended_change: 'Remove Openrouter.',
  expected_benefit: 'Fewer failures.', estimated_cost_saving: 'unknown', risk: 'Quality may change.', affected_stage: 'RANKER', ...extra });
const action = { id: 'drop-backups.RANKER.abc', kind: 'CONFIG', type: 'drop-backups', worker_type: 'RANKER', worker: 'Ranker',
  title: 'Remove failing backup model from Ranker', why: 'Openrouter failed 97.1% of 102 Ranker requests.',
  changes: [{ field: 'fallback_model', label: 'fallback model', before: 'Openrouter', after: 'gemma4:31b-cloud' }],
  payload: { fallback_model: 'gemma4:31b-cloud' }, group: null, upside: 'Shorter failure paths.', risk: 'Not tried if it recovers.',
  verify: 'Ranker failure rate.', confidence: { level: 'HIGH', sample: 102, text: 'Based on 102 requests in this period' },
  models: [{ model: 'Openrouter', role_before: 'fallback', role_after: '—', requests: 102, failure_rate: 0.971, rate_limited: 40 }] };
const brief = { confidence: { level: 'HIGH', sample: 1490, text: 'Based on 1,490 Ranker requests in this period' },
  doing: '1 configuration change below can be staged with one click.', upside: 'Fewer failures.', risk: 'Quality may change.',
  verify: 'Re-run Analyze after a few days.', undo: 'Undo restores the version before it.', notes: ['Written by m.'] };
const recommendations = (actions = {}) => ({
  report: { id: 42, summary: 'Over 7 days…', recommendations: [rec(), rec({ title: 'Deduplicate repeated calls', category: 'ARCHITECTURE', priority: 'MEDIUM', affected_stage: 'ALL' })],
    generated_by: 'llm', model_name: 'm', prompt_version: 'v4', time_range_start: '2026-11-07T00:00:00', time_range_end: '2026-11-14T00:00:00', created_at: '2026-11-14T10:00:00' },
  tasks: [], active: false, schedule: 'off', notice: 'Nothing changes on its own.',
  actions: { blocked: null, staged: null, decisions: [], items: [
    { index: 0, key: 'k0', actions: [action], brief, coding_prompt: null, dismissed: null, applied: [] },
    { index: 1, key: 'k1', actions: [], brief: { ...brief, doing: 'This needs a code change.' }, coding_prompt: '# Task: Deduplicate repeated calls\nRead AGENTS.md first.', dismissed: null, applied: [] }],
  ...actions } });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c","is_admin":true}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('scrollTo', vi.fn());
  fetchMock = vi.fn(async url => response(url.includes('/workspace/summary') ? ui.D.summary
    : url.endsWith('/stage') ? { decision: { id: 5, status: 'STAGED', title: action.title, base_version: 142 }, action,
      preview: { changes: [{ label: 'Ranker · fallback model', before: 'Openrouter', after: 'gemma4:31b-cloud', kind: 'changed' }],
        validation: { errors: [], warnings: [{ message: 'Reviewer and Tailor use the same model' }] }, can_apply: true } }
    : url.endsWith('/apply') ? { decision: { id: 5, status: 'APPLIED', config_version: 143 } }
    : url.endsWith('/undo') ? { decision: { id: 5, status: 'UNDONE', undo_version: 144 } }
    : url.includes('/recommendations') && !url.includes('/decisions/') && !url.includes('/dismiss') ? ui.D.monRec
    : url.includes('/monitoring/dashboard') ? ui.D.mon : {}));
  vi.stubGlobal('fetch', fetchMock);
  if (!ui) {
    context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console, Blob, URL });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock; context.confirm = confirm;
  ui.S.forms = {}; ui.S.busy = {}; ui.S.screen = 'monitoring'; ui.S.mon = null;
  ui.monState().tab = 'advisor';
  Object.assign(ui.D, { summary: { profile: {}, counts: {}, application_mode: 'manual_approval', model: 'm', total_jobs: 1,
    auto_search: { schedule: 'manual', setups: 0, roles: 0, locations: 0, next_run: null, last_run: null } },
  monRec: recommendations(), monStage: null });
});

const show = () => { ui.render(); return document.getElementById('view'); };
const posts = () => fetchMock.mock.calls.filter(([, opt]) => opt?.method === 'POST').map(([url]) => url);

describe('AI optimization · one-click changes', () => {
  it('shows a decision brief, the exact change and the model numbers behind it', () => {
    const text = show().textContent;
    expect(text).toContain('Decision brief');
    expect(text).toContain('high confidence');
    expect(text).toContain('Based on 1,490 Ranker requests in this period');
    expect(text).toContain('Remove failing backup model from Ranker');
    expect(text).toContain('97.1%');
    expect(text).toContain('gemma4:31b-cloud');
    expect(text).toContain('1 one-click change');
    expect(document.querySelector('[data-act="mon-act-stage"]').disabled).toBe(false);
  });

  it('stages a change, shows the validated diff, and applies it', async () => {
    show();
    await ui.A['mon-act-stage']({ dataset: { id: action.id } });
    expect(posts()).toContain('/api/monitoring/recommendations/42/actions/drop-backups.RANKER.abc/stage');
    ui.D.monRec = recommendations({ staged: { id: 5, status: 'STAGED', title: action.title, base_version: 142, detail: { changes: action.changes } } });
    const text = show().textContent;
    expect(text).toContain('Ready to apply: Remove failing backup model from Ranker');
    expect(text).toContain('configuration v143');
    expect(text).toContain('Reviewer and Tailor use the same model');
    expect(document.querySelector('[data-act="mon-act-stage"]').disabled).toBe(true);  // one staged change at a time
    await ui.A['mon-act-apply']({ dataset: { id: '5' } });
    expect(posts()).toContain('/api/monitoring/recommendations/decisions/5/apply');
    expect(document.getElementById('toast').textContent).toContain('Applied as configuration v143');
  });

  it('blocks staging while AI Models has unapplied edits', () => {
    ui.D.monRec = recommendations({ blocked: 'AI Models has unapplied edits. Apply or discard them first.' });
    const view = show();
    expect(view.textContent).toContain('AI Models has unapplied edits');
    expect(document.querySelector('[data-act="mon-act-stage"]').disabled).toBe(true);
  });

  it('offers a coding-assistant prompt for a code change', async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const text = show().textContent;
    expect(text).toContain('code prompt');
    expect(text).toContain('Code change: prompt for your coding assistant');
    await ui.A['mon-copy-prompt']({ dataset: { idx: '1' } });
    expect(writeText).toHaveBeenCalledWith('# Task: Deduplicate repeated calls\nRead AGENTS.md first.');
  });

  it('undoes an applied change after confirming', async () => {
    ui.D.monRec = recommendations();
    ui.D.monRec.actions.items[0].applied = [{ id: 5, status: 'APPLIED', config_version: 143, base_version: 142, title: action.title }];
    expect(show().textContent).toContain('applied · v143');
    await ui.A['mon-act-undo']({ dataset: { id: '5', version: '142' } });
    expect(confirm).toHaveBeenCalled();
    expect(posts()).toContain('/api/monitoring/recommendations/decisions/5/undo');
  });

  it('dismisses a recommendation and lists it for showing again', async () => {
    show();
    await ui.A['mon-act-dismiss']({ dataset: { key: 'k1' } });
    const [, opt] = fetchMock.mock.calls.find(([url]) => url.endsWith('/42/dismiss'));
    expect(JSON.parse(opt.body)).toEqual({ key: 'k1' });
    ui.D.monRec = recommendations();
    ui.D.monRec.actions.items[1].dismissed = { id: 9, status: 'DISMISSED' };
    const text = show().textContent;
    expect(text).toContain('1 dismissed recommendation');
    expect(document.querySelector('[data-act="mon-act-restore"]').dataset.id).toBe('9');
  });

  it('still shows the recommendations when the one-click layer is missing', () => {
    ui.D.monRec = { ...recommendations(), actions: undefined };
    const text = show().textContent;
    expect(text).toContain('Remove failing models from RANKER');
    expect(document.querySelector('[data-act="mon-act-stage"]')).toBeNull();
    expect(text).toContain('Quality may change.');  // the report's own risk and benefit stay visible
    expect(text).toContain('Fewer failures.');
  });
});
