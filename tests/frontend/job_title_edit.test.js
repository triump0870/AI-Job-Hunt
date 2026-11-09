import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// A board can send a URL or a placeholder as the title: the job modal lets a person correct it in place.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
let ui;
let fetchMock;

const job = { id: 5, title: 'https://kaavio.ai', company: 'Kaavio', location: 'Remote', board: 'Hackernews', posted: '25d ago',
  fit: null, status: 'discovered', description: 'Long enough description.', notes: '', url: '', wizard: [], matches: [], gaps: [],
  keywords: [], missing: [], gap_notes: [], fit_analysis: {} };
const modal = () => { document.getElementById('overlays').innerHTML = ui.renderJobModal(); return document.getElementById('overlays'); };

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"id":7,"email":"a@b.c","is_admin":true}}</script><div id="view"></div><div id="toast"></div><div id="overlays"></div>';
  if (!ui) {
    fetchMock = vi.fn();
    const context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl,
      setTimeout, clearTimeout, setInterval, clearInterval, console, fetch: fetchMock });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext('({ S, D, A, renderJobModal })', context);
    runInContext('load = async () => {}; render = () => { document.getElementById("overlays").innerHTML = renderJobModal(); };', context);
  }
  fetchMock.mockReset();
  ui.S.jobOpen = 5; ui.S.titleEdit = null; ui.S.busy = {}; ui.S.forms.jobtitle = {};
  ui.D.jobModal = { ...job };
});

describe('Job modal · editing the title', () => {
  it('shows the title with an edit button, and the editor only after a click', () => {
    let root = modal();
    expect(root.querySelector('.job-title span').textContent).toBe('https://kaavio.ai');
    expect(root.querySelector('#jobtitle-5')).toBeNull();
    ui.A['edit-job-title']({ dataset: { id: '5' } });
    root = document.getElementById('overlays');
    expect(root.querySelector('#jobtitle-5').value).toBe('https://kaavio.ai');
    expect(document.activeElement).toBe(root.querySelector('#jobtitle-5'));
  });

  it('saves the draft and shows the saved title', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ job: { id: 5, title: 'AI Engineer' } }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    ui.S.titleEdit = 5; ui.S.forms.jobtitle = { 5: '  AI Engineer ' };
    await ui.A['save-job-title']({ dataset: { id: '5' } });
    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/jobs/5/title');
    expect(options.method).toBe('PUT');
    expect(JSON.parse(options.body)).toEqual({ title: '  AI Engineer ' });
    expect(ui.S.titleEdit).toBeNull();
    expect(document.querySelector('.job-title span').textContent).toBe('AI Engineer');
  });

  it('stays in the editor with the draft kept when the save is refused', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: "Enter the job's title" }), { status: 422, headers: { 'Content-Type': 'application/json' } }));
    ui.S.titleEdit = 5; ui.S.forms.jobtitle = { 5: ' ' };
    await ui.A['save-job-title']({ dataset: { id: '5' } });
    expect(ui.S.titleEdit).toBe(5);
    expect(ui.S.forms.jobtitle[5]).toBe(' ');
    expect(ui.D.jobModal.title).toBe('https://kaavio.ai');
  });

  it('Escape in the box cancels the edit and keeps the modal open', () => {
    ui.S.titleEdit = 5; ui.S.forms.jobtitle = { 5: 'Half typed' };
    const root = modal();
    root.querySelector('#jobtitle-5').dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(ui.S.titleEdit).toBeNull();
    expect(ui.S.jobOpen).toBe(5);
    expect(ui.S.forms.jobtitle[5]).toBeUndefined();
  });
});
