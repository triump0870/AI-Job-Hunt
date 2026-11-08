/* Running workers: the account's background queues — JD parsing, JD recovery, ranking, the qualification graph,
   tailoring and its review step, the application worker, and the retrieval indexer — in one screen,
   with the controls to unstick them. Loaded before app.js; uses the workspace helpers (api, esc, btn, pill, render, go)
   and reuses the views each queue already had: the JD parsing and recovery panels (monitoring.js), the ranking tracker
   and the tailoring task list (app.js).

   Kill stops a task and takes it out of the queue: it ends as Stopped and no sweep picks its job up again until
   someone retries it (Retry, Run now, bulk Retry); killing all of a worker's
   tasks also clears the account's rate-limit cooldowns. Run now puts one task first in line, past its retry wait, the
   ranking cadence and any provider cooldown. Every number and row comes from /api/workers/* (app.services.workers). */
'use strict';

const WK_KINDS = [['classification', 'Domain check'], ['parsing', 'JD parsing'], ['recovery', 'JD recovery'], ['ranking', 'Ranking'], ['qualification', 'Qualification'], ['insights', 'Job insights'], ['tailoring', 'Tailoring'], ['reviewing', 'Reviewing'], ['application', 'Application'], ['indexing', 'Indexing'], ['resume_parsing', 'Resume parsing']];
// Tabs drawn from the generic task list (/api/workers/{kind}); ranking and tailoring keep their own views.
const WK_GENERIC = ['classification', 'parsing', 'recovery', 'qualification', 'insights', 'reviewing', 'application', 'indexing', 'resume_parsing'];
// Tabs whose Performance card comes from /api/workers/{kind}/performance (parsing and recovery bring their own metrics).
const WK_PERF = ['ranking', 'insights', 'tailoring', 'reviewing'];
// The worker tabs, then the account's worker settings (processing limits and rate-limit backoff).
const WK_TABS = [...WK_KINDS, ['settings', 'Settings']];
// [label, tone, symbol]: a status is always a word and a symbol, never colour alone.
const WK_STATUS = { RUNNING: ['Running', 'brand', '●'], QUEUED: ['Queued', 'muted', '○'], RETRYING: ['Retrying', 'warn', '↻'],
  COMPLETED: ['Done', 'good', '✓'], SUCCEEDED: ['Done', 'good', '✓'], PARTIAL: ['Partial', 'warn', '◐'], FAILED: ['Failed', 'bad', '✕'],
  BLOCKED: ['Blocked', 'bad', '⛔'], SKIPPED: ['Skipped', 'muted', '–'], CANCELLED: ['Cancelled', 'muted', '–'],
  STOPPED: ['Stopped', 'muted', '■'] };
const WK_POLL_ACTIVE = 3000, WK_POLL_IDLE = 20000;
// Status filters, the same as the server's (app.services.workers.STATUS_FILTERS).
const WK_FILTERS = [['all', 'All statuses'], ['active', 'Active (running, queued, retrying)'], ['running', 'Running'],
  ['queued', 'Queued'], ['retrying', 'Retrying'], ['failed', 'Failed, blocked or stopped'], ['done', 'Done']];
const WK_FILTER_MEMBERS = { active: ['RUNNING', 'QUEUED', 'RETRYING'], running: ['RUNNING'], queued: ['QUEUED'],
  retrying: ['RETRYING'], failed: ['FAILED', 'BLOCKED', 'STOPPED'], done: ['COMPLETED', 'PARTIAL', 'SUCCEEDED', 'SKIPPED', 'CANCELLED'] };
let wkTimer = null, wkSearchTimer = null;

function wkState() {
  if (!S.wk) S.wk = { tab: 'parsing', range: '7d' };
  if (!S.wk.f) S.wk.f = {};
  return S.wk;
}
// Each tab keeps its own search and status. Ranking searches through the tracker's own filter (S.rf).
function wkFilter(tab = wkState().tab) {
  const f = wkState().f;
  if (!f[tab]) f[tab] = { q: '', status: 'all' };
  return f[tab];
}
const wkMatches = (status, filter) => filter === 'all' || (WK_FILTER_MEMBERS[filter] || []).includes(status);
const wkCounted = statuses => Object.fromEntries([['all', statuses.length],
  ...Object.entries(WK_FILTER_MEMBERS).map(([name, members]) => [name, statuses.filter(s => members.includes(s)).length])]);
// The same words for the ranking tracker's and the tailoring list's own task fields.
function wkRankStatus(t) {
  if (t.status === 'PROCESSING') return 'RUNNING';
  if (t.status === 'PENDING') return t.attempt_count > 0 || later(t.next_attempt_at) ? 'RETRYING' : 'QUEUED';
  return wkStoppedRank(t) ? 'STOPPED' : t.status;
}
const wkTailorStatus = t => wkStoppedTailor(t) ? 'STOPPED' : ({ running: 'RUNNING', queued: 'QUEUED' })[t.status] || t.status.toUpperCase();
const wkText = (t, q) => { const x = (q || '').trim().toLowerCase(); if (!x) return true;
  if (/^#?\d+$/.test(x)) return String(t.job_id) === x.replace('#', '');
  return `${t.title || ''} ${t.company || ''}`.toLowerCase().includes(x); };
const wkSpec = kind => (D.wk?.workers || []).find(w => w.kind === kind);

/* ───────────── loading and polling ───────────── */
async function loadWorkers() {
  const w = wkState(), tab = w.tab, worker = WK_KINDS.some(([id]) => id === tab);
  // Ranking and tailoring keep the views they always had (the ranking tracker, the tailoring task list).
  const f = wkFilter(tab);
  const detail = WK_GENERIC.includes(tab)
    ? api(`/api/workers/${tab}?` + qs({ range: w.range, q: f.q.trim(), status: f.status === 'all' ? '' : f.status })) : null;
  const perf = WK_PERF.includes(tab) ? api(`/api/workers/${tab}/performance?` + qs({ range: w.range })).catch(() => null) : null;
  const [summary, data, act, performance] = await Promise.all([api('/api/workers'), detail,
    worker ? api(`/api/workers/${tab}/activity`).catch(() => null) : null, perf,
    tab === 'ranking' ? loadRanking() : null, tab === 'tailoring' ? refreshTasks() : null,
    tab === 'settings' ? Promise.all([D.settings ? null : api('/api/settings').then(x => { D.settings = x; }),
      api('/api/workers/settings').then(x => { D.wkSettings = x; })]) : null]);
  D.wk = summary;
  if (data) D.wkData = { ...(D.wkData || {}), [tab]: data };
  if (act) D.wkAct = { ...(D.wkAct || {}), [tab]: act };
  if (performance) D.wkPerf = { ...(D.wkPerf || {}), [tab]: performance };
  const h = w.history;
  if (h && h.kind === tab) D.wkHistory = await api(`/api/workers/${h.kind}/tasks/${h.id}/history`).catch(e => ({ error: e.message }));
  scheduleWkPoll();
}
/* ───────────── a task's history (click its name) ───────────── */
const WK_SOURCE_ICON = { task: '◆', attempt: '↻', step: '▸', browser: '▣', model: '●', workflow: '→', application: '✎', intent: '⚑' };
function wkHistoryPanel(tab) {
  const h = wkState().history;
  if (!h || h.kind !== tab) return '';
  const d = D.wkHistory;
  if (!d) return `<section class="card mb12">${loading()}</section>`;
  if (d.error) return `<section class="card mb12"><div class="row between"><div class="small">${esc(d.error)}</div>${btn('Close', 'wk-history-close', {}, 'sm')}</div></section>`;
  const r = d.task, tone = { good: 'var(--good)', warn: 'var(--warn)', bad: 'var(--bad)', brand: 'var(--brand)', muted: 'var(--line)' };
  const actions = [wkButtons(tab, r.task_id, { kill: r.can_kill, runNow: r.can_run_now }),
    tab === 'application' && r.job_id != null ? btn('Open Apply kit', 'open-kit', { id: r.job_id }, 'sm') : '',
    tab === 'application' && r.job_id != null && d.browser_use_ready ? `<span title="${attr('Opens a browser window (browser-use) that fills the form from your verified answers, checked step by step. It never submits: you do.')}">${btn(label('kit-browser-use', 'Run with browser-use', 'Queueing…'), 'kit-browser-use', { id: r.job_id }, 'sm', isBusy('kit-browser-use'))}</span>` : ''].join('');
  return `<section class="card mb12 wk-history" aria-label="Task history"><div class="row between wrap gap10" style="padding:14px 18px;border-bottom:1px solid var(--line2)">
      <div><div class="card-title" style="font-size:16px">${wkTaskName(r)}</div><div class="card-sub">Task #${r.task_id} · ${esc(wkDetail(r))}</div></div>
      <div class="row gap8 wrap">${actions}${btn('Close', 'wk-history-close', {}, 'sm')}</div></div>
    ${wkHistoryLogs(d)}
    <ol style="list-style:none;margin:0;padding:10px 18px;max-height:420px;overflow:auto">${(d.events || []).map(e => `<li class="row gap10" style="padding:6px 0;border-bottom:1px solid var(--line2);align-items:flex-start">
      <span aria-hidden="true" style="color:${tone[e.tone] || tone.muted};width:16px;text-align:center">${WK_SOURCE_ICON[e.source] || '•'}</span>
      <span class="xs muted mono" style="min-width:150px">${esc(fmtDateTime(e.at))}</span>
      <span class="grow"><span class="small">${esc(e.title)}</span>${e.detail ? `<span class="xs muted"> · ${esc(e.detail)}</span>` : ''}</span></li>`).join('') || '<li class="small muted">Nothing recorded yet.</li>'}</ol></section>`;
}
// Every few seconds while anything runs, slowly when idle, never on another screen or in a hidden tab.
function wkPollDelay() {
  if (S.screen !== 'workers' || document.hidden || wkState().tab === 'settings') return null;
  return D.wk?.active ? WK_POLL_ACTIVE : WK_POLL_IDLE;
}
function stopWkPoll() { clearTimeout(wkTimer); wkTimer = null; }
function scheduleWkPoll() {
  stopWkPoll();
  const delay = wkPollDelay();
  if (delay != null) wkTimer = setTimeout(wkPoll, delay);
}
async function wkPoll() {
  if (S.screen !== 'workers') return stopWkPoll();
  try { await loadWorkers(); } catch { scheduleWkPoll(); return; }
  if (S.screen === 'workers') wkPollRender();
}
// A background refresh must never redraw a dropdown the person has open: replacing a <select> closes its list (the
// status filter could not be picked while tasks were running). Hold the redraw until they leave the control.
let wkRenderHeld = false;
const wkPicking = () => document.activeElement?.tagName === 'SELECT' && !!document.activeElement.closest?.('#view');
function wkPollRender() {
  if (wkPicking()) { wkRenderHeld = true; return; }
  wkRenderHeld = false;
  render();
}
document.addEventListener('focusout', () => setTimeout(() => {
  if (wkRenderHeld && !wkPicking() && S.screen === 'workers') wkPollRender();
}, 0));
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopWkPoll();
  else if (S.screen === 'workers' && D.wk) wkPoll();
});

/* ───────────── pieces ───────────── */
const wkPill = (text, tone, sym) => `<span class="orch-chip ${tone}"><span aria-hidden="true">${sym}</span> ${esc(text)}</span>`;
const wkChip = status => wkPill(...(WK_STATUS[status] || [status, 'muted', '•']));
const wkCount = c => (c?.running || 0) + (c?.queued || 0) + (c?.retrying || 0);
const WK_COLORS = { running: 'var(--brand)', queued: '#b9bfd3', retrying: 'var(--amber)', done: 'var(--good-btn)', failed: 'var(--bad)' };
const wkNum = n => (n || 0).toLocaleString();
const wkDur = s => s == null ? '—' : s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`;
const wkPct = r => r == null ? '—' : `${Math.round(r * 1000) / 10}%`;
const wkTone = r => r == null ? '' : r >= 0.9 ? 'good' : r >= 0.7 ? 'warn' : 'bad';
const wkMeter = (ratio, color) => `<div class="wk-meter" role="presentation"><i style="width:${Math.max(0, Math.min(100, (ratio || 0) * 100))}%;background:${color}"></i></div>`;
const wkHumanize = code => String(code || '').toLowerCase().replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());
function wkScheduleFacts(s, kind) {
  if (!s) return ['Scheduler not running on this server'];
  if (kind === 'reviewing') return s.enabled ? ['✎ runs as the last step of each Tailoring task', `⇉ up to ${s.concurrency} at a time`] : ['The tailoring worker is turned off on this server'];
  if (kind === 'tailoring') return s.enabled ? [`⏱ checks every ${s.poll_seconds}s`, `⇉ up to ${s.concurrency} at a time`] : ['The tailoring worker is turned off on this server'];
  if (!s.enabled || !s.interval_minutes) return ['This worker is turned off on this server'];
  return [`⏱ every ${s.interval_minutes} min`, s.running ? '▶ running now' : s.next_run_at ? `▶ next run ${until(s.next_run_at)}` : null,
    s.last_finished_at ? `✓ last finished ${fmtDateTime(s.last_finished_at)}` : null].filter(Boolean);
}
// Kept for anything that wants the schedule as one line.
const wkScheduleText = (s, kind) => wkScheduleFacts(s, kind).map(x => x.replace(/^[⏱▶✓⇉] /, '')).join(' · ');
function wkCooldowns(list) {
  if (!list?.length) return '';
  return `<div class="banner warn" role="status"><span class="grow">Rate-limited: ${list.map(c => `<b>${esc(c.provider.toLowerCase())}</b> ${c.seconds_left}s left${c.calls_skipped ? ` (${c.calls_skipped} calls held back)` : ''}`).join(' · ')}. Waiting tasks resume when it ends.</span>
    ${btn(label('wkCooldown', 'Reset backoff', 'Resetting…'), 'wk-reset-cooldowns', {}, 'sm', isBusy('wkCooldown'))}</div>`;
}
// An application run waiting for the person (it is not claimable, so it is not "waiting its turn").
// The Monitoring → Logs lines this task's runs wrote: one button per run's trace, newest first.
function wkHistoryLogs(d) {
  const ids = d.trace_ids || [];
  if (!ids.length) return '<div class="xs muted" style="padding:8px 18px;border-bottom:1px solid var(--line2)">No log lines were written for this task.</div>';
  return `<div class="row gap6 wrap" style="padding:8px 18px;border-bottom:1px solid var(--line2);align-items:center"><span class="xs muted">${d.log_count ? `${d.log_count} log line${d.log_count === 1 ? '' : 's'} in Monitoring → Logs` : 'Its runs in Monitoring → Logs'}:</span>
    ${ids.map((id, i) => btn(ids.length === 1 ? 'View logs' : `${i === 0 ? 'Latest run' : `Run ${ids.length - i}`} · ${id.slice(0, 8)}`, 'wk-logs', { id }, 'sm')).join('')}</div>`;
}
function wkWaitingText(r) {
  const w = r.waiting_detail || {};
  if (w.kind === 'NEEDS_HUMAN' && w.fields) return `Waiting for you in the browser window: fill ${w.fields} highlighted field${w.fields === 1 ? '' : 's'}${w.reason === 'FIELDS_NEED_YOU' ? ': it carries on by itself when you finish, or press Continue' : ', then press Continue'}${w.expires_at ? ` · until ${fmtDateTime(w.expires_at)}` : ''}`;
  if (w.kind === 'NEEDS_HUMAN') return `Waiting for you to finish in the browser window${w.reason ? ` (${wkHumanize(w.reason).toLowerCase()})` : ''}, then press Continue${w.expires_at ? ` · until ${fmtDateTime(w.expires_at)}` : ''}`;
  const parts = [w.questions ? `answer ${w.questions} question${w.questions === 1 ? '' : 's'} in the Knowledge bank (and verify them)` : '', w.confirmations ? `confirm ${w.confirmations} saved answer${w.confirmations === 1 ? '' : 's'} in the Apply kit` : ''].filter(Boolean);
  return `Waiting for you: ${parts.join(', then ') || 'see the Apply kit'}. It continues by itself`;
}
function wkDetail(r) {
  if (r.waiting_for === 'YOU') return wkWaitingText(r);
  const of = r.max_attempts ? ` of ${r.max_attempts}` : '';
  const error = r.last_error ? ` · ${r.last_error}` : '';
  if (r.run_now && ['QUEUED', 'RETRYING'].includes(r.status)) return `First in line: runs on the next pass, without waiting${r.attempt ? ` · attempt ${r.attempt}${of} so far${error}` : ''}`;
  if (r.status === 'RUNNING') return `Attempt ${r.attempt}${of} · running ${r.started_at ? wkDur(Math.max(0, (Date.now() - toDate(r.started_at)) / 1000)) : ''}${r.stage && r.stage !== 'running' ? ` · ${r.stage}` : ''}`;
  if (r.status === 'RETRYING') return `${r.next_attempt_at && later(r.next_attempt_at) ? `Retries ${until(r.next_attempt_at)}` : 'Retries on the next run'} · attempt ${r.attempt}${of}${r.error_category ? ` (${r.error_category.toLowerCase().replace(/_/g, ' ')})` : ''}${error}`;
  if (r.status === 'QUEUED') return r.run_now ? 'First in line: runs on the next pass' : `Waiting its turn${r.queue_position ? ` · #${r.queue_position}` : ''}${r.created_at ? ` · queued ${fmtDateTime(r.created_at)}` : ''}`;
  const score = r.reviewer_score != null ? ` · reviewer score ${r.reviewer_score}` : '';
  const again = r.retried_by ? ` · run again as task #${r.retried_by.task_id} (${(WK_STATUS[r.retried_by.status] || [r.retried_by.status])[0].toLowerCase()})` : '';
  return `${(WK_STATUS[r.status] || [r.status])[0]}${r.finished_at ? ` ${fmtDateTime(r.finished_at)}` : ''}${again}${score}${r.result_code ? ` · ${r.result_code.toLowerCase().replace(/_/g, ' ')}` : ''}${error}`;
}
// Why a task is waiting or retrying, in full: rows cut the message to one line, the ⓘ beside them shows all of it.
function wkWhyText({ waiting, error, category, attempt, max, next, reason }) {
  return [waiting ? 'Why it is waiting' : 'Why it is retrying', error,
    category ? `Kind: ${wkHumanize(category)}` : null,
    !waiting && attempt ? `Attempt ${attempt}${max ? ` of ${max}` : ''} used` : null,
    next ? (later(next) ? `Next try ${until(next)} · ${fmtDateTime(next)}` : 'Next try on the next run') : null,
    reason ? `Queued because: ${reason}` : null].filter(Boolean).join('\n');
}
const wkInfo = text => text ? `<button type="button" class="wk-info" data-tip="${attr(text)}" aria-label="${attr(text)}">i</button>` : '';
// A row of the Running workers table (parsing, recovery) that is retrying, or waiting with a reason.
function wkRowWhy(r) {
  if (!(r.status === 'RETRYING' || (r.status === 'QUEUED' && r.last_error))) return '';
  return wkInfo(wkWhyText({ waiting: r.status === 'QUEUED', error: r.last_error, category: r.error_category, attempt: r.attempt,
    max: r.max_attempts, next: r.next_attempt_at, reason: r.reason ? wkHumanize(r.reason) : null }));
}
// A ranking tracker task (app.js renderRanking) waiting for its retry or for the account to be ready.
function wkRankWhy(t) {
  if (t.status !== 'PENDING' || !(t.last_error || later(t.next_attempt_at))) return '';
  return wkInfo(wkWhyText({ waiting: !t.attempt_count, error: t.last_error, category: t.error_category, attempt: t.attempt_count,
    max: t.max_attempts, next: t.next_attempt_at, reason: RANK_REASON[t.reason] || t.reason }));
}
// Attempts as dots: used ones amber, the one running now brand.
function wkTries(r) {
  const max = Math.min(r.max_attempts || 0, 8);
  if (!max || !['RUNNING', 'RETRYING', 'QUEUED'].includes(r.status) || (!r.attempt && r.status === 'QUEUED')) return '';
  const used = Math.min(r.attempt || 0, max);
  return `<span class="wk-tries" title="Attempt ${r.attempt || 0} of ${r.max_attempts}" aria-label="Attempt ${r.attempt || 0} of ${r.max_attempts}">${[...Array(max)].map((_, i) =>
    `<i class="${i < used ? (r.status === 'RUNNING' && i === used - 1 ? 'now' : 'used') : ''}"></i>`).join('')}</span>`;
}
// The two controls, the same on every worker's rows. `id` is the task id.
function wkButtons(kind, id, { kill, runNow }) {
  return (runNow ? btn(label(`wkRun${kind}${id}`, 'Run now', 'Starting…'), 'wk-run-now', { kind, id }, 'sm soft', isBusy(`wkRun${kind}${id}`)) : '')
    + (kill ? btn(label(`wkKill${kind}${id}`, 'Kill', 'Stopping…'), 'wk-kill', { kind, id }, 'sm danger', isBusy(`wkKill${kind}${id}`)) : '');
}
// Controls on the ranking tracker's rows (app.js renderRanking), from the tracker's own task fields.
function wkRankActions(t) {
  const retrying = t.status === 'PENDING' && (t.attempt_count > 0 || later(t.next_attempt_at));
  return wkButtons('ranking', t.id, { kill: t.status === 'PROCESSING' || retrying, runNow: t.status === 'PENDING' });
}
// Controls on the tailoring task list (app.js renderTaskStack).
function wkTailorActions(t) {
  return wkButtons('tailoring', t.id, { kill: t.status === 'running', runNow: t.status === 'queued' });
}
// When a task last ran: the start of its latest attempt (ranking keeps it in last_attempt_at, because a ranking
// waiting for its account clears started_at). A task that has never started says so.
const wkAgo = iso => {
  const sec = Math.max(0, Math.round((Date.now() - toDate(iso)) / 1000));
  return sec < 45 ? 'just now' : sec < 3600 ? `${Math.max(1, Math.round(sec / 60))}m ago` : sec < 86400 ? `${Math.round(sec / 3600)}h ago` : `${Math.round(sec / 86400)}d ago`;
};
function wkLastRun(t) {
  const at = t?.last_attempt_at || t?.started_at;
  if (!at) return '<div class="wk-last never"><small>Not run yet</small></div>';
  return `<div class="wk-last" title="${attr(`Last run ${fmtDateTime(at)}`)}"><b>${esc(fmtDateTime(at))}</b><small>${esc(wkAgo(at))}</small></div>`;
}
// The model a task uses (or its next request will use), and the route it goes by.
const WK_CHANGE = { FAILOVER: ['Failed over', 'bad'], RESTING: ['Using a backup', 'warn'], FALLBACK: ['Fallback', 'warn'], ESCALATED: ['Escalated', 'brand'], CONFIG: ['Model changed', 'brand'] };
const WK_ACTIVE = ['RUNNING', 'QUEUED', 'RETRYING', 'running', 'queued', 'PENDING', 'PROCESSING'];
function wkModel(r) {
  const m = r.model;
  if (m?.source === 'none') return '<div class="wk-model nocall"><small>No model call</small></div>';
  if (!m?.model) return '<div class="wk-model empty" aria-hidden="true"></div>';
  const verb = m.source === 'used' ? (WK_ACTIVE.includes(r.status) && r.waiting_for !== 'APPROVAL' ? 'in use' : 'used') : 'next request';
  return `<div class="wk-model" title="${attr(`${m.worker ? `${m.worker} · ` : ''}${m.source === 'used' ? 'In use' : 'Next request'}: ${m.model} via ${m.route_label}${m.via ? ` (${m.via})` : ''}${m.backups?.length ? ` · backups ${m.backups.join(', ')}` : ''}`)}">
    <span class="wk-route ${m.route === 'LITELLM' ? 'gw' : 'direct'}">${esc(m.route_label)}</span><span class="mono ellipsis">${esc(m.model)}</span>
    <small>${verb}${m.via ? ` · ${esc(m.via)}` : ''}</small></div>`;
}
function wkModelChange(r) {
  const c = r.model_change;
  if (!c) return `<div class="wk-change none">${r.model?.model ? '<span class="faint xs">No change</span>' : ''}</div>`;  // finished without a model call: nothing to say
  const [label, tone] = WK_CHANGE[c.kind] || [c.kind, 'muted'];
  return `<div class="wk-change t-${tone}" title="${attr(`${label}: ${c.from || '?'} → ${c.to || '?'} — ${c.reason || ''}`)}"><b>${esc(label)}</b>
    <span class="mono ellipsis">${c.from ? `<s>${esc(c.from)}</s> → ` : ''}${esc(c.to || '—')}</span></div>`;
}
// The same two facts for the ranking tracker and the tailoring list, which lay their rows out their own way.
function wkModelCells(t) {
  return t?.model?.model || t?.model?.source === 'none' ? `<div class="wk-cells">${wkModel(t)}${wkModelChange(t)}</div>` : '';
}
// A task's name: its job (title · company #id), or, for a task without a job (the retrieval indexer), what the server
// labels it — "Evidence #12 · index v3" — never the fact's text.
function wkTaskName(r) {
  if (r.job_id == null) return `${esc(r.title || `Task #${r.task_id}`)} <span class="muted" style="font-weight:400">· ${esc(r.company || '')}</span>${r.subject?.sensitive ? ' <span class="orch-chip muted" title="Only the question is embedded">question only</span>' : ''}`;
  return `${esc(r.title || `Job #${r.job_id}`)} <span class="muted" style="font-weight:400">· ${esc(r.company || '')}</span> <span class="xs faint mono" style="font-weight:400">#${r.job_id}</span>`;
}
function wkTable(kind, data) {
  const rows = data?.rows || [], f = wkFilter(kind), filtered = f.q.trim() || f.status !== 'all';
  if (!rows.length) {
    return `<section class="card">${filtered
      ? empty('No tasks match these filters', 'Try another status or search, or clear the filters.', btn('Clear filters', 'wk-clear-filters', { tab: kind }, 'primary'))
      : empty('Nothing in this queue', 'Tasks show up here while they wait or run, and for a day after they finish.')}</section>`;
  }
  const active = rows.filter(r => ['RUNNING', 'QUEUED', 'RETRYING'].includes(r.status)).length;
  const shown = data.total > rows.length ? ` · showing the first ${rows.length} of ${data.total}` : '';
  return `<section class="card flush"><div class="row between wrap gap10" style="padding:14px 18px;border-bottom:1px solid var(--line2)">
      <div><div class="card-title" style="font-size:16px">Tasks${filtered ? ' matching your filters' : ''}</div><div class="card-sub">${active} waiting, running or retrying · ${rows.length - active} finished in the last day${shown}</div></div>${wkShownNudge(kind, rows)}</div>
    <div style="max-height:560px;overflow:auto" data-scroll-key="wk-${kind}"><div class="wk-head" aria-hidden="true"><div></div><div>Status</div><div>Task</div><div>Last run</div><div>Model &amp; route</div><div>Change</div><div></div></div>${rows.map(r => `<div class="wk-row ${r.status === 'RUNNING' ? 'running' : ''} ${wkSel(kind).has(r.task_id) ? 'picked' : ''}">
      <div>${['FAILED', 'BLOCKED', 'STOPPED'].includes(r.status) && !r.retried_by ? wkSelectBox(kind, r.task_id, r.title) : ''}</div><div>${r.waiting_for === 'YOU' ? wkPill('Waiting for you', 'warn', '◷') : wkChip(r.status)}</div>
      <div style="min-width:0"><div class="small b ellipsis"><button type="button" class="linklike" style="all:unset;cursor:pointer" data-act="wk-history" data-kind="${attr(kind)}" data-id="${r.task_id}" title="Show what happened to this task">${wkTaskName(r)}</button></div>
        <div class="wk-detail xs ${['FAILED', 'BLOCKED'].includes(r.status) ? 'orch-bad' : 'muted'}"><span class="ellipsis" title="${attr(wkDetail(r))}">${wkTries(r)}${esc(wkDetail(r))}</span>${wkRowWhy(r)}</div></div>
      ${wkLastRun(r)}${wkModel(r)}${wkModelChange(r)}
      <div class="wk-row-actions">${r.retried_by ? `<span class="orch-chip muted" title="${attr(`A newer task (#${r.retried_by.task_id}) is handling this job`)}">↻ Run again</span>` : ''}${wkButtons(kind, r.task_id, { kill: r.can_kill, runNow: r.can_run_now })}${wkRowNudge(kind, r.job_id, r.status)}${wkTakeoverContinue(kind, r)}${r.waiting_for === 'YOU' && r.job_id != null ? btn(r.waiting_detail?.kind === 'NEEDS_HUMAN' ? 'Open Apply kit' : 'Answer questions', 'open-kit', { id: r.job_id }, wkTakeoverContinue(kind, r) ? 'sm' : 'sm primary') : ''}${r.job_id != null ? btn('Job', 'open-job', { id: r.job_id }, 'sm') : ''}</div></div>`).join('')}</div></section>`;
}

/* ───────────── overview: hero, queue and the last 24 hours ───────────── */
// What paces this worker, beside its schedule: concurrency and cadence, with a link to change the ones you own.
function wkLimits(tab) {
  if (tab === 'ranking' && D.ranking) return [`⇉ up to ${D.ranking.concurrency} at a time`, D.ranking.interval_minutes ? `↻ your cadence: every ${D.ranking.interval_minutes} min` : null].filter(Boolean);
  if (tab === 'recovery') { const x = D.wkData?.recovery?.diagnostics?.settings; return x ? [`⇉ ${x.max_concurrency} at a time · batches of ${x.batch_size}`] : []; }
  return [];
}
// Rank / run everything that is waiting, now, in bulk (not for tailoring, which has no wait).
function wkRunQueuedButton(tab, spec) {
  if (!['classification', 'parsing', 'recovery', 'ranking', 'qualification', 'insights', 'indexing', 'resume_parsing'].includes(tab)) return '';
  const c = spec.counts || {}, n = (c.queued || 0) + (c.retrying || 0), key = 'wkRunQueued' + tab;
  const verb = tab === 'ranking' ? 'Rank' : 'Run';
  return `<span title="${attr(n ? `${verb} the ${n} queued or waiting task${n === 1 ? '' : 's'} now: no retry wait${tab === 'ranking' ? ', no cadence' : ''}. A provider cooldown still applies.` : 'Nothing is queued')}">${btn(label(key, `${verb} ${n || ''} queued now`.replace('  ', ' '), 'Starting…'), 'wk-run-queued', { kind: tab, n }, 'sm', !n || isBusy(key))}</span>`;
}
// An application run waiting for you in its browser window: say you are done from here, without opening the Apply kit
// (the same Continue; the run reads the page again and checks everything before it acts).
function wkTakeoverContinue(kind, r) {
  const w = r.waiting_detail;
  if ((r.kind || kind) !== 'application' || r.waiting_for !== 'YOU' || !w || !w.takeover || w.continued) return '';
  return btn(label(`wkCont${r.task_id}`, 'I’m done — continue', 'Continuing…'), 'wk-continue', { id: r.task_id }, 'sm primary', isBusy(`wkCont${r.task_id}`));
}
// A finished task's own Nudge: enabled while its job is waiting for the next stage, otherwise it says why it is not.
const WK_OK = ['COMPLETED', 'SUCCEEDED', 'PARTIAL'];
function wkRowNudge(kind, jobId, status) {
  const next = wkSpec(kind)?.next_stage;
  if (!next || !WK_OK.includes(status)) return '';
  if ((next.job_ids || []).includes(jobId)) return btn(label('wkNudge' + kind + jobId, `→ ${next.to}`, 'Nudging…'), 'wk-nudge', { kind, job: jobId }, 'sm primary', isBusy('wkNudge' + kind + jobId));
  return `<span class="orch-chip muted" title="${attr(`Already in ${next.to}, or not eligible for it`)}">✓ moved on</span>`;
}
// One button for every finished task the list shows whose job is waiting for the next stage.
function wkShownNudge(kind, rows) {
  const next = wkSpec(kind)?.next_stage;
  if (!next) return '';
  const ids = [...new Set(rows.filter(r => WK_OK.includes(r.status) && (next.job_ids || []).includes(r.job_id)).map(r => r.job_id))];
  if (!ids.length) return '';
  return btn(label('wkNudgeShown' + kind, `Nudge ${ids.length} shown to ${next.to}`, 'Nudging…'), 'wk-nudge', { kind, jobs: ids.join(',') }, 'sm primary', isBusy('wkNudgeShown' + kind));
}
// Nudge forward: move this worker's finished jobs that never reached the next stage on to it (insights has none).
function wkNudgeButton(tab, spec) {
  const next = spec.next_stage;
  if (!next) return '';
  const n = next.waiting || 0, key = 'wkNudge' + tab;
  const title = n ? `${n} finished job${n === 1 ? '' : 's'} not yet in ${next.to}: move them now` : `Nothing is waiting between ${spec.label} and ${next.to}`;
  return `<span title="${attr(title)}">${btn(label(key, `Nudge to ${next.to}${n ? ` · ${n}` : ''}`, 'Nudging…'), 'wk-nudge', { kind: tab }, `sm ${n ? 'primary' : ''}`, !n || isBusy(key))}</span>`;
}
function wkHero(tab, spec, act) {
  const c = spec.counts || {}, s = spec.scheduler, live = c.running > 0;
  const stoppable = (c.running || 0) + (c.retrying || 0);
  const finished = act ? act.done + act.failed : null;
  const meta = [...wkScheduleFacts(s, tab), ...wkLimits(tab)].map(x => `<span>${esc(x)}</span>`).join('')
    + `<span><a href="#workers/settings">Settings</a></span>`
    + (s?.last_status === 'failed' ? '<span class="bad">✕ last run failed</span>' : '');
  const kpi = (name, value, sub, tone, sym) => `<div class="wk-kpi" data-tone="${value ? tone : ''}"><div class="wk-kpi-label"><span aria-hidden="true">${sym}</span>${esc(name)}</div>
    <div class="wk-kpi-value ${value ? '' : 'zero'}">${value == null ? '—' : wkNum(value)}</div><div class="wk-kpi-sub">${esc(sub)}</div></div>`;
  const active = wkCount(c);
  const parts = [['running', 'Running', c.running], ['queued', 'Queued', c.queued], ['retrying', 'Retrying', c.retrying]];
  const queue = active
    ? `<div class="wk-stack" role="img" aria-label="${attr(parts.map(([, n, v]) => `${v || 0} ${n.toLowerCase()}`).join(', '))}">${parts.filter(([, , v]) => v).map(([k, n, v]) =>
        `<i style="flex:${v};background:${WK_COLORS[k]}" data-tip="${attr(`${n}: ${v}`)}"></i>`).join('')}</div>
      <div class="wk-legend">${parts.map(([k, n, v]) => `<span><i style="background:${WK_COLORS[k]}"></i>${n} ${v || 0} · ${Math.round(100 * (v || 0) / active)}%</span>`).join('')}</div>`
    : '<div class="wk-stack"></div><div class="wk-legend"><span>Queue is empty: nothing waiting or running.</span></div>';
  return `<section class="card wk-hero">
    <div class="wk-hero-head"><div style="min-width:0;flex:1">
        <div class="wk-hero-title"><span class="wk-pulse ${live ? 'on' : ''}" aria-hidden="true"></span>${esc(spec.label || '')}${live ? ' <span class="orch-chip brand">● working</span>' : active ? ' <span class="orch-chip muted">○ waiting</span>' : ' <span class="orch-chip good">✓ idle</span>'}${spec.paused ? ' <a class="orch-chip warn" href="#workers/settings">⏸ paused for your account</a>' : ''}</div>
        <div class="card-sub mt4">${esc(spec.description || '')}</div></div>
      <div class="row gap8 wrap">${wkRunQueuedButton(tab, spec)}${wkNudgeButton(tab, spec)}${btn(label('wkKillAll' + tab, `Kill all ${spec.label || ''}`.trim(), 'Stopping…'), 'wk-kill-all', { kind: tab }, 'sm danger', !stoppable || isBusy('wkKillAll' + tab))}
        ${btn(label('wkRefresh', 'Refresh', 'Refreshing…'), 'wk-refresh', {}, 'sm', isBusy('wkRefresh'))}</div></div>
    <div class="wk-meta">${meta}</div>
    <div class="wk-kpis">${kpi('Running', c.running || 0, 'right now', 'brand', '●')}${kpi('Queued', c.queued || 0, 'waiting their turn', 'muted', '○')}
      ${kpi('Retrying', c.retrying || 0, 'backing off after an error', 'warn', '↻')}
      ${kpi('Finished · 24h', finished, act ? `${wkNum(act.done)} done · ${wkNum(act.failed)} failed` : 'loading…', act && act.failed > act.done ? 'bad' : 'good', '✓')}</div>
    <div><div class="caps-sm mb8">QUEUE NOW</div>${queue}</div>
    <details class="wk-help"><summary>How Kill and Run now work</summary><ul>
      <li><b>Kill</b> stops the task and takes it out of the queue. It ends as <b>Stopped</b>, and nothing picks its job up again until you press Run now or Retry.</li>
      <li>A run already talking to a model is abandoned, not interrupted — whatever it returns is discarded.</li>
      <li><b>Kill all</b> also clears your rate-limit backoff, so waiting tasks can start at once.</li>
      <li><b>Run now</b> puts a task first in line, past its retry wait${tab === 'ranking' ? ', your ranking cadence' : ''} and any provider cooldown.</li></ul></details>
  </section>`;
}
// Hour-by-hour columns: finished done and failed stacked, what was queued as a dashed column behind them.
function wkActivity(act) {
  if (!act) return `<section class="card">${loading()}</section>`;
  const hours = act.hours || [];
  const max = Math.max(1, ...hours.map(h => Math.max(h.queued, h.done + h.failed)));
  const cols = hours.map((h, i) => {
    const at = toDate(h.hour), hourText = at.toLocaleTimeString(undefined, { hour: 'numeric' });
    const seg = (v, color) => v ? `<i style="height:${Math.max(3, 132 * v / max)}px;background:${color}"></i>` : '';
    return `<div class="wk-col" data-tip="${attr(`${hourText}: ${h.done} done · ${h.failed} failed · ${h.queued} queued`)}">
      ${h.queued ? `<span class="q" style="height:${Math.max(3, 132 * h.queued / max)}px"></span>` : ''}
      <div class="s">${seg(h.done, WK_COLORS.done)}${seg(h.failed, WK_COLORS.failed)}</div>
      ${i % 6 === 0 || i === hours.length - 1 ? `<span class="x">${i === hours.length - 1 ? 'now' : esc(hourText)}</span>` : ''}</div>`;
  }).join('');
  const total = act.done + act.failed;
  const fact = (name, value, extra = '') => `<div><div class="wk-fact"><span>${esc(name)}</span><b>${value}</b></div>${extra}</div>`;
  return `<section class="card"><div class="row between wrap gap10 mb12"><div><div class="card-title">Last 24 hours</div><div class="card-sub">Tasks finished each hour, and how many were queued</div></div>
      <div class="wk-legend" style="margin:0"><span><i style="background:${WK_COLORS.done}"></i>Done</span><span><i style="background:${WK_COLORS.failed}"></i>Failed</span><span><i style="background:var(--brand-soft);border:1px dashed var(--brand-line)"></i>Queued</span></div></div>
    <div class="wk-split"><div>${total || act.queued ? `<div class="wk-chart"><span class="wk-axis">${max}</span>${cols}</div>` : `<div class="wk-chart" style="align-items:center;justify-content:center"><span class="small muted">Nothing queued or finished in the last 24 hours.</span></div>`}</div>
      <div class="wk-facts">${fact('Success rate', wkPct(act.success_rate), act.success_rate == null ? '' : wkMeter(act.success_rate, act.success_rate >= 0.9 ? WK_COLORS.done : act.success_rate >= 0.7 ? 'var(--amber)' : WK_COLORS.failed))}
        ${fact('Finished', `${wkNum(total)}`, `<div class="xs faint">${wkNum(act.done)} done · ${wkNum(act.failed)} failed · ${total ? (total / 24).toFixed(1) : 0}/hour</div>`)}
        ${fact('Queued', wkNum(act.queued))}${fact('Median run time', wkDur(act.median_run_seconds))}${fact('Median wait to start', wkDur(act.median_wait_seconds))}</div></div></section>`;
}

/* ───────────── insights: the metrics each queue already had, laid out for this screen ───────────── */
const WK_RANGES = [['today', 'Today'], ['7d', '7 days'], ['30d', '30 days'], ['90d', '90 days']];
const wkTile = (name, value, sub = '', tone = '', extra = '') => `<div class="wk-tile"><div class="wk-tile-label">${esc(name)}</div><div class="wk-tile-value ${tone}" title="${attr(value)}">${esc(value)}</div>${extra}${sub ? `<div class="wk-tile-sub">${esc(sub)}</div>` : ''}</div>`;
function wkRangeSeg() {
  const w = wkState();
  return `<div class="seg" role="tablist" aria-label="Range">${WK_RANGES.map(([id, text]) => `<button role="tab" aria-selected="${w.range === id}" class="${w.range === id ? 'on' : ''}" data-act="wk-range" data-id="${id}">${text}</button>`).join('')}</div>`;
}
// Ranked rows with a bar under each label: long labels (error messages, model names) wrap instead of being cut.
function wkRanked(items, color = 'var(--brand)', emptyText = 'Nothing in this range.') {
  if (!items.length) return `<div class="small muted">${esc(emptyText)}</div>`;
  const max = Math.max(1, ...items.map(([, n]) => n)), sum = items.reduce((a, [, n]) => a + n, 0);
  return `<div class="wk-rank">${items.map(([text, n]) => `<div class="wk-rank-row" data-tip="${attr(`${text}: ${n}`)}"><span class="t">${esc(text)}</span><b>${wkNum(n)} <span class="xs faint" style="font-weight:400">${Math.round(100 * n / sum)}%</span></b>${wkMeter(n / max, color)}</div>`).join('')}</div>`;
}
const wkCard = (title, sub, body, extra = '') => `<section class="card"><div class="row between wrap gap10 mb14"><div><div class="card-title">${esc(title)}</div>${sub ? `<div class="card-sub">${esc(sub)}</div>` : ''}</div>${extra}</div>${body}</section>`;
// Ranking, Job insights, Tailoring and Reviewing: read from the task rows and model calls of the range, so Clear finished
// (which only hides the list) never empties it.
const WK_PERF_TEXT = {
  ranking: ['Ranking results and its model calls in the selected range', 'RANKER', 'per ranking, end to end'],
  insights: ['Research runs and the model calls that organise them in the selected range', 'company and interview insights', 'per research run'],
  tailoring: ['Tailoring runs (tailor, then review) and the tailor’s model calls in the selected range', 'TAILOR', 'per run, tailor and review'],
  reviewing: ['Reviews of tailored packets and the reviewer’s model calls in the selected range', 'REVIEWER', 'per Tailoring task that reached review'],
};
function wkPerfTiles(tab, x) {
  if (tab === 'ranking' && x.ranking) {
    const r = x.ranking;
    return [wkTile('Qualified', wkNum(r.qualified), 'kept by policy', r.qualified ? 'good' : ''),
      wkTile('Archived', wkNum(r.archived), 'archived by policy'),
      wkTile('Average fit', r.average_fit == null ? '—' : String(r.average_fit), r.median_fit == null ? `${wkNum(r.rankings)} rankings` : `median ${r.median_fit} · ${wkNum(r.rankings)} rankings`)];
  }
  if (tab === 'insights' && x.insights) return [wkTile('Complete · partial', `${wkNum(x.insights.complete)} · ${wkNum(x.insights.partial)}`, 'partial: some sources failed or were not configured', x.insights.partial ? 'warn' : '')];
  if (x.review) {
    const v = x.review;
    return [wkTile('Median reviewer score', v.median_score == null ? '—' : String(Math.round(v.median_score)), v.scored ? `range ${v.lowest_score}–${v.highest_score} · ${wkNum(v.scored)} scored` : 'no scores yet'),
      wkTile('Auto-approved', wkNum(v.auto_approved), 'passed review and your auto-approve rule', v.auto_approved ? 'good' : ''),
      wkTile(tab === 'reviewing' ? 'Master resume reviewed' : 'Skip tailoring', wkNum(tab === 'reviewing' ? v.untailored : v.skip_mode), tab === 'reviewing' ? 'sent unchanged, scored by the reviewer' : 'runs that only reviewed the master resume')];
  }
  return [];
}
function wkPerformanceInsights(tab, x) {
  if (!x) return `<section class="card">${loading()}</section>`;
  const llm = x.llm || {}, [sub, stage, per] = WK_PERF_TEXT[tab] || ['Results in the selected range', '', 'per task'];
  const tokens = `${monTokens(llm.input_tokens || 0)} · ${monTokens(llm.output_tokens || 0)}`;
  const tiles = `<div class="wk-tiles">
    ${wkTile('Tasks', wkNum(x.tasks_created), `created in range${x.active ? ` · ${wkNum(x.active)} still active` : ''}`)}
    ${wkTile('Done', wkNum(x.completed), [x.skipped ? `${wkNum(x.skipped)} skipped` : '', x.stopped ? `${wkNum(x.stopped)} stopped` : '', x.retried ? `${wkNum(x.retried)} needed a retry` : ''].filter(Boolean).join(' · ') || 'finished successfully')}
    ${wkTile('Success rate', wkPct(x.success_rate), `${wkNum(x.failed)} failed`, wkTone(x.success_rate), x.success_rate == null ? '' : wkMeter(x.success_rate, x.success_rate >= 0.9 ? WK_COLORS.done : x.success_rate >= 0.7 ? 'var(--amber)' : WK_COLORS.failed))}
    ${wkTile('Average time', monMs(x.average_duration_ms), per)}
    ${wkTile('Median wait', wkDur(x.median_wait_seconds), 'from queued to started')}
    ${wkPerfTiles(tab, x).join('')}
    ${wkTile('Model calls', wkNum(llm.requests), `${wkNum(llm.failed)} failed${llm.requests ? ` · ${wkPct((llm.failed || 0) / llm.requests)}` : ''}`, llm.failed ? 'warn' : '')}
    ${wkTile('Model latency', monMs(llm.average_latency_ms), 'average per call')}
    ${wkTile('Tokens in · out', tokens, `${(llm.input_tokens || 0).toLocaleString()} in · ${(llm.output_tokens || 0).toLocaleString()} out`)}
    ${wkTile('Cost', llm.estimated_cost == null ? '—' : monUSD(llm.estimated_cost), llm.estimated_cost == null ? 'not reported' : 'estimated by the provider')}</div>
    ${x.truncated || llm.truncated ? '<div class="xs faint mt8">Counted over the most recent 5,000 rows of this range.</div>' : ''}`;
  const models = Object.entries(llm.models || {}).sort((a, b) => b[1] - a[1]);
  const errors = (x.top_errors || []).map(e => [e.error, e.tasks]);
  const categories = Object.entries(x.failures_by_category || {}).sort((a, b) => b[1] - a[1]);
  const decisions = tab === 'ranking' && x.ranking ? wkCard('Decisions', 'How code decided on the rankings made in this range', wkRanked(Object.entries(x.ranking.decisions || {}).map(([k, n]) => [wkHumanize(k), n]), 'var(--brand)', 'No rankings in this range.')) : '';
  const outcomes = wkCard('Outcomes', 'How the tasks created in this range ended (or stand now)', wkRanked(Object.entries(x.by_status || {}).map(([k, n]) => [wkHumanize(k), n]), 'var(--brand)', 'No tasks in this range.'));
  return `${wkCard('Performance', sub, tiles, wkRangeSeg())}
    <div class="wk-grid">${wkCard('Models used', `Calls per model, from LLM usage${stage ? ` (${stage} stage)` : ''}`, wkRanked(models, 'var(--brand)', 'No model calls in this range.'))}
      ${wkCard('Why tasks failed', categories.length ? categories.map(([k, n]) => `${wkHumanize(k)} ${n}`).join(' · ') : 'Most common errors in the selected range', wkRanked(errors, WK_COLORS.failed, 'No failed tasks in this range.'))}
      ${decisions || outcomes}</div>`;
}
// The model's NO_MATCH must pass code's checks before a job is hidden: what each check stopped, and where those jobs are now.
const WK_GATE_TEXT = {
  TITLE_PROTECTED: ['The title is yours', 'The title is one of your target roles, or another name for one.'],
  TEXT_NAMES_OWN_SKILLS: ['Names your skills', 'The posting names at least two of your skills.'],
  SKILLS_MATCHED: ['Model found your skills', 'The model said no match, yet listed skills of yours that the posting asks for.'],
  EVIDENCE_NOT_IN_POSTING: ['Proof not in the posting', 'The phrases the model quoted are not in the text it was shown.'],
  EVIDENCE_TOO_THIN: ['Proof too thin', 'The phrases the model quoted are too short or too generic to show different work.'],
  EVIDENCE_ONLY_IN_TITLE: ['Proof only in the title', 'The posting has a description, but the model quoted only the title.'],
  NO_EVIDENCE: ['No proof given', 'The model quoted no phrases to back its answer.'],
  TITLE_ONLY_TECHNICAL: ['Technical title, no description', 'Only a technical title was known, which is too little to reject on.'],
};
const WK_GATE_NOW = [
  ['waiting', 'Waiting for you', 'var(--amber)'],
  ['kept', 'You kept', WK_COLORS.done],
  ['removed', 'You removed', WK_COLORS.failed],
  ['moved_on', 'Moved on', 'var(--brand)'],
  ['other', 'Other', WK_COLORS.queued],
];
const wkGateNowLabel = key => (WK_GATE_NOW.find(([id]) => id === key) || [key, key])[1];
function wkGateExamples(entry) {
  return `<div class="wk-gate-examples"><div class="xs faint">Most recent ${entry.examples.length === 1 ? 'job' : `${entry.examples.length} jobs`}</div>${entry.examples.map(e => `<div class="wk-gate-example">
    <div class="grow" style="min-width:0"><div class="small b">${esc(e.title || `Job #${e.job_id}`)} <span class="muted" style="font-weight:400">· ${esc(e.company || '')}</span></div>
      ${e.reason ? `<div class="xs muted mt4">Model: ${esc(e.reason)}</div>` : ''}
      ${(e.evidence || []).length ? `<div class="xs muted mt4">Quoted as proof: ${e.evidence.map(p => `“${esc(p)}”`).join(', ')}</div>` : ''}</div>
    <div class="row gap8 none">${pill(wkGateNowLabel(e.now))}${btn('Job', 'open-job', { id: e.job_id }, 'sm')}</div></div>`).join('')}</div>`;
}
function wkGateReview(g) {
  if (!g || (!g.no_match && !g.unavailable)) return '';
  const codes = g.codes || [], open = wkState().gateOpen;
  const total = key => codes.reduce((sum, entry) => sum + (entry.now[key] || 0), 0);
  const waiting = total('waiting'), kept = total('kept'), removed = total('removed'), decided = kept + removed;
  const max = Math.max(1, ...codes.map(entry => entry.jobs));
  const shown = WK_GATE_NOW.filter(([key]) => total(key));
  const tiles = `<div class="wk-tiles">
    ${wkTile('Model said no match', wkNum(g.no_match), 'jobs in this range')}
    ${wkTile('Stopped by a check', wkNum(g.stopped), g.no_match ? `${wkPct(g.stopped / g.no_match)} of the model’s no-match answers` : '', g.stopped ? 'warn' : '', g.no_match ? wkMeter(g.stopped / g.no_match, 'var(--amber)') : '')}
    ${wkTile('Hidden', wkNum(g.stood), 'passed every check')}
    ${wkTile('Your decisions', `${wkNum(kept)} · ${wkNum(removed)}`, decided ? `kept · removed: the check was right on ${wkNum(kept)} of ${wkNum(decided)}` : 'kept · removed: none settled yet')}</div>`;
  const rows = codes.map(entry => {
    const [name, meaning] = WK_GATE_TEXT[entry.code] || [wkHumanize(entry.code), ''];
    const expanded = open === entry.code;
    const segments = WK_GATE_NOW.filter(([key]) => entry.now[key]).map(([key, text, color]) => `<i style="flex:${entry.now[key]};background:${color}" title="${attr(`${text}: ${entry.now[key]}`)}"></i>`).join('');
    const cell = key => `<td class="num">${entry.now[key] ? wkNum(entry.now[key]) : '<span class="faint">—</span>'}</td>`;
    return `<tr><td class="wk-gate-name"><div class="small b">${esc(name)}</div>${meaning ? `<div class="xs muted">${esc(meaning)}</div>` : ''}<div class="xs faint mono">${esc(entry.code)}</div></td>
      <td class="num"><b>${wkNum(entry.jobs)}</b><div class="xs faint">${g.stopped ? Math.round(100 * entry.jobs / g.stopped) : 0}%</div></td>
      <td class="wk-gate-bar"><div class="wk-stack" style="width:${Math.max(4, 100 * entry.jobs / max)}%" role="img" aria-label="${attr(WK_GATE_NOW.filter(([key]) => entry.now[key]).map(([key, text]) => `${text}: ${entry.now[key]}`).join(', '))}">${segments}</div></td>
      ${shown.map(([key]) => cell(key)).join('')}
      <td class="num">${entry.examples.length ? `<button class="btn sm" data-act="wk-gate-toggle" data-id="${attr(entry.code)}" aria-expanded="${expanded}">${expanded ? 'Hide' : 'Jobs'}</button>` : ''}</td></tr>
      ${expanded ? `<tr class="wk-gate-open"><td colspan="${shown.length + 4}">${wkGateExamples(entry)}</td></tr>` : ''}`;
  }).join('');
  const table = codes.length ? `<div class="wk-legend" style="margin:14px 0 8px">${shown.map(([, text, color]) => `<span><i style="background:${color}"></i>${esc(text)}</span>`).join('')}</div>
    <div class="mon-table-wrap wk-gate-table"><table class="mon-table"><thead><tr><th>Check</th><th class="num">Jobs</th><th>Where they are now</th>${shown.map(([, text]) => `<th class="num">${esc(text)}</th>`).join('')}<th></th></tr></thead><tbody>${rows}</tbody></table></div>` : '';
  const notes = [
    total('moved_on') ? 'Moved on: read under earlier rules, or settled by a later read.' : '',
    total('other') ? 'Other: hidden by another rule, being read again, or recorded in shadow mode.' : '',
    g.unavailable ? `The check could not run for ${wkNum(g.unavailable)} ${g.unavailable === 1 ? 'job, which' : 'jobs, which'} went on without a verdict.` : '',
    g.truncated ? 'Counted over the most recent 5,000 reads of this range.' : '',
  ].filter(Boolean);
  const review = waiting ? `<a class="btn sm primary" href="#discovery/review">Review ${wkNum(waiting)} ${waiting === 1 ? 'job' : 'jobs'}</a>` : '';
  return wkCard('When code overruled the model', 'A no-match answer hides a job only if it passes these checks. Each job counts once, by its latest read.',
    `${tiles}${table}${notes.length ? `<div class="xs faint mt8">${notes.map(esc).join(' ')}</div>` : ''}`, review);
}
function wkClassificationInsights(data) {
  const x = data.metrics || {}, c = x.classifications || {}, cmp = data.comparison || {};
  const tiles = `<div class="wk-tiles">
    ${wkTile('Mode', esc(data.mode || 'off'), data.mode === 'on' ? 'its verdicts move jobs' : data.mode === 'shadow' ? 'recorded only: no job changes' : 'the rules alone decide')}
    ${wkTile('Jobs', wkNum(x.jobs), `${wkNum((x.routes || {}).HARD)} rule hid · ${wkNum((x.routes || {}).POSITIVE)} rule kept`)}
    ${wkTile('Sent to the model', wkPct(x.ask_rate), `${wkNum(x.asked)} jobs · ${wkNum(x.cache_hits)} from the cache`)}
    ${wkTile('Verdicts', `${wkNum(c.MATCH)} · ${wkNum(c.UNCERTAIN)} · ${wkNum(c.NO_MATCH)}`, 'match · uncertain · no match')}
    ${wkTile('Model calls', wkNum(x.model_calls), x.tokens_per_call == null ? 'no tokens reported' : `${x.tokens_per_call.toLocaleString()} tokens per call`)}
    ${wkTile('Model latency', monMs(x.latency_p50_ms), `p95 ${monMs(x.latency_p95_ms)}`)}
    ${wkTile('Cost', x.cost == null ? '—' : monUSD(x.cost), x.cost == null ? 'not reported' : `reported on ${wkNum(x.calls_with_cost)} calls`)}</div>`;
  const comparison = cmp.compared ? `<div class="small">${wkNum(cmp.compared)} compared · the old filter hid ${wkNum(cmp.old_hides_new_keeps)} this keeps · it kept ${wkNum(cmp.old_keeps_new_hides)} this hides</div>` : '';
  return `${wkCard('Performance', 'The domain check in the selected range', tiles, wkRangeSeg())}
    ${wkGateReview(x.gate_review)}
    ${comparison ? wkCard('Against the old filter', 'Shadow mode: nothing here moved a job', comparison) : ''}`;
}
function wkParsingInsights(data) {
  const x = data.metrics, llm = x.llm || {}, job = x.scheduler;
  const tokens = `${monTokens(llm.input_tokens || 0)} · ${monTokens(llm.output_tokens || 0)}`;
  const tiles = `<div class="wk-tiles">
    ${wkTile('Parse tasks', wkNum(x.tasks_created), 'created in range')}
    ${wkTile('Parsed', wkNum(x.completed), `${wkNum(x.jobs_parsed)} jobs got structured requirements`)}
    ${wkTile('Success rate', wkPct(x.success_rate), `${wkNum(x.failed)} failed`, wkTone(x.success_rate), x.success_rate == null ? '' : wkMeter(x.success_rate, x.success_rate >= 0.9 ? WK_COLORS.done : x.success_rate >= 0.7 ? 'var(--amber)' : WK_COLORS.failed))}
    ${wkTile('Average time', monMs(x.average_duration_ms), 'per parse, end to end')}
    ${wkTile('Model calls', wkNum(llm.requests), `${wkNum(llm.failed)} failed${llm.requests ? ` · ${wkPct((llm.failed || 0) / llm.requests)}` : ''}`, llm.failed ? 'warn' : '')}
    ${wkTile('Model latency', monMs(llm.average_latency_ms), 'average per call')}
    ${wkTile('Tokens in · out', tokens, `${(llm.input_tokens || 0).toLocaleString()} in · ${(llm.output_tokens || 0).toLocaleString()} out`)}
    ${wkTile('Cost', llm.estimated_cost == null ? '—' : monUSD(llm.estimated_cost), llm.estimated_cost == null ? 'not reported' : 'estimated by the provider')}</div>`;
  const models = Object.entries(llm.models || {}).sort((a, b) => b[1] - a[1]);
  const errors = (x.top_errors || []).map(e => [e.error, e.tasks]);
  const categories = Object.entries(x.failures_by_category || {}).sort((a, b) => b[1] - a[1]);
  return `${wkCard('Performance', 'JD parser results and its model calls in the selected range', tiles, wkRangeSeg())}
    ${job?.last_error ? `<div class="banner warn"><span class="grow">The last scheduler run failed: ${esc(job.last_error)}</span></div>` : ''}
    <div class="wk-grid">${wkCard('Models used', 'Calls per model, from LLM usage (JD parser stage)', wkRanked(models, 'var(--brand)', 'No model calls in this range.'))}
      ${wkCard('Why tasks failed', categories.length ? categories.map(([k, n]) => `${wkHumanize(k)} ${n}`).join(' · ') : 'Most common errors in the selected range', wkRanked(errors, WK_COLORS.failed, 'No failed parse tasks in this range.'))}</div>`;
}
function wkRecoveryInsights(data) {
  const x = data.metrics, diag = data.diagnostics || {}, pw = diag.playwright || {};
  const tiles = `<div class="wk-tiles">
    ${wkTile('Missing descriptions', wkNum(x.missing_descriptions), 'jobs whose description is unusable now', x.missing_descriptions ? 'warn' : '')}
    ${wkTile('Recovery tasks', wkNum(x.tasks_created), 'created in range')}
    ${wkTile('Success rate', wkPct(x.success_rate), `${wkNum(x.recovered)} recovered`, wkTone(x.success_rate), x.success_rate == null ? '' : wkMeter(x.success_rate, x.success_rate >= 0.5 ? WK_COLORS.done : WK_COLORS.failed))}
    ${wkTile('Average latency', monMs(x.average_latency_ms), 'per recovery')}
    ${wkTile('Average length', x.average_recovered_length == null ? '—' : `${x.average_recovered_length.toLocaleString()} chars`, 'of recovered descriptions')}
    ${wkTile('Blocked', wkNum(x.blocked), 'login walls and anti-bot checks, never bypassed', x.blocked ? 'warn' : '')}
    ${wkTile('Failed', wkNum(x.failed), 'gave up after every attempt', x.failed ? 'bad' : '')}
    ${wkTile('Went on', `${wkNum(x.subsequently_parsed)} · ${wkNum(x.subsequently_ranked)}`, 'recovered jobs parsed · ranked after')}</div>`;
  const route = [['HTTP only', x.http_only_success_rate, 'recovered without a browser'], ['Browser fallback', x.playwright_fallback_rate, x.playwright_success_rate == null ? 'never needed' : `${wkPct(x.playwright_success_rate)} of those recovered`],
    ['Model fallback', x.llm_fallback_rate, x.llm_success_rate == null ? 'never needed' : `${wkPct(x.llm_success_rate)} of those recovered`]];
  const routes = `<div class="wk-rank">${route.map(([name, r, sub]) => `<div class="wk-rank-row"><span class="t">${esc(name)} <span class="xs faint">· ${esc(sub)}</span></span><b>${wkPct(r)}</b>${wkMeter(r || 0, 'var(--brand)')}</div>`).join('')}</div>`;
  const cap = (name, d = {}) => `<div class="wk-cap"><span class="b">${esc(name)}</span><span>${wkPill(...({ AVAILABLE: ['Available', 'good', '✓'], UNAVAILABLE: ['Unavailable', 'bad', '✕'] }[d.status] || ['Unknown', 'muted', '?']))}</span><span class="xs muted">${esc(d.reason || '')}</span></div>`;
  const capabilities = `${cap('HTTP extraction', diag.http)}${cap('Browser (Playwright)', pw)}${cap('Model extraction', diag.llm)}
    <div class="xs faint mt8">Automatic recovery ${diag.enabled ? 'on' : 'off (JD_FETCH_ENABLED=false); manual recovery still works'} · browser ${esc(pw.browser || 'chromium')}${pw.headless === false ? ' (headed, for debugging)' : ''}${pw.running ? ' · running' : ''}${diag.settings ? ` · ${diag.settings.max_attempts} attempts each` : ''}</div>`;
  const outcomes = Object.entries(x.by_result || {}).sort((a, b) => b[1] - a[1]).map(([code, n]) => [wkHumanize(code), n]);
  return `${wkCard('Performance', 'Descriptions read back from posting pages in the selected range', tiles, wkRangeSeg())}
    <div class="wk-grid">${wkCard('Route taken', 'HTTP first; the browser and a model only when needed', routes)}
      ${wkCard('Outcomes', 'How finished recoveries ended', wkRanked(outcomes, 'var(--brand)', 'No finished recoveries in this range.'))}
      ${wkCard('What recovery can use', 'Checked on this server', capabilities)}</div>`;
}

/* ───────────── settings ───────────── */
/* One form ('workers') over three sources, saved together:
   · your processing limits and backoff  → PATCH /api/settings (WORKER_SETTING_FIELDS, settings.js)
   · your per-worker settings            → PUT /api/workers/settings      keys a__<kind>__<field>
   · the server's settings (admin only)  → PUT /api/workers/server-settings keys s__<name>              */
const wkAKey = (kind, name) => `a__${kind}__${name}`;
const wkSKey = name => `s__${name}`;
function wkSettingsBaseline() {
  const w = D.wkSettings, out = {};
  if (!w) return out;
  for (const [kind, x] of Object.entries(w.account)) for (const f of x.fields) out[wkAKey(kind, f.name)] = x.values[f.name];
  for (const [name, x] of Object.entries(w.server)) out[wkSKey(name)] = x.value;
  return out;
}
// Save the per-worker and server parts of the form; settings.js saves the processing limits and backoff.
async function wkSaveExtra(changes) {
  const w = D.wkSettings, account = {}, server = {};
  for (const [key, value] of Object.entries(changes)) {
    const [scope, kind, name] = key.split('__');
    if (scope === 'a') {
      const f = w.account[kind]?.fields.find(x => x.name === name);
      (account[kind] = account[kind] || {})[name] = f?.type === 'bool' ? !!value : Number(value);
    } else if (scope === 's') server[kind] = Number(value);
  }
  if (Object.keys(account).length) D.wkSettings.account = (await api('/api/workers/settings', { method: 'PUT', body: { workers: account } })).account;
  if (Object.keys(server).length) D.wkSettings.server = (await api('/api/workers/server-settings', { method: 'PUT', body: { values: server } })).server;
}
const wkSecs = s => s < 60 ? `${s}s` : s < 3600 ? `${Math.round(s / 6) / 10}m`.replace('.0m', 'm') : `${Math.round(s / 360) / 10}h`.replace('.0h', 'h');
const WK_SET_ICONS = { parsing: '▤', recovery: '⟲', ranking: '★', qualification: '⇶', insights: '◈', tailoring: '✎', application: '⌨', indexing: '⌕', approvals: '✓', backoff: '⏱' };
// The unit beside a number, and how its default reads, from the setting's name.
function wkUnit(name) {
  if (/seconds/.test(name)) return ['sec', v => wkSecs(Number(v))];
  if (/minutes/.test(name)) return ['min', v => Number(v) ? `${v} min` : 'off'];
  if (/attempts/.test(name)) return ['tries', v => `${v}`];
  if (/^rag_/.test(name) && /batch_size|entries/.test(name)) return ['entries', v => `${v}`];
  if (/batch_size/.test(name)) return ['tasks', v => `${v}`];
  if (/concurrency/.test(name)) return ['at once', v => `${v}`];
  if (/multiplier/.test(name)) return ['×', v => `×${v}`];
  return ['', v => `${v}`];
}
// One setting: what it is on the left, a small control on the right, and its default with a reset when it differs.
function wkRow(key, label, help, control, def = null, unit = '') {
  const cur = fv('workers', key, settingsBaseline('workers')[key]);
  const custom = def != null && String(cur) !== String(def.value);
  return `<div class="wk-srow ${custom ? 'custom' : ''}"><div class="wk-srow-text"><label for="workers-${key}">${label}</label>${help ? `<p>${help}</p>` : ''}</div>
    <div class="wk-srow-ctl">${control}${def != null ? `<div class="wk-srow-meta">${custom ? `<span class="wk-dot" aria-hidden="true"></span>Custom · <button type="button" class="wk-link" data-act="wk-set-reset" data-key="${attr(key)}" data-value="${attr(def.value)}">reset to ${esc(def.text)}</button>` : `Default${def.text ? ` · ${esc(def.text)}` : ''}`}</div>` : ''}</div></div>`;
}
const wkNumber = (key, min, max, unit, extra = '') => `<div class="wk-num">${inputF('workers', key, settingsBaseline('workers')[key], `type="number" min="${min}" max="${max}" required ${extra}`)}${unit ? `<span>${esc(unit)}</span>` : ''}</div>`;
const wkToggle = (key, on, extra = '') => `<label class="wk-switch sm"><input type="checkbox" id="workers-${key}" data-form="workers" data-field="${key}" ${on ? 'checked' : ''} ${extra}><span aria-hidden="true"></span><b>${on ? 'On' : 'Off'}</b></label>`;
function wkAccountRows(kind) {
  const x = D.wkSettings.account[kind];
  return x.fields.filter(f => f.name !== 'auto').map(f => {
    const key = wkAKey(kind, f.name);
    if (f.type === 'bool') return wkRow(key, esc(f.label), esc(f.help), wkToggle(key, !!fv('workers', key, x.values[f.name]), 'data-rerender'), { value: f.default, text: f.default ? 'on' : 'off' });
    const [unit, fmt] = wkUnit(f.name);
    return wkRow(key, esc(f.label), esc(f.help), wkNumber(key, f.min, f.max, unit), { value: f.default, text: fmt(f.default) });
  }).join('');
}
function wkServerRows(kind) {
  const w = D.wkSettings, items = Object.entries(w.server).filter(([, x]) => x.worker === kind);
  if (!items.length) return '';
  const rows = items.map(([name, x]) => {
    const [unit, fmt] = wkUnit(name), key = wkSKey(name);
    if (!w.server_editable) return `<div class="wk-srow"><div class="wk-srow-text"><label>${esc(x.label)}</label><p>${esc(x.help)}</p></div><div class="wk-srow-ctl"><span class="wk-ro-val">${esc(fmt(x.value))}${unit && !/min|sec/.test(unit) ? ` <small>${esc(unit)}</small>` : ''}</span></div></div>`;
    return wkRow(key, esc(x.label), esc(x.help), wkNumber(key, x.min, x.max, unit), { value: x.default, text: fmt(x.default) });
  }).join('');
  return `<div class="wk-scol server"><div class="wk-scol-head"><span class="wk-scol-icon" aria-hidden="true">${w.server_editable ? '⚙' : '🔒'}</span><div><b>Server · every account</b><small>${w.server_editable ? 'You are an admin: changes pace every account’s scheduler jobs and apply within seconds.' : 'Set by your admin; shown for reference.'}</small></div></div>${rows}</div>`;
}
function wkSettingsCard(kind, title, extra = '') {
  if (!D.wkSettings.account[kind]) return '';  // a worker this server does not have
  const x = D.wkSettings.account[kind], key = wkAKey(kind, 'auto'), on = !!fv('workers', key, x.values.auto);
  const spec = wkSpec(kind) || {}, active = wkCount(spec.counts);
  const facts = wkScheduleFacts(spec.scheduler, kind).map(f => `<span>${esc(f)}</span>`).join('') + (active ? `<span>● ${active} active</span>` : '');
  const server = wkServerRows(kind);
  return `<section class="card flush wk-set-card ${on ? '' : 'paused'}" id="wk-set-${kind}">
    <header class="wk-set-head"><span class="wk-set-icon" aria-hidden="true">${WK_SET_ICONS[kind]}</span>
      <div class="grow" style="min-width:0"><div class="row gap8 wrap" style="align-items:center"><h3>${esc(title)}</h3>${on ? '<span class="orch-chip good">✓ Runs automatically</span>' : '<span class="orch-chip warn">⏸ Paused</span>'}</div><div class="wk-meta mt6">${facts}</div></div>
      <label class="wk-switch" title="Run ${attr(title)} automatically"><input type="checkbox" id="workers-${key}" data-form="workers" data-field="${key}" data-rerender ${on ? 'checked' : ''}><span aria-hidden="true"></span><b>${on ? 'Automatic' : 'Paused'}</b></label></header>
    ${on ? '' : `<div class="wk-paused-note">Queued ${esc(title)} tasks wait for your account until you turn this back on. Run now still starts one.</div>`}
    <div class="wk-scols ${server ? '' : 'single'}"><div class="wk-scol"><div class="wk-scol-head"><span class="wk-scol-icon" aria-hidden="true">👤</span><div><b>Your account</b><small>Only your tasks follow these.</small></div></div>${extra}${wkAccountRows(kind)}</div>${server}</div></section>`;
}
// Approvals: when a packet may be approved without you, and whether the browser tells you when a job was approved.
function wkApprovalsCard(b) {
  const wanted = notifyWanted(), permission = notifyPermission();
  const note = permission === 'unsupported' ? 'This browser cannot show desktop notifications; you will see a message in the app instead.'
    : permission === 'denied' ? 'Notifications are blocked for this site in your browser settings; you will see a message in the app instead.'
    : wanted && permission === 'default' ? 'Allow notifications when the browser asks.' : wanted ? 'You will get a notification here when a job is approved, even if this tab is in the background.' : 'Off for this browser.';
  return `<section class="card flush wk-set-card" id="wk-set-approvals"><header class="wk-set-head"><span class="wk-set-icon" aria-hidden="true">${WK_SET_ICONS.approvals}</span>
      <div class="grow"><h3>Approvals</h3><div class="xs muted mt4">When a reviewed packet is approved, and how you are told.</div></div></header>
    <div class="wk-scols single"><div class="wk-scol"><div class="wk-scol-head"><span class="wk-scol-icon" aria-hidden="true">👤</span><div><b>Your account</b><small>Only your packets follow these.</small></div></div>
      ${renderApprovalSettings(b)}
      <div class="wk-srow"><div class="wk-srow-text"><label for="wk-notify">Desktop notification when a job is approved</label><p>${esc(note)}</p></div>
        <div class="wk-srow-ctl"><label class="wk-switch"><input type="checkbox" id="wk-notify" data-act="wk-notify" ${wanted ? 'checked' : ''}><span aria-hidden="true"></span><b>${wanted ? 'On' : 'Off'}</b></label></div></div></div></div></section>`;
}
function wkSettings() {
  if (!D.settings || !D.wkSettings) return loading();
  const form = 'workers', b = settingsBaseline(form), max = D.settings.processing_maximums || { ranking_concurrency: 5, tailoring_concurrency: 2 };
  const backoffOn = !!fv(form, 'rate_limit_backoff', b.rate_limit_backoff);
  const cadence = Number(fv(form, 'ranking_interval_minutes', b.ranking_interval_minutes)) || 0;
  const serverMinutes = D.settings.ranking_interval_server_minutes || 2;
  const ranking = wkRow('ranking_concurrency', 'Jobs ranked at once', `Up to ${max.ranking_concurrency} on this server.`, wkNumber('ranking_concurrency', 1, max.ranking_concurrency, 'at once'), { value: max.ranking_concurrency, text: String(max.ranking_concurrency) })
    + wkRow('ranking_interval_minutes', 'Rank at most every', cadence > 0 ? `Bursts of up to ${esc(fv(form, 'ranking_concurrency', b.ranking_concurrency))} every ${cadence} min. Run now skips it for one task.` : `0 follows the server, which looks every ${serverMinutes} min.`, wkNumber('ranking_interval_minutes', 0, 1440, 'min'), { value: 0, text: 'server pace' });
  const tailoring = wkRow('tailoring_concurrency', 'Packets tailored at once', `Up to ${max.tailoring_concurrency} on this server. Reviewing runs inside each packet and follows these settings.`, wkNumber('tailoring_concurrency', 1, max.tailoring_concurrency, 'at once'), { value: max.tailoring_concurrency, text: String(max.tailoring_concurrency) });
  const execution = fv(form, 'execution_mode', b.execution_mode);
  const tailoringExtra = tailoring + settingsGroup('Who tailors and reviews', 'Choose who does the tailoring and review reasoning.', renderExecutionSettings(b))
    + renderTailorLengthSettings(b) + renderAutoTailorSettings(b, execution);
  const ladder = `<div class="wk-ladder" aria-label="Waits after rate limits in a row">${backoffLadder(b, 5, form).map((x, i) => `<span><small>${i + 1}${i === 4 ? '+' : ''}</small>${esc(x)}</span>`).join('<i aria-hidden="true">→</i>')}</div>`;
  const backoff = `<section class="card flush wk-set-card" id="wk-set-backoff"><header class="wk-set-head"><span class="wk-set-icon" aria-hidden="true">${WK_SET_ICONS.backoff}</span>
      <div class="grow"><h3>Rate-limit backoff</h3><div class="xs muted mt4">What every worker does after a provider answers 429. Applies to your account.</div></div>
      <label class="wk-switch"><input type="checkbox" id="workers-rate_limit_backoff" data-form="workers" data-field="rate_limit_backoff" data-rerender ${backoffOn ? 'checked' : ''}><span aria-hidden="true"></span><b>${backoffOn ? 'Back off' : 'Off'}</b></label></header>
    ${backoffOn ? `<div class="wk-scols"><div class="wk-scol">${wkRow('rate_limit_base_seconds', 'First wait', 'After the first 429.', wkNumber('rate_limit_base_seconds', 1, 600, 'sec'), { value: 30, text: '30s' })}${wkRow('rate_limit_multiplier', 'Multiply each time by', 'Each 429 in a row waits longer.', wkNumber('rate_limit_multiplier', 1, 10, '×', 'step="0.5"'), { value: 2, text: '×2' })}${wkRow('rate_limit_max_seconds', 'Longest wait', 'The waits stop growing here.', wkNumber('rate_limit_max_seconds', 5, 86400, 'sec'), { value: 900, text: '15m' })}</div>
      <div class="wk-scol server"><div class="wk-scol-head"><span class="wk-scol-icon" aria-hidden="true">↗</span><div><b>Waits after 429s in a row</b><small>Worked out from the values on the left.</small></div></div><div class="wk-pad">${ladder}<p class="settings-help mt12">A provider's own <code>Retry-After</code> wins when it asks for longer. Model calls and Apify calls back off separately; the first normal answer resets it. Kill all and Reset backoff clear a wait that is running now.</p></div></div></div>`
    : '<div class="wk-paused-note">Off: every queued task calls the provider again straight away, even while it is refusing them.</div>'}</section>`;
  const auto = id => !!fv('workers', wkAKey(id, 'auto'), D.wkSettings.account[id].values.auto);
  const nav = [['classification', 'Domain check'], ['parsing', 'JD parsing'], ['recovery', 'JD recovery'], ['ranking', 'Ranking'], ['qualification', 'Qualification'], ['insights', 'Job insights'], ['tailoring', 'Tailoring'], ['application', 'Application'], ['indexing', 'Indexing'], ['resume_parsing', 'Resume parsing'], ['approvals', 'Approvals'], ['backoff', 'Rate limits']]
    .filter(([id]) => id === 'backoff' || id === 'approvals' || D.wkSettings.account[id])
    .map(([id, t]) => `<a href="#wk-set-${id}" data-act="wk-set-jump" data-id="${id}"><span class="wk-set-icon sm" aria-hidden="true">${WK_SET_ICONS[id]}</span><span class="grow">${t}</span>${id === 'backoff' ? (backoffOn ? '<i class="wk-state on">On</i>' : '<i class="wk-state">Off</i>') : id === 'approvals' ? (fv(form, 'application_mode', b.application_mode) === 'automatic' ? '<i class="wk-state on">Auto</i>' : '<i class="wk-state">Manual</i>') : auto(id) ? '<i class="wk-state on">Auto</i>' : '<i class="wk-state paused">Paused</i>'}</a>`).join('');
  return `<form id="settings-editor" class="wk-settings" data-form="workers" novalidate><fieldset ${isBusy('settings-workers') ? 'disabled' : ''}>
    <div class="wk-set-layout"><nav class="wk-set-nav card" aria-label="Worker settings"><div class="caps-sm mb8">WORKER SETTINGS</div>${nav}
        <div class="wk-legend-box mt12"><div><span aria-hidden="true">👤</span> <b>Your account</b> only your tasks</div><div><span aria-hidden="true">${D.wkSettings.server_editable ? '⚙' : '🔒'}</span> <b>Server</b> ${D.wkSettings.server_editable ? 'every account (admin)' : 'set by your admin'}</div><div><span class="wk-dot" aria-hidden="true"></span> <b>Custom</b> differs from the default</div></div>
</nav>
      <div class="stack" style="min-width:0"><div id="settings-error" class="settings-error" role="alert" ${SETTING_ERRORS.workers ? '' : 'hidden'}>${esc(SETTING_ERRORS.workers || '')}</div>
        ${wkSettingsCard('classification', 'Domain check')}${wkSettingsCard('parsing', 'JD parsing')}${wkSettingsCard('recovery', 'JD recovery')}${wkSettingsCard('ranking', 'Ranking', ranking)}${wkSettingsCard('qualification', 'Qualification')}${wkSettingsCard('insights', 'Job insights')}${wkSettingsCard('tailoring', 'Tailoring', tailoringExtra)}${wkSettingsCard('application', 'Application')}${wkSettingsCard('indexing', 'Indexing')}${wkSettingsCard('resume_parsing', 'Resume parsing')}${wkApprovalsCard(b)}${backoff}
        <div class="wk-set-footer">${settingsFooter('workers')}</div></div></div></fieldset></form>`;
}

/* ───────────── multi-select and bulk retry ───────────── */
// What can be retried is what is shown now (after the filters) and failed without being run again since.
function wkSel(tab) {
  const w = wkState();
  if (!w.sel) w.sel = {};
  if (!w.sel[tab]) w.sel[tab] = new Set();
  return w.sel[tab];
}
function wkRetriable(tab) {
  if (WK_GENERIC.includes(tab)) return ((D.wkData || {})[tab]?.rows || []).filter(r => ['FAILED', 'BLOCKED', 'STOPPED'].includes(r.status) && !r.retried_by).map(r => r.task_id);
  if (tab === 'ranking') { const v = wkRankingView().view; return v ? [...v.active, ...v.recent].filter(t => (t.status === 'FAILED' || wkStoppedRank(t)) && !t.retried).map(t => t.id) : []; }
  if (tab === 'tailoring') return wkTailoringView().tasks.filter(t => (t.status === 'failed' || wkStoppedTailor(t)) && !retried(t)).map(t => t.id);
  return [];
}
const wkStoppedRank = t => t.status === 'SKIPPED' && t.last_error === 'Stopped by you';
const wkStoppedTailor = t => t.status === 'cancelled' && t.error === 'Stopped by you';
// Drop what is no longer retriable or no longer shown (finished retrying, filtered out, cleared).
function wkPrune(tab) {
  const ok = new Set(wkRetriable(tab)), sel = wkSel(tab);
  for (const id of [...sel]) if (!ok.has(id)) sel.delete(id);
  return ok;
}
const wkSelectBox = (tab, id, name) => `<label class="wk-check"><input type="checkbox" data-wk-select="${tab}" data-id="${id}" ${wkSel(tab).has(Number(id)) ? 'checked' : ''} aria-label="Select ${attr(name || `task ${id}`)} for retry"><span aria-hidden="true"></span></label>`;
function wkBulkBar(tab) {
  const ids = [...wkPrune(tab)];
  if (!ids.length) return '';
  const n = wkSel(tab).size, all = n === ids.length, key = 'wkRetry' + tab;
  return `<section class="card wk-bulk ${n ? 'on' : ''}"><label class="wk-bulk-all"><span class="wk-check ${n && !all ? 'partial' : ''}"><input type="checkbox" data-wk-select-all="${tab}" ${all ? 'checked' : ''} aria-label="Select all ${ids.length} failed tasks shown"><span aria-hidden="true"></span></span>
      <span>${n ? `<b>${n} selected</b> of ${ids.length} failed shown` : `Select all ${ids.length} failed task${ids.length === 1 ? '' : 's'} shown`}</span></label>
    <span class="xs muted grow">${n ? 'Retried tasks are queued again with fresh attempts and run in normal order.' : 'Tick failed tasks to retry several at once.'}</span>
    <div class="row gap8">${n ? btn('Clear selection', 'wk-sel-clear', { tab }, 'sm ghost') : ''}${btn(label(key, n ? `↻ Retry ${n}` : '↻ Retry selected', 'Retrying…'), 'wk-retry-selected', { tab }, 'sm primary', !n || isBusy(key))}</div></section>`;
}
document.addEventListener('change', e => {
  const one = e.target.dataset?.wkSelect, every = e.target.dataset?.wkSelectAll;
  if (!one && !every) return;
  const tab = one || every, sel = wkSel(tab);
  if (one) { const id = Number(e.target.dataset.id); if (e.target.checked) sel.add(id); else sel.delete(id); }
  else { sel.clear(); if (e.target.checked) wkRetriable(tab).forEach(id => sel.add(id)); }
  render();
});
function wkRetriedMessage(out) {
  const n = out.retried.length, skipped = out.skipped.length;
  const why = skipped ? ` · ${skipped} skipped: ${[...new Set(out.skipped.map(x => x.reason.replace(/ as task #\d+/, '').toLowerCase()))].join(', ')}` : '';
  return n ? `Queued ${n} task${n === 1 ? '' : 's'} again${why}` : `Nothing was retried${why}`;
}

/* ───────────── clear finished ───────────── */
// How many finished tasks the list shows now, and how many a clear is hiding (parsing and recovery say so).
function wkFinishedShown(tab) {
  if (WK_GENERIC.includes(tab)) return ((D.wkData || {})[tab]?.rows || []).filter(r => !['RUNNING', 'QUEUED', 'RETRYING'].includes(r.status)).length;
  if (tab === 'ranking') return (D.ranking?.recent || []).length;
  if (tab === 'tailoring') return wkTailoringView().all.filter(t => !['queued', 'running'].includes(t.status)).length;
  return 0;
}
function wkClearControl(tab) {
  const n = wkFinishedShown(tab), cut = wkClearedAt(tab), hidden = (D.wkData || {})[tab]?.cleared_hidden;
  const show = cut ? btn(label('wkUnclear' + tab, hidden ? `Show ${hidden} cleared` : 'Show cleared', 'Showing…'), 'wk-unclear', { tab }, 'sm ghost', isBusy('wkUnclear' + tab)) : '';
  const clear = n ? btn(label('wkClear' + tab, `Clear finished (${n})`, 'Clearing…'), 'wk-clear', { tab, n }, 'sm', isBusy('wkClear' + tab)) : '';
  return show || clear ? `<span class="row gap6 wk-clear" title="${attr(cut ? `Cleared ${fmtDateTime(cut)}: finished tasks from before then are hidden here; nothing was deleted.` : 'Hide finished tasks from this list. Nothing is deleted.')}">${show}${clear}</span>` : '';
}

/* ───────────── filters ───────────── */
// One bar for every tab: search (job title, company or #id), status with how many each choice shows, and for
// ranking the tracker's location. Ranking keeps its tracker inputs (#rf-q, #rf-location) and their handlers.
function wkFilterBar(tab, counts) {
  const f = wkFilter(tab), ranking = tab === 'ranking';
  const search = ranking
    ? `<input class="input grow" style="min-width:200px;margin-top:0" id="rf-q" data-rf="q" value="${attr(S.rf.q)}" placeholder="Search title, company or location…" aria-label="Search ranking tasks">`
    : `<input class="input grow" style="min-width:200px;margin-top:0" id="wk-q-${tab}" data-wk-search="${tab}" value="${attr(f.q)}" placeholder="${tab === 'indexing' ? 'Search evidence, answer or #source id…' : tab === 'resume_parsing' ? 'Search resume name or #resume id…' : 'Search job title, company or #job id…'}" aria-label="Search tasks">`;
  const status = `<select class="select" style="width:auto;margin-top:0" id="wk-status-${tab}" data-wk-status="${tab}" aria-label="Status">${WK_FILTERS.map(([id, text]) =>
    `<option value="${id}" ${f.status === id ? 'selected' : ''}>${esc(text)}${counts && counts[id] != null ? ` (${counts[id]})` : ''}</option>`).join('')}</select>`;
  const location = ranking && D.ranking ? `<select class="select" style="width:auto;margin-top:0" id="rf-location" data-rf="location" aria-label="Location">${locationOptions(D.ranking.locations).map(([v, t]) =>
    `<option value="${attr(v)}" ${String(v) === String(S.rf.location) ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>` : '';
  const on = (ranking ? S.rf.q || S.rf.location !== 'all' : f.q) || f.status !== 'all';
  return `<section class="card pad12"><div class="row gap8 wrap" style="align-items:center">${search}${status}${location}
    ${on ? btn('Clear filters', 'wk-clear-filters', { tab }, 'sm ghost') : ''}${wkClearControl(tab)}</div></section>`;
}
// The ranking tracker narrowed by status (the server already narrowed it by search and location).
function wkRankingView() {
  const r = D.ranking, f = wkFilter('ranking');
  if (!r) return { view: null, counts: null };
  const counts = wkCounted([...r.active, ...r.recent].map(wkRankStatus));
  if (f.status === 'all') return { view: r, counts };
  const keep = t => wkMatches(wkRankStatus(t), f.status);
  return { view: { ...r, active: r.active.filter(keep), recent: r.recent.filter(keep), filtered: true }, counts };
}
// Finished tasks from before the tab was cleared stay out of the list (nothing is deleted).
const wkClearedAt = tab => (tab === 'ranking' ? D.ranking?.finished_cleared_at : null) || wkSpec(tab)?.cleared_at || null;
function wkTailoringView() {
  const cut = wkClearedAt('tailoring'), f = wkFilter('tailoring');
  const all = (D.tasks || []).filter(t => ['queued', 'running'].includes(t.status) || !cut || !t.finished_at || toDate(t.finished_at) > toDate(cut));
  const searched = all.filter(t => wkText(t, f.q));
  return { tasks: searched.filter(t => wkMatches(wkTailorStatus(t), f.status)), all,
           counts: wkCounted(searched.map(wkTailorStatus)) };
}

/* ───────────── screen ───────────── */
// The qualification graph (QUALIFICATION_GRAPH_MODE) and the application worker (APPLICATION_EXECUTION_MODE) are
// optional: their tabs show once they are scheduled or have work.
function wkShown(id) {
  if (id !== 'qualification' && id !== 'application') return true;
  const spec = wkSpec(id);
  return !!spec && (!!spec.scheduler?.enabled || wkCount(spec.counts) > 0);
}
function renderWorkers() {
  const w = wkState(), s = D.wk;
  if (!s) return loading();
  const tabs = `<div class="seg" role="tablist" aria-label="Workers">${WK_TABS.filter(([id]) => wkShown(id)).map(([id, text]) => {
    const n = wkCount(wkSpec(id)?.counts);
    return `<button role="tab" aria-selected="${w.tab === id}" class="${w.tab === id ? 'on' : ''}" data-act="wk-tab" data-id="${id}">${id === 'settings' ? '<span aria-hidden="true">⚙</span> ' : ''}${text}${n ? ` <span class="wk-badge">${n}</span>` : ''}</button>`;
  }).join('')}</div>`;
  const top = `<section class="card pad12 wk-top">${tabs}
    <div class="row gap8 wrap">${s.active ? `<span class="xs muted">${s.running} running · ${s.active} active across all workers</span>` : '<span class="xs muted">All workers idle</span>'}
      ${btn(label('wkKillEverything', 'Kill all workers', 'Stopping…'), 'wk-kill-everything', {}, 'sm', !s.running && !Object.values(s.workers).some(x => x.counts.retrying) || isBusy('wkKillEverything'))}</div></section>`;
  if (w.tab === 'settings') return `<div class="stack">${top}${wkCooldowns(s.cooldowns)}${wkSettings()}</div>`;
  const spec = wkSpec(w.tab) || {};
  const act = (D.wkAct || {})[w.tab];
  const data = (D.wkData || {})[w.tab];
  let body, filterCounts = null;
  if (w.tab === 'ranking') {
    const ranking = wkRankingView();
    filterCounts = ranking.counts;
    body = wkPerformanceInsights(w.tab, (D.wkPerf || {}).ranking) + wkFilterBar(w.tab, filterCounts) + wkBulkBar(w.tab) + renderRanking(ranking.view);
  } else if (w.tab === 'tailoring') {
    const tailoring = wkTailoringView();
    filterCounts = tailoring.counts;
    body = wkPerformanceInsights(w.tab, (D.wkPerf || {}).tailoring) + wkFilterBar(w.tab, filterCounts) + wkBulkBar(w.tab) + (renderTaskStack(tailoring.tasks, tailoring.all) || `<section class="card">${empty('No tailoring tasks', 'Tasks you queue from the Tailor screen run here in the background.', btn('Open Tailor', 'go', { screen: 'tailor' }, 'primary'))}</section>`);
  } else {
    filterCounts = data?.status_counts || null;
    // The filters narrow the task list, not the metrics, so they sit right above the list.
    const insights = { classification: wkClassificationInsights, parsing: wkParsingInsights, recovery: wkRecoveryInsights }[w.tab]
      || (WK_PERF.includes(w.tab) ? () => wkPerformanceInsights(w.tab, (D.wkPerf || {})[w.tab]) : null);
    body = !data ? loading() : `${insights ? insights(data) : ''}${wkHistoryPanel(w.tab)}${wkFilterBar(w.tab, filterCounts)}${wkBulkBar(w.tab)}${wkTable(w.tab, data)}`;
  }
  return `<div class="stack">${top}${wkCooldowns(s.cooldowns)}${wkHero(w.tab, spec, act)}${wkActivity(act)}${body}</div>`;
}

/* ───────────── actions ───────────── */
function wkKilledMessage(out) {
  const cleared = out.cooldowns_cleared?.length ? ` · backoff cleared for ${out.cooldowns_cleared.map(p => p.toLowerCase()).join(', ')}` : '';
  if (!out.stopped) return `Nothing was running or retrying${cleared}`;
  return `Stopped ${out.stopped} task${out.stopped === 1 ? '' : 's'}${out.running ? ` (${out.running} mid-run; anything they return is discarded)` : ''}. They stay out of the queue until you retry them${cleared}`;
}
async function wkReload() { await loadWorkers(); }
// Search waits for a pause in typing; the status list applies at once. Ranking's own inputs keep their handlers.
document.addEventListener('input', e => {
  const tab = e.target.dataset?.wkSearch;
  if (!tab) return;
  wkFilter(tab).q = e.target.value;
  clearTimeout(wkSearchTimer);
  wkSearchTimer = setTimeout(() => wkApplyFilters(tab), tab === 'tailoring' ? 0 : 250);
});
document.addEventListener('change', e => {
  const tab = e.target.dataset?.wkStatus;
  if (!tab) return;
  wkFilter(tab).status = e.target.value;
  wkApplyFilters(tab);
});
async function wkApplyFilters(tab) {
  if (WK_GENERIC.includes(tab)) { try { await loadWorkers(); } catch (err) { toast(err.message, true); } }
  if (S.screen === 'workers') render();
}
const WORKERS_ACTIONS = {
  'wk-clear-filters': el => {
    const tab = el.dataset.tab;
    Object.assign(wkFilter(tab), { q: '', status: 'all' });
    if (tab === 'ranking') { S.rf = { q: '', location: 'all' }; loadRanking().then(render); return; }
    wkApplyFilters(tab);
  },
  'wk-tab': el => { wkState().tab = el.dataset.id; go('workers'); },
  'wk-clear': el => {
    const tab = el.dataset.tab, n = Number(el.dataset.n) || 0, name = wkSpec(tab)?.label || tab;
    if (!confirm(`Clear ${n} finished ${name} task${n === 1 ? '' : 's'} from this list? Nothing is deleted: job history, the Performance figures and the 24-hour chart keep them, and Show cleared brings them back.`)) return;
    return busy('wkClear' + tab, async () => {
      await api(`/api/workers/${tab}/clear`, { method: 'POST' });
      wkSel(tab).clear();
      await wkReload();
    }, `Finished ${name} tasks cleared from the list`);
  },
  'wk-unclear': el => busy('wkUnclear' + el.dataset.tab, async () => {
    await api(`/api/workers/${el.dataset.tab}/clear`, { method: 'DELETE' });
    await wkReload();
  }),
  'wk-sel-clear': el => { wkSel(el.dataset.tab).clear(); render(); },
  'wk-retry-selected': el => {
    const tab = el.dataset.tab, ids = [...wkSel(tab)];
    if (!ids.length) return;
    if (ids.length > 1 && !confirm(`Retry ${ids.length} failed tasks? Each is queued again with fresh attempts; they run in normal order.`)) return;
    return busy('wkRetry' + tab, async () => {
      const out = await api(`/api/workers/${tab}/retry`, { method: 'POST', body: { task_ids: ids } });
      wkSel(tab).clear();
      await wkReload();
      return out;
    }, wkRetriedMessage);
  },
  'wk-set-reset': el => { setF('workers', el.dataset.key, el.dataset.value === 'true' ? true : el.dataset.value === 'false' ? false : el.dataset.value); render(); },
  'wk-set-jump': (el, e) => { e?.preventDefault?.(); document.getElementById(`wk-set-${el.dataset.id}`)?.scrollIntoView?.({ behavior: 'smooth', block: 'start' }); },
  'wk-range': el => { wkState().range = el.dataset.id; wkReload().then(render).catch(err => toast(err.message, true)); },
  'wk-open': el => { wkState().tab = el.dataset.tab || wkState().tab; if (S.orch) Object.assign(S.orch, { node: null, edge: null }); go('workers'); },
  'wk-gate-toggle': el => { const w = wkState(); w.gateOpen = w.gateOpen === el.dataset.id ? null : el.dataset.id; render(); },
  'wk-refresh': () => busy('wkRefresh', wkReload),
  // A browser setting, not an account one: it applies at once and needs a click for the browser's permission prompt.
  'wk-notify': async el => { await setNotifyApproved(el.checked); render(); },
  'wk-run-queued': el => {
    const kind = el.dataset.kind, n = Number(el.dataset.n) || 0;
    if (n > 1 && !confirm(`${kind === 'ranking' ? 'Rank' : 'Run'} ${n} queued tasks now? They start as fast as your limits allow; a provider cooldown still applies.`)) return;
    return busy('wkRunQueued' + kind, async () => { const out = await api(`/api/workers/${kind}/run-queued`, { method: 'POST' }); await wkReload(); return out; },
      out => out.released ? `${out.released} queued task${out.released === 1 ? '' : 's'} released to run now` : 'Nothing was waiting');
  },
  'wk-nudge': el => busy(el.dataset.job ? `wkNudge${el.dataset.kind}${el.dataset.job}` : el.dataset.jobs ? 'wkNudgeShown' + el.dataset.kind : 'wkNudge' + el.dataset.kind, async () => {
    const jobs = el.dataset.job ? [Number(el.dataset.job)] : el.dataset.jobs ? el.dataset.jobs.split(',').map(Number) : null;
    const out = await api(`/api/workers/${el.dataset.kind}/nudge`, { method: 'POST', ...(jobs ? { body: { job_ids: jobs } } : {}) });
    await wkReload();
    return out;
  }, out => out.message || `Moved ${out.moved.length} job${out.moved.length === 1 ? '' : 's'} to ${out.to}${out.skipped.length ? ` · ${out.skipped.length} skipped: ${out.skipped[0].reason}` : ''}`),
  'wk-history': async el => {
    wkState().history = { kind: el.dataset.kind, id: Number(el.dataset.id) };
    D.wkHistory = null; render();
    D.wkHistory = await api(`/api/workers/${el.dataset.kind}/tasks/${el.dataset.id}/history`).catch(e => ({ error: e.message }));
    render();
  },
  'wk-logs': el => {
    const m = monState();
    Object.assign(m.logs, { category: '', level: '', provider: '', workflow: '', search: '', trace_id: el.dataset.id, page: 1, entry: null });
    m.tab = 'logs'; go('monitoring');
  },
  // Running workers with one task's history open (the Apply kit's link to its application run).
  'wk-open-task': el => {
    const w = wkState();
    w.tab = el.dataset.kind; w.history = { kind: el.dataset.kind, id: Number(el.dataset.id) };
    wkFilter(el.dataset.kind).status = 'all'; D.wkHistory = null;
    go('workers', { kitJob: null });
  },
  'wk-history-close': () => { wkState().history = null; D.wkHistory = null; render(); },
  'wk-run-now': el => busy(`wkRun${el.dataset.kind}${el.dataset.id}`, async () => {
    const out = await api(`/api/workers/${el.dataset.kind}/tasks/${el.dataset.id}/run-now`, { method: 'POST' });
    await wkReload();
    return out;
  }, out => `${out.title || 'The task'} runs next`),
  'wk-continue': el => busy(`wkCont${el.dataset.id}`, async () => {
    await api(`/api/application-executions/${el.dataset.id}/continue`, { method: 'POST' });
    await wkReload();
  }, 'Continuing in the same page: it checks everything again first'),
  'wk-kill': el => busy(`wkKill${el.dataset.kind}${el.dataset.id}`, async () => {
    const out = await api(`/api/workers/${el.dataset.kind}/tasks/${el.dataset.id}/kill`, { method: 'POST' });
    await wkReload();
    return out;
  }, wkKilledMessage),
  'wk-kill-all': el => {
    const name = wkSpec(el.dataset.kind)?.label || el.dataset.kind;
    if (!confirm(`Kill every running or retrying ${name} task? Each is stopped and taken out of the queue (Retry brings it back), and your rate-limit backoff is cleared.`)) return;
    return busy('wkKillAll' + el.dataset.kind, async () => {
      const out = await api(`/api/workers/${el.dataset.kind}/kill-all`, { method: 'POST' });
      await wkReload();
      return out;
    }, wkKilledMessage);
  },
  'wk-kill-everything': () => {
    if (!confirm('Kill every running or retrying task of every worker? Each is stopped and taken out of the queue (Retry brings it back), and your rate-limit backoff is cleared.')) return;
    return busy('wkKillEverything', async () => {
      const out = await api('/api/workers/kill-all', { method: 'POST' });
      await wkReload();
      return out;
    }, wkKilledMessage);
  },
  'wk-reset-cooldowns': () => busy('wkCooldown', async () => {
    const out = await api('/api/workers/cooldowns/reset', { method: 'POST' });
    await wkReload();
    return out;
  }, out => out.cleared.length ? `Backoff cleared for ${out.cleared.map(p => p.toLowerCase()).join(', ')}` : 'No backoff was running'),
};
