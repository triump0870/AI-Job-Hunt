import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'FO', 'FO_KEYS', 'foPanel', 'announceFailover', 'refreshFailover', 'setNotifyFailover', 'SETTINGS_ACTIONS'];
let ui, context, store, shown, permission, fetchMock, routes;

class FakeNotification {
  constructor(title, options) { this.title = title; this.options = options; shown.push(this); }
  close() {}
  static get permission() { return permission; }
  static async requestPermission() { permission = 'granted'; return permission; }
}
const response = data => ({ ok: true, status: 200, text: async () => JSON.stringify(data) });
const flush = () => new Promise(r => setTimeout(r, 0));
const event = (id, cause = 'RATE_LIMIT', message = `model-${id} is resting`) => ({ id, cause, message, worker_type: null, from_model: 'm', to_model: null });
const feed = (events, latest = events.at(-1)?.id ?? 0, notify = true) => ({ events, latest_id: latest, notify });
const settings = () => ({
  values: { enabled: true, algorithm: 'PRIORITY', max_failure_pct: 50, notify: true },
  algorithms: [{ name: 'PRIORITY', label: 'Configured order' }, { name: 'LATENCY', label: 'Fastest' }],
  fields: [
    { name: 'enabled', label: 'Automatic failover', help: 'on/off', type: 'bool', min: 0, max: 0, default: true },
    { name: 'algorithm', label: 'Routing algorithm', help: 'how', type: 'choice', min: 0, max: 0, default: 'PRIORITY' },
    { name: 'max_failure_pct', label: 'Failure rate limit (%)', help: 'limit', type: 'int', min: 1, max: 100, default: 50 },
    { name: 'rate_limit_rest_minutes', label: 'Rest after a rate limit (minutes, 0 = cooldown)', help: 'not used for this long', type: 'int', min: 0, max: 1440, default: 0 },
  ],
});
const route = (extra = {}) => ({ worker_type: 'RANKER', model: 'big-model', state: 'COOLING', resting: true, half_open: false, score: 40, cooling_until: '2026-10-10T12:00:00Z', reason: '3 rate limits in the last 60 minutes', samples: 10, failure_pct: 60, rate_limits: 3, p95_ms: 1200, ...extra });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c"}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast" hidden></div>';
  store = new Map(); shown = []; permission = 'granted';
  routes = {};
  fetchMock = vi.fn(async (url, opt = {}) => response((routes[`${opt.method || 'GET'} ${url.split('?')[0]}`] || (() => ({})))(opt.body ? JSON.parse(opt.body) : undefined)));
  const localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) };
  context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console, localStorage, Notification: FakeNotification, fetch: fetchMock, confirm: vi.fn(() => true) });
  for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'ai_models.js', 'failover.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
    runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
  }
  runInContext(appSource, context, { filename: resolve('app/static/app.js') });
  ui = runInContext(`({${names.join(',')}})`, context);
  ui.S.forms = {}; ui.S.busy = {};
});

describe('browser notification when a model switches', () => {
  it('stays quiet on the first visit, then announces each new event once', () => {
    expect(ui.announceFailover(feed([event(4)]))).toBe(false);  // history is not news
    expect(shown).toHaveLength(0);
    expect(ui.announceFailover(feed([event(4)]))).toBe(false);
    expect(ui.announceFailover(feed([event(5)]))).toBe(true);
    expect(shown).toHaveLength(1);
    expect(shown[0].title).toBe('AI model switched');
    expect(shown[0].options.body).toBe('model-5 is resting');
    expect(ui.announceFailover(feed([event(5)]))).toBe(false);  // a reload repeats nothing
    expect(shown).toHaveLength(1);
  });

  it('folds several events into one notification and says when the model recovered', () => {
    ui.announceFailover(feed([], 0));
    expect(ui.announceFailover(feed([event(1), event(2)]))).toBe(true);
    expect(shown).toHaveLength(1);
    expect(shown[0].options.body).toContain('and 1 more');
    expect(ui.announceFailover(feed([event(3, 'RECOVERED', 'big-model is healthy again')]))).toBe(true);
    expect(shown[1].title).toBe('Model recovered');
  });

  it('respects the account setting, the device switch and the browser permission', () => {
    ui.announceFailover(feed([], 0));
    expect(ui.announceFailover(feed([event(1)], 1, false))).toBe(false);  // notifications off in the settings
    store.set(ui.FO_KEYS.on, 'off');
    expect(ui.announceFailover(feed([event(2)]))).toBe(false);  // off on this device
    expect(store.get(ui.FO_KEYS.seen)).toBe('2');  // switching it back on does not replay it
    store.set(ui.FO_KEYS.on, 'on'); permission = 'denied';
    expect(ui.announceFailover(feed([event(3)]))).toBe(true);  // falls back to a message in the app
    expect(shown).toHaveLength(0);
    expect(document.getElementById('toast').textContent).toContain('AI model switched');
  });

  it('asks for permission only when the person turns alerts on', async () => {
    permission = 'default';
    expect(await ui.setNotifyFailover(true)).toBe('granted');
    expect(store.get(ui.FO_KEYS.on)).toBe('on');
    await ui.setNotifyFailover(false);
    expect(store.get(ui.FO_KEYS.on)).toBe('off');
  });

  it('polls the events feed from the newest id it has seen and acknowledges what it showed', async () => {
    store.set(ui.FO_KEYS.seen, '7');
    routes['GET /api/failover/events'] = () => feed([event(8)]);
    expect(await ui.refreshFailover()).toBe(true);
    expect(fetchMock.mock.calls.find(([u]) => u.startsWith('/api/failover/events'))[0]).toBe('/api/failover/events?since_id=7');
    expect(fetchMock.mock.calls.some(([u, o]) => u === '/api/failover/events/ack' && JSON.parse(o.body).up_to_id === 8)).toBe(true);
    routes['GET /api/failover/events'] = () => { throw new Error('offline'); };
    expect(await ui.refreshFailover()).toBe(false);  // a failed poll is silent
  });
});

describe('failover settings and route health', () => {
  it('shows a provider-wide rest as the whole provider with the aliases it covers', () => {
    ui.FO.settings = settings();
    ui.FO.routes = { routes: [route({ model: 'provider:openrouter', provider: 'openrouter', free_only: true,
      members: ['Openrouter B4'], reason: 'openrouter answered 429 for Openrouter B4' })], resting: 1 };
    document.getElementById('view').innerHTML = ui.foPanel();
    const text = document.getElementById('view').textContent;
    expect(text).toContain('openrouter — whole provider (free models)');
    expect(text).toContain('Includes Openrouter B4');
    expect(text).not.toContain('provider:openrouter');
    const reset = document.querySelector('#view [data-act="fo-reset"]');  // put back in service by its route name
    expect(reset.dataset.model).toBe('provider:openrouter');
  });

  it('lists resting routes with the reason and offers to put them back in service', async () => {
    ui.FO.settings = settings(); ui.FO.routes = { routes: [route(), route({ model: 'ok-model', state: 'HEALTHY', resting: false, reason: null })], resting: 1 };
    document.getElementById('view').innerHTML = ui.foPanel();
    const html = document.getElementById('view').textContent;
    expect(html).toContain('Automatic failover');
    expect(html).toContain('big-model');
    expect(html).toContain('3 rate limits in the last 60 minutes');
    expect(html).toContain('Put back in service');
    expect(document.querySelectorAll('#view [data-act="fo-reset"]')).toHaveLength(1);  // not offered for a healthy route
    // The rest after a rate limit is a main setting, not hidden among the thresholds.
    const rest = document.querySelector('#view [data-field="rate_limit_rest_minutes"]');
    expect(rest).not.toBeNull(); expect(rest.closest('details')).toBeNull();
  });

  it('counts routes by state and narrows the list to the ones that need attention', () => {
    ui.FO.settings = settings();
    ui.FO.routes = { routes: [route({ model: 'ok-model', state: 'HEALTHY', resting: false, reason: null, score: 96 }), route(),
      route({ model: 'tired-model', state: 'DEGRADED' }), route({ model: 'probe-model', half_open: true })], resting: 3 };
    const draw = () => { document.getElementById('view').innerHTML = ui.foPanel(); };
    draw();
    const stats = Object.fromEntries([...document.querySelectorAll('#view .fo-stat')].map(x => [x.querySelector('small').textContent, x.querySelector('b').textContent]));
    expect(stats).toEqual({ Failover: 'On', 'In service': '1', 'Rate limited': '1', Resting: '1', Probing: '1' });
    const names = () => [...document.querySelectorAll('#view .fo-route')].map(x => x.dataset.route);
    expect(names().at(-1)).toBe('ok-model');  // the ones that need attention come first
    expect(document.querySelector('#view .fo-route[data-route="probe-model"]').textContent).toContain('Probing');
    ui.SETTINGS_ACTIONS['fo-attention']();
    draw();
    expect(names()).toEqual(['big-model', 'tired-model', 'probe-model']);
    ui.SETTINGS_ACTIONS['fo-clear']();
    draw();
    expect(names()).toHaveLength(4);
  });

  it('shows a switch for on/off settings and the unit beside a number', () => {
    ui.FO.settings = settings(); ui.FO.routes = { routes: [], resting: 0 };
    document.getElementById('view').innerHTML = ui.foPanel();
    expect(document.querySelector('#view label.aim-switch #fo-enabled').checked).toBe(true);
    const rest = document.querySelector('#view label[for="fo-rate_limit_rest_minutes"]');
    expect(rest.querySelector('span').textContent).toBe('Rest after a rate limit');
    expect(rest.querySelector('.fo-num em').textContent).toBe('min0 = cooldown');  // the unit, then the label's own hint
    expect(document.querySelector('#view label[for="fo-max_failure_pct"] .fo-num em').textContent).toBe('%');
    expect(document.querySelector('#view .fo-empty').textContent).toContain('No model has been judged yet');
  });

  it('saves only the changed settings and shows the saved values', async () => {
    ui.FO.settings = settings(); ui.FO.routes = { routes: [], resting: 0 };
    routes['PUT /api/failover/settings'] = ({ values }) => ({ ...settings(), values: { ...settings().values, ...values } });
    ui.S.forms.fo = { max_failure_pct: '30', algorithm: 'LATENCY', enabled: true };
    await ui.SETTINGS_ACTIONS['fo-save']();
    await flush();
    const put = fetchMock.mock.calls.find(([u, o]) => u === '/api/failover/settings' && o.method === 'PUT');
    expect(JSON.parse(put[1].body).values).toEqual({ max_failure_pct: 30, algorithm: 'LATENCY', enabled: true });
    expect(ui.FO.settings.values.max_failure_pct).toBe(30);
    expect(ui.S.forms.fo).toBeUndefined();
  });

  it('puts a route back in service and reloads the list', async () => {
    ui.FO.settings = settings(); ui.FO.routes = { routes: [route()], resting: 1 };
    routes['POST /api/failover/routes/reset'] = () => ({ ok: true });
    routes['GET /api/failover/routes'] = () => ({ routes: [], resting: 0 });
    await ui.SETTINGS_ACTIONS['fo-reset']({ dataset: { model: 'big-model', worker: 'RANKER' } });
    await flush();
    const post = fetchMock.mock.calls.find(([u]) => u === '/api/failover/routes/reset');
    expect(JSON.parse(post[1].body)).toEqual({ model: 'big-model', worker_type: 'RANKER' });
    expect(ui.FO.routes.routes).toHaveLength(0);
  });
});
