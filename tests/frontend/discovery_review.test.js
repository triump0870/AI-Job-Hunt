import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// Discovery → Needs review: jobs the domain check could not settle wait for the person, who keeps or removes them.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
let ui, fetchMock;
const review = () => ({ total: 2, jobs: [
  { id: 7, title: 'Payments Lead', company: 'Acme', location: 'Berlin', url: 'https://x.test/7', reason: 'Could be either.',
    your_skills_found: ['python'], points_against: ['Run monthly payroll'], model_said: 'UNCERTAIN', summary: 'Own payments.' },
  { id: 8, title: '<b>Ops</b>', company: 'Beta', location: '', url: '', reason: '', your_skills_found: [], points_against: [],
    model_said: 'UNCERTAIN', summary: '' }] });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c","is_admin":true}}</script>'
    + '<nav id="nav"></nav><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><header id="top"></header>'
    + '<div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ done: [7], skipped: [], matches: true, total: 0, jobs: [] }) }));
  const context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl,
    setTimeout, clearTimeout, setInterval, clearInterval, console, fetch: fetchMock });
  for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
    runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
  }
  const names = ['S', 'D', 'A', 'renderReview', 'domainCheckNote'];
  runInContext(`${appSource}\n;globalThis.__ui = { ${names.map(n => `${n}: typeof ${n} !== 'undefined' ? ${n} : undefined`).join(', ')} };`,
    context, { filename: resolve('app/static/app.js') });
  ui = context.__ui;
  ui.D.review = review();
  ui.S.reviewChecked = new Set();
});

const html = () => { const box = document.createElement('div'); box.innerHTML = ui.renderReview(); return box; };

describe('Discovery → Needs review', () => {
  it('lists each job with the reason and the skills found on it', () => {
    const box = html();
    expect(box.textContent).toContain('2 jobs could go either way');
    expect(box.textContent).toContain('Could be either.');
    expect(box.textContent).toContain('python');
    expect(box.textContent).toContain('Run monthly payroll');
    expect(box.textContent).toContain('None of your skills found');
  });

  it('never renders a posting title as markup', () => {
    expect(html().querySelector('b b')).toBeNull();
    expect(html().textContent).toContain('<b>Ops</b>');
  });

  it('offers both choices per job, and for the selected jobs', () => {
    expect(html().querySelectorAll('[data-act="review-decide"][data-matches="1"]')).toHaveLength(2);
    ui.S.reviewChecked = new Set([7, 8]);
    const box = html();
    expect(box.textContent).toContain('These 2 match');
    expect(box.textContent).toContain('Remove these 2');
    expect(box.querySelector('[data-act="review-decide"][data-ids="7,8"]')).not.toBeNull();
  });

  it('says so when nothing is waiting', () => {
    ui.D.review = { total: 0, jobs: [] };
    expect(html().textContent).toContain('Nothing needs your review');
  });

  it('puts the same choice on the job itself', () => {
    const box = document.createElement('div');
    box.innerHTML = ui.domainCheckNote({ id: 9, domain_match: 'REVIEW', domain_reason: 'Needs your review: Unclear.' });
    expect(box.textContent).toContain('Unclear.');
    expect(box.querySelector('[data-act="review-decide"][data-ids="9"][data-matches="0"]')).not.toBeNull();
  });
});
