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
function foField(f, values) {
  const id = `fo-${f.name}`, value = fv('fo', f.name, values[f.name]);
  if (f.type === 'bool') return `<div class="field">${checkF('fo', f.name, values[f.name], esc(f.label))}<span class="settings-help">${esc(f.help)}</span></div>`;
  if (f.type === 'choice') return `<label class="field" for="${id}"><span>${esc(f.label)}</span><select class="select" id="${id}" data-form="fo" data-field="${attr(f.name)}">${(FO.settings.algorithms || []).map(a => aimOpt(a.name, a.label, value)).join('')}</select><span class="settings-help">${esc(f.help)}</span></label>`;
  return `<label class="field" for="${id}"><span>${esc(f.label)}</span>${inputF('fo', f.name, values[f.name], `type="number" min="${f.min}" max="${f.max}" step="1"`)}<span class="settings-help">${esc(f.help)}</span></label>`;
}
const FO_STATE = { HEALTHY: ['Healthy', 'good'], DEGRADED: ['Resting', 'bad'], COOLING: ['Rate limited', 'bad'] };
// What this model falls back to (its own fallbacks, set on its card in Models), when Models & routing is loaded.
function foFallbacks(model) {
  const list = (typeof aimModel === 'function' && aimModel(model)?.fallback_models) || [];
  return list.length ? `<div class="settings-help">Falls back to ${list.map(n => `<span class="mono">${esc(n)}</span>`).join(' → ')}</div>` : '';
}
function foRoutes() {
  const rows = (FO.routes?.routes || []).map(r => {
    const [label, tone] = r.half_open ? ['Probing', 'warn'] : (FO_STATE[r.state] || FO_STATE.HEALTHY);
    // A provider-wide rest: every model that provider serves (and the aliases seen using it), for every worker.
    const name = r.provider
      ? `<span>${esc(r.provider)} — whole provider${r.free_only ? ' (free models)' : ''}</span>${r.members?.length
        ? `<div class="settings-help">Includes ${r.members.map(m => `<span class="mono">${esc(m)}</span>`).join(', ')}</div>` : ''}`
      : esc(r.model);
    return `<tr><td class="mono">${name}</td><td>${r.worker_type ? esc(r.worker_type.replace(/_/g, ' ').toLowerCase()) : '<span class="muted">all workers</span>'}</td>
      <td>${pill(label, tone)}${r.reason ? `<div class="settings-help">${esc(r.reason)}</div>` : ''}${foFallbacks(r.model)}</td>
      <td>${r.score != null ? esc(String(r.score)) : '—'}</td><td>${r.samples != null ? `${esc(String(r.failure_pct ?? 0))}% of ${esc(String(r.samples))}` : '—'}</td>
      <td>${r.p95_ms != null ? `${esc(String(r.p95_ms))} ms` : '—'}</td><td>${r.resting ? esc(foWhen(r.cooling_until)) : '—'}</td>
      <td>${r.state !== 'HEALTHY' ? btn('Put back in service', 'fo-reset', { model: r.model, worker: r.worker_type || '' }, 'sm', isBusy('fo-reset')) : ''}</td></tr>`;
  }).join('');
  return rows ? `<div class="mon-table-wrap aim-wide"><table class="mon-table"><thead><tr><th>Model</th><th>Worker</th><th>State</th><th>Score</th><th>Failures</th><th>p95</th><th>Resting until</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
    : '<p class="settings-help">No model has been judged yet. Routes appear here once workers have made requests through LiteLLM.</p>';
}
function foPanel() {
  if (FO.settings === null && !FO.loading) { FO.loading = true; foLoad(); }
  if (!FO.settings) return settingsGroup('Automatic failover', '', loading());
  const values = FO.settings.values || {}, perm = notifyPermission(), wanted = foWanted();
  const fields = Object.fromEntries((FO.settings.fields || []).map(f => [f.name, f]));
  const alerts = perm === 'unsupported' ? 'This browser has no desktop notifications; you will see a message in the app instead.'
    : perm === 'denied' ? 'Notifications are blocked for this site in your browser settings; you will see a message in the app instead.'
      : wanted && perm === 'granted' ? 'Browser notifications are on.' : wanted ? 'Press the button to allow browser notifications.' : 'Browser notifications are off on this device.';
  return settingsGroup('Automatic failover', 'When a worker’s model is rate limited or keeps failing, its requests move to its backups (set per worker on the Workers tab). A resting model is tried again after a cooldown and returns when it recovers.',
    `${FO.error ? `<p class="settings-error" role="alert">${esc(FO.error)}</p>` : ''}
    <div class="settings-grid">${FO_MAIN.map(name => fields[name]).filter(Boolean).map(f => foField(f, values)).join('')}</div>
    ${settingsDetails('fo-thresholds', 'Thresholds', `<p class="settings-help">When a model counts as failing, and how long it rests.</p><div class="settings-grid">${(FO.settings.fields || []).filter(f => !FO_MAIN.includes(f.name)).map(f => foField(f, values)).join('')}</div>`)}
    <div class="row gap8 wrap">${btn(isBusy('fo-save') ? 'Saving…' : 'Save failover settings', 'fo-save', {}, 'primary sm', isBusy('fo-save'))}
      ${btn(wanted ? 'Turn browser alerts off' : 'Turn browser alerts on', 'fo-notify', { on: wanted ? '' : '1' }, 'sm')}</div>
    <p class="settings-help" role="status">${esc(alerts)}</p>
    <h4 class="mt14">Model health</h4>${foRoutes()}
    <p class="settings-help">A worker without backups never fails over.</p>`);
}

const FAILOVER_ACTIONS = {
  'fo-save': () => busy('fo-save', async () => {
    const form = S.forms.fo || {}, values = {};
    for (const f of FO.settings.fields) if (f.name in form) values[f.name] = f.type === 'bool' ? !!form[f.name] : f.type === 'choice' ? form[f.name] : Number(form[f.name]);
    if (!Object.keys(values).length) { toast('Nothing to save'); return; }
    FO.settings = await api('/api/failover/settings', { method: 'PUT', body: { values } });
    delete S.forms.fo; render();
  }, 'Failover settings saved'),
  'fo-notify': async el => { const perm = await setNotifyFailover(!!el.dataset.on); toast(el.dataset.on ? (perm === 'granted' ? 'Browser alerts on' : 'Alerts on; you will see messages in the app') : 'Browser alerts off'); render(); },
  'fo-reset': el => busy('fo-reset', async () => {
    await api('/api/failover/routes/reset', { method: 'POST', body: { model: el.dataset.model, worker_type: el.dataset.worker || '' } });
    FO.routes = await api('/api/failover/routes'); render();
  }, 'Model put back in service'),
};
Object.assign(SETTINGS_ACTIONS, FAILOVER_ACTIONS);
