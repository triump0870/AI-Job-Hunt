/* ───────────── model failover: thresholds, route health, switch notifications ─────────────
   Loaded after app.js and settings.js. The Tiers & routing tab embeds foPanel(); the poller runs from boot.
   Failover is a runtime overlay (it never edits the AI configuration), so nothing here goes through draft/apply. */
const FO = { settings: null, routes: null, error: '', loading: false };
const FO_KEYS = { on: 'jobhunt.notify.failover', seen: 'jobhunt.failover.seen' };
const FO_POLL_MS = 20000;
// Shown up front; the remaining thresholds fold away.
const FO_MAIN = ['enabled', 'algorithm', 'rate_limit_rest_minutes', 'usage_limit_rest_minutes', 'notify'];
let foTimer = null;

/* ── notifications: the same browser Notification API and fallbacks as the approval alerts (app.js) ── */
const foWanted = () => lsGet(FO_KEYS.on) !== 'off';
async function setNotifyFailover(on) {
  lsSet(FO_KEYS.on, on ? 'on' : 'off');
  if (on && notifySupported() && Notification.permission === 'default') await Notification.requestPermission();  // needs a click
  return notifyPermission();
}
// Each switch is announced once: the browser remembers the newest event id it has shown, a reload repeats none, and the
// first visit only sets the baseline, so history is never replayed.
function announceFailover(feed) {
  if (!feed) return false;
  const latest = Number(feed.latest_id || 0), seen = lsGet(FO_KEYS.seen);
  if (seen === null) { lsSet(FO_KEYS.seen, latest); return false; }
  const fresh = (feed.events || []).filter(e => e.id > Number(seen));
  if (!fresh.length) return false;
  lsSet(FO_KEYS.seen, Math.max(latest, ...fresh.map(e => e.id)));
  if (!feed.notify || !foWanted()) return false;
  const last = fresh[fresh.length - 1], recovered = fresh.every(e => e.cause === 'RECOVERED');
  const title = recovered ? 'Model recovered' : 'AI model switched';
  const body = fresh.length === 1 ? last.message : `${last.message} (and ${fresh.length - 1} more)`;
  if (notifySupported() && Notification.permission === 'granted') {
    const note = new Notification(title, { body, tag: 'jobhunt-failover' });
    note.onclick = () => { window.focus?.(); note.close?.(); };
  } else toast(`${title} · ${body}`, !recovered);
  return true;
}
async function refreshFailover() {
  try {
    const seen = lsGet(FO_KEYS.seen);
    const feed = await api(`/api/failover/events?since_id=${seen === null ? 0 : Number(seen)}`);
    const shown = announceFailover(feed);
    if (shown) api('/api/failover/events/ack', { method: 'POST', body: { up_to_id: feed.latest_id } }).catch(() => {});
    if (shown && S.screen === 'orchestration') { FO.routes = null; render(); }
    return shown;
  } catch { return false; }
}
// Slow and quiet, like the approval watcher: it never redraws the screen unless something changed.
function watchFailover() {
  clearInterval(foTimer);
  foTimer = setInterval(refreshFailover, FO_POLL_MS);
  refreshFailover();
}

/* ── the failover card in Tiers & routing ── */
async function foLoad() {
  FO.loading = true; FO.error = '';
  try {
    const [settings, routes] = await Promise.all([api('/api/failover/settings'), api('/api/failover/routes')]);
    FO.settings = settings; FO.routes = routes;
  } catch (err) { FO.error = err.message; FO.settings = FO.settings || { values: {}, fields: [], algorithms: [] }; }
  finally { FO.loading = false; render(); }
}
const foWhen = iso => iso ? fmtDateTime(iso) : '—';
// A unit written into a label ("Rest after a rate limit (minutes, 0 = cooldown)") is shown beside the input instead.
const FO_UNITS = [[/minutes?/i, 'min'], [/seconds?/i, 'sec'], [/hours?/i, 'h'], [/%/, '%'], [/\bms\b/i, 'ms']];
function foLabel(label) {
  const note = /\s*\(([^)]*)\)\s*$/.exec(label || ''), parts = note ? note[1].split(',').map(x => x.trim()) : [];
  const unit = parts.length ? (FO_UNITS.find(([re]) => re.test(parts[0])) || [])[1] : '';
  return unit ? { text: label.slice(0, note.index), unit, hint: parts.slice(1).join(', ') } : { text: label || '', unit: '', hint: '' };
}
function foField(f, values) {
  const id = `fo-${f.name}`, value = fv('fo', f.name, values[f.name]);
  if (f.type === 'bool') return `<label class="aim-switch fo-switch" for="${id}"><input type="checkbox" id="${id}" data-form="fo" data-field="${attr(f.name)}" ${fv('fo', f.name, !!values[f.name]) ? 'checked' : ''}><i aria-hidden="true"></i><span><b>${esc(f.label)}</b><small>${esc(f.help)}</small></span></label>`;
  if (f.type === 'choice') return `<label class="field fo-field" for="${id}"><span>${esc(f.label)}</span><select class="select" id="${id}" data-form="fo" data-field="${attr(f.name)}">${(FO.settings.algorithms || []).map(a => aimOpt(a.name, a.label, value)).join('')}</select><span class="settings-help">${esc(f.help)}</span></label>`;
  const { text, unit, hint } = foLabel(f.label);
  return `<label class="field fo-field" for="${id}"><span>${esc(text)}</span><span class="fo-num">${inputF('fo', f.name, values[f.name], `type="number" inputmode="numeric" min="${f.min}" max="${f.max}" step="1"`)}${unit ? `<em>${unit}${hint ? `<i>${esc(hint)}</i>` : ''}</em>` : ''}</span><span class="settings-help">${esc(f.help)}</span></label>`;
}
const FO_STATE = { HEALTHY: ['Healthy', 'good'], DEGRADED: ['Resting', 'bad'], COOLING: ['Rate limited', 'bad'] };
const foState = r => r.half_open ? ['Probing', 'warn'] : (FO_STATE[r.state] || FO_STATE.HEALTHY);
const foMs = ms => ms < 1000 ? `${ms} ms` : `${Math.round(ms / 100) / 10} s`;
// What this model falls back to (its own fallbacks, set on its card in Models), when Models & routing is loaded.
function foFallbacks(model) {
  const list = (typeof aimModel === 'function' && aimModel(model)?.fallback_models) || [];
  return list.length ? `<div class="fo-route-note">Falls back to ${list.map(n => `<span class="mono">${esc(n)}</span>`).join(' → ')}</div>` : '';
}
const foMetric = (label, value, tone = '') => `<div class="fo-metric ${tone}"><small>${label}</small><b>${value}</b></div>`;
function foRoute(r) {
  const [label, tone] = foState(r);
  // A provider-wide rest: every model that provider serves (and the aliases seen using it), for every worker.
  const name = r.provider ? `${esc(r.provider)} — whole provider${r.free_only ? ' (free models)' : ''}` : esc(r.model);
  const scope = r.worker_type ? esc(r.worker_type.replace(/_/g, ' ').toLowerCase()) : 'all workers';
  const fail = r.samples != null ? Number(r.failure_pct ?? 0) : null;
  const score = r.score != null ? `<span class="fo-score ${r.score >= 80 ? 'good' : r.score >= 50 ? 'warn' : 'bad'}" title="Health score ${attr(String(r.score))} of 100"><i style="width:${Math.max(0, Math.min(100, Number(r.score) || 0))}%"></i></span>` : '';
  return `<li class="fo-route ${tone}" data-route="${attr(r.model)}">
    <div class="fo-route-main"><div class="fo-route-name"><span class="fo-dot" aria-hidden="true"></span><b class="mono">${name}</b><span class="fo-scope">${scope}</span></div>
      ${r.reason ? `<div class="fo-route-reason">${esc(r.reason)}</div>` : ''}${r.provider && r.members?.length ? `<div class="fo-route-note">Includes ${r.members.map(m => `<span class="mono">${esc(m)}</span>`).join(', ')}</div>` : ''}${foFallbacks(r.model)}</div>
    <div class="fo-metrics">${foMetric('Score', r.score != null ? `${esc(String(r.score))}${score}` : '—')}${foMetric('Failures', fail != null ? `${esc(String(fail))}% <em>of ${esc(String(r.samples))}</em>` : '—', fail >= 50 ? 'bad' : '')}${foMetric('p95', r.p95_ms != null ? esc(foMs(Number(r.p95_ms))) : '—')}${r.resting ? foMetric('Back at', esc(foWhen(r.cooling_until))) : ''}</div>
    <div class="fo-route-act">${pill(label, tone)}${r.state !== 'HEALTHY' ? btn('Put back in service', 'fo-reset', { model: r.model, worker: r.worker_type || '' }, 'sm', isBusy('fo-reset')) : ''}</div></li>`;
}
const foAttention = r => r.half_open || r.state !== 'HEALTHY';
function foRoutes() {
  const all = FO.routes?.routes || [];
  if (!all.length) return '<div class="fo-empty"><b>No model has been judged yet</b><span>Routes appear here once workers have made requests through LiteLLM.</span></div>';
  const q = String(fv('fo-f', 'q', '')).toLowerCase().trim(), only = !!fv('fo-f', 'attention', false), flagged = all.filter(foAttention).length;
  const shown = all.filter(r => (!only || foAttention(r)) && (!q || `${r.model} ${r.provider || ''} ${r.worker_type || ''} ${(r.members || []).join(' ')} ${r.reason || ''}`.toLowerCase().includes(q)))
    .sort((a, b) => foAttention(b) - foAttention(a));
  const tools = all.length > 5 || flagged ? `<div class="fo-tools" role="search">${all.length > 5 ? `<label class="aim-mf-search"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M10.5 10.5L14 14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg><input type="search" id="fo-f-q" data-form="fo-f" data-field="q" data-live="1" value="${attr(fv('fo-f', 'q', ''))}" placeholder="Search ${all.length} routes by model or worker" aria-label="Search routes" autocomplete="off" spellcheck="false"></label>` : ''}${flagged && flagged < all.length ? `<button type="button" class="aim-mf-chip unhealthy ${only ? 'on' : ''}" data-act="fo-attention" aria-pressed="${only}">Needs attention<b>${flagged}</b></button>` : ''}</div>` : '';
  return `${tools}${shown.length ? `<ul class="fo-routes">${shown.map(foRoute).join('')}</ul>` : `<div class="fo-empty"><b>No routes match</b>${btn('Clear', 'fo-clear', {}, 'sm')}</div>`}`;
}
function foSummary() {
  const all = FO.routes?.routes || [], on = fv('fo', 'enabled', !!FO.settings.values?.enabled);
  const count = fn => all.filter(fn).length;
  const tiles = [['In service', count(r => !foAttention(r)), 'good'], ['Rate limited', count(r => !r.half_open && r.state === 'COOLING'), 'bad'],
    ['Resting', count(r => !r.half_open && r.state === 'DEGRADED'), 'bad'], ['Probing', count(r => r.half_open), 'warn']];
  return `<div class="fo-stats"><div class="fo-stat ${on ? 'brand' : 'off'}"><small>Failover</small><b>${on ? 'On' : 'Off'}</b></div>${tiles.map(([label, n, tone]) => `<div class="fo-stat ${n ? tone : 'zero'}"><small>${label}</small><b>${n}</b></div>`).join('')}</div>`;
}
function foPanel() {
  if (FO.settings === null && !FO.loading) { FO.loading = true; foLoad(); }
  if (!FO.settings) return settingsGroup('Automatic failover', '', loading());
  const values = FO.settings.values || {}, perm = notifyPermission(), wanted = foWanted();
  const all = FO.settings.fields || [], fields = Object.fromEntries(all.map(f => [f.name, f]));
  const main = FO_MAIN.map(name => fields[name]).filter(Boolean), rest = all.filter(f => !FO_MAIN.includes(f.name));
  const switches = main.filter(f => f.type === 'bool'), inputs = main.filter(f => f.type !== 'bool');
  const alerts = perm === 'unsupported' ? 'This browser has no desktop notifications; you will see a message in the app instead.'
    : perm === 'denied' ? 'Notifications are blocked for this site in your browser settings; you will see a message in the app instead.'
      : wanted && perm === 'granted' ? 'Browser notifications are on.' : wanted ? 'Press the button to allow browser notifications.' : 'Browser notifications are off on this device.';
  return settingsGroup('Automatic failover', 'When a worker’s model is rate limited or keeps failing, its requests move to its backups (set per worker on the Workers tab). A resting model is tried again after a cooldown and returns when it recovers.',
    `${FO.error ? `<p class="settings-error" role="alert">${esc(FO.error)}</p>` : ''}${foSummary()}
    <section class="fo-block"><div class="fo-block-head"><h4>Model health</h4><span>A worker without backups never fails over.</span></div>${foRoutes()}</section>
    <section class="fo-block"><div class="fo-block-head"><h4>Failover settings</h4><span>Apply to every worker, straight away.</span></div>
      ${switches.length ? `<div class="fo-switches">${switches.map(f => foField(f, values)).join('')}</div>` : ''}
      ${inputs.length ? `<div class="fo-fields">${inputs.map(f => foField(f, values)).join('')}</div>` : ''}
      ${rest.length ? settingsDetails('fo-thresholds', 'Thresholds', `<p class="settings-help">When a model counts as failing, and how long it rests.</p><div class="fo-fields">${rest.map(f => foField(f, values)).join('')}</div>`) : ''}
      <div class="fo-foot"><div class="fo-alerts"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 2.2a3.6 3.6 0 0 0-3.6 3.6c0 3-1.4 4.2-1.4 4.2h10s-1.4-1.2-1.4-4.2A3.6 3.6 0 0 0 8 2.2zM6.6 12.4a1.5 1.5 0 0 0 2.8 0" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg><p class="settings-help" role="status">${esc(alerts)}</p>${btn(wanted ? 'Turn browser alerts off' : 'Turn browser alerts on', 'fo-notify', { on: wanted ? '' : '1' }, 'sm ghost')}</div>
        ${btn(isBusy('fo-save') ? 'Saving…' : 'Save failover settings', 'fo-save', {}, 'primary sm', isBusy('fo-save'))}</div></section>`);
}

const FAILOVER_ACTIONS = {
  'fo-save': () => busy('fo-save', async () => {
    const form = S.forms.fo || {}, values = {};
    for (const f of FO.settings.fields) if (f.name in form) values[f.name] = f.type === 'bool' ? !!form[f.name] : f.type === 'choice' ? form[f.name] : Number(form[f.name]);
    if (!Object.keys(values).length) { toast('Nothing to save'); return; }
    FO.settings = await api('/api/failover/settings', { method: 'PUT', body: { values } });
    delete S.forms.fo; render();
  }, 'Failover settings saved'),
  'fo-attention': () => { setF('fo-f', 'attention', !fv('fo-f', 'attention', false)); render(); },
  'fo-clear': () => { delete S.forms['fo-f']; render(); },
  'fo-notify': async el => { const perm = await setNotifyFailover(!!el.dataset.on); toast(el.dataset.on ? (perm === 'granted' ? 'Browser alerts on' : 'Alerts on; you will see messages in the app') : 'Browser alerts off'); render(); },
  'fo-reset': el => busy('fo-reset', async () => {
    await api('/api/failover/routes/reset', { method: 'POST', body: { model: el.dataset.model, worker_type: el.dataset.worker || '' } });
    FO.routes = await api('/api/failover/routes'); render();
  }, 'Model put back in service'),
};
Object.assign(SETTINGS_ACTIONS, FAILOVER_ACTIONS);
