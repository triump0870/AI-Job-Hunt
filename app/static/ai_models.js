/* AI Orchestration → Models & routing: which model each AI worker uses, the model catalog and tiers, failover, and the
   route / LiteLLM connection / version history. Loaded after settings.js and before app.js; orchestration.js shows it
   as its Models workspace. Every value comes from /api/settings/ai.
   Saving applies at once: a save writes the draft and applies it as a new version (validated, and LiteLLM synced when
   needed), so every change can be rolled back from Settings → History. A refused save leaves the draft for Undo. */
'use strict';
// [id, label, symbol, what it holds]
const AIM_TABS = [['workers', 'Workers', '⇢', 'The model each AI worker uses'], ['models', 'Models', '▤', 'Catalog, tiers and auto router'],
  ['reliability', 'Reliability', '⟳', 'Failover and model health'], ['settings', 'Settings', '⚙', 'Route, connection and history']];
// Older links (orchestration nodes, saved URLs) name tabs that were merged.
const AIM_TAB_ALIASES = { overview: 'workers', routing: 'models', litellm: 'settings', advanced: 'settings' };
const AIM_CLASSIFIERS = { DISABLED: 'Disabled', HEURISTIC: 'Heuristic', HEURISTIC_V2: 'Heuristic v2 (trained)', LLM: 'LLM classifier', CUSTOM: 'Custom Job Hunt classifier' };
const AIM_CONDITIONS = { LOW_CONFIDENCE: 'Confidence below threshold', SCHEMA_FAILURE: 'Structured-output validation failed', AMBIGUOUS: 'Ambiguity detected', FATAL_REQUIREMENT_UNCERTAIN: 'Fatal requirement uncertain', MANUAL: 'Manual request' };
const AIM_SYNC = { SYNCED: ['Synced', 'good'], PENDING: ['Pending', 'warn'], APPLYING: ['Applying', 'brand'], OUT_OF_SYNC: ['Out of sync', 'bad'], FAILED: ['Failed', 'bad'] };
const AIM_CONN = { CONNECTED: ['Connected', 'good'], DISCONNECTED: ['Disconnected', 'muted'], AUTH_FAILED: ['Authentication failed', 'bad'], UNAVAILABLE: ['LiteLLM unavailable', 'bad'], UNKNOWN: ['Not tested', 'muted'] };
const AIM_BACKUPS = 4;  // fallback model + up to three secondary models
const AIM = { tab: 'workers', data: undefined, loading: false, error: '', drawer: null, failed: null, runtime: null, versions: null, audit: null, tests: {}, ollama: null, adding: false, insights: undefined, insightOpen: {} };

const aimData = () => AIM.data || {};
const aimRev = () => aimData().connection?.draft_revision;
const aimModels = () => aimData().models || [];
const aimModel = name => aimModels().find(m => m.name === name);
const aimTokens = n => n == null ? 'unknown' : n >= 1000 ? `${Math.round(n / 1024 * 10) / 10}K`.replace('.0K', 'K') : String(n);
const aimYes = v => v === true ? 'yes' : v === false ? 'no' : 'unknown';
// `data` adds hints the searchable model picker (model_picker.js) shows: name, sub, host, meta.
const aimOpt = (value, label, selected, data = {}) => `<option value="${attr(value)}" ${String(value) === String(selected ?? '') ? 'selected' : ''}${Object.entries(data).filter(([, x]) => x).map(([k, x]) => ` data-${k}="${attr(x)}"`).join('')}>${esc(label)}</option>`;
// Where a model runs; MIXED is a router with both kinds behind it. The server leaves it empty when unknown, so no tag shows.
const AIM_HOSTING = { LOCAL: 'Local', CLOUD: 'Cloud', MIXED: 'Local + Cloud' };
const aimHosting = name => AIM_HOSTING[aimModel(name)?.hosting] || '';
const aimHostName = name => aimHosting(name) ? `${name} · ${aimHosting(name)}` : name;  // options cannot hold a styled tag
const aimHostTag = name => { const h = aimHosting(name); return h ? `<em class="aim-host ${aimModel(name).hosting.toLowerCase()}">${h}</em>` : ''; };
// Chat pickers (tiers, backups, router) never offer an embedding model; the Embedder offers nothing else.
const aimFits = (m, capability = 'CHAT') => capability === 'EMBEDDING' ? m.model_type === 'EMBEDDING' : m.model_type !== 'EMBEDDING';
// One model as a picker option: its name, the provider model behind the alias, where it runs and its context.
function aimModelOpt(value, name, selected, meta) {
  const m = aimModel(name), sub = m?.underlying_model && m.underlying_model !== name ? m.underlying_model : '';
  return aimOpt(value, aimHostName(name), selected, { name, sub, host: m?.hosting ? m.hosting.toLowerCase() : '', meta: meta ?? (m?.context_window ? `${aimTokens(m.context_window)} ctx` : '') });
}
function aimModelOptions(selected, blank = 'Not set') {
  const names = aimModels().filter(m => m.enabled && aimFits(m)).map(m => m.name);
  if (selected && !names.includes(selected)) names.push(selected);
  return aimOpt('', blank, selected) + names.map(n => aimModelOpt(n, n, selected)).join('');
}
const aimPanel = (content, extra = '') => `<section class="card flush aim-panel ${extra}">${content}</section>`;

/* ── a worker's model: one choice instead of mode + model + tier ── */
// tier:NAME, model:NAME or auto (the auto router; its tier stays what it was, for while the router is off).
function aimTarget(c) {
  if (c?.mode === 'FIXED') return c.primary_model ? `model:${c.primary_model}` : '';
  if (c?.mode === 'AUTO_ROUTER') return 'auto';
  return c?.tier ? `tier:${c.tier}` : '';
}
function aimTargetOptions(selected, capability = 'CHAT') {
  const d = aimData(), embedding = capability === 'EMBEDDING';
  const tiers = embedding ? [] : Object.entries(d.tiers || {}), router = !embedding && d.routing?.auto_router_enabled;
  const models = aimModels().filter(m => m.enabled && aimFits(m, capability)).map(m => m.name);
  const [kind, name] = String(selected || '').split(/:(.*)/s);
  if (kind === 'model' && name && !models.includes(name)) models.push(name);
  if (embedding) return aimOpt('', 'Off (full-text retrieval only)', selected) + models.map(n => {
    const m = aimModel(n), size = m?.embedding_dimensions;
    return aimOpt(`model:${n}`, `${aimHostName(n)}${size ? ` · ${size} dims` : ' · size unknown'}`, selected, { name: n, host: m?.hosting ? m.hosting.toLowerCase() : '', meta: size ? `${size} dims` : 'size unknown' });
  }).join('');
  return aimOpt('', 'Choose a model', selected)
    + (tiers.length ? `<optgroup label="Tiers">${tiers.map(([t, x]) => aimOpt(`tier:${t}`, `${t}${x.model ? ` · ${aimHostName(x.model)}` : ''}`, selected, { name: t, sub: x.model ? `→ ${x.model}` : 'no model', host: aimModel(x.model)?.hosting?.toLowerCase() || '' })).join('')}</optgroup>` : '')
    + (models.length ? `<optgroup label="Models">${models.map(n => aimModelOpt(`model:${n}`, n, selected)).join('')}</optgroup>` : '')
    + (router || selected === 'auto' ? `<optgroup label="Auto router">${aimOpt('auto', `Auto router${router ? ` · ${d.routing.auto_router_model_name}` : ' (off)'}`, selected, { name: 'Auto router', sub: router ? `→ ${d.routing.auto_router_model_name}` : 'off' })}</optgroup>` : '');
}
// `c` is null for a worker added after the account's configuration was seeded (no row yet; saving creates it).
function aimTargetFields(target, c) {
  c = c || {};
  const [kind, name] = String(target || '').split(/:(.*)/s);
  if (kind === 'model') return { model_mode: 'FIXED', primary_model: name || null, tier_name: c.tier || null };
  if (kind === 'auto') return { model_mode: 'AUTO_ROUTER', primary_model: c.primary_model || null, tier_name: c.tier || null };
  return { model_mode: 'TIER', primary_model: c.primary_model || null, tier_name: name || null };
}
// The model a choice resolves to now, so the drawer offers only what that model supports.
function aimTargetModel(target) {
  const d = aimData(), [kind, name] = String(target || '').split(/:(.*)/s);
  if (kind === 'model') return name;
  if (kind === 'auto') return d.routing?.auto_router_enabled ? d.routing.auto_router_model_name : '';
  return d.tiers?.[name]?.model || '';
}
const aimBackups = c => [c?.fallback_model, ...(c?.secondary_models || [])].filter(Boolean);

/* ── loading and saving ── */
// Loading from the screen loader: render once at the end like every other loader.
async function aimLoadQuiet() {
  AIM.loading = true;
  try { AIM.data = await api('/api/settings/ai'); AIM.error = ''; }
  catch (err) { AIM.error = err.message; }
  finally { AIM.loading = false; }
}
async function aimLoad() {
  AIM.loading = true; AIM.error = '';
  try { AIM.data = await api('/api/settings/ai'); }
  catch (err) { AIM.error = err.message; }
  finally { AIM.loading = false; render(); }
}
// Worker insights (context used, best models, suggestions) load after the tab paints: undefined = not asked yet,
// null = could not be read. A failure only leaves the extras out, and keeps figures already shown.
async function aimLoadInsights() {
  if (AIM.insightsLoading) return;
  AIM.insightsLoading = true; AIM.insightsStale = false;
  try { AIM.insights = await api('/api/settings/ai/insights'); }
  catch { AIM.insights = AIM.insights || null; }
  finally { AIM.insightsLoading = false; render(); }
}
// A change that does not touch the versioned configuration (connection, tests, discovery).
async function aimRun(key, fn, ok) {
  return busy(key, async () => {
    try { const out = await fn(); await aimLoad(); return out; }
    catch (err) { if (err.status === 409) await aimLoad(); throw err; }
  }, ok);
}
// A validation issue as one line, naming what it is about ("Ranker: …") unless the message already does; repeats once.
const aimIssueText = i => !i.subject || i.message.startsWith(i.subject) ? i.message : `${i.subject}: ${i.message}`;
const aimIssues = list => { const seen = new Set(); return (list || []).map(i => ({ ...i, text: aimIssueText(i) })).filter(i => !seen.has(i.text) && seen.add(i.text)); };
// Where an issue's path is fixed: the tab, the worker drawer or the details to open, and the field to focus.
// Model paths end in a known field; the name before it may itself contain dots ("gpt-4.1").
const AIM_WORKER_FIELDS = { fallback_model: 'backup_0', escalation: 'esc_target', primary_model: 'target', tier: 'target', mode: 'target' };
const aimAttrSel = value => String(value).replace(/["\\]/g, '\\$&');
function aimFixTarget(path = '') {
  const [head, name, field, index] = path.split('.');
  if (head === 'workers' && name) {
    const id = field === 'secondary_models' ? `backup_${Number(index) + 1}` : AIM_WORKER_FIELDS[field] || field || 'target';
    const details = field === 'escalation' ? 'aim-escalation' : ['temperature', 'max_output_tokens', 'context_window', 'reasoning_effort', 'timeout_seconds', 'retry_count', 'max_concurrency'].includes(field) ? 'aim-generation' : null;
    return { tab: 'workers', worker: name, details, focus: `#aiw-${id}` };
  }
  if (head === 'models' && name) {
    const model = path.slice(7).replace(/\.(fallback_models\.\d+|rest_seconds|model_type|deployment_params)$/, '');
    return { tab: 'models', model, fallbacks: /\.fallback_models\.\d+$/.test(path), focus: `[data-model="${aimAttrSel(model)}"]` };
  }
  if (head === 'tiers' && name) return { tab: 'models', focus: `select[data-tier="${aimAttrSel(name)}"]` };
  if (head === 'routing') {
    const id = name === 'tier_models' ? `tier_${field}` : { auto_router_model_name: 'auto_router_model_name', default_model: 'default_model', classifier_model: 'classifier_model', classifier_plugin: 'classifier_plugin' }[name] || 'classifier';
    return { tab: 'models', details: 'aim-router', focus: `#aim-route-${id}` };
  }
  if (head === 'connection' || head === 'litellm') return { tab: 'settings', focus: '#aim-conn-base_url' };
  return null;
}
const aimFixLink = (i, text = 'Fix') => aimFixTarget(i.path) ? `<button type="button" class="aim-fix" data-act="aim-fix" data-path="${attr(i.path)}"${text === 'Fix' && i.subject ? ` aria-label="Fix ${attr(i.subject)}"` : ''}>${esc(text)}</button>` : '';
// Warnings with the same message once, naming each worker it is about, each a link to fix it.
function aimWarningLines(warnings) {
  const groups = new Map();
  for (const w of warnings) { const key = w.subject && !w.message.startsWith(w.subject) ? w.message : w.text; groups.set(key, [...(groups.get(key) || []), w]); }
  return [...groups].map(([message, list]) => list.length === 1 ? `<p class="settings-help">${esc(list[0].text)} ${aimFixLink(list[0])}</p>`
    : `<p class="settings-help">${esc(message)} — ${list.map(w => aimFixLink(w, w.subject) || esc(w.subject)).join(', ')}</p>`).join('');
}
// Apply whatever the draft holds now, as a new version. A refusal is kept on screen with Undo.
async function aimApplyNow() {
  await aimLoad();
  try {
    const out = await api('/api/settings/ai/apply', { method: 'POST', body: { revision: aimRev(), base_version: aimData().active_version } });
    AIM.failed = null; AIM.versions = null; AIM.insightsStale = true; await aimLoad();
    return out;
  } catch (err) {
    if (err.status === 409) { await aimLoad(); throw err; }
    AIM.failed = { errors: aimIssues(err.detail?.validation?.errors), warnings: aimIssues(err.detail?.validation?.warnings), message: err.message };
    if (!AIM.failed.errors.length) AIM.failed.errors = [{ message: err.message, text: err.message }];
    await aimLoad();
    throw new Error(`Not saved: ${AIM.failed.errors[0].text}`);
  }
}
const aimSaved = (ok, out) => out?.status === 'PENDING' ? `${ok} · v${out.version} — restart LiteLLM to load it` : out?.status === 'UNCHANGED' ? `${ok} · nothing changed` : `${ok} · v${out?.version}`;
// What disabling or deleting a model took it out of ("Ranker (backup 1)" → "Ranker"), for the toast.
const aimDropped = dropped => dropped?.length ? ` · no longer a backup for ${[...new Set(dropped.map(d => d.replace(/ \(backup \d+\)$/, '')))].join(', ')}` : '';
// A change to the configuration: write it, then apply it at once.
async function aimSave(key, write, ok = 'Saved') {
  return busy(key, async () => {
    try { await write(); }
    catch (err) { if (err.status === 409) await aimLoad(); throw err; }
    return aimApplyNow();
  }, out => aimSaved(typeof ok === 'function' ? ok(out) : ok, out));
}

/* ───────────── shell ───────────── */
function renderAIModels() {
  if (AIM.data === undefined && !AIM.loading && !AIM.error) { aimLoad(); return loading(); }
  if (AIM.error && !AIM.data) return `<section class="card"><div class="settings-error" role="alert">AI model settings could not be loaded: ${esc(AIM.error)}</div>${btn('Try again', 'aim-reload', {}, 'mt16')}</section>`;
  if (!AIM.data) return loading();
  if (typeof FO !== 'undefined' && FO.settings === null && !FO.loading && typeof foLoad === 'function') { FO.loading = true; foLoad(); }
  const d = aimData(), tab = AIM_TAB_ALIASES[AIM.tab] || (AIM_TABS.some(([id]) => id === AIM.tab) ? AIM.tab : 'workers');
  AIM.tab = tab;
  const body = { workers: aimWorkers, models: aimModelsTab, reliability: aimReliability, settings: aimSettings }[tab]();
  const resting = typeof FO !== 'undefined' ? FO.routes?.resting || 0 : 0;
  const counts = { workers: (d.workers || []).length, models: aimModels().filter(m => m.enabled).length, reliability: resting ? `${resting} resting` : null, settings: `v${d.active_version ?? 0}` };
  const nav = AIM_TABS.map(([id, name, sym, hint]) => `<button type="button" role="tab" aria-selected="${id === tab}" class="${id === tab ? 'on' : ''}" data-act="aim-tab" data-tab="${id}">
      <span class="aim-nav-sym" aria-hidden="true">${sym}</span><span class="grow"><b>${name}</b><small>${esc(hint)}</small></span>${counts[id] != null ? `<span class="aim-nav-count">${esc(String(counts[id]))}</span>` : ''}</button>`).join('');
  return `<div class="aim-ws">${aimStrip()}
    <div class="aim-ws-layout"><nav class="card aim-ws-nav" role="tablist" aria-label="Models and routing">${nav}</nav>
      <div class="aim-ws-main" role="tabpanel">${body}</div></div>${AIM.drawer ? aimDrawer() : ''}</div>`;
}
// One line of status: the route, LiteLLM, the active version, resting models; and what to do when a save was refused.
function aimStrip() {
  const d = aimData(), conn = d.connection || {}, [connLabel, connTone] = AIM_CONN[conn.status] || AIM_CONN.UNKNOWN;
  const pending = (d.pending_changes || []).length, resting = typeof FO !== 'undefined' ? FO.routes?.resting || 0 : 0;
  const f = AIM.failed;
  const notice = f ? `<div class="settings-error aim-failed" role="alert"><strong>Not saved.</strong>${f.errors.map(e => `<p>${esc(e.text)} ${aimFixLink(e)}</p>`).join('')}${aimWarningLines(f.warnings || [])}<div class="row gap8 mt8">${btn(isBusy('aim-discard') ? 'Undoing…' : 'Undo changes', 'aim-discard', {}, 'sm', isBusy('aim-discard'))}${btn('Try again', 'aim-apply', {}, 'sm', isBusy('aim-apply'))}</div></div>`
    : pending ? `<div class="settings-notice aim-failed" role="status">${pending} change${pending === 1 ? ' is' : 's are'} saved but not applied yet.<div class="row gap8 mt8">${btn(isBusy('aim-apply') ? 'Applying…' : 'Apply now', 'aim-apply', {}, 'primary sm', isBusy('aim-apply'))}${btn('Undo', 'aim-discard', {}, 'sm', isBusy('aim-discard'))}</div></div>` : '';
  return `<section class="card aim-hero"><div class="row between wrap gap10">
      <div style="min-width:min(100%,280px);flex:1"><div class="aim-hero-title">Models &amp; routing</div>
        <div class="card-sub">Which model each AI worker uses. Saving applies straight away; every save is a version you can roll back in Settings.</div></div>
      <div class="row gap6 wrap">${pill(d.gateway_enabled ? 'LiteLLM' : `Direct · ${d.gateway?.direct_label || d.llm_provider || 'provider'}`, d.gateway_enabled ? 'good' : 'muted')}${d.gateway_enabled ? pill(connLabel, connTone) : ''}${pill(`Active v${d.active_version ?? 0}`, 'brand')}${resting ? `<button type="button" class="pill bad" data-act="aim-tab" data-tab="reliability">${resting} model${resting === 1 ? '' : 's'} resting</button>` : ''}</div></div>
    ${d.gateway ? gatewayMissing(d.gateway) : ''}${notice}</section>`;
}

/* ───────────── workers ───────────── */
// Health of the model a worker uses, from the failover overlay: resting, probing, or fine.
function aimHealthState(w) {
  if (w.config?.enabled === false) return ['Off', 'muted', ''];
  const routes = (typeof FO !== 'undefined' && FO.routes?.routes) || [], model = w.resolved?.model;
  const route = routes.find(r => r.model === model && (!r.worker_type || r.worker_type === w.worker_type) && (r.resting || r.half_open));
  if (route?.resting) return ['Resting', 'bad', route.reason || ''];
  if (route?.half_open) return ['Recovering', 'warn', ''];
  return ['Healthy', 'good', ''];
}
function aimHealth(w) {
  const [label, tone, title] = aimHealthState(w);
  return `<span class="aim-health ${tone}"${title ? ` title="${attr(title)}"` : ''}><i aria-hidden="true"></i>${label}</span>`;
}
// A backup chain in the order it is tried: 1 → 2 → 3, each in its model's own colour.
// `shown` keeps a table row to one line: the first few, then "+2" naming the rest in its tooltip.
function aimBackupChain(backups, none = 'No backup', noun = 'Backup', shown = backups.length) {
  if (!backups.length) return `<span class="aim-wk-none">${esc(none)}</span>`;
  const rest = backups.slice(shown);
  return `<ol class="aim-chain">${backups.slice(0, shown).map((b, i) => `<li style="--h:${aimHue(b)}" title="${attr(`${noun} ${i + 1}: ${aimHostName(b)}`)}"><b>${i + 1}</b><span class="mono">${esc(b)}</span>${aimHostTag(b)}</li>`).join('')}${rest.length ? `<li class="aim-chain-more" title="${attr(rest.map((b, i) => `${noun} ${shown + i + 1}: ${aimHostName(b)}`).join('\n'))}">+${rest.length}</li>` : ''}</ol>`;
}
/* ── what a worker's own requests say: context used, best models, models worth trying (worker_insights) ── */
const aimInsight = type => AIM.insights?.workers?.[type] || null;
const aimPct = v => v == null ? 'unknown' : `${v}%`;
const aimMs = ms => ms == null ? null : ms < 1000 ? `${ms} ms` : `${Math.round(ms / 100) / 10} s`;
const AIM_BASIS = { OTHER_WORKERS: ['Your other workers', 'brand'], PLATFORM: ['Across accounts', 'good'], CATALOG: ['Catalog', 'muted'] };
// What stood in for a fact nobody reported: proof from the model's own answers, never an assumption.
const AIM_FLAGS = { CAPACITY_PROVEN: 'window not reported, but it has answered requests this large', JSON_PROVEN: 'JSON support not reported, but its answers were valid JSON' };
// Why the other models were not suggested, in the order the checks run (worker_insights gates 1–5).
function aimScreened(ins) {
  const r = ins.screened?.rejected || {}, n = ins.needs || {}, sum = (...keys) => keys.reduce((t, k) => t + (r[k] || 0), 0);
  const size = n.context_tokens ? `a ${aimTokens(n.context_tokens)}-token request` : 'its requests';
  return [[sum('CONTEXT_TOO_SMALL', 'CONTEXT_UNKNOWN', 'REFUSED_AS_TOO_LARGE'), `not shown to fit ${size}`], [sum('NO_STRUCTURED_OUTPUT', 'STRUCTURED_OUTPUT_UNKNOWN'), 'without proven JSON output'],
    [sum('LOW_SUCCESS'), `under ${AIM.insights.min_success_pct ?? 90}% success on comparable requests`], [sum('NOT_BETTER'), 'no better than the model in use'], [sum('NO_EVIDENCE'), 'with no requests of this size yet']]
    .filter(([count]) => count).map(([count, why]) => `${count} ${why}`);
}
// How much of its model's window a worker's requests fill, on average.
function aimContextMeter(ins) {
  const c = ins?.context;
  if (!c) return '';
  if (!c.avg_input_tokens) return '';  // nothing sent lately: say nothing here (the panel does)
  const pct = c.utilization_pct, tone = c.near_limit ? 'warn' : pct != null && pct >= 50 ? 'mid' : '';
  const title = `Average prompt ${c.avg_input_tokens.toLocaleString()} tokens over ${c.samples} answered requests in ${AIM.insights.window_days} days${c.p90_input_tokens ? ` · 9 in 10 are under ${c.p90_input_tokens.toLocaleString()}` : ''}${c.window ? ` · ${c.model || 'model'} window ${c.window.toLocaleString()}` : ' · window unknown'}${c.near_limit ? ' · the larger requests are close to the limit' : ''}`;
  // A bar only when the window is known: an empty bar says nothing.
  return `<div class="aim-ctx ${tone}" title="${attr(title)}">${pct == null ? '' : `<span class="aim-ctx-bar" aria-hidden="true"><i style="width:${Math.max(2, Math.min(100, pct))}%"></i></span>`}<small><b>${esc(aimTokens(c.avg_input_tokens))}</b> avg prompt${c.window ? ` · ${pct}% of ${esc(aimTokens(c.window))}` : ''}</small></div>`;
}
const aimStatsLine = st => [st.success_pct != null ? `${st.success_pct}% ok` : '', st.schema_valid_pct != null ? `JSON ${st.schema_valid_pct}%` : '', aimMs(st.p50_ms ?? st.avg_ms) ? `${aimMs(st.p50_ms ?? st.avg_ms)} typical` : '', st.samples ? `${st.samples} requests` : ''].filter(Boolean).join(' · ');
function aimSwitchButton(w, kind, index, item) {
  if (!item.apply) return '';
  const key = `aim-insight-${w.worker_type}`, chain = [item.apply.fallback_model, ...(item.apply.secondary_models || [])].filter(Boolean);
  const tier = w.config?.mode && w.config.mode !== 'FIXED' ? ` ${w.label} then uses this model directly instead of ${w.config.mode === 'TIER' ? `tier ${w.config.tier}` : 'the auto router'}.` : '';
  return `<div class="aim-ins-act"><button type="button" class="btn primary sm" data-act="aim-insight-apply" data-worker="${attr(w.worker_type)}" data-kind="${kind}" data-index="${index}" title="${attr(`Use ${item.model} for ${w.label} and update its backups, as one saved version.${tier}`)}" ${isBusy(key) ? 'disabled' : ''}>${isBusy(key) ? 'Switching…' : 'Switch'}</button><small>${chain.length ? `then ${chain.map(esc).join(' → ')}` : 'no backups'}</small></div>`;
}
function aimSuggestionWhy(sg) {
  const st = sg.stats || {};
  if (sg.basis === 'OTHER_WORKERS') return `On requests this size in ${esc((sg.workers || []).join(', '))}: ${esc(aimStatsLine(st))}`;
  if (sg.basis === 'PLATFORM') return `Across accounts on this server: ${esc(aimStatsLine(st))}`;
  return `Catalog score ${sg.catalog_score}${sg.current_catalog_score != null ? ` vs ${sg.current_catalog_score} for the current model` : ''} on price, speed and quality — not measured here yet`;
}
function aimInsightPanel(w, ins) {
  const days = AIM.insights.window_days, min = AIM.insights.min_samples, c = ins.context || {}, screened = aimScreened(ins);
  const best = (ins.best || []).map((b, i) => `<li><span class="aim-ins-rank" aria-hidden="true">${i + 1}</span><div class="aim-ins-body"><div class="aim-ins-name"><span class="mono b">${esc(b.model)}</span>${aimHostTag(b.model)}${b.is_current ? '<span class="pill brand">In use</span>' : ''}</div><small>${esc(aimStatsLine(b))}</small></div>${aimSwitchButton(w, 'best', i, b)}</li>`).join('');
  const tries = (ins.suggestions || []).map((sg, i) => { const [label, tone] = AIM_BASIS[sg.basis] || [sg.basis, 'muted']; const flags = (sg.flags || []).map(f => AIM_FLAGS[f]).filter(Boolean); return `<li><div class="aim-ins-body"><div class="aim-ins-name"><span class="mono b">${esc(sg.model)}</span>${aimHostTag(sg.model)}<span class="pill ${tone}">${label}</span></div><small>${aimSuggestionWhy(sg)}${flags.length ? `<span class="aim-ins-proof">${esc(flags.join(' · '))}</span>` : ''}</small></div>${aimSwitchButton(w, 'suggestions', i, sg)}</li>`; }).join('');
  const context = c.avg_input_tokens ? `<dl class="aim-ins-ctx"><div><dt>Average prompt</dt><dd>${esc(aimTokens(c.avg_input_tokens))}</dd></div><div><dt>9 in 10 under</dt><dd>${esc(aimTokens(c.p90_input_tokens))}</dd></div><div><dt>Window</dt><dd>${esc(aimTokens(c.window))}</dd></div><div><dt>Filled</dt><dd>${c.utilization_pct == null ? 'unknown' : `${c.utilization_pct}%`}</dd></div></dl>${c.near_limit ? '<p class="aim-ins-flag">▲ The larger requests are close to the window: a model with more context is safer.</p>' : ''}` : `<p class="aim-ins-empty">No answered requests in the last ${days} days.</p>`;
  return `<div class="aim-ins" id="aim-ins-${attr(w.worker_type)}">
    <section><h4>Context used</h4>${context}</section>
    <section><h4>Best so far</h4>${best ? `<ol class="aim-ins-list">${best}</ol>` : `<p class="aim-ins-empty">No model has ${min} requests from ${esc(w.label)} yet, so none is ranked.</p>`}</section>
    <section><h4>Worth trying</h4>${tries ? `<ul class="aim-ins-list">${tries}</ul>` : `<p class="aim-ins-empty">${w.capability === 'EMBEDDING' ? 'The embedder keeps one fixed model.' : 'Nothing looks better than what it uses now.'}</p>`}${screened.length ? `<p class="aim-ins-screened">${tries ? 'Not suggested' : `Checked ${ins.screened.considered} other model${ins.screened.considered === 1 ? '' : 's'}`}: ${esc(screened.join('; '))}.</p>` : ''}</section>
    <p class="aim-ins-foot">From ${ins.requests} of this account’s requests in the last ${days} days. A model is ranked from ${min} requests, and suggested only when it is shown to fit this worker’s requests, answers at least ${AIM.insights.min_success_pct ?? 90}% of comparable ones and beats the model in use. Switch saves the model and its backups together, and Settings → History can roll it back.</p></div>`;
}
// Suggestions backed by real requests (yours or across accounts). Catalog-only ideas stay inside the panel: they
// exist for nearly every worker, so flagging them on the row would flag every row.
const aimMeasured = ins => (ins?.suggestions || []).filter(x => x.basis !== 'CATALOG').length;
const AIM_WF = [['suggest', 'Has suggestion'], ['unhealthy', 'Not healthy'], ['near', 'Near context limit']];
function aimWorkers() {
  const d = aimData(), all = d.workers || [];
  // Stale after an applied change: the old figures stay on screen while the new ones load.
  if ((AIM.insights === undefined || AIM.insightsStale) && !AIM.insightsLoading) aimLoadInsights();
  const q = String(fv('aim-wf', 'q', '')).toLowerCase().split(/\s+/).filter(Boolean), on = fv('aim-wf', 'on', []);
  const facts = w => { const ins = aimInsight(w.worker_type); return { suggest: aimMeasured(ins) > 0, unhealthy: !['Healthy', 'Off'].includes(aimHealthState(w)[0]), near: !!ins?.context?.near_limit }; };
  const shown = all.filter(w => { const f = facts(w), hay = `${w.label} ${w.worker_type} ${w.description || ''} ${w.resolved?.model || ''} ${aimBackups(w.config).join(' ')}`.toLowerCase(); return q.every(x => hay.includes(x)) && on.every(k => f[k]); });
  const rows = shown.map(w => {
    const c = w.config || {}, test = AIM.tests[`worker:${w.worker_type}`], m = aimModel(w.resolved?.model), ins = aimInsight(w.worker_type);
    const short = m && m.context_window && m.context_window < w.min_context_tokens;
    const open = !!ins && !!AIM.insightOpen?.[w.worker_type], count = aimMeasured(ins);
    const toggle = ins ? `<button type="button" class="aim-ins-toggle ${count ? 'has' : ''} ${open ? 'on' : ''}" data-act="aim-insight-toggle" data-worker="${attr(w.worker_type)}" aria-expanded="${open}" aria-controls="aim-ins-${attr(w.worker_type)}" title="${attr(count ? `${count} model${count === 1 ? '' : 's'} did better elsewhere — see why` : 'Context used, best models and models worth trying')}" aria-label="${attr(`Insights for ${w.label}${count ? `, ${count} suggestion${count === 1 ? '' : 's'}` : ''}`)}">${count ? `<i aria-hidden="true">✦</i><span>${count}</span>` : '<span>Insights</span>'}<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></button>` : '';
    return `<tr class="${c.enabled === false ? 'off' : ''} ${open ? 'open' : ''}" data-worker-row="${attr(w.worker_type)}">
      <td><div class="aim-wk" style="--h:${aimHue(w.worker_type)}"><span class="aim-wk-mono" aria-hidden="true">${esc(aimMonogram(w.label))}</span><div class="aim-wk-name"><b>${esc(w.label)}</b>${w.has_prompt ? '' : '<span class="aim-wk-tag">reserved</span>'}<small title="${attr(w.description || '')}">${esc(w.description || '')}</small>${short ? `<small class="aim-wk-warn">▲ ${esc(m.name)} has less context than it needs</small>` : ''}</div></div></td>
      <td><select class="select aim-inline" aria-label="Model for ${attr(w.label)}" data-picker data-act-change="aim-worker-target" data-worker="${attr(w.worker_type)}">${aimTargetOptions(aimTarget(c?.enabled === false ? {} : c), w.capability)}</select>${aimContextMeter(ins)}</td>
      <td>${aimBackupChain(aimBackups(c), 'No backup', 'Backup', 2)}</td>
      <td>${aimHealth(w)}</td>
      <td class="aim-wk-act"><div>${toggle}${btn('Edit', 'aim-worker', { worker: w.worker_type }, 'sm ghost')}</div></td></tr>${open ? `<tr class="aim-wk-ins"><td colspan="5">${aimInsightPanel(w, ins)}</td></tr>` : ''}${test ? `<tr class="aim-wk-test"><td colspan="5">${aimTestResult(test)}</td></tr>` : ''}`;
  }).join('');
  const counts = Object.fromEntries(AIM_WF.map(([k]) => [k, all.filter(w => facts(w)[k]).length]));
  const chips = AIM_WF.filter(([k]) => counts[k] || on.includes(k)).map(([k, label]) => `<button type="button" class="aim-mf-chip ${on.includes(k) ? 'on' : ''}" data-act="aim-wf-chip" data-chip="${k}" aria-pressed="${on.includes(k)}">${label}<b>${counts[k]}</b></button>`).join('');
  const filtered = q.length || on.length;
  const bar = all.length > 1 ? `<div class="aim-mf aim-wf" role="search"><div class="aim-mf-top"><label class="aim-mf-search"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M10.5 10.5L14 14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg><input type="search" id="aim-wf-q" data-form="aim-wf" data-field="q" data-live="1" value="${attr(fv('aim-wf', 'q', ''))}" placeholder="Search ${all.length} workers by name or model" aria-label="Search workers" autocomplete="off" spellcheck="false"></label>${chips ? `<div class="aim-mf-group" role="group" aria-label="Show">${chips}</div>` : ''}${filtered ? `<span class="aim-wf-count" role="status">Showing <b>${shown.length}</b> of ${all.length}</span>${btn('Clear', 'aim-wf-clear', {}, 'sm ghost')}` : ''}</div></div>` : '';
  const direct = d.gateway_enabled ? '' : `<p class="settings-notice">Workers call ${esc(d.gateway?.direct_label || 'the direct provider')} directly right now (Settings). These assignments apply when the LiteLLM route is on.</p>`;
  const table = rows ? `<div class="aim-wk-wrap"><table class="aim-workers"><colgroup><col class="c-worker"><col class="c-model"><col class="c-backups"><col class="c-status"><col class="c-act"></colgroup><thead><tr><th>Worker</th><th>Model</th><th>Backups</th><th>Health</th><th><span class="sr-only">Actions</span></th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="settings-empty"><strong>No workers match</strong>${btn('Clear', 'aim-wf-clear', {}, 'sm')}</div>`;
  return aimRequestSize() + aimPanel(settingsGroup('Workers', 'Pick the model each worker uses. Backups take over when it is rate limited or failing.', `${direct}${bar}${table}`));
}
// Requests a provider refused as larger than its per-minute limit (README: Model failover). Waiting never helps them,
// so this shows the provider's own limits, this worker's request sizes, and the fixes that fit: a cap on answers when
// they fit under it, or a model with more room. A person applies a fix; nothing changes on its own.
const AIM_COUNTS = { total: 'prompt + answer', input: 'prompt', output: 'answer' };
const AIM_SIZE_SHOWN = 2;  // suggested models shown before "N more models"
// The limit these requests overrun the most: what it counts (prompt, answer or both) against the limit itself. A limit
// that counts the prompt comes first, so the bar shows the whole request; an answer-only limit is drawn only alone.
function aimSizeBinding(g) {
  const inp = g.typical_input, out = g.typical_output;
  const counted = l => l.counts === 'input' ? inp : l.counts === 'output' ? out : inp != null ? inp + (out || 0) : null;
  return g.limits.map(l => ({ ...l, used: counted(l) })).filter(l => l.used != null && l.limit > 0)
    .sort((a, b) => (a.counts === 'output') - (b.counts === 'output') || b.used / b.limit - a.used / a.limit)[0] || null;
}
// A bar of this worker's typical request against the binding limit; unknown sizes draw nothing.
function aimSizeMeter(g) {
  const b = aimSizeBinding(g);
  if (!b) return '';
  const inp = b.counts === 'output' ? 0 : g.typical_input || 0, out = b.counts === 'input' ? 0 : g.typical_output || 0;
  const scale = Math.max(b.limit, inp + out) || 1, pct = n => `${Math.round(n / scale * 1000) / 10}%`;
  const parts = [b.counts !== 'output' ? `Prompt <b>~${esc(aimTokens(g.typical_input))}</b>` : '', b.counts !== 'input' && g.typical_output != null ? `Answer <b>~${esc(aimTokens(g.typical_output))}</b>` : '',
    `Limit <b>${esc(aimTokens(b.limit))}</b> ${esc(AIM_COUNTS[b.counts] || b.counts)}/min on <span class="mono">${esc(b.deployment)}</span>`].filter(Boolean);
  return `<div class="aim-size-meter" role="img" aria-label="${attr(`Typical request ${aimTokens(inp + out)} tokens against a limit of ${aimTokens(b.limit)} ${AIM_COUNTS[b.counts] || b.counts} tokens per minute`)}">
      <div class="aim-size-bar">${inp ? `<span class="in" style="width:${pct(inp)}"></span>` : ''}${out ? `<span class="out" style="width:${pct(out)}"></span>` : ''}<i style="left:${pct(b.limit)}" title="${attr(`Limit ${aimTokens(b.limit)}/min`)}"></i></div>
      <p class="aim-size-legend">${parts.join('<span aria-hidden="true">·</span>')}<span class="muted">9 in 10 requests are smaller</span></p></div>`;
}
function aimSizeLimits(g) {
  const rows = g.limits.map(l => `<tr><td class="mono">${esc(l.deployment)}</td><td>${esc(aimTokens(l.limit))} ${esc(AIM_COUNTS[l.counts] || l.counts)} tokens/min (${esc(l.kind)})</td></tr>`).join('');
  return rows ? settingsDetails(`aim-size-limits:${g.worker_type}:${g.model}`, `${esc(g.model)}'s limits (${g.limits.length})`, `<table class="aim-size-limits"><tbody>${rows}</tbody></table>`) : '';
}
function aimSizeVerdict(g) {
  const cap = g.output_cap, box = (tone, head, why, act = '') => `<div class="aim-size-verdict ${tone}"><div><b>${head}</b><p>${why}</p></div>${act}</div>`;
  if (cap?.applied) return box('good', `✓ Answers are capped at ${esc(aimTokens(cap.current))} tokens`, 'Requests like these now fit.');
  if (cap) return box('brand', `Cap answers at ${esc(aimTokens(cap.value))} tokens`, `These requests then fit. ${cap.known_safe ? `This worker's answers are about ${esc(aimTokens(cap.typical_output))}, so they are not cut short.` : 'This worker has no answers on record yet, so check that a cap this size does not cut them short.'}`,
    btn(isBusy('aim-size') ? 'Saving…' : `Cap answers at ${aimTokens(cap.value)}`, 'aim-size-cap', { worker: g.worker_type, value: cap.value }, 'primary sm', isBusy('aim-size')));
  return box('warn', 'Switch to a model with more room', g.input_over ? `The prompt alone${g.typical_input != null ? ` (~${esc(aimTokens(g.typical_input))})` : ''} is over ${esc(g.model)}'s limit, so capping answers cannot help.`
    : `Prompt and answer together are more than ${esc(g.model)} accepts in a minute, and a smaller answer cap would cut this worker's answers short.`);
}
function aimSizeModels(g, backups) {
  if (!g.models.length) return `<div class="aim-size-none"><span>No model in your catalog is known to have room for these requests.</span>${btn('Add one in Models', 'aim-tab', { tab: 'models' }, 'ghost sm')}</div>`;
  const busy = isBusy('aim-size'), primary = g.output_cap && !g.output_cap.applied ? '' : 'primary';
  const row = (m, i) => {
    const use = `Use ${m.name} instead of ${g.model} for ${g.label}`;
    const tags = [aimHostTag(m.name), i === 0 ? '<span class="pill brand">Recommended</span>' : '', m.proven ? '<span class="pill good">Proven for this worker</span>' : '',
      `<span class="xs muted">${m.context_window ? `${esc(aimTokens(m.context_window))} context` : 'context unknown'}</span>`].join('');
    const backup = m.in_chain ? '<span class="pill muted" title="Already in this worker\'s backups">Backup</span>'
      : backups.length < AIM_BACKUPS ? btn('Add as backup', 'aim-size-backup', { worker: g.worker_type, model: m.name }, 'ghost sm', busy) : '';
    return `<li class="aim-size-model"><div class="aim-size-model-name"><span class="aim-size-dot" style="--h:${aimHue(m.name)}" aria-hidden="true"></span><span class="mono" title="${attr(m.name)}">${esc(m.name)}</span>${tags}</div>
      <div class="aim-size-model-act">${backup}<button class="btn sm ${i === 0 ? primary : ''}" data-act="aim-size-use" data-worker="${attr(g.worker_type)}" data-model="${attr(m.name)}" data-replace="${attr(g.model)}" data-role="${attr(g.role || 'primary')}" aria-label="${attr(use)}" title="${attr(use)}" ${busy ? 'disabled' : ''}>Switch</button></div></li>`;
  };
  const shown = g.models.slice(0, AIM_SIZE_SHOWN).map(row).join(''), rest = g.models.slice(AIM_SIZE_SHOWN);
  return `<h4 class="aim-size-h">Models with room for these requests</h4><ul class="aim-size-models">${shown}</ul>${rest.length ? settingsDetails(`aim-size-more:${g.worker_type}:${g.model}`, `${rest.length} more model${rest.length === 1 ? '' : 's'}`, `<ul class="aim-size-models">${rest.map((m, i) => row(m, i + AIM_SIZE_SHOWN)).join('')}</ul>`) : ''}`;
}
function aimRequestSize() {
  const groups = aimData().request_size || [];
  if (!groups.length) return '';
  const card = g => {
    const backups = aimBackups(aimWorkerByType(g.worker_type)?.config || {});
    const role = g.role && g.role !== 'primary' ? ` <span class="pill muted">its ${esc(g.role === 'backup' ? 'backup' : 'escalation model')}</span>` : '';
    return `<article class="aim-size" style="--h:${aimHue(g.worker_type)}"><header class="aim-size-head"><span class="aim-wk-mono" aria-hidden="true">${esc(aimMonogram(g.label))}</span>
        <div class="aim-size-title"><b>${esc(g.label)}</b> <span aria-hidden="true">→</span><span class="sr-only">sends to</span> <span class="mono">${esc(g.model)}</span>${role}</div>
        <div class="aim-size-when"><span class="pill bad">${g.count} request${g.count === 1 ? '' : 's'} refused as too large</span><span class="xs muted">last ${esc(fmtDateTime(g.last_at))}</span></div></header>
      ${aimSizeMeter(g)}${aimSizeVerdict(g)}${aimSizeModels(g, backups)}${aimSizeLimits(g)}</article>`;
  };
  const total = groups.reduce((n, g) => n + g.count, 0), workers = new Set(groups.map(g => g.worker_type)).size;
  return aimPanel(settingsGroup('Requests too big for their model', `Last 7 days · ${workers} worker${workers === 1 ? '' : 's'} · ${total} request${total === 1 ? '' : 's'} refused. Each was over the provider's per-minute token limit, so waiting does not help and the model is not rested for them.`, `<div class="aim-size-list">${groups.map(card).join('')}</div>`), 'aim-size-panel');
}
function aimTestResult(t) {
  if (t.error && !t.success) return `<div class="settings-error aim-test">${esc(t.error)}</div>`;
  const r = t.routing || {};
  const bits = [`model ${t.answered_by || t.underlying_model || t.model || '?'}`, r.model_group ? `group ${r.model_group}` : '', r.routing_tier ? `tier ${r.routing_tier}` : '', t.latency_ms != null ? `${t.latency_ms} ms` : '', t.input_tokens != null ? `${t.input_tokens}+${t.output_tokens ?? 0} tokens` : '', t.schema_ok === true || t.structured_output === true ? 'schema ✓' : t.schema_ok === false || t.structured_output === false ? 'schema ✗' : '', r.escalated ? `escalated (${r.escalation_reason})` : ''].filter(Boolean);
  return `<div class="settings-help aim-test" role="status">${t.success ? '✓' : '✗'} ${esc(bits.join(' · '))}</div>`;
}

// A provider error's short name for a tight spot: litellm.PaymentRequiredError → "Payment required".
function aimErrorHeadline(error) {
  const m = String(error || '').match(/\b([A-Z][A-Za-z]*?)(?:Error|Exception)\b/);
  if (!m || m[1].length < 3) return 'Test failed';
  const words = m[1].replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2').replace(/([a-z])([A-Z])/g, '$1 $2');
  return words.charAt(0) + words.slice(1).replace(/\b([A-Z][a-z]+)/g, w => w.toLowerCase());
}
// A model card's test result in one line: a failure's full text is a tooltip, not a block that stretches the card.
function aimModelTestLine(t) {
  if (!(t.error && !t.success)) return aimTestResult(t);
  const head = aimErrorHeadline(t.error);
  return `<button type="button" class="aim-mcard-err" data-tip="${attr(t.error)}" aria-label="${attr(`Test failed: ${t.error}`)}"><i aria-hidden="true">✗</i><span>Test failed</span><b>${esc(head === 'Test failed' ? 'hover for details' : head)}</b><em aria-hidden="true">ⓘ</em></button>`;
}

/* ── the worker drawer: model, backups, on/off; generation and escalation folded away ── */
function aimWorkerBaseline(w) {
  const c = w.config || {}, e = c.escalation || {}, backups = aimBackups(c);
  // An embedding worker is on exactly while it has a model: switched off, it shows Off whatever model it last had.
  const target = w.capability === 'EMBEDDING' && c.enabled === false ? '' : aimTarget(c);
  return { target, ...Object.fromEntries(Array.from({ length: AIM_BACKUPS }, (_, i) => [`backup_${i}`, backups[i] || ''])), temperature: c.temperature ?? '', max_output_tokens: c.max_output_tokens ?? '', context_window: c.context_window ?? '', prompt_token_budget: c.prompt_token_budget ?? '', reasoning_effort: c.reasoning_effort || '', timeout_seconds: c.timeout_seconds ?? '', retry_count: c.retry_count ?? '', max_concurrency: c.max_concurrency ?? '', use_structured_output: c.use_structured_output !== false, enabled: c.enabled !== false, esc_target: e.model ? `model:${e.model}` : e.tier ? `tier:${e.tier}` : '', esc_threshold: e.confidence_threshold ?? '', ...Object.fromEntries(Object.keys(AIM_CONDITIONS).map(k => [`esc_${k}`, (e.conditions || []).includes(k)])) };
}
function aimDrawerValues(w) { const base = aimWorkerBaseline(w); return Object.fromEntries(Object.keys(base).map(k => [k, fv('aiw', k, base[k])])); }
const aimDrawerBackups = v => Array.from({ length: AIM_BACKUPS }, (_, i) => v[`backup_${i}`]).filter(Boolean);
function aimWarnings(w, v) {
  const m = aimModel(aimTargetModel(v.target)), out = [];
  if (!m) return out;
  if (w.structured_output_required && m.supports_structured_output === false) out.push(`${m.name} cannot return structured output, which ${w.label} requires.`);
  else if (w.structured_output_required && m.supports_structured_output == null && m.model_type !== 'AUTO_ROUTER') out.push(`Structured Output Required: it is unknown whether ${m.name} returns JSON reliably.`);
  if (m.context_window && m.context_window < w.min_context_tokens) out.push(`This model may not satisfy ${w.label} requirements (${aimTokens(m.context_window)} context; ${aimTokens(w.min_context_tokens)} recommended).`);
  if (v.context_window && m.context_window && Number(v.context_window) > m.context_window) out.push(`Context ${aimTokens(Number(v.context_window))} exceeds the ${aimTokens(m.context_window)} ${m.name} runs with.`);
  const windowSize = Number(v.context_window) || m.context_window;
  if (w.trims_prompt && v.prompt_token_budget && windowSize && Number(v.prompt_token_budget) >= windowSize) out.push(`A ${aimTokens(Number(v.prompt_token_budget))} prompt budget is no smaller than the ${aimTokens(windowSize)} context, so it never trims.`);
  if (aimDrawerBackups(v).includes(m.name)) out.push('A backup cannot be the model itself.');
  return out;
}
// The worker editor's frame: who it is, what it needs, and a footer that stays in reach while the body scrolls.
function aimEditorHead(w, tags = '') {
  const [health, tone] = aimHealthState(w);
  return `<header class="aim-ed-head"><span class="aim-wk-mono" style="--h:${aimHue(w.worker_type)}" aria-hidden="true">${esc(aimMonogram(w.label))}</span>
      <div class="aim-ed-title"><h3 id="aim-drawer-title">${esc(w.label)}</h3><p>${esc(w.description || '')}</p></div>
      <button type="button" class="aim-ed-close" data-act="aim-drawer-close" aria-label="Close" title="Close"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg></button></header>
    <div class="aim-ed-tags"><span class="aim-health ${tone}"><i aria-hidden="true"></i>${health}</span>${tags}</div>`;
}
function aimEditorFoot(w) {
  const test = AIM.tests[`worker:${w.worker_type}`];
  return `<footer class="aim-ed-foot">${test ? aimTestResult(test) : ''}<div class="aim-ed-actions">${btn(isBusy(`aim-wtest-${w.worker_type}`) ? 'Testing…' : 'Test', 'aim-worker-test', { worker: w.worker_type }, '', isBusy(`aim-wtest-${w.worker_type}`))}${btn(isBusy('aim-worker-save') ? 'Saving…' : 'Save', 'aim-worker-save', { worker: w.worker_type }, 'primary', isBusy('aim-worker-save'))}</div></footer>`;
}
const aimEditorSection = (title, hint, body, extra = '') => `<section class="aim-ed-sec ${extra}"><div class="aim-ed-sec-head"><h4>${title}</h4>${hint ? `<span>${hint}</span>` : ''}</div>${body}</section>`;
function aimDrawer() {
  const w = (aimData().workers || []).find(x => x.worker_type === AIM.drawer);
  if (!w) return '';
  if (w.capability === 'EMBEDDING') return aimEmbedderDrawer(w);
  const v = aimDrawerValues(w), model = aimModel(aimTargetModel(v.target)), d = aimData(), warnings = aimWarnings(w, v);
  const f = (key, label, extra = '') => `<label class="field" for="aiw-${key}"><span>${label}</span>${inputF('aiw', key, v[key], extra)}</label>`;
  // Filled backups plus one empty slot, so the list grows as you fill it.
  const shown = Math.min(AIM_BACKUPS, aimDrawerBackups(v).length + 1);
  const filled = aimDrawerBackups(v).length;
  const backups = Array.from({ length: shown }, (_, i) => `<li class="${v[`backup_${i}`] ? '' : 'slot'}"><b aria-hidden="true">${v[`backup_${i}`] ? i + 1 : '+'}</b><label class="field" for="aiw-backup_${i}"><span class="sr-only">Backup ${i + 1}</span><select class="select" id="aiw-backup_${i}" data-form="aiw" data-field="backup_${i}" data-live="1" data-picker>${aimModelOptions(v[`backup_${i}`], v[`backup_${i}`] ? 'None (remove)' : filled ? 'Add another backup' : 'Add a backup')}</select></label></li>`).join('');
  const router = v.target === 'auto' ? `<p class="settings-help">Auto router: ${d.routing?.auto_router_enabled ? `requests go to <span class="mono">${esc(d.routing.auto_router_model_name)}</span>` : `off — tier ${esc(w.config?.tier || '—')} is used until it is on (Models tab)`}.</p>` : '';
  const reasoning = model?.supports_reasoning === true ? `<label class="field" for="aiw-reasoning_effort"><span>Reasoning effort</span><select class="select" id="aiw-reasoning_effort" data-form="aiw" data-field="reasoning_effort">${aimOpt('', 'Model default', v.reasoning_effort)}${['low', 'medium', 'high'].map(x => aimOpt(x, x.toUpperCase(), v.reasoning_effort)).join('')}</select></label>` : `<p class="settings-help">Reasoning effort: ${model ? (model.supports_reasoning === false ? `${esc(model.name)} does not support it.` : `unknown whether ${esc(model.name)} supports it, so it is not offered.`) : 'choose a model first.'}</p>`;
  const conditions = Object.entries(AIM_CONDITIONS).map(([k, label]) => checkF('aiw', `esc_${k}`, v[`esc_${k}`], label)).join('');
  const escTargets = aimOpt('', 'No escalation', v.esc_target) + Object.entries(d.tiers || {}).map(([t, x]) => aimOpt(`tier:${t}`, `Tier ${t}`, v.esc_target, { name: `Tier ${t}`, sub: x.model ? `→ ${x.model}` : '' })).join('') + aimModels().filter(m => m.enabled).map(m => aimModelOpt(`model:${m.name}`, m.name, v.esc_target)).join('');
  const tags = `${w.structured_output_required ? '<span class="pill brand" title="Its answers are parsed as typed JSON, so the model must return JSON reliably">Structured Output Required</span>' : ''}<span class="pill plain" title="The smallest context window a model should give this worker">needs ${esc(aimTokens(w.min_context_tokens))} context</span>`;
  const enabled = `<label class="aim-switch" for="aiw-enabled"><input type="checkbox" id="aiw-enabled" data-form="aiw" data-field="enabled" ${v.enabled ? 'checked' : ''}><i aria-hidden="true"></i><span><b>Worker enabled</b><small>Off: ${esc(w.label)} sends nothing to any model.</small></span></label>`;
  return `<aside class="aim-drawer aim-ed" role="dialog" aria-modal="false" aria-labelledby="aim-drawer-title">${aimEditorHead(w, tags)}
    <div class="aim-ed-body">
    ${warnings.length ? `<div class="settings-notice" role="status">${warnings.map(x => `<p>${esc(x)}</p>`).join('')}</div>` : ''}
    ${aimEditorSection('Model', 'Every request goes here first', `<label class="field" for="aiw-target"><span class="sr-only">Model</span><select class="select" id="aiw-target" data-form="aiw" data-field="target" data-live="1" data-picker>${aimTargetOptions(v.target)}</select></label>${aimContextMeter(aimInsight(w.worker_type))}${router}`)}
    ${aimEditorSection('Backups', 'Tried in this order', `<ol class="aim-ed-chain">${backups}</ol><p class="settings-help">Backups are tried in order when the model is rate limited, failing or down. Reliability decides when.</p>`)}
    ${enabled}
    ${settingsDetails('aim-generation', 'Generation settings', `<div class="settings-grid">${f('temperature', 'Temperature', 'type="number" min="0" max="2" step="0.1" placeholder="Model default"')}${f('max_output_tokens', 'Max output tokens', `type="number" min="1" max="${model?.max_output_tokens || 200000}" placeholder="Model default"`)}${f('context_window', 'Context window (tokens)', `type="number" min="512" placeholder="${attr(model?.context_window ? `Model: ${model.context_window}` : 'Model default')}" data-live="1"`)}${w.trims_prompt ? f('prompt_token_budget', 'Prompt token budget', 'type="number" min="256" placeholder="No trimming" data-live="1"') : ''}${reasoning}${f('timeout_seconds', 'Timeout (seconds)', 'type="number" min="1" max="3600" placeholder="Server default"')}${f('retry_count', 'Retries', 'type="number" min="0" max="10" placeholder="Server default"')}${f('max_concurrency', 'Maximum concurrency', 'type="number" min="1" max="100" placeholder="No limit"')}</div><div class="settings-checks">${checkF('aiw', 'use_structured_output', v.use_structured_output, 'Structured output (JSON mode)')}</div>${w.trims_prompt ? '<p class="settings-help">Prompt token budget: over it, the least relevant job details and evidence are left out of the prompt, in a fixed order, until it fits (for providers that refuse large requests, such as a per-minute token limit). Required requirements are never left out. Blank sends the whole prompt.</p>' : ''}`)}
    ${settingsDetails('aim-escalation', 'Quality escalation', `<p class="settings-help">When an answer is not good enough, ask a stronger model once. Separate from backups, which handle outages.</p><div class="settings-grid"><label class="field" for="aiw-esc_target"><span>Escalate to</span><select class="select" id="aiw-esc_target" data-form="aiw" data-field="esc_target" data-picker>${escTargets}</select></label>${f('esc_threshold', 'Confidence threshold', 'type="number" min="0" max="1" step="0.05" placeholder="e.g. 0.85"')}</div><div class="settings-checks">${conditions}</div>`)}
    </div>${aimEditorFoot(w)}</aside>`;
}
// An embedding worker (the Embedder): one fixed EMBEDDING model or Off. No tiers, backups, generation settings or
// escalation — vectors of another model cannot be compared, and Apply refuses them anyway.
function aimEmbedderDrawer(w) {
  const v = aimDrawerValues(w), m = aimModel(aimTargetModel(v.target));
  const available = aimModels().some(x => x.enabled && aimFits(x, 'EMBEDDING'));
  const size = m ? (m.embedding_dimensions ? `${m.embedding_dimensions} dimensions` : 'unknown — press Test on the model in Models, or set its vector size there') : '';
  return `<aside class="aim-drawer aim-ed" role="dialog" aria-modal="false" aria-labelledby="aim-drawer-title">${aimEditorHead(w, '<span class="pill plain">Embedding model only</span>')}
    <div class="aim-ed-body">
    ${available ? '' : '<div class="settings-notice" role="status"><p>No embedding models yet. Sync from LiteLLM in Models, and set a model\'s type to Embedding if discovery did not.</p></div>'}
    ${aimEditorSection('Model', 'One fixed model, or Off', `<label class="field" for="aiw-target"><span class="sr-only">Model</span><select class="select" id="aiw-target" data-form="aiw" data-field="target" data-live="1" data-picker>${aimTargetOptions(v.target, 'EMBEDDING')}</select></label>${m ? `<p class="settings-help">Vector size: ${esc(size)}</p>` : ''}`)}
    <p class="settings-help">One fixed embedding model, with no backups, generation settings or escalation: vectors of another model cannot be compared. Off keeps retrieval on keyword and full-text search. A new model is indexed beside the current one and takes over once it covers everything.</p>
    </div>${aimEditorFoot(w)}</aside>`;
}
function aimWorkerPayload(w) {
  if (w.capability === 'EMBEDDING') {
    const [kind, name] = String(aimDrawerValues(w).target || '').split(/:(.*)/s), model = kind === 'model' ? name : null;
    return { revision: aimRev(), model_mode: 'FIXED', primary_model: model, tier_name: null, fallback_model: null, secondary_models: [], escalation: null, enabled: !!model };
  }
  const v = aimDrawerValues(w), num = x => x === '' || x == null ? null : Number(x);
  const [kind, name] = String(v.esc_target || '').split(/:(.*)/s);
  const conditions = Object.keys(AIM_CONDITIONS).filter(k => v[`esc_${k}`]);
  const backups = [...new Set(aimDrawerBackups(v))];
  return { revision: aimRev(), ...aimTargetFields(v.target, w.config), fallback_model: backups[0] || null, secondary_models: backups.slice(1), temperature: num(v.temperature), max_output_tokens: num(v.max_output_tokens), context_window: num(v.context_window), prompt_token_budget: num(v.prompt_token_budget), reasoning_effort: v.reasoning_effort || null, timeout_seconds: num(v.timeout_seconds), retry_count: num(v.retry_count), max_concurrency: num(v.max_concurrency), use_structured_output: !!v.use_structured_output, enabled: !!v.enabled,
    escalation: kind && conditions.length ? { model: kind === 'model' ? name : null, tier: kind === 'tier' ? name : null, conditions, confidence_threshold: num(v.esc_threshold) } : null };
}

/* ───────────── models: catalog, tiers, auto router ───────────── */
// The catalog's search and filters. Within a group any chip may match; across groups all must.
const AIM_MF = {
  health: ['Health', [['HEALTHY', 'Healthy', m => m.health_status === 'HEALTHY'], ['UNHEALTHY', 'Unhealthy', m => m.health_status === 'UNHEALTHY'], ['UNTESTED', 'Not tested', m => !AIM_HEALTH[m.health_status]]]],
  host: ['Runs', [['LOCAL', 'Local', m => m.hosting === 'LOCAL'], ['CLOUD', 'Cloud', m => m.hosting === 'CLOUD'], ['MIXED', 'Local + Cloud', m => m.hosting === 'MIXED']]],
  use: ['Use', [['USED', 'In use', m => (m.used_by || []).length > 0], ['UNUSED', 'Unused', m => !(m.used_by || []).length], ['OFF', 'Disabled', m => !m.enabled]]],
};
const AIM_MF_SORT = { catalog: ['Catalog order', () => 0], name: ['Name', (a, b) => a.name.localeCompare(b.name)], health: ['Health', (a, b) => aimHealthRank(a) - aimHealthRank(b) || a.name.localeCompare(b.name)],
  used: ['Most used', (a, b) => (b.used_by || []).length - (a.used_by || []).length || a.name.localeCompare(b.name)],
  latency: ['Fastest', (a, b) => (a.health_latency_ms ?? Infinity) - (b.health_latency_ms ?? Infinity) || a.name.localeCompare(b.name)] };
const aimHealthRank = m => ({ UNHEALTHY: 0, HEALTHY: 2 })[m.health_status] ?? 1;  // problems first
const aimMfOn = () => fv('aim-mf', 'on', []);
function aimModelMatches(m, q, on, labels) {
  const hay = [m.name, m.provider, m.underlying_model, m.model_type, AIM_HOSTING[m.hosting], ...(m.used_by || []).map(u => labels[u] || u)].filter(Boolean).join(' ').toLowerCase();
  if (!String(q || '').toLowerCase().split(/\s+/).filter(Boolean).every(w => hay.includes(w))) return false;
  return Object.entries(AIM_MF).every(([g, [, chips]]) => {
    const picked = chips.filter(([id]) => on.includes(`${g}:${id}`));
    return !picked.length || picked.some(([, , test]) => test(m));
  });
}
function aimModelFilters(all, shown, labels) {
  const on = aimMfOn(), q = fv('aim-mf', 'q', ''), sort = fv('aim-mf', 'sort', 'catalog');
  const groups = Object.entries(AIM_MF).map(([g, [title, chips]]) => {
    const present = chips.map(([id, label, test]) => [id, label, all.filter(test).length]).filter(([id, , n]) => n || on.includes(`${g}:${id}`));
    if (present.length < 2 && !present.some(([id]) => on.includes(`${g}:${id}`))) return '';
    return `<div class="aim-mf-group" role="group" aria-label="${attr(title)}"><span class="aim-mf-title">${esc(title)}</span>${present.map(([id, label, n]) => {
      const pressed = on.includes(`${g}:${id}`);
      return `<button type="button" class="aim-mf-chip ${pressed ? 'on' : ''} ${g === 'health' ? id.toLowerCase() : ''}" data-act="aim-mf-chip" data-chip="${attr(`${g}:${id}`)}" aria-pressed="${pressed}">${esc(label)}<b>${n}</b></button>`;
    }).join('')}</div>`;
  }).join('');
  const filtered = q || on.length;
  return `<div class="aim-mf" role="search">
    <div class="aim-mf-top"><label class="aim-mf-search"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M10.5 10.5L14 14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg><input type="search" id="aim-mf-q" data-form="aim-mf" data-field="q" data-live="1" value="${attr(q)}" placeholder="Search ${all.length} models by name, provider or worker" aria-label="Search models" autocomplete="off" spellcheck="false"></label>
      <label class="aim-mf-sort"><span>Sort</span><select class="select" id="aim-mf-sort" data-form="aim-mf" data-field="sort" data-live="1">${Object.entries(AIM_MF_SORT).map(([k, [t]]) => aimOpt(k, t, sort)).join('')}</select></label></div>
    ${groups ? `<div class="aim-mf-chips">${groups}</div>` : ''}
    ${filtered ? `<div class="aim-mf-status" role="status"><span>Showing <b>${shown}</b> of ${all.length}</span>${btn('Clear filters', 'aim-mf-clear', {}, 'sm ghost')}</div>` : ''}</div>`;
}
function aimModelsTab() {
  const d = aimData(), labels = Object.fromEntries((d.workers || []).map(w => [w.worker_type, w.label]));
  const all = aimModels(), q = fv('aim-mf', 'q', ''), on = aimMfOn();
  const list = all.filter(m => aimModelMatches(m, q, on, labels)).sort((AIM_MF_SORT[fv('aim-mf', 'sort', 'catalog')] || AIM_MF_SORT.catalog)[1]);
  const cards = list.map(m => aimModelCard(m, labels, d)).join('');
  const none = all.length && !list.length ? `<section class="card aim-mf-none"><div class="settings-empty"><strong>No models match</strong><p>${q ? `Nothing matches “${esc(q)}”${on.length ? ' with these filters' : ''}.` : 'No model fits these filters.'}</p>${btn('Clear filters', 'aim-mf-clear', {}, 'sm')}</div></section>` : '';
  return `<div class="aim-section-head row between wrap gap10"><div><div class="card-title">Models</div><div class="card-sub">What LiteLLM serves. Values LiteLLM or Ollama do not report stay unknown.</div></div>
      <div class="row gap8">${btn(isBusy('aim-sync') ? 'Syncing…' : 'Sync from LiteLLM', 'aim-sync', {}, 'primary sm', isBusy('aim-sync'))}${btn(AIM.adding ? 'Cancel' : 'Add local Ollama model', 'aim-add-toggle', {}, 'sm')}</div></div>
    ${AIM.adding ? aimPanel(aimAddForm()) : ''}${all.length > 1 ? aimModelFilters(all, list.length, labels) : ''}${none}${cards ? `<div class="aim-grid">${cards}</div>` : none ? '' : `<section class="card"><div class="settings-empty"><strong>No models yet</strong><p>Connect LiteLLM in Settings, then sync to discover its models.</p></div></section>`}
    ${aimRemoved()}${aimTiers()}`;
}
/* ── a model card: who it is, how it is doing, what it can do, who uses it ── */
// A model's full name, never cut: the provider path ("groq/qwen/") dimmed before the model itself, with break points
// after "/", ":" and "-" so a long name wraps at a sensible place.
function aimModelName(name) {
  const text = String(name || ''), cut = text.lastIndexOf('/') + 1;
  const soft = part => esc(part).replace(/([/:\-])/g, '$1<wbr>');
  return `${cut ? `<span class="aim-mcard-path">${soft(text.slice(0, cut))}</span>` : ''}${soft(text.slice(cut))}`;
}
// A stable colour per model, so the same model looks the same everywhere it appears.
function aimHue(name) { let h = 0; for (const ch of String(name)) h = (h * 31 + ch.charCodeAt(0)) % 360; return h; }
function aimMonogram(name) {
  const parts = String(name).replace(/^[^/]*\//, '').split(/[-_:.\s/]+/).filter(Boolean);
  const next = parts.slice(1).find(x => /^[a-z]/i.test(x));
  return ((parts[0]?.[0] || '?') + (next?.[0] || parts[0]?.[1] || '')).toUpperCase();
}
const AIM_HEALTH = { HEALTHY: ['Healthy', 'good'], UNHEALTHY: ['Unhealthy', 'bad'] };
const AIM_CAPS = [['JSON', 'supports_structured_output', '{ }'], ['tools', 'supports_tools', '⚒'], ['reasoning', 'supports_reasoning', '◇'], ['vision', 'supports_vision', '◉']];
function aimModelCard(m, labels, d) {
  const test = AIM.tests[`model:${m.name}`], open = AIM.more?.[m.name], [healthLabel, tone] = AIM_HEALTH[m.health_status] || ['Not tested', 'muted'];
  const users = m.used_by || [], shown = users.slice(0, 3), hidden = users.length - shown.length;
  const sub = [m.provider || 'provider unknown', m.underlying_model && m.underlying_model !== m.name ? m.underlying_model : ''].filter(Boolean).join(' · ');
  const caps = AIM_CAPS.map(([n, key, icon]) => { const v = m[key]; return `<span class="aim-cap ${v === true ? 'yes' : v === false ? 'no' : 'unk'}" title="${attr(`${n}: ${aimYes(v)}`)}"><i aria-hidden="true">${icon}</i>${n}<b aria-hidden="true">${v === true ? '✓' : v === false ? '✗' : '?'}</b></span>`; }).join('');
  const stat = (k, v, hint = '') => `<div><dt>${k}</dt><dd>${v}${hint ? `<small>${hint}</small>` : ''}</dd></div>`;
  const tags = [aimHostTag(m.name), m.managed ? pill('Managed', 'brand') : '', m.present_in_litellm ? '' : pill('Not in LiteLLM', 'warn'), m.enabled ? '' : pill('Disabled', 'muted'), m.model_type && m.model_type !== 'CUSTOM' ? pill(m.model_type.replace('_', ' ').toLowerCase(), 'plain') : ''].join('');
  const more = open ? `<div class="aim-mcard-more"><label class="field" for="aim-type-${attr(m.name)}"><span>Type</span><select class="select" id="aim-type-${attr(m.name)}" data-act-change="aim-model-type" data-name="${attr(m.name)}">${(d.options?.model_types || []).map(t => aimOpt(t, t.replace('_', ' ').toLowerCase(), m.model_type)).join('')}</select></label>${m.model_type === 'EMBEDDING' ? `<label class="field" for="aim-dims-${attr(m.name)}"><span>Vector size (dimensions)</span><input class="input" type="number" min="1" max="65536" id="aim-dims-${attr(m.name)}" value="${attr(m.embedding_dimensions ?? '')}" placeholder="${attr(m.discovered?.embedding_dimensions ? `Discovered: ${m.discovered.embedding_dimensions}` : 'Unknown — run Test')}" data-act-change="aim-model-dims" data-name="${attr(m.name)}"></label>` : ''}${m.managed ? btn('Stop managing', 'aim-model-unmanage', { name: m.name }, 'sm') : ''}${btn(isBusy('aim-model-remove') ? 'Deleting…' : 'Delete model', 'aim-model-remove', { name: m.name, managed: m.managed ? '1' : '' }, 'sm danger', isBusy('aim-model-remove'))}</div>` : '';
  const refs = AIM.refs?.[m.name];
  return `<article class="card aim-mcard ${m.enabled ? '' : 'off'}" style="--h:${aimHue(m.name)}" data-model="${attr(m.name)}" tabindex="-1">
    <header class="aim-mcard-head"><span class="aim-mcard-mono" aria-hidden="true">${esc(aimMonogram(m.name))}</span>
      <div class="aim-mcard-title"><div class="aim-mcard-name mono b" title="${attr(m.name)}">${aimModelName(m.name)}</div><div class="xs muted ellipsis" title="${attr(sub)}">${esc(sub)}</div></div>
      <span class="aim-health ${tone}"><i aria-hidden="true"></i>${healthLabel}</span></header>
    ${tags ? `<div class="aim-mcard-tags">${tags}</div>` : ''}
    <dl class="aim-mcard-stats">${stat('Context', esc(aimTokens(m.context_window)), m.max_context_window ? `/ ${esc(aimTokens(m.max_context_window))}` : '')}${stat('Latency', aimLatency(m.health_latency_ms))}${stat('Workers', String(users.length))}</dl>
    <div class="aim-mcard-caps">${caps}</div>
    <div class="aim-mcard-users" title="${attr(users.map(u => labels[u] || u).join(', '))}">${users.length ? `<span class="xs muted">Used by</span>${shown.map(u => `<span class="aim-user">${esc(labels[u] || u)}</span>`).join('')}${hidden ? `<span class="aim-user more">+${hidden} more</span>` : ''}` : '<span class="xs faint">Not used by any worker</span>'}</div>
    ${aimFallbacks(m)}
    ${refs ? `<div class="settings-error small" role="alert"><strong>Not deleted.</strong> Still used by: ${refs.map(esc).join(', ')}. Change those first.</div>` : ''}
    ${more}
    <footer class="aim-mcard-foot">${btn(isBusy(`aim-mtest-${m.name}`) ? 'Testing…' : 'Test', 'aim-model-test', { name: m.name }, 'sm', isBusy(`aim-mtest-${m.name}`))}${btn(m.enabled ? 'Disable' : 'Enable', 'aim-model-toggle', { name: m.name, on: m.enabled ? '' : '1' }, 'sm')}
      <button type="button" class="btn sm aim-mcard-dots ${open ? 'on' : ''}" data-act="aim-model-more" data-name="${attr(m.name)}" aria-expanded="${open ? 'true' : 'false'}" aria-label="More for ${attr(m.name)}" title="Type and management">⋯</button></footer>
    ${test ? aimModelTestLine(test) : ''}</article>`;
}
// Latency from the model's health check: milliseconds while short, seconds once it is long.
const aimLatency = ms => ms == null ? '—' : ms < 10000 ? `${ms}<small>ms</small>` : `${Math.round(ms / 100) / 10}<small>s</small>`;
// How long a failing model rests before it is tried again: the model's own choice, or the Reliability setting.
const AIM_REST = [['', 'Reliability setting'], [60, '1 minute'], [300, '5 minutes'], [900, '15 minutes'], [1800, '30 minutes'],
  [3600, '1 hour'], [21600, '6 hours'], [86400, '24 hours']];
const aimRestLabel = s => s ? (AIM_REST.find(([v]) => v === s)?.[1] || (s < 3600 ? `${Math.round(s / 60)} minutes` : `${Math.round(s / 3600)} hours`)) : null;
// A model's own failover: the models every worker on it falls back to, in order, and how long it rests.
function aimFallbacks(m) {
  const editing = AIM.fbEdit === m.name, list = m.fallback_models || [], rest = aimRestLabel(m.rest_seconds);
  const view = `<div class="aim-mcard-fb"><div class="row between gap6"><span class="xs muted">Falls back to</span>${btn(editing ? 'Cancel' : 'Fallbacks', 'aim-fb-edit', { name: m.name }, 'sm aim-fb-btn')}</div>
    ${aimBackupChain(list, 'No fallback', 'Fallback')}<span class="xs muted">${rest ? `Rests ${esc(rest)} after a rate limit or failures, then is tried again` : 'Rests as set in Reliability, then is tried again'}</span></div>`;
  if (!editing) return view;
  const key = `fb-${m.name}`, values = Array.from({ length: 4 }, (_, i) => fv(key, `m${i}`, list[i] || ''));
  const shown = Math.min(4, values.filter(Boolean).length + 1);
  const others = aimModels().filter(x => x.enabled && x.name !== m.name).map(x => x.name);
  const options = selected => aimOpt('', 'None', selected) + [...new Set([...others, ...(selected ? [selected] : [])])].map(n => aimModelOpt(n, n, selected)).join('');
  const selects = Array.from({ length: shown }, (_, i) => `<label class="field" for="${attr(key)}-m${i}"><span>Fallback ${i + 1}</span><select class="select" id="${attr(key)}-m${i}" data-form="${attr(key)}" data-field="m${i}" data-live="1" data-picker>${options(values[i])}</select></label>`).join('');
  const restValue = fv(key, 'rest', m.rest_seconds ?? '');
  return `<div class="aim-mcard-fb editing"><p class="xs muted">Every worker using ${esc(m.name)} tries these, in order, after its own backups, when ${esc(m.name)} is rate limited or failing.</p>${selects}
    <label class="field" for="${attr(key)}-rest"><span>Rest after a failure</span><select class="select" id="${attr(key)}-rest" data-form="${attr(key)}" data-field="rest">${AIM_REST.map(([v, t]) => aimOpt(v, t, restValue)).join('')}</select><span class="settings-help">Then it is tried again. A set time is the same every time; the Reliability setting doubles after each failure in a row.</span></label>
    ${btn(isBusy('aim-fb') ? 'Saving…' : 'Save fallbacks', 'aim-fb-save', { name: m.name }, 'primary sm', isBusy('aim-fb'))}</div>`;
}
// Models a person removed: out of every picker and the configuration, and left removed by a sync, until restored.
function aimRemoved() {
  const list = aimData().removed_models || [];
  if (!list.length) return '';
  const rows = list.map(r => `<div class="row between gap10" style="padding:6px 0"><span><span class="mono">${esc(r.name)}</span> <span class="xs muted">removed ${esc(fmtDateTime(r.removed_at))}</span></span>${btn(isBusy('aim-model-restore') ? 'Restoring…' : 'Restore', 'aim-model-restore', { name: r.name }, 'sm', isBusy('aim-model-restore'))}</div>`).join('');
  return aimPanel(settingsDetails('aim-removed', `Removed models (${list.length})`, `<p class="settings-help">Removed from Job Hunt OS; a sync does not bring them back. A model LiteLLM serves on its own is still there.</p>${rows}`));
}
function aimAddForm() {
  const o = AIM.ollama, local = (o?.models || []).map(m => aimOpt(m.name, `${m.name}${m.context_length ? ` · ${aimTokens(m.context_length)} ctx` : ''}`, fv('aim-add', 'ollama_model', ''))).join('');
  return `<div class="settings-group aim-add"><p class="settings-help">Job Hunt OS deploys this model into LiteLLM when you add it (needs a sync mode in Settings).${o && !o.reachable ? ` Ollama at ${esc(o.api_base)} did not answer; type the model name instead.` : ''}</p><div class="settings-grid"><label class="field" for="aim-add-litellm_model_name"><span>LiteLLM model name (alias)</span>${inputF('aim-add', 'litellm_model_name', '', 'placeholder="local-fast" required')}</label><label class="field" for="aim-add-ollama_model"><span>Ollama model</span>${local ? `<select class="select" id="aim-add-ollama_model" data-form="aim-add" data-field="ollama_model">${aimOpt('', 'Choose…', fv('aim-add', 'ollama_model', ''))}${local}</select>` : inputF('aim-add', 'ollama_model', '', 'placeholder="llama3.2:3b"')}</label><label class="field" for="aim-add-num_ctx"><span>Context size (num_ctx)</span>${inputF('aim-add', 'num_ctx', '', 'type="number" min="512" placeholder="8192"')}</label><label class="field" for="aim-add-keep_alive"><span>Keep alive</span>${inputF('aim-add', 'keep_alive', '', 'placeholder="15m"')}</label><label class="field" for="aim-add-model_type"><span>Type</span><select class="select" id="aim-add-model_type" data-form="aim-add" data-field="model_type">${(aimData().options?.model_types || []).map(t => aimOpt(t, t.replace('_', ' ').toLowerCase(), fv('aim-add', 'model_type', 'CUSTOM'))).join('')}</select></label></div>${btn(isBusy('aim-add') ? 'Adding…' : 'Add model', 'aim-add', {}, 'primary sm', isBusy('aim-add'))}</div>`;
}
function aimTiers() {
  const d = aimData(), tiers = d.tiers || {}, r = d.routing || {};
  const tierRows = Object.entries(tiers).map(([name, t]) => `<tr><td class="mono">${esc(name)}<div class="settings-help">${esc(t.description || '')}</div></td><td><select class="select aim-inline" aria-label="Model for tier ${attr(name)}" data-picker data-act-change="aim-tier-model" data-tier="${attr(name)}">${aimModelOptions(t.model)}</select></td><td>${btn(t.enabled ? 'Disable' : 'Enable', 'aim-tier-toggle', { tier: name, on: t.enabled ? '' : '1' }, 'sm')}</td></tr>`).join('');
  const rv = k => fv('aim-route', k, k.startsWith('tier_') ? (r.tier_models || {})[k.slice(5)] || '' : r[k] ?? '');
  const classifier = rv('auto_router_enabled') ? rv('classifier') : 'DISABLED';
  const tierSelects = (d.options?.routing_tiers || ['SIMPLE', 'MEDIUM', 'COMPLEX', 'REASONING']).map(t => `<label class="field" for="aim-route-tier_${t}"><span>${t}</span><select class="select" id="aim-route-tier_${t}" data-form="aim-route" data-field="tier_${t}" data-picker>${aimModelOptions(rv(`tier_${t}`))}</select></label>`).join('');
  const router = `<p class="settings-help">LiteLLM classifies each request and sends it to the model for its complexity. Workers set to “Auto router” use it.</p><div class="settings-grid"><label class="field" for="aim-route-classifier"><span>Auto router</span><select class="select" id="aim-route-classifier" data-form="aim-route" data-field="classifier" data-live="1">${Object.entries(AIM_CLASSIFIERS).map(([k, t]) => aimOpt(k, t, classifier)).join('')}</select></label><label class="field" for="aim-route-auto_router_model_name"><span>Router model name</span>${inputF('aim-route', 'auto_router_model_name', r.auto_router_model_name || '', 'placeholder="jobhunt-auto"')}</label></div>
    ${classifier === 'DISABLED' ? '' : `<div class="settings-checks">${checkF('aim-route', 'manage_auto_router', rv('manage_auto_router'), 'Job Hunt OS deploys this router into LiteLLM (otherwise it must already exist there)')}${checkF('aim-route', 'send_metadata', rv('send_metadata'), 'Send Job Hunt OS request metadata (worker, workflow, retry, confidence)')}</div><div class="settings-grid">${tierSelects}<label class="field" for="aim-route-default_model"><span>Default model</span><select class="select" id="aim-route-default_model" data-form="aim-route" data-field="default_model" data-picker>${aimModelOptions(rv('default_model'))}</select></label>${classifier === 'LLM' ? `<label class="field" for="aim-route-classifier_model"><span>Classifier model</span><select class="select" id="aim-route-classifier_model" data-form="aim-route" data-field="classifier_model" data-picker>${aimModelOptions(rv('classifier_model'))}</select></label>` : ''}${classifier === 'CUSTOM' ? `<label class="field" for="aim-route-classifier_plugin"><span>Classifier plugin</span>${inputF('aim-route', 'classifier_plugin', r.classifier_plugin || '', 'placeholder="jobhunt_classifier.router"')}<span class="settings-help">A ClassifierPlugin importable by LiteLLM; see integrations/litellm/jobhunt_classifier.py.</span></label>` : ''}</div>`}
    ${btn(isBusy('aim-route') ? 'Saving…' : 'Save auto router', 'aim-route-save', {}, 'primary sm', isBusy('aim-route'))}`;
  return aimPanel(settingsGroup('Tiers', 'Named slots workers can point at. Change the model behind a tier and every worker on it follows.', `<div class="mon-table-wrap"><table class="mon-table"><thead><tr><th>Tier</th><th>Model</th><th></th></tr></thead><tbody>${tierRows}</tbody></table></div><div class="row gap8 mt14"><label class="field" for="aim-tier-new"><span>New tier</span>${inputF('aim-tier', 'new', '', 'placeholder="e.g. CHEAP_JSON" pattern="[A-Za-z][A-Za-z0-9_]+"')}</label>${btn('Add tier', 'aim-tier-add', {}, 'sm')}</div>
    ${settingsDetails('aim-router', `Auto router · ${classifier === 'DISABLED' ? 'off' : esc(AIM_CLASSIFIERS[classifier] || classifier)}`, router)}`));
}
function aimRoutingPayload() {
  const r = aimData().routing || {}, form = S.forms['aim-route'] || {}, body = { revision: aimRev() };
  if ('classifier' in form) { body.auto_router_enabled = form.classifier !== 'DISABLED'; if (form.classifier !== 'DISABLED') body.classifier = form.classifier; }
  for (const k of ['auto_router_model_name', 'default_model', 'classifier_model', 'classifier_plugin']) if (k in form) body[k] = form[k] || null;
  for (const k of ['manage_auto_router', 'send_metadata']) if (k in form) body[k] = !!form[k];
  const tiers = Object.keys(form).filter(k => k.startsWith('tier_'));
  if (tiers.length) body.tier_models = Object.fromEntries(['SIMPLE', 'MEDIUM', 'COMPLEX', 'REASONING'].map(t => [t, `tier_${t}` in form ? form[`tier_${t}`] || null : (r.tier_models || {})[t] || null]));
  return body;
}

/* ───────────── reliability ───────────── */
function aimReliability() {
  return aimPanel(typeof foPanel === 'function' ? foPanel() : settingsGroup('Automatic failover', '', '<p class="settings-help">Unavailable.</p>'));
}

/* ───────────── settings: route, connection, history ───────────── */
function aimSettings() {
  const d = aimData(), label = d.gateway?.direct_label || 'the direct provider';
  const route = d.gateway ? settingsGroup('Route', esc(gatewayNow(d.gateway)), gatewaySegment(d.gateway)) : '';
  const direct = D.settings?.ai ? `<form id="settings-editor" data-form="ai" class="aim-direct-form" novalidate><fieldset ${isBusy('settings-ai') ? 'disabled' : ''}><div id="settings-error" class="settings-error" role="alert" ${SETTING_ERRORS.ai ? '' : 'hidden'}>${esc(SETTING_ERRORS.ai || '')}</div>${renderAISettings()}</fieldset></form>` : '';
  const directGroup = direct ? (d.gateway_enabled ? settingsDetails('aim-direct', `Direct provider · ${esc(label)} (used when the route is direct)`, direct) : settingsGroup(`Direct provider · ${esc(label)}`, 'Workers call this provider directly with these models.', direct)) : '';
  const litellm = aimConnection();
  return aimPanel(route + (d.gateway_enabled ? litellm + directGroup : `<div class="aim-direct-models" data-route="direct">${directGroup}</div>` + settingsDetails('aim-litellm', 'LiteLLM connection (for when you switch the route)', litellm))) + aimPanel(aimHistoryGroup());
}
function aimConnection() {
  const c = aimData().connection || {}, [label, tone] = AIM_CONN[c.status] || AIM_CONN.UNKNOWN, t = AIM.tests.connection, v = AIM.tests.verify;
  const modes = (c.sync_modes || ['disabled', 'admin_api']).map(m => aimOpt(m, { disabled: 'Disabled (read only)', admin_api: 'Admin API (STORE_MODEL_IN_DB=True)', config_file: 'Managed config file + restart' }[m] || m, fv('aim-conn', 'sync_mode', c.sync_mode))).join('');
  const [syncLabel, syncTone] = AIM_SYNC[c.sync_status] || AIM_SYNC.SYNCED;
  const sync = `${c.last_sync_error ? `<p class="settings-error">${esc(c.last_sync_error)}</p>` : ''}${c.managed_config_path ? `<p class="settings-help">Managed file: <span class="mono">${esc(c.managed_config_path)}</span>${c.restart_command_configured ? ' · restarted automatically' : ' · restart LiteLLM after saving'}</p>` : ''}<div class="row gap8">${btn(isBusy('aim-verify') ? 'Verifying…' : 'Verify against LiteLLM', 'aim-verify', {}, 'sm', isBusy('aim-verify'))}${btn(isBusy('aim-runtime') ? 'Checking…' : 'Check what workers run', 'aim-runtime', {}, 'sm', isBusy('aim-runtime'))}</div>${v ? `<p class="settings-help" role="status">${esc(v.status)}${(v.differences || []).length ? ` — ${esc(v.differences.join('; '))}` : ''}${(v.missing_models || []).length ? ` — not served: ${esc(v.missing_models.join(', '))}` : ''}</p>` : ''}${AIM.runtime ? aimRuntimeTable() : ''}`;
  return settingsGroup('LiteLLM', `${pill(label, tone)} ${c.litellm_version ? `LiteLLM ${esc(c.litellm_version)}` : ''} ${pill(syncLabel, syncTone)}${c.last_sync_at ? ` last applied ${esc(fmtDateTime(c.last_sync_at))}` : ''}`, `${c.secret_storage === false ? '<div class="settings-notice">Key storage is unavailable until the server has a SECRET_KEY.</div>' : ''}<div class="settings-grid"><label class="field" for="aim-conn-base_url"><span>LiteLLM base URL</span>${inputF('aim-conn', 'base_url', c.base_url_saved || '', `type="url" placeholder="${attr(c.base_url_env || 'http://localhost:4000')}"`)}<span class="settings-help">${c.base_url_env && !c.base_url_saved ? `Using the server default ${esc(c.base_url_env)}.` : ''}</span></label><label class="field" for="aim-conn-api_key"><span>LiteLLM API key</span>${inputF('aim-conn', 'api_key', '', `type="password" autocomplete="off" spellcheck="false" placeholder="${attr(c.api_key_configured ? `Saved ${c.api_key_hint || ''}` : 'sk-…')}"`)}<span class="settings-help">Encrypted; never shown again. Leave blank to keep the saved key.</span></label><label class="field" for="aim-conn-sync_mode"><span>Configuration sync</span><select class="select" id="aim-conn-sync_mode" data-form="aim-conn" data-field="sync_mode">${modes}</select><span class="settings-help">Only managed models and the auto router live in LiteLLM; worker settings travel with each request.</span></label></div><div class="row gap8">${btn(isBusy('aim-conn') ? 'Saving…' : 'Save', 'aim-conn-save', {}, 'primary sm', isBusy('aim-conn'))}${btn(isBusy('aim-conn-test') ? 'Testing…' : 'Test Connection', 'aim-conn-test', {}, 'sm', isBusy('aim-conn-test'))}${c.api_key_configured ? btn('Remove saved key', 'aim-conn-key-remove', {}, 'sm') : ''}</div>${t ? `<ul class="aim-checks" role="status"><li>${t.reachable ? '✓' : '✗'} LiteLLM reachable</li><li>${t.authenticated ? '✓' : '✗'} Authentication</li><li>${t.discovery ? '✓' : '✗'} Model discovery (${t.models} models)</li><li>Latency ${t.latency_ms != null ? `${t.latency_ms} ms` : 'unknown'}</li>${t.error ? `<li class="settings-error">${esc(t.error)}</li>` : ''}</ul>` : ''}${c.last_error && !t ? `<p class="settings-error">${esc(c.last_error)}</p>` : ''}
    ${settingsDetails('aim-sync-details', 'Synchronization', sync)}`);
}
function aimRuntimeTable() {
  const r = AIM.runtime;
  return `<p class="settings-help">${r.live_error ? `LiteLLM could not be read: ${esc(r.live_error)}` : 'LiteLLM was read just now.'}</p><div class="mon-table-wrap"><table class="mon-table"><thead><tr><th>Worker</th><th>Desired</th><th>Active</th><th>Status</th></tr></thead><tbody>${(r.workers || []).map(w => { const [text, tone] = AIM_SYNC[w.state] || AIM_SYNC.SYNCED; return `<tr><td>${esc(w.label)}</td><td class="mono">${esc(w.desired || '—')}</td><td class="mono">${esc(w.active || '—')}</td><td>${pill(w.state === 'SYNCED' ? 'Synced ✓' : text, tone)}</td></tr>`; }).join('')}</tbody></table></div>`;
}
function aimHistoryGroup() {
  if (AIM.versions === null && !isBusy('aim-history')) aimHistory();
  const versions = (AIM.versions || []).map((v, i) => `<tr><td class="mono">v${v.version}${i === 0 ? ' ' + pill('Active', 'good') : ''}</td><td>${esc(fmtDateTime(v.created_at))}</td><td>${esc(v.changed_by)} <span class="settings-help">${esc(v.source)}</span></td><td>${esc(v.summary)}${v.restored_from_version ? ` <span class="settings-help">(from v${v.restored_from_version})</span>` : ''}</td><td>${i === 0 ? '' : btn('Roll back', 'aim-restore', { id: v.id, version: v.version }, 'sm', isBusy('aim-restore'))}</td></tr>`).join('');
  const audit = (AIM.audit || []).slice(0, 30).map(a => `<tr><td>${esc(fmtDateTime(a.created_at))}</td><td>${esc(a.action)}</td><td>${pill(a.result, a.result === 'SUCCEEDED' ? 'good' : a.result === 'REJECTED' ? 'warn' : 'bad')}</td><td>${esc(a.actor)} <span class="settings-help">${esc(a.source)}</span></td><td class="settings-help">${esc(a.error || (a.version ? `v${a.version}` : ''))}</td></tr>`).join('');
  return settingsGroup('History', 'Every save is kept as a version. Rolling back applies an old version as a new one.', `${versions ? `<div class="mon-table-wrap aim-wide"><table class="mon-table"><thead><tr><th>Version</th><th>When</th><th>By</th><th>Change</th><th></th></tr></thead><tbody>${versions}</tbody></table></div>` : AIM.versions ? '<p class="settings-help">No versions yet.</p>' : loading()}
    ${settingsDetails('aim-audit', 'Audit trail', audit ? `<div class="mon-table-wrap aim-wide"><table class="mon-table"><thead><tr><th>When</th><th>Action</th><th>Result</th><th>By</th><th>Detail</th></tr></thead><tbody>${audit}</tbody></table></div>` : '<p class="settings-help">Nothing recorded yet.</p>')}`);
}
async function aimHistory() {
  S.busy['aim-history'] = true;
  try { const [v, a] = await Promise.all([api('/api/settings/ai/versions'), api('/api/settings/ai/audit')]); AIM.versions = v.versions || []; AIM.audit = a.entries || []; }
  catch (err) { AIM.versions = []; AIM.audit = []; toast(err.message, true); }
  finally { delete S.busy['aim-history']; render(); }
}

const aimWorkerByType = type => (aimData().workers || []).find(x => x.worker_type === type);
const AI_MODELS_ACTIONS = {
  'aim-reload': () => { AIM.error = ''; AIM.data = undefined; render(); },
  'aim-tab': el => { AIM.tab = el.dataset.tab; AIM.drawer = null; render(); },
  'aim-worker': el => { AIM.drawer = el.dataset.worker; delete S.forms.aiw; AIM.tab = 'workers'; render(); document.getElementById('aiw-target')?.focus(); },
  'aim-fix': el => {
    const t = aimFixTarget(el.dataset.path);
    if (!t) return;
    AIM.tab = t.tab; AIM.drawer = t.worker || null;
    if (t.worker) delete S.forms.aiw;
    if (t.details) SETTING_DETAILS.add(t.details);
    if (t.fallbacks) { delete S.forms[`fb-${t.model}`]; AIM.fbEdit = t.model; }
    render();
    const target = document.querySelector(t.focus) || (t.worker && document.getElementById('aiw-target'));
    target?.scrollIntoView?.({ block: 'center', behavior: 'smooth' });
    target?.focus?.({ preventScroll: true });
  },
  'aim-size-cap': el => {
    const w = aimWorkerByType(el.dataset.worker), value = Number(el.dataset.value);
    return aimSave('aim-size', () => api(`/api/settings/ai/workers/${encodeURIComponent(w.worker_type)}`, { method: 'PUT', body: { revision: aimRev(), max_output_tokens: value } }), `${w.label}: answers capped at ${aimTokens(value)} tokens`);
  },
  'aim-size-backup': el => {
    const w = aimWorkerByType(el.dataset.worker), list = [...new Set([...aimBackups(w.config), el.dataset.model])].slice(0, AIM_BACKUPS);
    return aimSave('aim-size', () => api(`/api/settings/ai/workers/${encodeURIComponent(w.worker_type)}`, { method: 'PUT', body: { revision: aimRev(), fallback_model: list[0], secondary_models: list.slice(1) } }), `${el.dataset.model} is now a backup for ${w.label}`);
  },
  // Replaces the refusing model in the role it has for this worker: its model, one of its backups, or its escalation.
  'aim-size-use': el => {
    const w = aimWorkerByType(el.dataset.worker), c = w.config || {}, { model: name, replace: old, role } = el.dataset;
    let change;
    if (role === 'backup') {
      const list = [...new Set(aimBackups(c).map(m => m === old ? name : m))];
      change = { fallback_model: list[0] || null, secondary_models: list.slice(1) };
    } else if (role === 'escalation') {
      change = { escalation: { ...(c.escalation || {}), model: name, tier: null } };
    } else {
      // The new primary leaves the backups, where it would only be tried after itself.
      const list = aimBackups(c).filter(m => m !== name);
      change = { ...aimTargetFields(`model:${name}`, c), fallback_model: list[0] || null, secondary_models: list.slice(1) };
    }
    const where = role === 'backup' ? ' as a backup' : role === 'escalation' ? ' for escalation' : '';
    return aimSave('aim-size', () => api(`/api/settings/ai/workers/${encodeURIComponent(w.worker_type)}`, { method: 'PUT', body: { revision: aimRev(), ...change } }), `${w.label} now uses ${name}${where} instead of ${old}`);
  },
  'aim-insight-toggle': el => { const t = el.dataset.worker; AIM.insightOpen = { ...(AIM.insightOpen || {}), [t]: !AIM.insightOpen?.[t] }; render(); },
  // One tap: the model and its backups go in one PUT, so the switch is one version (and one rollback).
  'aim-insight-apply': el => {
    const w = aimWorkerByType(el.dataset.worker), item = aimInsight(w.worker_type)?.[el.dataset.kind]?.[Number(el.dataset.index)];
    if (!item?.apply) return;
    const chain = [item.apply.fallback_model, ...(item.apply.secondary_models || [])].filter(Boolean);
    return aimSave(`aim-insight-${w.worker_type}`, () => api(`/api/settings/ai/workers/${encodeURIComponent(w.worker_type)}`, { method: 'PUT', body: { revision: aimRev(), ...item.apply } }), `${w.label} now uses ${item.model}${chain.length ? `, then ${chain.join(' → ')}` : ''}`);
  },
  'aim-wf-chip': el => { const c = el.dataset.chip, on = fv('aim-wf', 'on', []); setF('aim-wf', 'on', on.includes(c) ? on.filter(x => x !== c) : [...on, c]); render(); },
  'aim-wf-clear': () => { setF('aim-wf', 'q', ''); setF('aim-wf', 'on', []); render(); },
  'aim-drawer-close': () => { AIM.drawer = null; delete S.forms.aiw; render(); },
  'aim-worker-save': async el => {
    const w = aimWorkerByType(el.dataset.worker);
    // The drawer closes only once the change is applied; a refused save keeps what was typed.
    const out = await aimSave('aim-worker-save', () => api(`/api/settings/ai/workers/${encodeURIComponent(w.worker_type)}`, { method: 'PUT', body: aimWorkerPayload(w) }), `${w.label} saved`);
    if (out) { delete S.forms.aiw; AIM.drawer = null; render(); }
    return out;
  },
  'aim-worker-target': el => {
    const w = aimWorkerByType(el.dataset.worker);
    // An embedding worker is on exactly while it has a model (it is seeded off and never borrows a chat tier).
    const extra = w.capability === 'EMBEDDING' ? { enabled: !!el.value, model_mode: 'FIXED' } : {};
    return aimSave('aim-worker-target', () => api(`/api/settings/ai/workers/${encodeURIComponent(w.worker_type)}`, { method: 'PUT', body: { revision: aimRev(), ...aimTargetFields(el.value, w.config), ...extra } }), `${w.label} now uses ${el.value.replace(/^(model|tier):/, '') || (w.capability === 'EMBEDDING' ? 'nothing (off)' : 'its default')}`);
  },
  'aim-worker-test': el => { const w = el.dataset.worker; return busy(`aim-wtest-${w}`, async () => { AIM.tests[`worker:${w}`] = await api(`/api/settings/ai/workers/${encodeURIComponent(w)}/test`, { method: 'POST', body: { configuration: 'draft' } }); }); },
  'aim-model-test': el => {
    const n = el.dataset.name;
    return busy(`aim-mtest-${n}`, async () => {
      const t = AIM.tests[`model:${n}`] = await api(`/api/settings/ai/models/${encodeURIComponent(n)}/test`, { method: 'POST' });
      await aimLoad();
      if (t?.error && !t.success) toast(t.error, true, { title: `${n} · ${aimErrorHeadline(t.error)}`, top: true, ms: 10000 });
    });
  },
  'aim-model-toggle': el => {
    let dropped = [];
    return aimSave('aim-model', async () => { dropped = (await api(`/api/settings/ai/models/${encodeURIComponent(el.dataset.name)}`, { method: 'PUT', body: { revision: aimRev(), enabled: !!el.dataset.on } }))?.dropped || []; }, () => `${el.dataset.name} ${el.dataset.on ? 'enabled' : 'disabled'}${aimDropped(dropped)}`);
  },
  'aim-model-dims': el => {
    const m = aimModel(el.dataset.name) || {}, size = el.value === '' ? null : Number(el.value);
    return aimSave('aim-model', () => api(`/api/settings/ai/models/${encodeURIComponent(el.dataset.name)}`, { method: 'PUT', body: { revision: aimRev(), overrides: { ...(m.overrides || {}), embedding_dimensions: size } } }), 'Vector size saved');
  },
  'aim-model-type': el => aimSave('aim-model', () => api(`/api/settings/ai/models/${encodeURIComponent(el.dataset.name)}`, { method: 'PUT', body: { revision: aimRev(), model_type: el.value } }), 'Model type saved'),
  'aim-model-unmanage': el => { if (!confirm(`Stop managing ${el.dataset.name}? Its deployment is removed from LiteLLM.`)) return; return aimSave('aim-model', () => api(`/api/settings/ai/models/${encodeURIComponent(el.dataset.name)}?revision=${aimRev()}`, { method: 'DELETE' }), `${el.dataset.name} no longer managed`); },
  'aim-sync': () => aimSave('aim-sync', async () => { AIM.tests.sync = await api('/api/settings/ai/models/sync', { method: 'POST' }); }, () => { const s = AIM.tests.sync || {}; return `${s.models ?? 0} models discovered${s.added?.length ? `, ${s.added.length} new` : ''}`; }),
  'aim-fb-edit': el => { const n = el.dataset.name; delete S.forms[`fb-${n}`]; AIM.fbEdit = AIM.fbEdit === n ? null : n; render(); },
  'aim-fb-save': async el => {
    const n = el.dataset.name, f = S.forms[`fb-${n}`] || {}, m = aimModel(n) || {};
    const list = Array.from({ length: 4 }, (_, i) => (`m${i}` in f ? f[`m${i}`] : (m.fallback_models || [])[i]) || '').filter(Boolean);
    const rest = 'rest' in f ? f.rest : m.rest_seconds ?? '';
    const out = await aimSave('aim-fb', () => api(`/api/settings/ai/models/${encodeURIComponent(n)}`, { method: 'PUT', body: { revision: aimRev(), fallback_models: [...new Set(list)], rest_seconds: rest === '' || rest == null ? null : Number(rest) } }), `Fallbacks for ${n} saved`);
    if (out) { AIM.fbEdit = null; delete S.forms[`fb-${n}`]; render(); }
    return out;
  },
  'aim-model-remove': el => {
    const n = el.dataset.name;
    const what = el.dataset.managed ? `Its deployment is also removed from LiteLLM.` : `It stays in LiteLLM; Job Hunt OS stops using it.`;
    if (!confirm(`Delete ${n} from Job Hunt OS? ${what} A sync won't bring it back; you can restore it from Removed models.`)) return;
    AIM.refs = { ...(AIM.refs || {}) }; delete AIM.refs[n];
    let dropped = [];
    return aimSave('aim-model-remove', async () => {
      try { dropped = (await api(`/api/settings/ai/models/${encodeURIComponent(n)}/remove`, { method: 'POST', body: { revision: aimRev() } }))?.dropped || []; }
      catch (err) { if (err.status === 409 && err.detail?.references) { AIM.refs[n] = err.detail.references; render(); } throw err; }
      AIM.more = { ...(AIM.more || {}), [n]: false };
    }, () => `${n} deleted${aimDropped(dropped)}`);
  },
  'aim-model-restore': el => aimSave('aim-model-restore', () => api(`/api/settings/ai/models/${encodeURIComponent(el.dataset.name)}/restore`, { method: 'POST', body: { revision: aimRev() } }), `${el.dataset.name} restored`),
  'aim-mf-chip': el => { const c = el.dataset.chip, on = aimMfOn(); setF('aim-mf', 'on', on.includes(c) ? on.filter(x => x !== c) : [...on, c]); render(); },
  'aim-mf-clear': () => { setF('aim-mf', 'q', ''); setF('aim-mf', 'on', []); render(); },
  'aim-model-more': el => { AIM.more = { ...(AIM.more || {}), [el.dataset.name]: !AIM.more?.[el.dataset.name] }; render(); },
  'aim-add-toggle': async () => { AIM.adding = !AIM.adding; render(); if (AIM.adding && !AIM.ollama) { AIM.ollama = await api('/api/settings/ai/ollama').catch(() => ({ reachable: false, models: [] })); render(); } },
  'aim-add': () => {
    const f = S.forms['aim-add'] || {};
    if (!String(f.litellm_model_name || '').trim()) { toast('Give the model a LiteLLM model name', true); return; }
    return aimSave('aim-add', async () => { await api('/api/settings/ai/models', { method: 'POST', body: { revision: aimRev(), litellm_model_name: f.litellm_model_name.trim(), ollama_model: f.ollama_model || '', num_ctx: f.num_ctx ? Number(f.num_ctx) : null, keep_alive: f.keep_alive || null, model_type: f.model_type || 'CUSTOM' } }); delete S.forms['aim-add']; AIM.adding = false; }, 'Model added');
  },
  'aim-tier-model': el => aimSave('aim-tier', () => api(`/api/settings/ai/tiers/${encodeURIComponent(el.dataset.tier)}`, { method: 'PUT', body: { revision: aimRev(), litellm_model_name: el.value || null } }), `Tier ${el.dataset.tier} saved`),
  'aim-tier-toggle': el => aimSave('aim-tier', () => api(`/api/settings/ai/tiers/${encodeURIComponent(el.dataset.tier)}`, { method: 'PUT', body: { revision: aimRev(), enabled: !!el.dataset.on } }), `Tier ${el.dataset.tier} ${el.dataset.on ? 'enabled' : 'disabled'}`),
  'aim-tier-add': () => { const name = String(fv('aim-tier', 'new', '')).trim().toUpperCase(); if (!name) return; return aimSave('aim-tier', async () => { await api(`/api/settings/ai/tiers/${encodeURIComponent(name)}`, { method: 'PUT', body: { revision: aimRev(), enabled: true } }); delete S.forms['aim-tier']; }, `Tier ${name} added`); },
  'aim-route-save': () => aimSave('aim-route', async () => { await api('/api/settings/ai/routing', { method: 'PUT', body: aimRoutingPayload() }); delete S.forms['aim-route']; }, 'Auto router saved'),
  'aim-conn-save': () => {
    const f = S.forms['aim-conn'] || {}, body = {};
    if ('base_url' in f) body.base_url = f.base_url; if (f.api_key) body.api_key = f.api_key; if ('sync_mode' in f) body.sync_mode = f.sync_mode;
    return aimRun('aim-conn', async () => { await api('/api/settings/ai/connection', { method: 'PUT', body }); delete S.forms['aim-conn']; }, 'LiteLLM connection saved');
  },
  'aim-conn-key-remove': () => { if (!confirm('Remove the saved LiteLLM key?')) return; return aimRun('aim-conn', () => api('/api/settings/ai/connection', { method: 'PUT', body: { api_key: '' } }), 'Key removed'); },
  'aim-conn-test': () => busy('aim-conn-test', async () => { AIM.tests.connection = await api('/api/settings/ai/test-connection', { method: 'POST' }); await aimLoad(); }, () => AIM.tests.connection?.label),
  'aim-runtime': () => busy('aim-runtime', async () => { AIM.runtime = await api('/api/settings/ai/runtime'); }),
  'aim-verify': () => busy('aim-verify', async () => { AIM.tests.verify = await api('/api/settings/ai/verify', { method: 'POST' }); await aimLoad(); }),
  'aim-apply': () => busy('aim-apply', aimApplyNow, out => aimSaved('Applied', out)),
  'aim-discard': () => busy('aim-discard', async () => { await api('/api/settings/ai/discard', { method: 'POST' }); AIM.failed = null; delete S.forms.aiw; await aimLoad(); }, 'Changes undone'),
  'aim-restore': el => {
    if (!confirm(`Roll back to v${el.dataset.version}? It is applied as a new version; history is kept.`)) return;
    return busy('aim-restore', async () => { const out = await api(`/api/settings/ai/versions/${el.dataset.id}/restore`, { method: 'POST', body: { base_version: aimData().active_version } }); AIM.versions = null; await aimLoad(); return out; }, out => `Restored as v${out.version}`);
  },
};
Object.assign(SETTINGS_ACTIONS, AI_MODELS_ACTIONS);
