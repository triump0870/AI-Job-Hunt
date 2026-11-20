import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// Tailor and Approvals share one filter bar: status tabs with counts, filter pills, removable chips.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
let ui;
const facets = (extra = {}) => ({ items: [], total: 12, total_all: 12, boards: ['Naukri', 'LinkedIn'], locations: [],
  easy_apply: 3, status_counts: { all: 12, starred: 1, waiting: 7, sent_back: 2, approved: 2, rejected: 3 }, ...extra });
const view = (f = facets()) => { document.getElementById('view').innerHTML = ui.renderPacketFilters('approvals', f); return document.getElementById('view'); };

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"id":7,"email":"a@b.c","is_admin":true}}</script><div id="view"></div><div id="toast"></div>';
  if (!ui) {
    const context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl,
      setTimeout, clearTimeout, setInterval, clearInterval, console, fetch: vi.fn() });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext('({ S, D, A, renderPacketFilters, pfDefaults })', context);
    context.document = document;
    runInContext('load = async () => {}; render = () => {};', context);
  }
  ui.S.pf.approvals = ui.pfDefaults('approvals'); ui.S.rangeEdit = null; ui.S.rangeError = '';
  ui.S.pfOpen = 'approvals';  // Approvals keeps these filters in its Filters popover
});

describe('Packet filter bar', () => {
  it('shows each status as a tab with its count, the current one selected', () => {
    ui.S.pf.approvals.status = 'waiting';
    const tabs = [...view().querySelectorAll('.pf-tab')];
    expect(tabs.map(t => t.textContent)).toEqual(['All12', 'Starred1', 'New7', 'Sent back2', 'Approved2', 'Rejected3']);
    expect(tabs.find(t => t.classList.contains('on')).dataset.status).toBe('waiting');
    ui.A['pf-status'](tabs[4]);
    expect(ui.S.pf.approvals.status).toBe('approved');
  });

  it('offers fit ≥ and ≤ in one pill and lists set filters as removable chips', () => {
    let root = view();
    expect(root.querySelector('.filter-chips')).toBeNull();
    expect([...root.querySelector('#pf-approvals-fit').options].map(o => o.textContent))
      .toEqual(['Any fit', 'Fit ≥ 50', 'Fit ≥ 70', 'Fit ≥ 80', 'Fit ≤ 30', 'Fit ≤ 50', 'Fit ≤ 70', 'Custom range…']);
    Object.assign(ui.S.pf.approvals, { max_fit: 50, min_review: 75, easy_apply: 'only' });
    root = view();
    expect(root.querySelector('#pf-approvals-fit').value).toBe('max:50');
    expect(root.querySelector('#pf-approvals-fit').classList.contains('on')).toBe(true);
    expect(root.querySelector('#pf-approvals-easy-apply').checked).toBe(true);
    const chips = [...root.querySelectorAll('.filter-chips .chip')];
    expect(chips.map(c => c.firstChild.textContent)).toEqual(['Fit ≤ 50', 'Reviewer ≥ 75', 'Easy Apply only']);
    ui.A['pf-clear'](chips[0].querySelector('button'));
    expect(ui.S.pf.approvals.max_fit).toBe(0);
    expect(ui.S.pf.approvals.min_review).toBe(75);
  });

  it('hides the board pill when there is only one board', () => {
    expect(view().querySelector('#pf-approvals-board')).not.toBeNull();
    expect(view(facets({ boards: ['Naukri'] })).querySelector('#pf-approvals-board')).toBeNull();
  });

  it('opens a custom range editor from either score pill and applies a validated range', () => {
    const pick = (id, value) => { const el = document.getElementById(id); el.value = value; el.dispatchEvent(new Event('change', { bubbles: true })); };
    view();
    pick('pf-approvals-review', 'custom');
    expect(ui.S.rangeEdit).toEqual({ scope: 'approvals', field: 'review' });
    let root = view();
    expect(root.querySelector('#range-panel').getAttribute('aria-label')).toBe('Custom reviewer range');
    root.querySelector('#range-lo').value = '90'; root.querySelector('#range-hi').value = '60';
    ui.A['range-apply']();
    expect(ui.S.rangeError).toContain('must not be above');
    root = view();
    root.querySelector('#range-lo').value = '60'; root.querySelector('#range-hi').value = '90';
    ui.A['range-apply']();
    expect([ui.S.pf.approvals.min_review, ui.S.pf.approvals.max_review, ui.S.rangeEdit]).toEqual([60, 90, null]);
    root = view();
    const review = root.querySelector('#pf-approvals-review');
    expect(review.selectedOptions[0].textContent).toBe('Reviewer 60–90');
    expect([...root.querySelectorAll('.filter-chips .chip')].map(c => c.firstChild.textContent)).toEqual(['Reviewer 60–90']);
    pick('pf-approvals-fit', 'custom');
    root = view();
    root.querySelector('#range-lo').value = '63'; root.querySelector('#range-hi').value = '';
    ui.A['range-apply']();
    expect([ui.S.pf.approvals.min_fit, ui.S.pf.approvals.max_fit]).toEqual([63, 0]);
    expect(view().querySelector('#pf-approvals-fit').selectedOptions[0].textContent).toBe('Fit ≥ 63');
  });

  it('rejects scores outside 0–100', () => {
    ui.S.rangeEdit = { scope: 'approvals', field: 'fit' };
    const root = view();
    root.querySelector('#range-hi').value = '120';
    ui.A['range-apply']();
    expect(ui.S.rangeError).toBe('Use whole numbers from 0 to 100.');
    expect(ui.S.pf.approvals.max_fit).toBe(0);
  });

  it('refuses an entered "To" of 0 instead of reading it as no limit', () => {
    ui.S.rangeEdit = { scope: 'approvals', field: 'fit' };
    let root = view();
    root.querySelector('#range-lo').value = '60'; root.querySelector('#range-hi').value = '0';
    ui.A['range-apply']();
    expect(ui.S.rangeError).toBe('Leave “To” empty for no upper limit, or use 1–100.');
    expect([ui.S.pf.approvals.min_fit, ui.S.rangeEdit]).toEqual([0, { scope: 'approvals', field: 'fit' }]);
    root = view();
    root.querySelector('#range-lo').value = '60'; root.querySelector('#range-hi').value = '';
    ui.A['range-apply']();
    expect([ui.S.pf.approvals.min_fit, ui.S.pf.approvals.max_fit, ui.S.rangeEdit]).toEqual([60, 0, null]);
  });

  it('keeps Approvals to one row: the other filters wait in a popover that counts the active ones', () => {
    ui.S.pfOpen = null;
    Object.assign(ui.S.pf.approvals, { min_fit: 70, work_mode: 'remote' });
    let root = view(facets({ work_modes: { remote: 2 } }));
    expect(root.querySelector('#pf-approvals-fit')).toBeNull();
    const open = root.querySelector('[data-act="pf-open"]');
    expect(open.textContent).toContain('Filters');
    expect(open.querySelector('.pf-tab-count').textContent).toBe('2');
    ui.S.pfOpen = 'approvals';
    root = view(facets({ work_modes: { remote: 2 } }));
    expect(root.querySelector('.pf-pop #pf-approvals-fit')).not.toBeNull();
    expect(root.querySelector('.pf-pop #pf-approvals-work_mode').value).toBe('remote');
  });
});
