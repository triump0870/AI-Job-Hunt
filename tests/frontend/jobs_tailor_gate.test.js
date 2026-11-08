import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// Tailoring works from the Ranker's analysis: the Jobs screen offers it only for ranked jobs.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
let ui;

const row = (id, fit, description_state = 'PARSED') => ({ id, title: `Product Manager ${id}`, company: 'Paytm',
  location: 'Bengaluru, India', fit, status: 'discovered', board: 'Naukri', posted: '5d ago', description_state });
const jobs = items => ({ items, page: 1, pages: 1, per_page: 25, total: items.length, total_all: items.length,
  boards: ['Naukri'] });
const view = () => { document.getElementById('view').innerHTML = ui.renderTable(); return document.getElementById('view'); };
const tailorButton = root => [...root.querySelectorAll('[data-act="bulk-to-tailor"]')][0];

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"id":7,"email":"a@b.c","is_admin":true}}</script><div id="view"></div><div id="toast"></div>';
  if (!ui) {
    const context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl,
      setTimeout, clearTimeout, setInterval, clearInterval, console, fetch: vi.fn() });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext('({ S, D, A, renderTable, renderTriage, freshFilters, tableQuery, DEFAULT_FILTERS, FILTERS_KEY, recoverQueuedMsg })', context);
    runInContext('load = async () => {}; render = () => {};', context);
  }
  ui.S.checked = new Set(); ui.S.rankedIds = new Set(); ui.S.parsedIds = new Set(); ui.S.busy = {}; ui.S.selJob = null; ui.D.selDetail = null;
  ui.S.filters = { q: '', min_fit: 0, board: 'all', stage: 'discovered', sort: 'fit', per_page: 25 };
});

describe('Jobs · Bulk tailor needs a ranking', () => {
  it('is disabled when only unranked jobs are selected, and says why', () => {
    ui.D.jobs = jobs([row(1, null), row(2, null)]);
    ui.S.checked = new Set([1]);
    const button = tailorButton(view());
    expect(button.disabled).toBe(true);
    expect(button.textContent.trim()).toBe('Bulk tailor');
    expect(button.parentElement.title).toContain('1 selected job is not ranked yet');
  });

  it('counts only the ranked jobs in a mixed selection', () => {
    ui.D.jobs = jobs([row(1, null), row(2, 78), row(3, 64)]);
    ui.S.checked = new Set([1, 2, 3]);
    const button = tailorButton(view());
    expect(button.disabled).toBe(false);
    expect(button.textContent.trim()).toBe('Bulk tailor 2');
    expect(button.parentElement.title).toContain('1 selected job is not ranked yet');
  });

  it('offers to rank an unranked job instead of tailoring it', () => {
    ui.D.jobs = jobs([row(1, null)]);
    ui.S.selJob = 1;
    ui.D.selDetail = { ...row(1, null), matches: [], gaps: [], missing: [], summary: '' };
    const root = view();
    expect(root.querySelector('[data-act="rank-job"]').textContent.trim()).toBe('Rank first');
    expect(root.querySelector('[data-act="triage"][data-tailor]')).toBeNull();
  });
});

describe('Jobs · ranking and tailoring need a parsed description', () => {
  const bar = root => root.querySelector('.selbar');
  const act = (root, name) => root.querySelector(`.selbar [data-act="${name}"]`);

  it('offers only Parse when no selected job is parsed', () => {
    ui.D.jobs = jobs([row(1, 88, 'FAILED'), row(2, null, 'NOT_PARSED')]);
    ui.S.checked = new Set([1, 2]);
    const root = view();
    expect(act(root, 'bulk-parse').textContent.trim()).toBe('Parse 2');
    expect(act(root, 'bulk-rank')).toBeNull();
    expect(act(root, 'bulk-to-tailor')).toBeNull();
    expect(bar(root).textContent).toContain('2 selected');
  });

  it('counts only parsed jobs for Rank and Bulk tailor in a mixed selection', () => {
    ui.D.jobs = jobs([row(1, 88, 'FAILED'), row(2, 80), row(3, null)]);
    ui.S.checked = new Set([1, 2, 3]);
    const root = view();
    expect(act(root, 'bulk-parse').textContent.trim()).toBe('Parse 1');
    expect(act(root, 'bulk-rank').textContent.trim()).toBe('Rank 2');
    expect(act(root, 'bulk-to-tailor').textContent.trim()).toBe('Bulk tailor 1');
  });

  it('keeps the old buttons when parsing is off', () => {
    ui.D.jobs = { ...jobs([row(1, 88, 'NOT_PARSED')]), parse_enabled: false };
    ui.S.checked = new Set([1]);
    const root = view();
    expect(act(root, 'bulk-parse')).toBeNull();
    expect(act(root, 'bulk-rank').textContent.trim()).toBe('Rank 1');
    expect(act(root, 'bulk-to-tailor').textContent.trim()).toBe('Bulk tailor 1');
  });

  it('offers Parse instead of Rank or Tailor for a job whose parse failed', () => {
    ui.D.jobs = jobs([row(1, 88, 'FAILED')]);
    ui.S.selJob = 1;
    ui.D.selDetail = { ...row(1, 88, 'FAILED'), parse_enabled: true, matches: [], gaps: [], missing: [], summary: '' };
    const root = view();
    expect(root.querySelector('[data-act="parse-job"]').textContent.trim()).toBe('Parse again');
    expect(root.querySelector('[data-act="rank-job"]')).toBeNull();
    expect(root.querySelector('[data-act="triage"][data-tailor]')).toBeNull();
  });

  it('keeps Rank now in triage when parsing is off, though the card itself does not say so', () => {
    const card = { ...row(1, null, 'NOT_PARSED'), agency: false, summary: '', matches: [], gaps: [], missing: [], keywords: [] };
    ui.S.triagedThisSession = 0; ui.D.triageDetail = null; ui.D.setups = [];
    ui.D.queue = { items: [card], total: 1, parse_enabled: false };
    document.getElementById('view').innerHTML = ui.renderTriage();
    let root = document.getElementById('view');
    expect(root.querySelector('[data-act="rank-job"]')).not.toBeNull();
    expect(root.querySelector('[data-act="parse-job"]')).toBeNull();
    ui.D.queue = { ...ui.D.queue, parse_enabled: true };
    document.getElementById('view').innerHTML = ui.renderTriage();
    root = document.getElementById('view');
    expect(root.querySelector('[data-act="parse-job"]')).not.toBeNull();
    expect(root.querySelector('[data-act="rank-job"]')).toBeNull();
  });

  it('shows a disabled Parsing… while the parse runs', () => {
    ui.D.jobs = jobs([row(1, null, 'PARSING')]);
    ui.S.selJob = 1;
    ui.D.selDetail = { ...row(1, null, 'PARSING'), parse_enabled: true, matches: [], gaps: [], missing: [], summary: '' };
    const parse = view().querySelector('[data-act="parse-job"]');
    expect(parse.disabled).toBe(true);
    expect(parse.textContent.trim()).toBe('Parsing…');
  });
});

describe('Jobs · date filter and applied jobs', () => {
  it('offers found/posted date windows, a newest-posted sort and a Show applied box', () => {
    ui.D.jobs = { ...jobs([row(1, 80)]), applied_hidden: 3, total_all: 4 };
    ui.S.datePop = true; ui.S.moreFilters = true;
    ui.S.filters = { ...ui.S.filters, date_field: 'found', within: '7d' };
    const root = view();
    expect([...root.querySelectorAll('[data-act="date-field"]')].map(o => o.dataset.value)).toEqual(['found', 'posted']);
    expect([...root.querySelectorAll('[data-act="date-window"]')].map(o => o.textContent))
      .toEqual(['24 hours', '3 days', '7 days', '15 days', '30 days', '60 days', '90 days']);
    expect(root.querySelector('#flt-date').textContent).toContain('Found · last 7 days');
    expect(root.querySelector('#flt-sort option[value="posted"]').textContent).toBe('Sort: newest posted');
    expect(root.querySelector('#flt-include-applied').checked).toBe(false);
    ui.S.datePop = false; ui.S.moreFilters = false;
    expect(root.textContent).toContain('3 applied jobs hidden');
    expect(root.textContent).toContain('no filters applied');
  });
});

describe('Jobs · modern filter bar', () => {
  it('keeps bulk actions out of the toolbar until rows are selected', () => {
    ui.D.jobs = jobs([row(1, 80)]);
    let root = view();
    expect(root.querySelector('.selbar')).toBeNull();
    expect(root.querySelector('[data-act="rank-batch"]')).not.toBeNull();
    ui.S.checked = new Set([1]);
    root = view();
    expect(root.querySelector('.selbar').textContent).toContain('1 selected');
    expect(root.querySelector('[data-act="rank-batch"]')).toBeNull();
    expect(root.querySelector('.selbar [data-act="bulk-delete"]').disabled).toBe(false);
  });

  it('offers to fetch every missing description only under the No description filter', () => {
    ui.S.checked = new Set();
    ui.S.filters.description = 'all';
    expect(view().querySelector('[data-act="recover-descriptions"]')).toBeNull();
    ui.S.filters.description = 'NO_DESCRIPTION';
    const button = view().querySelector('[data-act="recover-descriptions"]');
    expect(button.textContent).toContain('Fetch missing descriptions');
    ui.S.checked = new Set([1]);
    expect(view().querySelector('[data-act="recover-descriptions"]')).toBeNull();
    ui.S.checked = new Set();
    ui.S.filters.description = 'all';
  });

  it('says what the bulk fetch queued and what it left out', () => {
    const none = { queued: 0, already_queued: 0, hidden_by_domain: 0, held_for_review: 0, set_aside: 0, no_url: 0, over_limit: 0 };
    expect(ui.recoverQueuedMsg({ ...none, queued: 1 })).toBe('Queued 1 job to read from its posting');
    expect(ui.recoverQueuedMsg({ ...none, queued: 29, hidden_by_domain: 77, already_queued: 2 }))
      .toBe('Queued 29 jobs to read from their postings · 2 already queued · 77 hidden by your domain filter left out');
    expect(ui.recoverQueuedMsg({ ...none, over_limit: 5 })).toBe('Nothing new to fetch · 5 more: click again once these finish');
  });

  it('labels older-than and between-dates ranges, and offers No posting date only for Posted', () => {
    ui.D.jobs = jobs([row(1, 80)]);
    ui.S.datePop = true;
    ui.S.filters = { ...ui.S.filters, date_field: 'found', within: '', older_than: '30d' };
    let root = view();
    expect(root.querySelector('#flt-date').textContent).toContain('Found · older than 30 days');
    expect(root.querySelector('[data-mode="unknown"]')).toBeNull();
    expect(root.querySelector('[data-act="date-window"][data-key="older_than"][data-value="30d"]').classList.contains('on')).toBe(true);
    ui.S.filters = { ...ui.S.filters, date_field: 'posted', older_than: '', date_from: '2026-10-01', date_to: '' };
    root = view();
    expect(root.querySelector('#flt-date').textContent).toMatch(/Posted · .*Oct.* – today/);
    expect(root.querySelector('#flt-date-from').value).toBe('2026-10-01');
    expect(root.querySelector('[data-mode="unknown"]')).not.toBeNull();
    ui.S.datePop = false;
  });

  it('shows a removable chip per active filter and counts the More filters ones', () => {
    ui.D.jobs = { ...jobs([row(1, 80)]), setups: [[4, 'Backend · remote']], job_types: ['full-time'] };
    ui.S.filters = { ...ui.S.filters, stage: 'all', min_fit: 70, work_mode: 'remote', setup_id: '4', has_salary: true, keywords: ['python'] };
    const root = view();
    const chips = [...root.querySelectorAll('.filter-chips .chip')].map(c => c.firstChild.textContent);
    expect(chips).toEqual(['Fit ≥ 70', 'Remote', 'Setup: Backend · remote', 'Mentions python', 'Salary listed']);
    expect(root.querySelector('[data-act="toggle-more-filters"]').textContent).toContain('More filters · 4');
    expect(root.querySelector('[data-act="clear-filter"][aria-label="Remove filter Remote"]').dataset.keys).toBe('work_mode');
  });
});

describe('Jobs · filter actions', () => {
  let A;
  beforeEach(() => {
    A = ui.A;
    ui.S.filters = ui.freshFilters();
  });

  it('choosing Older than clears the other date modes and asks the server for older_than', async () => {
    ui.S.filters.within = '3d';
    A['date-mode']({ dataset: { mode: 'older' } });
    expect(ui.S.filters).toMatchObject({ within: '', older_than: '7d', date_from: '', posted_unknown: false });
    expect(ui.tableQuery(ui.S.filters)).toContain('older_than=7d');
    expect(ui.tableQuery(ui.S.filters)).not.toContain('within=');
  });

  it('a chip × resets only its own keys, and Reset restores every default but rows per page', () => {
    Object.assign(ui.S.filters, { work_mode: 'hybrid', min_fit: 50, keywords: ['go'], per_page: 50 });
    A['clear-filter']({ dataset: { keys: 'work_mode' } });
    expect(ui.S.filters).toMatchObject({ work_mode: 'all', min_fit: 50, keywords: ['go'] });
    A['reset-filters'](null, { preventDefault() {} });
    expect(ui.S.filters).toEqual({ ...ui.DEFAULT_FILTERS, per_page: 50 });
    expect(ui.tableQuery(ui.S.filters)).not.toContain('keywords');
  });

  it('remembers filters per account, so a shared browser never shows another account\'s searches', () => {
    expect(ui.FILTERS_KEY).toBe('discoveryFilters:7');
  });
});
