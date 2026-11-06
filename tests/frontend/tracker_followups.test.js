import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'render'];
let ui, context, fetchMock;

const application = (over = {}) => ({
  id: 3, job_id: 11, title: 'Lead Software Engineer', company: 'Epsilon', location: 'Bengaluru, Karnataka, India',
  url: 'https://www.linkedin.com/jobs/view/4465457931', fit: 85, stage: 'applied', age: '1d', tag: '', tone: 'muted',
  meta: 'applied 22 Sep · follow-up 27 Sep', recruiter_name: '', recruiter_email: '', notes: '', recruiters: [],
  docs: 'resume v1 · cover letter', resume_id: 7, draft: '', draft_id: null, draft_status: null, followup_pending: true, next: 'Send the follow-up',
  next_date: 'due 27 Sep', timeline: [{ id: 1, kind: 'applied', title: 'Applied', detail: '', at: '2026-09-22T13:49:00' }],
  ...over });

const tracker = (over = {}) => ({
  applications: [application()], stages: [{ id: 'applied', label: 'Applied' }, { id: 'closed', label: 'Closed' }],
  available_stages: [], extra_stages: [],
  stats: { live: 1, replies: 0, due: 1, response_rate: '0%' }, ...over });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c"}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('scrollTo', vi.fn());
  // Opening the packet navigates, which reloads the shell: answer every endpoint it touches.
  fetchMock = vi.fn(async url => ({ ok: true, status: 200, text: async () => JSON.stringify(
    url.includes('/workspace/summary') ? ui.D.summary
      : url.includes('/api/tracker') ? ui.D.tracker
      : url.includes('/api/tailor/queue') ? { items: [], total: 0, total_all: 0, boards: [] }
      : url.includes('/api/tailor/') ? { job: { id: 11, status: 'applied' }, resume: null, min_review_score: 55 }
      : url.includes('/api/knowledge') ? { answers: [] }
      : url.includes('/api/tailor-tasks') ? [] : {}) }));  // the endpoint returns a list
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
  ui.S.forms = {}; ui.S.busy = {}; ui.S.screen = 'tracker'; ui.S.selApp = 3; ui.S.trackerView = 'board';
  ui.S.jobOpen = null; ui.S.tailorJob = null; ui.S.scheduling = false;
  Object.assign(ui.D, { tracker: tracker(), jobModal: null,
    summary: { profile: {}, counts: {}, application_mode: 'manual_approval', model: 'm', total_jobs: 1,
      auto_search: { schedule: 'manual', setups: 0, roles: 0, locations: 0, next_run: null, last_run: null } } });
  ui.render();
});

const view = () => document.getElementById('view');
const buttons = () => [...view().querySelectorAll('[data-act]')].map(b => b.textContent.trim());
const button = act => view().querySelector(`[data-act="${act}"]`);
const posted = () => fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST').map(([url]) => url);
const show = over => { ui.D.tracker = tracker({ applications: [application(over)] }); ui.render(); };

describe('recording a follow-up from the tracker', () => {
  it('offers Mark sent and Skip beside Review & copy for a draft', () => {
    show({ draft: 'Hi Dana, checking in.', draft_id: 41, draft_status: 'draft' });
    const shown = buttons();
    const copy = shown.indexOf('Review & copy');
    expect(copy).toBeGreaterThan(-1);
    expect(shown.slice(copy, copy + 3)).toEqual(['Review & copy', 'Mark sent', 'Skip']);
  });

  it('records the draft as sent only after the person confirms, and never sends it', async () => {
    show({ draft: 'Hi Dana, checking in.', draft_id: 41, draft_status: 'draft' });
    ui.A['followup-sent'](button('followup-sent'));
    await vi.waitFor(() => expect(posted()).toContain('/api/followups/41/sent'));
    expect(confirm).toHaveBeenCalled();
    expect(posted().some(url => url.includes('followup-draft'))).toBe(false);

    fetchMock.mockClear();
    confirm.mockImplementationOnce(() => false);
    ui.A['followup-sent'](button('followup-sent'));
    await new Promise(r => setTimeout(r, 0));
    expect(posted()).toEqual([]);
  });

  it('skips a draft by its id', async () => {
    show({ draft: 'Hi Dana, checking in.', draft_id: 41, draft_status: 'draft' });
    ui.A['followup-skip'](button('followup-skip'));
    await vi.waitFor(() => expect(posted()).toContain('/api/followups/41/skip'));
  });

  it('skips a due date that has no draft yet through the application', async () => {
    show({ draft: '', draft_id: null, draft_status: null, followup_pending: true });
    expect(buttons()).not.toContain('Mark sent');
    expect(buttons()).toContain('Skip');
    ui.A['followup-skip'](button('followup-skip'));
    await vi.waitFor(() => expect(posted()).toContain('/api/applications/3/followup-skip'));
  });

  it('offers neither once the follow-up was sent', () => {
    show({ draft: 'Hi Dana, checking in.', draft_id: 41, draft_status: 'sent', followup_pending: false });
    expect(buttons()).not.toContain('Mark sent');
    expect(buttons()).not.toContain('Skip');
    expect(view().textContent).toContain('this is the follow-up that went out');
  });

  it('offers nothing to record when no follow-up is pending', () => {
    show({ followup_pending: false });
    expect(buttons()).not.toContain('Mark sent');
    expect(buttons()).not.toContain('Skip');
  });
});
