import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'render', 'rejectionBanner'];
let ui, context, fetchMock;

const REASONS = [{ id: 'compensation', label: 'Pay too low' }, { id: 'location', label: 'Location or remote policy' },
                 { id: 'seniority', label: 'Wrong seniority' }, { id: 'other', label: 'Other' }];
const job = (over = {}) => ({ id: 7, title: 'Lead Software Engineer', company: 'Logitech', location: 'Remote',
  url: 'https://example.test/job', source: 'manual', board: 'Manual', fit: 92, status: 'tailored',
  discovered_at: '2026-09-18T09:00:00', posted: '3d', matches: [], gaps: [], missing: [], keywords: [],
  summary: '', strategy: '', snippet: '', agency: false, search_run_id: null, application_id: null,
  application_status: null, stage: null, rejection: null, ...over });
const approval = (over = {}) => ({ job: job(), min_review_score: 55, checklist: [],
  resume: { markdown: '# Rohan', reviewer_score: 45, generated: '3d ago', edits: [] }, answers: [], notes: '',
  application_id: null, rejection_reasons: REASONS, profile: { name: 'Rohan', headline: '', location: '' }, ...over });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c"}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('scrollTo', vi.fn());
  // Confirming rejects and then reloads the screen, so every endpoint it touches has to answer sensibly.
  const approved = { application_id: 3, status: 'approved', apply_url: '', approval_override: true,
    reviewer_score: 45, min_review_score: 55 };
  const rejected = { ok: true, job: job({ status: 'dismissed' }),
    rejection: { reason: 'compensation', label: 'Pay too low', note: '', at: '2026-09-22T10:00:00', on: '22 Sep' } };
  const answer = url => url.endsWith('/approve') ? approved
    : url.endsWith('/reject') ? rejected
    : url.includes('/workspace/summary') ? ui.D.summary
    : url.includes('/api/approvals/') ? approval()
    : url.includes('/api/approvals') ? { items: [], facets: ui.D.approvalFacets }
    : url.includes('/apply-kit') ? { job: job(), files: [], answers: [], steps: [], checklist: [], links: [] }
    : {};
  fetchMock = vi.fn(async url => ({ ok: true, status: 200, text: async () => JSON.stringify(answer(url)) }));
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
  ui.S.forms = {}; ui.S.busy = {}; ui.S.screen = 'approvals'; ui.S.approvalJob = 7; ui.S.rejecting = null;
  ui.S.overriding = null; ui.S.kitJob = null; ui.D.kit = null;
  ui.S.pf = { tailor: { q: '', min_fit: 0, min_review: 0, board: 'all', status: 'all', sort: 'default' },
              approvals: { q: '', min_fit: 0, min_review: 0, board: 'all', status: 'all', sort: 'default' } };
  Object.assign(ui.D, {
    approvals: [{ ...job(), reviewer_score: 45, age: 'tailored 3d ago', waiting: true }],
    approvalFacets: { total: 1, total_all: 1, boards: ['Manual'] }, approval: approval(),
    summary: { profile: {}, counts: {}, application_mode: 'manual_approval', model: 'm', total_jobs: 1,
      auto_search: { schedule: 'manual', setups: 0, roles: 0, locations: 0, next_run: null, last_run: null } } });
  ui.render();
});

const view = () => document.getElementById('view');

describe('reviewer notes on Approvals', () => {
  const show = resume => {
    ui.D.approval = approval({ resume: { markdown: '# Rohan', generated: '3d ago', edits: [], ...resume } });
    ui.render();
    return view().querySelector('#approval-review');
  };

  it('shows the score and puts concerns ahead of strengths, blockers first', () => {
    const section = show({ reviewer_score: 45, review: [
      { tone: 'good', text: 'Strong Python backend' },
      { tone: 'warn', text: 'No Salesforce experience' },
      { tone: 'bad', text: 'Cites evidence that does not exist' }] });
    expect(section.textContent).toContain('45 / 100');
    expect(section.textContent).toContain('TO WEIGH BEFORE APPROVING · 2');
    expect(section.textContent).toContain('WHAT HOLDS UP · 1');
    expect([...section.querySelectorAll('.pretty')].map(n => n.textContent)).toEqual(
      ['Cites evidence that does not exist', 'No Salesforce experience', 'Strong Python backend']);
  });

  it('treats a note as text, not markup', () => {
    const section = show({ reviewer_score: 70, review: [{ tone: 'warn', text: '<img src=x onerror=alert(1)>' }] });
    expect(section.querySelector('img')).toBeNull();
    expect(section.textContent).toContain('<img src=x onerror=alert(1)>');
  });

  it('says so when the reviewer left no notes', () => {
    expect(show({ reviewer_score: 70, review: [] }).textContent).toContain('The reviewer left no notes.');
  });

  it('points an unreviewed packet at Tailor and shows no score', () => {
    const section = show({ reviewer_score: null, review: [] });
    expect(section.textContent).toContain('has not been reviewed');
    expect(section.textContent).not.toContain('/ 100');
    expect(section.querySelector('[data-act="go"]').dataset.screen).toBe('tailor');
  });
});

describe('rejecting a job from Approvals', () => {
  it('offers Reject beside the other decisions', () => {
    const actions = [...view().querySelectorAll('[data-act]')].map(b => b.dataset.act);
    // This packet scored below the minimum, so the approve button routes through the override panel.
    expect(actions).toEqual(expect.arrayContaining(['reject-open', 'revise', 'approve-override-open']));
    expect(view().querySelector('[data-act="reject-open"]').textContent).toBe('Reject');
    expect(view().textContent).not.toContain('Reject this job?');
  });

  it('asks for a reason but does not insist on one', () => {
    ui.A['reject-open']({ dataset: { id: '7' } });
    const panel = view().textContent;
    expect(panel).toContain('Reject this job?');
    expect(panel).toContain('It leaves the approval queue and your pipeline');
    expect([...view().querySelectorAll('[data-act="reject-reason"]')].map(b => b.textContent.trim()))
      .toEqual(['Pay too low', 'Location or remote policy', 'Wrong seniority', 'Other']);
    expect(view().querySelector('[data-act="reject-confirm"]').textContent).toBe('Reject without a reason');
  });

  it('confirms with the chosen reason and note', async () => {
    ui.A['reject-open']({ dataset: { id: '7' } });
    ui.A['reject-reason']({ dataset: { id: 'compensation' } });
    ui.setF?.('reject', 'notes', 'Band tops out too low');
    expect(view().querySelector('[data-act="reject-confirm"]').textContent).toBe('Reject with this reason');
    expect(view().querySelector('[data-act="reject-reason"]').textContent.trim()).toContain('✓');

    await ui.A['reject-confirm']({ dataset: { id: '7' } });
    const [url, options] = fetchMock.mock.calls.find(([u]) => u.endsWith('/reject'));
    expect(url).toBe('/api/jobs/7/reject');
    expect(JSON.parse(options.body).reason).toBe('compensation');
    expect(ui.S.rejecting).toBeNull();
    expect(ui.S.forms.reject).toBeUndefined();
  });

  it('sends no reason when none was picked', async () => {
    ui.A['reject-open']({ dataset: { id: '7' } });
    await ui.A['reject-confirm']({ dataset: { id: '7' } });
    const [, options] = fetchMock.mock.calls.find(([u]) => u.endsWith('/reject'));
    expect(JSON.parse(options.body)).toEqual({ reason: '', notes: '' });
  });

  it('can back out of approving below the minimum', () => {
    ui.A['approve-override-open']({ dataset: { id: '7' } });
    ui.A['approve-override-close']({}, { preventDefault() {} });
    expect(ui.S.overriding).toBeNull();
    expect(view().textContent).not.toContain('Approve below your reviewer minimum?');
    expect(fetchMock.mock.calls.some(([u]) => u.endsWith('/approve'))).toBe(false);
  });

  it('can be cancelled without rejecting anything', () => {
    ui.A['reject-open']({ dataset: { id: '7' } });
    ui.A['reject-reason']({ dataset: { id: 'seniority' } });
    ui.A['reject-close']({}, { preventDefault() {} });
    expect(ui.S.rejecting).toBeNull();
    expect(ui.S.forms.reject).toBeUndefined();
    expect(view().textContent).not.toContain('Reject this job?');
    expect(fetchMock.mock.calls.some(([u]) => u.endsWith('/reject'))).toBe(false);
  });

  it('offers to approve anyway when the reviewer scored below the minimum', async () => {
    // The fixture's packet scored 45 against a minimum of 55.
    const approveButton = view().querySelector('[data-act="approve-override-open"]');
    expect(approveButton.textContent).toBe('Approve anyway…');
    expect(view().querySelector('[data-act="approve"]')).toBeNull();

    ui.A['approve-override-open']({ dataset: { id: '7' } });
    const panel = view().textContent;
    expect(panel).toContain('Approve below your reviewer minimum?');
    expect(panel).toContain('45');
    expect(panel).toContain('55');
    expect(panel).toContain('your minimum does not change');
    expect(view().querySelector('a[href="#workers/settings"]').textContent).toBe('Change my minimum');

    await ui.A.approve({ dataset: { id: '7', override: '1' } });
    const [url, options] = fetchMock.mock.calls.find(([u]) => u.endsWith('/approve'));
    expect(url).toBe('/api/jobs/7/approve');
    expect(JSON.parse(options.body).override).toBe(true);
    expect(ui.S.overriding).toBeNull();
  });

  it('approves directly when the score clears the minimum', async () => {
    ui.D.approval = approval({ resume: { markdown: '# Rohan', reviewer_score: 80, generated: '3d ago', edits: [] } });
    ui.render();
    expect(view().querySelector('[data-act="approve"]').textContent).toBe('Approve → apply kit');
    expect(view().querySelector('[data-act="approve-override-open"]')).toBeNull();

    await ui.A.approve({ dataset: { id: '7' } });
    const [, options] = fetchMock.mock.calls.find(([u]) => u.endsWith('/approve'));
    expect(JSON.parse(options.body)).toEqual({ override: false, note: '' });
  });

  it('says afterwards that a packet was approved below the minimum', () => {
    ui.D.approval = approval({ job: job({ status: 'approved' }), approval: { override: true, note: 'Checked by hand' } });
    ui.render();
    expect(view().textContent).toContain('Approved below your reviewer minimum');
    expect(view().textContent).toContain('Checked by hand');
  });

  it('shows the rejection on the job afterwards, with or without a reason', () => {
    expect(ui.rejectionBanner(job())).toBe('');
    const withReason = ui.rejectionBanner(job({ rejection: { reason: 'compensation', label: 'Pay too low', note: 'Too low', on: '22 Sep' } }));
    expect(withReason).toContain('Rejected 22 Sep');
    expect(withReason).toContain('Pay too low');
    expect(withReason).toContain('Too low');
    const quiet = ui.rejectionBanner(job({ rejection: { reason: null, label: 'No reason given', note: '', on: '22 Sep' } }));
    expect(quiet).toContain('No reason given');
  });
});
