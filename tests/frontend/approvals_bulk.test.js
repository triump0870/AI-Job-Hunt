import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'render', 'setF'];
let ui, context, fetchMock, bulkReply, taskReply;

const REASONS = [{ id: 'compensation', label: 'Pay too low' }, { id: 'location', label: 'Location or remote policy' }];
const job = (id, over = {}) => ({ id, title: `Engineer ${id}`, company: 'Logitech', location: 'Remote', url: '', source: 'manual',
  board: 'Manual', fit: 80, status: 'tailored', discovered_at: '2026-09-18T09:00:00', posted: '3d', matches: [], gaps: [],
  missing: [], keywords: [], summary: '', strategy: '', snippet: '', agency: false, search_run_id: null, application_id: null,
  application_status: null, stage: null, rejection: null, ...over });
const row = (id, over = {}) => ({ ...job(id, over), reviewer_score: 80, age: 'tailored 3d ago', waiting: true, ...over });
const approval = () => ({ job: job(1), min_review_score: 70, checklist: [], answers: [], notes: '', application_id: null,
  resume: { markdown: '# Rohan', reviewer_score: 80, generated: '3d ago', edits: [] }, rejection_reasons: REASONS,
  profile: { name: 'Rohan', headline: '', location: '' } });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c"}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('scrollTo', vi.fn());
  bulkReply = null; taskReply = null;
  const answer = (url, options) => url.endsWith('/api/approvals/bulk') ? bulkReply
    : url.endsWith('/api/tailor-tasks') ? (options?.method === 'POST' ? taskReply : ui.D.tasks || [])
    : url.includes('/workspace/summary') ? ui.D.summary
    : url.includes('/api/approvals/') ? approval()
    : url.includes('/api/approvals') ? { ...ui.D.approvalFacets, items: ui.D.approvals }
    : {};
  fetchMock = vi.fn(async (url, options) => ({ ok: true, status: 200, text: async () => JSON.stringify(answer(url, options)) }));
  vi.stubGlobal('fetch', fetchMock);
  if (!ui) {
    context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock;
  ui.S.forms = {}; ui.S.busy = {}; ui.S.screen = 'approvals'; ui.S.approvalJob = 1; ui.S.rejecting = null;
  ui.S.overriding = null; ui.S.approvalPicked = new Set(); ui.S.bulkRejecting = false;
  ui.S.pf = { tailor: { q: '', min_fit: 0, min_review: 0, board: 'all', status: 'all', sort: 'default' },
              approvals: { q: '', min_fit: 0, min_review: 0, board: 'all', status: 'all', sort: 'default' } };
  Object.assign(ui.D, {
    tasks: [],
    approvals: [row(1), row(2, { reviewer_score: 50 }), row(3, { status: 'pending_approval', waiting: false })],
    approvalFacets: { total: 3, total_all: 3, boards: ['Manual'] }, approval: approval(),
    summary: { profile: {}, counts: {}, application_mode: 'manual_approval', model: 'm', total_jobs: 3,
      auto_search: { schedule: 'manual', setups: 0, roles: 0, locations: 0, next_run: null, last_run: null } } });
  ui.render();
});

const view = () => document.getElementById('view');
const button = act => [...view().querySelectorAll(`[data-act="${act}"]`)];

describe('acting on several approvals at once', () => {
  it('shows no bulk actions until a job is picked', () => {
    expect(button('approval-check')).toHaveLength(3);
    expect(button('bulk-approval')).toHaveLength(0);
    ui.A['approval-check']({ dataset: { id: '1' } }, { stopPropagation() {} });
    expect(view().textContent).toContain('1 selected');
  });

  it('counts only the picked jobs each action applies to', () => {
    ui.A['approval-check-all']();
    expect(view().textContent).toContain('3 selected');
    const [revise, approve] = button('bulk-approval');
    expect(revise.textContent).toBe('Send back 2');  // job 3 is already sent back
    expect(approve.textContent).toBe('Approve 2');        // job 2 is below the minimum of 70
    ui.A['approval-check-all']();
    expect(ui.S.approvalPicked.size).toBe(0);
  });

  it('rejects the picked jobs with one reason and keeps the skipped ones picked', async () => {
    ui.A['approval-check']({ dataset: { id: '1' } }, { stopPropagation() {} });
    ui.A['approval-check']({ dataset: { id: '2' } }, { stopPropagation() {} });
    ui.A['bulk-reject-open']();
    expect(view().textContent).toContain('Reject 2 jobs?');
    ui.A['bulk-reject-reason']({ dataset: { id: 'location' } });
    bulkReply = { ok: true, action: 'reject', done: [1], skipped: [{ id: 2, title: 'Engineer 2', reason: 'Not in the approval queue' }] };
    await ui.A['bulk-approval']({ dataset: { action: 'reject' } });
    const [, options] = fetchMock.mock.calls.find(([u]) => u.endsWith('/api/approvals/bulk'));
    expect(JSON.parse(options.body)).toEqual({ action: 'reject', job_ids: [1, 2], reason: 'location', notes: '' });
    expect([...ui.S.approvalPicked]).toEqual([2]);
    expect(ui.S.bulkRejecting).toBe(false);
    expect(document.getElementById('toast').textContent).toContain('Rejected 1 job · 1 skipped');
  });

  it('sends back for revision without a reason', async () => {
    ui.A['approval-check']({ dataset: { id: '1' } }, { stopPropagation() {} });
    bulkReply = { ok: true, action: 'revise', done: [1], skipped: [] };
    await ui.A['bulk-approval']({ dataset: { action: 'revise' } });
    const [, options] = fetchMock.mock.calls.find(([u]) => u.endsWith('/api/approvals/bulk'));
    expect(JSON.parse(options.body)).toEqual({ action: 'revise', job_ids: [1] });
    expect(ui.S.approvalPicked.size).toBe(0);
  });

  it('opens each picked posting in its own tab, skipping jobs without a web link', () => {
    ui.D.approvals = [row(1, { url: 'https://jobs.example.test/1' }), row(2, { url: '' }),
      row(3, { url: 'javascript:alert(1)' }), row(4, { url: 'http://jobs.example.test/4' })];
    ui.render();
    expect(button('bulk-open-postings')).toHaveLength(0);
    ui.A['approval-check-all']();
    const [open] = button('bulk-open-postings');
    expect(open.textContent).toBe('Open 2 postings ↗');
    const tabs = [];
    vi.stubGlobal('open', vi.fn(url => { const tab = { url, opener: window }; tabs.push(tab); return tab; }));
    ui.A['bulk-open-postings']();
    expect(tabs.map(t => t.url)).toEqual(['https://jobs.example.test/1', 'http://jobs.example.test/4']);
    expect(tabs.every(t => t.opener === null)).toBe(true);
    expect(document.getElementById('toast').textContent).toContain('Opened 2 postings');
  });

  it('says so when the browser blocks the extra tabs', () => {
    ui.D.approvals = [row(1, { url: 'https://a.example.test' }), row(2, { url: 'https://b.example.test' })];
    ui.A['approval-check-all']();
    vi.stubGlobal('open', vi.fn().mockReturnValueOnce({}).mockReturnValue(null));
    ui.A['bulk-open-postings']();
    expect(window.open).toHaveBeenCalledTimes(2);
    expect(document.getElementById('toast').textContent).toContain('Opened 1 of 2 postings');
  });

  it('stars a job from the queue without selecting it, and puts the star back if saving fails', async () => {
    const [star] = button('approval-star');
    expect(star.textContent).toBe('☆');
    await ui.A['approval-star']({ dataset: { id: '2' } }, { stopPropagation() {} });
    const [, options] = fetchMock.mock.calls.find(([u]) => u.endsWith('/api/jobs/2/star'));
    expect(JSON.parse(options.body)).toEqual({ starred: true });
    expect(ui.D.approvals.find(q => q.id === 2).starred).toBe(true);
    expect(ui.S.approvalJob).toBe(1);

    fetchMock.mockImplementationOnce(async () => ({ ok: false, status: 500, text: async () => JSON.stringify({ detail: 'down' }) }));
    await ui.A['approval-star']({ dataset: { id: '1' } }, { stopPropagation() {} });
    expect(ui.D.approvals.find(q => q.id === 1).starred).toBeFalsy();
    expect(ui.D.approval.job.starred).toBeFalsy();
  });

  it('labels an approved job whose posting closed', () => {
    ui.D.approvals = [row(1, { status: 'approved', posting_closed: { at: '2026-10-18T09:00:00', ago: '1d ago', reason: 'JOB_CLOSED' } }), row(2)];
    ui.render();
    const rows = [...view().querySelectorAll('[data-act="select-approval"]')];
    expect(rows[0].textContent).toContain('posting closed');
    expect(rows[1].textContent).not.toContain('posting closed');
  });

  it('counts the packets that can be retailored or reviewed again', () => {
    ui.D.approvals = [row(1), row(2, { fit: null }), row(3, { status: 'pending_approval', waiting: false }),
      row(4, { status: 'approved', waiting: false }), row(5)];
    ui.D.tasks = [{ id: 9, job_id: 5, mode: 'review', status: 'running' }];
    ui.A['approval-check-all']();
    expect(button('bulk-retailor')[0].textContent).toBe('Re-tailor 2');   // 2 is not ranked, 4 is approved, 5 is busy
    expect(button('bulk-rereview')[0].textContent).toBe('Re-review 3');
    const rows = [...view().querySelectorAll('[data-act="select-approval"]')];
    expect(rows[4].textContent).toContain('re-reviewing');
  });

  it('retailors the picked packets from their last review and unpicks the queued ones', async () => {
    ui.A['approval-check']({ dataset: { id: '1' } }, { stopPropagation() {} });
    ui.A['approval-check']({ dataset: { id: '3' } }, { stopPropagation() {} });
    taskReply = { queued: [{ id: 7, job_id: 1, mode: 'tailor' }], skipped: [{ id: 3, reason: 'already queued' }] };
    await ui.A['bulk-retailor']();
    const [, options] = fetchMock.mock.calls.find(([u, o]) => u.endsWith('/api/tailor-tasks') && o?.method === 'POST');
    expect(JSON.parse(options.body)).toEqual({ ids: [1, 3], mode: 'tailor', regenerate: true, revise: true });
    expect([...ui.S.approvalPicked]).toEqual([3]);
    expect(document.getElementById('toast').textContent).toContain('Re-tailoring 1 job in the background · 1 skipped');
  });

  it('reviews the picked packets again without tailoring', async () => {
    ui.A['approval-check']({ dataset: { id: '2' } }, { stopPropagation() {} });
    taskReply = { queued: [{ id: 8, job_id: 2, mode: 'review' }], skipped: [] };
    await ui.A['bulk-rereview']();
    const [, options] = fetchMock.mock.calls.find(([u, o]) => u.endsWith('/api/tailor-tasks') && o?.method === 'POST');
    expect(JSON.parse(options.body)).toEqual({ ids: [2], mode: 'review', regenerate: true });
    expect(ui.S.approvalPicked.size).toBe(0);
    expect(document.getElementById('toast').textContent).toContain('Reviewing 1 job in the background');
  });
});
