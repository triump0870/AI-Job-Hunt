import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// Working the Approvals queue: duplicate postings grouped, range picking, sending back with reasons, moving on after
// a decision, and the keyboard shortcuts.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
let ui, context, fetchMock, calls;

const row = (id, over = {}) => ({ id, title: `Engineer ${id}`, company: 'Logitech', location: 'Remote', url: '', source: 'manual',
  board: 'Manual', fit: 80, status: 'tailored', discovered_at: '2026-09-18T09:00:00', posted: '3d', starred: false,
  reviewer_score: 80, age: 'tailored 3d ago', waiting: true, to_decide: true, duplicate_key: `k${id}`, ...over });
const approval = id => ({ job: row(id), min_review_score: 70, notes: '', application_id: null, rejection_reasons: [],
  checklist: [{ label: 'No invented experience', detail: 'Cover letter: “x” isn\'t in …', state: 'warn', level: 'blocker', fix: { label: 'Fix in Tailor', screen: 'tailor' } },
              { label: 'Length', detail: '≈1 pages', state: 'ok', level: 'nudge' }],
  answers: { items: [], known: false }, role_gaps: [{ requirement: 'OAuth', level: 'required', status: 'partial', supported: 'JWT', unsupported: 'OIDC', fatal: false }],
  facts: { location: 'Dublin', work_mode: 'hybrid', work_mode_label: 'Hybrid', location_wanted: false, salary: { text: 'EUR 110K–135K/yr' }, route: { label: 'Greenhouse', auto_submit: false } },
  approve_effect: { kind: 'kit', label: 'Approve → apply kit', detail: 'Auto submit is off' },
  resume: { id: 9, markdown: '# Rohan', reviewer_score: 80, generated: '3d ago', edits: [], cover_letter: 'Dear team', cover_words: 2,
    review: [{ tone: 'bad', group: 'packet', text: 'Unsupported: S3 claim' }, { tone: 'warn', group: 'gap', text: 'OAuth not shown' }] } });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c"}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('scrollTo', vi.fn());
  calls = [];
  const answer = (url, options) => {
    calls.push([url, options?.method || 'GET', options?.body ? JSON.parse(options.body) : null]);
    if (url.includes('/revision')) return { ok: true, status: 'pending_approval', retailor: { queued: [{ id: 1 }], skipped: [] } };
    if (url.includes('/api/tailor-tasks')) return [];
    if (url.includes('/workspace/summary')) return ui.D.summary;
    const one = url.match(/\/api\/approvals\/(\d+)/);
    if (one) return approval(Number(one[1]));
    if (url.includes('/api/approvals')) return { ...ui.D.approvalFacets, items: ui.D.approvals };
    return {};
  };
  fetchMock = vi.fn(async (url, options) => ({ ok: true, status: 200, text: async () => JSON.stringify(answer(url, options)) }));
  vi.stubGlobal('fetch', fetchMock);
  if (!ui) {
    context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext('({ S, D, A, render, pfDefaults })', context);
  }
  context.fetch = fetchMock;
  Object.assign(ui.S, { forms: {}, busy: {}, screen: 'approvals', approvalJob: 1, rejecting: null, overriding: null, revising: null,
    approvalPicked: new Set(), approvalLast: null, dupOpen: new Set(), bulkRejecting: false, apprKeys: false, apprFocus: false, apprQueueHidden: false, pfOpen: null });
  ui.S.pf.approvals = ui.pfDefaults('approvals');
  Object.assign(ui.D, {
    tasks: [], insights: {},
    approvals: [row(1), row(2, { duplicate_key: 'dup', reviewer_score: 60 }), row(3, { duplicate_key: 'dup', reviewer_score: 90 }), row(4)],
    approvalFacets: { total: 4, total_all: 4, boards: ['Manual'], status_counts: { all: 4, waiting: 4, sent_back: 0, approved: 0 } },
    approval: approval(1),
    summary: { profile: {}, counts: {}, application_mode: 'manual_approval', model: 'm', total_jobs: 4,
      auto_search: { schedule: 'manual', setups: 0, roles: 0, locations: 0, next_run: null, last_run: null } } });
  ui.render();
});

const view = () => document.getElementById('view');
const rows = () => [...view().querySelectorAll('.appr-row')].map(r => Number(r.dataset.id));

describe('the Approvals queue', () => {
  it('collapses postings of one role behind their best-scored posting, and picking it picks them all', () => {
    expect(rows()).toEqual([1, 3, 4]);
    const toggle = view().querySelector('[data-act="approval-dup-toggle"]');
    expect(toggle.textContent).toBe('+1');
    ui.A['approval-check'](view().querySelector('.appr-row[data-id="3"] .appr-check'), {});
    expect([...ui.S.approvalPicked].sort()).toEqual([2, 3]);
    ui.A['approval-dup-toggle'](toggle, {});
    expect(rows()).toEqual([1, 3, 2, 4]);
  });

  it('picks a range with shift-click', () => {
    const check = id => view().querySelector(`.appr-row[data-id="${id}"] .appr-check`);
    ui.A['approval-check'](check(1), {});
    ui.A['approval-check'](check(4), { shiftKey: true });
    expect([...ui.S.approvalPicked].sort()).toEqual([1, 2, 3, 4]);
  });

  it('shows the role facts, blockers with their fix, and the label of what approving does', () => {
    const text = view().textContent;
    expect(text).toContain('EUR 110K–135K/yr');
    expect(view().querySelector('.fact.warn').textContent).toBe('Dublin');
    expect(view().querySelector('.check-row.bad').textContent).toContain('Fix in Tailor');
    expect(view().querySelector('.ok-chip').textContent).toContain('Length');
    expect(view().querySelector('[data-act="approve"]').textContent).toBe('Approve → apply kit');
    expect(text).toContain('HOW YOU MEET THE REQUIREMENTS · 0 of 1 met');
  });

  it('sends back with the ticked findings and a note, starts the revision and moves to the next job', async () => {
    ui.A['revise-open']({ dataset: { id: '1' } });
    const boxes = [...view().querySelectorAll('[data-act="revise-pick"]')];
    expect(boxes.map(b => b.checked)).toEqual([true, false]);  // fixes are ticked, suggestions are not
    ui.A['revise-pick'](boxes[1]);
    ui.S.forms.revise.note = 'Shorter summary';
    await ui.A.revise({ dataset: { id: '1' } });
    const [, method, body] = calls.find(([url]) => url.includes('/api/jobs/1/revision'));
    expect(method).toBe('POST');
    expect(body.requests).toEqual(['Unsupported: S3 claim', 'OAuth not shown', 'Shorter summary']);
    expect(body.retailor).toBe(true);
    expect(ui.S.approvalJob).toBe(3);
  });

  it('moves with J and K and toggles the shortcut sheet with ?', async () => {
    document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'j', bubbles: true }));
    expect(ui.S.approvalJob).toBe(3);
    document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'k', bubbles: true }));
    expect(ui.S.approvalJob).toBe(1);
    document.dispatchEvent(new window.KeyboardEvent('keydown', { key: '?', bubbles: true }));
    expect(view().textContent).toContain('Keyboard shortcuts');
  });

  it('edits a change and the cover letter in place, and keeps notes beside the decision', async () => {
    ui.D.approval = { ...approval(1), resume: { ...approval(1).resume, edits: [{ index: 0, section: 'SUMMARY', before: 'Old', after: 'New', status: 'review', evidence: 'e1' }] } };
    ui.S.apprTab = 'edits'; ui.render();
    ui.A['appr-edit-start']({ dataset: { index: '0' } });
    document.getElementById('appr-edit-text-0').value = 'Newer';
    await ui.A['appr-edit-save']({ dataset: { index: '0', resume: '9', job: '1' } });
    const [, method, body] = calls.find(([url]) => url.includes('/api/resumes/9/edits/0'));
    expect(method).toBe('PATCH');
    expect(body).toEqual({ text: 'Newer', status: 'accepted' });
    ui.S.apprTab = 'cover'; ui.render();
    expect(document.getElementById('cover9-text').value).toBe('Dear team');
    ui.S.apprTab = 'resume';
    ui.A['appr-notes']();
    expect(view().querySelector('.appr-head #approval-notes')).not.toBeNull();
  });

  it('explains the score and warns about an old posting', () => {
    ui.D.approval = { ...approval(1), stale: { kind: 'old', text: 'Posted 45 days ago: it may be filled.' },
      score_why: { score: 80, minimum: 70, summary: 'Solid backend match', recommendation: 'APPLY', confidence: 80, decision: 'PASS', decision_reasons: ['meets your minimum'], disputed: 1, checks_failed: 0 } };
    ui.render();
    expect(view().querySelector('.appr-head .banner').textContent).toContain('Posted 45 days ago');
    const why = view().querySelector('#approval-review details');
    expect(why.querySelector('summary').textContent).toBe('Why 80? clears your minimum of 70');
    expect(why.textContent).toContain('1 disputed claim');
  });

  it('opens the job full screen, moves through the queue there and closes with Esc', async () => {
    ui.A['appr-focus']();
    const overlay = document.querySelector('#overlays .appr-focus');
    expect(overlay).not.toBeNull();
    expect(overlay.querySelector('.appr-head [data-act="approve"]').textContent).toBe('Approve → apply kit');
    expect(view().querySelector('.appr-head')).toBeNull();  // one copy of the detail: its fields have ids
    expect(overlay.querySelector('.appr-focus-bar').textContent).toContain('1 of 4');
    document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'j', bubbles: true }));
    expect(ui.S.approvalJob).toBe(3);
    document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(ui.S.apprFocus).toBe(false);
    expect(document.querySelector('#overlays .appr-focus')).toBeNull();
  });

  it('collapses the queue to a rail, and steps through jobs from it', () => {
    ui.A['appr-queue-toggle']();
    expect(view().querySelector('.appr-rail')).not.toBeNull();
    expect(view().querySelector('.appr-rail .rail-pos').textContent).toContain('1');
    ui.A['appr-step']({ dataset: { dir: '1' } });
    expect(ui.S.approvalJob).toBe(2);
    ui.A['appr-queue-toggle']();
    expect(view().querySelector('.appr-rail')).toBeNull();
  });
});
