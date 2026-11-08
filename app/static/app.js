/* Job Hunt OS workspace — client for the redesigned UI. Every value comes from the API. */
'use strict';

const BOOT = JSON.parse(document.getElementById('boot').textContent);
const $ = sel => document.querySelector(sel);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const attr = esc;

/* ───────────── state ───────────── */
// Filters on the Tailor and Approvals screens, sent to /api/tailor/queue and /api/approvals.
function pfDefaults(kind) { return { q: '', min_fit: 0, min_review: 0, board: 'all', location: 'all', status: 'all', ...(kind === 'approvals' ? { easy_apply: 'all' } : {}), sort: kind === 'tailor' ? 'fit' : 'default' }; }
// Discovery table filters, sent to /api/jobs/search. The date filter is one of: within (last N), older_than, a
// date_from–date_to range, or posted_unknown (Posted only); date_field says which date they look at.
const DEFAULT_FILTERS = { q: '', min_fit: 0, max_fit: 0, board: 'all', location: 'all', stage: 'all', description: 'all', domain: 'matching',
  date_field: 'found', within: '', older_than: '', date_from: '', date_to: '', posted_unknown: false, include_applied: false,
  work_mode: 'all', job_type: 'all', setup_id: '', has_salary: false, hide_agencies: false, hide_reposts: false, keywords: [],
  sort: 'fit', page: 1, per_page: 25 };
const freshFilters = (keep = {}) => ({ ...DEFAULT_FILTERS, keywords: [], ...keep });
// The table remembers its filters in this browser, per account (a shared browser never shows another account's
// searches); a stored value of the wrong type is ignored.
const FILTERS_KEY = `discoveryFilters:${BOOT.user?.id ?? 'anon'}`;
function storedFilters() {
  try {
    const saved = JSON.parse(localStorage.getItem(FILTERS_KEY) || 'null') || {};
    const kept = Object.fromEntries(Object.entries(saved).filter(([k, v]) => k in DEFAULT_FILTERS && k !== 'page'
      && (Array.isArray(DEFAULT_FILTERS[k]) ? Array.isArray(v) : typeof v === typeof DEFAULT_FILTERS[k])));
    return freshFilters(kept);
  } catch { return freshFilters(); }
}
const S = {
  screen: 'overview', knowledgeSection: 'answers', settingsSection: 'profile', todayVariant: 'focus', discMode: 'triage',
  filters: storedFilters(), datePop: false, dateMode: null, moreFilters: false,
  triage: { min_fit: 0, location: 'all', hide_agencies: true, hide_reposts: true, keywords: [] }, triagedThisSession: 0,
  checked: new Set(), rankedIds: new Set(), parsedIds: new Set(), selJob: null, jobOpen: null, jobTab: 'description',
  paletteOpen: false, paletteQuery: '', paletteJobs: [],
  kitJob: null, tailorJob: null, tailorTab: 'diff', editing: null,
  tailorMode: 'single', tailorChecked: new Set(), tailorRegenerate: false, tailorPages: '',  // '' = the default length in Settings
  pf: { tailor: pfDefaults('tailor'), approvals: pfDefaults('approvals') },
  approvalJob: null, approvalPicked: new Set(), bulkRejecting: false, rejecting: null, overriding: null, selApp: null, trackerView: 'board', trackerArchive: null, showArchived: false, addingStage: false, tf: { q: '', location: 'all', min_fit: 0 }, rf: { q: '', location: 'all' }, runSel: null, setupSel: null,
  scheduling: false, editingRecruiter: false, recruiterDraft: [], ivDraft: null, manualMissing: null,
  polling: null, busy: {}, forms: {}, apifyImport: false, importPick: new Set(), autoRefreshed: false,
};
const D = {};
const SCREENS = ['overview', 'today', 'discovery', 'tailor', 'approvals', 'tracker', 'orchestration', 'workers', 'monitoring', 'knowledge', 'settings'];

/* ───────────── helpers ───────────── */
async function api(url, { method = 'GET', body, form } = {}) {
  const opt = { method, headers: {} };
  if (form) opt.body = form;
  else if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const res = await fetch(url, opt);
  if (res.status === 401 && !url.startsWith('/api/auth/')) {
    window.location.href = '/login'; // session expired or signed out in another tab
    throw new Error('Sign in required');
  }
  const text = await res.text();
  let data; try { data = JSON.parse(text); } catch { data = text; }
  if (!res.ok) {
    const d = data && data.detail;
    const msg = typeof d === 'string' ? d : d?.message || (Array.isArray(d) ? d.map(x => x.msg).join('; ') : '') || res.statusText;
    const err = new Error(msg); err.detail = d; err.status = res.status; throw err;
  }
  return data;
}
// Location filters group by city (the first part of "Bengaluru, Karnataka, India"), and anything remote as Remote.
// The server does the same for Discovery, Tailor and Approvals (app.workspace.location_key); facets are [city, count].
const locationOptions = facets => [['all', 'All locations'], ...(facets || []).map(([k, n]) => [k, `${k} (${n})`])];
const locationKey = loc => { const l = (loc || '').trim(); return !l ? 'Not listed' : /remote/i.test(l) ? 'Remote' : l.split(',')[0].trim(); };
const qs = obj => new URLSearchParams(Object.entries(obj).filter(([, v]) => v !== '' && v !== null && v !== undefined)).toString();
let toastTimer;
function toast(msg, bad = false) {
  const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (bad ? ' bad' : ''); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 4000);
}
async function busy(key, fn, okMsg) {
  if (S.busy[key]) return;
  S.busy[key] = true; render();
  try {
    const out = await fn();
    const message = typeof okMsg === 'function' ? okMsg(out) : okMsg;
    if (message) toast(message);
    return out;
  } catch (e) { toast(e.message, true); }
  finally { delete S.busy[key]; render(); }
}
const isBusy = key => !!S.busy[key];
const label = (key, idle, working) => isBusy(key) ? working : idle;
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch { const t = document.createElement('textarea'); t.value = text; document.body.appendChild(t); t.select(); document.execCommand('copy'); t.remove(); }
  toast('Copied to clipboard');
}
const pill = (text, tone = 'muted', extra = '') => `<span class="pill ${tone} ${extra}" title="${attr(text)}"><span class="pt">${esc(text)}</span></span>`;
const bar = (pct, color, cls = '') => `<div class="bar ${cls}"><i style="width:${attr(pct)};background:${attr(color)}"></i></div>`;
const fitCls = n => n == null ? 'fit-none' : n >= 80 ? 'fit-high' : n >= 65 ? 'fit-mid' : 'fit-low';
const fitTxt = n => n == null ? '—' : String(Math.round(n));
const initials = s => (s || '?').split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0]).join('').toUpperCase();
const STATUS_TONE = { discovered: 'muted', shortlisted: 'brand', maybe: 'muted', archived: 'muted', dismissed: 'bad', tailored: 'warn', needs_retailor: 'warn', pending_approval: 'warn', rejected: 'bad', approved: 'good', applied: 'good' };
const STATUS_LABEL = { needs_retailor: 're-tailor' };
const statusText = s => STATUS_LABEL[s] || (s || '').replace(/_/g, ' ');
const statusTitle = s => statusText(s).charAt(0).toUpperCase() + statusText(s).slice(1);
const statusPill = s => pill(STATUS_LABEL[s] || (s || '').replace(/_/g, ' '), STATUS_TONE[s] || 'muted');
const toDate = iso => iso ? new Date(/Z|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z') : null;
const fmtDay = iso => iso ? toDate(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) : '';
// When the employer posted the job (a calendar date, so read in UTC: midnight must not slip to the day before),
// as the board gave it. Never the date we found it — that is j.posted ("found 44m ago").
const postedTxt = j => {
  if (j.posted_at) {
    const d = toDate(j.posted_at), thisYear = d.getUTCFullYear() === new Date().getUTCFullYear();
    return `posted ${d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', ...(thisYear ? {} : { year: 'numeric' }), timeZone: 'UTC' })} (${j.posted_ago})`;
  }
  return j.posted_raw ? `posted ${j.posted_raw}` : '';
};
// Where a posting's parse stands (app.services.job_description.DESCRIPTION_STATES). Parsed is the normal case, so
// only the others get a badge in lists.
const DESC_STATE = { PARSED: ['Parsed', 'good'], PARSING: ['Parsing…', 'brand'], FAILED: ['Parse failed', 'bad'],
  NOT_PARSED: ['Not parsed', 'muted'], NO_DESCRIPTION: ['No description', 'warn'] };
const descBadge = (state, always = false) => !state || (state === 'PARSED' && !always) ? ''
  : pill(DESC_STATE[state]?.[0] || state, DESC_STATE[state]?.[1] || 'muted');
const fmtDateTime = iso => iso ? toDate(iso).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';
const loading = () => '<div class="loading">Loading…</div>';
const empty = (title, body, actions = '') => `<div class="empty"><div class="empty-title">${esc(title)}</div><div class="muted small">${esc(body)}</div>${actions ? `<div class="row gap8 center wrap mt14">${actions}</div>` : ''}</div>`;
const btn = (text, act, data = {}, cls = '', disabled = false) =>
  `<button class="btn ${cls}" data-act="${act}" ${Object.entries(data).map(([k, v]) => `data-${k}="${attr(v)}"`).join(' ')} ${disabled ? 'disabled' : ''}>${text}</button>`;

/* form drafts survive re-renders */
const fv = (form, key, fallback) => (S.forms[form] && key in S.forms[form]) ? S.forms[form][key] : fallback;
const setF = (form, key, value) => { (S.forms[form] = S.forms[form] || {})[key] = value; if (form === 'sp') schedulePreview(); settingsTouched(form); };
function inputF(form, key, value, extra = '') { return `<input class="input" id="${form}-${key}" data-form="${form}" data-field="${key}" value="${attr(fv(form, key, value ?? ''))}" ${extra}>`; }
function textareaF(form, key, value, extra = '') { return `<textarea class="textarea" id="${form}-${key}" data-form="${form}" data-field="${key}" ${extra}>${esc(fv(form, key, value ?? ''))}</textarea>`; }
function checkF(form, key, value, text, extra = '') { const on = fv(form, key, !!value); return `<label class="check"><input type="checkbox" id="${form}-${key}" data-form="${form}" data-field="${key}" ${on ? 'checked' : ''} ${extra}>${text}</label>`; }
function selectF(form, key, value, options, extra = '') {
  const cur = String(fv(form, key, value));
  return `<select class="select" id="${form}-${key}" data-form="${form}" data-field="${key}" ${extra}>${options.map(([v, t]) => `<option value="${attr(v)}" ${String(v) === cur ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>`;
}
function chips(form, key, values, tone = '') {
  return (fv(form, key, values) || []).map((v, i) => `<span class="chip ${tone}">${esc(v)}<button type="button" aria-label="Remove ${attr(v)}" data-act="tag-remove" data-form="${form}" data-key="${key}" data-index="${i}">×</button></span>`).join('');
}
function tagBox(form, key, values, placeholder, max = 20, tone = '') {
  return `<div class="tagbox" data-tag-box="${key}" data-act="focus-tag" data-target="${form}-${key}">${chips(form, key, values, tone)}<input id="${form}-${key}" data-tag="${key}" data-form="${form}" data-max="${max}" ${LOCATION_KEYS.includes(key) ? 'data-split="newline"' : ''} autocomplete="off" placeholder="${attr(placeholder)}"></div>`;
}
// Locations keep their commas ("New York, NY"), so those inputs split on new lines only.
const LOCATION_KEYS = ['locations', 'preferred_locations'];
function addTags(form, key, raw, max, fallback, commas = !LOCATION_KEYS.includes(key)) {
  const list = [...(fv(form, key, fallback) || [])];
  for (const part of raw.split(commas ? /[,\n]/ : /\n/)) {
    const v = part.trim();
    if (!v || list.some(x => x.toLowerCase() === v.toLowerCase())) continue;
    if (list.length >= max) { toast(`You can add up to ${max}.`, true); break; }
    list.push(v);
  }
  setF(form, key, list);
}
function highlight(text, good, gaps) {
  const terms = [...good.map(t => [t, 'm-good']), ...gaps.map(t => [t, 'm-gap'])].filter(([t]) => t && t.length >= 3);
  const safe = esc(text);
  if (!terms.length) return safe;
  const cls = {}; terms.forEach(([t, c]) => { cls[esc(t).toLowerCase()] = cls[esc(t).toLowerCase()] || c; });
  const re = new RegExp('(' + terms.map(([t]) => esc(t).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).sort((a, b) => b.length - a.length).join('|') + ')', 'gi');
  return safe.replace(re, m => `<mark class="${cls[m.toLowerCase()] || 'm-good'}">${m}</mark>`);
}
// Scraped postings often arrive as escaped markdown (\\-, **bold**, * bullets); show them as plain text.
const unmark = line => line.replace(/\\([\\`*_{}\[\]()#+\-.!|>~])/g, '$1').replace(/^\s*[*-]\s+/, '• ');
const paragraphs = (text, good = [], gaps = []) => (text || '').split(/\n{1,}/).filter(l => l.trim()).map(l => `<p>${highlight(unmark(l), good, gaps).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/\*\*/g, '')}</p>`).join('') || '<p class="faint">No description captured.</p>';
function miniMarkdown(md) {
  const body = (md || '').split('## Source Resume')[0];
  let html = '', inList = false;
  for (const line of body.split('\n')) {
    if (line.startsWith('- ')) { if (!inList) { html += '<ul>'; inList = true; } html += `<li>${esc(line.slice(2))}</li>`; continue; }
    if (inList) { html += '</ul>'; inList = false; }
    if (line.startsWith('### ')) {
      const [title, ...rest] = line.slice(4).split('|').map(part => part.trim());
      html += `<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;justify-content:space-between"><span><b style="color:var(--ink)">${esc(title)}</b>${rest.length > 1 ? ` <span class="muted">· ${esc(rest.slice(0, -1).join(' · '))}</span>` : ''}</span><span class="muted">${esc(rest.length ? rest[rest.length - 1] : '')}</span></div>`;
    } else if (line.startsWith('# ')) html += `<h3>${esc(line.slice(2))}</h3>`;
    else if (line.startsWith('## ')) html += `<h4>${esc(line.slice(3))}</h4>`;
    else if (line.trim()) html += `<div>${esc(line)}</div>`;
  }
  return html + (inList ? '</ul>' : '');
}

/* ───────────── routing + loading ───────────── */
function setHash() {
  const sub = S.screen === 'discovery' ? '/' + S.discMode : S.screen === 'today' ? '/' + S.todayVariant : S.screen === 'settings' ? '/' + S.settingsSection : S.screen === 'knowledge' ? '/' + S.knowledgeSection : S.screen === 'workers' ? '/' + wkState().tab : S.screen === 'orchestration' && orchState().workspace !== 'pipeline' ? '/' + orchState().workspace : '';
  const next = '#' + S.screen + sub;
  if (location.hash !== next) history.replaceState(null, '', next);
}
function readHash() {
  const [screen, sub] = location.hash.replace(/^#/, '').split('/');
  // The ranking tracker moved from Discovery to Running workers; old links still land on it.
  if (screen === 'discovery' && sub === 'ranking') { S.screen = 'workers'; wkState().tab = 'ranking'; return; }
  // AI models moved from Settings to AI Orchestration → Models.
  if (screen === 'settings' && sub === 'ai') { S.screen = 'orchestration'; orchState().workspace = 'models'; return; }
  // Workflow & cadence moved to Running workers → Settings (approvals, tailoring, cadence); old links land there.
  if (screen === 'settings' && sub === 'workflow') { S.screen = 'workers'; wkState().tab = 'settings'; return; }
  if (screen === 'orchestration') orchState().workspace = ['models', 'langgraph'].includes(sub) ? sub : 'pipeline';
  if (SCREENS.includes(screen)) S.screen = screen;
  if (screen === 'workers' && WK_TABS.some(([id]) => id === sub)) wkState().tab = sub;
  if (screen === 'settings') S.settingsSection = settingsSection(sub);
  if (screen === 'knowledge') S.knowledgeSection = knowledgeSection(sub);
  if (screen === 'discovery' && ['triage', 'table', 'review', 'setup'].includes(sub)) S.discMode = sub;
  if (screen === 'today' && ['focus', 'run'].includes(sub)) S.todayVariant = sub;
}
function go(screen, extra = {}) {
  if (!settingsCanLeave(screen, extra.settingsSection || S.settingsSection)) return;
  // Opening one job's packet lands on that packet, not on the task list and filters above it (focusPacket).
  const tailorFocus = screen === 'tailor' && !!extra.tailorJob;
  Object.assign(S, { screen, editing: null, scheduling: false, editingRecruiter: false, tailorFocus }, extra);
  // Opening one application from elsewhere shows it, even when it is archived or the board's filters hide it.
  S.trackerReveal = screen === 'tracker' && extra.selApp ? Number(extra.selApp) : null;
  setHash(); window.scrollTo(0, 0); render(); load();
}
// Scroll the requested job's packet ("Tailoring for …") into view once it has loaded, then forget the request so
// later re-renders (task polling) never move the page again.
function focusPacket() {
  if (!S.tailorFocus || S.screen !== 'tailor' || !D.packet) return;
  S.tailorFocus = false;  // one attempt per navigation, whatever was found
  const target = document.getElementById('tailor-packet');
  if (target && D.packet.job?.id === S.tailorJob) target.scrollIntoView({ block: 'start', behavior: 'auto' });
}

const LOADERS = {
  async overview() { [D.overview, D.gateway] = await Promise.all([api('/api/workspace/overview'), api('/api/settings/ai/gateway').catch(() => null)]); },
  async today() { D.today = await api('/api/workspace/today'); },
  async discovery() {
    D.review = await api('/api/discovery/review').catch(() => null);
    if (S.discMode === 'review') return;
    if (S.discMode === 'triage') await loadQueue();
    else if (S.discMode === 'table') await loadTable();
    else await loadSetup();
  },
  async tailor() {
    [D.tailorFacets] = await Promise.all([api('/api/tailor/queue?' + qs({ ...S.pf.tailor, with_facets: true })), refreshTasks()]);
    D.tailorQueue = D.tailorFacets.items;
    D.packet = null;
    // A job opened from elsewhere (Discovery, Today, Approvals) or hidden by the current filter is still
    // shown: asking for a specific job must never land on a different one.
    if (S.tailorJob && !D.tailorQueue.some(j => j.id === S.tailorJob)) {
      const p = await api(`/api/tailor/${S.tailorJob}`).catch(() => null);
      if (p) D.packet = p; else S.tailorJob = null;
    }
    if (!S.tailorJob) S.tailorJob = (D.tailorQueue.find(j => j.has_resume) || D.tailorQueue[0] || {}).id || null;
    if (!D.packet && S.tailorJob) D.packet = await api(`/api/tailor/${S.tailorJob}`);
    if (!D.knowledge) D.knowledge = await api('/api/knowledge');
  },
  async approvals() {
    D.approvalFacets = await api('/api/approvals?' + qs({ ...S.pf.approvals, with_facets: true }));
    D.approvals = D.approvalFacets.items;
    if (!S.approvalJob || !D.approvals.some(j => j.id === S.approvalJob)) S.approvalJob = (D.approvals[0] || {}).id || null;
    D.approval = S.approvalJob ? await api(`/api/approvals/${S.approvalJob}`) : null;
    if (S.approvalJob) await loadInsights(S.approvalJob).catch(() => null);
  },
  async tracker() {
    D.tracker = await api('/api/tracker');
    const apps = D.tracker.applications;
    if (!apps.some(a => a.id === S.selApp)) S.selApp = (apps[0] || {}).id || null;
    revealTrackerApp();
  },
  knowledge: loadKnowledge,
  async monitoring() { await loadMonitoring(); },
  async orchestration() { await loadOrchestration(); },
  async workers() { await loadWorkers(); },
  async settings() {
    D.settingsLoadError = '';
    try {
      [D.profile, D.settings, D.prefs] = await Promise.all([api('/api/profile'), api('/api/settings'), api('/api/preferences')]);
    } catch (err) { D.settingsLoadError = err.message; throw err; }
  },
};
async function loadQueue() {
  const t = S.triage;
  D.queue = await api('/api/jobs/search?' + qs({ queue: true, per_page: 60, min_fit: t.min_fit || '', location: t.location, hide_agencies: t.hide_agencies, hide_reposts: t.hide_reposts, keywords: t.keywords.join(',') }));
  D.queueTotal = D.queue.total;
  if (!D.setups) D.setups = (await api('/api/search/setups')).setups;
  if (!D.runs) D.runs = await api('/api/search/apify/runs');
}
async function loadResumeChoices() {
  if (!D.resumeChoices) {
    const out = await api('/api/resumes/choices').catch(() => ({ items: [], catalog: [] }));
    [D.resumeChoices, D.resumeCatalog] = [out.items, out.catalog || []];
  }
  return D.resumeChoices;
}
const tableQuery = (f, extra = {}) => qs({ ...f, min_fit: f.min_fit || '', max_fit: f.max_fit || '', keywords: (f.keywords || []).join(','), ...extra });
async function loadTable() {
  const f = S.filters;
  try { localStorage.setItem(FILTERS_KEY, JSON.stringify(f)); } catch { /* storage blocked: filters last this page */ }
  [D.jobs] = await Promise.all([api('/api/jobs/search?' + tableQuery(f)), loadResumeChoices()]);
  if (!D.jobs.items.some(j => j.id === S.selJob)) S.selJob = (D.jobs.items[0] || {}).id || null;
  D.selDetail = S.selJob ? await api(`/api/jobs/${S.selJob}`) : null;
}
async function loadSetup() {
  const [setups, runs, actors] = await Promise.all([api('/api/search/setups'), api('/api/search/apify/runs'), api('/api/search/actors'), loadResumeChoices()]);
  [D.setups, D.runs, D.actors] = [setups.setups, runs, actors];
  const sel = curSetup();
  if (sel?.id !== S.setupSel) { S.setupSel = sel?.id ?? null; delete S.forms.sp; }
  // The form below edits the selected setup's settings, so the rest of the screen needs no other source.
  D.searchProfile = { profile: sel ? sel.settings : {}, board_yield: setups.board_yield, apify_configured: setups.apify_configured };
  schedulePreview();
  if (!D.runs.some(r => r.id === S.runSel)) S.runSel = (D.runs[0] || {}).id || null;
  D.runDetail = S.runSel ? await api(`/api/search/apify/runs/${S.runSel}/detail`) : null;
  // Runs left "ready" after a reload are checked once per visit, quietly.
  if (!S.autoRefreshed && !S.polling && D.searchProfile.apify_configured && D.runs.some(r => !r.completed_at)) {
    S.autoRefreshed = true;
    api('/api/search/apify/runs/refresh-pending', { method: 'POST' }).then(async out => {
      if (!out.results.some(r => r.ok && r.completed)) return;
      await afterPull(out.results);
      if (S.screen === 'discovery' && S.discMode === 'setup') { render(); toast(summarizePull(out.results)); }
    }).catch(() => {});
  }
}
function renderApifyImport() {
  const remote = D.remoteRuns, chosen = S.importPick;
  if (!remote) return `<div class="small muted" style="padding:14px 16px;background:#fbfbfe;border-bottom:1px solid var(--line2)">${isBusy('remoteRuns') ? 'Loading runs from your Apify account…' : 'Could not load Apify runs.'}</div>`;
  return `<div style="padding:12px 16px;background:#fbfbfe;border-bottom:1px solid var(--line2)">
    <div class="xs muted mb8">Runs in your Apify account, newest first — including ones started outside Job Hunt OS. Imported jobs are normalized and deduplicated against everything already in your pipeline.</div>
    ${remote.length ? remote.map(r => `<label class="row gap9 top-align" style="padding:8px 0;border-bottom:1px solid var(--line3);cursor:${r.importable ? 'pointer' : 'default'}">
      <input type="checkbox" data-act="pick-remote" data-id="${attr(r.external_run_id)}" ${chosen.has(r.external_run_id) ? 'checked' : ''} ${r.importable ? '' : 'disabled'} style="margin-top:3px;accent-color:var(--brand)">
      <span class="grow" style="min-width:0"><span class="small b ellipsis" style="display:block">${esc(r.actor_title)}</span>
        <span class="tiny muted" style="display:block"><span class="mono">${esc(r.actor_id)}</span> · ${fmtDateTime(r.started_at)}${r.item_count != null ? ` · ${r.item_count} items` : ''}${r.origin ? ` · ${esc(r.origin.toLowerCase())}` : ''}</span></span>
      <span class="col gap4 none" style="align-items:flex-end">${pill(r.status.toLowerCase(), r.status === 'SUCCEEDED' ? 'good' : ['READY', 'RUNNING'].includes(r.status) ? 'warn' : 'bad')}${r.local_run_id ? `<span class="tiny faint">${r.imported ? 'imported' : 'tracking'} · #${r.local_run_id}</span>` : ''}</span></label>`).join('')
      : '<div class="small muted">No runs found in your Apify account.</div>'}
    <div class="row gap8 wrap mt10">${btn(label('importRuns', chosen.size ? `Import ${chosen.size} selected` : 'Select runs to import', 'Importing…'), 'import-remote', {}, 'sm primary', !chosen.size || isBusy('importRuns'))}${btn(label('remoteRuns', 'Reload', 'Loading…'), 'load-remote', {}, 'sm', isBusy('remoteRuns'))}
      <span class="tiny faint grow">Already imported? Open the run and use Pull again.</span></div></div>`;
}
// Ranking runs on the server: imports queue new jobs and the ranking worker scores them in the background.
// After queueing, poll the jobs' ranking tasks for a while and refresh once none are waiting or running.
async function watchRanking(ids) {
  const watched = ids.slice(0, 200);
  if (!watched.length) return;
  for (let i = 0; i < 40; i++) {
    await new Promise(r => setTimeout(r, 3000));
    const out = await api(`/api/ranking/tasks?job_ids=${watched.join(',')}`).catch(() => null);
    if (!out || !out.active) break;
  }
  if (S.jobOpen) D.jobModal = await api(`/api/jobs/${S.jobOpen}`).catch(() => D.jobModal);
  await load();
}
function parseQueuedMsg(out) {
  const n = out.queued.length, why = [...new Set(out.skipped.map(s => s.reason))].join(', ');
  return `${n ? `Queued ${n} job${n === 1 ? '' : 's'} for parsing — ranking follows` : 'Nothing to parse'}${out.skipped.length ? ` · ${out.skipped.length} skipped (${why})` : ''}`;
}
// After parses are queued: re-read the jobs wherever they are shown, then keep watching a few of them until their
// parse ends (the worker runs in the background), so "Parsing…" turns into Rank, Tailor or Parse again on its own.
async function refreshParsed(id) {
  const fresh = await api(`/api/jobs/${id}`).catch(() => null);
  if (fresh && D.selDetail?.id === id) D.selDetail = fresh;
  if (fresh && D.triageDetail?.id === id) D.triageDetail = fresh;
  if (fresh && D.jobModal?.id === id) D.jobModal = fresh;
  return fresh;
}
async function watchParsing(ids) {
  let watched = ids.slice(0, 10);
  for (let i = 0; i < 40 && watched.length; i++) {
    await new Promise(r => setTimeout(r, 3000));
    const fresh = await Promise.all(watched.map(refreshParsed));
    watched = watched.filter((id, n) => !fresh[n] || fresh[n].description_state === 'PARSING');
    if (watched.length) render();
  }
  await load();
}
function rankQueuedMsg(out) {
  const n = out.queued.length;
  return `${n ? `Queued ${n} job${n === 1 ? '' : 's'} for ranking` : 'Nothing new to rank'}${out.skipped.length ? ` · ${out.skipped.length} skipped` : ''}${out.blocked ? ` · waiting: ${out.blocked}` : ''}`;
}
function recoverQueuedMsg(out) {
  const n = out.queued;
  const notes = [[out.already_queued, 'already queued'], [out.hidden_by_domain, 'hidden by your domain filter left out'],
    [out.held_for_review, 'waiting on the domain check left out'], [out.set_aside, 'you set aside left out'], [out.no_url, 'with no posting URL left out'],
    [out.over_limit, 'more: click again once these finish']].filter(([count]) => count).map(([count, text]) => `${count} ${text}`);
  return [n ? `Queued ${n} job${n === 1 ? ' to read from its posting' : 's to read from their postings'}` : 'Nothing new to fetch', ...notes].join(' · ');
}
async function afterPull(results) {
  const ok = results.filter(r => r.ok && r.id);
  if (ok[0]) S.runSel = ok[0].id;
  await loadSetup();
  api('/api/workspace/summary').then(s => { D.summary = s; render(); }).catch(() => {});
}
function summarizePull(results) {
  const ok = results.filter(r => r.ok), failed = results.filter(r => !r.ok);
  if (!ok.length && !failed.length) return 'No runs were waiting';
  const created = ok.reduce((a, r) => a + (r.import?.created || 0), 0), dup = ok.reduce((a, r) => a + (r.import?.duplicates || 0), 0);
  const running = ok.filter(r => !r.completed).length;
  return [`${created} new job${created === 1 ? '' : 's'}`, dup ? `${dup} duplicate${dup === 1 ? '' : 's'} skipped` : '',
    running ? `${running} still running` : '', failed.length ? `${failed.length} failed: ${failed[0].error}` : ''].filter(Boolean).join(' · ');
}
let loadSeq = 0;
async function load() {
  const seq = ++loadSeq;
  try {
    await Promise.all([LOADERS[S.screen](), api('/api/workspace/summary').then(s => { D.summary = s; announceApprovals(s); })]);
  } catch (e) { toast(e.message, true); }
  if (seq === loadSeq) { render(); focusPacket(); guideRefresh(); }
}

/* ───────────── shell ───────────── */
const NAV = [['overview', 'Overview', null], ['today', 'Today', 'today'], ['discovery', 'Discovery', 'discovery'], ['tailor', 'Tailor', 'tailor'], ['approvals', 'Approvals', 'approvals'], ['tracker', 'Tracker', 'tracker']];
// How the system runs, separate from the work itself: AI Orchestration (topology and live packets) and Monitoring
// (detailed telemetry) are two workspaces of their own.
const NAV_SYSTEM = [['orchestration', 'AI Orchestration', null], ['workers', 'Running workers', null], ['monitoring', 'Monitoring', null]];
const NAV2 = [['knowledge', 'Knowledge bank'], ['settings', 'Settings & profile']];
function renderNav() {
  const c = D.summary?.counts || {};
  const count = (id, key) => key ? c[key] ?? '·' : id === 'workers' && D.wk ? D.wk.active : '—';
  const item = ([id, text, key]) => `<a class="nav-btn ${S.screen === id ? 'on' : ''}" href="#${id}" data-act="go" data-screen="${id}"><span>${text}</span><span class="nav-count">${count(id, key)}</span></a>`;
  return `<div class="nav-group"><div class="nav-caps">WORKSPACE</div>${NAV.map(item).join('')}</div>
    <div class="nav-group"><div class="nav-caps">SYSTEM</div>${NAV_SYSTEM.map(item).join('')}</div>
    <div class="nav-group"><div class="nav-caps">SETUP</div>${guideNavItem()}${NAV2.map(([id, text]) => `<a class="nav-btn ${S.screen === id ? 'on' : ''}" href="#${id}" data-act="go" data-screen="${id}"><span>${text}</span></a>`).join('')}</div>`;
}
function renderSideFoot() {
  const account = BOOT.user ? `<div class="side-card"><div class="side-card-title ellipsis" title="${attr(BOOT.user.email)}">${esc(BOOT.user.email)}</div>
    <div class="side-card-body">${btn(label('logout', 'Sign out', 'Signing out…'), 'logout', {}, 'sm', isBusy('logout'))}</div></div>` : '';
  const s = D.summary; if (!s) return account;
  const a = s.auto_search || {};  // a partial summary must never break the whole shell
  const on = a.setups > 0;
  const last = a.last_run ? `Last run ${fmtDay(a.last_run)} · ${a.last_run_status.toLowerCase()}.` : 'No searches have run yet.';
  return `<div class="side-card"><div class="side-card-title"><span class="dot" style="background:${on ? '#4ade80' : '#6a7388'}"></span>Auto-search ${on ? 'on' : 'off'}</div>
    <div class="side-card-body">${on ? `${a.setups} scheduled setup${a.setups === 1 ? '' : 's'} · ${a.roles} role${a.roles === 1 ? '' : 's'} × ${Math.max(1, a.locations)} location${a.locations === 1 ? '' : 's'}${a.next_run ? `, next ${fmtDay(a.next_run)}` : ''}.` : 'Searches run when you start them.'} ${esc(last)}</div></div>${account}`;
}
function screenMeta() {
  const s = D.summary, c = s?.counts || {};
  const now = new Date();
  return {
    overview: ['Overview', D.overview ? `The whole hunt on one screen · ${D.overview.total} discovered in 30 days, ${c.tracker ?? 0} live` : 'The whole hunt on one screen'],
    today: ['Today', `${now.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })} · everything that needs you, in order`],
    discovery: ['Discovery', `${s?.total_jobs ?? 0} in pipeline · ${c.discovery ?? 0} waiting for triage`],
    tailor: ['Tailor & review', 'AI drafts, you approve every claim'],
    approvals: ['Approvals', 'Nothing leaves this queue without your sign-off'],
    tracker: ['Tracker', D.tracker ? `${D.tracker.stats.live} live applications · ${D.tracker.stats.due} follow-ups due` : 'Every submitted application'],
    workers: ['Running workers', 'Background work for your account: JD parsing, JD recovery, ranking and tailoring · kill a stuck task or run one now'],
    orchestration: ['AI Orchestration', 'Live system topology, models and packet execution · what is running, where each job is and which model handles it'],
    monitoring: ['Monitoring', 'Every LLM request: tokens, context, latency, cost and failures · recommendations never change anything automatically'],
    knowledge: ['Knowledge bank', 'Answer once, reuse in every ATS form'],
    settings: ['Settings', 'A workspace that works your way'],
  }[S.screen];
}
function renderTop() {
  const [title, sub] = screenMeta();
  let seg = '';
  if (S.screen === 'today') seg = [['focus', 'Command centre'], ['run', 'Run sheet']].map(([id, t]) => `<button class="${S.todayVariant === id ? 'on' : ''}" data-act="variant" data-kind="today" data-id="${id}">${t}</button>`).join('');
  if (S.screen === 'tailor') seg = [['single', 'One job'], ['bulk', 'Bulk tailor']].map(([id, t]) => `<button class="${S.tailorMode === id ? 'on' : ''}" data-act="variant" data-kind="tailor" data-id="${id}">${t}</button>`).join('');
  if (S.screen === 'discovery') seg = [['triage', 'Triage queue'], ['table', 'Table + detail'], ...(D.review?.total || S.discMode === 'review' ? [['review', `Needs review${D.review?.total ? ` ${D.review.total}` : ''}`]] : []), ['setup', 'Search setup']].map(([id, t]) => `<button class="${S.discMode === id ? 'on' : ''}" data-act="variant" data-kind="discovery" data-id="${id}">${t}</button>`).join('');
  return `<div class="top-row"><div><h1>${esc(title)}</h1><div class="sub">${esc(sub)}</div></div>
    <div class="row gap8 wrap">${seg ? `<div class="seg">${seg}</div>` : ''}
      <button class="btn" data-act="palette">Search · ⌘K</button>
      ${S.screen === 'settings' ? '' : '<button class="btn primary" data-act="go" data-screen="discovery" data-mode="setup">New search</button>'}</div></div>`;
}

/* ───────────── overview ───────────── */
// Where every AI worker sends its requests: the LiteLLM gateway, or the direct provider chosen in AI Orchestration →
// Models (with its model). Shared by the Overview screen and AI Orchestration → Models; `g` is /api/settings/ai/gateway.
function gatewaySegment(g) {
  const option = (on, name, ready, why) => `<button type="button" class="${on ? 'on' : ''}" data-act="${on ? '' : 'set-gateway'}" data-on="${name === 'litellm' ? '1' : ''}"
    ${!on && (!ready || isBusy('gateway')) ? `disabled title="${attr(ready ? 'Switching…' : why)}"` : ''} aria-pressed="${on}">${esc(name === 'litellm' ? 'LiteLLM gateway' : `${g.direct_label} (direct)`)}</button>`;
  return `<div class="seg" role="group" aria-label="AI gateway">${option(g.gateway_enabled, 'litellm', g.litellm_ready, 'Save a LiteLLM base URL in AI Orchestration → Models first')}${option(!g.gateway_enabled, g.direct_provider, g.direct_ready, `Save a ${g.direct_label} API key in Settings → API keys first`)}</div>`;
}
function gatewayNow(g) {
  return g.gateway_enabled
    ? `Every AI worker sends its requests through LiteLLM${g.litellm_base_url ? ` at ${g.litellm_base_url}` : ''}, using the models set per worker.`
    : `Every AI worker calls ${g.direct_label} directly${g.direct_model ? `, default model ${g.direct_model}` : ''}.`;
}
function gatewayMissing(g) {
  const missing = !g.litellm_ready ? 'LiteLLM needs a base URL first (AI Orchestration → Models → LiteLLM connection).'
    : !g.direct_ready ? `${g.direct_label} needs an API key first (Settings → API keys).` : '';
  return missing ? `<div class="xs muted mt8">${esc(missing)} <a href="${!g.litellm_ready ? '#orchestration/models' : '#settings/api-keys'}">${!g.litellm_ready ? 'Open Models' : 'Open settings'}</a></div>` : '';
}
function renderGatewaySwitch() {
  const g = D.gateway; if (!g) return '';
  return `<section class="card"><div class="row base between wrap gap12"><div style="min-width:0"><div class="card-title">AI gateway</div>
      <div class="card-sub">${esc(gatewayNow(g))}</div></div>${gatewaySegment(g)}</div>${gatewayMissing(g)}</section>`;
}

function renderOverview() {
  const o = D.overview; if (!o) return loading();
  const perf = o.source_perf;
  const withApplied = perf.filter(b => b.applied);
  const best = [...withApplied].sort((a, b) => parseFloat(b.rate) - parseFloat(a.rate))[0];
  const dead = perf.find(b => b.found >= 10 && b.applied >= 2 && b.replies === 0);
  const insight = !perf.length ? 'Run a search to see which boards pay off.' : best
    ? `${best.board} replies ${best.rate} of the time.${dead ? ` ${dead.board} has returned ${dead.found} jobs and zero replies — worth pausing.` : ''}`
    : 'No applications yet — reply rates appear once you apply.';
  const t = o.time_in_stage;
  const days = v => v == null ? '—' : `${v} days`;
  const bottleneck = o.stale_approved ? `${o.stale_approved} approved packet${o.stale_approved === 1 ? ' has' : 's have'} sat unsubmitted for over 2 days.`
    : o.approvals ? `${o.approvals} packet${o.approvals === 1 ? ' is' : 's are'} waiting for your approval.` : 'Nothing is stuck right now.';
  return `<div class="stack">${renderGatewaySwitch()}
  <section class="card"><div class="row base between wrap mb16"><div><div class="card-title">Pipeline health</div><div class="card-sub">Where ${o.total} discovered job${o.total === 1 ? '' : 's'} actually ended up. Click a stage to work it.</div></div><span class="small muted">last 30 days</span></div>
    <div class="row wrap gap10">${o.funnel.map(f => `<button class="stage-btn" data-act="go" data-screen="${f.screen}"><div class="stage-label">${esc(f.label)}</div><div class="mono big">${f.n}</div>${bar(f.pct, f.color)}<div class="tiny faint mt6">${esc(f.conv) || '&nbsp;'}</div></button>`).join('')}</div></section>
  <div class="split gap16">
    <section class="card" style="flex:1 1 380px;min-width:0"><div class="card-title mb4">Where each board earns its keep</div><div class="card-sub mb14">Reply rate is what matters — not how many jobs a board returns.</div>
      <div class="tgrid boards thead"><span>BOARD</span><span class="tr">FOUND</span><span class="tr">APPLIED</span><span class="tr">REPLIES</span><span class="tr">REPLY RATE</span></div>
      ${perf.length ? perf.map(b => `<div class="tgrid boards trow"><span class="b base">${esc(b.board)}</span><span class="tr mono small muted">${b.found}</span><span class="tr mono small muted">${b.applied}</span><span class="tr mono small muted">${b.replies}</span><span class="tr mono base bb">${esc(b.rate)}</span></div>`).join('') : `<div class="small muted mt12">No jobs discovered in the last 30 days.</div>`}
      <div class="small muted mt12">${esc(insight)}</div></section>
    <section class="card" style="flex:1 1 280px;min-width:0"><div class="card-title mb14">Next 10 days</div>
      <div class="col gap9">${o.upcoming.length ? o.upcoming.map(u => `<div class="box row gap12"><span class="mono small b muted none">${fmtDay(u.date)}</span><span class="small grow pretty" style="font-weight:500">${esc(u.label)}</span>${pill(u.kind, u.kind === 'interview' ? 'good' : 'warn', 'none')}</div>`).join('') : '<div class="small muted">No follow-ups or interviews scheduled.</div>'}</div>
      <button class="btn block mt12" data-act="go" data-screen="tracker">Open tracker</button></section>
  </div>
  <div class="split gap16">
    <section class="card" style="flex:1 1 300px;min-width:0"><div class="card-title mb4">Applications per week</div><div class="card-sub mb16">Target is ${o.weekly_target} · six-week trend</div>
      <div class="vbars">${o.weeks.map(w => `<div class="vbar" title="${w.n} applied"><span class="tiny faint tc">${w.n || ''}</span><i style="height:${w.pct}"></i><div class="tiny faint tc mt6">${esc(w.label)}</div></div>`).join('')}</div></section>
    <section class="card" style="flex:1 1 280px;min-width:0"><div class="card-title mb14">What the searches are returning</div>
      <div class="col gap12">${o.role_mix.length ? o.role_mix.map((r, i) => `<div><div class="row between small mb4"><span style="font-weight:500">${esc(r.label)}</span><span class="mono muted">${r.n}</span></div>${bar(r.pct, ['#5b4ae8', '#a79bf0', '#c6cad8', '#e0a136'][i], 'thick')}</div>`).join('') : '<div class="small muted">Nothing discovered yet.</div>'}</div>
      <div class="small muted mt14" style="padding-top:14px;border-top:1px solid var(--line2)">Median fit of new jobs: <b>${o.median_fit ?? '—'}</b>.${o.median_fit != null && o.median_fit < 60 ? ' Tighten the search profile if that keeps falling.' : ''}</div></section>
    <section class="card dark" style="flex:1 1 260px;min-width:0"><div class="card-title mb14" style="color:#cfd5e4">Time in stage</div>
      <div class="col gap12">
        <div><div class="small" style="color:#8a92a8">Discovered → triaged</div><div class="mono bb" style="font-size:19px">${days(t.triage)}</div></div>
        <div><div class="small" style="color:#8a92a8">Shortlisted → applied</div><div class="mono bb" style="font-size:19px">${days(t.apply)}</div></div>
        <div><div class="small" style="color:#8a92a8">Applied → first reply</div><div class="mono bb" style="font-size:19px">${days(t.reply)}</div></div></div>
      <div class="small mt16" style="color:#8a92a8;line-height:1.5;padding-top:14px;border-top:1px solid #252c42">${esc(bottleneck)}</div>
      <button class="btn primary block mt12" data-act="go" data-screen="approvals">Clear the approval queue</button></section>
  </div>${renderOutcomes(o)}</div>`;
}

// Applications grouped by location, role or fit band: rows from _outcome_rows in app/workspace.py.
function outcomeTable(rows, keyLabel) {
  if (!rows.length) return '<div class="small muted mt12">No applications sent in this window yet.</div>';
  return `<div class="tgrid outcomes thead"><span>${esc(keyLabel)}</span><span class="tr">APPLIED</span><span class="tr">REPLIES</span><span class="tr">INTERVIEWS</span><span class="tr" title="Reply rate">RATE</span></div>
    ${rows.map(r => `<div class="tgrid outcomes trow"><span class="b base ellipsis" title="${attr(r.label)}">${esc(r.label)}</span><span class="tr mono small muted">${r.applied}</span><span class="tr mono small muted">${r.replies}</span><span class="tr mono small muted">${r.interviews}</span><span class="tr mono base bb">${esc(r.rate)}</span></div>`).join('')}`;
}
// One line on which group answers best and which has gone quiet, once there is enough to say so.
function outcomeInsight(rows, noun) {
  const named = rows.filter(r => r.label !== 'Other' && r.label !== 'Other roles' && r.label !== 'Not listed');
  const best = named.filter(r => r.applied >= 3 && r.replies).sort((a, b) => b.replies / b.applied - a.replies / a.applied)[0];
  const quiet = named.find(r => r.applied >= 5 && !r.replies);
  if (!best && !quiet) return rows.length ? `Reply rates by ${noun} firm up after a few applications each.` : '';
  return [best && `${best.label} replies most (${best.rate} of ${best.applied}).`,
    quiet && `${quiet.label}: ${quiet.applied} applications, no replies yet.`].filter(Boolean).join(' ');
}
function renderOutcomes(o) {
  const out = o.outcomes, span = `last ${o.outcome_days} days`;
  const tableCard = (title, sub, rows, key, noun) => `<section class="card" style="flex:1 1 380px;min-width:0"><div class="row base between wrap"><div class="card-title mb4">${esc(title)}</div><span class="small muted">${span}</span></div><div class="card-sub mb14">${esc(sub)}</div>
      ${outcomeTable(rows, key)}${rows.length ? `<div class="small muted mt12">${esc(outcomeInsight(rows, noun))}</div>` : ''}</section>`;
  const ended = `<section class="card" style="flex:1 1 380px;min-width:0"><div class="row base between wrap"><div class="card-title mb4">How applications ended</div><span class="small muted">${span}</span></div>
      <div class="card-sub mb14">${out.total ? `${out.response_rate} heard back from the company — ${out.answered} of ${out.total}, rejections included.` : 'Where every application sent stands now.'}</div>
      ${out.total ? `<div class="stackbar">${out.buckets.map(b => `<i style="width:${attr(b.pct)};background:${attr(b.color)}" title="${attr(`${b.label}: ${b.n}`)}"></i>`).join('')}</div>
      <div class="col gap8 mt14">${out.buckets.map(b => `<div class="row between small"><span class="row gap8"><span class="dot" style="background:${attr(b.color)}"></span>${esc(b.label)}</span><span class="mono muted">${b.n} · ${esc(b.pct)}</span></div>`).join('')}</div>`
      : '<div class="small muted">No applications sent in the last ' + o.outcome_days + ' days.</div>'}</section>`;
  const fitRows = o.fit_vs_outcome;
  const fit = `<section class="card" style="flex:1 1 280px;min-width:0"><div class="card-title mb4">Does fit predict replies?</div><div class="card-sub mb14">Reply rate by the ranker's fit score.</div>
      <div class="col gap12">${fitRows.length ? fitRows.map(r => `<div><div class="row between small mb4"><span style="font-weight:500">${esc(r.label)}</span><span class="mono muted">${r.replies}/${r.applied} · ${esc(r.rate)}</span></div>${bar(r.rate === '—' ? '0%' : r.rate, '#0f9c86', 'thick')}</div>`).join('') : '<div class="small muted">No applications sent yet.</div>'}</div></section>`;
  const inbox = o.inbox
    ? `<section class="card" style="flex:1 1 280px;min-width:0"><div class="row base between wrap"><div class="card-title mb4">Company emails</div><span class="small muted">last 30 days</span></div><div class="card-sub mb14">${o.inbox.total} email${o.inbox.total === 1 ? '' : 's'} matched to your applications by the Application Inbox.</div>
      <div class="statgrid">${o.inbox.groups.map(g => `<div class="box"><div class="mono bb" style="font-size:19px">${g.n}</div><div class="tiny muted">${esc(g.label)}</div></div>`).join('')}</div>
      ${o.inbox.needs_action ? `<button class="btn block mt12" data-act="go" data-screen="tracker" data-view="inbox">${o.inbox.needs_action} email${o.inbox.needs_action === 1 ? ' needs' : 's need'} a reply or action</button>` : ''}</section>`
    : `<section class="card" style="flex:1 1 280px;min-width:0"><div class="card-title mb4">Company emails</div><div class="small muted">Connect Gmail in Settings → Application Inbox to count interview invites, assessments and rejections here.</div></section>`;
  const passed = o.pass_reasons.length ? `<section class="card" style="flex:1 1 280px;min-width:0"><div class="card-title mb4">Why you pass on jobs</div><div class="card-sub mb14">Reasons given when turning jobs down, last 30 days. Tighten the search setup for the top one.</div>
      <div class="col gap12">${o.pass_reasons.map((r, i) => `<div><div class="row between small mb4"><span style="font-weight:500">${esc(r.label)}</span><span class="mono muted">${r.n}</span></div>${bar(r.pct, ['#c22f4e', '#e0a136', '#a79bf0', '#c6cad8'][i], 'thick')}</div>`).join('')}</div></section>` : '';
  return `<div class="split gap16">${tableCard('Where companies reply', 'Replies by job location. Remote roles are grouped together.', o.by_location, 'LOCATION', 'location')}
    ${tableCard('Which roles get replies', 'Replies by your target roles, then by role type (AI/ML Engineer, Software Engineer…) read from the job title. Levels like Senior or Staff are grouped together.', o.by_role, 'ROLE', 'role')}</div>
  <div class="split gap16">${ended}${fit}</div>
  <div class="split gap16">${inbox}${passed}</div>`;
}

/* ───────────── today ───────────── */
const ACTION_META = {
  approve: { tag: 'APPROVE', tone: 'warn', cta: 'Review', av: '' },
  tailor: { tag: 'TAILOR', tone: 'brand', cta: 'Tailor', av: 'good' },
  follow_up: { tag: 'FOLLOW UP', tone: 'good', cta: 'Open', av: 'warn' },
};
function renderToday() {
  const t = D.today; if (!t) return loading();
  if (S.todayVariant === 'run') return renderRunSheet(t);
  const minutes = t.actions.slice(0, 3).reduce((a, x) => a + x.minutes, 0);
  const w = t.week;
  return `<div class="split">
  <div class="c-main stack" style="gap:18px">
    <section class="card flush"><div class="card-head row between"><div><div class="card-title">Do this next</div><div class="card-sub">Ordered by stage, then fit.${t.actions.length ? ` The top ${Math.min(3, t.actions.length)} take ≈ ${minutes} minutes.` : ''}</div></div>${pill('ranked', 'brand')}</div>
      ${t.actions.length ? t.actions.map(a => { const m = ACTION_META[a.kind]; return `<div class="lrow" style="padding:15px 18px;gap:14px"><div class="avatar ${m.av}">${esc(initials(a.company))}</div>
        <div class="grow"><div class="row gap8 wrap"><span class="b" style="font-size:14px">${esc(a.company)}</span>${pill(m.tag, m.tone)}</div><div class="small muted mt4 pretty">${esc(a.title)} — ${esc(a.reason)}</div></div>
        <div class="tr none"><div class="mono bb ${fitCls(a.fit)}" style="font-size:19px">${fitTxt(a.fit)}</div><div class="caps-sm faint">FIT</div></div>
        ${btn(m.cta, 'do-action', { kind: a.kind, job: a.job_id, app: a.application_id || '' }, 'primary none')}</div>`; }).join('')
        : empty('Nothing needs you right now', 'Shortlist jobs in Discovery and they will show up here.')}
      <button class="linkbtn" data-act="go" data-screen="discovery" data-mode="triage">${t.pending_triage ? `Triage ${t.pending_triage} new job${t.pending_triage === 1 ? '' : 's'} →` : 'Start a new search →'}</button></section>
    <section class="card pad18"><div class="row base between mb14"><div class="card-title">Follow-ups due</div><a href="#tracker" data-act="go" data-screen="tracker">Open tracker</a></div>
      <div class="col gap9">${t.followups.length ? t.followups.map(f => {
        const c = { overdue: ['#c22f4e', '#fffafb', 'var(--bad)'], today: ['#e0a136', '#fffdf9', 'var(--warn)'], soon: ['#c6cad8', '#fff', 'var(--muted)'] }[f.tone];
        return `<div class="box row gap12" style="background:${c[1]};padding:12px 14px"><div style="width:3px;align-self:stretch;border-radius:99px;background:${c[0]}"></div>
        <div class="grow"><div class="b" style="font-size:13.5px">${esc(f.company)} · ${esc(f.role)}</div><div class="small muted mt4">${esc(f.detail)}</div></div>
        <span class="xs b none" style="color:${c[2]}">${esc(f.due)}</span>${btn(label('nudge' + f.application_id, 'Draft nudge', 'Drafting…'), 'draft-nudge', { app: f.application_id }, 'sm none', isBusy('nudge' + f.application_id))}</div>`; }).join('') : '<div class="small muted">No follow-ups due in the next 3 days.</div>'}</div></section>
  </div>
  <div class="c-side stack" style="gap:18px">
    <section class="card dark pad18"><div class="base b" style="color:#cfd5e4">This week</div>
      <div class="row gap10" style="align-items:flex-end;margin:10px 0 4px"><span class="mono bb" style="font-size:40px;line-height:1">${w.applied}</span><span class="base" style="color:#8a92a8;padding-bottom:6px">of ${w.target} applications</span></div>
      <div class="bar fat" style="margin:12px 0 14px"><i style="width:${Math.min(100, 100 * w.applied / w.target)}%;background:var(--brand)"></i></div>
      <div class="grid-auto" style="grid-template-columns:repeat(3,1fr);gap:10px">
        <div><div class="mono bb" style="font-size:17px">${w.replies}</div><div class="tiny" style="color:#8a92a8">Replies</div></div>
        <div><div class="mono bb" style="font-size:17px">${w.interviews}</div><div class="tiny" style="color:#8a92a8">Interviews</div></div>
        <div><div class="mono bb" style="font-size:17px">${esc(w.response_rate)}</div><div class="tiny" style="color:#8a92a8">Response</div></div></div></section>
    <section class="card pad18"><div class="card-title mb4">Funnel</div><div class="small muted mb14">Last 30 days</div>
      <div class="col gap9">${t.funnel.map(f => `<div><div class="row between small mb4"><span style="font-weight:500">${esc(f.label)}</span><span class="mono muted">${f.n}</span></div>${bar(f.pct, f.color, 'thick')}</div>`).join('')}</div></section>
    <section class="card pad18"><div class="card-title mb12">Needs your attention</div><div class="col gap9">
      <button class="box warn row gap12" style="cursor:pointer;text-align:left;width:100%" data-act="go" data-screen="approvals"><span class="mono bb" style="font-size:18px;color:var(--warn)">${t.approvals}</span><span class="small" style="color:var(--warn-deep);line-height:1.4">tailored packet${t.approvals === 1 ? '' : 's'} waiting for your approval</span></button>
      <button class="box row gap12" style="cursor:pointer;text-align:left;width:100%" data-act="go" data-screen="knowledge"><span class="mono bb muted" style="font-size:18px">${t.unverified_answers}</span><span class="small muted" style="line-height:1.4">ATS answers unverified — verify once, reuse everywhere</span></button>
    </div></section>
  </div></div>`;
}
function renderRunSheet(t) {
  const now = new Date();
  const triageDone = t.pending_triage === 0 && t.triaged_today > 0;
  const items = [{ done: triageDone, title: t.pending_triage ? `Triage ${t.pending_triage} new job${t.pending_triage === 1 ? '' : 's'}` : 'Triage the new jobs', sub: triageDone ? `Done · ${t.triaged_today} triaged today` : t.pending_triage ? 'Shortlist, park or skip each one' : 'Queue is empty — run a search to fill it', minutes: Math.max(2, Math.ceil(t.pending_triage * 0.3)), cta: triageDone ? 'Review' : 'Start', act: 'go-triage' }];
  t.actions.slice(0, 3).forEach(a => items.push({ done: false, title: `${ACTION_META[a.kind].cta === 'Tailor' ? 'Tailor' : a.kind === 'approve' ? 'Approve' : 'Nudge'} ${a.company}`, sub: a.reason, minutes: a.minutes, cta: ACTION_META[a.kind].cta, act: 'do-action', data: { kind: a.kind, job: a.job_id, app: a.application_id || '' } }));
  const open = items.filter(i => !i.done);
  const total = open.reduce((a, i) => a + i.minutes, 0);
  let n = 0;
  return `<div class="col gap14" style="max-width:820px;margin:0 auto">
    <div class="card" style="padding:22px 24px"><div class="caps" style="letter-spacing:.08em;color:var(--faint)">${esc(now.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'short' }).toUpperCase())} · RUN SHEET</div>
      <div class="pretty" style="font-size:26px;font-weight:600;letter-spacing:-.02em;margin:8px 0 6px">${open.length ? `${['Zero', 'One', 'Two', 'Three', 'Four', 'Five'][open.length]} thing${open.length === 1 ? '' : 's'}, then you're done for the day.` : "You're done for the day."}</div>
      <div class="muted" style="font-size:13.5px">${open.length ? `Estimated ${total} minutes. Nothing else on this screen needs you.` : 'Come back tomorrow, or start a new search.'}</div></div>
    ${items.map(i => { const first = !i.done && n++ === 0; return `<div class="card row gap16" style="padding:18px 20px;${first ? 'border-color:var(--brand-line)' : ''}${i.done ? ';background:#fbfcfe' : ''}">
      <div style="width:26px;height:26px;flex:none;border-radius:99px;border:2px solid ${i.done ? '#0f9c86' : first ? '#5b4ae8' : '#9aa1b2'};display:grid;place-items:center;font-size:12px;font-weight:700;color:${i.done ? '#0f9c86' : first ? '#5b4ae8' : '#9aa1b2'}">${i.done ? '✓' : n}</div>
      <div class="grow"><div class="b" style="font-size:15px;${i.done ? 'text-decoration:line-through' : ''}">${esc(i.title)}</div><div class="small muted mt4 pretty">${esc(i.sub)}</div></div>
      <span class="xs faint mono none">${i.minutes} min</span>${btn(i.cta, i.act, i.data || {}, first ? 'primary none' : 'none')}</div>`; }).join('')}
    <div class="card row gap14 wrap" style="padding:18px 20px"><div class="grow"><div class="b" style="font-size:13.5px">Weekly pace</div><div class="small muted mt4">${t.week.applied} of ${t.week.target} applications · ${esc(t.week.response_rate)} response rate</div></div>
      <div class="row" style="gap:5px">${t.week.days.map(d => { const max = Math.max(2, ...t.week.days.map(x => x.n)); return `<div style="width:26px" class="tc"><div style="height:34px;border-radius:6px;background:#f0f1f7;display:flex;align-items:flex-end;overflow:hidden"><div style="width:100%;height:${100 * d.n / max}%;background:${d.future ? '#a79bf0' : '#5b4ae8'}"></div></div><div class="tiny faint mt4">${d.label}</div></div>`; }).join('')}</div></div>
  </div>`;
}

/* ───────────── discovery ───────────── */
function domainCheckNote(job) {
  if (job.domain_match === 'REVIEW') return `<div class="banner warn mb12" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><span class="grow"><b>Needs your review.</b> ${esc((job.domain_reason || '').replace('Needs your review: ', ''))} Nothing is spent on it until you choose.</span>${btn('It matches', 'review-decide', { ids: String(job.id), matches: '1' }, 'sm soft')}${btn('Not a match', 'review-decide', { ids: String(job.id), matches: '0' }, 'sm')}</div>`;
  if (job.domain_match === 'PENDING') return `<div class="xs muted mb12">${esc(job.domain_reason || 'Waiting for the AI check')}: it runs before anything else is spent on this job.</div>`;
  return (job.domain_reason || '').startsWith('AI check') ? `<div class="xs muted mb12">${esc(job.domain_reason)}</div>` : '';
}
function renderReview() {
  const r = D.review || { total: 0, jobs: [] };
  const picked = S.reviewChecked || (S.reviewChecked = new Set());
  if (!r.jobs.length) return '<div class="card"><p class="settings-help">Nothing needs your review. Jobs the domain check cannot settle wait here, and nothing is spent on them until you choose.</p></div>';
  const ids = [...picked].filter(id => r.jobs.some(j => j.id === id));
  const bulk = ids.length ? `${btn(label('reviewYes', `These ${ids.length} match`, 'Saving…'), 'review-decide', { ids: ids.join(','), matches: '1' }, 'primary sm', isBusy('reviewYes'))}${btn(label('reviewNo', `Remove these ${ids.length}`, 'Saving…'), 'review-decide', { ids: ids.join(','), matches: '0' }, 'sm', isBusy('reviewNo'))}` : '<span class="xs muted">Select jobs to decide several at once</span>';
  return `<div class="card flush"><div class="row gap10 wrap" style="padding:14px 18px;align-items:center;border-bottom:1px solid var(--line2)">
      <input type="checkbox" data-act="review-all" ${ids.length === r.jobs.length ? 'checked' : ''} aria-label="Select all"><div class="grow"><b>${r.total} job${r.total === 1 ? '' : 's'} could go either way</b><div class="xs muted">The rules and the AI check could not settle these, so you decide. Jobs that match go on to the detailed match; removed ones move to Not matching.</div></div>
      <div class="row gap8 wrap">${bulk}</div></div>
    ${r.jobs.map(j => `<div class="row gap10" style="padding:14px 18px;align-items:flex-start;border-bottom:1px solid var(--line2)">
      <input type="checkbox" data-act="review-check" data-id="${j.id}" ${picked.has(j.id) ? 'checked' : ''} aria-label="Select" style="margin-top:3px"><div class="grow" style="min-width:0">
        <div class="small b">${j.url ? `<a href="${attr(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a>` : esc(j.title)}</div><div class="xs muted">${esc([j.company, j.location].filter(Boolean).join(' · '))}</div>
        ${j.reason ? `<div class="small mt4">${esc(j.reason)}</div>` : ''}
        <div class="row gap6 wrap mt6">${j.your_skills_found.length ? j.your_skills_found.slice(0, 10).map(s => pill(s, 'good')).join('') : '<span class="xs muted">None of your skills found in the posting</span>'}${j.points_against.slice(0, 2).map(p => pill(p, 'warn')).join('')}</div>
        ${j.summary ? `<div class="xs muted mt6 pretty">${esc(j.summary)}…</div>` : ''}</div>
      <div class="row gap6" style="flex:none">${btn('It matches', 'review-decide', { ids: String(j.id), matches: '1' }, 'sm soft')}${btn('Remove', 'review-decide', { ids: String(j.id), matches: '0' }, 'sm')}</div></div>`).join('')}</div>`;
}
function renderDiscovery() {
  const banner = S.polling ? `<div class="banner mb14"><span class="dot" style="background:var(--brand)"></span><span class="grow">${esc(S.polling.text)}</span></div>` : '';
  if (S.discMode === 'review') return banner + renderReview();
  if (S.discMode === 'table') return banner + renderTable();
  if (S.discMode === 'setup') return banner + renderSetup();
  return banner + renderTriage();
}

/* ───────────── ranking tracker ───────────── */
// The asynchronous ranker's work for this account (/api/ranking/tracker): waiting, running, retrying and finished.
let rankingTimer = null;
// Shown as the Ranking tab of Running workers (app/static/workers.js).
const onRankingTab = () => S.screen === 'workers' && wkState().tab === 'ranking';
const RANK_REASON = { new: 'New job', user_request: 'You asked', resume_changed: 'Resume changed', profile_changed: 'Profile changed', prompt_changed: 'Ranking prompt updated', job_parsed: 'Posting parsed', description_changed: 'Description added' };
const until = iso => {
  if (!iso) return '';
  const sec = Math.round((new Date(iso.slice(0, 23) + 'Z') - Date.now()) / 1000);
  return sec <= 0 ? 'now' : sec < 60 ? `in ${sec}s` : sec < 3600 ? `in ${Math.round(sec / 60)}m` : `in ${Math.round(sec / 3600)}h`;
};
const later = iso => !!iso && until(iso) !== 'now';
// How long until a queued ranking starts, from what the server measured (never shown when nothing is measured).
const rankEta = sec => sec == null ? '' : sec <= 5 ? 'starting next' : sec < 60 ? `~${sec}s` : sec < 3600 ? `~${Math.round(sec / 60)}m` : `~${Math.round(sec / 3600)}h`;
const RANK_STAGE_COLOR = { ranking: '#5b4ae8', queued: '#8b93ad', waiting: '#c3c8da', retry: '#e0a136' };
// One bar per job in the queue: where it is now, its place in line and when it should start.
function rankProgress(t, total) {
  if (t.percent == null) return '';
  const place = t.queue_position ? `#${t.queue_position}${total ? ` of ${total}` : ''}` : '';
  const eta = rankEta(t.eta_seconds);
  const note = [place, t.stage_label, t.stage === 'ranking' && eta ? `${eta} left` : eta].filter(Boolean).join(' · ');
  return `<div class="row base gap8 mt6"><span class="grow" style="max-width:220px">${bar(t.percent + '%', RANK_STAGE_COLOR[t.stage] || '#5b4ae8')}</span>
    <span class="tiny mono faint none">${esc(note)}</span>${wkRankWhy(t)}</div>`;
}
async function loadRanking() {
  const before = new Set((D.ranking?.active || []).map(t => t.id));
  D.ranking = await api('/api/ranking/tracker?' + qs(S.rf));
  const done = (D.ranking.recent || []).filter(t => before.has(t.id));
  if (done.length) {
    const one = done.length === 1 ? done[0] : null, failed = done.filter(t => t.status === 'FAILED').length;
    toast(one ? `${one.company}: ${rankTaskDetail(one)}` : `${done.length} rankings finished${failed ? ` · ${failed} failed` : ''}`, failed === done.length);
  }
  scheduleRankingPoll();
}
// Poll while the tracker is open: every few seconds while work is in flight, slowly otherwise.
function scheduleRankingPoll() {
  clearTimeout(rankingTimer); rankingTimer = null;
  if (!onRankingTab()) return;
  rankingTimer = setTimeout(async () => {
    if (!onRankingTab()) return;
    try { await loadRanking(); } catch { scheduleRankingPoll(); return; }
    if (onRankingTab()) wkPollRender();  // never closes an open filter dropdown
  }, D.ranking?.active?.length ? 4000 : 20000);
}
function rankTaskPill(t) {
  if (t.status === 'PROCESSING') return pill('ranking…', 'brand');
  if (t.status === 'PENDING') return later(t.next_attempt_at) ? pill(t.attempt_count ? 'retry scheduled' : 'waiting', 'warn') : pill('queued', 'muted');
  if (t.status === 'FAILED') return pill('failed', 'bad');
  if (wkStoppedRank(t)) return pill('stopped', 'muted');
  if (t.status === 'SKIPPED') return pill('skipped', 'muted');
  return t.ranking?.decision === 'QUALIFIED' ? pill('qualified', 'good') : pill('archived', 'muted');
}
function rankTaskDetail(t) {
  const category = t.error_category ? ` (${t.error_category.toLowerCase().replace(/_/g, ' ')})` : '';
  if (t.status === 'PROCESSING') return `Ranking · attempt ${t.attempt_count} of ${t.max_attempts} · ${elapsed(t.started_at)}`;
  if (t.status === 'PENDING') {
    if (later(t.next_attempt_at) && t.attempt_count) return `Attempt ${t.attempt_count} of ${t.max_attempts} failed${category} · retry ${until(t.next_attempt_at)}${t.last_error ? ` · ${t.last_error}` : ''}`;
    if (later(t.next_attempt_at)) return `Waiting: ${t.last_error || 'your account is not ready'} · checks again ${until(t.next_attempt_at)}`;
    return 'Queued for the next ranking run';
  }
  if (t.status === 'FAILED') return `Gave up after ${t.attempt_count} attempt${t.attempt_count === 1 ? '' : 's'}${category}${t.last_error ? ` · ${t.last_error}` : ''}`;
  if (t.status === 'SKIPPED') return t.last_error || 'Skipped';
  const r = t.ranking;
  if (!r) return 'Ranked';
  return `${r.decision === 'QUALIFIED' ? 'Qualified' : 'Archived'} · fit ${fitTxt(r.overall_fit_score)} · candidate ${fitTxt(r.candidate_fit_score)} · resume ${fitTxt(r.resume_fit_score)} · confidence ${Math.round(r.confidence * 100)}% · v${r.version}`;
}
// `r` is the tracker, possibly narrowed by Running workers' status filter (wkRankingView).
function renderRanking(r = D.ranking) {
  if (!r) return loading();
  const c = r.counts, w = r.worker || {}, day = r.day || c;  // the last day's results: Clear finished never resets them
  const tile = ([title, n, hint, tone]) => `<div class="box"><div class="xs muted">${esc(title)}</div>
    <div class="mono bb" style="font-size:24px;line-height:1.3;color:${n ? { brand: 'var(--brand)', warn: 'var(--warn)', good: 'var(--good)', bad: 'var(--bad)' }[tone] || 'var(--ink)' : 'var(--grey)'}">${n}</div>
    <div class="tiny faint">${esc(hint)}</div></div>`;
  const tileGroup = (caption, items) => `<div class="grow" style="min-width:240px"><div class="caps-sm mb8">${esc(caption)}</div>
    <div style="display:grid;grid-template-columns:repeat(${items.length},minmax(0,1fr));gap:8px">${items.map(tile).join('')}</div></div>`;
  const tiles = tileGroup('Pipeline now', [['Filtering', c.filtering, 'hard filters', 'muted'], ['Queued', c.queued, 'next run', 'muted'],
    ['Ranking', c.processing, 'model call', 'brand'], ['Retry / wait', c.retry_scheduled + c.waiting, 'backing off', 'warn'],
    ['No description', c.no_description ?? 0, 'not rankable', 'warn']])
    + tileGroup('Last 24 hours', [['Qualified', day.qualified, 'kept', 'good'], ['Archived', day.archived, 'by policy', 'muted'],
      ['Failed', day.failed, 'gave up', 'bad']]);
  const inFlight = r.active.length, { finished, total } = r.progress;
  const pct = total ? Math.round(100 * finished / total) : 100;
  // Your own cadence when you set one (Running workers → Settings), otherwise the server's own sweep.
  const cadence = r.interval_minutes ? `every ${r.interval_minutes} min · your cadence` : `every ${w.interval_minutes} min`;
  const nextRun = r.next_run_at || w.next_run_at;
  const workerTxt = !w.enabled ? 'The ranking worker is turned off on this server'
    : w.running && !r.next_run_at ? 'A ranking run is in progress'
    : nextRun ? `Next ranking run ${until(nextRun)} · ${cadence}` : `Ranking runs ${cadence}`;
  const blocked = r.blocked ? `<div class="banner warn mb14"><span class="grow">Ranking is paused for your account: ${esc(r.blocked)}</span>${btn('Open settings', 'go', { screen: 'settings' }, 'sm')}</div>` : '';
  const autoOff = r.auto_rank ? '' : `<div class="banner mb14"><span class="grow">Auto-rank on import is off, so only the jobs you ask to rank are ranked.</span>${btn('Search setup', 'go', { screen: 'discovery', mode: 'setup' }, 'sm')}</div>`;
  const row = t => {
    const retry = t.removed ? btn(isBusy('rankRetry' + t.job_id) ? 'Queueing…' : 'Rank again', 'ranking-retry', { job: t.job_id }, 'sm', isBusy('rankRetry' + t.job_id))
      : t.status !== 'FAILED' ? '' : t.retried ? btn('Retried', 'ranking-retry', {}, 'sm', true)
      : btn(isBusy('rankRetry' + t.job_id) ? 'Queueing…' : 'Retry', 'ranking-retry', { job: t.job_id }, 'sm', isBusy('rankRetry' + t.job_id));
    // A waiting job can be taken out of the queue; one being ranked right now cannot (its model call is running).
    const remove = t.status === 'PENDING' ? btn(label('rankRemove' + t.job_id, 'Remove', 'Removing…'), 'ranking-remove', { ids: t.job_id }, 'sm ghost danger', isBusy('rankRemove' + t.job_id)) : '';
    const detail = rankTaskDetail(t), why = RANK_REASON[t.reason] || t.reason;
    return `<div class="lrow ${t.status === 'PROCESSING' ? 'sel' : ''}" style="gap:12px;align-items:flex-start">
      ${(t.status === 'FAILED' || wkStoppedRank(t)) && !t.retried ? wkSelectBox('ranking', t.id, t.title) : ''}
      <div class="grow" style="min-width:0"><div class="small b ellipsis">${esc(t.title)} <span class="muted" style="font-weight:400">· ${esc(t.company)}</span></div>
        <div class="xs ellipsis ${t.status === 'FAILED' ? '' : 'muted'}" style="${t.status === 'FAILED' ? 'color:var(--bad)' : ''}" title="${attr(detail)}">${esc(why)} · ${esc(detail)}</div>
        ${rankProgress(t, r.in_flight)}</div>
      ${typeof wkLastRun === 'function' ? wkLastRun(t) : ''}${typeof wkModelCells === 'function' ? wkModelCells(t) : ''}${wkRankActions(t)}${retry}${remove}${wkRowNudge('ranking', t.job_id, t.status)}${btn('Job', 'open-job', { id: t.job_id }, 'sm')}${rankTaskPill(t)}</div>`;
  };
  const section = (key, title, sub, items, actions = '') => `<section class="card flush">
    <div class="row between wrap gap10" style="padding:14px 18px;border-bottom:1px solid var(--line2)"><div><div class="card-title" style="font-size:16px">${esc(title)}</div><div class="card-sub">${esc(sub)}</div></div>${actions ? `<div class="row gap8">${actions}</div>` : ''}</div>
    <div style="max-height:460px;overflow:auto" data-scroll-key="ranking-${key}">${items.map(row).join('')}</div></section>`;
  const nothing = !inFlight && !r.recent.length && !c.filtering && !r.filtered && !r.finished_cleared_at;
  const removable = r.active.filter(t => t.status === 'PENDING').map(t => t.job_id);
  // Search, location and status live in Running workers' filter bar; this line says what they matched.
  const filters = `<section class="card flush"><div class="toolbar" style="border-bottom:0">
    ${r.filtered ? `<span class="small muted">${r.active.length} in progress · ${r.recent.length} finished match</span>${btn('Reset', 'ranking-reset-filters', {}, 'sm')}` : ''}
    ${r.filtered && removable.length ? btn(label('rankRemoveAll', `Remove ${removable.length} shown from ranking`, 'Removing…'), 'ranking-remove', { ids: removable.join(','), confirm: 1 }, 'sm danger', isBusy('rankRemoveAll')) : ''}</div></section>`;
  return `<div class="stack">${blocked}${autoOff}
    <section class="card">
      <div class="row wrap gap10 mb14"><div class="grow" style="min-width:220px"><div class="card-title">${inFlight || c.filtering ? 'Ranking in the background' : 'Ranking is idle'}</div>
        <div class="card-sub">${esc(workerTxt)}${w.enabled ? ' · runs on the server, safe to leave this page' : ''}</div>
        <div class="row base gap8 mt8"><span class="grow">${bar(pct + '%', '#5b4ae8')}</span><span class="xs mono muted none">${finished}/${total} in the last day</span></div>
        <div class="xs muted mt6">Up to ${r.concurrency} ranking${r.concurrency === 1 ? '' : 's'} at a time${r.typical_seconds ? ` · about ${rankEta(r.typical_seconds).replace('~', '')} each` : ''} · <a href="#workers/settings">change</a></div></div>
        ${btn(label('rankBatch', 'Queue unranked jobs', 'Queueing…'), 'rank-batch', {}, 'sm', isBusy('rankBatch'))}
        ${btn(label('rankRefresh', 'Refresh', 'Refreshing…'), 'ranking-refresh', {}, 'sm', isBusy('rankRefresh'))}</div>
      <div class="row wrap gap16" style="align-items:flex-start">${tiles}</div></section>
    ${r.filtered ? filters : ''}
    ${r.filtered && !r.active.length && !r.recent.length ? `<section class="card">${empty('No ranking work matches', 'Try another location or clear the filters.', btn('Reset filters', 'ranking-reset-filters', {}, 'primary'))}</section>` : ''}
    ${nothing ? `<section class="card">${empty('No ranking activity in the last day', 'Jobs from your searches are filtered and ranked automatically. Rankings you request from a job, the table or the triage queue show up here too.', btn('Go to triage', 'go-triage', {}, 'primary'))}</section>` : ''}
    ${r.active.length ? section('active', 'In progress', `${r.filtered ? `${r.active.length} of ${inFlight}` : inFlight} job${inFlight === 1 ? '' : 's'} waiting, ranking or retrying`, r.active) : ''}
    ${r.recent.length ? section('recent', r.finished_cleared_at ? `Finished since you cleared the list at ${fmtDateTime(r.finished_cleared_at)}` : 'Finished in the last 24 hours',
      `${c.completed} ranked · ${c.failed} failed${c.skipped ? ` · ${c.skipped} skipped` : ''}`, r.recent)
      : r.finished_cleared_at ? `<section class="card pad16"><span class="small muted">Finished rankings cleared at ${esc(fmtDateTime(r.finished_cleared_at))}. New results show up here as they finish; Show cleared (above) brings the rest back.</span></section>` : ''}
  </div>`;
}
function fitSummary(j) {
  return `<div class="row wrap gap10 mt16">
    <div class="box good grow" style="min-width:220px"><div class="xs b mb4" style="color:var(--good)">MATCHED · ${j.matches.length}</div><div class="small ink2" style="line-height:1.5">${j.matches.length ? esc(j.matches.join(', ')) : 'Rank this job to see matches'}</div></div>
    <div class="box warn grow" style="min-width:220px"><div class="xs b mb4" style="color:var(--warn)">GAPS · ${j.gaps.length + j.missing.length}</div><div class="small" style="color:var(--warn-deep);line-height:1.5">${[...j.missing, ...j.gaps].length ? esc([...j.missing, ...j.gaps].join(' · ')) : j.fit == null ? '—' : 'No gaps flagged'}</div></div></div>`;
}
function renderTriage() {
  const q = D.queue; if (!q) return loading();
  const job = q.items[0];
  const done = S.triagedThisSession;
  const total = q.total + done;
  const setups = D.setups || [];
  const side = `<div class="c-narrow col gap14">
    <section class="card pad16"><div class="card-title sm mb10">Up next</div><div class="col gap4">${q.items.slice(1, 6).map((u, i) => `<div class="row gap10" style="padding:9px;border-radius:9px;cursor:pointer;background:${i === 0 ? 'var(--softer)' : 'transparent'}" data-act="open-job" data-id="${u.id}"><span class="mono bb ${fitCls(u.fit)}" style="width:24px">${fitTxt(u.fit)}</span><div class="grow"><div class="small ellipsis" style="font-weight:500">${esc(u.title)}</div><div class="tiny muted ellipsis">${esc(u.company)}</div></div></div>`).join('') || '<div class="small muted">Nothing else in the queue.</div>'}</div></section>
    <section class="card pad16"><div class="card-title sm mb4">Filters for this batch</div><div class="small muted mb12">Narrowing re-ranks the queue instantly.</div>
      <div class="col gap12"><label class="field strong">Minimum fit · <span class="mono">${S.triage.min_fit}</span><input type="range" min="0" max="100" step="5" id="triage-min" data-triage="min_fit" value="${S.triage.min_fit}"></label>
        <label class="field strong">Location<select class="select" style="margin:4px 0 0" id="triage-location" data-triage="location">${locationOptions(q.locations).map(([v, t]) => `<option value="${attr(v)}" ${v === S.triage.location ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select></label>
        <div><div class="xs muted mb4">Must mention (use / for either)</div>${tagBox('triage', 'keywords', S.triage.keywords, 'Hybrid, Lead/Principal…', 6)}</div>
        <label class="check"><input type="checkbox" data-triage="hide_agencies" ${S.triage.hide_agencies ? 'checked' : ''}>Hide staffing agencies</label>
        <label class="check"><input type="checkbox" data-triage="hide_reposts" ${S.triage.hide_reposts ? 'checked' : ''}>Hide reposts I've seen</label>
        ${q.items.some(i => i.fit == null) ? btn(label('rankBatch', 'Rank every unranked job', 'Queueing…'), 'rank-batch', {}, 'soft block', isBusy('rankBatch')) : ''}
        ${q.domain_counts?.not_matching ? `<a href="#" class="xs" data-act="show-not-matching" title="Outside your domain: hidden at discovery, never ranked">${q.domain_counts.not_matching} hidden as not matching →</a>` : ''}</div></section>
    <section class="card pad16"><div class="row between mb10"><div class="card-title sm">Saved searches</div>${setups.length ? pill(`${setups.length} setup${setups.length === 1 ? '' : 's'}`, 'muted') : ''}</div>
      <div class="col gap6">${setups.map(x => `<div class="box" style="cursor:pointer" data-act="open-setup" data-id="${x.id}"><div class="row between gap8"><span class="small b ellipsis">${esc(x.name)}</span>${pill(x.schedule_label, x.schedule_kind !== 'manual' && x.enabled ? 'good' : 'muted')}</div><div class="xs muted mt4">${esc((x.settings.roles || []).join(' · ') || 'No roles yet')} · ${esc((x.settings.locations || []).join(' · ') || 'Any location')}</div></div>`).join('') || '<div class="small muted">No saved searches yet.</div>'}</div>
      <div class="col gap6 mt10">${(D.runs || []).slice(0, 3).map(r => `<div class="xs muted row gap8"><span class="dot" style="background:${r.status === 'SUCCEEDED' ? '#0f9c86' : r.completed_at ? '#c22f4e' : '#e0a136'}"></span>Run #${r.id} · ${r.created_jobs} new from ${r.item_count} · ${fmtDay(r.created_at)}</div>`).join('')}</div>
      <button class="btn block mt12" data-act="go" data-screen="discovery" data-mode="setup">Edit search setup</button></section></div>`;
  if (!job) {
    return `<div class="split"><div class="c-main">${`<section class="card">${empty('Inbox zero', done ? `You triaged ${done} job${done === 1 ? '' : 's'} this session. Nothing else is waiting.` : 'No untriaged jobs match these filters.', `${btn('Run the saved search', 'run-search', {}, 'primary')}${btn('Browse all jobs', 'go', { screen: 'discovery', mode: 'table' })}${btn('Add a job manually', 'go', { screen: 'discovery', mode: 'setup' })}`)}</section>`}</div>${side}</div>`;
  }
  const d = D.triageDetail && D.triageDetail.id === job.id ? D.triageDetail : job;
  return `<div class="split">
  <div class="c-main col gap14">
    <div class="card row gap12" style="padding:12px 16px;border-radius:12px"><span class="base b">Job ${done + 1} of ${total}</span><div class="bar grow">${''}<i style="width:${total ? (100 * done / total) : 0}%;background:var(--brand)"></i></div><span class="small muted">${q.total - 1} left · ~${Math.max(1, Math.ceil((q.total) * 0.3))} min</span></div>
    <div class="card flush" style="border-radius:16px">
      <div class="row top-align gap16 wrap" style="padding:22px 24px;border-bottom:1px solid var(--line2)">
        <div class="avatar lg">${esc(initials(job.company))}</div>
        <div style="flex:1 1 210px;min-width:0"><div style="font-size:20px;font-weight:600;letter-spacing:-.01em">${esc(job.title)}</div><div class="muted mt4" style="font-size:13.5px">${esc(job.company)}${job.location ? ' · ' + esc(job.location) : ''}</div>
          <div class="row gap6 wrap mt10">${pill(`${job.board} · found ${job.posted}`, 'plain', 'lg')}${postedTxt(job) ? pill(postedTxt(job), 'plain', 'lg') : ''}${job.agency ? pill('Likely agency', 'warn', 'lg') : ''}${job.url ? `<a class="pill plain lg" href="${attr(job.url)}" target="_blank" rel="noopener">Posting ↗</a>` : ''}</div></div>
        ${job.fit == null ? `<div class="scorebox"><div class="n" style="color:var(--grey)">—</div><div class="l">NOT RANKED</div>${job.needs_description ? btn('Add description', 'open-job', { id: job.id }, 'sm soft mt8') : needsParse(d) ? parseBtn(d, 'sm soft mt8') : btn(label('rank' + job.id, 'Rank now', 'Queueing…'), 'rank-job', { id: job.id }, 'sm soft mt8', isBusy('rank' + job.id))}</div>` : `<div class="scorebox"><div class="n">${fitTxt(job.fit)}</div><div class="l">FIT SCORE</div></div>`}
      </div>
      <div style="padding:18px 24px;border-bottom:1px solid var(--line2)">
        <div class="row gap8 mb10">${pill('AI', 'brand')}<span class="base b">${job.fit == null ? 'Not ranked yet' : `Why this scores ${fitTxt(job.fit)}`}</span></div>
        <div class="small ink2 pretty" style="line-height:1.55">${esc(job.summary || (job.fit == null ? 'Ranking compares the posting against your profile and verified evidence.' : ''))}</div>
        ${job.strategy ? `<div class="small muted mt8 pretty"><b>Strategy:</b> ${esc(job.strategy)}</div>` : ''}
        ${fitSummary(job)}
      </div>
      <div style="padding:18px 24px"><div class="base b mb8">Job description</div><div class="desc fade">${paragraphs(d.description || job.snippet, [...job.matches, ...job.keywords], [...job.gaps, ...job.missing])}</div></div>
      <button class="linkbtn" style="border-top:1px solid var(--line2)" data-act="open-job" data-id="${job.id}">Open full job description</button>
    </div>
    <div class="actionbar">
      ${btn('Skip <span class="kbd">X</span>', 'triage', { id: job.id, action: 'skip' }, 'lg')}
      ${btn('Maybe later <span class="kbd">M</span>', 'triage', { id: job.id, action: 'maybe' }, 'lg')}
      <div class="grow"></div>
      ${btn('Shortlist <span class="kbd">S</span>', 'triage', { id: job.id, action: 'shortlist' }, 'lg soft')}
      ${btn('Shortlist &amp; tailor now <span class="kbd">⏎</span>', 'triage', { id: job.id, action: 'shortlist', tailor: 1 }, 'lg primary')}
    </div>
  </div>${side}</div>`;
}
// Tailoring works from the Ranker's analysis, so only ranked jobs can be sent to tailoring. A job is ranked once it
// has a fit score; rows on the page and "select all matching" both say which selected jobs are.
function noteRanked(rows) {
  rows.forEach(r => r.fit == null ? S.rankedIds.delete(r.id) : S.rankedIds.add(r.id));
  rows.forEach(r => r.description_state === 'PARSED' ? S.parsedIds.add(r.id) : S.parsedIds.delete(r.id));
}
// Ranking and tailoring wait for a parsed description while parsing is on (parse_enabled): until then a job offers
// Parse instead. With parsing off nothing is ever parsed, so every job counts as ready.
const parseGate = () => D.jobs?.parse_enabled !== false;
const parsedSelection = () => [...S.checked].filter(id => !parseGate() || S.parsedIds.has(id));
const rankedSelection = () => parsedSelection().filter(id => S.rankedIds.has(id));
// Cards from /api/jobs/search (the table, triage) carry the switch on the response, details on the job itself.
const needsParse = j => !!j && (j.parse_enabled ?? D.queue?.parse_enabled ?? D.jobs?.parse_enabled) !== false && !!j.description_state && !['PARSED', 'NO_DESCRIPTION'].includes(j.description_state);
const parseBtn = (j, cls = '') => j.description_state === 'PARSING'
  ? `<span title="Ranking and tailoring start once the description is parsed">${btn('Parsing…', 'parse-job', { id: j.id }, cls, true)}</span>`
  : `<span title="${attr(j.description_state === 'FAILED' ? 'Parsing failed: ranking and tailoring wait for a parsed description' : 'Ranking and tailoring wait for a parsed description')}">${btn(label('parse' + j.id, j.description_state === 'FAILED' ? 'Parse again' : 'Parse', 'Queueing…'), 'parse-job', { id: j.id }, cls, isBusy('parse' + j.id))}</span>`;

// ── Discovery table filters ──
const DATE_WINDOW_NAMES = { '24h': '24 hours', '3d': '3 days', '7d': '7 days', '15d': '15 days', '30d': '30 days', '60d': '60 days', '90d': '90 days' };
const DATE_KEYS = ['within', 'older_than', 'date_from', 'date_to', 'posted_unknown'];
const WORK_MODE_NAMES = { remote: 'Remote', hybrid: 'Hybrid', onsite_unknown: 'On-site / not stated' };
const isoDay = v => { const [y, m, d] = String(v).split('-').map(Number); return new Date(y, m - 1, d).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: y === new Date().getFullYear() ? undefined : 'numeric' }); };
function dateMode(f) {
  if (f.posted_unknown && f.date_field === 'posted') return 'unknown';
  if (f.within) return 'within';
  if (f.older_than) return 'older';
  if (f.date_from || f.date_to) return 'between';
  return S.dateMode === 'between' ? 'between' : 'any';
}
function dateLabel(f) {
  const field = f.date_field === 'posted' ? 'Posted' : 'Found', mode = dateMode(f);
  if (mode === 'unknown') return 'Posted · no date given';
  if (mode === 'within') return `${field} · last ${DATE_WINDOW_NAMES[f.within] || f.within}`;
  if (mode === 'older') return `${field} · older than ${DATE_WINDOW_NAMES[f.older_than] || f.older_than}`;
  if (f.date_from || f.date_to) return `${field} · ${f.date_from ? isoDay(f.date_from) : 'any'} – ${f.date_to ? isoDay(f.date_to) : 'today'}`;
  return `${field} · any time`;
}
function datePopover(f) {
  const mode = dateMode(f);
  const modes = [['any', 'Any time'], ['within', 'In the last'], ['older', 'Older than'], ['between', 'Between dates'], ...(f.date_field === 'posted' ? [['unknown', 'No posting date']] : [])];
  const windows = key => `<div class="row gap6 wrap">${Object.entries(DATE_WINDOW_NAMES).map(([v, t]) => `<button type="button" class="pickchip ${f[key] === v ? 'on' : ''}" data-act="date-window" data-key="${key}" data-value="${v}">${t}</button>`).join('')}</div>`;
  return `<div class="date-panel" id="flt-date-panel" role="group" aria-label="Date filter">
    <div class="xs muted mb6">Filter by the date the job was</div>
    <div class="seg mb12">${[['found', 'Found'], ['posted', 'Posted']].map(([v, t]) => `<button type="button" class="${f.date_field === v ? 'on' : ''}" data-act="date-field" data-value="${v}" aria-pressed="${f.date_field === v}">${t}</button>`).join('')}</div>
    <div class="row gap6 wrap mb12" role="radiogroup" aria-label="Date range">${modes.map(([v, t]) => `<button type="button" role="radio" aria-checked="${mode === v}" class="pickchip ${mode === v ? 'on' : ''}" data-act="date-mode" data-mode="${v}">${t}</button>`).join('')}</div>
    ${mode === 'within' ? windows('within') : mode === 'older' ? windows('older_than')
      : mode === 'between' ? `<div class="row gap8 wrap"><label class="field xs">From<input class="input" type="date" id="flt-date-from" data-filter="date_from" value="${attr(f.date_from)}" max="${attr(f.date_to || '')}"></label><label class="field xs">To<input class="input" type="date" id="flt-date-to" data-filter="date_to" value="${attr(f.date_to)}" min="${attr(f.date_from || '')}"></label></div><div class="xs faint mt6">Both days included · leave one empty for an open range</div>`
      : mode === 'unknown' ? '<div class="xs muted">Jobs whose board never gave a posting date. They never match a date range.</div>'
      : `<div class="xs muted">${f.date_field === 'posted' ? 'Jobs without a posting date only match “No posting date”.' : 'Every job, whenever it was found.'}</div>`}
    <div class="row mt12"><span class="grow"></span>${btn('Done', 'date-pop', {}, 'sm')}</div></div>`;
}
// Every filter that differs from its default, as [keys it resets, label]; "more" marks the ones in the More panel.
function activeFilters(j) {
  const f = S.filters, out = [], name = (list, v) => (list.find(([k]) => String(k) === String(v)) || [v, v])[1];
  const add = (keys, text, more = false) => out.push({ keys, text, more });
  if (f.q) add(['q'], `“${f.q}”`);
  if (dateMode(f) !== 'any' && (DATE_KEYS.some(k => f[k]))) add(DATE_KEYS, dateLabel(f));
  if (f.min_fit) add(['min_fit'], `Fit ≥ ${f.min_fit}`);
  if (f.stage && f.stage !== 'all') add(['stage'], f.stage === 'unranked' ? 'Unranked' : statusTitle(f.stage));
  if (f.max_fit) add(['max_fit'], `Fit ≤ ${f.max_fit}`, true);
  if (f.board && f.board !== 'all') add(['board'], f.board, true);
  if (f.location && f.location !== 'all') add(['location'], name(locationOptions(j.locations), f.location), true);
  if (f.domain && f.domain !== 'matching') add(['domain'], f.domain === 'all' ? 'Any domain' : 'Not matching', true);
  if (f.description && f.description !== 'all') add(['description'], name(j.description_states || [], f.description), true);
  if (f.work_mode && f.work_mode !== 'all') add(['work_mode'], WORK_MODE_NAMES[f.work_mode] || f.work_mode, true);
  if (f.job_type && f.job_type !== 'all') add(['job_type'], titleCase(f.job_type), true);
  if (f.setup_id) add(['setup_id'], `Setup: ${name(j.setups || [], f.setup_id)}`, true);
  if ((f.keywords || []).length) add(['keywords'], `Mentions ${f.keywords.join(', ')}`, true);
  if (f.has_salary) add(['has_salary'], 'Salary listed', true);
  if (f.hide_agencies) add(['hide_agencies'], 'No agencies', true);
  if (f.hide_reposts) add(['hide_reposts'], 'No reposts', true);
  if (f.include_applied) add(['include_applied'], 'Applied shown', true);
  return out;
}
const titleCase = s => String(s).replace(/(^|[\s-])(\w)/g, (m, a, b) => a + b.toUpperCase());
function filterBar(j, opts) {
  const f = S.filters, active = activeFilters(j), moreCount = active.filter(a => a.more).length;
  const hasDate = DATE_KEYS.some(k => f[k]);
  const fitCaps = [[0, 'Any'], [49, 'Below 50'], [69, 'Below 70'], [79, 'Below 80']];
  const check = (key, text, title = '') => `<label class="check small" ${title ? `title="${attr(title)}"` : ''}><input type="checkbox" id="flt-${key.replace(/_/g, '-')}" data-filter="${key}" ${f[key] ? 'checked' : ''}>${text}</label>`;
  const field = (text, control) => `<label class="field xs">${text}${control}</label>`;
  return `<div class="filterbar">
    <div class="toolbar">
      <input class="input grow" style="min-width:180px" id="flt-q" data-filter="q" value="${attr(f.q)}" placeholder="Search title, company or location…" aria-label="Search jobs">
      <button type="button" class="btn sm datebtn ${hasDate ? 'on' : ''}" id="flt-date" data-act="date-pop" aria-expanded="${S.datePop}" aria-controls="flt-date-panel"><span aria-hidden="true">◷</span>${esc(dateLabel(f))}<span class="faint" aria-hidden="true">${S.datePop ? '▴' : '▾'}</span></button>
      <select class="select" id="flt-min" data-filter="min_fit" aria-label="Minimum fit">${opts([[0, 'Any fit'], [50, 'Fit ≥ 50'], [70, 'Fit ≥ 70'], [80, 'Fit ≥ 80']], f.min_fit)}</select>
      <select class="select" id="flt-stage" data-filter="stage" aria-label="Stage">${opts([['all', 'All stages'], ['unranked', 'Unranked'], ...['discovered', 'shortlisted', 'maybe', 'archived', 'dismissed', 'tailored', 'needs_retailor', 'pending_approval', 'approved', 'applied'].map(s => [s, statusTitle(s)])], f.stage)}</select>
      <select class="select" id="flt-sort" data-filter="sort" aria-label="Sort">${opts([['fit', 'Sort: best fit'], ['new', 'Sort: newest found'], ['posted', 'Sort: newest posted'], ['company', 'Sort: company']], f.sort)}</select>
      <button type="button" class="btn sm ${S.moreFilters || moreCount ? 'soft' : ''}" data-act="toggle-more-filters" aria-expanded="${S.moreFilters}" aria-controls="flt-more">More filters${moreCount ? ` · ${moreCount}` : ''} <span aria-hidden="true">${S.moreFilters ? '▴' : '▾'}</span></button>
      <span class="grow"></span>
      ${S.checked.size || f.description !== 'NO_DESCRIPTION' ? '' : `<span title="Reads every job with no description from its posting. Jobs your domain filter hides are left out.">${btn(label('recoverDescriptions', 'Fetch missing descriptions', 'Queueing…'), 'recover-descriptions', {}, 'sm', isBusy('recoverDescriptions'))}</span>`}
      ${S.checked.size ? '' : btn(label('rankBatch', 'Rank unranked', 'Queueing…'), 'rank-batch', {}, 'sm', isBusy('rankBatch'))}
    </div>
    ${S.datePop ? datePopover(f) : ''}
    ${S.moreFilters ? `<div class="filter-more" id="flt-more">
      ${field('Board', `<select class="select" id="flt-board" data-filter="board">${opts([['all', 'All boards'], ...(j.boards || []).map(b => [b, b])], f.board)}</select>`)}
      ${field('Location', `<select class="select" id="flt-location" data-filter="location">${opts(locationOptions(j.locations || []), f.location)}</select>`)}
      ${field('Work mode', `<select class="select" id="flt-work-mode" data-filter="work_mode" title="As the board states it: a job that does not say remote or hybrid is “not stated”">${opts([['all', 'Any work mode'], ...Object.entries(WORK_MODE_NAMES).map(([k, t]) => [k, `${t} (${j.work_modes?.[k] ?? 0})`])], f.work_mode)}</select>`)}
      ${field('Job type', `<select class="select" id="flt-job-type" data-filter="job_type">${opts([['all', 'Any job type'], ...(j.job_types || []).map(t => [t, titleCase(t)])], f.job_type)}</select>`)}
      ${field('Fit up to', `<select class="select" id="flt-max" data-filter="max_fit">${opts(fitCaps, f.max_fit)}</select>`)}
      ${field('Search setup', `<select class="select" id="flt-setup" data-filter="setup_id">${opts([['', 'Any setup'], ...(j.setups || [])], f.setup_id)}</select>`)}
      ${field('Domain', `<select class="select" id="flt-domain" data-filter="domain" title="Jobs outside your domain are hidden at discovery and never ranked (Settings → Domain filter)">${opts([['matching', `Matching (${j.domain_counts?.matching ?? 0})`], ['not_matching', `Not matching (${j.domain_counts?.not_matching ?? 0})`], ['all', 'Any domain']], f.domain || 'matching')}</select>`)}
      ${field('Description', `<select class="select" id="flt-description" data-filter="description">${opts([['all', 'Any description'], ...(j.description_states || []).map(([k, name, n]) => [k, `${name} (${n})`])], f.description)}</select>`)}
      <div class="filter-wide"><div class="xs muted mb4">Must mention <span class="faint">· all of them; use / for either</span></div>${tagBox('flt', 'keywords', f.keywords || [], 'Python, Lead/Principal… then Enter', 6)}</div>
      <div class="filter-wide row gap16 wrap">${check('has_salary', 'Salary listed', 'Only jobs whose posting states a salary')}${check('hide_agencies', 'Hide staffing agencies')}${check('hide_reposts', 'Hide reposts')}${check('include_applied', 'Show applied', 'Jobs you already applied to are hidden from Discovery; the Applied stage always shows them')}</div>
    </div>` : ''}
    ${active.length ? `<div class="filter-chips" aria-label="Active filters">${active.map(a => `<span class="chip grey">${esc(a.text)}<button type="button" aria-label="Remove filter ${attr(a.text)}" data-act="clear-filter" data-keys="${a.keys.join(',')}">×</button></span>`).join('')}<a href="#" class="xs" data-act="reset-filters">Clear all</a></div>` : ''}
  </div>`;
}

function renderTable() {
  const j = D.jobs; if (!j) return loading();
  noteRanked(j.items);
  const f = S.filters;
  const rankable = parsedSelection().length, unparsed = S.checked.size - rankable;
  const tailorable = rankedSelection().length, unranked = rankable - tailorable;
  const sel = D.selDetail;
  const opts = (list, cur) => list.map(([v, t]) => `<option value="${attr(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(t)}</option>`).join('');
  const pages = []; for (let p = Math.max(1, j.page - 2); p <= Math.min(j.pages, Math.max(1, j.page - 2) + 4); p++) pages.push(p);
  const start = j.total ? (j.page - 1) * j.per_page + 1 : 0;
  const allOnPage = j.items.length && j.items.every(r => S.checked.has(r.id));
  return `<div class="split">
  <section class="card flush c-main">
    ${filterBar(j, opts)}
    ${S.checked.size ? `<div class="selbar" role="toolbar" aria-label="Selected jobs">
      <span class="small b" style="color:var(--brand-ink)">${S.checked.size} selected</span>
      ${unparsed ? `<span title="${attr(`${unparsed} selected job${unparsed === 1 ? ' is' : 's are'} not parsed yet: ranking and tailoring wait for a parsed description`)}">${btn(label('bulkParse', `Parse ${unparsed}`, 'Queueing…'), 'bulk-parse', {}, 'sm' + (rankable ? '' : ' primary'), isBusy('bulkParse'))}</span>` : ''}
      ${rankable ? btn(isBusy('bulkRank') ? esc(S.rankProgress || 'Queueing…') : `Rank ${rankable}`, 'bulk-rank', {}, 'sm', isBusy('bulkRank')) : ''}
      ${btn(`Shortlist ${S.checked.size}`, 'bulk', { action: 'shortlist' }, 'sm soft')}
      ${rankable ? `<span title="${attr(unranked ? `${unranked} selected job${unranked === 1 ? ' is' : 's are'} not ranked yet. Rank ${unranked === 1 ? 'it' : 'them'} first: tailoring works from the ranking.` : 'Send the selected ranked jobs to Bulk tailor')}">${btn(label('bulkToTailor', `Bulk tailor${tailorable ? ` ${tailorable}` : ''}`, 'Queueing…'), 'bulk-to-tailor', {}, 'sm primary', !tailorable || isBusy('bulkToTailor'))}</span>` : ''}
      ${btn('Archive', 'bulk', { action: 'archive' }, 'sm')}
      ${btn('Dismiss', 'bulk', { action: 'dismiss' }, 'sm')}
      ${btn(label('bulkDelete', `Delete ${S.checked.size}`, 'Deleting…'), 'bulk-delete', {}, 'sm danger', isBusy('bulkDelete'))}
      ${f.domain !== 'matching' ? btn(label('bulkKeep', `Keep ${S.checked.size}`, 'Keeping…'), 'bulk-domain-keep', {}, 'sm soft', isBusy('bulkKeep')) : ''}
      ${resumeSelect('bulk-resume', '', { placeholder: 'Use resume…', disabled: isBusy('bulkResume'), fill: false })}
      <span class="grow"></span>${btn('Clear selection', 'clear-selection', {}, 'sm ghost')}
    </div>` : ''}
    <div class="row between wrap" style="padding:9px 14px;border-bottom:1px solid var(--line2);background:#fbfbfe"><span class="small muted">${j.total ? `Showing ${start}–${Math.min(j.page * j.per_page, j.total)} of ${j.total} jobs` : 'No matches'}</span><span class="small muted">Page ${j.page} of ${j.pages} · ${j.total + (j.applied_hidden || 0) === j.total_all ? 'no filters applied' : `${j.total_all - j.total - (j.applied_hidden || 0)} hidden by filters`}${j.applied_hidden ? ` · ${j.applied_hidden} applied job${j.applied_hidden === 1 ? '' : 's'} hidden` : ''}</span></div>
    ${allOnPage && j.total > j.items.length && S.checked.size < j.total ? `<div class="banner" style="border-radius:0;border-width:0 0 1px">All ${j.items.length} on this page are selected. <a href="#" data-act="select-all-matching">${isBusy('selectAll') ? 'Selecting…' : `Select all ${j.total} matching jobs`}</a></div>` : ''}
    ${S.checked.size && S.checked.size >= j.total && j.total > j.items.length ? `<div class="banner" style="border-radius:0;border-width:0 0 1px">All ${S.checked.size} matching jobs are selected. <a href="#" data-act="clear-selection">Clear selection</a></div>` : ''}
    <div class="jgrid jhead"><input type="checkbox" data-act="check-all" ${allOnPage ? 'checked' : ''} aria-label="Select page"><span>OPPORTUNITY</span><span class="tr">FIT</span><span>STAGE</span><span>SOURCE</span><span class="tr">POSTED</span></div>
    ${j.items.map(r => `<div class="jgrid jrow ${r.id === S.selJob ? 'sel' : ''}" data-act="select-job" data-id="${r.id}">
      <input type="checkbox" data-act="check" data-id="${r.id}" ${S.checked.has(r.id) ? 'checked' : ''} aria-label="Select">
      <div style="min-width:0"><div class="b ellipsis" style="font-size:13.5px">${esc(r.title)}</div><div class="xs muted ellipsis">${esc(r.company)}${r.location ? ' · ' + esc(r.location) : ''}</div></div>
      <div class="tr mono bb ${fitCls(r.fit)}" style="font-size:13.5px">${fitTxt(r.fit)}</div><div class="col gap4" style="align-items:flex-start">${r.domain_match === 'NOT_MATCHING' ? `<span title="${attr(r.domain_reason || 'Outside your domain')}">${pill('Not matching', 'warn')}</span>` : r.domain_match === 'PENDING' ? pill('AI check', 'muted') : r.domain_match === 'REVIEW' ? pill('Needs review', 'warn') : statusPill(r.status)}${descBadge(r.description_state)}</div><div class="xs muted">${esc(r.board)}</div><div class="tr xs ${r.posted_ago || r.posted_raw ? 'muted' : 'faint'}" title="${attr(`found ${r.posted}`)}">${esc(r.posted_ago || r.posted_raw || `found ${r.posted}`)}</div></div>`).join('')}
    ${!j.total ? `<div class="empty small muted">No jobs match these filters. <a href="#" data-act="reset-filters">Reset</a> to see all ${j.total_all}.</div>` : ''}
    <div class="row wrap gap10 pager" style="padding:12px 14px;background:#fbfbfe"><div class="row gap6"><span class="small muted">Rows</span><select class="select" style="margin:0;width:auto;padding:6px 8px" id="flt-per" data-filter="per_page">${opts([[10, 10], [25, 25], [50, 50]], f.per_page)}</select></div><div class="grow"></div>
      ${btn('← Prev', 'page', { page: j.page - 1 }, 'sm', j.page <= 1)}${pages.map(p => btn(String(p), 'page', { page: p }, 'sm ' + (p === j.page ? 'on' : ''))).join('')}${btn('Next →', 'page', { page: j.page + 1 }, 'sm', j.page >= j.pages)}</div>
  </section>
  <section class="card pad18 c-side sticky-panel">${sel ? `
    <div class="row top-align between gap10"><div class="grow"><div style="font-size:17px;font-weight:600">${esc(sel.title)}</div><div class="base muted mt4">${esc(sel.company)}${sel.location ? ' · ' + esc(sel.location) : ''}</div></div><div class="mono bb ${fitCls(sel.fit)}" style="font-size:24px">${fitTxt(sel.fit)}</div></div>
    <div class="row gap6 wrap" style="margin:14px 0">${statusPill(sel.status)}${pill(sel.board + ' · found ' + sel.posted, 'plain', 'lg')}${postedTxt(sel) ? pill(postedTxt(sel), 'plain', 'lg') : ''}${pill(`${sel.matches.length} matched`, 'good', 'lg')}${pill(`${sel.gaps.length + sel.missing.length} gaps`, 'warn', 'lg')}</div>
    ${sel.domain_match === 'NOT_MATCHING' ? `<div class="banner warn mb12" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><span class="grow"><b>Hidden: not matching.</b> ${esc(sel.domain_reason || 'Outside your domain')}. It is not ranked further.</span>${btn(label('keep' + sel.id, 'Keep this job', 'Keeping…'), 'domain-keep', { id: sel.id }, 'sm soft', isBusy('keep' + sel.id))}</div>` : domainCheckNote(sel)}
    <div class="base pretty" style="color:var(--ink3);line-height:1.6;border-top:1px solid var(--line2);padding-top:14px">${esc(sel.summary || (sel.description || '').slice(0, 420) || 'No description captured.')}</div>
    <div class="mt12"><div class="row gap8" style="align-items:center;min-width:0"><span class="xs muted" style="white-space:nowrap">Resume used</span>${resumeSelect('job-resume', sel.resume_choice_id, { id: sel.id, choice: sel.resume_choice })}</div>${sel.resume_choice === 'AUTO' ? '<div class="xs faint mt4">Matched by resume tags</div>' : ''}</div>
    <div class="row gap8 mt16">${['tailored', 'needs_retailor', 'approved', 'applied', 'pending_approval'].includes(sel.status) ? btn(sel.status === 'needs_retailor' ? 'Re-tailor' : 'Open packet', 'go', { screen: sel.status === 'applied' ? 'tracker' : sel.status === 'approved' ? 'approvals' : 'tailor', job: sel.id }, 'primary grow') : sel.needs_description ? `<span class="grow" title="Ranking needs the job description">${btn('Add description', 'open-job', { id: sel.id }, 'primary grow')}</span>`
      : needsParse(sel) ? parseBtn(sel, 'primary')
      : sel.fit == null ? `<span class="grow" title="Tailoring works from the ranking: rank this job first">${btn(label('rank' + sel.id, 'Rank first', 'Queueing…'), 'rank-job', { id: sel.id }, 'primary', isBusy('rank' + sel.id))}</span>`
      : btn('Tailor', 'triage', { id: sel.id, action: 'shortlist', tailor: 1 }, 'primary grow')}${btn('Full description', 'open-job', { id: sel.id }, 'grow')}</div>` : empty('Select a job', 'Pick a row to see its details.')}
  </section></div>`;
}
function renderSetup() {
  const data = D.searchProfile; if (!data) return loading();
  const setup = curSetup();
  const p = data.profile, F = 'sp';
  const kind = setupField('schedule_kind', 'manual'), days = setupField('weekdays', []) || [];
  const roles = fv(F, 'roles', p.roles), sites = fv(F, 'sites', p.sites);
  const suggestions = (D.summary && D.profileRoles || []).filter(r => !roles.includes(r)).slice(0, 3);
  const actors = fv(F, 'actors', p.actors || []), customs = fv(F, 'custom_actors', p.custom_actors || []);
  const catalogList = [...(D.actors?.actors || []).filter(a => !a.custom).map(a => ({ ...a, group: a.group || 'Job boards' })),
    ...customs.map(c => ({ ...c, name: c.name || c.id, summary: 'Added by you · listed in every setup', cost: "see the actor's pricing", custom: true, group: 'Your actors' }))];
  const on = id => actors.includes(id);
  const startup = Object.fromEntries(Object.entries(STARTUP_ACTORS).map(([k, id]) => [k, on(id)]));
  const platforms = fv(F, 'remote_platforms', p.remote_platforms || []), seniority = fv(F, 'seniority', p.seniority || []);
  const hint = catalogList.filter(a => on(a.id) && a.location_hint).map(a => a.location_hint).join(' ');
  const preview = D.spPreview;
  const run = D.runDetail;
  const boardName = { linkedin: 'LinkedIn', indeed: 'Indeed', glassdoor: 'Glassdoor', naukri: 'Naukri', bayt: 'Bayt', zip_recruiter: 'ZipRecruiter' };
  return `<div class="split">
  <section class="card flush c-main">
    <div style="padding:18px 22px;border-bottom:1px solid var(--line2)"><div class="card-title" style="font-size:16px">Search setups</div><div class="card-sub">Save as many searches as you like — run one whenever you want, or let it run on its own schedule.</div>
      <div class="row gap6 wrap mt12">${(D.setups || []).map(x => `<button type="button" class="btn sm ${x.id === setup?.id ? 'soft' : ''}" data-act="select-setup" data-id="${x.id}" title="${attr(x.schedule_label)}"><span class="dot" style="width:6px;height:6px;margin-right:6px;background:${x.schedule_kind !== 'manual' && x.enabled ? 'var(--good)' : 'var(--line)'}"></span>${esc(x.name)}</button>`).join('')}
        ${btn(label('newSetup', '+ New setup', 'Creating…'), 'new-setup', {}, 'sm ghost', isBusy('newSetup'))}</div></div>
    ${data.apify_configured ? '' : `<div class="banner warn" style="margin:16px 22px 0">APIFY_API_TOKEN is not set on the server, so searches cannot start. You can still add jobs manually.</div>`}
    <div class="col gap18" style="padding:20px 22px">
      <label class="field strong">Setup name${inputF(F, 'name', setup?.name || 'My search', 'maxlength="120" placeholder="e.g. Lead AI Engineer · remote"')}</label>
      <div><div class="small b mb8">Roles <span class="faint" style="font-weight:400">· up to 5</span></div>
        <div class="tagbox" data-tag-input="roles" data-act="focus-tag" data-target="sp-roles">${chips(F, 'roles', p.roles)}<input id="sp-roles" data-tag="roles" data-form="sp" data-max="5" autocomplete="off" placeholder="Type a role, then press Enter"></div>
        <div class="helper">Press Enter to add each role · Backspace removes the last one${suggestions.length ? ` · Suggested from your profile: ${suggestions.map(s => `<a href="#" data-act="suggest-role" data-role="${attr(s)}">${esc(s)}</a>`).join(' · ')}` : ''}</div></div>
      <div><div class="small b mb8">Primary skills <span class="faint" style="font-weight:400">· optional, up to 3</span></div>
        ${tagBox(F, 'primary_skills', p.primary_skills || [], 'Python, Java… then press Enter', 3)}
        <div class="helper">The first skill is added to each role's search (Backend Engineer Python) · jobs that mention none of them are hidden as Not matching · your own actors read them as <span class="mono">{{skill}}</span> and <span class="mono">{{skills}}</span></div></div>
      <div><div class="small b mb8">Locations <span class="faint" style="font-weight:400">· up to 5, one search each</span></div>
        <div class="tagbox" data-tag-input="locations" data-act="focus-tag" data-target="sp-locations">${chips(F, 'locations', p.locations)}<input id="sp-locations" data-tag="locations" data-form="sp" data-max="5" data-split="newline" autocomplete="off" placeholder="Type a city or Remote, then press Enter"></div>
        <div class="helper">Press Enter to add each location — commas stay part of it (New York, NY) · leave empty to search anywhere</div></div>
      <section aria-labelledby="actors-heading"><div class="row base between mb8"><h3 class="small b" id="actors-heading" style="margin:0">Apify actors <span class="faint" style="font-weight:400" aria-live="polite">· ${actors.length ? `${actors.length} selected` : 'pick one or more'}</span></h3><button type="button" class="textbtn xs" data-act="toggle-add-actor" aria-expanded="${S.addActor ? 'true' : 'false'}">${S.addActor ? 'Close' : '+ Add your own actor'}</button></div>
        <div class="col gap14">${catalogList.length ? actorPicker(catalogList, on) : '<span class="small muted">Loading actors…</span>'}</div>
        ${hint ? `<div class="helper" role="note" style="color:var(--warn)">${esc(hint)}</div>` : ''}
        ${S.addActor ? addActorForm() : ''}</section>
      ${on('openclawai/job-board-scraper') ? `<div class="box soft" style="padding:14px"><div class="small b mb8">Multi Job Board settings</div>
        <div class="grid-auto w140">${Object.keys(boardName).map(b => `<label class="board-opt ${sites.includes(b) ? 'on' : ''}"><input type="checkbox" data-act="toggle-site" data-site="${b}" ${sites.includes(b) ? 'checked' : ''}><span class="grow">${boardName[b]}</span><span class="tiny mono faint" title="kept per 100 found, last 30 days">${data.board_yield[b] ?? '—'}</span></label>`).join('')}</div>
        <div class="helper">Numbers show jobs kept per 100 found over the last 30 days.</div>
        <div class="grid-auto mt12"><label class="field">Indeed &amp; Glassdoor country${inputF(F, 'country_indeed', p.country_indeed, 'list="indeed-countries" placeholder="usa, uk, india…"')}<datalist id="indeed-countries">${COUNTRIES.map(c => `<option value="${c}">`).join('')}</datalist></label>
          <label class="field">Search radius (miles)${inputF(F, 'distance', p.distance, 'type="number" min="0" max="500"')}</label>
          <label class="field">Skip first N results${inputF(F, 'offset', p.offset, 'type="number" min="0" max="1000"')}</label></div>
        <div class="mt12"><div class="xs muted mb4">LinkedIn company IDs <span class="faint">· optional, restricts LinkedIn to these companies</span></div>${tagBox(F, 'linkedin_company_ids', p.linkedin_company_ids || [], 'e.g. 1441, then Enter', 20)}</div>
        <div class="helper mt8" role="note">Descriptions: this actor returns full descriptions for <b>LinkedIn</b> (with the option below) and <b>Indeed</b> only — Naukri, ZipRecruiter and Bayt listings arrive without one.${sites.includes('naukri') ? (on('crawlerbros/naukri-scraper') ? ' <span style="color:var(--warn)">Naukri is also searched by the Naukri actor (with descriptions): untick Naukri here so it is not scraped twice.</span>' : ' For Naukri with descriptions, add the <b>Naukri</b> actor above and untick Naukri here.') : ''}</div>
        <div class="row gap16 wrap mt12">${checkF(F, 'fetch_description', p.fetch_description, 'Fetch full LinkedIn descriptions')}${checkF(F, 'enforce_annual_salary', p.enforce_annual_salary, 'Normalize salaries to annual')}${checkF(F, 'residential_proxy', p.residential_proxy, 'Residential proxy (recommended for LinkedIn)')}</div></div>` : ''}
      ${on('benthepythondev/remote-jobs-aggregator') ? `<div class="box soft" style="padding:14px"><div class="small b mb8">Remote Jobs Aggregator boards</div>
        <div class="grid-auto w140">${(D.actors?.remote_platforms || []).map(pl => `<label class="board-opt ${platforms.includes(pl) ? 'on' : ''}"><input type="checkbox" data-act="toggle-list" data-key="remote_platforms" data-value="${pl}" ${platforms.includes(pl) ? 'checked' : ''}><span class="grow">${pl}</span></label>`).join('')}</div></div>` : ''}
      ${on('shahidirfan/Remoteok-Job-Scraper') ? `<div class="box soft" style="padding:14px"><label class="field strong">RemoteOK start page${inputF(F, 'remoteok_url', p.remoteok_url, 'placeholder="https://remoteok.com/remote-dev-jobs"')}</label>
        <div class="helper">Use a category page such as https://remoteok.com/remote-dev-jobs to narrow results before the keyword filter.</div></div>` : ''}
      ${Object.values(startup).some(Boolean) ? `<fieldset class="box soft actor-settings"><legend class="small b">Startup board settings</legend>
        ${startup.waas || startup.yc ? `<div class="mt8"><label class="xs muted mb4" for="sp-yc_batches" style="display:block">YC batches <span class="faint">· optional, e.g. W24 or S23${startup.yc ? ' · Y Combinator Jobs uses the first one' : ''}</span></label>${tagBox(F, 'yc_batches', p.yc_batches || [], 'e.g. W24, then Enter', 10)}</div>` : ''}
        ${startup.hn || startup.wellfound ? `<div class="grid-auto mt12">
          ${startup.hn ? `<label class="field">HN thread month${inputF(F, 'hn_month', p.hn_month || 'current', 'placeholder="current or 2026-09" pattern="current|\\d{4}-(0[1-9]|1[0-2])" aria-describedby="hn-month-help"')}<span class="helper" id="hn-month-help">"current" is the latest Who is hiring? thread.</span></label>` : ''}
          <label class="field">Minimum salary (USD / year)${inputF(F, 'min_salary', p.min_salary || 0, 'type="number" min="0" step="5000" inputmode="numeric" aria-describedby="use-min_salary"')}${filterNote(catalogList, on, 'min_salary')}</label></div>` : ''}
        ${startup.waas || startup.yc ? `<div class="row gap16 wrap mt12">${checkF(F, 'startup_details', p.startup_details ?? true, 'Fetch full descriptions &amp; company details', 'aria-describedby="startup-details-help"')}</div>
          <div class="helper" id="startup-details-help">Needed for ranking; turning it off is faster and cheaper but most jobs arrive without a description.</div>` : ''}
        ${startup.hn ? '<div class="helper">HN Who\'s Hiring costs $0.02 per job — keep “Results per search” low.</div>' : ''}</fieldset>` : ''}
      <section aria-labelledby="filters-heading"><h3 class="small b mb8" id="filters-heading" style="margin-top:0">Filters <span class="faint" style="font-weight:400">· each actor receives the filters it supports</span></h3><div class="grid-auto">
        <label class="field">Posted within${selectF(F, 'hours_old', p.hours_old, [[1, 'Past hour'], [24, 'Past 24 hours'], [72, 'Past 3 days'], [168, 'Past 7 days'], [720, 'Past 30 days'], [0, 'Any time']], 'aria-describedby="use-hours"')}${filterNote(catalogList, on, 'hours')}</label>
        <label class="field">Job type${selectF(F, 'job_type', p.job_type, [['', 'Any job type'], ['fulltime', 'Full-time'], ['parttime', 'Part-time'], ['contract', 'Contract'], ['internship', 'Internship'], ['temporary', 'Temporary']], 'aria-describedby="use-job_type"')}${filterNote(catalogList, on, 'job_type')}</label>
        <label class="field">Results per search${inputF(F, 'max_results', p.max_results, 'type="number" min="1" max="100" aria-describedby="max-results-help"')}<span class="helper" id="max-results-help">The most jobs one run brings in — Multi Job Board splits it across boards and roles. Other actors use it for each run they start.</span></label>
        </div>
        <div class="mt12" role="group" aria-labelledby="seniority-label" aria-describedby="use-seniority"><div class="xs muted mb4" id="seniority-label">Seniority</div><div class="row gap6 wrap">${(D.actors?.seniority || []).map(s => `<button type="button" class="btn sm ${seniority.includes(s) ? 'soft' : ''}" data-act="toggle-list" data-key="seniority" data-value="${s}" aria-pressed="${seniority.includes(s) ? 'true' : 'false'}">${seniority.includes(s) ? '✓ ' : ''}${s}</button>`).join('')}</div>${filterNote(catalogList, on, 'seniority')}</div>
        <div class="mt12"><label class="xs muted mb4" for="sp-exclude_keywords" style="display:block">Exclude titles or keywords</label>${tagBox(F, 'exclude_keywords', p.exclude_keywords || [], 'e.g. intern, then Enter', 20)}${filterNote(catalogList, on, 'exclude_keywords')}</div>
        <div class="row gap16 wrap mt12">${usedCheck(catalogList, on, 'remote', 'is_remote', p.is_remote, 'Remote only')}${usedCheck(catalogList, on, 'easy_apply', 'easy_apply', p.easy_apply, 'Easy Apply only')}${usedCheck(catalogList, on, 'salary_only', 'salary_only', p.salary_only, 'Salary listed only')}${usedCheck(catalogList, on, 'visa_sponsorship', 'visa_sponsorship', p.visa_sponsorship, 'Offers visa sponsorship')}${usedCheck(catalogList, on, 'skip_agencies', 'skip_agencies', p.skip_agencies, 'Skip agencies &amp; reposts')}${checkF(F, 'auto_rank', p.auto_rank, 'Auto-rank on import')}</div>
        <label class="field strong mt12">Resume for these jobs<span class="xs muted" style="font-weight:400"> — ranking, tailoring and review of jobs this search finds use it</span>${selectF(F, 'resume_select', setupResumeValue(p), [['auto', 'Auto — best match by resume tags (default)'], ['active', 'Active resume'], ...(D.resumeChoices || []).map(r => { const d = tagGroups(r.domains, r.domain, r.roles).map(g => g.domain).filter(Boolean).join(', '); return [r.id, `${d ? `${d}: ` : ''}${resumeLabel(r)}`]; })])}</label>
        <div class="helper">“Not used” means none of the actors you picked reads that setting.</div></section>
      <div class="box soft" style="padding:14px"><div class="row between wrap gap8 mb8"><span class="small b">Schedule <span class="faint" style="font-weight:400">· this setup only</span></span>
        ${kind === 'manual' ? '' : checkF(F, 'enabled', setup ? setup.enabled : true, 'Scheduled')}</div>
        <div class="grid-auto"><label class="field">When${selectF(F, 'schedule_kind', kind, SCHEDULE_KINDS)}</label>
          ${kind === 'interval' ? `<label class="field">Every N hours${inputF(F, 'every_hours', setupField('every_hours', 24), 'type="number" min="1" max="168"')}</label>` : ''}
          ${kind === 'daily' || kind === 'weekly' ? `<label class="field">At${inputF(F, 'time_of_day', setupField('time_of_day', '07:00'), 'type="time"')}</label>
            <label class="field">Time zone${inputF(F, 'timezone', setupField('timezone', localZone()), 'placeholder="Asia/Kolkata"')}</label>` : ''}</div>
        ${kind === 'weekly' ? `<div class="mt12"><div class="xs muted mb4">Days</div><div class="row gap6 wrap">${DAY_NAMES.map((d, i) => `<button type="button" class="btn sm ${days.includes(i) ? 'soft' : ''}" data-act="toggle-day" data-day="${i}">${days.includes(i) ? '✓ ' : ''}${d}</button>`).join('')}</div></div>` : ''}
        <div class="helper">${kind === 'manual' ? 'This setup only runs when you press Run now.'
          : setup?.next_run_at && setup.schedule_kind === kind && setup.enabled ? `Next run ${fmtDateTime(setup.next_run_at)}${setup.last_run_at ? ` · last ran ${fmtDateTime(setup.last_run_at)}` : ''}`
          : 'Save the setup to schedule it.'}</div></div>
    </div>
    <div class="card-foot row gap12 wrap">
      ${btn(label('run', 'Run now &amp; triage', 'Starting…'), 'run-search', {}, 'primary lg', isBusy('run') || !!S.polling || !setup)}
      ${btn(label('saveSp', 'Save', 'Saving…'), 'save-search', {}, 'lg', isBusy('saveSp'))}
      ${btn(label('saveAsNew', 'Save as new', 'Saving…'), 'save-as-new', {}, '', isBusy('saveAsNew'))}
      ${setup ? btn(label('dupSetup', 'Duplicate', 'Copying…'), 'duplicate-setup', {}, '', isBusy('dupSetup')) : ''}
      ${setup && (D.setups || []).length > 1 ? btn(label('delSetup', 'Delete', 'Deleting…'), 'delete-setup', {}, 'ghost danger', isBusy('delSetup')) : ''}
      <span class="small muted grow">${!preview ? 'Planning runs…' : preview.error ? esc(preview.error)
        : `${preview.runs.length} run${preview.runs.length === 1 ? '' : 's'} across ${new Set(preview.runs.map(r => r.actor_id)).size} actor${new Set(preview.runs.map(r => r.actor_id)).size === 1 ? '' : 's'}`}</span>
      ${preview?.runs?.length ? btn(S.showInputs ? 'Hide inputs' : 'Show exact inputs', 'toggle-inputs', {}, 'sm ghost') : ''}</div>
    ${preview?.warnings?.length ? `<div class="banner warn" style="margin:0 22px 16px;display:block">${preview.warnings.map(w => `<div>• ${esc(w)}</div>`).join('')}</div>` : ''}
    ${S.showInputs && preview?.runs?.length ? `<div style="margin:0 22px 18px" class="doc clip"><pre class="plain mono" style="font-size:11px">${esc(JSON.stringify(preview.runs.map(r => ({ actor: r.actor_id, location: r.location, input: r.input })), null, 2))}</pre></div>` : ''}
  </section>
  <div class="c-narrow col gap14">
    <section class="card flush"><div class="card-head"><div class="row between wrap gap8"><div class="card-title sm">Recent runs</div>
        <div class="row gap6">${D.runs.some(r => !r.completed_at) ? btn(label('refreshPending', `Refresh ${D.runs.filter(r => !r.completed_at).length} pending`, 'Checking…'), 'refresh-pending', {}, 'sm', isBusy('refreshPending') || !data.apify_configured) : ''}${btn(S.apifyImport ? 'Close import' : 'Import from Apify', 'toggle-apify-import', {}, 'sm soft', !data.apify_configured)}</div></div>
        <div class="card-sub">Pick a run to see exactly what it searched and everything it pulled.</div></div>
      ${S.apifyImport ? renderApifyImport() : ''}
      ${D.runs.length ? D.runs.slice(0, 6).map(r => `<div class="lrow click ${r.id === S.runSel ? 'sel' : ''}" data-act="select-run" data-id="${r.id}" style="align-items:flex-start"><span class="dot" style="width:7px;height:7px;margin-top:6px;background:${r.status === 'SUCCEEDED' ? '#0f9c86' : r.completed_at ? '#c22f4e' : '#e0a136'}"></span><div class="grow"><div class="row base gap6 wrap"><span class="small b">Run #${r.id} · ${esc(r.status.toLowerCase())}</span>${r.saved_search_name ? pill(r.saved_search_name, 'plain') : ''}</div><div class="xs muted mt4"><span class="mono">${esc((r.actor_id || '').split('/').pop())}</span> · ${r.created_jobs} new from ${r.item_count} results · ${fmtDateTime(r.created_at)}</div></div></div>`).join('') : `<div class="small muted" style="padding:16px">No runs yet.</div>`}
      ${run ? `<div style="padding:16px;background:#fbfbfe">
        <div class="row base between wrap gap8 mb12"><div class="base b">Run #${run.id}</div><div class="row gap6">${pill(run.status, run.status === 'SUCCEEDED' ? 'good' : run.completed ? 'bad' : 'warn')}${data.apify_configured ? btn(label('pull' + run.id, run.completed ? 'Pull again' : 'Check status &amp; pull', 'Checking…'), 'pull-run', { id: run.id, reimport: run.completed ? '1' : '' }, 'sm', isBusy('pull' + run.id)) : ''}</div></div>
        <div class="grid-auto w96 mb14">${[['pulled', 'Pulled', 'var(--ink)'], ['new', 'New', 'var(--good)'], ['duplicates', 'Duplicates', 'var(--muted)'], ['filtered', 'Filtered out', 'var(--warn)']].map(([k, l, c]) => `<div class="box" style="padding:9px 10px"><div class="mono bb" style="font-size:17px;color:${c}">${run.counts[k]}</div><div class="tiny muted">${l}</div></div>`).join('')}</div>
        <div class="box mb12"><div class="caps mb8" style="font-size:11px">WHAT IT SEARCHED</div><div class="col gap6">${run.context.map(([k, v]) => `<div class="row top-align small" style="gap:10px"><span class="faint none" style="width:72px">${esc(k)}</span><span class="ink2 grow pretty">${esc(v)}</span></div>`).join('')}</div></div>
        <div class="box" style="padding:0;overflow:hidden"><div class="row between" style="padding:10px 12px;border-bottom:1px solid var(--line3)"><span class="caps" style="font-size:11px">EVERYTHING IT PULLED</span><span class="tiny faint">showing ${Math.min(6, run.results.length)} of ${run.counts.new}</span></div>
          ${run.results.slice(0, 6).map(r => `<div class="row gap9" style="padding:9px 12px;border-bottom:1px solid #f8f9fd;cursor:pointer" data-act="open-job" data-id="${r.id}"><span class="mono bb small ${fitCls(r.fit)}" style="width:22px">${fitTxt(r.fit)}</span><div class="grow"><div class="small b ellipsis">${esc(r.title)}</div><div class="tiny muted ellipsis">${esc(r.company)} · ${esc(r.board)}</div></div>${pill(r.verdict, r.verdict === 'new' ? 'good' : 'warn')}</div>`).join('') || '<div class="small muted" style="padding:12px">No new jobs were imported by this run.</div>'}
          <button class="linkbtn" data-act="go" data-screen="discovery" data-mode="table">See every job in the table →</button></div></div>` : ''}
    </section>
    ${renderManualAdd()}
    <section class="card pad16"><div class="card-title sm mb4">Import a company board</div><div class="small muted mb12">Pull the open roles from a company's Greenhouse or Lever board — all of them, or only the teams and roles you pick. No Apify token needed.</div>
      <div class="row gap8">${selectF('board', 'kind', 'greenhouse', [['greenhouse', 'Greenhouse'], ['lever', 'Lever']], 'style="width:auto"')}${inputF('board', 'token', '', 'placeholder="Board token, e.g. stripe"')}</div>
      <div class="helper">The token is the company slug in boards.greenhouse.io/<b>stripe</b> or jobs.lever.co/<b>stripe</b>.</div>
      <label class="field mt8">Role keywords <span class="faint">(optional)</span>${inputF('board', 'roles', '', 'placeholder="e.g. engineer, data scientist" aria-describedby="board-roles-help"')}<span class="helper" id="board-roles-help">Only roles whose title has one of these words. Separate several with commas.</span></label>
      <div class="mt8"><div class="row base between"><span class="xs muted">Teams <span class="faint">(optional)</span></span>${btn(label('boardTeams', S.boardTeams ? 'Refresh teams' : 'Find teams', 'Looking…'), 'board-teams', {}, 'ghost sm', isBusy('boardTeams'))}</div>
        ${S.boardTeams ? (S.boardTeams.length ? `<div class="row gap6 wrap mt6" role="group" aria-label="Teams">${S.boardTeams.map(t => { const on = (fv('board', 'teams', []) || []).includes(t.name); return `<button type="button" class="btn sm ${on ? 'soft' : ''}" data-act="toggle-board-team" data-team="${attr(t.name)}" aria-pressed="${on ? 'true' : 'false'}">${on ? '✓ ' : ''}${esc(t.name)} <span class="tiny faint">${t.count}</span></button>`; }).join('')}</div>` : '<div class="helper">This board does not list teams — use role keywords instead.</div>') : '<div class="helper">Look up the board to choose from its teams. Nothing picked imports every open role.</div>'}</div>
      <div class="row gap8 mt8">${btn(label('boardReview', 'Review roles first', 'Loading…'), 'board-review', {}, 'ghost grow', isBusy('boardReview'))}${btn(label('board', 'Import board', 'Importing…'), 'import-board', {}, 'grow', isBusy('board'))}</div></section>
  </div></div>`;
}

// Discovery → Search setup: add one job by its link, an uploaded description (PDF, DOCX, TXT) or pasted text. A file
// is only read into a draft (/api/jobs/read-file); the person checks the fields, then the text path adds it.
const MANUAL_MODES = [['link', 'Link'], ['file', 'PDF file'], ['text', 'Paste text']];
function renderManualAdd() {
  const mode = S.manualMode || 'link';
  const seg = `<div class="seg mb12" role="group" aria-label="Add a job by">${MANUAL_MODES.map(([id, t]) => `<button type="button" class="${mode === id ? 'on' : ''}" data-act="manual-mode" data-id="${id}" aria-pressed="${mode === id}">${t}</button>`).join('')}</div>`;
  const fields = (urlLabel) => `<div class="col gap8">${inputF('manual', 'title', '', 'placeholder="Job title" aria-label="Job title"')}${inputF('manual', 'company', '', 'placeholder="Company" aria-label="Company"')}${inputF('manual', 'location', '', 'placeholder="Location (optional)" aria-label="Location"')}${urlLabel ? inputF('manual', 'url', '', `placeholder="${urlLabel}" aria-label="Posting link"`) : ''}</div>`;
  const sub = { link: 'Paste any job URL — we\'ll pull the title, company and description.',
    file: 'Upload the job description as a PDF (or DOCX / TXT). Check the details, then add it.',
    text: 'Paste the job description and fill in the title and company.' }[mode];
  let body;
  if (mode === 'link') {
    body = `${inputF('manual', 'url', '', 'placeholder="https://…" aria-label="Job URL"')}
      ${S.manualMissing ? `<div class="col gap8 mt8"><div class="xs" style="color:var(--warn)">${esc(S.manualMissing)}</div>${inputF('manual', 'title', '', 'placeholder="Job title"')}${inputF('manual', 'company', '', 'placeholder="Company"')}${inputF('manual', 'location', '', 'placeholder="Location (optional)"')}${textareaF('manual', 'description', '', 'placeholder="Paste the description"')}</div>` : ''}
      ${btn(label('manual', 'Add to pipeline', 'Reading posting…'), 'add-manual', {}, 'block mt8', isBusy('manual'))}`;
  } else if (mode === 'file') {
    const name = fv('manual', 'file_name', '');
    body = `<label class="settings-upload" style="margin-top:0" for="manual-jd-file"><strong>${isBusy('manualFile') ? 'Reading file…' : name ? `＋ Replace ${esc(name)}` : '＋ Choose a job description'}</strong><span>PDF, DOCX or TXT · up to 10 MB</span><input type="file" id="manual-jd-file" accept=".pdf,.docx,.txt" aria-label="Upload job description" data-act-change="manual-file"${isBusy('manualFile') ? ' disabled' : ''}></label>
      ${name ? `<div class="mt12">${fields('Posting link (optional)')}<div class="mt8">${textareaF('manual', 'description', '', 'rows="8" aria-label="Job description"')}</div><div class="helper">Text read from ${esc(name)} — edit anything it got wrong.</div></div>
      ${btn(label('manual', 'Add to pipeline', 'Adding…'), 'add-manual-text', {}, 'block mt8', isBusy('manual'))}` : ''}`;
  } else {
    body = `${fields('Posting link (optional)')}<div class="mt8">${textareaF('manual', 'description', '', 'rows="8" placeholder="Paste the full job description" aria-label="Job description"')}</div>
      ${btn(label('manual', 'Add to pipeline', 'Adding…'), 'add-manual-text', {}, 'block mt8', isBusy('manual'))}`;
  }
  return `<section class="card pad16"><div class="card-title sm mb4">Add one manually</div><div class="small muted mb12">${sub}</div>${seg}${body}</section>`;
}

/* ───────────── packet filters (Tailor, Approvals) ───────────── */
const PF_STATUS = {
  tailor: [['all', 'All packets'], ['not_tailored', 'Not tailored'], ['has_packet', 'Has packet'], ['needs_retailor', 'Needs re-tailoring'], ['sent_back', 'Sent back'], ['untailored', 'Master resume only']],
  approvals: [['all', 'All statuses'], ['starred', 'Starred'], ['waiting', 'Waiting'], ['sent_back', 'Sent back'], ['approved', 'Approved']],
};
const PF_SORT = {
  tailor: [['fit', 'Sort: best fit'], ['reviewer', 'Sort: reviewer score'], ['new', 'Sort: newest'], ['company', 'Sort: company']],
  approvals: [['default', 'Sort: waiting first'], ['reviewer', 'Sort: reviewer score'], ['fit', 'Sort: best fit'], ['new', 'Sort: newest'], ['company', 'Sort: company']],
};
const pfActive = kind => JSON.stringify(S.pf[kind]) !== JSON.stringify(pfDefaults(kind));
function renderPacketFilters(kind, facets) {
  const f = S.pf[kind];
  const opts = (list, cur) => list.map(([v, t]) => `<option value="${attr(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(t)}</option>`).join('');
  const sel = (key, list) => `<select class="select" style="margin:0;width:auto" id="pf-${kind}-${key}" data-pf="${kind}" data-pfkey="${key}">${opts(list, f[key])}</select>`;
  return `<section class="card flush"><div class="toolbar" style="border-bottom:0">
    <input class="input grow" style="min-width:180px;margin:0" id="pf-${kind}-q" data-pf="${kind}" data-pfkey="q" value="${attr(f.q)}" placeholder="Search title, company or location…">
    ${sel('min_fit', [[0, 'Any fit'], [50, 'Fit ≥ 50'], [70, 'Fit ≥ 70'], [80, 'Fit ≥ 80']])}
    ${sel('min_review', [[0, 'Any reviewer score'], [50, 'Reviewer ≥ 50'], [65, 'Reviewer ≥ 65'], [75, 'Reviewer ≥ 75'], [85, 'Reviewer ≥ 85']])}
    ${sel('board', [['all', 'All boards'], ...facets.boards.map(b => [b, b])])}
    ${sel('location', locationOptions(facets.locations))}
    ${'easy_apply' in f ? sel('easy_apply', [['all', 'Any apply method'], ['only', `Easy Apply only (${facets.easy_apply || 0})`]]) : ''}
    ${sel('status', PF_STATUS[kind])}${sel('sort', PF_SORT[kind])}
    <span class="small muted none">${facets.total === facets.total_all ? `${facets.total_all} job${facets.total_all === 1 ? '' : 's'}` : `${facets.total} of ${facets.total_all}`}</span>
    ${btn('Reset', 'pf-reset', { kind }, 'sm', !pfActive(kind))}
  </div></section>`;
}
const pfNoMatch = kind => `<section class="card">${empty('No jobs match these filters', `${S.pf[kind].q ? `Nothing matches “${esc(S.pf[kind].q)}” with the current filters. ` : ''}Loosen or reset the filters to see all ${(kind === 'tailor' ? D.tailorFacets : D.approvalFacets).total_all}.`, btn('Reset filters', 'pf-reset', { kind }, 'primary'))}</section>`;

/* ───────────── tailor ───────────── */
const PACKET_LOCKED = ['approved', 'applied'];
// Background tailoring tasks (/api/tailor-tasks). They run on the server, so leaving or reloading the page is safe.
const STAGE_LABEL = { queued: 'Queued', ranking: 'Ranking', tailoring: 'Tailoring', reviewing: 'Reviewing', done: 'Done' };
const activeTask = jobId => (D.tasks || []).find(t => t.job_id === jobId && (t.status === 'queued' || t.status === 'running'));
const elapsed = iso => { if (!iso) return ''; const sec = Math.max(0, Math.round((Date.now() - new Date(iso.slice(0, 23) + 'Z')) / 1000)); return sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m ${sec % 60}s`; };
// A failed task counts as retried once a newer task exists for the same job, whatever that task's outcome.
const retried = t => (D.tasks || []).some(o => o.job_id === t.job_id && o.id > t.id);
function taskPill(t) {
  if (t.status === 'running') return pill(`${STAGE_LABEL[t.stage] || 'Working'}…`, 'brand');
  if (t.status === 'queued') return pill('queued', 'muted');
  if (t.status === 'failed') return pill('failed', 'bad');
  if (wkStoppedTailor(t)) return pill('stopped', 'muted');
  if (t.status === 'cancelled') return pill('cancelled', 'muted');
  return pill(t.result?.auto_approved ? 'auto-approved' : 'packet ready', 'good');
}
// The Tailor screen only points at the background work; the task list itself is in Running workers → Tailoring.
function renderTaskLink() {
  const tasks = D.tasks || [], running = tasks.filter(t => t.status === 'running').length, queued = tasks.filter(t => t.status === 'queued').length;
  if (!running && !queued) return '';
  return `<section class="banner"><span class="dot" style="background:var(--brand)"></span><span class="grow">${[running && `${running} tailoring now`, queued && `${queued} queued`].filter(Boolean).join(' · ')} · runs on the server, safe to leave this page</span>${btn('Running workers →', 'wk-open', { tab: 'tailoring' }, 'sm')}</section>`;
}
// `tasks` may be narrowed by Running workers' search and status filter; the header still counts every task.
function renderTaskStack(tasks = D.tasks || [], all = tasks) {
  if (!all.length) return '';
  const count = st => all.filter(t => t.status === st).length;
  const running = count('running'), queued = count('queued'), done = count('succeeded'), failed = count('failed');
  const finished = all.length - running - queued, total = all.length;
  const overall = Math.round(all.reduce((sum, t) => sum + t.progress, 0) / total);
  const parts = [running && `${running} running`, queued && `${queued} queued`, done && `${done} done`, failed && `${failed} failed`].filter(Boolean).join(' · ');
  const rows = tasks.map(t => {
    const active = t.status === 'running' || t.status === 'queued';
    const detail = t.status === 'running' ? `${STAGE_LABEL[t.stage]} · ${elapsed(t.started_at)}`
      : t.status === 'queued' ? 'Waiting for a free worker'
      : t.status === 'failed' ? esc(t.error || 'Failed')
      : wkStoppedTailor(t) ? 'Stopped by you · Retry puts it back in the queue'
      : t.status === 'cancelled' ? 'Cancelled'
      : `Reviewer ${fitTxt(t.result?.reviewer_score)}${t.result?.untailored ? ' · master resume' : ''}`;
    const actions = t.status === 'queued' ? btn('Cancel', 'task-cancel', { id: t.id }, 'sm')
      : t.status === 'succeeded' ? btn('Open', 'task-open', { job: t.job_id }, 'sm')
      : t.status === 'failed' || wkStoppedTailor(t) ? (retried(t) ? btn('Retried', 'task-retry', {}, 'sm', true) : btn(isBusy('retry' + t.id) ? 'Retrying…' : 'Retry', 'task-retry', { id: t.id, job: t.job_id, mode: t.mode }, 'sm', isBusy('retry' + t.id))) : '';
    return `<div class="lrow ${t.status === 'running' ? 'sel' : ''}" style="gap:12px">
      ${(t.status === 'failed' || wkStoppedTailor(t)) && !retried(t) && S.screen === 'workers' ? wkSelectBox('tailoring', t.id, t.title) : ''}
      <div class="grow" style="min-width:0"><div class="small b ellipsis">${esc(t.title)} <span class="muted" style="font-weight:400">· ${esc(t.company)}</span></div>
        <div class="xs ${t.status === 'failed' ? '' : 'muted'} ellipsis" style="${t.status === 'failed' ? 'color:var(--bad)' : ''}">${t.mode === 'skip' ? 'Skip tailoring · ' : t.mode === 'review' ? 'Review only · ' : ''}${detail}</div>
        ${active ? `<div class="row base gap8 mt6"><span class="grow">${bar(t.progress + '%', t.status === 'running' ? '#5b4ae8' : '#c9cbe0')}</span><span class="xs mono muted none" style="width:34px;text-align:right">${t.progress}%</span></div>` : ''}</div>
      ${typeof wkLastRun === 'function' ? wkLastRun(t) : ''}${typeof wkModelCells === 'function' ? wkModelCells(t) : ''}${wkTailorActions(t)}${S.screen === 'workers' ? wkRowNudge('tailoring', t.job_id, t.status.toUpperCase()) : ''}${actions}${taskPill(t)}</div>`;
  }).join('');
  return `<section class="card flush">
    <div class="row wrap gap10" style="padding:14px 18px;border-bottom:1px solid var(--line2)">
      <div class="grow" style="min-width:220px"><div class="card-title" style="font-size:16px">${running || queued ? 'Tailoring in the background' : 'Tailoring finished'}</div>
        <div class="card-sub">${parts}${running || queued ? ' · runs on the server, safe to leave this page' : ''}</div>
        <div class="row base gap8 mt8"><span class="grow">${bar(overall + '%', '#5b4ae8')}</span><span class="xs mono muted none">${finished}/${total}</span></div></div>
      ${queued ? btn(`Cancel queued (${queued})`, 'tasks-cancel', {}, 'sm') : ''}
      ${done ? btn('Review in Approvals', 'go', { screen: 'approvals' }, 'sm soft') : ''}
    </div>
    <div style="max-height:420px;overflow:auto" data-scroll-key="tailor-tasks">${rows || '<div class="small muted" style="padding:16px 18px">No tailoring tasks match these filters.</div>'}</div></section>`;
}

// A Tailor-queue job whose latest packet was saved but never reviewed (its review failed or never ran).
const unreviewedPacket = j => j.has_resume && j.reviewer_score == null && j.status !== 'needs_retailor' && !activeTask(j.id);
// What Review sends: unreviewed packets, and with "Redo" ticked every packet (reviewed again, never re-tailored).
const reviewablePacket = j => unreviewedPacket(j) || (S.tailorRegenerate && j.has_resume && j.status !== 'needs_retailor' && !activeTask(j.id));

function renderBulkTailor(queue) {
  const selected = queue.filter(j => S.tailorChecked.has(j.id));
  const needsWork = j => !j.has_resume || j.status === 'needs_retailor';
  const pending = selected.filter(j => !activeTask(j.id) && (S.tailorRegenerate || needsWork(j)));
  const eligible = pending.length, tailorable = pending.filter(j => j.fit != null).length;
  const reviewable = selected.filter(reviewablePacket).length, unreviewed = queue.filter(unreviewedPacket);
  const untailored = queue.filter(needsWork);
  const allOn = queue.every(j => S.tailorChecked.has(j.id));
  const allToggle = `<input type="checkbox" data-act="tailor-select" data-which="${allOn ? 'none' : 'all'}" ${allOn ? 'checked' : ''} aria-label="Select all jobs">`;
  const head = `<div class="lrow caps" style="gap:12px;padding-top:9px;padding-bottom:9px;background:#fafbfe;font-size:10.5px;letter-spacing:.06em">
      ${allToggle}<span class="none" style="width:26px" title="AI fit score">FIT</span><span class="grow">JOB</span>
      <span class="none" style="width:54px;text-align:right" title="Independent reviewer score of the latest packet">REVIEWER</span>
      <span class="none" style="width:48px;text-align:right" title="Share of the posting's ATS keywords the packet covers">ATS</span>
      <span class="none" style="width:210px;text-align:right">STATUS</span></div>`;
  const rows = queue.map(j => {
    const task = activeTask(j.id);
    const retailor = j.status === 'needs_retailor';
    const status = task ? taskPill(task)
      : retailor ? `<span title="${attr((j.review_reasons || []).join(' · ') || 'Review sent this packet back to be tailored again')}">${pill('re-tailor', 'warn')}</span>`
      : unreviewedPacket(j) ? `<span title="The packet was saved but its review never ran or failed: send it to review">${pill('not reviewed', 'warn')}</span>`
      : j.has_resume ? pill(j.status === 'pending_approval' ? 'sent back' : 'has packet', 'muted')
      : j.fit == null ? `<span title="Tailoring works from the ranking: rank this job first, or skip tailoring">${pill('rank first', 'warn')}</span>`
      : pill('not tailored', 'warn');
    return `<div class="lrow click ${task?.status === 'running' ? 'sel' : ''}" data-act="tailor-check" data-id="${j.id}" style="gap:12px">
      <input type="checkbox" data-act="tailor-check" data-id="${j.id}" ${S.tailorChecked.has(j.id) ? 'checked' : ''} aria-label="Select ${attr(j.title)}">
      <span class="mono bb ${fitCls(j.fit)}" style="width:26px" title="Fit score">${fitTxt(j.fit)}</span>
      <div class="grow" style="min-width:0"><div class="small b ellipsis">${esc(j.title)}</div><div class="xs muted ellipsis">${esc(j.company)} · ${esc(j.board)}${j.location ? ' · ' + esc(j.location) : ''}</div></div>
      <span class="mono xs none" style="width:54px;text-align:right" title="Reviewer score">${j.reviewer_score == null ? '<span class="faint">—</span>' : fitTxt(j.reviewer_score)}${j.untailored ? '<span class="faint"> ·m</span>' : ''}</span>
      <span class="mono xs none" style="width:48px;text-align:right" title="ATS keyword coverage">${j.ats_pct == null ? '<span class="faint">—</span>' : j.ats_pct + '%'}</span>
      <span class="row gap8 none" style="width:210px;justify-content:flex-end">${btn('Description', 'open-job', { id: j.id }, 'sm')}${status}</span></div>`;
  }).join('');
  const queueing = isBusy('bulkTailor');
  return `<div class="stack">${renderTaskLink()}${renderPacketFilters('tailor', D.tailorFacets)}<section class="card flush">
    <div class="row wrap gap10" style="padding:14px 18px;border-bottom:1px solid var(--line2)">
      <div class="grow" style="min-width:220px"><div class="card-title" style="font-size:16px">Bulk tailor</div>
        <div class="card-sub">${selected.length} of ${queue.length} selected · ${untailored.length} not tailored yet</div></div>
      ${btn(`Select not tailored (${untailored.length})`, 'tailor-select', { which: 'untailored' }, 'sm', !untailored.length)}
      ${btn(`Select not reviewed (${unreviewed.length})`, 'tailor-select', { which: 'unreviewed' }, 'sm', !unreviewed.length)}
      ${btn(allOn ? 'Clear' : 'Select all', 'tailor-select', { which: allOn ? 'none' : 'all' }, 'sm')}
      <span title="${attr(S.tailorRegenerate ? 'Review the saved packets of the selected jobs again, without tailoring them again' : 'Review the saved packets of the selected jobs whose review never ran or failed, without tailoring them again. Tick “Redo” to review reviewed packets again too.')}">${btn(`Review${reviewable ? ` ${reviewable}` : ''}`, 'bulk-tailor', { mode: 'review' }, 'sm', !reviewable || queueing)}</span>
      ${btn(`Skip tailoring${eligible ? ` ${eligible}` : ''}`, 'bulk-tailor', { mode: 'skip' }, 'sm', !eligible || queueing)}
      ${tailorPagesPicker()}
      <span title="${attr(eligible > tailorable ? `${eligible - tailorable} selected job${eligible - tailorable === 1 ? ' is' : 's are'} not ranked yet: rank first, or skip tailoring` : 'Tailor the selected jobs')}">${btn(queueing ? 'Queueing…' : `Tailor${tailorable ? ` ${tailorable}` : ''}`, 'bulk-tailor', { mode: 'tailor' }, 'sm primary', !tailorable || queueing)}</span>
    </div>
    <div class="row wrap gap10 xs muted" style="padding:10px 18px;border-bottom:1px solid var(--line2)">
      <label class="row gap6"><input type="checkbox" data-act="tailor-regenerate" ${S.tailorRegenerate ? 'checked' : ''}> Redo jobs that already have a packet</label>
      <span class="grow"></span><span>Each job is tailored (guided by its ranking when it has one), then independently reviewed on the server. You can leave this page.</span>
    </div>
    ${head}${rows}</section></div>`;
}

function renderTailor() {
  const queue = D.tailorQueue; if (!queue) return loading();
  const outside = S.tailorMode === 'single' && D.packet && !queue.some(j => j.id === D.packet.job.id);
  if (outside) return renderTailorSingle(queue);
  if (!queue.length && D.tailorFacets.total_all) return `<div class="stack">${renderTaskLink()}${renderPacketFilters('tailor', D.tailorFacets)}${pfNoMatch('tailor')}</div>`;
  if (!queue.length) return `<section class="card">${empty('Nothing to tailor yet', 'Shortlist a job in Discovery and it will appear here.', btn('Go to triage', 'go', { screen: 'discovery', mode: 'triage' }, 'primary'))}</section>`;
  if (S.tailorMode === 'bulk') return renderBulkTailor(queue);
  return renderTailorSingle(queue);
}
function renderTailorSingle(queue) {
  const p = D.packet; if (!p) return loading();
  const job = p.job, r = p.resume;
  const locked = PACKET_LOCKED.includes(job.status);
  // The open job keeps its place in the picker even when the queue filter hides it.
  const extra = queue.some(j => j.id === job.id) ? ''
    : `<option value="${job.id}" selected>${esc(job.company)} — ${esc(job.title)} (${esc(statusText(job.status))})</option>`;
  const picker = `<select class="select" style="margin:0;width:auto;max-width:320px" id="tailor-pick" data-act-change="pick-tailor">${extra}${queue.map(j => `<option value="${j.id}" ${j.id === S.tailorJob ? 'selected' : ''}>${esc(j.company)} — ${esc(j.title)}${j.status === 'needs_retailor' ? ' (re-tailor)' : j.has_resume ? '' : ' (not tailored)'}</option>`).join('')}</select>`;
  const task = activeTask(job.id), working = !!task || isBusy('queue' + job.id);
  const generating = working && !['skip', 'review'].includes(task?.mode), skipping = task?.mode === 'skip';
  const reviewing = task?.mode === 'review';
  const stack = renderTaskLink() + renderPacketFilters('tailor', D.tailorFacets) + (locked ? `<section class="banner ${job.status === 'applied' ? '' : 'warn'} row wrap gap10" style="margin:0">
      <span class="grow" style="min-width:220px">${job.status === 'applied'
        ? '<b>Already applied.</b> Edits here no longer change what you submitted.'
        : '<b>This packet is approved.</b> Editing the resume or cover letter below withdraws the approval; approve it again afterwards. To regenerate it or skip tailoring, send it back for revision first.'}</span>
      ${job.status === 'approved' ? btn(label('revise', 'Send back for revision', 'Sending back…'), 'revise', { id: job.id }, 'sm', isBusy('revise')) + btn('Open apply kit', 'open-kit', { id: job.id }, 'sm good') : ''}</section>` : '');
  if (!r) {
    return `<div class="stack">${stack}<section class="card row gap16 wrap" id="tailor-packet"><div class="grow" style="min-width:220px"><div class="card-title" style="font-size:16px">Tailoring for ${esc(job.title)} · ${esc(job.company)}</div><div class="card-sub">Fit ${fitTxt(job.fit)} · no packet generated yet</div></div>${picker}</section>
      <section class="card">${empty(generating ? 'Drafting your packet…' : skipping ? 'Reviewing your master resume…' : 'No packet yet', generating ? `${task ? `${STAGE_LABEL[task.stage]} · ${task.progress}%. ` : ''}Tailoring and an independent review run on the server. You can leave this page.` : skipping ? `${STAGE_LABEL[task.stage]}. Your master resume is used unchanged; the reviewer is scoring it against this posting.` : 'Generate a tailored resume, cover letter and reviewer score. Tailoring may only cite your master resume and verified evidence. If this posting needs no tailoring, skip it and send your master resume as-is.', tailorPagesPicker() + btn(generating ? 'Generating…' : 'Generate tailored packet', 'generate', { id: job.id }, 'primary lg', working) + btn(skipping ? 'Skipping…' : 'Skip tailoring', 'skip-tailor', { id: job.id }, 'lg', working) + btn('Read the posting', 'open-job', { id: job.id }))}</section></div>`;
  }
  const accepted = r.edits.filter(e => e.status === 'accepted').length;
  const unreviewed = r.reviewer_score == null;
  const reviewButton = locked || !unreviewed ? '' : btn(reviewing ? 'Reviewing…' : 'Send to review', 'send-review', { id: job.id }, 'primary', working);
  const threshold = unreviewed ? 'Approval needs a reviewed packet: send this one to review first.'
    : p.application_mode === 'automatic'
    ? `Auto-approval threshold is ${p.min_review_score}. ${r.reviewer_score >= p.min_review_score ? 'This packet cleared it.' : 'This packet needs your sign-off.'}`
    : `Manual approval mode · minimum review score ${p.min_review_score}.`;
  let body = '';
  if (S.tailorTab === 'diff' && r.untailored) {
    body = `<div style="padding:18px">${empty('Tailoring skipped', 'This packet uses your master resume unchanged, so there are no edits to review. Use “Tailor instead” to generate a tailored version.')}</div>`;
  } else if (S.tailorTab === 'diff') {
    body = r.edits.map(e => {
      const tone = { accepted: 'good', review: 'warn', reverted: 'muted' }[e.status];
      const editing = S.editing === e.index;
      return `<div class="diff ${e.status}"><div class="row between mb10"><span class="caps">${esc(e.section)}</span>${pill(e.status === 'review' ? 'review this' : e.status, tone)}</div>
        <div class="diff-before pretty">${esc(e.before)}</div>
        ${editing ? `<textarea class="textarea" id="edit-text-${e.index}">${esc(e.after)}</textarea>` : `<div class="diff-after pretty">${esc(e.after)}</div>`}
        <div class="row wrap gap10 mt10">${e.evidence ? pill('evidence · ' + e.evidence, 'good', 'lg') : pill('no verified evidence matched', 'warn', 'lg')}<div class="grow"></div>
          ${editing ? btn('Cancel', 'edit-cancel', {}, 'sm') + btn('Save edit', 'edit-save', { index: e.index, resume: r.id }, 'sm primary')
            : `${btn('Edit', 'edit-start', { index: e.index }, 'sm')}${e.status !== 'accepted' ? btn(e.status === 'reverted' ? 'Restore' : 'Accept', 'edit-status', { index: e.index, resume: r.id, status: 'accepted' }, 'sm soft') : ''}${e.status !== 'reverted' ? btn('Revert', 'edit-status', { index: e.index, resume: r.id, status: 'reverted' }, 'sm') : ''}`}</div></div>`;
    }).join('');
  } else if (S.tailorTab === 'resume') {
    body = `<div style="padding:18px"><div class="doc">${miniMarkdown(r.markdown)}</div></div>`;
  } else if (S.tailorTab === 'cover') {
    body = `<div style="padding:18px">${textareaF('cover' + r.id, 'text', r.cover_letter, 'style="min-height:260px;font-size:13px"')}<div class="row gap8 mt10"><span class="small muted grow">${r.cover_words} words</span>${btn(label('cover', 'Save cover letter', 'Saving…'), 'save-cover', { resume: r.id }, 'primary sm', isBusy('cover'))}</div></div>`;
  } else {
    const ans = D.knowledge || [];
    body = `<div style="padding:18px" class="col gap9">${ans.length ? ans.slice(0, 12).map(a => `<div class="box"><div class="small muted">${esc(a.question)}</div><div class="row gap8 mt4"><span class="small b grow">${esc(a.answer || 'Awaiting answer')}</span>${pill(a.sensitive ? 'confirm each time' : a.verified ? 'verified' : 'needs review', a.sensitive || !a.verified ? 'warn' : 'good')}</div></div>`).join('') : '<div class="small muted">No saved ATS answers yet. Add them in the Knowledge bank.</div>'}</div>`;
  }
  return `<div class="stack">${stack}
  <section class="card row gap16 wrap" id="tailor-packet" style="padding:16px 20px">
    <div class="grow" style="min-width:220px"><div class="card-title" style="font-size:16px">Tailoring for ${esc(job.title)} · ${esc(job.company)}</div><div class="card-sub">v${r.version} · generated ${esc(r.generated)}${packetLength(r)} · ${r.untailored ? 'master resume, not tailored' : 'edits must trace to verified evidence'}</div>${packetSourceNote(r.source, r.resume_in_use)}</div>
    ${picker}
    <div class="row gap14"><div class="metric"><div class="n" style="color:${r.reviewer_score >= p.min_review_score ? 'var(--good)' : 'var(--warn)'}">${fitTxt(r.reviewer_score)}</div><div class="l">REVIEWER</div></div>
      <div class="metric"><div class="n">${r.ats_pct == null ? '—' : r.ats_pct + '%'}</div><div class="l">ATS KEYWORDS</div></div>
      ${locked ? '' : tailorPagesPicker()}${btn(generating ? (r.untailored ? 'Tailoring…' : 'Regenerating…') : (r.untailored ? 'Tailor instead' : 'Regenerate'), 'generate', { id: job.id }, '', working || locked)}
      ${r.untailored ? '' : btn(skipping ? 'Skipping…' : 'Skip tailoring', 'skip-tailor', { id: job.id }, '', working || locked)}
      ${btn('Job description', 'open-job', { id: job.id })}
      ${locked ? '' : unreviewed ? reviewButton : btn('Send to approval', 'go', { screen: 'approvals', approval: job.id }, 'primary')}</div>
  </section>
  <div class="split gap16">
    <section class="card flush c-main"><div class="row between wrap gap10" style="padding:14px 18px;border-bottom:1px solid var(--line2)">
      <div class="tabs">${[['diff', 'Diff'], ['resume', 'Resume'], ['cover', 'Cover letter'], ['answers', 'ATS answers']].map(([id, t]) => `<button class="${S.tailorTab === id ? 'on' : ''}" data-act="tailor-tab" data-id="${id}">${t}</button>`).join('')}</div>
      <span class="small muted">${r.edits.length} edits · ${accepted} accepted</span></div>${body}</section>
    <div class="c-side col gap14">
      <section class="card pad18"><div class="row gap8 mb12">${pill('AI', 'brand')}<span class="card-title sm">Reviewer notes</span><span class="mono bb base" style="margin-left:auto;color:var(--good)">${fitTxt(r.reviewer_score)} / 100</span></div>
        <div class="col gap10">${unreviewed ? `<div class="small muted">${reviewing ? 'The reviewer is checking this packet on the server. You can leave this page.' : 'This packet has not been reviewed: its review never ran or failed. Send it to review to score it without tailoring it again.'}</div>${reviewButton ? `<div>${reviewButton}</div>` : ''}` : r.review.length ? r.review.map(n => `<div class="row top-align gap10"><span class="note-dot" style="background:${{ good: '#0f9c86', warn: '#e0a136', bad: '#c22f4e' }[n.tone]}"></span><div class="small ink2 pretty" style="line-height:1.55">${esc(n.text)}</div></div>`).join('') : '<div class="small muted">The reviewer left no notes.</div>'}</div>
        <div class="xs faint mt12" style="padding-top:12px;border-top:1px solid var(--line2)">${esc(threshold)}</div></section>
      <section class="card pad18"><div class="card-title sm mb12">Keyword coverage</div><div class="row wrap gap6">${r.keywords.length ? r.keywords.map(k => `<span class="kw ${k.status}" title="${k.status}">${esc(k.word)}</span>`).join('') : '<span class="small muted">No keywords yet: they come from the parsed job description (Job description → Structured).</span>'}</div>${r.missing_supported?.length ? `<div class="small muted mt8">Your verified facts support these, but the resume doesn't use them yet: ${esc(r.missing_supported.join(', '))}</div>` : ''}</section>
      <section class="card pad18"><div class="card-title sm mb10">Exports</div><div class="col gap8">
        <a class="btn between" href="/api/jobs/${job.id}/resume.pdf">Resume PDF <span class="faint">plain text</span></a>
        <a class="btn between" href="/api/jobs/${job.id}/resume.docx">Resume DOCX <span class="faint">ATS-safe</span></a>
        <a class="btn between" href="/api/jobs/${job.id}/cover-letter.txt">Cover letter <span class="faint">${r.cover_words} words</span></a></div></section>
    </div></div></div>`;
}

/* ───────────── approvals ───────────── */
function renderApprovals() {
  const list = D.approvals; if (!list) return loading();
  if (!D.approvalFacets.total_all) return renderApprovalsBody(list);
  return `<div class="stack">${renderPacketFilters('approvals', D.approvalFacets)}${list.length ? renderApprovalsBody(list) : pfNoMatch('approvals')}</div>`;
}
function renderApprovalsBody(list) {
  if (!list.length) return `<section class="card">${empty('The queue is clear', 'Tailored packets land here for your sign-off.', btn('Go to Tailor', 'go', { screen: 'tailor' }, 'primary'))}</section>`;
  const a = D.approval;
  const waiting = list.filter(x => x.waiting).length;
  const allOn = list.every(q => S.approvalPicked.has(q.id));
  const queue = `<section class="card flush c-narrow"><div class="card-head row gap10"><input type="checkbox" data-act="approval-check-all" ${allOn ? 'checked' : ''} aria-label="Select every job in the queue"><span class="card-title sm grow">Queue · ${waiting} waiting</span></div>
    ${renderApprovalBulkBar(list)}
    ${list.map(q => `<div class="lrow click ${q.id === S.approvalJob ? 'sel' : ''}" data-act="select-approval" data-id="${q.id}"><input type="checkbox" data-act="approval-check" data-id="${q.id}" ${S.approvalPicked.has(q.id) ? 'checked' : ''} aria-label="Select ${attr(q.title)}">${starButton(q)}<div class="grow"><div class="base b ellipsis">${esc(q.title)}</div><div class="xs muted">${esc(q.company)} · ${esc(q.age)}</div></div>${q.easy_apply ? pill('Easy Apply', 'brand') : ''}${q.approval_override ? pill('waived', 'warn') : ''}${approvalTaskPill(q)}${postingClosedPill(q)}<span class="mono bb base" style="color:${q.status === 'approved' ? 'var(--faint)' : (q.reviewer_score || 0) >= 75 ? 'var(--good)' : (q.reviewer_score || 0) >= 65 ? 'var(--warn)' : 'var(--bad)'}">${fitTxt(q.reviewer_score)}</span></div>`).join('')}</section>`;
  if (!a) return `<div class="split">${queue}<section class="card c-main">${loading()}</section></div>`;
  const r = a.resume, job = a.job;
  const approved = job.status === 'approved';
  // A packet the reviewer scored below your minimum can still be approved — deliberately, and on the record.
  const below = !!r && r.reviewer_score != null && r.reviewer_score < a.min_review_score;
  const approveAct = below ? 'approve-override-open' : 'approve';
  return `<div class="split">${queue}
  <section class="card flush c-main">
    <div class="row between wrap gap12" style="padding:18px 22px;border-bottom:1px solid var(--line2)"><div><div class="row gap8" style="font-size:17px;font-weight:600">${starButton(job)}<span>${esc(job.title)} · ${esc(job.company)}</span></div><div class="small muted mt4">Fit ${fitTxt(job.fit)} · reviewer ${fitTxt(r?.reviewer_score)} · tailored ${esc(r?.generated || '')} · ${statusPill(job.status)} ${postingClosedPill(job)}${list.find(x => x.id === job.id)?.easy_apply ? ` ${pill('Easy Apply', 'brand')}` : ''}</div></div>
      <div class="row gap8 wrap">${btn('Job details', 'open-job', { id: job.id })}${job.url ? `<a class="btn" href="${attr(job.url)}" target="_blank" rel="noopener">Open posting ↗</a>` : ''}${btn(label('reject', 'Reject', 'Rejecting…'), 'reject-open', { id: job.id }, 'ghost danger', isBusy('reject'))}${approved ? btn('Open apply kit', 'open-kit', { id: job.id }, 'good') : `${btn(label('revise', 'Needs revision', 'Sending back…'), 'revise', { id: job.id }, '', isBusy('revise'))}${btn(label('approve', below ? 'Approve anyway…' : 'Approve → apply kit', 'Approving…'), approveAct, { id: job.id }, 'good', isBusy('approve') || !r)}`}</div></div>
    ${pipelineBanner({ ...job, pipeline: a.pipeline }, '12px 22px 0')}
    ${a.approval?.override ? `<div class="banner warn" style="margin:0 22px;display:block"><b>Approved below your reviewer minimum.</b>${a.approval.note ? ` ${esc(a.approval.note)}` : ''}</div>` : ''}
    ${S.rejecting === job.id ? renderRejectPicker(job, a) : ''}
    ${S.overriding === job.id ? renderApprovalOverride(job, a, r) : ''}
    ${renderInsightsCard(job.id)}
    ${r ? `<div style="padding:18px 22px;border-bottom:1px solid var(--line2)"><div class="base b mb12">Pre-submit checklist</div>
      <div class="grid-auto" style="grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px">${a.checklist.map(c => `<div class="check-item ${c.state}"><span class="mark">${c.state === 'ok' ? '✓' : '!'}</span><div><div class="small b lbl">${esc(c.label)}</div><div class="xs muted mt4">${esc(c.detail)}</div></div></div>`).join('')}</div></div>
    <div class="grid-auto" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:0">
      <div style="padding:18px 22px;border-right:1px solid var(--line2)"><div class="row between mb10"><div class="base b">Tailored resume</div><a class="small" href="#" data-act="go" data-screen="tailor" data-job="${job.id}">Edit in Tailor</a></div><div class="doc clip">${miniMarkdown(r.markdown)}</div></div>
      <div style="padding:18px 22px"><div class="base b mb10">ATS answers to reuse</div>
        <div class="col gap9">${a.answers.length ? a.answers.map(x => `<div class="box"><div class="small muted">${esc(x.question)}</div><div class="row gap8 mt4"><span class="small b grow">${esc(x.answer || 'Awaiting answer')}</span>${pill(x.sensitive ? 'confirm' : x.verified ? 'verified' : 'unverified', x.sensitive || !x.verified ? 'warn' : 'good')}</div></div>`).join('') : '<div class="small muted">No saved answers. Add them in the Knowledge bank.</div>'}</div>
        <textarea class="textarea mt12" id="approval-notes" data-act-blur="save-notes" data-id="${job.id}" placeholder="Notes for yourself — saved when you click away">${esc(fv('approvalNotes', job.id, a.notes))}</textarea></div></div>` : empty('No packet', 'This job has no tailored resume.')}
  </section></div>`;
}
// Packets that can be made again (Retailor) or reviewed again (Re-review): not decided yet, and not already being
// worked on. The server checks the same and skips the rest with a reason (app.services.tailor_tasks.enqueue).
const approvalRedoable = picked => picked.filter(q => ['tailored', 'pending_approval'].includes(q.status) && !activeTask(q.id));
// Acting on several queued jobs at once. Each button counts only the picked jobs it applies to; the server
// decides each job on its own and reports the ones it skipped. Bulk approval never waives the reviewer minimum.
function approvalBulkCounts(picked) {
  const min = D.approval?.min_review_score;
  return {
    reject: picked.length,
    revise: picked.filter(q => q.status !== 'pending_approval').length,
    approve: picked.filter(q => q.status !== 'approved' && q.reviewer_score != null && (min == null || q.reviewer_score >= min)).length,
    retailor: approvalRedoable(picked).filter(q => q.fit != null).length,
    review: approvalRedoable(picked).length,
  };
}
// A retailoring or re-review running in the background for this packet.
function approvalTaskPill(q) {
  const task = activeTask(q.id);
  return task ? pill(task.mode === 'review' ? 're-reviewing' : 'retailoring', 'brand') : '';
}
// The daily posting check found this posting closed; the job stays because you approved it.
const postingClosedPill = job => job.posting_closed ? `<span title="${attr(`The posting was found closed ${job.posting_closed.ago || ''}. Kept because you approved it.`)}">${pill('posting closed', 'bad')}</span>` : '';
// A star marks a job to come back to. It is a bookmark only: it changes no workflow state.
const starButton = job => `<button type="button" class="star-btn ${job.starred ? 'on' : ''}" data-act="approval-star" data-id="${job.id}" aria-pressed="${job.starred ? 'true' : 'false'}" aria-label="${job.starred ? 'Unstar' : 'Star'} ${attr(job.title)}" title="${job.starred ? 'Starred — click to unstar' : 'Star this job'}">${job.starred ? '★' : '☆'}</button>`;
// Only web links are opened in a new tab; a posting URL comes from a job board and is not trusted.
function postingUrl(url) { return /^https?:\/\//i.test(url || '') ? url : ''; }
// One tab per picked posting. window.open runs inside the click itself, before any await, or the browser
// treats every tab after the first as an unrequested pop-up.
function openPostings(urls) {
  let opened = 0;
  for (const url of urls) {
    const tab = window.open(url, '_blank');
    if (!tab) break;
    tab.opener = null; opened += 1;
  }
  return opened;
}
function queueApprovalRedo(mode, busyKey, extra) {
  const picked = (D.approvals || []).filter(q => S.approvalPicked.has(q.id));
  const ids = approvalRedoable(picked).filter(q => mode !== 'tailor' || q.fit != null).map(q => q.id);
  return queueTailoring(ids, mode, true, busyKey, async out => {
    out.queued.forEach(t => S.approvalPicked.delete(t.job_id));
    await load();
  }, extra);
}
const BULK_APPROVAL_DONE = { reject: 'Rejected', revise: 'Sent back for revision', approve: 'Approved' };
function bulkApprovalMsg(out) {
  const n = out.done.length, skipped = out.skipped.length;
  const first = skipped ? ` · ${skipped} skipped (${out.skipped[0].title ? `${out.skipped[0].title}: ` : ''}${out.skipped[0].reason}${skipped > 1 ? ', …' : ''})` : '';
  return `${BULK_APPROVAL_DONE[out.action]} ${n} job${n === 1 ? '' : 's'}${first}`;
}
function renderApprovalBulkBar(list) {
  const picked = list.filter(q => S.approvalPicked.has(q.id));
  if (!picked.length) return '';
  const n = approvalBulkCounts(picked), working = isBusy('bulkApproval');
  const links = picked.filter(q => postingUrl(q.url)).length;
  const head = `<div class="row wrap gap8" style="padding:10px 18px;border-bottom:1px solid var(--line2);background:var(--brand-softer)">
    <span class="small b grow">${picked.length} selected</span>
    <span title="${attr(links < picked.length ? `${picked.length - links} selected job${picked.length - links === 1 ? ' has' : 's have'} no posting link` : 'Open each selected posting in a new tab')}">${btn(`Open postings${links ? ` ${links}` : ''} ↗`, 'bulk-open-postings', {}, 'sm', !links)}</span>
    ${btn('Reject…', 'bulk-reject-open', {}, 'sm ghost danger', working || S.bulkRejecting)}
    ${btn(label('bulkApproval', `Needs revision${n.revise ? ` ${n.revise}` : ''}`, 'Working…'), 'bulk-approval', { action: 'revise' }, 'sm', working || !n.revise)}
    <span title="Tailor again from each packet's last review, then review the new version. In the background: you can leave this page.">${btn(label('bulkRetailor', `Retailor${n.retailor ? ` ${n.retailor}` : ''}`, 'Queueing…'), 'bulk-retailor', {}, 'sm', working || isBusy('bulkRetailor') || !n.retailor)}</span>
    <span title="Run the Reviewer again on each packet's current version. Nothing is rewritten.">${btn(label('bulkRereview', `Re-review${n.review ? ` ${n.review}` : ''}`, 'Queueing…'), 'bulk-rereview', {}, 'sm', working || isBusy('bulkRereview') || !n.review)}</span>
    <span title="Approves packets that cleared your reviewer minimum. To waive the minimum, approve that packet on its own.">${btn(label('bulkApproval', `Approve${n.approve ? ` ${n.approve}` : ''}`, 'Working…'), 'bulk-approval', { action: 'approve' }, 'sm good', working || !n.approve)}</span>
    <a href="#" class="small" data-act="approval-check-none">Clear</a></div>`;
  if (!S.bulkRejecting) return head;
  const reasons = D.approval?.rejection_reasons || [];
  const chosen = fv('bulkReject', 'reason', '');
  return head + `<div style="padding:12px 18px;border-bottom:1px solid var(--line2);background:var(--warn-soft)">
    <div class="row between gap10 mb8"><div class="small b">Reject ${picked.length} job${picked.length === 1 ? '' : 's'}?</div><a href="#" class="small" data-act="bulk-reject-close">Cancel</a></div>
    <div class="xs muted mb8">They leave the approval queue and your pipeline. Tailored resumes are kept, and nothing is sent to anyone.</div>
    <div class="row gap6 wrap mb8">${reasons.map(x => `<button type="button" class="btn sm ${chosen === x.id ? 'soft' : ''}" data-act="bulk-reject-reason" data-id="${attr(x.id)}">${chosen === x.id ? '✓ ' : ''}${esc(x.label)}</button>`).join('')}</div>
    ${inputF('bulkReject', 'notes', '', 'placeholder="Anything worth remembering (optional)" maxlength="2000"')}
    <div class="row gap8 wrap mt10">${btn(label('bulkApproval', `Reject ${picked.length}${chosen ? ' with this reason' : ''}`, 'Rejecting…'), 'bulk-approval', { action: 'reject' }, 'danger-solid sm', working)}</div></div>`;
}
// Approving a packet the reviewer scored below your minimum. The minimum can be waived for one packet; the
// review itself cannot be skipped, so a packet nobody scored is not offered this.
function renderApprovalOverride(job, a, r) {
  const flagged = (a.checklist || []).filter(c => c.state !== 'ok');
  return `<div style="padding:16px 22px;border-bottom:1px solid var(--line2);background:var(--warn-soft)">
    <div class="row between wrap gap10 mb10"><div><div class="base b">Approve below your reviewer minimum?</div>
      <div class="xs muted mt4">The reviewer scored this packet <b>${fitTxt(r.reviewer_score)}</b> against your minimum of <b>${a.min_review_score}</b>. Approving records that you waived it for this packet; your minimum does not change.</div></div>
      <a href="#" class="small" data-act="approve-override-close">Cancel</a></div>
    ${flagged.length ? `<div class="box mb10"><div class="caps-sm mb8">STILL FLAGGED</div>
      <div class="col gap4">${flagged.map(c => `<div class="xs"><b>${esc(c.label)}</b> · ${esc(c.detail)}</div>`).join('')}</div></div>` : ''}
    ${inputF('override', 'note', '', 'placeholder="Why this one is fine anyway (optional)" maxlength="2000"')}
    <div class="row gap8 wrap mt10">${btn(label('approve', 'Approve anyway → apply kit', 'Approving…'), 'approve', { id: job.id, override: '1' }, 'good', isBusy('approve'))}
      <a class="btn sm ghost" href="#workers/settings">Change my minimum</a></div></div>`;
}

// Turning a job down. The reason is optional on purpose: one click is enough when you just want it gone,
// and a reason is kept when you give one (it is shown wherever the job appears afterwards).
function renderRejectPicker(job, a) {
  const reasons = a.rejection_reasons || [];
  const chosen = fv('reject', 'reason', '');
  return `<div style="padding:16px 22px;border-bottom:1px solid var(--line2);background:var(--warn-soft)">
    <div class="row between wrap gap10 mb10"><div><div class="base b">Reject this job?</div>
      <div class="xs muted mt4">It leaves the approval queue and your pipeline. The tailored resume is kept, and nothing is sent to anyone.</div></div>
      <a href="#" class="small" data-act="reject-close">Cancel</a></div>
    <div class="row gap6 wrap mb10">${reasons.map(x => `<button type="button" class="btn sm ${chosen === x.id ? 'soft' : ''}" data-act="reject-reason" data-id="${attr(x.id)}">${chosen === x.id ? '✓ ' : ''}${esc(x.label)}</button>`).join('')}</div>
    ${inputF('reject', 'notes', '', 'placeholder="Anything worth remembering (optional)" maxlength="2000"')}
    <div class="row gap8 wrap mt10">${btn(label('reject', chosen ? 'Reject with this reason' : 'Reject without a reason', 'Rejecting…'), 'reject-confirm', { id: job.id }, 'danger-solid', isBusy('reject'))}
      ${chosen ? btn('Clear reason', 'reject-reason', { id: '' }, 'sm ghost') : ''}</div></div>`;
}
function renderBoardReview() {
  const r = S.boardReview, open = r.jobs.filter(j => !j.in_pipeline), n = r.selected.size;
  return `<div class="overlay center" data-act="overlay-close"><div class="modal white" role="dialog" aria-label="Review board roles" style="max-width:640px">
    <div class="row top-align between gap16" style="padding:20px 24px;border-bottom:1px solid var(--line2)"><div>
      <div style="font-size:19px;font-weight:600;letter-spacing:-.02em">Review roles · ${esc(r.token)}</div>
      <div class="small muted mt4">${r.jobs.length} matching of ${r.total} open roles${open.length < r.jobs.length ? ` · ${r.jobs.length - open.length} already in your pipeline` : ''}. Untick any you don't want.</div></div>
      ${btn('Close', 'board-review-close', {}, 'none')}</div>
    <div class="row gap8" style="padding:10px 24px">${btn('Select all', 'board-review-all', {}, 'ghost sm')}${btn('Select none', 'board-review-none', {}, 'ghost sm')}</div>
    <div style="max-height:52vh;overflow:auto;border-top:1px solid var(--line2)">${r.jobs.length ? r.jobs.map(j => `<label class="row gap10" style="padding:10px 24px;border-bottom:1px solid #f8f9fd;${j.in_pipeline ? 'opacity:.5' : 'cursor:pointer'}">
      <input type="checkbox" data-act="toggle-board-job" data-ref="${attr(j.source_ref)}" ${r.selected.has(j.source_ref) ? 'checked' : ''} ${j.in_pipeline ? 'disabled' : ''}>
      <div class="grow"><div class="small b">${esc(j.title)}</div><div class="tiny muted">${esc([j.team, j.location].filter(Boolean).join(' · ')) || '—'}${j.in_pipeline ? ' · already added' : ''}</div></div>
      ${j.url ? `<a class="tiny" href="${attr(j.url)}" target="_blank" rel="noopener">Posting ↗</a>` : ''}</label>`).join('') : '<div class="small muted" style="padding:20px 24px">No roles match these filters.</div>'}</div>
    <div class="row gap8 end" style="padding:14px 24px;border-top:1px solid var(--line2)">${btn('Cancel', 'board-review-close', {}, 'ghost')}${btn(label('board', `Add ${n} selected`, 'Importing…'), 'board-review-import', {}, '', isBusy('board') || !n)}</div>
  </div></div>`;
}
function renderKit() {
  const k = D.kit; if (!k) return `<div class="overlay center" data-act="overlay-close"><div class="modal white" style="padding:40px">${loading()}</div></div>`;
  const job = k.job;
  return `<div class="overlay center" data-act="overlay-close"><div class="modal white" role="dialog" aria-label="Apply kit">
    <div class="row top-align between gap16" style="padding:22px 24px;border-bottom:1px solid var(--line2)"><div>
      <div class="row gap9">${pill(k.status === 'applied' ? 'APPLIED' : 'APPROVED', 'good')}<span class="small muted">Fit ${fitTxt(job.fit)} · reviewer ${fitTxt(k.reviewer_score)}</span></div>
      <div style="font-size:21px;font-weight:600;letter-spacing:-.02em;margin-top:8px">Apply kit · ${esc(job.title)} at ${esc(job.company)}</div>
      <div class="base muted mt4 pretty">We never submit for you. Everything the form will ask for is on this screen — open the posting, paste, come back and confirm.</div></div>
      ${btn('Close', 'close-kit', {}, 'none')}</div>
    <div class="grid-auto" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:0">
      <div style="padding:20px 24px;border-right:1px solid var(--line2)">
        <div class="caps mb10">1 · TAKE THESE</div><div class="col gap9">${k.files.map(f => `<div class="box row gap12"><div class="avatar" style="width:32px;height:32px;border-radius:8px;font-size:10px;background:var(--soft);color:var(--muted)">${f.kind}</div><div class="grow"><div class="small b ellipsis">${esc(f.name)}</div><div class="xs muted">${esc(f.meta)}</div></div><a class="btn sm soft" href="${attr(f.href)}" download="${attr(f.name)}">Download</a></div>`).join('')}</div>
        <div class="caps" style="margin:20px 0 11px">2 · PASTE THESE</div><div class="col gap8">${k.answers.length ? k.answers.map(x => `<div class="box row gap10"><div class="grow"><div class="xs muted">${esc(x.question)}</div><div class="small b mt4">${esc(x.answer)}</div></div>${btn('Copy', 'copy-answer', { id: x.id, text: x.answer }, 'ghost sm none')}</div>`).join('') : '<div class="small muted">No verified answers yet — anything you type into their form can be saved to the Knowledge bank afterwards.</div>'}</div>
      </div>
      <div style="padding:20px 24px;background:#fbfbfe">
        <div class="caps mb10">3 · SUBMIT ON THEIR SITE</div>
        ${k.status === 'applied' ? '' : renderKitClaim(k)}
        ${job.url ? `<a class="btn primary block lg" style="border-radius:11px;padding:14px" href="${attr(job.url)}" target="_blank" rel="noopener">Open ${esc(job.company)} application ↗</a>` : `<div class="banner warn">No posting URL was captured for this job.</div>`}
        <div class="small muted mt10 pretty" style="line-height:1.55">Opens in a new tab. Job Hunt OS never bypasses logins or CAPTCHAs.</div>
        <div class="hr"></div>
        <div class="caps mb10">4 · CONFIRM BACK HERE</div>
        ${k.status === 'applied' ? `<div class="box good"><div class="b" style="color:var(--good)">Already marked applied</div><div class="small ink2 mt4">Track replies and follow-ups in the Tracker.</div>${btn('Open tracker', 'go', { screen: 'tracker', app: job.application_id }, 'block mt12')}</div>` : `
        <div class="box good" style="padding:15px;border-radius:13px"><div class="b" style="font-size:13.5px;color:var(--good)">Did you submit it?</div>
          <div class="small ink2 mt4" style="line-height:1.55">Confirming moves this to <b>Applied</b>, timestamps it, and schedules a follow-up for <b>${fmtDay(k.followup_date)}</b>.</div>
          <div class="grid-auto mt12" style="grid-template-columns:1fr 1fr;gap:8px">${inputF('kit', 'recruiter_name', '', 'placeholder="Recruiter name (optional)"')}${inputF('kit', 'recruiter_email', '', 'placeholder="Recruiter email (optional)"')}</div>
          ${btn(label('applied', 'Yes — mark applied', 'Saving…'), 'mark-applied', { id: job.id }, 'good block lg mt12', isBusy('applied'))}
          ${btn('Not yet — keep it in approvals', 'close-kit', {}, 'block mt8')}</div>`}
      </div></div></div></div>`;
}

// A submission in progress somewhere (auto submit, Claude Desktop) or whose outcome is unknown. Nothing to start here:
// the duplicate check is automatic, and a submission you make yourself is recorded when you confirm it below.
function renderKitClaim(k) {
  const c = k.claim, job = k.job;
  const execution = renderKitExecution(k);
  if (!c) return execution;
  if (c.status === 'UNCERTAIN') {
    return `<div class="banner warn mb12"><div class="b">A submission was started ${fmtDateTime(c.claimed_at)} and never reported</div>
      <div class="small mt4">Did it go through? This posting stays reserved until you say.</div>
      <div class="row gap8 mt10">${btn('It was submitted', 'kit-resolve', { id: job.id, claim: c.id, submitted: '1' }, 'good sm')}${btn('It was not submitted', 'kit-resolve', { id: job.id, claim: c.id, submitted: '' }, 'sm')}</div></div>`;
  }
  if (!c.claim_token) {
    return `<div class="banner warn mb12"><div class="b">${esc(c.started_by)} is submitting this posting${c.job_id !== job.id ? ` (job #${c.job_id})` : ''}</div><div class="small mt4">Wait for it to report before you submit here.</div></div>`;
  }
  return `<div class="box mb12" style="padding:13px;border-radius:12px"><div class="small b">Submission started ${fmtDateTime(c.claimed_at)}</div>
    <div class="xs muted mt4">Reserved for you until ${fmtDateTime(c.lease_expires_at)}; then it waits for you to say what happened.</div>
    ${btn("I didn't submit — release it", 'kit-release', { id: job.id, token: c.claim_token }, 'ghost sm mt8')}</div>${execution}`;
}
// The application worker (APPLICATION_EXECUTION_MODE): reads and, in assist mode, fills the form. In guarded_auto
// (auto submit) a run of an account that turned Auto submit on submits it, after the pre-submit checks and the shared
// claim; an unknown outcome waits for you (the claim banner above).
function renderKitTakeover(k) {
  const t = k.execution_takeover, x = k.execution;
  if (!t || !x) return '';
  const fields = t.fields || [], login = ['LOGIN_REQUIRED', 'LOGIN_FAILED', 'OTP_REQUIRED', 'ACCOUNT_REQUIRED'].includes(t.reason);
  const captcha = ['CAPTCHA_OR_ANTI_BOT', 'NAVIGATOR_CAPTCHA'].includes(t.reason);
  const why = { LOGIN_REQUIRED: 'The site wants you to sign in', LOGIN_FAILED: 'The saved login did not work', OTP_REQUIRED: 'The site sent a one-time code', ACCOUNT_REQUIRED: 'The site wants a new account', CAPTCHA_OR_ANTI_BOT: 'The site shows a CAPTCHA',
    FIELDS_NEED_YOU: `${fields.length} field${fields.length === 1 ? '' : 's'} need${fields.length === 1 ? 's' : ''} you`, NAVIGATOR_UNEXPECTED_PAGE: 'The page needs a look', NAVIGATOR_LEGAL: 'The form asks you to agree to something',
    LEGAL_ATTESTATION: 'The form asks you to agree to something: tick it in the window', SENSITIVE_NOT_CONFIRMED: 'A sensitive answer needs your OK',
    SUBMIT_NOT_FOUND: 'Job Hunt OS could not tell which button submits this form', REQUIRED_FIELDS_MISSING: 'A required field is still empty' }[t.reason]
    || (String(t.reason || '').startsWith('NAVIGATOR_') ? 'The page needs you' : 'The page needs you');
  const list = fields.length ? `<ul class="small mt6" style="margin:6px 0 0 18px">${fields.map(f => `<li class="pretty">${esc(f.label || f.id)}</li>`).join('')}</ul>` : '';
  return `<div class="banner warn stack mt8"><div class="b">${esc(why)}</div>
    <div class="small mt4 pretty">${fields.length ? 'The browser window is open with these fields highlighted. Fill them there' + ((k.execution_questions || []).length ? ', or answer the questions below' : '') + (t.reason === 'FIELDS_NEED_YOU' ? '. It carries on by itself a few seconds after you finish, or press Continue.' : ', then press Continue.')
      : captcha ? 'Solve it in the browser window Job Hunt OS opened: the application continues by itself as soon as it is solved (or press Continue).'
      : 'Finish it in the browser window Job Hunt OS opened, then press Continue.'} The run picks up that same page: the page is not reloaded. Kept until ${fmtDateTime(t.expires_at)}. ${x.mode === 'GUARDED_AUTO' ? 'Job Hunt OS checks everything again before it submits.' : 'Job Hunt OS never submits from this window; you do.'}</div>${list}
    <div class="row gap8 mt10 wrap">${btn(label('kit-continue', 'I’m done — continue', 'Continuing…'), 'kit-continue', { exec: x.id }, 'sm', isBusy('kit-continue'))}
    ${t.reason === 'ACCOUNT_REQUIRED' ? btn('Sign up with a generated password', 'kit-signup', { exec: x.id }, 'sm') : ''}
    ${login ? btn('Save this login', 'kit-save-login', { exec: x.id }, 'sm ghost') : ''}</div>
    ${S.kitSignup && S.kitSignup.exec === x.id ? `<div class="box mt8" style="padding:10px"><div class="xs muted">Use exactly these on ${esc(S.kitSignup.domain)}; the password is not shown again.</div><div class="small mono mt4">${esc(S.kitSignup.username)}</div><div class="row gap8 mt4"><span class="small mono">${esc(S.kitSignup.password)}</span>${btn('Copy', 'kit-copy-password', {}, 'sm none')}</div></div>` : ''}</div>`;
}
// Fields the person filled in the window: offer to keep those answers for next time.
function renderKitPersonFilled(k) {
  const x = k.execution, filled = (x?.fields || []).filter(f => f.status === 'PERSON');
  if (!filled.length || x.stage !== 'READY_TO_SUBMIT') return '';
  return `<div class="box mt8" style="padding:10px;border-radius:10px"><div class="small">You filled ${filled.length} field${filled.length === 1 ? '' : 's'} yourself in the window (${esc(filled.map(f => f.label).join(', '))}).</div>
    ${btn(label('kit-save-window', 'Save what you typed to the Knowledge bank', 'Saving…'), 'kit-save-window', { exec: x.id }, 'sm mt6', isBusy('kit-save-window'))}<div class="xs muted mt4">Saved as your verified answers, reused for the same questions on other applications. Only while the window is still open.</div></div>`;
}
function renderKitIntent(k) {
  const i = k.execution_intent;
  if (!i) return '';
  const tone = i.status === 'CONFIRMED' ? 'good' : ['UNCERTAIN', 'CLICKING'].includes(i.status) ? 'warn' : 'muted';
  return `<div class="row gap8 mt8">${pill(`Submit ${i.status.replaceAll('_', ' ').toLowerCase()}`, tone)}${i.reason ? `<span class="xs muted pretty">${esc(i.reason)}</span>` : ''}</div>${renderKitEmailCode(k)}`;
}
// After the final click the site emailed a code to finish: look it up in Gmail when you ask, and paste it into the
// browser window yourself. The code lives only in this page while the kit is open; Job Hunt OS never enters it.
function renderKitEmailCode(k) {
  const i = k.execution_intent, x = k.execution;
  if (!i || !x || i.status !== 'UNCERTAIN' || !(i.evidence && i.evidence.email_code)) return '';
  const got = S.kitEmailCode && S.kitEmailCode.exec === x.id ? S.kitEmailCode : null;
  return `<div class="banner warn stack mt8"><div class="b">The site emailed you a code to finish the application</div>
    <div class="small pretty">Enter it in the browser window Job Hunt OS left open. It records the application once the site confirms it.</div>
    ${got && got.code ? `<div class="row gap8 wrap"><span class="mono b" style="font-size:18px;letter-spacing:2px">${esc(got.code)}</span>${btn('Copy', 'kit-copy-email-code', {}, 'sm')}</div>` : ''}
    ${got && got.message ? `<div class="xs pretty">${esc(got.message)}</div>` : ''}
    <div class="row gap8 wrap">${btn(label('kit-email-code', got && got.code ? 'Look again' : 'Get code from Gmail', 'Looking…'), 'kit-email-code', { exec: x.id }, 'sm', isBusy('kit-email-code'))}</div></div>`;
}
// The questions the application worker could not answer from a verified source: answer them here (saved to the
// Knowledge bank as verified, your own words), or open just these in the Knowledge bank. The run continues by itself
// once the last one is answered. Yes/no and choice questions offer the form's own options.
function renderKitQuestions(k) {
  const qs = k.execution_questions || [], left = qs.filter(q => !q.answered).length;
  if (!qs.length) return `<div class="xs muted mt6">Answer ${(k.execution?.interrupt?.question_ids || []).length} question(s) in the Knowledge bank; it continues by itself.</div>`;
  const field = q => {
    const opts = q.answer_options?.length ? q.answer_options : q.answer_type === 'boolean' ? ['Yes', 'No'] : [];
    return opts.length ? selectF('kitq', `q${q.id}`, q.answer, [['', 'Choose…'], ...opts.map(o => [o, o])], `aria-label="${attr(q.question)}"`)
      : textareaF('kitq', `q${q.id}`, q.answer, `rows="2" aria-label="${attr(q.question)}" placeholder="Your answer, in your own words"`);
  };
  return `<div class="mt10"><div class="small b">${left ? `${left} of ${qs.length} question${qs.length === 1 ? '' : 's'} to answer` : 'All answered: it continues on the next pass'}</div>
    <div class="xs muted mt4 pretty">The form asks these and nothing verified answers them yet. What you save here goes to the Knowledge bank as a verified answer, and is reused for the same question on other applications.</div>
    ${qs.map(q => `<div class="box mt8" style="padding:10px;border-radius:10px"><div class="small pretty">${esc(q.question)}${q.sensitive ? ` ${pill('Sensitive · confirmed per application', 'bad')}` : ''}</div>
      ${q.answered ? `<div class="row gap8 mt6"><span class="small b pretty grow">${esc(q.answer)}</span>${pill('Saved', 'good')}</div>`
        : `<div class="mt6">${field(q)}</div>${btn(label(`kitq-${q.id}`, 'Save answer', 'Saving…'), 'kit-answer', { qid: q.id }, 'sm mt6', isBusy(`kitq-${q.id}`))}`}</div>`).join('')}
    ${btn('Open these in the Knowledge bank', 'kit-open-questions', { ids: qs.map(q => q.id).join(',') }, 'sm ghost mt8')}</div>`;
}
function renderKitExecution(k) {
  const x = k.execution, mode = k.execution_mode;
  if (!mode || mode === 'off') return '';
  const waiting = x && x.stage === 'NEEDS_INPUT';
  // Each sensitive answer is shown with the question it answers before it is confirmed: a person authorizes the
  // values they can see, never a count. Only the ones shown are sent.
  const confirm = waiting ? (k.execution_confirmations || []) : [];
  const confirmList = confirm.length ? `<div class="mt8">${confirm.map(c => `<div class="small mt6"><div class="ink2 pretty">${esc(c.question)}</div><div class="b pretty">${esc(c.answer || '(no saved answer)')}</div></div>`).join('')}</div>` : '';
  const state = !x ? '' : `<div class="row gap8 mt8">${pill(x.stage.replaceAll('_', ' '), x.stage === 'READY_TO_SUBMIT' ? 'good' : ['BLOCKED', 'NEEDS_HUMAN', 'FAILED'].includes(x.stage) ? 'warn' : 'muted')}${x.result_code ? `<span class="xs muted">${esc(x.result_code)}</span>` : ''}</div>
    ${waiting && (x.interrupt.question_ids || []).length ? renderKitQuestions(k) : ''}
    ${waiting && confirm.length ? `${confirmList}${btn(confirm.length === 1 ? 'Use this answer for this application' : `Use these ${confirm.length} answers for this application`, 'kit-confirm', { exec: x.id, ids: confirm.map(c => c.id).join(',') }, 'sm mt8')}` : ''}
    ${x.stage === 'READY_TO_SUBMIT' ? `<div class="xs muted mt6 pretty">${x.result_code === 'SUBMITTED_IN_WINDOW' ? 'You submitted it in the browser window; the site confirmed it.' : x.backend === 'browser_use_webui' ? 'Every required field is filled in the open browser window: check it and submit it there.' : x.mode === 'ASSIST' ? 'Every required field was filled from a verified source in a private browser page, which was then closed. Open the posting, fill it in with the answers in this kit and submit it there.' : 'Every required field has a verified source.'}</div>` : ''}`;
  const active = x && ['PENDING', 'PROCESSING'].includes(x.status);
  const guarded = mode === 'guarded_auto' && k.guarded_consent;
  const what = mode === 'observe' ? '' : guarded ? ', fills it and submits it for you — only the site’s confirmation marks it applied; it stops for you only when something needs you' : ', then fills it. You submit';
  // Where the run is: its task in Running workers (status, model, history and logs), one click away.
  const link = x ? btn(active ? 'Watch it in Running workers →' : 'Open in Running workers →', 'wk-open-task', { kind: 'application', id: x.id }, 'sm ghost') : '';
  const retry = active && x.last_error ? `<div class="xs muted mt6 pretty">Last try: ${esc(x.last_error)}${x.attempts ? ` · attempt ${x.attempts}` : ''}</div>` : '';
  return `<div class="box mb12" style="padding:13px;border-radius:12px"><div class="row between wrap gap8"><div class="small b">Application worker · ${esc(mode)}${x && x.backend ? ` · ${esc(x.backend.replaceAll('_', ' '))}` : ''}</div>${link}</div>
    <div class="xs muted mt4 pretty">Reads the form and finds a verified source for every answer${what}.</div>${retry}
    ${state}${renderKitIntent(k)}${renderKitTakeover(k)}${renderKitPersonFilled(k)}${active ? '' : `<div class="row gap8 wrap">${btn(label('kit-prepare', x ? 'Fill again' : 'Fill the form', 'Queueing…'), 'kit-prepare', { id: k.job.id, mode: mode === 'observe' ? '' : 'ASSIST' }, 'sm mt8', isBusy('kit-prepare'))}${k.browser_use_ready && mode !== 'off' ? `<span title="${attr('Opens a browser window (browser-use) that fills the form from your verified answers, step by step, checked by Job Hunt OS, and leaves it open for you to submit.')}">${btn(label('kit-browser-use', 'Run with browser-use', 'Queueing…'), 'kit-browser-use', { id: k.job.id }, 'sm mt8', isBusy('kit-browser-use'))}</span>` : ''}${guarded ? btn(label('kit-guarded', 'Apply for me', 'Queueing…'), 'kit-guarded', { id: k.job.id }, 'sm mt8 primary', isBusy('kit-guarded')) : ''}</div>`}</div>`;
}

/* ───────────── tracker ───────────── */
// Tracker filters run in the browser: /api/tracker already returns every submitted application.
function trackerLocations(apps) {
  const counts = {};
  for (const a of apps) { const k = locationKey(a.location); counts[k] = (counts[k] || 0) + 1; }
  return Object.entries(counts).sort((x, y) => y[1] - x[1] || x[0].localeCompare(y[0]));
}
function filterTracker(apps, f) {
  const q = f.q.trim().toLowerCase();
  return apps.filter(a => (f.location === 'all' || locationKey(a.location) === f.location)
    && (!f.min_fit || (a.fit ?? -1) >= f.min_fit)
    && (!q || `${a.title} ${a.company} ${a.location || ''}`.toLowerCase().includes(q)));
}
// A link to one application (go('tracker', { selApp })) brings it onto the board: archived cards are shown and
// filters that hide it are cleared, once, so the detail panel and the board always agree.
function revealTrackerApp() {
  const id = S.trackerReveal; S.trackerReveal = null;
  const app = id && D.tracker?.applications.find(a => a.id === id);
  if (!app) return;
  if (app.archived) S.showArchived = true;
  if (!filterTracker([app], S.tf).length) S.tf = { q: '', location: 'all', min_fit: 0 };
}
function renderTrackerFilters(all, shown, archived) {
  const f = S.tf, active = f.q || f.location !== 'all' || f.min_fit;
  const opts = (list, cur) => list.map(([v, t]) => `<option value="${attr(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(t)}</option>`).join('');
  return `<div class="tr-toolbar">
    <label class="tr-search"><span class="tr-search-icon" aria-hidden="true">⌕</span><input class="input bare" id="tf-q" data-tf="q" value="${attr(f.q)}" placeholder="Search role, company or location…" aria-label="Search applications"></label>
    <select class="select" id="tf-location" data-tf="location" aria-label="Location">${opts(locationOptions(trackerLocations(all)), f.location)}</select>
    <select class="select" id="tf-min" data-tf="min_fit" aria-label="Minimum fit">${opts([[0, 'Any fit'], [50, 'Fit ≥ 50'], [70, 'Fit ≥ 70'], [80, 'Fit ≥ 80']], f.min_fit)}</select>
    ${active ? `<span class="tr-showing">Showing ${shown} of ${all.length}</span>${btn('Reset', 'tracker-reset-filters', {}, 'sm ghost')}` : ''}
    <span class="grow"></span>
    ${archived ? `<button class="tr-chip ${S.showArchived ? 'on' : ''}" data-act="tracker-show-archived" aria-pressed="${S.showArchived}">${S.showArchived ? 'Hide archived' : `Show archived (${archived})`}</button>` : ''}
    ${btn('Archive old…', 'tracker-archive-open', {}, 'sm' + (S.trackerArchive ? ' primary' : ''))}</div>`;
}

// Rejected and Closed hold finished applications: their columns are folded to a narrow strip unless the viewer
// opened them (remembered in this browser). A card can still be dropped on a folded column.
const FOLDABLE_STAGES = ['rejected', 'closed'], OPEN_STAGES_KEY = 'trackerOpenStages';
const openStages = (() => { try { return new Set(JSON.parse(localStorage.getItem(OPEN_STAGES_KEY) || '[]')); } catch { return new Set(); } })();
const stageFolded = id => FOLDABLE_STAGES.includes(id) && !openStages.has(id);
function toggleStage(id) {
  if (openStages.has(id)) openStages.delete(id); else openStages.add(id);
  try { localStorage.setItem(OPEN_STAGES_KEY, JSON.stringify([...openStages])); } catch { /* storage blocked: kept for this page */ }
}

// Archive old applications: take the ones submitted more than N days ago (0: any age) off the board; a live one also
// moves to Closed. The server counts them first (dry_run), so the button says exactly how many, and from which stages.
const ARCHIVE_PRESETS = [15, 30, 45, 60, 90];
const ARCHIVE_SCOPES = [['no_reply', 'Applied and Acknowledged (no reply yet)'], ['live', 'Every live stage, interviews and offers included'],
  ['finished', 'Rejected and Closed'], ['all', 'Everything, live and finished']];
function renderTrackerArchive() {
  const a = S.trackerArchive; if (!a) return '';
  const p = a.preview, n = p?.count ?? 0;
  const stages = p?.by_stage?.length ? p.by_stage.map(s => `<span class="tr-tally"><b>${s.count}</b> ${esc(s.label)}</span>`).join('') : '';
  return `<section class="card tr-archive">
    <div class="row between top-align gap10"><div><div class="card-title">Archive old applications</div>
      <div class="small muted mt4 pretty">Takes them off the board. Live ones also move to Closed, so they get no follow-ups and are no longer checked in Gmail; Rejected ones stay Rejected. Show archived brings them back into view, and Unarchive puts one back.</div></div>
      ${btn('Cancel', 'tracker-archive-cancel', {}, 'sm ghost none')}</div>
    <div class="tr-archive-grid">
      <div class="caps">SUBMITTED MORE THAN</div>
      <div class="row wrap gap6">${ARCHIVE_PRESETS.map(d => btn(`${d} days`, 'tracker-archive-days', { days: d }, 'sm ' + (a.days === d ? 'primary' : ''))).join('')}
        <span class="tr-days"><input class="input" type="number" min="0" max="3650" id="archive-days" data-act-change="tracker-archive-days" value="${a.days}" aria-label="Days since submission"><span class="small muted">days ago</span></span>
        ${btn('Any age', 'tracker-archive-days', { days: 0 }, 'sm ' + (a.days === 0 ? 'primary' : ''))}</div>
      <div class="caps">FROM</div>
      <select class="select" style="width:auto;max-width:100%" id="archive-scope" data-act-change="tracker-archive-scope" aria-label="Stages to archive">
        ${ARCHIVE_SCOPES.map(([v, t]) => `<option value="${v}" ${a.scope === v ? 'selected' : ''}>${t}</option>`).join('')}</select>
    </div>
    <div class="tr-archive-foot">${btn(label('archive', a.loading ? 'Counting…' : n ? `Archive ${n} application${n === 1 ? '' : 's'}` : 'Nothing to archive', 'Archiving…'), 'tracker-archive-run', {}, 'primary', isBusy('archive') || a.loading || !n)}
      ${stages ? `<div class="row wrap gap6">${stages}</div>` : ''}</div></section>`;
}

async function trackerArchivePreview() {
  const a = S.trackerArchive; if (!a) return;
  a.loading = true; render();
  const ask = { days: a.days, scope: a.scope };
  try {
    const preview = await api('/api/tracker/archive', { method: 'POST', body: { ...ask, dry_run: true } });
    if (S.trackerArchive === a && a.days === ask.days && a.scope === ask.scope) a.preview = preview;
  } catch (e) { toast(e.message, true); }
  finally { a.loading = false; render(); }
}

// A soft colour per company, so the same employer reads the same everywhere on the board.
const AVATAR_TONES = [['#eeecff', '#3a2bb5'], ['#e3f6f2', '#0f7a68'], ['#fff3e0', '#9a5a00'], ['#fdeef1', '#a8263f'], ['#e8f1fd', '#1f5fa8'], ['#f1ecfb', '#6b3fa8']];
function companyAvatar(name, cls = '') {
  const text = String(name || '?').trim();
  let h = 0; for (const ch of text) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const [bg, fg] = AVATAR_TONES[h % AVATAR_TONES.length];
  return `<span class="tr-avatar ${cls}" style="background:${bg};color:${fg}" aria-hidden="true">${esc((text.match(/[A-Za-z0-9]/) || ['?'])[0].toUpperCase())}</span>`;
}
const fitBadge = n => `<span class="tr-fit ${fitCls(n)}" title="Fit score">${fitTxt(n)}</span>`;
function trackerCard(k, sel) {
  const tag = k.tag ? pill(k.tag, k.tone === 'warn' ? 'warn' : k.tone === 'good' ? 'good' : 'muted') : '';
  return `<div class="kcard ${k.id === sel.id ? 'sel' : ''} ${k.archived ? 'archived' : ''}" draggable="true" data-drag-app="${k.id}" data-act="select-app" data-id="${k.id}">
    <div class="row top-align between gap8"><div class="tr-card-title">${esc(k.title)}</div>${fitBadge(k.fit)}</div>
    <div class="row gap6 mt6 tr-card-co">${companyAvatar(k.company)}<span class="xs ink2 tr-ellipsis">${esc(k.company)}</span></div>
    <div class="row gap6 wrap tr-card-foot"><span class="tr-age" title="Submitted">${esc(k.age)}</span>${tag}${k.archived ? pill('archived', 'muted') : ''}</div></div>`;
}

function renderTracker() {
  const t = D.tracker; if (!t) return loading();
  const archived = t.applications.filter(a => a.archived).length;
  const all = S.showArchived ? t.applications : t.applications.filter(a => !a.archived), apps = filterTracker(all, S.tf);
  const stats = [[t.stats.live, 'Live', ''], [t.stats.replies, 'Replies', 'good'], [t.stats.due, 'Follow-ups due', t.stats.due ? 'warn' : ''], [t.stats.response_rate, 'Response rate', 'brand']];
  const views = [['board', 'Board'], ['timeline', 'Timeline'], ['inbox', 'Inbox insights']];
  const head = `<div class="tr-head"><div class="tr-stats">${stats.map(([n, l, tone]) => `<div class="tr-stat${tone ? ' tone-' + tone : ''}"><div class="tr-stat-n">${esc(n)}</div><div class="tr-stat-l">${l}</div></div>`).join('')}</div>
    <div class="seg" role="tablist" aria-label="Tracker view">${views.map(([id, l]) => `<button class="${S.trackerView === id ? 'on' : ''}" data-act="tracker-view" data-id="${id}" role="tab" aria-selected="${S.trackerView === id}">${l}</button>`).join('')}</div></div>`;
  if (S.trackerView === 'inbox') return `<div class="stack">${head}${renderInboxInsights()}</div>`;
  const toolbar = t.applications.length ? `${renderTrackerFilters(all, apps.length, archived)}${renderTrackerArchive()}` : '';
  if (!all.length && archived) return `<div class="stack">${head}${toolbar}<section class="card">${empty('Every application is archived', 'Show archived to see them, or unarchive one to put it back on the board.', btn(`Show archived (${archived})`, 'tracker-show-archived', {}, 'primary'))}</section></div>`;
  if (!all.length) return `<div class="stack">${head}<section class="card">${empty('No submitted applications yet', 'Approve a packet and confirm you applied from the apply kit — it lands here.', btn('Open approvals', 'go', { screen: 'approvals' }, 'primary'))}</section></div>`;
  if (!apps.length) return `<div class="stack">${head}${toolbar}<section class="card">${empty('No applications match', 'Try another location or clear the filters.', btn('Reset filters', 'tracker-reset-filters', {}, 'primary'))}</section></div>`;
  if (S.trackerView === 'timeline') {
    const events = apps.flatMap(a => a.timeline.map(e => ({ ...e, app: a }))).sort((x, y) => y.at.localeCompare(x.at));
    return `<div class="stack">${head}${toolbar}<section class="card">${events.map(e => `<div class="tl"><div class="tl-rail"><span class="d" style="background:${KIND_DOT[e.kind] || '#c6cad8'}"></span><span class="l"></span></div><div style="padding-bottom:16px" class="grow row top-align gap10">${companyAvatar(e.app.company, 'sm')}<div class="grow"><div class="small b">${esc(e.app.company)} · ${esc(e.title)}</div><div class="xs muted mt4">${fmtDateTime(e.at)}${e.detail && e.kind !== 'stage' ? ' · ' + esc(e.detail) : ''}</div></div></div>${btn('Open', 'select-app', { id: e.app.id, board: 1 }, 'sm none')}</div>`).join('')}</section></div>`;
  }
  // The panel only ever shows a card the board shows (folded columns included), so its actions never act on an
  // application the filters hid. A link to one hidden application reveals it first (revealTrackerApp).
  const sel = apps.find(a => a.id === S.selApp) || apps.find(a => !stageFolded(a.stage) && !a.archived) || apps[0];
  const selStage = t.stages.find(s => s.id === sel.stage) || { label: sel.stage, dot: '#c6cad8' };
  const column = st => {
    const cards = apps.filter(a => a.stage === st.id);
    if (stageFolded(st.id)) return `<div class="kcol folded" data-drop-stage="${st.id}" data-act="tracker-fold" data-id="${st.id}" title="${attr(`Show ${st.label}`)}" role="button" aria-expanded="false">
      <span class="dot" style="width:8px;height:8px;background:${st.dot}"></span><span class="mono xs muted">${cards.length}</span><span class="small b kfold-label">${esc(st.label)}</span><span class="tr-unfold" aria-hidden="true">›</span></div>`;
    return `<div class="kcol" data-drop-stage="${st.id}" style="--stage:${st.dot}">
      <div class="kcol-head"><span class="dot" style="width:8px;height:8px;background:${st.dot}"></span><span class="small b grow">${esc(st.label)}</span><span class="tr-count">${cards.length}</span>${FOLDABLE_STAGES.includes(st.id) ? `<button class="tr-icon-btn" data-act="tracker-fold" data-id="${st.id}" title="Collapse ${attr(st.label)}" aria-label="Collapse ${attr(st.label)}" aria-expanded="true">‹</button>` : ''}</div>
      <div class="kcol-body">${cards.length ? cards.map(k => trackerCard(k, sel)).join('') : '<div class="kcol-empty">Drop a card here</div>'}</div></div>`;
  };
  const addStage = S.addingStage ? `<div class="kadd"><div class="row between"><span class="small b ink2">Add a stage</span><button class="tr-icon-btn" data-act="tracker-add-toggle" aria-label="Close">×</button></div>
      <div class="xs muted" style="line-height:1.45">Track the parts of your process this board is missing.</div>
      <div class="row gap6 wrap">${t.available_stages.map(s => btn('+ ' + esc(s.label), 'add-stage', { id: s.id }, 'sm')).join('') || '<span class="xs muted">Every stage is on the board.</span>'}</div>
      ${t.extra_stages.length ? `<button class="btn ghost sm danger" style="margin-top:auto;justify-content:flex-start" data-act="remove-stage">Remove the last added stage</button>` : ''}</div>`
    : `<button class="kadd-mini" data-act="tracker-add-toggle" title="Add a stage" aria-label="Add a stage">+</button>`;
  return `<div class="stack" style="gap:14px">${head}${toolbar}
  <div class="kanban">${t.stages.map(column).join('')}${addStage}</div>
  <section class="card tr-detail">
    <div class="tr-detail-head">${companyAvatar(sel.company, 'lg')}
      <div class="grow" style="min-width:0"><div class="row gap8 wrap"><span class="card-title">${esc(sel.title)}</span>${fitBadge(sel.fit)}</div>
        <div class="small muted mt4"><b class="ink2">${esc(sel.company)}</b>${sel.location ? ' · ' + esc(sel.location) : ''} · ${esc(sel.meta)}</div>
        <div class="row gap6 wrap mt8"><span class="tr-stage-pill" style="--stage:${selStage.dot}"><span class="dot" style="background:${selStage.dot}"></span>${esc(selStage.label)}</span>${sel.archived ? pill('archived', 'muted') : ''}</div></div>
      <div class="row gap8 wrap tr-detail-actions">${sel.archived ? btn(label('unarchive', 'Unarchive', 'Unarchiving…'), 'tracker-unarchive', { app: sel.id }, 'sm primary', isBusy('unarchive')) : ''}${btn('Open job', 'open-job', { id: sel.job_id }, 'sm')}${sel.resume_id ? btn('Open packet', 'go', { screen: 'tailor', job: sel.job_id }, 'sm') : ''}${sel.url ? `<a class="btn sm" href="${attr(sel.url)}" target="_blank" rel="noopener">Open posting ↗</a>` : ''}</div></div>
    <div class="movebar"><span class="caps none">MOVE TO</span>
      <div class="row wrap gap6 grow">${t.stages.map(st => `<button class="tr-move ${st.id === sel.stage ? 'on' : ''}" data-act="move-stage" data-app="${sel.id}" data-stage="${st.id}" ${st.id === sel.stage ? 'aria-current="true"' : ''}><span class="dot" style="background:${st.dot}"></span>${esc(st.label)}</button>`).join('')}</div><span class="xs muted none drag-hint">or drag the card between columns</span></div>
    <div class="split gap18">
      <div style="flex:1 1 320px;min-width:0"><div class="caps mb12">HISTORY</div>
        <div class="col">${sel.timeline.map(e => `<div class="tl"><div class="tl-rail"><span class="d" style="background:${KIND_DOT[e.kind] || '#c6cad8'}"></span><span class="l"></span></div><div style="padding-bottom:16px"><div class="small b">${esc(e.title)}</div><div class="xs muted mt4 pretty">${eventTime(e)}${e.detail && e.kind !== 'stage' ? ' · ' + esc(e.detail) : ''}</div>${eventContact(e)}</div></div>`).join('')}</div>
        ${renderEmailActivity(sel)}
        <div class="logbox"><select class="select" style="width:auto;padding:6px 8px" id="log-kind">${['note', 'call', 'email', 'referral'].map(k => `<option value="${k}">${k[0].toUpperCase() + k.slice(1)}</option>`).join('')}</select>
          <input class="input bare grow" id="log-text" placeholder="Log something — call, email, referral…" data-enter="log-event" data-app="${sel.id}">${btn(label('log', 'Add', 'Adding…'), 'log-event', { app: sel.id }, 'sm', isBusy('log'))}</div>
      </div>
      <div style="flex:1 1 260px;min-width:0" class="col gap12">
        <div class="box brand tr-next"><div class="caps mb8">NEXT STEP</div><div class="base b">${esc(sel.next)}</div>${sel.next_date ? `<div class="small muted mt4">${esc(sel.next_date)}</div>` : ''}</div>
        ${renderRecruiters(sel)}
        <div class="box" style="padding:13px"><div class="caps mb8">SUBMITTED</div>
          <div class="small ink2" style="line-height:1.5">${sel.resume_id ? `<a href="#" data-act="go" data-screen="tailor" data-job="${sel.job_id}">${esc(sel.docs)}</a>` : esc(sel.docs)}</div>${sel.notes ? `<div class="xs muted mt8 pretty">${esc(sel.notes)}</div>` : ''}</div>
        <div class="box" style="padding:13px;border-color:#e2ddff;background:var(--brand-softer)"><div class="row gap6 mb8">${pill('AI', 'brand')}<span class="xs b" style="color:var(--brand-ink)">Follow-up draft</span></div>
          ${sel.inbox_reply ? '<div class="xs mb8" style="color:var(--good)">The employer already replied (Email activity), so a follow-up is probably unnecessary.</div>' : ''}
          <div class="small ink2 pretty" style="line-height:1.55;white-space:pre-wrap">${esc(sel.draft || 'No draft yet. Generate one — nothing is ever sent for you.')}</div>
          <div class="row gap8 mt10">${sel.draft ? btn('Review &amp; copy', 'copy-draft', { app: sel.id }, 'sm primary') : ''}${btn(label('nudge' + sel.id, sel.draft ? 'Regenerate' : 'Draft follow-up', 'Drafting…'), 'draft-nudge', { app: sel.id, stay: 1 }, 'sm' + (sel.draft ? '' : ' primary'), isBusy('nudge' + sel.id))}</div></div>
        ${renderStagePrep(sel)}
        ${S.scheduling ? renderScheduleForm(sel) : btn('Schedule an interview', 'schedule-start', {}, 'block')}
      </div></div>
  </section></div>`;
}

/* recruiters and interviews */
// Some browsers still report renamed IANA zones; show and store the current names.
const TZ_RENAMED = { 'Asia/Calcutta': 'Asia/Kolkata', 'Asia/Saigon': 'Asia/Ho_Chi_Minh', 'Asia/Katmandu': 'Asia/Kathmandu', 'Asia/Rangoon': 'Asia/Yangon',
  'Europe/Kiev': 'Europe/Kyiv', 'Atlantic/Faeroe': 'Atlantic/Faroe', 'America/Buenos_Aires': 'America/Argentina/Buenos_Aires', 'Pacific/Enderbury': 'Pacific/Kanton' };
const tzName = tz => TZ_RENAMED[tz] || tz;
const BROWSER_TZ = (() => { try { return tzName(Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'); } catch { return 'UTC'; } })();
let tzOptionsCache = null;
function tzOptions() {
  if (tzOptionsCache) return tzOptionsCache;
  let zones = [];
  try { zones = [...new Set(Intl.supportedValuesOf('timeZone').map(tzName))].sort(); } catch { /* older browsers */ }
  if (!zones.length) zones = ['UTC', 'Asia/Kolkata', 'Asia/Dubai', 'Asia/Singapore', 'Europe/London', 'Europe/Berlin', 'America/New_York', 'America/Chicago', 'America/Los_Angeles', 'Australia/Sydney'];
  if (!zones.includes('UTC')) zones = ['UTC', ...zones];
  if (!zones.includes(BROWSER_TZ)) zones = [BROWSER_TZ, ...zones];
  const offset = tz => { try { return new Intl.DateTimeFormat('en-US', { timeZone: tz, timeZoneName: 'shortOffset' }).formatToParts(new Date()).find(p => p.type === 'timeZoneName')?.value || ''; } catch { return ''; } };
  tzOptionsCache = zones.map(tz => [tz, `${tz.replace(/_/g, ' ')}${offset(tz) ? ` (${offset(tz)})` : ''}`]);
  return tzOptionsCache;
}
function eventTime(e) {
  if (!e.at_local) return fmtDateTime(e.at);
  return esc(e.at_local) + (e.timezone !== BROWSER_TZ ? ` <span class="faint">(${fmtDateTime(e.at)} your time)</span>` : '');
}
function eventContact(e) {
  const bits = [e.contact_name && esc(e.contact_name), e.contact_email && `<a href="mailto:${attr(e.contact_email)}">${esc(e.contact_email)}</a>`,
    e.contact_phone && `<a href="tel:${attr(e.contact_phone.replace(/[^+\d]/g, ''))}">${esc(e.contact_phone)}</a>`].filter(Boolean);
  return bits.length ? `<div class="xs muted mt4">${bits.join(' · ')}</div>` : '';
}
function renderRecruiters(sel) {
  const list = sel.recruiters || [];
  if (!S.editingRecruiter) {
    return `<div class="box" style="padding:13px"><div class="row between mb8"><span class="caps">RECRUITERS${list.length > 1 ? ` · ${list.length}` : ''}</span><a href="#" class="xs" data-act="edit-recruiter" data-app="${sel.id}">${list.length ? 'Edit' : 'Add'}</a></div>
      ${list.length ? `<div class="col gap10">${list.map((r, i) => `<div${i ? ' style="padding-top:10px;border-top:1px solid var(--line3)"' : ''}>
        <div class="base b">${esc(r.name || r.email || r.phone)}${i === 0 && list.length > 1 ? ' <span class="xs muted" style="font-weight:400">· primary</span>' : ''}</div>
        ${r.email && r.name ? `<div class="small mt4"><a href="mailto:${attr(r.email)}">${esc(r.email)}</a></div>` : ''}
        ${r.phone && (r.name || r.email) ? `<div class="small mt4"><a href="tel:${attr(r.phone.replace(/[^+\d]/g, ''))}">${esc(r.phone)}</a></div>` : ''}</div>`).join('')}</div>`
        : `<div class="base b">No contact captured</div><div class="small muted mt4">Add recruiters to personalise nudges</div>`}</div>`;
  }
  const rows = S.recruiterDraft.map((r, i) => `<div class="col gap6"${i ? ' style="padding-top:10px;border-top:1px solid var(--line3)"' : ''}>
      <div class="row between"><span class="xs muted">${i === 0 ? 'Primary recruiter' : `Recruiter ${i + 1}`}</span>${btn('Remove', 'recruiter-remove', { index: i }, 'sm ghost')}</div>
      <input class="input" style="margin:0" id="rec-${i}-name" data-rec="${i}" data-recfield="name" value="${attr(r.name)}" placeholder="Name">
      <input class="input" style="margin:0" id="rec-${i}-email" data-rec="${i}" data-recfield="email" type="email" value="${attr(r.email)}" placeholder="Email (optional)">
      <input class="input" style="margin:0" id="rec-${i}-phone" data-rec="${i}" data-recfield="phone" type="tel" value="${attr(r.phone)}" placeholder="Phone (optional)"></div>`).join('');
  return `<div class="box" style="padding:13px"><div class="caps mb8">RECRUITERS</div>
    <div class="col gap10">${rows || '<div class="small muted">No recruiters. Add one below.</div>'}</div>
    ${S.recruiterDraft.length < 20 ? btn('+ Add recruiter', 'recruiter-add', {}, 'sm mt10') : ''}
    <div class="row gap8 mt10">${btn('Cancel', 'edit-recruiter-cancel', {}, 'sm')}${btn(label('recruiter', 'Save', 'Saving…'), 'save-recruiter', { app: sel.id }, 'sm primary', isBusy('recruiter'))}</div></div>`;
}
function renderScheduleForm(sel) {
  const d = S.ivDraft;
  const field = (key, extra) => `<input class="input" style="margin:0" id="iv-${key}" data-iv="${key}" value="${attr(d[key])}" ${extra}>`;
  return `<div class="box" style="padding:13px"><div class="caps mb8">SCHEDULE AN INTERVIEW</div><div class="col gap8">
    ${field('title', 'placeholder="e.g. Panel interview"')}
    <div class="row gap8 wrap">${field('at', 'type="datetime-local" style="margin:0;flex:1 1 180px"')}
      <select class="select" style="margin:0;flex:1 1 180px" id="iv-timezone" data-iv="timezone" aria-label="Time zone">${tzOptions().map(([v, t]) => `<option value="${attr(v)}" ${v === d.timezone ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select></div>
    <div class="xs muted mt4">Contact for this interview (optional)</div>
    ${field('contact_name', 'placeholder="Name"')}
    <div class="row gap8 wrap">${field('contact_email', 'type="email" placeholder="Email" style="margin:0;flex:1 1 160px"')}${field('contact_phone', 'type="tel" placeholder="Phone" style="margin:0;flex:1 1 140px"')}</div></div>
    <div class="row gap8 mt10">${btn('Cancel', 'schedule-cancel', {}, 'sm')}${btn(label('schedule', 'Save', 'Saving…'), 'schedule-save', { app: sel.id }, 'sm primary', isBusy('schedule'))}</div></div>`;
}
const KIND_DOT = { applied: '#5b4ae8', stage: '#0f9c86', note: '#c6cad8', call: '#0f9c86', email: '#a79bf0', referral: '#a79bf0', interview: '#e0a136' };

/* ───────────── job modal + palette ───────────── */
// A job you turned down says so wherever it turns up again, with the reason when you gave one.
const rejectionBanner = j => !j.rejection ? ''
  : `<div class="banner warn" style="margin:0 24px;display:block"><b>Rejected ${esc(j.rejection.on)}</b> · ${esc(j.rejection.label)}${j.rejection.note ? ` — ${esc(j.rejection.note)}` : ''}</div>`;

// Where a job is held in the workflow (ranker.pipeline_status) and the one action that puts it back. Approval is
// refused while `approval_blocked`, so the banner says so where the person would try to approve.
const pipelineBanner = (j, margin = '0 24px') => {
  const p = j?.pipeline;
  if (!p) return '';
  const when = p.since ? ` · ${esc(fmtDay(p.since))}` : '';
  const action = p.action === 'rank'
    ? btn(label('pipeline' + j.id, p.action_label, 'Queueing…'), 'pipeline-rank', { id: j.id }, 'sm primary', isBusy('pipeline' + j.id))
    : p.action === 'description' ? btn(p.action_label, 'open-job', { id: j.id }, 'sm primary') : '';
  return `<div class="banner warn" data-pipeline="${attr(p.state)}" style="margin:${margin};display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap">
    <div class="grow" style="min-width:220px"><b>${esc(p.title)}</b>${when}${p.approval_blocked ? ' · <span class="b">approval is on hold</span>' : ''}
      <div class="small mt4">${esc(p.message)}</div>${p.reason ? `<div class="xs muted mt4" title="${attr(p.reason)}">Last step: ${esc(p.reason.length > 160 ? p.reason.slice(0, 160) + '…' : p.reason)}</div>` : ''}</div>${action}</div>`;
};

// The resume a job is ranked and tailored with (Job.resume_id / resume_choice): auto = the best-tagged resume
// (app.services.resume_matching), active = always the active resume, or one specific version.
const resumeLabel = r => `${r.name}${r.roles?.length ? ` — ${r.roles.slice(0, 2).join(', ')}${r.roles.length > 2 ? '…' : ''}` : ''}${r.is_active ? ' (active)' : ''}`;
const resumeValue = (id, choice) => choice === 'ACTIVE' || (choice === 'SETUP' && id == null) ? 'active' : id == null ? 'auto' : String(id);
// The resume a job actually resolves to (workspace.resume_in_use) and why, so "Auto — best match by tags" is never a
// mystery: the select shows the choice, this line names the resume ranking and tailoring read.
const resumeName = r => `${r.name}${r.name === `Version ${r.version}` ? '' : ` · v${r.version}`}${r.is_active ? ' (active)' : ''}`;
const resumeInUseNote = (u, extra = '', jobId = null) => !u ? ''
  : `<div class="xs faint mt4" data-resume-in-use="${attr(u.id)}">Uses <b class="ink2">${esc(resumeName(u))}</b> — ${esc(u.how)}${extra}</div>${rematchNote(u.rematch, jobId)}`;
// Tags changed since this job was matched (workspace.stale_match): say what they pick now, and re-match on a click only,
// since a ranked job is ranked again.
const rematchNote = (r, jobId) => !r || jobId == null ? ''
  : `<div class="row gap8 wrap mt4" style="align-items:center" data-rematch="${attr(r.resume?.id ?? '')}"><span class="xs" style="color:var(--warn)">Your resume tags ${r.pinned ? '' : 'now '}match ${r.resume ? `<b>${esc(resumeName(r.resume))}</b>` : 'no resume clearly (the active resume would be used)'} (${esc(r.reason)})</span>${btn(label('rematch' + jobId, r.pinned ? 'Use the tag match' : 'Re-match this job', 'Re-matching…'), 'job-rematch', { id: jobId, pinned: r.pinned ? 1 : '' }, 'sm soft', isBusy('rematch' + jobId))}</div>`;
// The resume a packet was tailored from (workspace.packet_source_resume), and a warning when the job now uses another.
function packetSourceNote(src, now) {
  if (!src) return '';
  if (!src.recorded) return '<div class="xs faint mt4" data-packet-source="">Tailored from: not recorded for this version</div>';
  const name = src.resume ? `<b class="ink2">${esc(resumeName(src.resume))}</b>` : '<b class="ink2">a resume that was deleted</b>';
  const why = !src.differs && now ? ` — ${esc(now.how)}` : '';
  const warn = src.differs && now ? `<div class="xs mt4" style="color:var(--warn)">This job now uses ${esc(resumeName(now))} (${esc(now.how)}). Regenerate to tailor from it.</div>` : '';
  return `<div class="xs muted mt4" data-packet-source="${attr(src.resume?.id ?? '')}">Tailored from ${name}${why}</div>${warn}`;
}
function resumeOptions(choices) {
  const groups = {};
  for (const r of choices) (groups[tagGroups(r.domains, r.domain, r.roles).map(g => g.domain).filter(Boolean).join(', ') || 'Untagged'] ||= []).push(r);
  return Object.entries(groups).sort(([a], [b]) => (a === 'Untagged') - (b === 'Untagged') || a.localeCompare(b))
    .map(([domain, list]) => ({ domain, list }));
}
function resumeSelect(kind, current, { id = '', placeholder = '', disabled = false, choice = null, fill = true } = {}) {
  const choices = D.resumeChoices || [];
  if (!choices.length) return '';
  const cur = placeholder ? '' : resumeValue(current, choice);
  const opt = (v, t) => `<option value="${attr(v)}" ${String(v) === cur ? 'selected' : ''}>${esc(t)}</option>`;
  const groups = resumeOptions(choices).map(g => `<optgroup label="${attr(g.domain)}">${g.list.map(r => opt(r.id, resumeLabel(r))).join('')}</optgroup>`).join('');
  // Option text can be long (name — roles): the select fills the space it has and never grows past it.
  const size = fill ? 'flex:1 1 0;width:100%' : 'flex:0 1 240px;width:240px';
  return `<select class="select" style="margin:0;${size};min-width:0;max-width:100%;padding:6px 8px;text-overflow:ellipsis" data-resume-choice="${kind}" data-id="${attr(id)}" aria-label="Resume" ${disabled ? 'disabled' : ''}>${placeholder ? opt('', placeholder) : ''}${opt('auto', 'Auto — best match by tags')}${opt('active', 'Active resume')}${groups}</select>`;
}

// Tagging a resume with its domains, each with the roles it targets. A library file and its version share one set of
// tags. The editor keeps group i in S.forms.rtag as domain_i / roles_i; `groups` lists the indexes still shown.
const tagGroups = (domains, domain, roles = []) => domains?.length ? domains : (domain || roles.length ? [{ domain, roles }] : []);
function resumeTags(kind, id, domain, roles = [], domains = null) {
  if (S.tagEdit === `${kind}:${id}`) return resumeTagEditor(kind, id);
  const groups = tagGroups(domains, domain, roles);
  const shown = groups.map(g => `${g.domain ? pill(g.domain, 'brand') : ''}${(g.roles || []).map(r => pill(r, 'plain')).join('')}`)
    .join('<span class="faint" aria-hidden="true">·</span>');
  return `<div class="row wrap gap6 mt6" style="align-items:center">${shown}${btn(groups.length ? 'Edit tags' : 'Tag domains &amp; roles', 'rtag-edit', { kind, id, groups: JSON.stringify(groups) }, 'sm ghost')}</div>`;
}
function resumeTagGroup(index, catalog, removable) {
  const domain = String(fv('rtag', `domain_${index}`, '') || ''), roles = fv('rtag', `roles_${index}`, []);
  const known = catalog.find(c => c.domain.toLowerCase() === domain.trim().toLowerCase());
  const suggestions = (known ? known.roles : []).filter(r => !roles.includes(r));
  return `<div class="col gap8" data-rtag-group="${index}">
    <div class="row gap8" style="align-items:flex-end"><label class="field" style="flex:1;margin:0"><span class="xs muted">Domain</span>${inputF('rtag', `domain_${index}`, domain, 'list="resume-domain-list" placeholder="e.g. Backend, Product" maxlength="80"')}</label>${removable ? btn('Remove domain', 'rtag-remove-group', { index }, 'sm ghost') : ''}</div>
    <div><div class="xs muted mb4">Roles for this domain</div>${tagBox('rtag', `roles_${index}`, roles, 'Add a role, e.g. Lead Engineer…', 30)}</div>
    ${suggestions.length ? `<div class="row wrap gap6">${suggestions.map(r => btn(`+ ${esc(r)}`, 'rtag-add', { role: r, index }, 'sm ghost')).join('')}</div>` : domain ? '' : `<div class="xs muted">Pick a domain to see suggested roles: ${catalog.map(c => esc(c.domain)).join(' · ')}</div>`}</div>`;
}
function resumeTagEditor(kind, id) {
  const catalog = D.resumeCatalog || [], groups = S.forms.rtag?.groups || [0];
  return `<div class="box mt8 col gap10" data-rtag-editor style="max-width:620px">
    <datalist id="resume-domain-list">${catalog.map(c => `<option value="${attr(c.domain)}"></option>`).join('')}</datalist>
    ${groups.map(i => resumeTagGroup(i, catalog, groups.length > 1)).join('<div class="hr" style="margin:4px 0"></div>')}
    ${groups.length < 8 ? `<div>${btn('+ Add domain', 'rtag-add-group', {}, 'sm ghost')}</div>` : ''}
    <div class="xs faint">New jobs are matched to the resume whose roles their title names (then its domains), scored by the domain that fits best. Pinned choices are never changed.</div>
    <div class="row gap8">${btn(label('rtagSave', 'Save tags', 'Saving…'), 'rtag-save', { kind, id }, 'sm primary', isBusy('rtagSave'))}${btn('Cancel', 'rtag-cancel', {}, 'sm')}</div></div>`;
}
// The editor's groups, in order, as the API takes them.
function resumeTagBody() {
  const form = S.forms.rtag || {};
  return { domains: (form.groups || []).map(i => ({ domain: String(form[`domain_${i}`] || '').trim() || null, roles: form[`roles_${i}`] || [] }))
    .filter(g => g.domain || g.roles.length) };
}

// Changing a job's resume ranks it again with that resume (and a tailored packet is marked for re-tailoring).
function chooseResume(el) {
  if (el.value === '') return;
  const body = ['auto', 'active'].includes(el.value) ? { mode: el.value } : { mode: 'fixed', resume_id: Number(el.value) };
  const bulk = el.dataset.resumeChoice === 'bulk-resume';
  return busy(bulk ? 'bulkResume' : 'resume' + el.dataset.id, async () => {
    const out = bulk
      ? await api('/api/jobs/bulk-resume', { method: 'POST', body: { ids: [...S.checked], ...body } })
      : await api(`/api/jobs/${el.dataset.id}/resume`, { method: 'PUT', body });
    if (bulk) S.checked.clear();
    if (S.jobOpen) D.jobModal = await api(`/api/jobs/${S.jobOpen}`).catch(() => D.jobModal);
    await load();
    if (out.ranking_queued.length) watchRanking(out.ranking_queued);
    return out;
  }, out => `Resume set for ${out.changed.length} job${out.changed.length === 1 ? '' : 's'}${out.ranking_queued.length ? ` · ${out.ranking_queued.length} queued for ranking again` : ''}`);
}

// Job description recovery (app.services.jd_recovery): a posting that arrived without a usable description is read
// from its URL in the background. This strip says where that stands, in plain words, and offers Recover / Retry.
const RECOVERY_TONE = { PENDING: 'brand', PROCESSING: 'brand', COMPLETED: 'good', BLOCKED: 'warn', FAILED: 'bad', SKIPPED: 'bad' };
const RECOVERY_VIA = { HTTP: 'the posting page', PLAYWRIGHT: 'the posting page (browser)', LLM_ASSISTED: 'the posting page (AI-assisted)' };
const recoveryActive = r => !!r && ['PENDING', 'PROCESSING'].includes(r.state);
function recoveryStrip(j) {
  const r = j.description_recovery;
  if (!r) return '';
  const active = recoveryActive(r), ended = ['BLOCKED', 'FAILED', 'SKIPPED'].includes(r.state);
  const recovered = r.description_source === 'RECOVERED';
  if (r.usable && !active && !recovered && !ended) return '';
  const action = r.can_recover && (!r.usable || ended)
    ? btn(label('recover' + j.id, ended ? 'Retry' : 'Recover Description', 'Queueing…'), 'recover-description', { id: j.id }, 'sm soft', isBusy('recover' + j.id)) : '';
  const hint = !r.has_url ? 'This job has no posting URL — paste the description instead.'
    : active ? 'Reading the posting in the background — you can close this and come back.'
    : r.state === 'BLOCKED' ? 'The site needs a person: open the posting and paste the description.'
    : ended ? (r.task?.message || '') : '';
  const via = recovered ? `<span class="xs muted">Recovered from ${esc(RECOVERY_VIA[r.fetch_method] || 'the posting')}${r.fetched_at ? ` · ${esc(fmtDateTime(r.fetched_at))}` : ''}${r.has_original ? ' · the board\'s original text is kept' : ''}</span>` : '';
  return `<div class="banner ${r.usable ? '' : 'warn'}" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap" data-recovery="${attr(r.state)}">
    ${r.usable ? '' : '<b>Job description unavailable.</b>'}${r.label && !(recovered && r.state === 'COMPLETED') ? pill(r.label, RECOVERY_TONE[r.state] || 'muted') : ''}
    ${hint ? `<span class="xs muted">${esc(hint)}</span>` : ''}${via}<span class="grow"></span>${action}</div>`;
}
async function watchRecovery(id) {
  for (let i = 0; i < 40; i++) {
    await new Promise(r => setTimeout(r, 3000));
    const out = await api(`/api/jobs/${id}/description-recovery`).catch(() => null);
    if (!out || !recoveryActive(out)) break;
    if (D.jobModal?.id === Number(id)) { D.jobModal = { ...D.jobModal, description_recovery: out }; render(); }
  }
  if (S.jobOpen) D.jobModal = await api(`/api/jobs/${S.jobOpen}`).catch(() => D.jobModal);
  await load();
}

// Ranking needs the employer's words. With none (or while editing), the Description tab offers to read them from
// the posting URL or to paste them; the text is saved only when the user says so.
function renderDescriptionEditor(j) {
  const fetched = S.descFetched?.[j.id];
  return `<div class="col gap10" style="padding:20px 22px">
    ${recoveryStrip(j)}
    ${j.needs_description ? `<div class="banner warn" style="display:block"><b>No job description yet.</b> ${j.fit == null ? 'Ranking waits until there is one' : 'This fit score was made without one — saving a description ranks the job again'}.</div>` : ''}
    <div class="row gap8 wrap">${j.url ? btn(label('descfetch' + j.id, 'Fetch from posting', 'Reading the posting…'), 'fetch-job-description', { id: j.id }, 'sm soft', isBusy('descfetch' + j.id)) : ''}
      ${j.url ? `<a class="btn sm" href="${attr(j.url)}" target="_blank" rel="noopener">Open posting to copy ↗</a>` : ''}</div>
    ${fetched === 'page_text' ? '<div class="xs muted">This page has no structured job data, so this is all of its text. Trim menus, cookie notices and other jobs before saving.</div>' : fetched === 'posting' ? '<div class="xs muted">Read from the posting. Check it before saving.</div>' : ''}
    ${textareaF('jobdesc', j.id, j.description, 'placeholder="Paste the full job description — responsibilities, requirements, nice-to-haves…" style="min-height:220px"')}
    <div class="row gap8">${btn(label('descsave' + j.id, j.fit == null ? 'Save and rank' : 'Save and re-rank', 'Saving…'), 'save-job-description', { id: j.id }, 'primary sm', isBusy('descsave' + j.id))}
      ${j.needs_description ? '' : btn('Cancel', 'cancel-job-description', { id: j.id }, 'sm')}</div></div>`;
}

function renderJobModal() {
  const j = D.jobModal;
  if (!j || j.id !== S.jobOpen) return `<div class="overlay" data-act="overlay-close"><div class="modal" style="padding:40px">${loading()}</div></div>`;
  const hereIdx = j.wizard.findIndex(w => !w.done);
  const actionable = ['discovered', 'shortlisted', 'maybe', 'archived', 'dismissed'].includes(j.status);
  let tab = '';
  if (S.jobTab === 'description' && (j.needs_description || S.descEdit === j.id)) tab = renderDescriptionEditor(j);
  else if (S.jobTab === 'description') tab = `<div class="desc" style="font-size:13.5px;line-height:1.7;padding:20px 22px">${recoveryStrip(j) ? `<div class="mb12">${recoveryStrip(j)}</div>` : ''}${paragraphs(j.description, [...j.matches, ...j.keywords], [...j.gaps, ...j.missing])}
      <div class="row gap12 wrap xs faint" style="margin-top:20px;padding-top:16px;border-top:1px solid var(--line2)"><span class="row gap6"><span style="width:9px;height:9px;border-radius:3px;background:#9fe0d2"></span>matched to your evidence</span><span class="row gap6"><span style="width:9px;height:9px;border-radius:3px;background:#ffd79a"></span>gap — needs a real answer</span><span>source: ${esc(j.board)}${j.search_run_id ? ` · pulled in run #${j.search_run_id}` : ''}</span>${descBadge(j.description_state, true)}${btn('Edit description', 'edit-job-description', { id: j.id }, 'sm ghost')}</div></div>`;
  else if (S.jobTab === 'requirements') tab = `<div style="padding:20px 22px" class="col gap14">${fitSummary(j)}${j.fit_analysis.must_have_missing?.length ? `<div class="box bad"><div class="xs b mb4" style="color:var(--bad)">MUST-HAVES MISSING</div><div class="small">${esc(j.fit_analysis.must_have_missing.join(' · '))}</div></div>` : ''}<div><div class="caps mb8">KEYWORDS</div><div class="row wrap gap6">${j.keywords.map(k => pill(k, 'plain', 'lg')).join('') || '<span class="small muted">Rank this job to extract keywords.</span>'}</div></div></div>`;
  else if (S.jobTab === 'structured') tab = renderJobStructured(j);
  else if (S.jobTab === 'insights') tab = renderJobInsights(j.id);
  else tab = `<div style="padding:20px 22px">${textareaF('jobnotes', j.id, j.notes, 'placeholder="Anything worth remembering about this role…" style="min-height:160px"')}${btn(label('jobnotes', 'Save notes', 'Saving…'), 'save-job-notes', { id: j.id }, 'primary sm mt10', isBusy('jobnotes'))}</div>`;
  return `<div class="overlay" data-act="overlay-close"><div class="modal" role="dialog" aria-label="Job detail" data-scroll-key="job-modal-${j.id}">
    <div class="modal-head"><div class="row top-align gap16 wrap"><div class="avatar lg">${esc(initials(j.company))}</div>
      <div style="flex:1 1 240px;min-width:0"><div style="font-size:21px;font-weight:600;letter-spacing:-.02em">${esc(j.title)}</div><div class="muted mt4" style="font-size:13.5px">${esc(j.company)}${j.location ? ' · ' + esc(j.location) : ''} · ${esc(j.board)} · ${postedTxt(j) ? `<span class="ink2">${esc(postedTxt(j))}</span>` : '<span class="faint" title="The job board did not give a posting date">posting date not listed</span>'} · found ${esc(j.posted)}</div></div>
      <div class="row gap8 wrap"><div class="scorebox sm"><div class="n">${fitTxt(j.fit)}</div><div class="l">FIT</div></div>
        ${j.url ? `<a class="btn" href="${attr(j.url)}" target="_blank" rel="noopener">Open posting ↗</a>` : ''}
        ${['approved', 'applied'].includes(j.status) ? btn('Open apply kit', 'modal-kit', { id: j.id }, 'primary')
          : j.resume_id ? btn('Open packet', 'modal-tailor', { id: j.id }, 'primary')
          : j.needs_description ? `<span title="Ranking and tailoring need the job description">${btn('Add description', 'edit-job-description', { id: j.id }, 'primary')}</span>`
          : needsParse(j) ? parseBtn(j, 'primary')
          : j.fit == null ? `<span title="Tailoring works from the ranking: rank this job first">${btn(label('rank' + j.id, 'Rank first', 'Queueing…'), 'rank-job', { id: j.id }, 'primary', isBusy('rank' + j.id))}</span>`
          : btn('Tailor resume', 'modal-tailor', { id: j.id }, 'primary')}${btn('Close', 'close-job')}</div></div>
      <div class="wizard">${j.wizard.map((w, i) => `<div class="${w.done ? 'done' : i === hereIdx ? 'here' : ''}"><div class="row gap6"><span class="m">${w.done ? '✓' : i + 1}</span><span class="small b" style="color:${w.done ? 'var(--ink)' : i === hereIdx ? 'var(--brand-ink)' : 'var(--faint)'}">${w.label}</span></div><div class="xs muted mt4">${esc(i === hereIdx && !w.done ? 'next step' : w.meta)}</div></div>`).join('')}</div></div>
    ${rejectionBanner(j)}
    ${pipelineBanner(j)}
    ${j.domain_match === 'NOT_MATCHING' ? `<div class="banner warn" style="margin:0 24px;display:flex;gap:8px;align-items:center;flex-wrap:wrap"><span class="grow"><b>Hidden: not matching.</b> ${esc(j.domain_reason || 'Outside your domain')} — not ranked further.</span>${btn(label('keep' + j.id, 'Keep this job', 'Keeping…'), 'domain-keep', { id: j.id }, 'sm soft', isBusy('keep' + j.id))}</div>` : domainCheckNote(j)}
    <div class="split gap16" style="padding:18px 24px 24px">
      <section class="card flush" style="flex:1 1 420px;min-width:0"><div class="tabs under">${[['description', 'Description'], ['structured', 'Structured JD'], ['requirements', 'Requirements map'], ['insights', 'Insights'], ['notes', 'Notes']].map(([id, t]) => `<button class="${S.jobTab === id ? 'on' : ''}" data-act="job-tab" data-id="${id}">${t}</button>`).join('')}</div>${tab}</section>
      <div class="col gap14" style="flex:1 1 280px;min-width:0">
        <section class="card pad18"><div class="row gap8 mb14">${pill('AI', 'brand')}<span class="card-title sm">Fit breakdown</span></div>
          ${D.resumeChoices?.length ? `<div class="mb12"><div class="row gap8" style="align-items:center;min-width:0"><span class="xs muted" style="white-space:nowrap">Resume used</span>${resumeSelect('job-resume', j.resume_choice_id, { id: j.id, choice: j.resume_choice })}</div>${j.resume_in_use ? resumeInUseNote(j.resume_in_use, '', j.id) : j.resume_choice === 'AUTO' ? '<div class="xs faint mt4">Matched by resume tags</div>' : ''}${j.packet_source?.differs || (j.packet_source && !j.resume_in_use) ? packetSourceNote(j.packet_source, j.resume_in_use) : ''}</div>` : ''}
          ${j.fit == null && j.needs_description ? '<div class="small muted">Not ranked: add the job description first — a score from a title alone would mean nothing.</div>'
          : j.fit == null && needsParse(j) ? `<div class="small muted mb12">Not ranked yet: ranking waits for the description to be parsed.</div>${parseBtn(j, 'soft')}`
          : j.fit == null ? `<div class="small muted mb12">Not ranked yet.</div>${btn(label('rank' + j.id, 'Rank this job', 'Queueing…'), 'rank-job', { id: j.id }, 'soft', isBusy('rank' + j.id))}` : `
          <div class="row base gap8 mb14"><span class="mono bb" style="font-size:34px;color:var(--brand-ink);line-height:1">${fitTxt(j.fit)}</span><span class="small muted">${j.rank_pct != null ? `top ${j.rank_pct}% of your ranked jobs` : ''}</span></div>
          <div class="col gap10"><div><div class="row between small mb4"><span class="muted">Strong matches</span><span class="mono b">${j.matches.length}</span></div>${bar(Math.min(100, j.matches.length * 15) + '%', '#5b4ae8')}</div>
            <div><div class="row between small mb4"><span class="muted">Gaps flagged</span><span class="mono b">${j.gaps.length + j.missing.length}</span></div>${bar(Math.min(100, (j.gaps.length + j.missing.length) * 20) + '%', '#e0a136')}</div>
            <div><div class="row between small mb4"><span class="muted">Model confidence</span><span class="mono b">${Math.round((j.fit_analysis.confidence || 0) * 100)}</span></div>${bar(Math.round((j.fit_analysis.confidence || 0) * 100) + '%', '#a79bf0')}</div></div>
          ${j.summary ? `<div class="small ink2 mt12 pretty">${esc(j.summary)}</div>` : ''}`}</section>
        <section class="card pad18"><div class="card-title sm mb12">Close the gaps</div><div class="col gap10">${j.gap_notes.length ? j.gap_notes.map(g => g.partial
          ? `<div class="box"><div class="small b">${esc(g.gap)}</div><div class="small muted mt4" style="line-height:1.5">Partial: ${esc(g.partial)}</div></div>`
          : `<div class="box warn"><div class="small b" style="color:var(--warn-deep)">${esc(g.gap)}</div><div class="small mt4" style="color:var(--warn-deep);line-height:1.5">No verified evidence. Add a claim or leave it out — tailoring never invents experience.</div>${btn('Add evidence', 'add-evidence-for', { gap: g.gap }, 'sm mt8')}</div>`).join('') : '<div class="small muted">No gaps flagged.</div>'}</div></section>
        <section class="card pad18"><div class="card-title sm mb10">Move this job</div><div class="row gap6 wrap">${actionable
          ? `${btn('Shortlist', 'modal-triage', { id: j.id, action: 'shortlist' }, 'sm soft')}${btn('Maybe later', 'modal-triage', { id: j.id, action: 'maybe' }, 'sm')}${['archived', 'dismissed'].includes(j.status) ? `<span title="Back to Discovery as a new job">${btn('Restore', 'modal-triage', { id: j.id, action: 'restore' }, 'sm')}</span>` : `<span title="Put it away without dismissing it: it leaves the queue and is not ranked, and the domain filter learns nothing from it">${btn('Archive', 'modal-triage', { id: j.id, action: 'archive' }, 'sm')}</span>`}${j.status === 'dismissed' ? '' : btn('Dismiss', 'modal-triage', { id: j.id, action: 'dismiss' }, 'sm danger')}`
          : `<span class="small muted">This job is ${esc(statusText(j.status))} — manage it from ${j.status === 'applied' ? 'the Tracker' : ['tailored', 'needs_retailor'].includes(j.status) ? 'Tailor' : 'Approvals'}.</span>`}</div></section>
      </div></div></div></div>`;
}
function renderPalette() {
  const q = S.paletteQuery;
  const item = (key, text, meta, act, data = {}) => `<button class="pal-item" data-act="${act}" ${Object.entries(data).map(([k, v]) => `data-${k}="${attr(v)}"`).join(' ')}><span class="k">${key}</span><span class="grow ellipsis">${esc(text)}</span><span class="xs faint none">${esc(meta)}</span></button>`;
  const c = D.summary?.counts || {};
  return `<div class="overlay" data-act="overlay-close"><div class="palette" role="dialog" aria-label="Command palette">
    <div class="row gap10" style="padding:15px 18px;border-bottom:1px solid var(--line2)"><span class="mono xs faint">⌘K</span><input class="input bare grow" style="font-size:15px" id="palette-q" data-palette="1" value="${attr(q)}" placeholder="Jump to a job, company or action…" autocomplete="off">${btn('esc', 'close-palette', {}, 'sm')}</div>
    <div style="padding:10px 0 12px;max-height:55vh;overflow:auto">
      ${q ? `<div class="row between" style="padding:8px 18px"><span class="caps-sm faint">JOBS</span><span class="tiny faint">${S.paletteTotal ?? 0} match</span></div>${S.paletteJobs.map(j => item('↳', `${j.company} — ${j.title}`, `fit ${fitTxt(j.fit)} · ${j.location || j.board}`, 'palette-job', { id: j.id })).join('') || '<div class="small muted" style="padding:6px 18px">No jobs match.</div>'}` : ''}
      <div class="caps-sm faint" style="padding:8px 18px">JUMP TO</div>
      ${item('O', 'Overview', 'pipeline health', 'palette-go', { screen: 'overview' })}${item('T', 'Today · run sheet', `${c.today ?? 0} actions`, 'palette-go', { screen: 'today', variant: 'run' })}${item('D', 'Discovery · triage queue', `${c.discovery ?? 0} new`, 'palette-go', { screen: 'discovery', mode: 'triage' })}${item('A', 'Approvals', `${c.approvals ?? 0} waiting`, 'palette-go', { screen: 'approvals' })}${item('R', 'Tracker', `${c.tracker ?? 0} live`, 'palette-go', { screen: 'tracker' })}
      <div class="caps-sm faint" style="padding:14px 18px 8px;border-top:1px solid var(--line3);margin-top:6px">ACTIONS</div>
      ${item('N', 'Run the saved search now', 'the setup you last opened', 'palette-run')}${item('K', 'Open the apply kit for the latest approved packet', '', 'palette-kit')}${item('F', 'Work the follow-ups that are due', `${D.today?.followups?.length ?? ''}`, 'palette-go', { screen: 'today' })}${item('+', 'Add a job from a URL', 'manual', 'palette-go', { screen: 'discovery', mode: 'setup' })}
    </div><div class="xs muted" style="padding:10px 18px;border-top:1px solid var(--line2);background:#fbfbfe">Search every job in the pipeline, jump between screens, or fire an action without leaving the keyboard.</div></div></div>`;
}

/* ───────────── render ───────────── */
const RENDER = { overview: renderOverview, today: renderToday, discovery: renderDiscovery, tailor: renderTailor, approvals: renderApprovals, tracker: renderTracker, orchestration: renderOrchestration, workers: renderWorkers, monitoring: renderMonitoring, knowledge: renderKnowledge, settings: renderSettings };
// Re-rendering replaces the markup, so every scrolled box (a modal, a list, a textarea) would snap back to the top
// on each refresh, and the field being typed in would lose its focus and caret; polls re-render every few seconds.
// render() keeps them: each scrolled element, and the focused one, is found again by where it sits under its nearest
// data-scroll-key or id (else under the render root), counting same-tag siblings. Give a box a data-scroll-key when
// what renders before it comes and goes. A box located only through a render root is restored only while the
// screen (the hash) is the same, so a new screen never inherits another one's scroll.
const RENDER_ROOTS = ['nav', 'side-foot', 'top', 'view', 'overlays'];
// A field whose text an action has taken (Enter on data-enter) is submitted, not being typed: never put it back.
const consumedFields = new WeakSet();
function domPath(el) {
  const steps = [];
  for (let n = el; n && n !== document.body; n = n.parentElement) {
    if (n.dataset?.scrollKey) return { anchor: { key: n.dataset.scrollKey }, steps: steps.reverse() };
    if (n.id) return { anchor: { id: n.id }, steps: steps.reverse(), loose: RENDER_ROOTS.includes(n.id) };
    let i = 0;
    for (let s = n.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === n.tagName) i++;
    steps.push([n.tagName, i]);
  }
  return null;
}
function findPath(path, keyed) {
  let n = path.anchor.key != null ? keyed.get(path.anchor.key) : document.getElementById(path.anchor.id);
  for (const [tag, i] of path.steps) {
    if (!n) return null;
    n = [...n.children].filter(c => c.tagName === tag)[i];
  }
  return n || null;
}
function keptView() {
  const scrolled = [];
  for (const id of RENDER_ROOTS) {
    for (const el of document.getElementById(id)?.querySelectorAll('*') || []) {
      if (!el.scrollTop && !el.scrollLeft) continue;
      const path = domPath(el);
      if (path) scrolled.push([path, el.scrollTop, el.scrollLeft]);
    }
  }
  const active = document.activeElement, field = {};
  if (active && active !== document.body && RENDER_ROOTS.some(id => document.getElementById(id)?.contains(active))) {
    field.path = domPath(active);
    if ('defaultValue' in active && active.value !== active.defaultValue && active.type !== 'file' && !consumedFields.has(active)) field.value = [active.defaultValue, active.value];
    try { if (typeof active.selectionStart === 'number') field.range = [active.selectionStart, active.selectionEnd, active.selectionDirection]; } catch { /* not a text field */ }
  }
  return { scrolled, field, hash: location.hash, page: [window.scrollX, window.scrollY] };
}
function restoreView(kept) {
  const keyed = new Map([...document.querySelectorAll('[data-scroll-key]')].map(el => [el.dataset.scrollKey, el]));
  const sameScreen = kept.hash === location.hash;
  const find = path => (path && (sameScreen || !path.loose) ? findPath(path, keyed) : null);
  const el = find(kept.field.path);
  // A button found by position may now be another job's: only an element with its own id, or a field, gets focus back.
  if (el && (!kept.field.path.steps.length || ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName))) {
    // Typing the poll has not seen yet stays, unless the render brought a different value for this field.
    const [was, typed] = kept.field.value || [];
    if (kept.field.value && el.defaultValue === was && el.value === el.defaultValue) el.value = typed;
    el.focus({ preventScroll: true });
    if (kept.field.range) { try { el.setSelectionRange(...kept.field.range); } catch { /* ignore */ } }
  }
  for (const [path, top, left] of kept.scrolled) {
    const box = find(path);
    if (box) { box.scrollTop = top; box.scrollLeft = left; }
  }
  if (sameScreen && (window.scrollX !== kept.page[0] || window.scrollY !== kept.page[1])) window.scrollTo?.(...kept.page);
}
function render() {
  const kept = keptView();
  $('#nav').innerHTML = renderNav();
  $('#side-foot').innerHTML = renderSideFoot();
  if (D.summary) $('#side-meta').textContent = `${D.summary.model || D.summary.llm_provider || ''} · ${(D.summary.application_mode || '').replace('_', ' ')}`;
  if (D.summary?.profile) $('#brand-sub').textContent = [D.summary.profile.name, D.summary.profile.headline].filter(Boolean).join(' · ') || 'Set up your profile';
  $('#top').innerHTML = renderTop();
  $('#view').innerHTML = RENDER[S.screen]();
  $('#overlays').innerHTML = (S.jobOpen ? renderJobModal() : '') + (S.kitJob ? renderKit() : '') + (S.paletteOpen ? renderPalette() : '') + (S.mon?.request ? renderMonRequestModal() : '')
    + (S.boardReview ? renderBoardReview() : '') + (S.mon?.ext?.request ? renderMonExtRequestModal() : '') + (S.mon?.logs?.entry ? renderMonLogModal() : '');
  document.body.style.overflow = (S.jobOpen || S.kitJob || S.paletteOpen || S.mon?.request || S.boardReview) ? 'hidden' : '';
  restoreView(kept);
}

/* ───────────── background tailoring ───────────── */
let taskTimer = null;
async function refreshTasks() {
  const before = new Map((D.tasks || []).map(t => [t.id, t.status]));
  D.tasks = await api('/api/tailor-tasks').catch(() => D.tasks || []);
  const finished = D.tasks.filter(t => ['queued', 'running'].includes(before.get(t.id)) && !['queued', 'running'].includes(t.status));
  if (finished.length) {
    const ok = finished.filter(t => t.status === 'succeeded'), bad = finished.filter(t => t.status === 'failed');
    const one = finished.length === 1 ? finished[0] : null;
    toast(one ? (one.status === 'succeeded' ? `${one.company}: ${one.mode === 'review' ? 'reviewed' : 'packet ready'} · reviewer ${fitTxt(one.result?.reviewer_score)}${one.result?.auto_approved ? ' · auto-approved' : ''}` : one.status === 'failed' ? `${one.company}: ${one.error}` : `${one.company}: cancelled`)
      : `${ok.length} packet${ok.length === 1 ? '' : 's'} ready${bad.length ? ` · ${bad.length} failed` : ''}`, !ok.length && !!bad.length);
  }
  if (finished.some(t => t.result?.auto_approved)) refreshApprovals();
  scheduleTaskPoll();
  return { finished };
}
// Poll while anything is queued or running, on every screen, so finished jobs are announced wherever you are.
function scheduleTaskPoll() {
  clearTimeout(taskTimer); taskTimer = null;
  if (!(D.tasks || []).some(t => t.status === 'queued' || t.status === 'running')) return;
  taskTimer = setTimeout(async () => {
    const { finished } = await refreshTasks();
    if (S.screen !== 'tailor' && S.screen !== 'approvals') return;
    if (finished.length) await load(); else render();
  }, 2500);
}
// The resume length for the next tailoring run (app.services.resume_length); '' uses the default in Settings.
function tailorPagesPicker() {
  const options = [['', 'Default length'], ['1', '1 page'], ['2', '2 pages'], ['3', '3 pages (not recommended)']];
  return `<select class="select sm" data-act-change="tailor-pages" aria-label="Tailored resume length" title="How long the tailored resume is: your latest roles get the most bullets">${options.map(([v, t]) => `<option value="${v}" ${S.tailorPages === v ? 'selected' : ''}>${t}</option>`).join('')}</select>`;
}
function packetLength(r) {
  const l = r.length || {};
  if (r.untailored || !l.estimated_pages) return '';
  const over = l.target_pages && l.estimated_pages > l.target_pages + 0.15;
  return ` · ${l.target_pages ? `target ${l.target_pages} page${l.target_pages === 1 ? '' : 's'} · ` : ''}<span data-packet-length${over ? ' style="color:var(--warn)"' : ''}>≈${l.estimated_pages} page${l.estimated_pages === 1 ? '' : 's'}</span>${over ? ' · over target: accept fewer bullets or regenerate' : ''}`;
}
// `extra` adds to the request: { revise: true } makes each run a revision pass that fixes the packet's last review.
function queueTailoring(ids, mode, regenerate, busyKey, onQueued, extra = {}) {
  if (!ids.length) return;
  const pages = mode === 'tailor' && S.tailorPages ? { pages: Number(S.tailorPages) } : {};
  return busy(busyKey, async () => {
    const out = await api('/api/tailor-tasks', { method: 'POST', body: { ids, mode, regenerate, ...pages, ...extra } });
    await onQueued?.(out);
    await refreshTasks();
    return out;
  }, out => {
    const n = out.queued.length;
    const verb = mode === 'skip' ? 'Skipping tailoring for' : mode === 'review' ? 'Reviewing' : extra.revise ? 'Retailoring' : 'Tailoring';
    if (!n) return `Nothing queued: ${out.skipped[0]?.reason || 'no eligible jobs'}`;
    return `${verb} ${n} job${n === 1 ? '' : 's'} in the background${out.skipped.length ? ` · ${out.skipped.length} skipped` : ''}`;
  });
}

/* ───────────── actions ───────────── */
async function openJob(id) {
  S.jobOpen = Number(id); S.jobTab = 'description'; render();
  try {
    [D.jobModal] = await Promise.all([api(`/api/jobs/${id}`), loadResumeChoices()]);
    if (recoveryActive(D.jobModal.description_recovery)) watchRecovery(id);
  } catch (e) { toast(e.message, true); S.jobOpen = null; }
  render();
}
async function openKit(id) {
  S.kitJob = Number(id); D.kit = null; render();
  try { D.kit = await api(`/api/jobs/${id}/apply-kit`); } catch (e) { toast(e.message, true); S.kitJob = null; }
  render();
}
async function triageJob(id, action, thenTailor) {
  try {
    await api(`/api/jobs/${id}/triage`, { method: 'POST', body: { action } });
    if (S.screen === 'discovery' && S.discMode === 'triage') S.triagedThisSession++;
    if (thenTailor) return go('tailor', { tailorJob: Number(id), tailorTab: 'diff' });
    toast({ shortlist: 'Shortlisted', maybe: 'Parked for later', skip: 'Skipped', dismiss: 'Dismissed', archive: 'Archived', restore: 'Restored to Discovery' }[action]);
    await load();
  } catch (e) { toast(e.message, true); }
}
async function pollRuns() {
  const p = S.polling; if (!p) return;
  try {
    const runs = await Promise.all(p.ids.map(id => api(`/api/search/apify/runs/${id}`)));
    const done = runs.filter(r => r.completed).length;
    const created = runs.reduce((a, r) => a + (r.created_jobs || 0), 0);
    p.text = `${done} of ${runs.length} searches complete · ${created} new jobs so far`;
    if (done < runs.length) { render(); setTimeout(pollRuns, 4000); return; }
    const failed = runs.filter(r => r.status !== 'SUCCEEDED');
    S.polling = null; D.runs = null;
    toast(failed.length ? `${failed.length} search${failed.length === 1 ? '' : 'es'} failed. Completed results were kept.` : `Discovery complete · ${created} new jobs`, !!failed.length);
    go('discovery', { discMode: 'triage', triagedThisSession: 0 });
  } catch (e) { toast(`Could not refresh search status: ${e.message}`, true); S.polling = null; render(); }
}
const FANOUT = { role_location: 'one run per role × location', location: 'one run per location', role: 'one run per role', once: 'one run for everything' };
// What each `supports` key of an actor means on screen, so a card can say which of the settings it reads.
const ACTOR_USES = { boards: 'Boards', location: 'Location', distance: 'Radius', country: 'Country', remote: 'Remote only', job_type: 'Job type', hours: 'Posted within', easy_apply: 'Easy Apply', fetch_description: 'Full descriptions', annual_salary: 'Annual salary', linkedin_company_ids: 'Company IDs', seniority: 'Seniority', salary_only: 'Salary listed', visa_sponsorship: 'Visa sponsorship', exclude_keywords: 'Exclude', skip_agencies: 'Skip agencies', remote_platforms: 'Boards', remoteok_url: 'Start page', yc_batches: 'YC batches', startup_details: 'Company details', hn_month: 'Thread month', min_salary: 'Min salary', skills: 'Primary skills' };
const STARTUP_ACTORS = { waas: 'antishock/workatastartup-jobs-scraper', yc: 'automation-lab/ycombinator-jobs-scraper', hn: 'parseforge/hn-whoishiring-scraper', wellfound: 'blackfalcondata/wellfound-scraper' };
// A user-added actor reads whichever filters its input template names.
const TEMPLATE_USES = { skill: 'skills', skills: 'skills', hours_old: 'hours', days_old: 'hours', is_remote: 'remote', job_type: 'job_type', location: 'location', locations: 'location', sites: 'boards', country: 'country',
  seniority: 'seniority', distance: 'distance', easy_apply: 'easy_apply', fetch_description: 'fetch_description', enforce_annual_salary: 'annual_salary', salary_only: 'salary_only',
  visa_sponsorship: 'visa_sponsorship', exclude_keywords: 'exclude_keywords', linkedin_company_ids: 'linkedin_company_ids', skip_agencies: 'skip_agencies', min_salary: 'min_salary',
  yc_batches: 'yc_batches', hn_month: 'hn_month', startup_details: 'startup_details', remote_platforms: 'remote_platforms', remoteok_url: 'remoteok_url' };
function actorUses(a) {
  if (!a.custom) return a.supports || [];
  const text = JSON.stringify(a.input_template || {});
  return [...new Set(Object.entries(TEMPLATE_USES).filter(([k]) => text.includes(`{{${k}}}`)).map(([, v]) => v))];
}
const domId = value => String(value).replace(/[^\w-]/g, '-');
function actorCard(a, on) {
  const id = `actor-${domId(a.id)}`, uses = [...new Set(actorUses(a).map(u => ACTOR_USES[u]).filter(Boolean))];
  return `<label class="board-opt actor-opt ${on ? 'on' : ''}" for="${id}"><input type="checkbox" id="${id}" data-act="toggle-actor" data-actor="${attr(a.id)}" aria-describedby="${id}-about" ${on ? 'checked' : ''}>
    <span class="grow" style="min-width:0"><span class="small b" style="display:block">${esc(a.name)}</span>
      <span class="xs muted" style="display:block" id="${id}-about">${esc(a.summary)} <span class="sr-only">Cost ${esc(a.cost)}, ${esc(FANOUT[a.fan_out] || a.fan_out)}.</span></span>
      <span class="tiny faint" style="display:block;margin-top:4px" aria-hidden="true"><span class="mono">${esc(a.id)}</span> · ${esc(a.cost)} · ${esc(FANOUT[a.fan_out] || a.fan_out)}</span>
      ${uses.length ? `<span class="actor-uses" aria-label="Settings it uses: ${attr(uses.join(', '))}">${uses.map(u => `<span>${esc(u)}</span>`).join('')}</span>` : ''}</span>
    ${a.custom ? `<span class="row gap6"><button type="button" class="btn ghost sm" data-act="edit-actor" data-actor="${attr(a.id)}" aria-label="Edit ${attr(a.name)}">Edit</button><button type="button" class="btn ghost sm danger none" data-act="remove-actor" data-actor="${attr(a.id)}" aria-label="Remove ${attr(a.name)}">Remove</button></span>` : ''}</label>`;
}
function actorPicker(list, on) {
  const groups = [...new Set([...(D.actors?.groups || []), ...list.map(a => a.group)])];
  return groups.map(g => {
    const items = list.filter(a => a.group === g); if (!items.length) return '';
    const count = items.filter(a => on(a.id)).length;
    return `<fieldset class="actor-group"><legend><span class="caps">${esc(g.toUpperCase())}</span>${count ? `<span class="tiny faint"> · ${count} selected</span>` : ''}</legend>
      <div class="grid-auto" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px">${items.map(a => actorCard(a, on(a.id))).join('')}</div></fieldset>`;
  }).join('');
}
// Which selected actors read a setting, shown beside it so a filter nobody uses is obvious.
function usedBy(list, on, key) {
  const names = list.filter(a => on(a.id) && actorUses(a).includes(key)).map(a => a.name);
  return names.length ? `Used by ${names.join(', ')}` : 'Not used by the selected actors';
}
function filterNote(list, on, key) {
  const names = list.filter(a => on(a.id) && actorUses(a).includes(key));
  return `<span class="helper filter-note ${names.length ? '' : 'unused'}" id="use-${key}">${esc(usedBy(list, on, key))}</span>`;
}
function usedCheck(list, on, key, field, value, text) {
  const used = list.some(a => on(a.id) && actorUses(a).includes(key));
  return `<span class="${used ? '' : 'filter-unused'}" title="${attr(usedBy(list, on, key))}">${checkF('sp', field, value, text + (used ? '' : ' <span class="tiny">(not used)</span>'))}</span>`;
}
const COUNTRIES = ['usa', 'uk', 'canada', 'australia', 'india', 'singapore', 'uae', 'germany', 'france', 'netherlands', 'ireland', 'spain', 'italy', 'japan', 'brazil', 'mexico', 'south africa', 'new zealand'];
// What a placeholder fills in, shown on its button; the setup's primary skills are the ones easiest to miss.
const PLACEHOLDER_HINTS = { role: 'One role', roles: 'Every role (list)', location: 'One location', locations: 'Every location (list)', skill: 'First primary skill', skills: 'Every primary skill (list)' };
const DEFAULT_TEMPLATE = JSON.stringify({ searchTerm: '{{role}}', location: '{{location}}', maxItems: '{{max_results}}' }, null, 2);
function addActorForm() {
  const editing = S.editActor, f = S.forms.ca || {};
  return `<div class="box brand mt10" style="padding:14px"><div class="small b mb4">${editing ? `Edit ${esc(f.name || editing)}` : 'Add an Apify actor'}</div>
    <div class="xs muted mb8">${editing ? 'Change how this actor is called: its name, how many runs it starts and the input it receives.' : 'Any Store actor that returns job listings. Items are normalized automatically — title, company, location, URL and description.'}</div>
    <div class="row gap8 wrap">${inputF('ca', 'id', '', `placeholder="username/actor-name" style="flex:1 1 220px;margin-top:0" ${editing ? 'readonly aria-readonly="true"' : ''}`)}${btn(label('lookup', editing ? 'Load input schema' : 'Look up on Apify', 'Looking up…'), 'lookup-actor', {}, 'sm', isBusy('lookup'))}</div>
    ${inputF('ca', 'name', '', 'placeholder="Display name"')}
    <label class="field mt8">Runs${selectF('ca', 'fan_out', 'role_location', Object.entries(FANOUT).map(([k, v]) => [k, v[0].toUpperCase() + v.slice(1)]))}</label>
    <label class="field mt8">Input template (JSON)${textareaF('ca', 'input_template', DEFAULT_TEMPLATE, 'style="font-family:var(--mono);font-size:11.5px;min-height:130px"')}</label>
    <div class="helper" role="group" aria-label="Placeholders">Placeholders — select a value in the template (or place the cursor) and click one to insert it: <span class="row gap6 wrap mt4">${(D.actors?.placeholders || []).map(x => `<button type="button" class="btn sm" data-act="insert-placeholder" data-name="${attr(x)}"${PLACEHOLDER_HINTS[x] ? ` title="${attr(PLACEHOLDER_HINTS[x])}"` : ''}><span class="mono">{{${x}}}</span>${x === 'skill' || x === 'skills' ? ` <span class="tiny faint">${esc(x === 'skill' ? 'primary skill' : 'primary skills')}</span>` : ''}</button>`).join('')}</span> A value that is only a placeholder keeps its type (number, list, true/false).</div>
    ${S.caFields ? `<div class="xs muted mt8" role="group" aria-label="Input schema">This actor accepts — click a field to add it with the matching placeholder:
      <div class="row gap6 wrap mt4">${S.caFields.map(x => { const fill = Array.isArray(x.suggested) ? `[${x.suggested.join(', ')}]` : x.suggested; return `<button type="button" class="btn sm" data-act="insert-actor-field" data-name="${attr(x.name)}" title="${attr([x.type, x.title, fill && `fills with ${fill}`].filter(Boolean).join(' · '))}"><span class="mono">${esc(x.name)}${x.required ? '*' : ''}</span>${fill ? ` <span class="tiny mono" style="color:var(--brand)">${esc(fill)}</span>` : x.type ? ` <span class="tiny faint">${esc(x.type)}</span>` : ''}</button>`; }).join('') || 'no documented inputs'}</div></div>` : ''}
    <div class="row gap8 mt10">${btn(editing ? 'Save changes' : 'Add actor', 'add-actor', {}, 'primary sm')}${editing ? btn('Cancel', 'toggle-add-actor', {}, 'ghost sm') : ''}</div></div>`;
}
const setupResumeValue = p => p.resume_mode === 'fixed' || (!p.resume_mode && p.resume_id) ? String(p.resume_id) : (p.resume_mode || 'auto');
const setupResume = value => ['auto', 'active'].includes(String(value)) ? { resume_mode: String(value), resume_id: null } : { resume_mode: 'fixed', resume_id: Number(value) };
function searchPayload() {
  const p = D.searchProfile.profile, F = 'sp', v = (k, d) => fv(F, k, p[k] ?? d);
  return {
    roles: v('roles', []), primary_skills: v('primary_skills', []), locations: v('locations', []), actors: v('actors', []), custom_actors: v('custom_actors', []),
    sites: v('sites', []), remote_platforms: v('remote_platforms', []), remoteok_url: v('remoteok_url', ''),
    hours_old: Number(v('hours_old', 168)) || 0, max_results: Number(v('max_results', 20)) || 20, offset: Number(v('offset', 0)) || 0,
    job_type: v('job_type', ''), seniority: v('seniority', []), country_indeed: v('country_indeed', 'usa'),
    distance: Number(v('distance', 50)) || 0, is_remote: !!v('is_remote', false), easy_apply: !!v('easy_apply', false),
    fetch_description: !!v('fetch_description', true), enforce_annual_salary: !!v('enforce_annual_salary', false),
    residential_proxy: !!v('residential_proxy', true), salary_only: !!v('salary_only', false), visa_sponsorship: !!v('visa_sponsorship', false),
    exclude_keywords: v('exclude_keywords', []), linkedin_company_ids: v('linkedin_company_ids', []),
    skip_agencies: !!v('skip_agencies', true), auto_rank: !!v('auto_rank', true), schedule: v('schedule', 'manual'),
    yc_batches: v('yc_batches', []), hn_month: String(v('hn_month', 'current') || 'current').trim(),
    min_salary: Math.max(0, Number(v('min_salary', 0)) || 0), startup_details: !!v('startup_details', true),
    ...setupResume(v('resume_select', setupResumeValue(p))),
  };
}
let previewTimer;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    if (!D.searchProfile) return;
    try { D.spPreview = await api('/api/search/profile/preview', { method: 'POST', body: searchPayload() }); }
    catch (e) { D.spPreview = { runs: [], warnings: [], error: e.message }; }
    if (S.screen === 'discovery' && S.discMode === 'setup') render();
  }, 350);
}
function toggleIn(key, value) {
  const list = [...(fv('sp', key, D.searchProfile.profile[key] || []) || [])];
  const i = list.indexOf(value); i >= 0 ? list.splice(i, 1) : list.push(value);
  setF('sp', key, list); render();
}
/* ───────────── saved setups ───────────── */
// Each setup is a whole search profile with a name and its own schedule. The selected one is what
// every field above edits, so switching setups is just swapping D.searchProfile.profile.
const curSetup = () => (D.setups || []).find(x => x.id === S.setupSel) || (D.setups || [])[0] || null;
const localZone = () => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch { return 'UTC'; } };
const DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const SCHEDULE_KINDS = [['manual', 'Manual only'], ['interval', 'Every N hours'], ['daily', 'Daily'], ['weekly', 'Weekly']];
const setupField = (key, fallback) => fv('sp', key, curSetup()?.[key] ?? fallback);

function useSetup(id) {
  if (id === S.setupSel) return;
  S.setupSel = id; delete S.forms.sp;
  const sel = curSetup();
  D.searchProfile.profile = sel ? sel.settings : {};
  schedulePreview(); render();
}
function setupPayload() {
  return {
    name: String(setupField('name', 'My search') || '').trim(),
    settings: searchPayload(),
    schedule_kind: setupField('schedule_kind', 'manual'),
    every_hours: Number(setupField('every_hours', 24)) || 24,
    time_of_day: setupField('time_of_day', '07:00'),
    weekdays: setupField('weekdays', []),
    timezone: setupField('timezone', localZone()),
    enabled: !!setupField('enabled', true),
    // Only what was done to your own actors in this form reaches the other setups (the server ignores the rest).
    changed_actors: fv('sp', 'changed_actors', []), removed_actors: fv('sp', 'removed_actors', []),
  };
}
// Text still sitting in a tag input counts as typed — save it rather than dropping it.
function flushTagDrafts() {
  for (const key of ['roles', 'primary_skills', 'locations']) {
    const pending = document.getElementById(`sp-${key}`)?.value.trim();
    if (pending) addTags('sp', key, pending, key === 'primary_skills' ? 3 : 5, D.searchProfile.profile[key]);
  }
}
// Actors a person adds are shared by every setup (the server keeps them in step), so the other setups follow too:
// they list the same actors, and one removed here is no longer ticked there.
function shareActors(other, setup, paused) {
  const customs = setup.settings.custom_actors || [], keep = new Set(customs.map(c => c.id));
  const gone = new Set((other.settings.custom_actors || []).map(c => c.id).filter(id => !keep.has(id)));
  const settings = { ...other.settings, custom_actors: customs };
  if (other.settings.actors) settings.actors = other.settings.actors.filter(a => !gone.has(a));
  return { ...other, settings, ...(paused.includes(other.name) ? { enabled: false } : {}) };
}
// A scheduled setup whose last ticked actor was removed has nothing to run, so the server paused its schedule.
// Said with the save's own message (one toast replaces another).
const withPaused = (message, setup) => setup?.paused?.length
  ? `${message} · schedule paused for ${setup.paused.map(n => `“${n}”`).join(', ')}: no actor is ticked there any more` : message;
function adoptSetup(setup, paused = []) {
  const others = (D.setups || []).map(x => x.id === setup.id ? setup : shareActors(x, setup, paused));
  D.setups = others.some(x => x.id === setup.id) ? others : [...others, setup];
  S.setupSel = setup.id; delete S.forms.sp;
  D.searchProfile.profile = setup.settings;
  if (paused.length) setup = { ...setup, paused };
  S.triage.hide_agencies = setup.settings.skip_agencies; S.triage.hide_reposts = setup.settings.skip_agencies;
  return setup;
}
async function saveSetup() {
  const setup = curSetup();
  if (!setup) return createSetup();
  flushTagDrafts();
  const out = await api(`/api/search/setups/${setup.id}`, { method: 'PUT', body: setupPayload() });
  // A changed resume: the jobs this setup found follow it (not the ones you pinned yourself, approved or applied).
  const moved = out.resume_jobs?.changed?.length || 0;
  if (moved) toast(`${moved} job${moved === 1 ? '' : 's'} from this setup now use${moved === 1 ? 's' : ''} its resume${out.resume_jobs.ranking_queued?.length ? ` · ${out.resume_jobs.ranking_queued.length} queued for ranking again` : ''}`);
  return adoptSetup(out.setup, out.paused_setups || []);
}
async function createSetup(body) {
  if (!body) flushTagDrafts();
  const out = await api('/api/search/setups', { method: 'POST', body: body || { ...setupPayload(), name: nextSetupName() } });
  return adoptSetup(out.setup, out.paused_setups || []);
}
function nextSetupName(preferred) {
  const taken = new Set((D.setups || []).map(x => x.name.toLowerCase()));
  const base = String(preferred || setupField('name', 'My search') || 'My search').trim() || 'My search';
  if (!taken.has(base.toLowerCase())) return base;
  for (let n = 2; ; n++) if (!taken.has(`${base} ${n}`.toLowerCase())) return `${base} ${n}`;
}
async function refreshAfter(msg) { if (msg) toast(msg); await load(); }

const A = {
  go: el => go(el.dataset.screen, {
    ...(el.dataset.mode ? { discMode: el.dataset.mode } : {}), ...(el.dataset.job ? { tailorJob: Number(el.dataset.job), tailorMode: 'single', tailorTab: 'diff' } : {}),
    ...(el.dataset.approval ? { approvalJob: Number(el.dataset.approval) } : {}), ...(el.dataset.app ? { selApp: Number(el.dataset.app), trackerView: 'board' } : {}),
    // The tracker's Inbox insights view, reloaded so it shows the emails the button counted.
    ...(el.dataset.view === 'inbox' ? (D.inbox = undefined, { trackerView: 'inbox', selApp: null }) : {}),
    jobOpen: null, kitJob: null, paletteOpen: false,
  }),
  variant: el => {
    if (el.dataset.kind === 'today') S.todayVariant = el.dataset.id;
    else if (el.dataset.kind === 'tailor') S.tailorMode = el.dataset.id;
    else { S.discMode = el.dataset.id; S.manualMissing = null; }
    go(S.screen);
  },
  'go-triage': () => go('discovery', { discMode: 'triage' }),
  'do-action': el => {
    const { kind, job, app } = el.dataset;
    if (kind === 'approve') go('approvals', { approvalJob: Number(job) });
    else if (kind === 'tailor') go('tailor', { tailorJob: Number(job) });
    else go('tracker', { selApp: Number(app), trackerView: 'board' });
  },
  'draft-nudge': el => busy('nudge' + el.dataset.app, async () => {
    await api(`/api/applications/${el.dataset.app}/followup-draft`, { method: 'POST' });
    if (el.dataset.stay) await load(); else go('tracker', { selApp: Number(el.dataset.app), trackerView: 'board' });
  }, 'Follow-up draft ready — review it before sending'),
  palette: () => { S.paletteOpen = true; S.paletteQuery = ''; S.paletteJobs = []; render(); setTimeout(() => document.getElementById('palette-q')?.focus(), 0); },
  'close-palette': () => { S.paletteOpen = false; render(); },
  'palette-go': el => { S.paletteOpen = false; go(el.dataset.screen, { ...(el.dataset.mode ? { discMode: el.dataset.mode } : {}), ...(el.dataset.variant ? { todayVariant: el.dataset.variant } : {}) }); },
  'palette-job': el => { S.paletteOpen = false; openJob(el.dataset.id); },
  'palette-run': () => { S.paletteOpen = false; go('discovery', { discMode: 'setup' }); setTimeout(() => A['run-search'](), 300); },
  'palette-kit': async () => {
    S.paletteOpen = false;
    const list = await api('/api/approvals').catch(() => []);
    const approved = list.find(a => a.status === 'approved');
    if (approved) openKit(approved.id); else { toast('No approved packet is waiting for submission', true); render(); }
  },
  'overlay-close': (el, e) => { if (e.target !== el) return; S.boardReview = null; S.jobOpen = null; S.kitJob = null; S.paletteOpen = false; render(); },
  'open-job': el => openJob(el.dataset.id),
  'close-job': () => { S.jobOpen = null; render(); },
  'job-tab': el => { S.jobTab = el.dataset.id; render(); if (S.jobTab === 'insights') insEnsure(S.jobOpen); },
  'modal-tailor': async el => {
    const id = Number(el.dataset.id), j = D.jobModal;
    if (!j.resume_id && ['discovered', 'shortlisted', 'maybe', 'archived', 'dismissed'].includes(j.status)) await api(`/api/jobs/${id}/triage`, { method: 'POST', body: { action: 'shortlist' } }).catch(e => toast(e.message, true));
    S.jobOpen = null;
    // An applied job still has a packet, and the button says "Open packet" — so open it (read-only), rather
    // than sending the reader back to the tracker they most likely came from.
    if (j.resume_id) go('tailor', { tailorJob: id, tailorMode: 'single', tailorTab: 'diff' });
    else if (j.status === 'approved') go('approvals', { approvalJob: id });
    else if (j.status === 'applied') go('tracker', { selApp: j.application_id });
    else go('tailor', { tailorJob: id });
  },
  // An approved or applied job's next step is its Apply kit: open it in place of the job, not over it.
  'modal-kit': el => { S.jobOpen = null; openKit(el.dataset.id); },
  'modal-triage': async el => { await triageJob(el.dataset.id, el.dataset.action); if (S.jobOpen) openJob(S.jobOpen); },
  'save-job-notes': el => busy('jobnotes', () => api(`/api/jobs/${el.dataset.id}/notes`, { method: 'PUT', body: { notes: fv('jobnotes', el.dataset.id, D.jobModal.notes) } }), 'Notes saved'),
  'add-evidence-for': el => {
    S.jobOpen = null;
    // Preserve an existing claim draft while incorporating the new job gap.
    const current = fv('ev', 'claim', S.evidencePrefill || '');
    S.evidencePrefill = current ? current + '\n' + el.dataset.gap + ': ' : el.dataset.gap + ': ';
    setF('ev', 'claim', S.evidencePrefill);
    knowledgeState().editors.evidence = { id: null, form: 'ev' };
    go('knowledge', { knowledgeSection: 'evidence' });
    document.getElementById('ev-claim')?.focus();
  },
  'tracker-reset-filters': () => { S.tf = { q: '', location: 'all', min_fit: 0 }; render(); },
  'set-gateway': el => {
    // The route takes effect on every worker's next request; there is no draft to review, so ask first.
    const g = D.gateway || (typeof AIM !== 'undefined' && AIM.data?.gateway) || {};
    const target = el.dataset.on ? 'the LiteLLM gateway' : `${g.direct_label || 'the direct provider'} directly`;
    if (!confirm(`Switch every AI worker to ${target}? It takes effect on their next request. You can switch back here at any time.`)) return;
    return busy('gateway', async () => {
    D.gateway = await api('/api/settings/ai/gateway', { method: 'PUT', body: { enabled: !!el.dataset.on } });
    api('/api/workspace/summary').then(sum => { D.summary = sum; render(); }).catch(() => {});
    if (S.screen === 'orchestration' && typeof aimLoad === 'function') await aimLoad();  // AI Orchestration → Models shows it too
    return D.gateway;
  }, g => g.gateway_enabled ? 'Workers now use the LiteLLM gateway' : `Workers now call ${g.direct_label} directly`);
  },
  'edit-job-description': el => { S.jobTab = 'description'; S.descEdit = Number(el.dataset.id); render(); },
  'cancel-job-description': el => {
    S.descEdit = null; if (S.forms.jobdesc) delete S.forms.jobdesc[el.dataset.id];
    if (S.descFetched) delete S.descFetched[el.dataset.id];
    render();
  },
  'fetch-job-description': el => busy('descfetch' + el.dataset.id, async () => {
    const out = await api(`/api/jobs/${el.dataset.id}/description/fetch`, { method: 'POST' });
    (S.forms.jobdesc ||= {})[el.dataset.id] = out.description;
    (S.descFetched ||= {})[el.dataset.id] = out.source;
    return out;
  }, 'Description read from the posting — check it, then save'),
  'save-job-description': el => busy('descsave' + el.dataset.id, async () => {
    const id = el.dataset.id;
    const out = await api(`/api/jobs/${id}/description`, { method: 'PUT', body: { description: fv('jobdesc', id, D.jobModal?.description || '') } });
    S.descEdit = null; delete S.forms.jobdesc?.[id]; delete S.descFetched?.[id];
    if (out.ranking_queued) watchRanking([Number(id)]);
    D.jobModal = await api(`/api/jobs/${id}`).catch(() => D.jobModal);
    return out;
  }, out => out.ranking_queued ? 'Description saved · queued for ranking' : `Description saved${out.ranking_skipped ? ` · not re-ranked: ${out.ranking_skipped}` : ''}`),
  'domain-learned': el => busy('domainLearned', async () => {
    D.domain = await api('/api/settings/domain/learned', { method: 'PUT', body: { term: el.dataset.term, kind: el.dataset.kind, action: el.dataset.action } });
    return el.dataset.action;
  }, action => ({ disable: 'Rule turned off', enable: 'Rule turned on', promote: 'Added to your permanent excludes', delete: 'Rule forgotten' }[action])),
  'domain-reapply': () => busy('domainReapply', () => api('/api/jobs/domain/reapply', { method: 'POST' }),
    out => `Checked ${out.checked} unranked job${out.checked === 1 ? '' : 's'} · ${out.hidden} hidden · ${out.restored} back in the queue`),
  'domain-recheck': () => busy('domainRecheck', () => api('/api/jobs/domain/recheck', { method: 'POST' }),
    out => `Checked ${out.checked} hidden job${out.checked === 1 ? '' : 's'} · ${out.queued} sent to the AI check · ${out.restored} back in the queue · ${out.still_hidden} still hidden`),
  'review-check': el => { const picked = S.reviewChecked || (S.reviewChecked = new Set()), id = Number(el.dataset.id); picked.has(id) ? picked.delete(id) : picked.add(id); render(); },
  'review-all': () => { const all = (D.review?.jobs || []).map(j => j.id), picked = S.reviewChecked || new Set(); S.reviewChecked = new Set(all.every(id => picked.has(id)) ? [] : all); render(); },
  'review-decide': el => busy(el.dataset.matches === '1' ? 'reviewYes' : 'reviewNo', async () => {
    const ids = el.dataset.ids.split(',').map(Number), matches = el.dataset.matches === '1';
    const out = await api('/api/discovery/review', { method: 'POST', body: { ids, matches } });
    ids.forEach(id => S.reviewChecked?.delete(id));
    if (S.jobOpen) D.jobModal = await api(`/api/jobs/${S.jobOpen}`).catch(() => D.jobModal);
    await load();
    return out;
  }, out => `${out.done.length} job${out.done.length === 1 ? '' : 's'} ${out.matches ? 'sent on to the detailed match' : 'moved to Not matching'}`),
  'show-not-matching': (el, e) => { e.preventDefault(); S.filters = { ...S.filters, domain: 'not_matching', page: 1 }; go('discovery', { discMode: 'table' }); },
  'rtag-edit': el => {
    S.tagEdit = `${el.dataset.kind}:${el.dataset.id}`;
    const groups = JSON.parse(el.dataset.groups || '[]');
    S.forms.rtag = { groups: groups.length ? groups.map((_, i) => i) : [0] };
    groups.forEach((g, i) => { S.forms.rtag[`domain_${i}`] = g.domain || ''; S.forms.rtag[`roles_${i}`] = [...(g.roles || [])]; });
    loadResumeChoices().then(render); render();
  },
  'rtag-add': el => { const key = `roles_${el.dataset.index}`, roles = fv('rtag', key, []); if (!roles.includes(el.dataset.role)) setF('rtag', key, [...roles, el.dataset.role]); render(); },
  'rtag-add-group': () => { const groups = S.forms.rtag?.groups || [0]; setF('rtag', 'groups', [...groups, Math.max(-1, ...groups) + 1]); render(); },
  'rtag-remove-group': el => { const i = Number(el.dataset.index); setF('rtag', 'groups', (S.forms.rtag?.groups || []).filter(g => g !== i)); render(); },
  'rtag-cancel': () => { S.tagEdit = null; delete S.forms.rtag; render(); },
  'rtag-save': el => busy('rtagSave', async () => {
    document.querySelectorAll('input[data-tag][data-form="rtag"]').forEach(input => { if (input.value.trim()) commitTag(input, false); });
    const body = resumeTagBody();
    const url = el.dataset.kind === 'file' ? `/api/profile/resumes/${el.dataset.id}/tags` : `/api/resumes/${el.dataset.id}/tags`;
    const out = await api(url, { method: 'PUT', body });
    S.tagEdit = null; delete S.forms.rtag; D.resumeChoices = null;
    D.resumeData = await api(rdResumeUrl()).catch(() => D.resumeData);  // the resume being reviewed stays open
    const plan = await api('/api/jobs/resume/rematch?dry_run=true', { method: 'POST' }).catch(() => null);
    return { ...out, changing: plan?.changing || 0 };
  }, out => out.changing ? `Tags saved — ${out.changing} existing job${out.changing === 1 ? '' : 's'} would now use another resume: use Re-match jobs to resume tags` : 'Tags saved — new jobs are matched with them'),
  'job-rematch': el => busy('rematch' + el.dataset.id, async () => {
    const ranked = D.jobModal?.id === Number(el.dataset.id) && D.jobModal.fit != null;
    const why = [el.dataset.pinned ? 'This replaces the resume you or its search chose with the one its tags match.' : '',
      ranked ? 'The job will be ranked again with it (model calls). A tailored packet is tailored again from it when automatic tailoring is on, otherwise it waits for you to regenerate it.' : ''].filter(Boolean);
    if (why.length && !confirm(`${why.join(' ')} Continue?`)) return { cancelled: true };
    const out = await api(`/api/jobs/${el.dataset.id}/resume`, { method: 'PUT', body: { mode: 'auto' } });
    if (S.jobOpen) D.jobModal = await api(`/api/jobs/${S.jobOpen}`).catch(() => D.jobModal);
    await load();
    if (out.ranking_queued?.length) watchRanking(out.ranking_queued);  // the open job shows the new fit once ranked
    return out;
  }, out => out.cancelled ? '' : `Re-matched${out.ranking_queued?.length ? ' · queued for ranking' : ''}`),
  'domain-keep': el => busy('keep' + el.dataset.id, async () => {
    const out = await api(`/api/jobs/${el.dataset.id}/domain-keep`, { method: 'POST' });
    if (S.jobOpen) D.jobModal = await api(`/api/jobs/${S.jobOpen}`).catch(() => D.jobModal);
    await load();
    return out;
  }, out => out.kept.length ? 'Kept — it goes through ranking like any new job' : (out.skipped[0]?.reason || 'Nothing to keep')),
  'bulk-domain-keep': () => busy('bulkKeep', async () => {
    const out = await api('/api/jobs/bulk-domain-keep', { method: 'POST', body: { ids: [...S.checked] } });
    S.checked.clear(); await load(); return out;
  }, out => `Kept ${out.kept.length} job${out.kept.length === 1 ? '' : 's'}`),
  'recover-description': el => busy('recover' + el.dataset.id, async () => {
    const id = el.dataset.id;
    const out = await api(`/api/jobs/${id}/recover-description`, { method: 'POST' });
    if (D.jobModal?.id === Number(id)) D.jobModal = { ...D.jobModal, description_recovery: out };
    if (recoveryActive(out)) watchRecovery(id);
    return out;
  }, out => out.queued ? 'Recovering the description from the posting…' : out.message),
  // Put a held job back where it stopped (ranker.pipeline_status → POST /api/jobs/{id}/rank), then refresh what is on
  // screen once the new ranking is in.
  'pipeline-rank': el => busy('pipeline' + el.dataset.id, async () => {
    const id = Number(el.dataset.id);
    const out = await api(`/api/jobs/${id}/rank`, { method: 'POST' });
    const refresh = async () => {
      if (S.jobOpen === id) D.jobModal = await api(`/api/jobs/${id}`).catch(() => D.jobModal);
      if (S.approvalJob === id) D.approval = await api(`/api/approvals/${id}`).catch(() => D.approval);
      render();
    };
    await refresh();
    watchRanking(out.queued).then(refresh);
    return out;
  }, out => out.queued.length ? 'Back in the ranking queue — approval opens once it qualifies again' : (out.skipped[0]?.reason || 'Nothing to queue')),
  'rank-job': el => busy('rank' + el.dataset.id, async () => {
    const out = await api(`/api/jobs/${el.dataset.id}/rank`, { method: 'POST' });
    watchRanking(out.queued);
    return out;
  }, rankQueuedMsg),
  triage: el => triageJob(el.dataset.id, el.dataset.action, !!el.dataset.tailor),
  // table
  'select-job': async (el, e) => {
    if (e.target.matches('input[type=checkbox]')) return;
    S.selJob = Number(el.dataset.id); render();
    D.selDetail = await api(`/api/jobs/${S.selJob}`).catch(() => null); render();
  },
  check: (el, e) => { e.stopPropagation(); const id = Number(el.dataset.id); S.checked.has(id) ? S.checked.delete(id) : S.checked.add(id); render(); },
  'check-all': el => { const ids = D.jobs.items.map(r => r.id); const all = ids.every(id => S.checked.has(id)); ids.forEach(id => all ? S.checked.delete(id) : S.checked.add(id)); render(); },
  bulk: el => busy('bulk', async () => {
    const out = await api('/api/jobs/bulk-triage', { method: 'POST', body: { ids: [...S.checked], action: el.dataset.action } });
    S.checked.clear(); await load(); return out;
  }, out => `${out.changed} job${out.changed === 1 ? '' : 's'} updated`),
  // Shortlist the selection so it reaches the Tailor queue, then open Bulk tailor with exactly those jobs ticked.
  // Only ranked jobs go: tailoring works from the ranking. Unranked ones stay selected here, ready to rank.
  'bulk-to-tailor': () => busy('bulkToTailor', async () => {
    const ids = rankedSelection();
    const unranked = parsedSelection().length - ids.length, unparsed = S.checked.size - parsedSelection().length;
    await api('/api/jobs/bulk-triage', { method: 'POST', body: { ids, action: 'shortlist' } });
    const queue = await api('/api/tailor/queue'), inQueue = new Set(queue.map(j => j.id));
    const queued = ids.filter(id => inQueue.has(id));
    ids.forEach(id => S.checked.delete(id));
    go('tailor', { tailorMode: 'bulk', tailorChecked: new Set(queued) });
    return { queued: queued.length, left: ids.length - queued.length, unranked, unparsed };
  }, r => `${r.queued} job${r.queued === 1 ? '' : 's'} ready to bulk tailor${r.left ? ` · ${r.left} already approved or applied` : ''}${r.unranked ? ` · ${r.unranked} not ranked yet, left out` : ''}${r.unparsed ? ` · ${r.unparsed} not parsed yet, left out` : ''}`),
  'select-all-matching': (el, e) => {
    e.preventDefault();
    return busy('selectAll', async () => {
      const f = S.filters;
      const out = await api('/api/jobs/search?' + tableQuery(f, { ids_only: true }));
      out.ids.forEach(id => S.checked.add(id));
      const ranked = new Set(out.ranked_ids || []), parsed = new Set(out.parsed_ids || []);
      out.ids.forEach(id => ranked.has(id) ? S.rankedIds.add(id) : S.rankedIds.delete(id));
      out.ids.forEach(id => parsed.has(id) ? S.parsedIds.add(id) : S.parsedIds.delete(id));
    });
  },
  'clear-selection': (el, e) => { e.preventDefault(); S.checked.clear(); render(); },
  // Only parsed jobs are ranked; the others stay selected, ready to parse.
  'bulk-rank': () => busy('bulkRank', async () => {
    const ids = parsedSelection();
    const out = await api('/api/jobs/bulk-rank?rerank=true', { method: 'POST', body: { ids } });
    ids.forEach(id => S.checked.delete(id)); await load(); watchRanking(out.queued);
    return out;
  }, rankQueuedMsg),
  'bulk-parse': () => busy('bulkParse', async () => {
    const out = await api('/api/jobs/bulk-parse', { method: 'POST', body: { ids: [...S.checked].filter(id => !S.parsedIds.has(id)) } });
    out.queued.forEach(id => S.checked.delete(id)); await load(); watchParsing(out.queued);
    return out;
  }, parseQueuedMsg),
  'parse-job': el => busy('parse' + el.dataset.id, async () => {
    const out = await api('/api/jobs/bulk-parse', { method: 'POST', body: { ids: [Number(el.dataset.id)] } });
    await refreshParsed(Number(el.dataset.id)); await load(); watchParsing(out.queued);
    return out;
  }, parseQueuedMsg),
  'bulk-delete': () => {
    const count = S.checked.size;
    if (!confirm(`Permanently delete ${count} job${count === 1 ? '' : 's'}? Tailored resumes for them are deleted too. Approved or applied jobs are kept unless you confirm again.`)) return;
    return busy('bulkDelete', async () => {
      const ids = [...S.checked];
      let out = await api('/api/jobs/bulk-delete', { method: 'POST', body: { ids } });
      if (out.protected.length && confirm(`${out.protected.length} of them are approved or applied and have tracker history. Delete those too?`)) {
        const more = await api('/api/jobs/bulk-delete', { method: 'POST', body: { ids: out.protected, include_submitted: true } });
        out = { ...out, deleted: out.deleted + more.deleted, protected: [] };
      }
      S.checked = new Set(out.protected);
      if (!S.checked.has(S.selJob)) S.selJob = null;
      await load();
      return out;
    }, out => `Deleted ${out.deleted} job${out.deleted === 1 ? '' : 's'}${out.protected.length ? ` · kept ${out.protected.length} approved or applied` : ''}`);
  },
  'pf-reset': el => { S.pf[el.dataset.kind] = pfDefaults(el.dataset.kind); load(); },
  'reset-filters': (el, e) => { e.preventDefault(); S.filters = freshFilters({ per_page: S.filters.per_page }); S.dateMode = null; load(); },
  'clear-filter': el => {
    el.dataset.keys.split(',').forEach(k => { S.filters[k] = Array.isArray(DEFAULT_FILTERS[k]) ? [] : DEFAULT_FILTERS[k]; });
    if (el.dataset.keys.includes('within')) S.dateMode = null;
    S.filters.page = 1; load();
  },
  'toggle-more-filters': () => { S.moreFilters = !S.moreFilters; if (S.moreFilters) S.datePop = false; render(); },
  'date-pop': () => { S.datePop = !S.datePop; if (S.datePop) S.moreFilters = false; render(); },
  'date-field': el => {
    const f = S.filters; if (f.date_field === el.dataset.value) return;
    f.date_field = el.dataset.value;
    if (f.date_field === 'found' && f.posted_unknown) { f.posted_unknown = false; S.dateMode = null; }
    f.page = 1; load();
  },
  // Choosing a mode clears the other modes' values: one date filter at a time.
  'date-mode': el => {
    const f = S.filters, mode = el.dataset.mode, before = JSON.stringify(f);
    if (mode === dateMode(f)) return;
    Object.assign(f, { within: '', older_than: '', date_from: '', date_to: '', posted_unknown: false, page: 1 });
    if (mode === 'within') f.within = '7d';
    if (mode === 'older') f.older_than = '7d';
    if (mode === 'unknown') f.posted_unknown = true;
    S.dateMode = mode === 'any' ? null : mode;
    if (JSON.stringify(f) !== before) load(); else render();
  },
  'date-window': el => { const f = S.filters; f[el.dataset.key] = el.dataset.value; f.page = 1; load(); },
  page: el => { S.filters.page = Number(el.dataset.page); load(); },
  // setup
  'focus-tag': (el, e) => { if (e.target === el) document.getElementById(el.dataset.target)?.focus(); },
  'tag-remove': (el, e) => {
    e.stopPropagation();
    const { form, key } = el.dataset;
    if (FILTER_TAGS[form]) { FILTER_TAGS[form]().keywords.splice(Number(el.dataset.index), 1); S.filters.page = 1; load(); return; }
    const list = [...(fv(form, key, tagFallback(form, key)) || [])]; list.splice(Number(el.dataset.index), 1); setF(form, key, list); render();
  },
  'suggest-role': (el, e) => { e.preventDefault(); addTags('sp', 'roles', el.dataset.role, 5, D.searchProfile.profile.roles); render(); },
  'toggle-site': el => toggleIn('sites', el.dataset.site),
  'toggle-actor': el => toggleIn('actors', el.dataset.actor),
  'toggle-list': el => toggleIn(el.dataset.key, el.dataset.value),
  'toggle-inputs': () => { S.showInputs = !S.showInputs; render(); },
  'toggle-add-actor': (el, e) => { e.preventDefault(); S.addActor = !S.addActor; S.editActor = null; S.caFields = null; delete S.forms.ca; render(); },
  'edit-actor': (el, e) => {
    e.preventDefault(); e.stopPropagation();
    const p = D.searchProfile.profile, id = el.dataset.actor;
    const entry = fv('sp', 'custom_actors', p.custom_actors || []).find(c => c.id === id);
    if (!entry) return;
    S.forms.ca = { id, name: entry.name || id, fan_out: entry.fan_out || 'role_location', input_template: JSON.stringify(entry.input_template || {}, null, 2) };
    S.addActor = true; S.editActor = id; S.caFields = null;
    render();
    document.getElementById('ca-input_template')?.scrollIntoView?.({ block: 'center' });
  },
  'insert-placeholder': el => {
    const box = document.getElementById('ca-input_template'), f = S.forms.ca || {};
    const text = box ? box.value : (f.input_template ?? DEFAULT_TEMPLATE);
    const start = box ? box.selectionStart : text.length, end = box ? box.selectionEnd : text.length;
    const token = `{{${el.dataset.name}}}`;
    // Inside a string the placeholder goes in as is; over a bare value (false, 20…) it replaces it as a quoted string.
    const inString = (text.slice(0, start).match(/(?<!\\)"/g) || []).length % 2 === 1;
    const insert = inString ? token : `"${token}"`;
    setF('ca', 'input_template', text.slice(0, start) + insert + text.slice(end));
    render();
    const next = document.getElementById('ca-input_template');
    if (next) { next.focus(); next.selectionStart = next.selectionEnd = start + insert.length; }
  },
  'insert-actor-field': el => {
    const f = S.forms.ca || {}, name = el.dataset.name;
    let template;
    try { template = JSON.parse(f.input_template ?? DEFAULT_TEMPLATE); } catch { return toast('Fix the JSON before adding a field', true); }
    if (!template || typeof template !== 'object' || Array.isArray(template)) return toast('The input template must be a JSON object', true);
    if (name in template) return toast(`${name} is already in the template`);
    const field = (S.caFields || []).find(x => x.name === name) || {};
    template[name] = field.suggested || (field.default ?? field.prefill ?? (field.type === 'integer' || field.type === 'number' ? 0 : field.type === 'boolean' ? false : field.type === 'array' ? [] : field.type === 'object' ? {} : ''));
    setF('ca', 'input_template', JSON.stringify(template, null, 2));
    render();
  },
  'remove-actor': (el, e) => {
    e.preventDefault(); e.stopPropagation();
    const p = D.searchProfile.profile, id = el.dataset.actor;
    setF('sp', 'custom_actors', fv('sp', 'custom_actors', p.custom_actors || []).filter(c => c.id !== id));
    setF('sp', 'actors', fv('sp', 'actors', p.actors || []).filter(a => a !== id));
    setF('sp', 'changed_actors', fv('sp', 'changed_actors', []).filter(a => a !== id));
    setF('sp', 'removed_actors', [...new Set([...fv('sp', 'removed_actors', []), id])]);
    render(); toast('Actor removed from every setup once you save');
  },
  'lookup-actor': () => busy('lookup', async () => {
    const id = (fv('ca', 'id', '') || '').trim();
    if (!/^[\w.-]+[/~][\w.-]+$/.test(id)) throw new Error('Enter the actor as username/actor-name');
    const out = await api('/api/search/actors/lookup', { method: 'POST', body: { id } });
    if (!S.editActor) {
      setF('ca', 'id', out.id); setF('ca', 'name', out.name);
      if (Object.keys(out.input_template).length) setF('ca', 'input_template', JSON.stringify(out.input_template, null, 2));
    }
    S.caFields = out.fields;
    return out;
  }, out => `Found ${out.name} · ${out.fields.length} documented inputs`),
  'add-actor': () => {
    const f = S.forms.ca || {}, p = D.searchProfile.profile, id = (f.id || '').trim();
    if (!/^[\w.-]+[/~][\w.-]+$/.test(id)) return toast('Enter the actor as username/actor-name', true);
    if (!S.editActor && (D.actors?.actors || []).some(a => !a.custom && a.id === id)) return toast('That actor is already built in — just tick it', true);
    let template;
    try { template = JSON.parse(f.input_template ?? DEFAULT_TEMPLATE); } catch { return toast('The input template is not valid JSON', true); }
    if (!template || typeof template !== 'object' || Array.isArray(template)) return toast('The input template must be a JSON object', true);
    const entry = { id, name: (f.name || '').trim() || id, fan_out: f.fan_out || 'role_location', input_template: template };
    const existing = fv('sp', 'custom_actors', p.custom_actors || []);
    // Editing keeps the actor where it was in the list; adding appends (replacing a same-id entry).
    const customs = existing.some(c => c.id === id) ? existing.map(c => c.id === id ? entry : c) : [...existing, entry];
    setF('sp', 'custom_actors', customs);
    const selected = fv('sp', 'actors', p.actors || []);
    if (!selected.includes(id)) setF('sp', 'actors', [...selected, id]);
    setF('sp', 'changed_actors', [...new Set([...fv('sp', 'changed_actors', []), id])]);
    setF('sp', 'removed_actors', fv('sp', 'removed_actors', []).filter(a => a !== id));
    const edited = !!S.editActor;
    delete S.forms.ca; S.addActor = false; S.editActor = null; S.caFields = null;
    render(); toast(edited ? 'Actor updated — save the profile to keep it' : 'Actor added — save the profile to keep it');
  },
  'save-search': () => busy('saveSp', saveSetup, setup => withPaused(`“${setup.name}” saved`, setup)),
  'save-as-new': () => busy('saveAsNew', () => createSetup(), setup => withPaused(`Saved as “${setup.name}”`, setup)),
  'select-setup': el => useSetup(Number(el.dataset.id)),
  'open-setup': el => { S.setupSel = Number(el.dataset.id); delete S.forms.sp; go('discovery', { discMode: 'setup' }); },
  'new-setup': () => busy('newSetup', async () => {
    // A new setup keeps the actors and filters on screen — only what makes it a different search starts empty.
    const base = setupPayload();
    return createSetup({ ...base, name: nextSetupName('New search'), schedule_kind: 'manual', enabled: true,
                         settings: { ...base.settings, roles: [], locations: [] } });
  }, setup => `“${setup.name}” created — name it and pick its roles`),
  'duplicate-setup': () => busy('dupSetup', async () => {
    const out = await api(`/api/search/setups/${curSetup().id}/duplicate`, { method: 'POST' });
    return adoptSetup(out.setup);
  }, setup => `Copied to “${setup.name}”`),
  'delete-setup': () => {
    const setup = curSetup();
    if (!setup || !confirm(`Delete “${setup.name}”? Jobs it already found stay in your pipeline.`)) return;
    busy('delSetup', async () => {
      await api(`/api/search/setups/${setup.id}`, { method: 'DELETE' });
      D.setups = (D.setups || []).filter(x => x.id !== setup.id);
      S.setupSel = (D.setups[0] || {}).id ?? null; delete S.forms.sp;
      D.searchProfile.profile = curSetup()?.settings || {};
      await load();
    }, `“${setup.name}” deleted`);
  },
  'toggle-day': el => {
    const day = Number(el.dataset.day), days = [...(setupField('weekdays', []) || [])];
    const i = days.indexOf(day); i >= 0 ? days.splice(i, 1) : days.push(day);
    setF('sp', 'weekdays', days.sort((a, b) => a - b)); render();
  },
  'run-search': () => busy('run', async () => {
    const setup = S.discMode === 'setup' ? await saveSetup() : curSetup();
    if (!setup) throw new Error('Create a search setup first');
    const out = await api(`/api/search/setups/${setup.id}/run`, { method: 'POST' });
    adoptSetup(out.setup);
    const actorCount = new Set(out.runs.map(r => r.actor_id)).size;
    S.polling = { ids: out.runs.map(r => r.id), text: `Started ${out.runs.length} run${out.runs.length === 1 ? '' : 's'} on ${actorCount} actor${actorCount === 1 ? '' : 's'}. Waiting for results…` };
    setTimeout(pollRuns, 3000);
    return out;
  }, out => out.warnings?.length ? `Started · note: ${out.warnings[0]}` : null),
  'toggle-apify-import': () => { S.apifyImport = !S.apifyImport; if (S.apifyImport && !D.remoteRuns) A['load-remote'](); else render(); },
  'load-remote': () => busy('remoteRuns', async () => {
    D.remoteRuns = null; render();
    D.remoteRuns = (await api('/api/search/apify/remote-runs?limit=20')).runs;
  }),
  'pick-remote': el => { const id = el.dataset.id; S.importPick.has(id) ? S.importPick.delete(id) : S.importPick.add(id); render(); },
  'import-remote': () => busy('importRuns', async () => {
    const out = await api('/api/search/apify/import', { method: 'POST', body: { external_run_ids: [...S.importPick] } });
    if (out.results.every(r => !r.ok)) throw new Error(out.results[0].error);
    S.importPick.clear();
    D.remoteRuns = (await api('/api/search/apify/remote-runs?limit=20')).runs;
    await afterPull(out.results);
    return out;
  }, out => summarizePull(out.results)),
  'refresh-pending': () => busy('refreshPending', async () => {
    const out = await api('/api/search/apify/runs/refresh-pending', { method: 'POST' });
    if (out.results.length && out.results.every(r => !r.ok)) throw new Error(out.results[0].error);
    await afterPull(out.results);
    return out;
  }, out => summarizePull(out.results)),
  'pull-run': el => busy('pull' + el.dataset.id, async () => {
    const run = await api(`/api/search/apify/runs/${el.dataset.id}${el.dataset.reimport ? '?reimport=true' : ''}`);
    await afterPull([{ ...run, ok: true }]);
    return run;
  }, run => run.completed ? summarizePull([{ ...run, ok: true }]) : `Still ${run.status.toLowerCase()} on Apify — check again shortly`),
  'select-run': async el => { S.runSel = Number(el.dataset.id); render(); D.runDetail = await api(`/api/search/apify/runs/${S.runSel}/detail`).catch(e => { toast(e.message, true); return null; }); render(); },
  'add-manual': () => busy('manual', async () => {
    const m = S.forms.manual || {};
    try {
      const out = await api('/api/jobs/from-url', { method: 'POST', body: { url: (m.url || '').trim(), title: m.title || '', company: m.company || '', location: m.location || '', description: m.description || '' } });
      delete S.forms.manual; S.manualMissing = null;
      toast(out.created ? 'Added to your triage queue' : 'That job was already in the pipeline — details refreshed');
      openJob(out.job.id);
    } catch (e) {
      if (e.status === 422 && e.detail?.extracted) {
        S.manualMissing = e.detail.message;
        for (const [k, v] of Object.entries(e.detail.extracted)) if (v && !m[k]) setF('manual', k, v);
        return;
      }
      throw e;
    }
  }),
  'manual-mode': el => { S.manualMode = el.dataset.id; S.manualMissing = null; render(); },
  'manual-file': el => {
    const file = el.files && el.files[0]; el.value = '';
    if (!file) return;
    return busy('manualFile', async () => {
      const form = new FormData(); form.append('file', file);
      const out = await api('/api/jobs/read-file', { method: 'POST', form });
      const m = S.forms.manual || {};
      for (const k of ['title', 'company', 'location']) if (out[k] && !m[k]) setF('manual', k, out[k]);
      setF('manual', 'description', out.description); setF('manual', 'file_name', file.name);
    }, () => 'File read — check the title and company, then add it');
  },
  'add-manual-text': () => busy('manual', async () => {
    const m = S.forms.manual || {};
    const out = await api('/api/jobs/from-text', { method: 'POST', body: { title: (m.title || '').trim(), company: (m.company || '').trim(), location: (m.location || '').trim(), url: (m.url || '').trim(), description: m.description || '' } });
    delete S.forms.manual;
    toast(out.created ? 'Added to your triage queue' : 'That job was already in the pipeline — details refreshed');
    openJob(out.job.id);
  }),
  'board-teams': () => busy('boardTeams', async () => {
    const kind = fv('board', 'kind', 'greenhouse'), token = (fv('board', 'token', '') || '').trim();
    if (!/^[\w.-]+$/.test(token)) throw new Error('Enter the board token — letters, numbers, dots or dashes');
    const out = await api(`/api/import/${kind}/${encodeURIComponent(token)}/teams`);
    S.boardTeams = out.teams; setF('board', 'teams', []);
    return out;
  }, out => out.teams.length ? `${out.teams.length} teams found` : 'This board does not list teams'),
  'toggle-board-team': el => {
    const team = el.dataset.team, chosen = fv('board', 'teams', []) || [];
    setF('board', 'teams', chosen.includes(team) ? chosen.filter(t => t !== team) : [...chosen, team]); render();
  },
  'board-review': () => busy('boardReview', async () => {
    const kind = fv('board', 'kind', 'greenhouse'), token = (fv('board', 'token', '') || '').trim();
    if (!/^[\w.-]+$/.test(token)) throw new Error('Enter the board token — letters, numbers, dots or dashes');
    const body = { teams: fv('board', 'teams', []) || [], roles: String(fv('board', 'roles', '') || '').split(',').map(r => r.trim()).filter(Boolean) };
    const out = await api(`/api/import/${kind}/${encodeURIComponent(token)}/preview`, { method: 'POST', body });
    S.boardReview = { kind, token, jobs: out.jobs, total: out.total, selected: new Set(out.jobs.filter(j => !j.in_pipeline).map(j => j.source_ref)) };
    return out;
  }, out => `${out.jobs.length} matching roles to review`),
  'toggle-board-job': el => { const s = S.boardReview?.selected; if (!s) return; const ref = el.dataset.ref; if (s.has(ref)) s.delete(ref); else s.add(ref); render(); },
  'board-review-all': () => { const r = S.boardReview; r.selected = new Set(r.jobs.filter(j => !j.in_pipeline).map(j => j.source_ref)); render(); },
  'board-review-none': () => { S.boardReview.selected = new Set(); render(); },
  'board-review-close': () => { S.boardReview = null; render(); },
  'board-review-import': () => busy('board', async () => {
    const r = S.boardReview;
    if (!r?.selected.size) throw new Error('Pick at least one role');
    const body = { teams: fv('board', 'teams', []) || [], roles: String(fv('board', 'roles', '') || '').split(',').map(x => x.trim()).filter(Boolean), source_refs: [...r.selected] };
    const out = await api(`/api/import/${r.kind}/${encodeURIComponent(r.token)}`, { method: 'POST', body });
    delete S.forms.board; S.boardTeams = null; S.boardReview = null; await load(); return out;
  }, out => `Added ${out.created} new role${out.created === 1 ? '' : 's'} — they are in your triage queue`),
  'import-board': () => busy('board', async () => {
    const kind = fv('board', 'kind', 'greenhouse'), token = (fv('board', 'token', '') || '').trim();
    if (!/^[\w.-]+$/.test(token)) throw new Error('Enter the board token — letters, numbers, dots or dashes');
    const body = { teams: fv('board', 'teams', []) || [], roles: String(fv('board', 'roles', '') || '').split(',').map(r => r.trim()).filter(Boolean) };
    const out = await api(`/api/import/${kind}/${encodeURIComponent(token)}`, { method: 'POST', body });
    delete S.forms.board; S.boardTeams = null; await load(); return out;
  }, out => `Imported ${out.created} new of ${out.seen} ${out.seen === out.total ? 'open' : 'matching'} roles — they are in your triage queue`),
  'ranking-retry': el => busy('rankRetry' + el.dataset.job, async () => {
    const out = await api(`/api/jobs/${el.dataset.job}/rank`, { method: 'POST' });
    await loadRanking();
    return out;
  }, rankQueuedMsg),
  'ranking-refresh': () => busy('rankRefresh', () => loadRanking()),
  // Clearing hides finished rankings from this list only: each job keeps its rankings, errors and history.
  'ranking-reset-filters': () => { S.rf = { q: '', location: 'all' }; wkFilter('ranking').status = 'all'; loadRanking().then(render); },
  'ranking-remove': el => {
    const ids = String(el.dataset.ids).split(',').map(Number);
    if (el.dataset.confirm && !confirm(`Take ${ids.length} job${ids.length === 1 ? '' : 's'} out of the ranking queue? You can rank ${ids.length === 1 ? 'it' : 'any of them'} again later from the job.`)) return;
    return busy(el.dataset.confirm ? 'rankRemoveAll' : 'rankRemove' + ids[0], async () => {
      const out = await api('/api/ranking/remove', { method: 'POST', body: { ids } });
      await loadRanking();
      return out;
    }, out => `Removed ${out.removed.length} from ranking${out.skipped.length ? ` · ${out.skipped.length} not removed: ${out.skipped[0].reason}` : ''}`);
  },
  'rank-batch': () => busy('rankBatch', async () => {
    const out = await api('/api/jobs/rank-batch', { method: 'POST' });
    if (!out.queued.length && !out.skipped.length) throw new Error('Every job is already ranked');
    await load(); watchRanking(out.queued);
    return out;
  }, rankQueuedMsg),
  'recover-descriptions': () => busy('recoverDescriptions', async () => {
    const out = await api('/api/jobs/recover-descriptions', { method: 'POST' });
    await load();
    return out;
  }, recoverQueuedMsg),
  // tailor
  'pick-tailor': el => go('tailor', { tailorJob: Number(el.value) }),
  'tailor-tab': el => { S.tailorTab = el.dataset.id; S.editing = null; render(); },
  generate: el => queueTailoring([Number(el.dataset.id)], 'tailor', true, 'queue' + el.dataset.id),
  'task-cancel': el => busy('tasks', async () => { await api('/api/tailor-tasks/cancel', { method: 'POST', body: { ids: [Number(el.dataset.id)] } }); await refreshTasks(); render(); }),
  'tasks-cancel': () => busy('tasks', async () => { await api('/api/tailor-tasks/cancel', { method: 'POST', body: {} }); await refreshTasks(); render(); }, 'Queued jobs cancelled'),
  'task-retry': el => {
    const task = (D.tasks || []).find(t => t.id === Number(el.dataset.id));
    if (!task || retried(task)) return;
    return queueTailoring([task.job_id], task.mode, true, 'retry' + task.id);
  },
  'task-open': el => go('tailor', { tailorMode: 'single', tailorJob: Number(el.dataset.job), tailorTab: 'diff' }),
  'tailor-check': (el, e) => {
    e.stopPropagation();
    const id = Number(el.dataset.id);
    S.tailorChecked.has(id) ? S.tailorChecked.delete(id) : S.tailorChecked.add(id);
    render();
  },
  'tailor-select': el => {
    const queue = D.tailorQueue || [], which = el.dataset.which;
    S.tailorChecked = new Set(which === 'all' ? queue.map(j => j.id) : which === 'untailored' ? queue.filter(j => !j.has_resume || j.status === 'needs_retailor').map(j => j.id)
      : which === 'unreviewed' ? queue.filter(unreviewedPacket).map(j => j.id) : []);
    render();
  },
  'tailor-pages': el => { S.tailorPages = el.value; render(); },
  'tailor-regenerate': (el, e) => { e.stopPropagation(); S.tailorRegenerate = !S.tailorRegenerate; render(); },
  'bulk-tailor': el => {
    if (el.dataset.mode === 'review') {
      const picked = (D.tailorQueue || []).filter(j => S.tailorChecked.has(j.id) && reviewablePacket(j)).map(j => j.id);
      return queueTailoring(picked, 'review', S.tailorRegenerate, 'bulkTailor', () => picked.forEach(id => S.tailorChecked.delete(id)));
    }
    const ids = (D.tailorQueue || []).filter(j => S.tailorChecked.has(j.id) && !activeTask(j.id) && (S.tailorRegenerate || !j.has_resume || j.status === 'needs_retailor')
      && (el.dataset.mode === 'skip' || j.fit != null)).map(j => j.id);
    return queueTailoring(ids, el.dataset.mode, S.tailorRegenerate, 'bulkTailor', () => ids.forEach(id => S.tailorChecked.delete(id)));
  },
  'skip-tailor': el => queueTailoring([Number(el.dataset.id)], 'skip', true, 'queue' + el.dataset.id),
  'send-review': el => queueTailoring([Number(el.dataset.id)], 'review', false, 'queue' + el.dataset.id),
  'edit-start': el => { S.editing = Number(el.dataset.index); render(); document.getElementById(`edit-text-${el.dataset.index}`)?.focus(); },
  'edit-cancel': () => { S.editing = null; render(); },
  // Read typed values before busy() re-renders and replaces the inputs.
  'edit-save': el => { const text = document.getElementById(`edit-text-${el.dataset.index}`).value; return busy('edit', async () => {
    D.packet = await api(`/api/resumes/${el.dataset.resume}/edits/${el.dataset.index}`, { method: 'PATCH', body: { text, status: 'accepted' } });
    S.editing = null;
  }, 'Edit saved'); },
  'edit-status': el => busy('edit', async () => {
    D.packet = await api(`/api/resumes/${el.dataset.resume}/edits/${el.dataset.index}`, { method: 'PATCH', body: { status: el.dataset.status } });
  }),
  'save-cover': el => busy('cover', async () => {
    await api(`/api/resumes/${el.dataset.resume}/cover-letter`, { method: 'PUT', body: { text: fv('cover' + el.dataset.resume, 'text', D.packet.resume.cover_letter) } });
    delete S.forms['cover' + el.dataset.resume]; await load();
  }, 'Cover letter saved'),
  // approvals
  'select-approval': async el => { S.approvalJob = Number(el.dataset.id); render(); insEnsure(S.approvalJob); D.approval = await api(`/api/approvals/${S.approvalJob}`).catch(() => null); render(); },
  'approve-override-open': el => { S.overriding = Number(el.dataset.id); S.rejecting = null; delete S.forms.override; render(); },
  'approve-override-close': (el, e) => { e?.preventDefault?.(); S.overriding = null; delete S.forms.override; render(); },
  approve: el => busy('approve', async () => {
    const override = el.dataset.override === '1';
    const out = await api(`/api/jobs/${el.dataset.id}/approve`, { method: 'POST',
      body: { override, note: override ? fv('override', 'note', '') : '' } });
    S.overriding = null; delete S.forms.override;
    await load(); openKit(el.dataset.id); return out;
  }, out => out.approval_override ? 'Approved below your minimum — here is your apply kit' : 'Approved — here is your apply kit'),
  'approval-check': (el, e) => {
    e.stopPropagation();
    const id = Number(el.dataset.id);
    S.approvalPicked.has(id) ? S.approvalPicked.delete(id) : S.approvalPicked.add(id);
    if (!S.approvalPicked.size) S.bulkRejecting = false;
    render();
  },
  'approval-check-all': () => {
    const list = D.approvals || [];
    S.approvalPicked = list.every(q => S.approvalPicked.has(q.id)) ? new Set() : new Set(list.map(q => q.id));
    if (!S.approvalPicked.size) S.bulkRejecting = false;
    render();
  },
  'approval-check-none': (el, e) => { e?.preventDefault?.(); S.approvalPicked = new Set(); S.bulkRejecting = false; render(); },
  'approval-star': async (el, e) => {
    e?.stopPropagation?.();
    const id = Number(el.dataset.id);
    const row = (D.approvals || []).find(q => q.id === id), detail = D.approval?.job?.id === id ? D.approval.job : null;
    const starred = !(row || detail)?.starred;
    const set = v => { if (row) row.starred = v; if (detail) detail.starred = v; };
    set(starred); render();
    try {
      await api(`/api/jobs/${id}/star`, { method: 'PUT', body: { starred } });
      // The queue puts starred jobs first, and the Starred filter drops an unstarred one.
      await load();
    } catch (err) { set(!starred); render(); toast(err.message, true); }
  },
  'bulk-open-postings': () => {
    const urls = (D.approvals || []).filter(q => S.approvalPicked.has(q.id)).map(q => postingUrl(q.url)).filter(Boolean);
    if (!urls.length) return;
    const opened = openPostings(urls);
    if (opened < urls.length) toast(`Opened ${opened} of ${urls.length} postings — your browser blocked the rest. Allow pop-ups for this site and try again.`, true);
    else toast(`Opened ${opened} posting${opened === 1 ? '' : 's'} in new tabs`);
  },
  // Retailor and Re-review queue background tailor tasks. Queued jobs leave the selection; skipped ones stay picked.
  'bulk-retailor': () => queueApprovalRedo('tailor', 'bulkRetailor', { revise: true }),
  'bulk-rereview': () => queueApprovalRedo('review', 'bulkRereview'),
  'bulk-reject-open': () => { S.bulkRejecting = true; delete S.forms.bulkReject; render(); },
  'bulk-reject-close': (el, e) => { e?.preventDefault?.(); S.bulkRejecting = false; delete S.forms.bulkReject; render(); },
  'bulk-reject-reason': el => { setF('bulkReject', 'reason', fv('bulkReject', 'reason', '') === el.dataset.id ? '' : el.dataset.id); render(); },
  'bulk-approval': el => busy('bulkApproval', async () => {
    const action = el.dataset.action;
    const ids = (D.approvals || []).filter(q => S.approvalPicked.has(q.id)).map(q => q.id);
    if (!ids.length) return null;
    const body = { action, job_ids: ids, ...(action === 'reject' ? { reason: fv('bulkReject', 'reason', ''), notes: fv('bulkReject', 'notes', '') } : {}) };
    const out = await api('/api/approvals/bulk', { method: 'POST', body });
    // Skipped jobs stay selected, so it is clear which ones still need a decision.
    S.approvalPicked = new Set(out.skipped.map(x => x.id));
    S.bulkRejecting = false; delete S.forms.bulkReject;
    if (action === 'reject' && out.done.includes(S.approvalJob)) { S.approvalJob = null; D.approval = null; }
    await load();
    return out;
  }, out => out && bulkApprovalMsg(out)),
  'reject-open': el => { S.rejecting = Number(el.dataset.id); delete S.forms.reject; render(); },
  'reject-close': (el, e) => { e?.preventDefault?.(); S.rejecting = null; delete S.forms.reject; render(); },
  'reject-reason': el => { setF('reject', 'reason', el.dataset.id); render(); },
  'reject-confirm': el => busy('reject', async () => {
    const out = await api(`/api/jobs/${el.dataset.id}/reject`, { method: 'POST', body: {
      reason: fv('reject', 'reason', ''), notes: fv('reject', 'notes', '') } });
    S.rejecting = null; delete S.forms.reject;
    S.approvalJob = null; D.approval = null;
    await load();
    return out;
  }, out => `Rejected${out.rejection?.reason ? ` · ${out.rejection.label.toLowerCase()}` : ''}`),
  revise: el => busy('revise', async () => {
    await api(`/api/jobs/${el.dataset.id}/revision`, { method: 'POST', body: { notes: fv('approvalNotes', el.dataset.id, D.approval?.job?.id === Number(el.dataset.id) ? D.approval.notes || '' : '') } });
    await load();
  }, 'Sent back for revision'),
  'open-kit': el => openKit(el.dataset.id),
  'close-kit': () => { S.kitJob = null; S.kitEmailCode = null; render(); },
  'copy-answer': el => { copyText(el.dataset.text); api(`/api/knowledge/${el.dataset.id}/used`, { method: 'POST' }).catch(() => {}); },
  'kit-release': el => busy('kit-release', async () => {
    await api(`/api/jobs/${el.dataset.id}/submission/release`, { method: 'POST', body: { claim_token: el.dataset.token, reason: 'Not submitted (Apply kit)' } });
    D.kit = await api(`/api/jobs/${el.dataset.id}/apply-kit`);
  }, 'Released'),
  'kit-resolve': el => busy('kit-resolve', async () => {
    await api(`/api/jobs/${el.dataset.id}/submission/resolve`, { method: 'POST', body: { claim_id: Number(el.dataset.claim), submitted: !!el.dataset.submitted } });
    D.kit = await api(`/api/jobs/${el.dataset.id}/apply-kit`);
  }, 'Saved'),
  'kit-prepare': el => busy('kit-prepare', async () => {
    await api(`/api/jobs/${el.dataset.id}/application-execution`, { method: 'POST', body: el.dataset.mode ? { mode: el.dataset.mode } : {} });
    D.kit = await api(`/api/jobs/${el.dataset.id}/apply-kit`);
  }, 'Queued for the application worker'),
  'kit-answer': el => {
    const q = (D.kit?.execution_questions || []).find(x => x.id === Number(el.dataset.qid));
    const answer = String(fv('kitq', `q${el.dataset.qid}`, q?.answer || '')).trim();
    if (!q) return;
    if (!answer) { toast('Type or choose an answer first', true); return; }
    return busy(`kitq-${q.id}`, async () => {
      await api(`/api/knowledge/${q.id}`, { method: 'PUT', body: { question: q.question, answer, category: q.category, answer_type: q.answer_type, verified: true, sensitive: q.sensitive } });
      if (S.forms.kitq) delete S.forms.kitq[`q${q.id}`];
      D.kit = await api(`/api/jobs/${S.kitJob}/apply-kit`);
    }, 'Saved to the Knowledge bank');
  },
  'kit-open-questions': el => {
    const ids = el.dataset.ids.split(',').map(Number).filter(Boolean);
    S.kitJob = null;
    const f = knowledgeState().filters.answers;
    Object.assign(f, { q: '', status: 'all', category: 'all', sensitive: 'all', ids });
    go('knowledge');
  },
  'kit-save-window': el => busy('kit-save-window', async () => {
    const out = await api(`/api/application-executions/${el.dataset.exec}/save-window-answers`, { method: 'POST' });
    D.kit = await api(`/api/jobs/${S.kitJob}/apply-kit`);
    return out;
  }, out => `${out.saved} answer${out.saved === 1 ? '' : 's'} saved to the Knowledge bank`),
  'kit-browser-use': el => busy('kit-browser-use', async () => {
    await api(`/api/jobs/${el.dataset.id}/application-execution`, { method: 'POST', body: { backend: 'browser_use_webui' } });
    if (S.kitJob) D.kit = await api(`/api/jobs/${el.dataset.id}/apply-kit`);
    if (S.screen === 'workers' && typeof loadWorkers === 'function') await loadWorkers();
  }, 'Queued on browser-use: a browser window opens on the next pass'),
  'kit-guarded': el => busy('kit-guarded', async () => {
    await api(`/api/jobs/${el.dataset.id}/application-execution`, { method: 'POST', body: { mode: 'GUARDED_AUTO' } });
    D.kit = await api(`/api/jobs/${el.dataset.id}/apply-kit`);
  }, 'Queued: it fills and submits the application, and asks you only if something needs you'),
  'kit-continue': el => busy('kit-continue', async () => {
    await api(`/api/application-executions/${el.dataset.exec}/continue`, { method: 'POST' });
    S.kitSignup = null;
    D.kit = await api(`/api/jobs/${S.kitJob}/apply-kit`);
  }, 'Continuing in the same page: it checks everything again first'),
  'kit-signup': el => busy('kit-signup', async () => {
    const out = await api(`/api/application-executions/${el.dataset.exec}/signup`, { method: 'POST' });
    S.kitSignup = { exec: Number(el.dataset.exec), domain: out.domain, username: out.username, password: out.password };
  }, 'Use these on the signup form, then Save this login'),
  'kit-copy-password': () => { if (S.kitSignup) copyText(S.kitSignup.password); },
  'kit-email-code': el => busy('kit-email-code', async () => {
    const out = await api(`/api/application-executions/${el.dataset.exec}/email-code`);
    S.kitEmailCode = { exec: Number(el.dataset.exec), code: out.status === 'FOUND' ? out.code : null, message: out.status === 'FOUND' ? '' : out.message };
    return out;
  }, out => (out.status === 'FOUND' ? 'Code found: copy it into the browser window' : out.message)),
  'kit-copy-email-code': () => { if (S.kitEmailCode && S.kitEmailCode.code) copyText(S.kitEmailCode.code); },
  'kit-save-login': el => busy('kit-save-login', async () => {
    const typed = S.kitSignup && S.kitSignup.exec === Number(el.dataset.exec) ? null : window.prompt('The password you used on the site (stored encrypted, never shown again). Leave empty to keep only the session.');
    const out = await api(`/api/application-executions/${el.dataset.exec}/save-login`, { method: 'POST', body: typed ? { password: typed } : {} });
    S.kitSignup = null;
    toast(out.session_saved ? 'Login saved: the next application on this site starts signed in' : 'Login saved');
  }, null),
  'kit-confirm': el => busy('kit-confirm', async () => {
    await api(`/api/application-executions/${el.dataset.exec}/confirm`, { method: 'POST', body: { question_ids: el.dataset.ids.split(',').map(Number) } });
    D.kit = await api(`/api/jobs/${S.kitJob}/apply-kit`);
  }, 'Confirmed for this application'),
  'mark-applied': el => busy('applied', async () => {
    const k = S.forms.kit || {};
    const token = D.kit?.claim?.claim_token;
    await api(`/api/jobs/${el.dataset.id}/applied`, { method: 'POST', body: { recruiter_name: k.recruiter_name || '', recruiter_email: k.recruiter_email || '', notes: '', ...(token ? { claim_token: token } : {}) } });
    delete S.forms.kit; S.kitJob = null;
    const tracker = await api('/api/tracker');
    const app = tracker.applications.find(a => a.job_id === Number(el.dataset.id));
    go('tracker', { selApp: app?.id, trackerView: 'board' });
  }, 'Marked applied · follow-up scheduled'),
  // tracker
  'tracker-view': el => { S.trackerView = el.dataset.id; if (el.dataset.id === 'inbox') D.inbox = undefined; render(); },
  'select-app': el => { S.selApp = Number(el.dataset.id); S.editingRecruiter = false; S.scheduling = false; if (el.dataset.board) S.trackerView = 'board'; render(); },
  'move-stage': el => moveStage(el.dataset.app, el.dataset.stage),
  'tracker-fold': el => { toggleStage(el.dataset.id); render(); },
  'tracker-show-archived': () => { S.showArchived = !S.showArchived; render(); },
  'tracker-add-toggle': () => { S.addingStage = !S.addingStage; render(); },
  'tracker-unarchive': el => busy('unarchive', async () => {
    await api(`/api/applications/${el.dataset.app}/unarchive`, { method: 'POST' });
    S.selApp = Number(el.dataset.app); await load();
  }, 'Back on the board'),
  'tracker-archive-open': () => {
    if (S.trackerArchive) { S.trackerArchive = null; render(); return; }
    S.trackerArchive = { days: 30, scope: 'no_reply', preview: null, loading: false }; trackerArchivePreview();
  },
  'tracker-archive-cancel': () => { S.trackerArchive = null; render(); },
  'tracker-archive-days': el => {
    const days = Number(el.dataset.days ?? el.value);
    if (!S.trackerArchive || !Number.isInteger(days) || days < 0) return;
    S.trackerArchive.days = days; S.trackerArchive.preview = null; trackerArchivePreview();
  },
  'tracker-archive-scope': el => { if (!S.trackerArchive) return; S.trackerArchive.scope = el.value; S.trackerArchive.preview = null; trackerArchivePreview(); },
  'tracker-archive-run': () => busy('archive', async () => {
    const { days, scope } = S.trackerArchive;
    const out = await api('/api/tracker/archive', { method: 'POST', body: { days, scope } });
    D.tracker = out.tracker; S.trackerArchive = null;
    return out;
  }, out => `Archived ${out.count} application${out.count === 1 ? '' : 's'}`),
  'add-stage': el => busy('stages', async () => { D.tracker = await api('/api/tracker/stages', { method: 'PUT', body: { extra_stages: [...D.tracker.extra_stages, el.dataset.id] } }); }),
  'remove-stage': () => busy('stages', async () => {
    const extras = D.tracker.extra_stages.slice(0, -1), removed = D.tracker.extra_stages.at(-1);
    if (D.tracker.applications.some(a => a.stage === removed)) throw new Error('Move the applications out of that stage first');
    D.tracker = await api('/api/tracker/stages', { method: 'PUT', body: { extra_stages: extras } });
  }),
  'log-event': el => {
    const text = document.getElementById('log-text').value.trim(), kind = document.getElementById('log-kind').value;
    if (!text) return toast('Write something to log', true);
    return busy('log', async () => {
      await api(`/api/applications/${el.dataset.app}/events`, { method: 'POST', body: { kind, title: kind[0].toUpperCase() + kind.slice(1), detail: text } });
      await load();
    }, 'Logged');
  },
  'edit-recruiter': (el, e) => {
    e.preventDefault();
    const sel = D.tracker.applications.find(a => a.id === Number(el.dataset.app));
    S.recruiterDraft = (sel?.recruiters || []).map(r => ({ ...r }));
    if (!S.recruiterDraft.length) S.recruiterDraft.push({ name: '', email: '', phone: '' });
    S.editingRecruiter = true; render();
  },
  'edit-recruiter-cancel': () => { S.editingRecruiter = false; render(); },
  'recruiter-add': () => { S.recruiterDraft.push({ name: '', email: '', phone: '' }); render(); document.getElementById(`rec-${S.recruiterDraft.length - 1}-name`)?.focus(); },
  'recruiter-remove': el => { S.recruiterDraft.splice(Number(el.dataset.index), 1); render(); },
  'save-recruiter': el => busy('recruiter', async () => {
    const recruiters = S.recruiterDraft.map(r => ({ name: r.name.trim(), email: r.email.trim(), phone: r.phone.trim() })).filter(r => r.name || r.email || r.phone);
    await api(`/api/applications/${el.dataset.app}/recruiters`, { method: 'PUT', body: { recruiters } });
    S.editingRecruiter = false; await load();
  }, 'Recruiters saved'),
  'copy-draft': el => copyText(D.tracker.applications.find(a => a.id === Number(el.dataset.app)).draft),
  'schedule-start': () => {
    let tz = BROWSER_TZ; try { tz = tzName(localStorage.getItem('interviewTimezone') || tz); } catch { /* storage blocked */ }
    if (!tzOptions().some(([v]) => v === tz)) tz = BROWSER_TZ;
    S.ivDraft = { title: '', at: '', timezone: tz, contact_name: '', contact_email: '', contact_phone: '' };
    S.scheduling = true; render(); document.getElementById('iv-title')?.focus();
  },
  'schedule-cancel': () => { S.scheduling = false; render(); },
  'schedule-save': el => {
    const d = S.ivDraft;
    if (!d.at) return toast('Pick a date and time', true);
    return busy('schedule', async () => {
      await api(`/api/applications/${el.dataset.app}/events`, { method: 'POST', body: {
        kind: 'interview', title: d.title.trim() || 'Interview', detail: 'Scheduled', local_time: d.at, timezone: d.timezone,
        contact_name: d.contact_name.trim(), contact_email: d.contact_email.trim(), contact_phone: d.contact_phone.trim() } });
      try { localStorage.setItem('interviewTimezone', d.timezone); } catch { /* storage blocked */ }
      S.scheduling = false; await load();
    }, 'Interview scheduled');
  },
  ...KNOWLEDGE_ACTIONS,
  ...SETTINGS_ACTIONS,
  ...RESUME_DATA_ACTIONS,
  ...JOB_DATA_ACTIONS,
  ...MONITORING_ACTIONS,
  ...ORCHESTRATION_ACTIONS,
  ...WORKERS_ACTIONS,
  ...INBOX_ACTIONS,
  ...GUIDE_ACTIONS,
  ...INSIGHTS_ACTIONS,
};
function tagFallback(form, key) {
  if (form === 'sp') return D.searchProfile?.profile?.[key] || [];
  if (form === 'domain') return D.domain?.[key] || [];
  if (form === 'rtag') return S.forms.rtag?.[key] || [];
  if (form === 'preferences') { const c = CHIP_FIELDS.find(x => x[0] === key); return (c && c[5] === 'sp' ? D.profile?.search_preferences?.[key] : D.profile?.[key]) || []; }
  return [];
}
async function moveStage(appId, stage) {
  try { await api(`/api/applications/${appId}/stage`, { method: 'POST', body: { stage } }); S.selApp = Number(appId); await load(); }
  catch (e) { toast(e.message, true); }
}

/* ───────────── events ───────────── */
document.addEventListener('click', e => {
  const knowledgeLink = e.target.closest('a[href^="#knowledge/"]');
  if (knowledgeLink && S.screen === 'knowledge' && !e.metaKey && !e.ctrlKey) { e.preventDefault(); knowledgeNavigate(knowledgeLink.hash.split('/')[1]); return; }
  const link = e.target.closest('a[href^="#settings/"]');
  if (link && S.screen === 'settings' && !e.metaKey && !e.ctrlKey) { e.preventDefault(); settingsNavigate(link.hash.split('/')[1]); return; }
  const el = e.target.closest('[data-act]');
  if (!el || el.disabled) return;
  const fn = A[el.dataset.act]; if (!fn) return;
  if (el.tagName === 'A' || (el.tagName === 'BUTTON' && el.closest('form'))) e.preventDefault();
  if (el.tagName === 'LABEL' || (el.tagName === 'INPUT' && el.type === 'checkbox' && el.dataset.act === 'toggle-site')) { /* let the checkbox toggle */ }
  fn(el, e);
});
let filterTimer, paletteTimer;
document.addEventListener('input', e => {
  const el = e.target;
  if (knowledgeFilterEvent(el)) return;
  if (el.dataset.authEntry || el.dataset.authNotes) { settingsAuthUpdate(); return; }
  if (el.dataset.rdPath) { resumeDataInput(el); return; }
  if (el.dataset.jdPath) { jobDataInput(el); return; }
  if (el.dataset.resumeChoice) { chooseResume(el); return; }
  if (el.dataset.form === 'rtag' && el.dataset.field.startsWith('domain_')) { setF('rtag', el.dataset.field, el.value); render(); return; }
  if (el.dataset.form && el.dataset.field) {
    setF(el.dataset.form, el.dataset.field, el.type === 'checkbox' ? el.checked : el.value);
    if (el.dataset.live || el.type === 'radio') render();
  }
  if (el.dataset.tag && S.screen === 'settings') settingsTouched(el.dataset.form);
  if (el.dataset.rec !== undefined && S.recruiterDraft?.[el.dataset.rec]) S.recruiterDraft[el.dataset.rec][el.dataset.recfield] = el.value;
  if (el.dataset.iv && S.ivDraft) S.ivDraft[el.dataset.iv] = el.value;
  if (el.dataset.pf && el.dataset.pfkey === 'q') { S.pf[el.dataset.pf].q = el.value; clearTimeout(filterTimer); filterTimer = setTimeout(load, 250); }
  if (el.dataset.tf === 'q') { S.tf.q = el.value; render(); }
  if (el.dataset.rf === 'q') { S.rf.q = el.value; clearTimeout(filterTimer); filterTimer = setTimeout(() => loadRanking().then(render), 250); }
  if (el.dataset.filter === 'q') { S.filters.q = el.value; S.filters.page = 1; clearTimeout(filterTimer); filterTimer = setTimeout(load, 250); }
  if (el.dataset.triage === 'min_fit') { S.triage.min_fit = Number(el.value); clearTimeout(filterTimer); filterTimer = setTimeout(load, 250); render(); }
  if (el.dataset.palette) {
    S.paletteQuery = el.value; clearTimeout(paletteTimer);
    paletteTimer = setTimeout(async () => {
      if (!S.paletteQuery) { S.paletteJobs = []; render(); return; }
      const out = await api('/api/jobs/search?' + qs({ q: S.paletteQuery, per_page: 6 })).catch(() => ({ items: [], total: 0 }));
      S.paletteJobs = out.items; S.paletteTotal = out.total; render();
    }, 180);
  }
});
document.addEventListener('change', e => {
  const el = e.target;
  if (knowledgeFilterEvent(el)) return;
  if (el.dataset.authEntry) { settingsAuthUpdate(); return; }
  if (el.dataset.rdPath) { resumeDataInput(el); return; }
  if (el.dataset.jdPath) { jobDataInput(el); return; }
  if (el.dataset.form && el.dataset.field && (el.tagName === 'SELECT' || el.type === 'checkbox' || el.type === 'radio')) {
    setF(el.dataset.form, el.dataset.field, el.type === 'checkbox' ? el.checked : el.value);
    // Radios, and checkboxes marked data-rerender, change which fields the form shows.
    if (el.type === 'radio' || 'rerender' in el.dataset) render();
  }
  if (el.dataset.iv && S.ivDraft) S.ivDraft[el.dataset.iv] = el.value;
  if (el.dataset.pf && el.dataset.pfkey !== 'q') { const k = el.dataset.pfkey; S.pf[el.dataset.pf][k] = k.startsWith('min_') ? Number(el.value) : el.value; load(); }
  if (el.dataset.rf && el.dataset.rf !== 'q') { S.rf[el.dataset.rf] = el.value; loadRanking().then(render); }
  if (el.dataset.tf && el.dataset.tf !== 'q') { S.tf[el.dataset.tf] = el.dataset.tf === 'min_fit' ? Number(el.value) : el.value; render(); }
  if (el.dataset.filter && el.dataset.filter !== 'q') { S.filters[el.dataset.filter] = el.type === 'checkbox' ? el.checked : ['per_page', 'min_fit', 'max_fit'].includes(el.dataset.filter) ? Number(el.value) : el.value; S.filters.page = 1; load(); }
  if (el.dataset.triage && el.dataset.triage !== 'min_fit') { S.triage[el.dataset.triage] = el.type === 'checkbox' ? el.checked : el.value; load(); }
  if (el.dataset.actChange) A[el.dataset.actChange](el, e);
  if (el.id === 'resume-file' && el.files[0]) { settingsUploadResume(el.files[0]); el.value = ''; }
});
document.addEventListener('focusout', e => {
  const el = e.target;
  if (el.dataset && el.dataset.tag && el.value.trim()) commitTag(el, false);
  if (el.dataset && el.dataset.actBlur === 'save-notes') {
    const id = el.dataset.id; if (el.value === (D.approval?.notes || '')) return;
    setF('approvalNotes', id, el.value);
    api(`/api/jobs/${id}/notes`, { method: 'PUT', body: { notes: el.value } }).then(() => { if (D.approval) D.approval.notes = el.value; toast('Notes saved'); }).catch(err => toast(err.message, true));
  }
});
// "Must mention" keyword boxes that are filters, not forms: Triage's and the Discovery table's.
const FILTER_TAGS = { triage: () => S.triage, flt: () => S.filters };
function commitTag(el, redraw = true) {
  const { form, tag } = el.dataset, max = Number(el.dataset.max || 20);
  if (FILTER_TAGS[form]) {
    const holder = FILTER_TAGS[form](), list = holder.keywords || [];
    S.forms.__t = { keywords: list }; addTags('__t', 'keywords', el.value, max, list); holder.keywords = S.forms.__t.keywords; delete S.forms.__t;
    el.value = ''; S.filters.page = 1; load(); return;
  }
  addTags(form, tag, el.value, max, tagFallback(form, tag)); el.value = '';
  if (redraw) render();
  else { const box = el.closest('.tagbox'); if (box) { box.querySelectorAll('.chip').forEach(c => c.remove()); el.insertAdjacentHTML('beforebegin', chips(form, tag, tagFallback(form, tag))); } }
}
document.addEventListener('keydown', e => {
  const el = e.target;
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); A.palette(); return; }
  if (e.key === 'Escape' && S.datePop) { S.datePop = false; render(); document.getElementById('flt-date')?.focus(); return; }
  if (e.key === 'Escape') { if (S.boardReview && !S.paletteOpen && !S.jobOpen && !S.kitJob) { S.boardReview = null; render(); return; } if (S.paletteOpen || S.jobOpen || S.kitJob) { S.paletteOpen = false; S.jobOpen = null; S.kitJob = null; render(); } return; }
  if (el.dataset && el.dataset.tag) {
    if (e.key === 'Enter' || (e.key === ',' && el.dataset.split !== 'newline')) { e.preventDefault(); if (el.value.trim()) commitTag(el); return; }
    if (e.key === 'Backspace' && !el.value) {
      const { form, tag } = el.dataset;
      if (FILTER_TAGS[form]) { FILTER_TAGS[form]().keywords.pop(); S.filters.page = 1; load(); return; }
      const list = [...(fv(form, tag, tagFallback(form, tag)) || [])]; list.pop(); setF(form, tag, list); render(); return;
    }
  }
  if (el.dataset && el.dataset.enter && e.key === 'Enter') { e.preventDefault(); consumedFields.add(el); A[el.dataset.enter](el, e); return; }
  if (el.dataset && el.dataset.palette && e.key === 'Enter') { const first = S.paletteJobs[0]; if (first) { S.paletteOpen = false; openJob(first.id); } return; }
  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName);
  if (typing || e.metaKey || e.ctrlKey || e.altKey || S.jobOpen || S.kitJob || S.paletteOpen) return;
  if (S.screen === 'discovery' && S.discMode === 'triage' && D.queue?.items[0]) {
    const id = D.queue.items[0].id;
    const map = { x: 'skip', m: 'maybe', s: 'shortlist' };
    if (map[e.key.toLowerCase()]) { e.preventDefault(); triageJob(id, map[e.key.toLowerCase()]); }
    else if (e.key === 'Enter') { e.preventDefault(); triageJob(id, 'shortlist', true); }
  }
});
document.addEventListener('paste', e => {
  const el = e.target;
  if (!el.dataset || !el.dataset.tag) return;
  const text = e.clipboardData.getData('text');
  if ((el.dataset.split === 'newline' ? /\n/ : /[,\n]/).test(text)) { e.preventDefault(); el.value = text; commitTag(el); }
});
// Tracker drag and drop
document.addEventListener('dragstart', e => { const card = e.target.closest?.('[data-drag-app]'); if (card) { e.dataTransfer.setData('text/plain', card.dataset.dragApp); e.dataTransfer.effectAllowed = 'move'; } });
document.addEventListener('dragover', e => { const col = e.target.closest?.('[data-drop-stage]'); if (col) { e.preventDefault(); document.querySelectorAll('.kcol.drop').forEach(c => c !== col && c.classList.remove('drop')); col.classList.add('drop'); } });
document.addEventListener('dragleave', e => { const col = e.target.closest?.('[data-drop-stage]'); if (col && !col.contains(e.relatedTarget)) col.classList.remove('drop'); });
document.addEventListener('drop', e => { const col = e.target.closest?.('[data-drop-stage]'); if (!col) return; e.preventDefault(); col.classList.remove('drop'); const id = e.dataTransfer.getData('text/plain'); if (id) moveStage(id, col.dataset.dropStage); });
window.addEventListener('hashchange', () => {
  const [screen, section] = location.hash.slice(1).split('/');
  if (!settingsCanLeave(screen, settingsSection(section))) { setHash(); return; }
  const before = S.screen + S.discMode + S.todayVariant + S.settingsSection + S.knowledgeSection + wkState().tab;
  readHash(); setHash();
  if (before !== S.screen + S.discMode + S.todayVariant + S.settingsSection + S.knowledgeSection + wkState().tab) {
    render(); load();
    if (S.screen === 'settings') document.getElementById('settings-heading')?.focus();
    if (S.screen === 'knowledge') document.getElementById('knowledge-heading')?.focus();
  }
});
window.addEventListener('beforeunload', e => {
  if ((S.screen === 'settings' && settingsAnyDirty()) || screenFormDirty()) { e.preventDefault(); e.returnValue = ''; }
});
document.addEventListener('submit', e => {
  if (e.target.id === 'knowledge-editor-form') { e.preventDefault(); knowledgeSave(); }
  if (e.target.id === 'settings-editor') { e.preventDefault(); settingsSave(e.target.dataset.form || settingsForm()); }
});
document.addEventListener('toggle', e => {
  const id = e.target.dataset?.settingsDetails;
  if (id && e.target.isConnected) { if (e.target.open) SETTING_DETAILS.add(id); else SETTING_DETAILS.delete(id); }
}, true);

/* ───────────── desktop notification: a job was approved ───────────── */
// The server says when the newest approval happened (summary.approved.latest_at); the browser remembers the last one it
// announced, so each approval raises one notification, a reload repeats none and history is never replayed.
const NOTIFY_KEYS = { on: 'jobhunt.notify.approved', seen: 'jobhunt.notify.approved.seen' };
// Where localStorage is blocked (some private or embedded contexts) the values live in memory for this page instead.
const lsMemory = new Map();
const lsGet = key => { try { const v = localStorage.getItem(key); if (v !== null) return v; } catch { /* use memory */ } return lsMemory.has(key) ? lsMemory.get(key) : null; };
const lsSet = (key, value) => { lsMemory.set(key, String(value)); try { localStorage.setItem(key, value); } catch { /* memory only */ } };
const notifySupported = () => typeof Notification !== 'undefined';
// On unless the person turned it off here; the browser's own permission still has the last word.
const notifyWanted = () => lsGet(NOTIFY_KEYS.on) !== 'off';
const notifyPermission = () => (notifySupported() ? Notification.permission : 'unsupported');
async function setNotifyApproved(on) {
  lsSet(NOTIFY_KEYS.on, on ? 'on' : 'off');
  if (on && notifySupported() && Notification.permission === 'default') await Notification.requestPermission();  // needs a click
  return notifyPermission();
}
function announceApprovals(summary) {
  if (!summary?.approved) return false;
  const latest = summary.approved.latest_at, seen = lsGet(NOTIFY_KEYS.seen);
  // First visit: what is already approved is not news. With no approval yet the baseline is empty (''), so the first
  // approval that ever happens is announced.
  if (seen === null) { lsSet(NOTIFY_KEYS.seen, latest || ''); return false; }
  if (!latest) return false;
  if (latest <= seen) return false;
  lsSet(NOTIFY_KEYS.seen, latest);
  if (!notifyWanted()) return false;
  const count = summary.approved.count, body = `${count} approved packet${count === 1 ? '' : 's'} ready: check them before you apply.`;
  if (notifySupported() && Notification.permission === 'granted') {
    const note = new Notification('Job approved', { body, tag: 'jobhunt-approved' });
    note.onclick = () => { window.focus?.(); go('approvals'); note.close?.(); };
  } else toast(`Job approved · ${body}`);
  return true;
}
async function refreshApprovals() {
  try { const s = await api('/api/workspace/summary'); D.summary = s; return announceApprovals(s); } catch { return false; }
}
// Slow and quiet: it runs in a hidden tab too (that is when a notification matters) and never redraws the screen.
const APPROVAL_POLL_MS = 30000;
let approvalTimer = null;
function watchApprovals() {
  clearInterval(approvalTimer);
  approvalTimer = setInterval(refreshApprovals, APPROVAL_POLL_MS);
}

/* ───────────── boot ───────────── */
readHash();
setHash();
render();
refreshTasks();
watchApprovals();
if (typeof watchFailover === 'function') watchFailover();
load().then(async () => {
  guideBoot();
  try { const prof = await api('/api/profile'); D.profileRoles = prof.target_roles || []; } catch { /* optional */ }
});
