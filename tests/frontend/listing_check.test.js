import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// Discovery can start a listing check by hand: the server reads the postings in the background, the screen follows.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
let ui;
let fetchMock;

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const toastText = () => document.getElementById('toast').textContent;

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"id":7,"email":"a@b.c","is_admin":true}}</script><div id="view"></div><div id="toast"></div><div id="overlays"></div>';
  if (!ui) {
    fetchMock = vi.fn();
    const context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl,
      setTimeout: () => 0, clearTimeout, setInterval, clearInterval, console, fetch: fetchMock });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext('({ S, A, listingCheckButton, pollListingCheck })', context);
    runInContext('load = async () => {}; render = () => {};', context);
  }
  fetchMock.mockReset();
  ui.S.busy = {}; ui.S.listingRun = null;
});

const button = () => { document.getElementById('view').innerHTML = ui.listingCheckButton(); return document.querySelector('[data-act="listing-check"]'); };

describe('Discovery · Check listings', () => {
  it('offers the check, and shows progress with the button disabled while it runs', () => {
    expect(button().textContent).toBe('Check listings');
    expect(button().disabled).toBe(false);
    ui.S.listingRun = { due: 60, since: 't', checked: 12, removed: 0 };
    expect(button().textContent).toBe('Checking 12 of 60…');
    expect(button().disabled).toBe(true);
  });

  it('starts a run from what the server says is due', async () => {
    fetchMock.mockResolvedValue(json({ due: 60, since: '2026-11-08T10:00:00' }, 202));
    await ui.A['listing-check']();
    expect(fetchMock.mock.calls[0][0]).toBe('/api/jobs/listing-check');
    expect(fetchMock.mock.calls[0][1].method).toBe('POST');
    expect(ui.S.listingRun).toMatchObject({ due: 60, since: '2026-11-08T10:00:00', checked: 0 });
  });

  it('starts nothing when every posting was checked recently, and says so', async () => {
    fetchMock.mockResolvedValue(json({ due: 0, since: '2026-11-08T10:00:00' }, 202));
    await ui.A['listing-check']();
    expect(ui.S.listingRun).toBeNull();
    expect(toastText()).toContain('checked in the last hour');
  });

  it('reports what was removed once every posting was read', async () => {
    ui.S.listingRun = { due: 3, since: '2026-11-08T10:00:00', checked: 0, removed: 0 };
    fetchMock.mockResolvedValue(json({ checked: 3, removed: 2, running: false }));
    await ui.pollListingCheck();
    expect(fetchMock.mock.calls[0][0]).toBe('/api/jobs/listing-check/status?since=2026-11-08T10%3A00%3A00');
    expect(ui.S.listingRun).toBeNull();
    expect(toastText()).toContain('2 dead postings removed');
  });

  it('says a run stopped short, rather than done, when it ends with postings unread', async () => {
    ui.S.listingRun = { due: 640, since: 't', checked: 40, removed: 3, idle: 4 };
    fetchMock.mockResolvedValue(json({ checked: 40, removed: 3, running: false }));
    await ui.pollListingCheck();
    expect(ui.S.listingRun).toBeNull();
    expect(toastText()).toContain('stopped at 40 of 640');
    expect(toastText()).not.toContain('done');
  });

  it('keeps following a run that is still going', async () => {
    ui.S.listingRun = { due: 3, since: 't', checked: 0, removed: 0 };
    fetchMock.mockResolvedValue(json({ checked: 1, removed: 0, running: true }));
    await ui.pollListingCheck();
    expect(ui.S.listingRun.checked).toBe(1);
  });

  it('never calls a run finished when it only lost sight of it', async () => {
    ui.S.listingRun = { due: 3, since: 't', checked: 1, removed: 0 };
    fetchMock.mockRejectedValue(new Error('offline'));
    await ui.pollListingCheck();
    expect(ui.S.listingRun).toBeNull();
    expect(toastText()).toContain('goes on in the background');
    expect(toastText()).not.toContain('no dead postings');
  });
});
