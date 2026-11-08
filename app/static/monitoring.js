/* Monitoring & AI Optimization tab. Loaded before app.js; functions use the workspace's shared helpers.
   Every number comes from /api/monitoring/*. Charts are single-series in the brand hue, except input vs output
   tokens (brand + orange, validated for colour-vision deficiency); every chart has hover details and a table view. */
'use strict';

const MON_STAGES = { RANKER: 'Ranker', TAILOR: 'Tailor', REVIEWER: 'Reviewer', RESUME_PARSER: 'Resume parser', JD_PARSER: 'JD parser', JD_RECOVERY: 'JD recovery', DOMAIN_CLASSIFIER: 'Domain check', APPLICATION: 'Application', FOLLOW_UP: 'Follow-up', INBOX: 'Inbox', COMPANY_INSIGHTS: 'Company insights', INTERVIEW_INSIGHTS: 'Interview insights', ADVISOR: 'Advisor', OTHER: 'Other' };
const MON_ERRORS = { TIMEOUT: 'Timeouts', RATE_LIMIT: 'Rate limits', INVALID_JSON: 'Invalid JSON', SCHEMA_VALIDATION: 'Schema validation', PROVIDER_ERROR: 'Provider errors', CONTEXT_OVERFLOW: 'Context overflow', CONFIGURATION: 'Configuration', OTHER: 'Other' };
const MON_RANGES = [['today', 'Today'], ['7d', '7 days'], ['30d', '30 days']];
const MON_SERIES = { input: '#5b4ae8', output: '#eb6834' };
const MON_FILTER_KEYS = ['provider', 'model', 'stage', 'prompt_version', 'status', 'job_id', 'worker_type'];
// Worker labels as AI Orchestration → Models names them; unknown workers fall back to their type.
const monWorker = w => w ? w.replace(/_/g, ' ').toLowerCase().replace(/^./, c => c.toUpperCase()) : '—';
let monTimer = null;

// Monitoring is one screen with seven sections; each loads only what it shows (loadMonitoring).
const MON_TABS = [['overview', 'Overview'], ['llm', 'LLM usage'], ['models', 'Model performance'], ['external', 'External APIs'],
                  ['apify', 'Apify'], ['inbox', 'Application inbox'], ['logs', 'Logs'], ['advisor', 'AI optimization']];
const MON_CATEGORY_TONE = { EXTERNAL_API: 'brand', APIFY: 'brand', LLM: 'muted', SCHEDULER: 'muted',
                            WORKFLOW: 'muted', APPLICATION: 'muted', EMAIL: 'muted', SYSTEM: 'muted' };
const MON_LEVEL_TONE = { ERROR: 'bad', WARNING: 'warn', INFO: 'muted', DEBUG: 'muted' };
// Every dimension the API calls table can be narrowed by, and how each is labelled on its chip.
const MON_EXT_FILTERS = [['provider', 'Provider'], ['operation', 'Operation'], ['workflow', 'Workflow'],
                         ['status', 'Status'], ['error_type', 'Failure'], ['http_status_code', 'HTTP']];
const monExtFiltered = () => MON_EXT_FILTERS.some(([k]) => monState().ext[k]) || monState().ext.error_only;
const MON_EXT_ERRORS = { TIMEOUT: 'Timeouts', RATE_LIMIT: 'Rate limits (429)', CLIENT_ERROR: 'Client errors (4xx)',
                         SERVER_ERROR: 'Server errors (5xx)', CONNECTION: 'Connection failures', DNS: 'DNS failures',
                         INVALID_RESPONSE: 'Invalid responses', RETRY_EXHAUSTED: 'Retries exhausted',
                         CONFIGURATION: 'Configuration', SDK_ERROR: 'SDK errors', OTHER: 'Other' };

// How a request's output contract was enforced, and why an answer failed it (app.prompts.contracts).
const MON_SCHEMA_ENFORCEMENT = {
  native_schema: 'JSON Schema enforced by the provider', json_mode: 'JSON mode; contract checked by Job Hunt OS',
  prompt_only: 'contract in the prompt; checked by Job Hunt OS', none: 'no contract',
};
function monSchemaReason(reason) {
  return ({ POSSIBLE_CROSS_WORKER_OUTPUT: "another worker's output shape", WRONG_OUTPUT_SCHEMA: 'wrong output schema',
    INVALID_JSON: 'not JSON', SEMANTIC_VALIDATION: 'failed the service checks' })[reason] || reason;
}

function monState() {
  if (!S.mon) S.mon = { tab: 'overview', range: '7d', provider: '', model: '', stage: '', prompt_version: '', status: '', job_id: '', worker_type: '', all: false, page: 1, tables: new Set(), request: null, perfBy: 'model', perfDim: 'overview',
    ext: { provider: '', operation: '', workflow: '', status: '', error_type: '', http_status_code: '', error_only: false, page: 1, request: null },
    logs: { category: '', level: '', provider: '', workflow: '', search: '', trace_id: '', page: 1, entry: null } };
  return S.mon;
}
function monQuery(extra = {}) {
  const m = monState();
  return qs({ range: m.range, ...Object.fromEntries(MON_FILTER_KEYS.map(k => [k, m[k]])), all_accounts: m.all ? 'true' : '', ...extra });
}
function monExtQuery(extra = {}) {
  const m = monState(), e = m.ext;
  return qs({ range: m.range, ...Object.fromEntries(MON_EXT_FILTERS.map(([k]) => [k, e[k] ?? ''])),
    error_only: e.error_only ? 'true' : '', all_accounts: m.all ? 'true' : '', ...extra });
}
function monLogQuery(extra = {}) {
  const m = monState(), l = m.logs;
  return qs({ range: m.range, category: l.category, level: l.level, provider: l.provider, search: l.search, trace_id: l.trace_id,
    all_accounts: m.all ? 'true' : '', ...extra });
}
async function loadMonitoring() {
  const m = monState();
  // Each section asks only for what it draws, so opening Logs does not aggregate every LLM request.
  const wantsLlm = ['overview', 'llm', 'advisor'].includes(m.tab);
  const wantsExternal = ['overview', 'external', 'apify'].includes(m.tab);
  const [dash, reqs, capture, rec, ext, extReqs, logs, inbox] = await Promise.all([
    wantsLlm ? api('/api/monitoring/dashboard?' + monQuery()) : Promise.resolve(D.mon),
    m.tab === 'llm' ? api('/api/monitoring/requests?' + monQuery({ page: m.page, per_page: 20 })) : Promise.resolve(D.monRequests),
    ['llm', 'logs'].includes(m.tab) ? api('/api/monitoring/capture').catch(() => D.monCapture) : Promise.resolve(D.monCapture),
    api('/api/monitoring/recommendations'),
    wantsExternal ? api('/api/monitoring/external/dashboard?' + monExtQuery()) : Promise.resolve(D.monExt),
    m.tab === 'external' ? api('/api/monitoring/external/requests?' + monExtQuery({ page: m.ext.page, per_page: 25 })) : Promise.resolve(D.monExtRequests),
    m.tab === 'logs' ? api('/api/monitoring/logs?' + monLogQuery({ page: m.logs.page, per_page: 50 })) : Promise.resolve(D.monLogs),
    m.tab === 'inbox' ? api('/api/monitoring/application-inbox?' + monQuery()) : Promise.resolve(D.monInbox),
  ]);
  if (m.tab === 'models') D.monPerf = await api('/api/monitoring/performance?' + monQuery({ group_by: m.perfBy }));
  const wasActive = D.monRec?.active;
  Object.assign(D, { mon: dash, monRequests: reqs, monCapture: capture, monRec: rec, monExt: ext, monExtRequests: extReqs, monLogs: logs, monInbox: inbox });
  if (wasActive && !rec.active) {
    const task = rec.tasks[0];
    toast(task?.status === 'FAILED' ? `AI usage analysis failed: ${task.error || 'unknown error'}` : 'AI usage analysis finished: recommendations updated', task?.status === 'FAILED');
  }
  scheduleMonPoll();
}
function scheduleMonPoll() {
  clearTimeout(monTimer); monTimer = null;
  if (S.screen !== 'monitoring' || !D.monRec?.active) return;
  monTimer = setTimeout(async () => {
    if (S.screen !== 'monitoring') return;
    try { await loadMonitoring(); } catch { scheduleMonPoll(); return; }
    if (S.screen === 'monitoring') render();
  }, 4000);
}

/* ───────────── formatting ───────────── */
const monUSD = v => v == null ? '—' : v === 0 ? '$0' : Math.abs(v) < 0.01 ? `$${v.toFixed(4)}` : `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const monTokens = n => n == null ? '—' : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}K` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}K` : String(Math.round(n));
/* Dynamic context selection: what could have been sent versus what was, from the context builders. */
function monSelection(sel) {
  if (!sel || !sel.requests) return '';
  const line = x => `${monTokens(x.full_available_context_tokens)} available · ${monTokens(x.selected_context_tokens)} sent · ${x.context_reduction_percent == null ? '—' : x.context_reduction_percent.toFixed(1) + '%'} reduction`;
  const jobLine = x => !x.job_requests ? '' : ` · job description ${monTokens(x.raw_job_tokens)} → ${monTokens(x.selected_job_context_tokens)} (${x.job_context_reduction_percent == null ? '—' : x.job_context_reduction_percent.toFixed(1) + '%'})`;
  return `<div class="mt10" data-mon-selection><div class="caps-sm mb8">Context selection</div><div class="small">${line(sel)}${jobLine(sel)}</div>${sel.by_stage.map(x => `<div class="xs muted">${esc(monStage(x.stage))}: ${line(x)}${jobLine(x)}</div>`).join('')}</div>`;
}
const monMs = ms => ms == null ? '—' : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
const monPct = (r, digits = 1) => r == null ? '—' : `${(r * 100).toFixed(digits)}%`;
const monStage = s => MON_STAGES[s] || s;


/* ───────────── model performance ─────────────
   Six dimensions, never one score. Every number is an aggregate of raw request rows (/api/monitoring/performance);
   a dash means the provider did not report it, not zero. */
const MON_PERF_BY = [['model', 'Model'], ['provider', 'Provider'], ['worker', 'Worker'], ['stage', 'Stage'], ['prompt_version', 'Prompt version'],
  ['config_version', 'Config version'], ['routing_tier', 'Routing tier'], ['day', 'Day'], ['hour', 'Hour']];
const monRate = v => v == null ? '—' : v < 10 ? v.toFixed(1) : String(Math.round(v));
const monPerMin = v => v == null ? '—' : v >= 10 ? v.toFixed(0) : v >= 1 ? v.toFixed(1) : v.toFixed(2);
const monCost = v => v == null ? 'unknown' : monUSD(v);
// Column sets per dimension: [header, value of a group, tooltip]
const MON_PERF_DIMS = {
  overview: ['Overview', [['Requests', g => g.requests.toLocaleString()], ['Success', g => monPct(g.reliability.success_rate)], ['p50', g => monMs(g.latency.p50_ms)],
    ['p95', g => monMs(g.latency.p95_ms)], ['TTFT', g => monMs(g.latency.ttft_p50_ms)], ['Tok/s', g => monRate(g.latency.tokens_per_sec)],
    ['Schema pass', g => monPct(g.reliability.schema_pass_rate)], ['Cost/req', g => g.cost.free ? '$0' : monCost(g.cost.per_request)]]],
  reliability: ['Reliability', [['Requests', g => g.requests.toLocaleString()], ['Success', g => monPct(g.reliability.success_rate)], ['First-try', g => monPct(g.reliability.retry_adjusted_success_rate)],
    ['Timeout', g => monPct(g.reliability.timeout_rate)], ['Rate limit', g => monPct(g.reliability.rate_limit_rate)], ['Retried', g => monPct(g.reliability.retry_rate)],
    ['Fallback', g => monPct(g.reliability.fallback_rate)], ['Failover', g => monPct(g.reliability.failover_rate)],
    ['Off config', g => g.reliability.model_mismatch_samples ? `${monPct(g.reliability.model_mismatch_rate)} of ${g.reliability.model_mismatch_samples}` : '—'],
    ['Escalation', g => monPct(g.reliability.escalation_rate)], ['Schema pass', g => monPct(g.reliability.schema_pass_rate)]]],
  latency: ['Latency', [['Requests', g => g.requests.toLocaleString()], ['p50', g => monMs(g.latency.p50_ms)], ['p75', g => monMs(g.latency.p75_ms)], ['p95', g => monMs(g.latency.p95_ms)],
    ['p99', g => monMs(g.latency.p99_ms)], ['TTFT p50', g => monMs(g.latency.ttft_p50_ms)], ['Generation', g => monMs(g.latency.generation_avg_ms)],
    ['Queue wait p95', g => monMs(g.latency.queue_wait_p95_ms)],
    ['Retrieval p50 / p95', g => g.latency.retrieval_samples ? `${monMs(g.latency.retrieval_p50_ms)} / ${monMs(g.latency.retrieval_p95_ms)}` : '—'], ['Tok/s', g => monRate(g.latency.tokens_per_sec)], ['ms / out token', g => g.latency.ms_per_output_token ?? '—']]],
  throughput: ['Throughput', [['Requests', g => g.requests.toLocaleString()], ['Req/min', g => monPerMin(g.throughput.requests_per_min)], ['Req/hour', g => monPerMin(g.throughput.requests_per_hour)],
    ['OK req/min', g => monPerMin(g.throughput.successful_requests_per_min)], ['In tok/min', g => monTokens(g.throughput.input_tokens_per_min)], ['Out tok/min', g => monTokens(g.throughput.output_tokens_per_min)],
    ['Peak req/min', g => g.throughput.peak_requests_per_min ?? '—'], ['Peak concurrency', g => g.throughput.peak_concurrency ?? '—']]],
  cost: ['Cost', [['Requests', g => g.requests.toLocaleString()], ['Total', g => g.cost.total == null ? 'unknown' : monUSD(g.cost.total)], ['Cost/req', g => g.cost.free ? '$0' : monCost(g.cost.per_request)],
    ['Cost/OK req', g => g.cost.free ? '$0' : monCost(g.cost.per_successful_request)], ['Cost/OK task', g => g.cost.free ? '$0' : monCost(g.cost.per_successful_task)],
    ['Per 1K tasks', g => g.cost.free ? '$0' : monCost(g.cost.per_1k_tasks)], ['Unpriced', g => g.cost.unpriced_requests], ['Share of cost', g => monPct(g.share.cost)]]],
  quality: ['Quality', [['Requests', g => g.requests.toLocaleString()], ['Schema pass', g => monPct(g.quality.schema_pass_rate)], ['Checked', g => g.quality.schema_checked],
    ['Retry after invalid', g => monPct(g.quality.retry_after_invalid_output_rate)], ['Avg confidence', g => g.quality.avg_confidence ?? '—'], ['Escalation', g => monPct(g.reliability.escalation_rate)]]],
  context: ['Context & tokens', [['Requests', g => g.requests.toLocaleString()], ['Input', g => monTokens(g.tokens.input)], ['Output', g => monTokens(g.tokens.output)],
    ['Cached in', g => monTokens(g.tokens.cached_input)], ['Reasoning', g => monTokens(g.tokens.reasoning)], ['Out/in', g => g.tokens.output_input_ratio ?? '—'],
    ['Context used', g => g.context.avg_utilization_percent == null ? '—' : `${g.context.avg_utilization_percent.toFixed(1)}%`],
    ['Selected / available', g => g.context.available_tokens ? `${monTokens(g.context.selected_tokens)} / ${monTokens(g.context.available_tokens)}` : '—']]],
};
function monPerfTable(groups, labelHeader, dim = 'overview') {
  const cols = (MON_PERF_DIMS[dim] || MON_PERF_DIMS.overview)[1];
  if (!groups.length) return '<div class="small muted" style="padding:18px 0">No requests in this range.</div>';
  return `<div class="mon-table-wrap"><table class="mon-table"><thead><tr><th>${esc(labelHeader)}</th>${cols.map(([h]) => `<th>${esc(h)}</th>`).join('')}<th>Share</th></tr></thead><tbody>
    ${groups.map(g => `<tr><td class="mono">${esc(g.label)}${g.low_sample ? ' <span class="xs muted" title="Fewer than 20 requests: treat as indicative">·small sample</span>' : ''}</td>${cols.map(([, f]) => `<td>${esc(f(g))}</td>`).join('')}<td>${monPct(g.share.requests, 0)}</td></tr>`).join('')}</tbody></table></div>`;
}
const monPerfDimSwitch = (cur, act) => `<div class="seg">${Object.entries(MON_PERF_DIMS).map(([id, [t]]) => `<button class="${cur === id ? 'on' : ''}" data-act="${act}" data-id="${id}">${esc(t)}</button>`).join('')}</div>`;
function renderMonPerformance() {
  const m = monState(), d = D.monPerf;
  if (!d) return loading();
  const t = d.totals, o = d.outcomes;
  const by = `<div class="seg">${MON_PERF_BY.map(([id, tx]) => `<button class="${m.perfBy === id ? 'on' : ''}" data-act="mon-perf-by" data-id="${id}">${tx}</button>`).join('')}</div>`;
  const tiles = `<section class="mon-tiles">
    ${monTile('Requests', t.requests.toLocaleString(), `${monPerMin(t.throughput.requests_per_min)}/min over the range`)}
    ${monTile('Success', monPct(t.reliability.success_rate), `${monPct(t.reliability.retry_adjusted_success_rate)} first-try`)}
    ${monTile('p50 / p95', `${monMs(t.latency.p50_ms)} / ${monMs(t.latency.p95_ms)}`, `p99 ${monMs(t.latency.p99_ms)}`)}
    ${monTile('Tokens/sec', monRate(t.latency.tokens_per_sec), t.latency.tokens_per_sec_samples ? `median of ${t.latency.tokens_per_sec_samples} requests` : 'not reported')}
    ${monTile('Schema pass', monPct(t.reliability.schema_pass_rate), `${t.reliability.schema_checked} checked`)}
    ${monTile('Cost', t.cost.total == null ? 'unknown' : monUSD(t.cost.total), t.cost.unpriced_requests ? `+ ${t.cost.unpriced_requests} unpriced` : 'all priced')}
    ${monTile('Peak concurrency', t.throughput.peak_concurrency ?? '—', `peak ${t.throughput.peak_requests_per_min ?? '—'} req/min`)}</section>`;
  const table = monPerfTable(d.groups, (MON_PERF_BY.find(([id]) => id === m.perfBy) || [])[1] || 'Group', m.perfDim);
  const outcomes = o ? `<section class="card mon-card"><div class="card-title">Outcomes people and workers stored</div>
    <div class="card-sub">Per worker, not per model. These are real stored signals, not a quality score.</div><div class="orch-kvs mt8">
    ${[['Reviewer', o.reviewer.available ? `${o.reviewer.reviewed} reviewed · pass ${monPct(o.reviewer.pass_rate)} · re-tailor ${monPct(o.reviewer.retailor_rate)} · median score ${o.reviewer.median_score ?? '—'}` : 'no reviews in range'],
      ['Inbox corrections', o.inbox_corrections.available ? `${o.inbox_corrections.corrected} of ${o.inbox_corrections.events} corrected (${monPct(o.inbox_corrections.correction_rate)})` : 'no inbox events in range'],
      ['Job feedback', o.user_feedback.available ? Object.entries(o.user_feedback.actions).map(([k, v]) => `${k.toLowerCase()} ${v}`).join(' · ') : 'none in range'],
      ['Handed to a person', o.human_answers.available ? `${o.human_answers.asked} application question${o.human_answers.asked === 1 ? '' : 's'}, ${o.human_answers.answered} answered` : 'none in range']]
      .map(([k, v]) => `<div class="orch-kv"><span class="xs muted">${esc(k)}</span><span class="small">${esc(v)}</span></div>`).join('')}</div></section>` : '';
  const gaps = `<details class="xs muted mt8"><summary>What is not measured, and why</summary><ul>${Object.values(d.unsupported).map(v => `<li>${esc(v)}</li>`).join('')}</ul></details>`;
  return `<div class="stack"><section class="card pad16"><div class="row between wrap gap10" style="align-items:center">
      <div class="row gap10 wrap" style="align-items:center"><span class="xs muted">Group by</span>${by}</div>
      ${BOOT.user?.is_admin ? `<label class="row gap6 small"><input type="checkbox" data-act-change="mon-all" ${m.all ? 'checked' : ''}> All accounts</label>` : ''}
      ${btn(label('monRefresh', 'Refresh', 'Refreshing…'), 'mon-refresh', {}, 'sm', isBusy('monRefresh'))}</div>
      ${d.truncated ? '<div class="xs muted mt8">Showing the most recent 50,000 requests in this range.</div>' : ''}</section>${tiles}
    <section class="card mon-card"><div class="row between wrap gap10 mb12" style="align-items:center"><div><div class="card-title">Model comparison</div>
      <div class="card-sub">Computed from raw request rows · a dash is unknown, never zero · cost per task includes failed attempts</div></div>${monPerfDimSwitch(m.perfDim, 'mon-perf-dim')}</div>${table}${gaps}</section>${outcomes}</div>`;
}

/* ───────────── chart pieces ───────────── */
// Horizontal bars: one series, value at the bar tip, the whole row is the hover target.
function monBars(rows, valueOf, textOf, tipOf, { labelWidth = 130, valueWidth = 72 } = {}) {
  const max = Math.max(0, ...rows.map(valueOf));
  if (!rows.length || max <= 0) return '<div class="small muted" style="padding:18px 0">No data in this range.</div>';
  return `<div class="mon-bars">${rows.map(r => {
    const v = valueOf(r), w = max ? Math.max(v > 0 ? 1.5 : 0, 100 * v / max) : 0;
    return `<div class="mon-bar-row" data-tip="${attr(tipOf(r))}"><span class="mon-bar-label ellipsis" style="width:${labelWidth}px">${esc(r._label)}</span>
      <span class="mon-bar-track"><span class="mon-bar-fill"><i style="width:${w}%;background:${MON_SERIES.input}"></i></span><b style="width:${valueWidth}px">${esc(textOf(r))}</b></span></div>`;
  }).join('')}</div>`;
}
// Columns over days: stacked series with a 2px surface gap, rounded top, square baseline, one axis.
function monColumns(days, series, fmt, tipOf) {
  if (!days.length) return '<div class="small muted">No data.</div>';
  const totals = days.map(d => series.reduce((a, s) => a + (d[s.key] || 0), 0));
  const max = Math.max(0, ...totals);
  const labelEvery = Math.max(1, Math.ceil(days.length / 7));
  const cols = days.map((d, i) => {
    const segs = series.map(s => {
      const v = d[s.key] || 0; if (!v || !max) return '';
      return `<i style="height:${Math.max(1.5, 100 * v / max)}%;background:${s.color}"></i>`;
    }).join('');
    return `<div class="mon-col" data-tip="${attr(tipOf(d))}"><div class="mon-col-stack">${segs}</div><span class="mon-col-x">${(i % labelEvery === 0 && days.length - 1 - i >= labelEvery) || i === days.length - 1 ? esc(fmtDay(d.day)) : ''}</span></div>`;
  }).join('');
  const legend = series.length > 1 ? `<div class="row gap14 xs muted mb8">${series.map(s => `<span class="row gap6"><span style="width:10px;height:10px;border-radius:3px;background:${s.color}"></span>${esc(s.name)}</span>`).join('')}</div>` : '';
  return `${legend}<div class="mon-cols-wrap"><span class="mon-axis-max xs faint">${max ? esc(fmt(max)) : ''}</span><div class="mon-cols">${cols}</div></div>`;
}
function monTable(head, rows) {
  return `<div class="mon-table-wrap"><table class="mon-table"><thead><tr>${head.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
function monCard(key, title, sub, chart, table, extra = '') {
  const showTable = monState().tables.has(key);
  return `<section class="card mon-card"><div class="row between gap10 mb12" style="align-items:flex-start"><div><div class="card-title">${esc(title)}</div>${sub ? `<div class="card-sub">${sub}</div>` : ''}</div>
    ${btn(showTable ? 'Chart' : 'Table', 'mon-table', { key }, 'sm')}</div>${showTable ? table : chart}${extra}</section>`;
}
function monTile(label, value, sub = '', tone = '') {
  return `<div class="box mon-tile"><div class="xs muted">${esc(label)}</div><div class="mono bb mon-tile-value" style="${tone ? `color:var(--${tone})` : ''}">${esc(value)}</div>${sub ? `<div class="tiny faint">${esc(sub)}</div>` : ''}</div>`;
}

/* ───────────── page ───────────── */
function renderMonitoring() {
  const m = monState();
  const tabs = `<section class="card pad12"><div class="row between wrap gap10" style="align-items:center">
    <div class="seg">${MON_TABS.map(([id, t]) => `<button class="${m.tab === id ? 'on' : ''}" data-act="mon-tab" data-id="${id}">${t}</button>`).join('')}</div>
    <div class="seg">${MON_RANGES.map(([id, t]) => `<button class="${m.range === id ? 'on' : ''}" data-act="mon-range" data-id="${id}">${t}</button>`).join('')}</div></div></section>`;
  const body = m.tab === 'llm' ? renderMonLlm() : m.tab === 'models' ? renderMonPerformance() : m.tab === 'external' ? renderMonExternal()
    : m.tab === 'apify' ? renderMonApify() : m.tab === 'inbox' ? renderMonInbox() : m.tab === 'logs' ? renderMonLogs()
    : m.tab === 'advisor' ? renderAdvisor() : renderMonOverview();
  return `<div class="stack">${tabs}${body}</div>`;
}

/* Overview: the few numbers that say whether the whole system is healthy, from both telemetry tables. */
function renderMonOverview() {
  const d = D.mon, e = D.monExt;
  if (!d || !e) return loading();
  const s = d.summary, x = e.summary, ap = e.apify;
  const rangeLabel = MON_RANGES.find(([id]) => id === monState().range)?.[1].toLowerCase() || 'this range';
  const tiles = `<section class="mon-tiles">
    ${monTile(`External API calls · ${rangeLabel}`, x.requests.toLocaleString(), `${x.providers} provider${x.providers === 1 ? '' : 's'}`)}
    ${monTile('Success rate', monPct(x.success_rate), `${x.failed} failed`, x.failed ? 'warn' : '')}
    ${monTile('Average latency', monMs(x.avg_latency_ms), `p95 ${monMs(x.p95_latency_ms)}`)}
    ${monTile('LLM requests', s.requests.toLocaleString(), `${monUSD(s.estimated_cost)} estimated`)}
    ${monTile('Apify runs', (ap.runs || 0).toLocaleString(), `${ap.succeeded} succeeded · ${ap.failed} failed`)}
    ${monTile('Jobs discovered', (ap.jobs_discovered || 0).toLocaleString(), `${ap.jobs_after_deduplication || 0} kept after deduplication`)}
  </section>`;
  const providers = e.providers.map(p => ({ ...p, _label: p.provider }));
  const health = monCard('ov-providers', 'Provider health', 'Calls, failures and p95 latency for every third-party service',
    monBars(providers, p => p.requests, p => `${p.requests.toLocaleString()} · ${monPct(p.failure_rate, 0)} failed`,
      p => `${p.provider}: ${p.requests} calls · ${monPct(p.success_rate)} success · p95 ${monMs(p.p95_latency_ms)}`, { labelWidth: 120, valueWidth: 140 }),
    monTable(['Provider', 'Calls', 'Failed', 'Success rate', 'p95 latency'], e.providers.map(p => [p.provider, p.requests, p.failed, monPct(p.success_rate), monMs(p.p95_latency_ms)])));
  const stages = d.stages.map(v => ({ ...v, _label: monStage(v.stage) }));
  const llm = monCard('ov-stages', 'LLM usage by stage', 'Requests per stage in this range',
    monBars(stages, v => v.requests, v => v.requests.toLocaleString(), v => `${monStage(v.stage)}: ${v.requests} requests · ${monUSD(v.estimated_cost)}`),
    monTable(['Stage', 'Requests', 'Estimated cost'], stages.map(v => [monStage(v.stage), v.requests, monUSD(v.estimated_cost)])));
  return `${tiles}<div class="mon-grid">${health}${llm}</div>${renderMonRecentFailures(e)}`;
}

function renderMonRecentFailures(e) {
  const rows = e.errors.by_type;
  if (!rows.length) return `<section class="card pad16"><div class="card-title">No failed external calls</div><div class="card-sub">Every third-party call in this range succeeded.</div></section>`;
  return `<section class="card pad16"><div class="card-title mb4">Failures by type</div>
    <div class="card-sub mb12">${e.errors.failed} failed · ${monPct(e.errors.failure_rate)} of calls · <a href="#" data-act="mon-tab" data-id="external">see the calls</a></div>
    <div class="row gap8 wrap">${rows.map(r => pill(`${MON_EXT_ERRORS[r.error_type] || r.error_type}: ${r.requests}`, 'bad')).join('')}</div></section>`;
}

function renderMonLlm() {
  const m = monState(), d = D.mon;
  if (!d) return loading();
  const s = d.summary, o = d.options;
  const selects = [['provider', 'All providers', o.providers.map(v => [v, v])], ['model', 'All models', o.models.map(v => [v, v])],
    ['stage', 'All stages', o.stages.map(v => [v, monStage(v)])], ['prompt_version', 'All prompt versions', o.prompt_versions.map(v => [v, v])],
    ['status', 'All statuses', [['SUCCEEDED', 'Succeeded'], ['FAILED', 'Failed only']]],
    ...((o.worker_types || []).length ? [['worker_type', 'All workers', o.worker_types.map(v => [v, monWorker(v)])]] : [])]
    .map(([k, all, opts]) => `<select class="select mon-select" data-act-change="mon-filter" data-key="${k}" aria-label="${attr(all)}">${[['', all], ...opts].map(([v, t]) => `<option value="${attr(v)}" ${String(m[k]) === v ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>`).join('');
  const filtered = MON_FILTER_KEYS.some(k => m[k]) || m.all;
  const filters = `<section class="card pad16"><div class="row wrap gap8" style="align-items:center">
    ${selects}<input class="input mon-select" style="width:96px" inputmode="numeric" placeholder="Job id" value="${attr(m.job_id)}" data-act-change="mon-filter" data-key="job_id" aria-label="Job id">
    ${BOOT.user?.is_admin ? `<label class="row gap6 small"><input type="checkbox" data-act-change="mon-all" ${m.all ? 'checked' : ''}> All accounts</label>` : ''}
    ${filtered ? btn('Reset', 'mon-reset', {}, 'sm') : ''}<span class="grow"></span>${btn(label('monRefresh', 'Refresh', 'Refreshing…'), 'mon-refresh', {}, 'sm', isBusy('monRefresh'))}</div>
    ${d.truncated ? '<div class="xs muted mt8">Showing the most recent 50,000 requests in this range.</div>' : ''}</section>`;

  const rangeLabel = MON_RANGES.find(([id]) => id === m.range)?.[1].toLowerCase() || 'this range';
  const tiles = `<section class="mon-tiles">
    ${monTile(m.range === 'today' ? 'LLM requests today' : `LLM requests · ${rangeLabel}`, s.requests.toLocaleString(), `${s.succeeded.toLocaleString()} succeeded`)}
    ${monTile('Total tokens', monTokens(s.total_tokens), [s.estimated_token_requests && `${s.estimated_token_requests} estimated locally`, s.requests_without_usage && `${s.requests_without_usage} without usage`].filter(Boolean).join(' · ') || 'provider-reported')}
    ${monTile('Input tokens', monTokens(s.input_tokens))}
    ${monTile('Output tokens', monTokens(s.output_tokens))}
    ${monTile('Estimated cost', monUSD(s.estimated_cost), s.unpriced_requests ? `+ ${s.unpriced_requests} unpriced request${s.unpriced_requests === 1 ? '' : 's'}` : 'all requests priced')}
    ${monTile('Average latency', monMs(s.avg_latency_ms))}
    ${monTile('P95 latency', monMs(s.p95_latency_ms))}
    ${monTile('Failed requests', s.failed.toLocaleString(), s.requests ? `${monPct(s.failure_rate)} of requests` : '', s.failed ? 'bad' : '')}
    ${monTile('Retry rate', monPct(s.retry_rate), `${s.retried_requests} retried`)}
    ${monTile('Average context', s.avg_context_tokens == null ? '—' : `${monTokens(s.avg_context_tokens)} tokens`, s.avg_context_utilization_percent == null ? 'context window unknown' : `${s.avg_context_utilization_percent.toFixed(1)}% of window`)}
  </section>`;

  const days = d.timeseries;
  const costChart = monCard('cost', 'Cost over time', 'Estimated USD per day. Unpriced requests are counted separately, never guessed.',
    monColumns(days, [{ key: 'estimated_cost', color: MON_SERIES.input, name: 'Estimated cost' }], monUSD,
      x => `${fmtDay(x.day)} · ${monUSD(x.estimated_cost)} · ${x.requests} request${x.requests === 1 ? '' : 's'}${x.unpriced_requests ? ` (${x.unpriced_requests} unpriced)` : ''}`),
    monTable(['Day', 'Requests', 'Estimated cost', 'Unpriced'], days.map(x => [x.day, x.requests, monUSD(x.estimated_cost), x.unpriced_requests])));
  const tokenChart = monCard('tokens', 'Token usage over time', 'Input vs output tokens per day',
    monColumns(days, [{ key: 'input_tokens', color: MON_SERIES.input, name: 'Input' }, { key: 'output_tokens', color: MON_SERIES.output, name: 'Output' }], monTokens,
      x => `${fmtDay(x.day)} · input ${monTokens(x.input_tokens)} · output ${monTokens(x.output_tokens)}`),
    monTable(['Day', 'Input tokens', 'Output tokens'], days.map(x => [x.day, x.input_tokens.toLocaleString(), x.output_tokens.toLocaleString()])));

  const stages = d.stages.map(x => ({ ...x, _label: monStage(x.stage) }));
  const stageChart = monCard('stages', 'Usage by stage', 'Share of requests; hover for tokens and cost share',
    monBars(stages, x => x.requests, x => x.requests ? `${x.requests.toLocaleString()} · ${monPct(x.request_share, 0)}` : '0',
      x => `${monStage(x.stage)} · ${x.requests} requests · ${monTokens(x.input_tokens + x.output_tokens)} tokens · ${monUSD(x.estimated_cost)}${x.cost_share != null ? ` (${monPct(x.cost_share, 0)} of cost)` : ''} · avg context ${monTokens(x.avg_context_tokens)}`),
    monTable(['Stage', 'Requests', 'Share', 'Tokens', 'Estimated cost', 'Cost share', 'Failures', 'Avg context'], stages.map(x => [monStage(x.stage), x.requests, monPct(x.request_share), (x.input_tokens + x.output_tokens).toLocaleString(), monUSD(x.estimated_cost), monPct(x.cost_share), x.failed, monTokens(x.avg_context_tokens)])));

  const models = d.models.slice(0, 12).map(x => ({ ...x, _label: x.model }));
  const modelChart = monCard('models', 'Cost by model', 'Free models cost $0; unknown prices stay unknown',
    monBars(models, x => x.estimated_cost || 0, x => x.free ? 'free' : `${monUSD(x.estimated_cost)}${x.unpriced_requests ? ' + unpriced' : ''}`,
      x => `${x.provider} · ${x.model} · ${x.requests} requests · ${monUSD(x.estimated_cost)}${x.unpriced_requests ? ` · ${x.unpriced_requests} unpriced` : ''} · schema failures ${monPct(x.structured_output_failure_rate)}`, { labelWidth: 150, valueWidth: 118 })
      + (models.length && models.every(x => !x.estimated_cost) ? `<div class="xs muted mt8">${models.map(x => `${esc(x.model)}: ${x.free ? 'free' : x.unpriced_requests ? 'price unknown' : '$0'}`).join(' · ')}</div>` : ''),
    monTable(['Provider', 'Model', 'Requests', 'Estimated cost', 'Unpriced', 'Failure rate', 'Schema failures', 'Avg latency'], d.models.map(x => [x.provider, x.model, x.requests, monUSD(x.estimated_cost), x.unpriced_requests, monPct(x.failure_rate), monPct(x.structured_output_failure_rate), monMs(x.avg_latency_ms)])));

  const lat = d.latency.overall;
  const latRows = [['Average', lat.avg_latency_ms], ['p50', lat.p50_latency_ms], ['p95', lat.p95_latency_ms], ['p99', lat.p99_latency_ms]].map(([n, v]) => ({ _label: n, v }));
  const latencyChart = monCard('latency', 'Latency', 'Time from request to response',
    monBars(latRows, x => x.v || 0, x => monMs(x.v), x => `${x._label}: ${monMs(x.v)}`, { labelWidth: 70, valueWidth: 52 }),
    monTable(['Measure', 'Latency'], latRows.map(x => [x._label, monMs(x.v)]).concat(d.latency.by_stage.map(x => [`${monStage(x.stage)} p95`, monMs(x.p95_latency_ms)]))));

  const c = d.context;
  const buckets = c.buckets.map(x => ({ ...x, _label: x.label }));
  const contextChart = monCard('context', 'Context utilisation', `Average ${c.avg_utilization_percent == null ? '—' : c.avg_utilization_percent.toFixed(1) + '%'} of the context window · ${c.high_context_requests} high-context (≥50%) · ${c.near_limit_requests} near the limit (≥90%)`,
    monBars(buckets, x => x.requests, x => x.requests.toLocaleString(), x => `${x.label} of context window: ${x.requests} requests`, { labelWidth: 70, valueWidth: 44 })
      + (c.unknown_window ? `<div class="xs muted mt8">${c.unknown_window} request${c.unknown_window === 1 ? '' : 's'} used a model whose context window is not in the model catalog.</div>` : '')
      + (Object.keys(c.contributors).length ? `<div class="xs muted mt8">Context by source: ${Object.entries(c.contributors).map(([k, v]) => `${esc(k.replace(/_/g, ' '))} ${monPct(v, 0)}`).join(' · ')}</div>` : ''),
    monTable(['Utilisation', 'Requests'], buckets.map(x => [x.label, x.requests]).concat([['Window unknown', c.unknown_window]])),
    monSelection(c.selection) +
    c.largest.length ? `<div class="mt10"><div class="caps-sm mb8">Largest contexts</div>${c.largest.slice(0, 5).map(x => `<div class="row between gap10 small click" data-act="mon-request" data-id="${x.id}" style="padding:4px 0;cursor:pointer" title="${attr(`${monStage(x.stage)} · ${x.model || ''}`)}"><span class="ellipsis" style="min-width:0">${esc(monStage(x.stage))} · ${esc(x.model || '')}</span><span class="mono none" style="white-space:nowrap">${monTokens(x.context_tokens)}/${monTokens(x.context_window_size)} · ${x.utilization_percent.toFixed(0)}%</span></div>`).join('')}</div>` : '');

  const errs = d.errors.by_type.map(x => ({ ...x, _label: MON_ERRORS[x.error_type] || x.error_type }));
  const errorChart = monCard('errors', 'Failures', `${d.errors.failed} failed · ${monPct(d.errors.failure_rate)} of requests`,
    monBars(errs, x => x.requests, x => x.requests.toLocaleString(), x => `${x._label}: ${x.requests}`, { valueWidth: 44 }),
    monTable(['Failure type', 'Requests'], errs.map(x => [x._label, x.requests])));

  return `${filters}${tiles}
    <div class="mon-grid">${costChart}${tokenChart}</div>
    <div class="mon-grid">${stageChart}${modelChart}</div>
    <div class="mon-grid mon-grid-3">${latencyChart}${contextChart}${errorChart}</div>
    ${renderMonRouting(d.routing)}
    ${renderMonRequests()}`;
}

// Model routing through the LiteLLM gateway: which worker used which model, the auto router's tiers, quality
// escalation and infrastructure fallback — kept apart, because they answer different questions.
function renderMonRouting(r) {
  if (!r || !r.requests) return '';
  const workers = r.workers.map(x => ({ ...x, _label: monWorker(x.worker_type) }));
  const tiers = r.auto_router.requests ? `<div class="xs muted mt8">Auto Router tiers (${r.auto_router.requests} requests): ${r.auto_router.tiers.map(t => `${esc(t.tier)} ${monPct(t.share, 0)}`).join(' · ')}</div>` : '';
  const escalations = r.escalations.length ? `<div class="xs muted mt8">Escalations: ${r.escalations.map(e => `${esc(monWorker(e.worker_type))} ${e.requests}× (${esc(e.reason)})`).join(' · ')}</div>` : '';
  const notes = [r.slowest_model && `Slowest model (p95): ${r.slowest_model}`, r.most_schema_failures && `Most schema failures: ${r.most_schema_failures}`].filter(Boolean);
  return `<div class="mon-grid">${monCard('routing', 'Model routing', 'Inference time by worker (wall-clock latency) · escalation and fallback are counted separately',
    monBars(workers, x => x.inference_ms, x => monPct(x.inference_share, 0), x => `${monWorker(x.worker_type)} · ${x.requests} requests · models ${x.models.map(m => m[0]).join(', ') || '—'} · escalated ${monPct(x.escalation_rate)} · fallback ${monPct(x.fallback_rate)} · p95 ${monMs(x.p95_latency_ms)}`, { labelWidth: 150, valueWidth: 52 })
      + tiers + escalations + (notes.length ? `<div class="xs muted mt8">${esc(notes.join(' · '))}</div>` : ''),
    monTable(['Worker', 'Requests', 'Configured', 'Actual model', 'Off config', 'Escalated', 'Fallback', 'Failover', 'Schema failures', 'p50 / p95 / p99', 'Inference share'], workers.map(x => [monWorker(x.worker_type), x.requests, x.configured.map(m => m[0]).join(', ') || '—', x.models.map(m => m[0]).join(', ') || '—', x.model_mismatch_samples ? monPct(x.model_mismatch_rate) : '—', monPct(x.escalation_rate), monPct(x.fallback_rate), monPct(x.failover_rate), monPct(x.schema_failure_rate), `${monMs(x.p50_latency_ms)} / ${monMs(x.p95_latency_ms)} / ${monMs(x.p99_latency_ms)}`, monPct(x.inference_share)])))}
    ${monCard('routing-models', 'Models behind the workers', 'Per model: schema failures, latency and share of inference time',
      monBars(r.models.map(x => ({ ...x, _label: x.model })), x => x.inference_ms, x => monPct(x.inference_share, 0), x => `${x.model} · ${x.requests} requests · schema failures ${monPct(x.schema_failure_rate)} · p95 ${monMs(x.p95_latency_ms)}`, { labelWidth: 170, valueWidth: 52 }),
      monTable(['Model', 'Requests', 'Schema failures', 'Failure rate', 'p95', 'Avg confidence', 'Inference share'], r.models.map(x => [x.model, x.requests, monPct(x.schema_failure_rate), monPct(x.failure_rate), monMs(x.p95_latency_ms), x.avg_confidence ?? '—', monPct(x.inference_share)])))}</div>`;
}

function renderMonRequests() {
  const r = D.monRequests; if (!r) return '';
  const pages = Math.max(1, Math.ceil(r.total / r.per_page));
  const rows = r.items.map(x => `<tr class="click" data-act="mon-request" data-id="${x.id}">
    <td>${esc(fmtDateTime(x.created_at))}</td><td>${esc(x.worker_type ? monWorker(x.worker_type) : monStage(x.stage))}${x.escalated ? ` ${pill('escalated', 'warn')}` : ''}${x.fallback_used ? ` ${pill('fallback', 'warn')}` : ''}</td><td class="ellipsis" style="max-width:200px" title="${attr(x.model || '')}">${esc(x.model || '—')}</td>
    <td>${x.status === 'FAILED' ? pill(MON_ERRORS[x.error_type] || 'failed', 'bad') : pill('ok', 'good')}</td>
    <td class="mono">${monTokens(x.input_tokens)} / ${monTokens(x.output_tokens)}${x.usage_source === 'estimated' ? ' <span class="faint" title="Estimated locally">~</span>' : ''}</td>
    <td class="mono">${x.context_utilization_percent == null ? '—' : x.context_utilization_percent.toFixed(1) + '%'}</td>
    <td class="mono">${monMs(x.latency_ms)}</td><td class="mono">${monUSD(x.estimated_cost)}</td><td class="mono">${x.retry_count}</td></tr>`).join('');
  return `<section class="card flush"><div class="row between wrap gap10" style="padding:14px 18px;border-bottom:1px solid var(--line2)">
      <div><div class="card-title">LLM requests</div><div class="card-sub">${r.total.toLocaleString()} matching · click a request for its details${D.monCapture?.enabled || D.monCapture?.server_wide ? ', full prompt and response' : ''} and its logs</div>${monCaptureSwitch()}</div>
      <div class="row gap8 xs muted">${btn('‹ Prev', 'mon-page', { dir: -1 }, 'sm', r.page <= 1)}<span>Page ${r.page} of ${pages}</span>${btn('Next ›', 'mon-page', { dir: 1 }, 'sm', r.page >= pages)}</div></div>
    ${r.items.length ? `<div class="mon-table-wrap" data-scroll-key="mon-llm"><table class="mon-table"><thead><tr><th>Time</th><th>Worker / stage</th><th>Model</th><th>Status</th><th>Tokens in / out</th><th>Context</th><th>Latency</th><th>Cost</th><th>Retry</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div style="padding:18px">${empty('No LLM requests', 'Requests appear here as ranking, tailoring, reviews and follow-ups call a model.')}</div>`}</section>`;
}

/* ───────────── External APIs ───────────── */
function renderMonExternal() {
  const m = monState(), e = D.monExt;
  if (!e) return loading();
  const s = e.summary, f = m.ext;
  // Options come from the whole range (facets), never from the rows the current filter left behind — otherwise
  // choosing one provider would drop every other provider from the list, with no way back but Reset.
  const choices = e.facets || { providers: [], operations: [], workflows: [], error_types: [], status_codes: [] };
  const opts = (key, all, values) => `<select class="select mon-select" data-act-change="mon-ext-filter" data-key="${key}" aria-label="${attr(all)}">${[['', all], ...values].map(([v, t]) => `<option value="${attr(v)}" ${String(f[key] ?? '') === String(v) ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>`;
  const filters = `<section class="card pad16"><div class="row wrap gap8" style="align-items:center">
    ${opts('provider', 'All providers', choices.providers.map(p => [p, p]))}
    ${opts('operation', 'All operations', choices.operations.map(o => [o, o]))}
    ${opts('workflow', 'All workflows', choices.workflows.map(w => [w, w]))}
    ${opts('status', 'All statuses', [['SUCCEEDED', 'Succeeded'], ['FAILED', 'Failed only']])}
    ${opts('error_type', 'All failure types', choices.error_types.map(t => [t, MON_EXT_ERRORS[t] || t]))}
    ${opts('http_status_code', 'All HTTP codes', choices.status_codes.map(code => [code, code]))}
    <label class="row gap6 small"><input type="checkbox" data-act-change="mon-ext-errors" ${f.error_only ? 'checked' : ''}> Errors only</label>
    ${BOOT.user?.is_admin ? `<label class="row gap6 small"><input type="checkbox" data-act-change="mon-all" ${m.all ? 'checked' : ''}> All accounts</label>` : ''}
    ${monExtFiltered() ? btn('Reset', 'mon-ext-reset', {}, 'sm') : ''}
    <span class="grow"></span>${btn(label('monRefresh', 'Refresh', 'Refreshing…'), 'mon-refresh', {}, 'sm', isBusy('monRefresh'))}</div>
    ${monExtFiltered() ? `<div class="row gap6 wrap mt10">${MON_EXT_FILTERS.filter(([k]) => f[k]).map(([k, name]) => `<button type="button" class="btn sm soft" data-act="mon-ext-clear" data-key="${k}" title="Remove this filter">${esc(name)}: ${esc(k === 'error_type' ? (MON_EXT_ERRORS[f[k]] || f[k]) : f[k])} ×</button>`).join('')}${f.error_only ? `<button type="button" class="btn sm soft" data-act="mon-ext-clear" data-key="error_only">Errors only ×</button>` : ''}</div>` : ''}</section>`;

  const rangeLabel = MON_RANGES.find(([id]) => id === m.range)?.[1].toLowerCase() || 'this range';
  // A provider that rate limited this account is being left alone; say so, because its calls are being held.
  const cooling = (e.cooldowns || []).length ? `<section class="card pad16"><div class="row gap10 wrap" style="align-items:center">
    <span class="grow small"><b>Backing off</b> · ${e.cooldowns.map(c => `${esc(c.provider)} for ${monMs(c.seconds_left * 1000)}${c.calls_skipped ? ` (${c.calls_skipped} call${c.calls_skipped === 1 ? '' : 's'} held)` : ''}`).join(' · ')}</span>
    <a class="small" href="#workers/settings">Change the backoff</a></div>
    <div class="xs muted mt6">Calls to these providers are not being made until the wait passes; queued work retries afterwards.</div></section>` : '';
  const tiles = `<section class="mon-tiles">
    ${monTile(m.range === 'today' ? 'External API calls today' : `External API calls · ${rangeLabel}`, s.requests.toLocaleString(), `${s.operations} operation${s.operations === 1 ? '' : 's'}`)}
    ${monTile('Success rate', monPct(s.success_rate))}
    ${monTile('Failed calls', s.failed.toLocaleString(), s.requests ? monPct(s.failure_rate) + ' of calls' : '', s.failed ? 'bad' : '')}
    ${monTile('Average latency', monMs(s.avg_latency_ms))}
    ${monTile('P95 latency', monMs(s.p95_latency_ms), `p99 ${monMs(s.p99_latency_ms)}`)}
    ${monTile('Retries', s.retries.toLocaleString(), `${s.retried_requests} call${s.retried_requests === 1 ? '' : 's'} retried`)}
    ${monTile('Rate limited', s.rate_limited.toLocaleString(), monPct(s.rate_limit_rate) + ' of calls', s.rate_limited ? 'warn' : '')}
    ${monTile('Estimated cost', monUSD(s.estimated_cost), s.unpriced_requests ? `${s.unpriced_requests} call${s.unpriced_requests === 1 ? '' : 's'} with no reported cost` : 'all calls priced')}
  </section>`;

  const providers = e.providers.map(p => ({ ...p, _label: p.provider }));
  const providerCard = monCard('ext-providers', 'By service', 'Calls per provider; hover for the reliability detail',
    monBars(providers, p => p.requests, p => `${p.requests.toLocaleString()} · ${monPct(p.failure_rate, 0)} failed`,
      p => `${p.provider}: ${p.requests} calls · ${monPct(p.success_rate)} success · ${p.rate_limited} rate limited · ${p.timeouts} timeouts · p95 ${monMs(p.p95_latency_ms)}`, { labelWidth: 120, valueWidth: 140 }),
    monTable(['Provider', 'Calls', 'Success', 'Failed', '429', '5xx', 'Timeouts', 'Retries', 'Avg', 'p95', 'Cost'],
      e.providers.map(p => [p.provider, p.requests, monPct(p.success_rate), p.failed, p.rate_limited, p.server_errors, p.timeouts, p.retries, monMs(p.avg_latency_ms), monMs(p.p95_latency_ms), monUSD(p.estimated_cost)])));

  const ops = e.operations.slice(0, 12).map(o => ({ ...o, _label: `${o.provider} · ${o.operation || '—'}` }));
  const opCard = monCard('ext-operations', 'By operation', 'Which calls happen most, and how reliable each one is',
    monBars(ops, o => o.requests, o => `${o.requests.toLocaleString()} · ${monMs(o.p95_latency_ms)}`,
      o => `${o.provider} ${o.operation}: ${o.requests} calls · ${monPct(o.success_rate)} success · p95 ${monMs(o.p95_latency_ms)}`, { labelWidth: 190, valueWidth: 130 }),
    monTable(['Provider', 'Operation', 'Calls', 'Failed', 'Success rate', 'Avg', 'p95', 'Retries'],
      e.operations.map(o => [o.provider, o.operation || '—', o.requests, o.failed, monPct(o.success_rate), monMs(o.avg_latency_ms), monMs(o.p95_latency_ms), o.retries])));

  const errs = e.errors.by_type.map(x => ({ ...x, _label: MON_EXT_ERRORS[x.error_type] || x.error_type }));
  const errorCard = monCard('ext-errors', 'Failures', `${e.errors.failed} failed · ${monPct(e.errors.failure_rate)} of calls`,
    monBars(errs, x => x.requests, x => x.requests.toLocaleString(), x => `${x._label}: ${x.requests}`, { valueWidth: 44 }),
    monTable(['Failure type', 'Calls', 'Share'], errs.map(x => [x._label, x.requests, monPct(x.share)])));

  const days = e.timeseries.map(x => ({ ...x, day: x.at }));
  const volumeCard = monCard('ext-volume', 'Calls over time', 'Calls per day (per hour for today), with failures',
    monColumns(days, [{ key: 'requests', color: MON_SERIES.input, name: 'Calls' }], v => String(v),
      x => `${fmtDateTime(x.at)} · ${x.requests} call${x.requests === 1 ? '' : 's'}${x.failed ? ` · ${x.failed} failed` : ''} · avg ${monMs(x.avg_latency_ms)}`),
    monTable(['When', 'Calls', 'Failed', 'Avg latency'], days.map(x => [fmtDateTime(x.at), x.requests, x.failed, monMs(x.avg_latency_ms)])));

  const workflows = e.workflows.map(w => ({ ...w, _label: w.workflow }));
  const workflowCard = monCard('ext-workflows', 'By workflow', 'Which part of the system made the calls',
    monBars(workflows, w => w.requests, w => `${w.requests.toLocaleString()} · ${monMs(w.p95_latency_ms)}`,
      w => `${w.workflow}: ${w.requests} calls · ${monPct(w.success_rate)} success · p95 ${monMs(w.p95_latency_ms)}`, { labelWidth: 120, valueWidth: 130 }),
    monTable(['Workflow', 'Calls', 'Failed', 'Success rate', 'p95 latency'], e.workflows.map(w => [w.workflow, w.requests, w.failed, monPct(w.success_rate), monMs(w.p95_latency_ms)])));

  return `${filters}${cooling}${tiles}
    <div class="mon-grid">${providerCard}${opCard}</div>
    <div class="mon-grid mon-grid-3">${volumeCard}${workflowCard}${errorCard}</div>
    ${renderMonExtRequests()}`;
}

function renderMonExtRequests() {
  const r = D.monExtRequests; if (!r) return '';
  // Any value in the table can be clicked to filter by it.
  const pick = (key, value, text, extra = '') => value == null || value === '' ? esc(text ?? '—')
    : `<a href="#" class="mon-pick ${extra}" data-act="mon-ext-pick" data-key="${key}" data-value="${attr(value)}" title="Show only ${attr(value)}">${esc(text ?? value)}</a>`;
  const rows = r.items.map(x => `<tr class="click" data-act="mon-ext-request" data-id="${x.id}">
    <td>${esc(fmtDateTime(x.created_at))}</td><td>${pick('provider', x.provider)}</td>
    <td class="ellipsis" style="max-width:170px" title="${attr(x.operation || '')}">${pick('operation', x.operation)}</td>
    <td>${pick('workflow', x.workflow)}</td>
    <td>${x.status === 'FAILED'
      ? pick('error_type', x.error_type, MON_EXT_ERRORS[x.error_type] || 'failed', 'bad-pick')
        + (x.http_status_code ? ` ${pick('http_status_code', x.http_status_code)}` : '')
      : pill(x.http_status_code || 'ok', 'good')}</td>
    <td class="mono">${monMs(x.latency_ms)}</td><td class="mono">${x.retry_count}</td><td class="mono">${monUSD(x.estimated_cost)}</td></tr>`).join('');
  const filtered = monExtFiltered();
  return `<section class="card flush"><div class="row between wrap gap10" style="padding:14px 18px;border-bottom:1px solid var(--line2)">
      <div><div class="card-title">API calls</div><div class="card-sub">${r.total.toLocaleString()} matching${filtered ? ' with these filters' : ''} · click any value to filter by it, or a row for its safe detail (no headers, credentials or bodies are stored)</div></div>
      <div class="row gap8 xs muted">${btn('‹ Prev', 'mon-ext-page', { dir: -1 }, 'sm', r.page <= 1)}<span>Page ${r.page} of ${r.pages}</span>${btn('Next ›', 'mon-ext-page', { dir: 1 }, 'sm', r.page >= r.pages)}</div></div>
    ${r.items.length ? `<div class="mon-table-wrap" data-scroll-key="mon-external"><table class="mon-table"><thead><tr><th>Time</th><th>Provider</th><th>Operation</th><th>Workflow</th><th>Status</th><th>Latency</th><th>Retries</th><th>Cost</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div style="padding:18px">${filtered
        ? empty('No calls match these filters', 'Nothing in this range matches every filter at once. Clear one of them to widen the search.', btn('Reset filters', 'mon-ext-reset', {}, 'primary'))
        : empty('No external API calls', 'Calls appear here as searches, job boards and models are contacted.')}</div>`}</section>`;
}

/* ───────────── Apify ───────────── */
/* JD parsing and JD recovery moved to Running workers (app/static/workers.js: wkParsingInsights, wkRecoveryInsights). */

/* Application inbox: Gmail syncs, matching, classification and stage changes. Model calls are under LLM requests. */
function renderMonInbox() {
  const data = D.monInbox;
  if (!data) return loading();
  const x = data.metrics, conn = data.status?.connection;
  const tiles = `<section class="mon-tiles">
    ${monTile('Sync runs', (x.sync_runs || 0).toLocaleString(), x.sync_failures ? `${x.sync_failures} did not finish` : 'in range', x.sync_failures ? 'warn' : '')}
    ${monTile('Applications checked', (x.applications_checked || 0).toLocaleString(), 'submitted and still live')}
    ${monTile('Messages searched', (x.messages_searched || 0).toLocaleString(), `${x.messages_fetched || 0} read`)}
    ${monTile('Matched', (x.messages_matched || 0).toLocaleString(), `${x.deterministic_matches || 0} by rules · ${x.llm_assisted_matches || 0} model-confirmed · ${x.user_matches || 0} by you`)}
    ${monTile('Unmatched', (x.messages_unmatched || 0).toLocaleString(), 'kept as ids or headers only')}
    ${monTile('Needs review', (x.needs_review || 0).toLocaleString(), `${x.messages_needs_review || 0} emails to link`, x.needs_review ? 'warn' : '')}
    ${monTile('Stage changes', (x.stage_changes_applied || 0).toLocaleString(), `${x.stage_changes_suggested || 0} suggested · ${x.stage_changes_invalid || 0} refused`)}
    ${monTile('Mean confidence', x.mean_confidence == null ? '—' : monPct(x.mean_confidence), 'of analysed emails')}
    ${monTile('Corrections', (x.user_corrections || 0).toLocaleString(), 'made by you, never overwritten')}
    ${monTile('Model failures', (x.llm_failures || 0).toLocaleString(), `${x.schema_failures || 0} schema · ${x.pending_analysis || 0} waiting to retry`, x.llm_failures ? 'warn' : '')}
    ${monTile('Gmail requests', (x.gmail_requests?.total || 0).toLocaleString(), `${x.gmail_requests?.failed || 0} failed · ${x.gmail_requests?.rate_limited || 0} rate limited`, x.gmail_requests?.failed ? 'warn' : '')}
    ${monTile('Follow-ups suppressed', (x.followups_suppressed || 0).toLocaleString(), 'the employer had replied')}
  </section>`;
  const types = Object.entries(x.classifications || {});
  const byType = monCard('inbox-types', 'Classifications', `Rules ${x.classification_methods?.RULE || 0} · model ${x.classification_methods?.LLM || 0} · you ${x.classification_methods?.USER || 0}`,
    types.length ? monBars(types.map(([t, n]) => ({ _label: t, n })), r => r.n, r => String(r.n), r => r._label, { labelWidth: 200 }) : '<div class="small muted">No analysed emails in this range.</div>',
    monTable(['Event type', 'Emails'], types.map(([t, n]) => [t, n])));
  const models = Object.entries(x.models || {}).map(([k, n]) => `${esc(k)} (${n})`).join(', ') || 'none';
  const versions = Object.entries(x.prompt_versions || {}).map(([k, n]) => `${esc(k)} (${n})`).join(', ') || 'none';
  const state = `<section class="card mon-card"><div class="card-title mb12">Connection</div><div class="col gap8 small">
    <div>${conn ? `${esc(conn.email || '')} · ${esc(conn.status)}${conn.last_successful_sync_at ? ` · last synced ${esc(fmtDateTime(conn.last_successful_sync_at))}` : ''}` : 'Gmail is not connected'}</div>
    ${conn?.last_error ? `<div class="xs" style="color:var(--warn)">${esc(conn.last_error)}</div>` : ''}
    <div class="xs muted">Models: ${models} · prompt versions: ${versions}. Tokens and latency are in LLM requests (Inbox stage).</div></div></section>`;
  return `${tiles}<div class="mon-grid">${state}${byType}</div>`;
}

function renderMonApify() {
  const e = D.monExt;
  if (!e) return loading();
  const a = e.apify;
  const tiles = `<section class="mon-tiles">
    ${monTile('Runs today', (a.runs_today || 0).toLocaleString(), `${a.runs_this_month || 0} this month`)}
    ${monTile('Runs in range', (a.runs || 0).toLocaleString(), `${a.running} still running`)}
    ${monTile('Successful runs', (a.succeeded || 0).toLocaleString(), a.success_rate == null ? 'no finished runs' : `${monPct(a.success_rate)} of finished runs`)}
    ${monTile('Failed runs', (a.failed || 0).toLocaleString(), '', a.failed ? 'bad' : '')}
    ${monTile('Compute units', a.compute_units == null ? '—' : String(a.compute_units), a.compute_units == null ? 'Apify reported none' : `${a.runs_with_reported_usage} run${a.runs_with_reported_usage === 1 ? '' : 's'} reported usage`)}
    ${monTile('Dataset items', (a.dataset_items || 0).toLocaleString(), 'pulled from Apify')}
    ${monTile('Jobs found', (a.jobs_discovered || 0).toLocaleString(), a.jobs_deduplicated ? `${a.jobs_deduplicated} duplicates` : '')}
    ${monTile('After deduplication', (a.jobs_after_deduplication || 0).toLocaleString(), 'new jobs in the pipeline')}
    ${monTile('Reported cost', a.reported_cost == null ? '—' : monUSD(a.reported_cost), a.reported_cost == null ? 'Apify reported no cost' : `${a.runs_with_reported_cost} run${a.runs_with_reported_cost === 1 ? '' : 's'} priced by Apify`)}
    ${monTile('API calls', (a.api.requests || 0).toLocaleString(), `${monPct(a.api.success_rate)} success · p95 ${monMs(a.api.p95_latency_ms)}`)}
  </section>`;
  const group = (key, title, sub, labelKey) => {
    const rows = (a[key] || []).slice(0, 12).map(x => ({ ...x, _label: String(x[labelKey] || '—') }));
    return monCard(`apify-${key}`, title, sub,
      monBars(rows, x => x.runs, x => `${x.runs} run${x.runs === 1 ? '' : 's'} · ${x.jobs_created} new`,
        x => `${x._label}: ${x.runs} runs · ${x.jobs_discovered} found · ${x.jobs_created} new${x.compute_units == null ? '' : ` · ${x.compute_units} CU`}${x.reported_cost == null ? '' : ` · ${monUSD(x.reported_cost)}`}`, { labelWidth: 170, valueWidth: 140 }),
      monTable([title.replace('By ', ''), 'Runs', 'Succeeded', 'Failed', 'Found', 'New', 'Compute', 'Cost'],
        (a[key] || []).map(x => [x[labelKey] || '—', x.runs, x.succeeded, x.failed, x.jobs_discovered, x.jobs_created, x.compute_units ?? '—', x.reported_cost == null ? '—' : monUSD(x.reported_cost)])));
  };
  const sites = (a.by_site || []).map(x => ({ ...x, _label: x.site }));
  const siteCard = monCard('apify-sites', 'By board', 'Jobs discovered per board in this range',
    monBars(sites, x => x.jobs, x => x.jobs.toLocaleString(), x => `${x.site}: ${x.jobs} jobs`, { labelWidth: 120, valueWidth: 60 }),
    monTable(['Board', 'Jobs'], sites.map(x => [x.site, x.jobs])));
  const runs = a.recent_runs || [];
  const runRows = runs.map(x => `<tr><td>${esc(fmtDateTime(x.created_at))}</td>
    <td class="ellipsis" style="max-width:190px" title="${attr(x.actor_id)}"><span class="mono">${esc((x.actor_id || '').split('/').pop())}</span></td>
    <td>${esc(x.saved_search || '—')}</td><td>${esc(x.location || '—')}</td>
    <td>${x.status === 'SUCCEEDED' ? pill('succeeded', 'good') : x.finished_at ? pill(x.status.toLowerCase(), 'bad') : pill(x.status.toLowerCase(), 'warn')}</td>
    <td class="mono">${x.jobs_discovered ?? '—'}</td><td class="mono">${x.jobs_deduplicated ?? '—'}</td><td class="mono">${x.jobs_created ?? '—'}</td>
    <td class="mono">${x.compute_units ?? '—'}</td><td class="mono">${x.reported_cost == null ? '—' : monUSD(x.reported_cost)}</td>
    <td class="mono">${x.duration_ms == null ? '—' : monMs(x.duration_ms)}</td></tr>`).join('');
  const runTable = `<section class="card flush"><div style="padding:14px 18px;border-bottom:1px solid var(--line2)">
      <div class="card-title">Recent runs</div><div class="card-sub">Compute units and cost are Apify's own figures; a dash means Apify reported none.</div></div>
    ${runs.length ? `<div class="mon-table-wrap" data-scroll-key="mon-apify"><table class="mon-table"><thead><tr><th>Started</th><th>Actor</th><th>Saved search</th><th>Location</th><th>Status</th><th>Found</th><th>Duplicates</th><th>New</th><th>Compute</th><th>Cost</th><th>Duration</th></tr></thead><tbody>${runRows}</tbody></table></div>`
      : `<div style="padding:18px">${empty('No Apify runs', 'Runs appear here once a saved search starts one.')}</div>`}</section>`;
  return `${tiles}
    <div class="mon-grid">${group('by_actor', 'By actor', 'Runs and what each actor found', 'actor_id')}${group('by_saved_search', 'By saved search', 'Which setup started the runs', 'saved_search')}</div>
    <div class="mon-grid mon-grid-3">${group('by_location', 'By location', 'Runs per location searched', 'location')}${siteCard}${group('by_day', 'By day', 'Runs per day', 'day')}</div>
    ${runTable}`;
}

/* ───────────── Logs ───────────── */
function renderMonLogs() {
  const m = monState(), l = D.monLogs;
  if (!l) return loading();
  const f = m.logs;
  const opts = (key, all, values) => `<select class="select mon-select" data-act-change="mon-log-filter" data-key="${key}" aria-label="${attr(all)}">${[['', all], ...values].map(([v, t]) => `<option value="${attr(v)}" ${String(f[key]) === String(v) ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>`;
  const filters = `<section class="card pad16"><div class="row wrap gap8" style="align-items:center">
    ${f.trace_id ? `<span class="pill brand lg"><span class="pt">Trace ${esc(f.trace_id.slice(0, 12))}</span></span>${btn('Show all logs', 'mon-log-clear-trace', {}, 'sm')}` : ''}
    <input class="input mon-select" style="width:210px" placeholder="Search messages" value="${attr(f.search)}" data-act-change="mon-log-filter" data-key="search" aria-label="Search messages">
    ${opts('category', 'All categories', (l.categories?.length ? l.categories : l.all_categories).map(c => [c, c.replace(/_/g, ' ').toLowerCase()]))}
    ${opts('level', 'All levels', l.levels.map(v => [v, v.toLowerCase()]))}
    ${opts('provider', 'All providers', (l.providers || []).map(v => [v, v]))}
    ${opts('workflow', 'All workflows', (l.workflows || []).map(v => [v, v]))}
    ${BOOT.user?.is_admin ? `<label class="row gap6 small"><input type="checkbox" data-act-change="mon-all" ${m.all ? 'checked' : ''}> All accounts</label>` : ''}
    ${(f.category || f.level || f.provider || f.workflow || f.search) ? btn('Reset', 'mon-log-reset', {}, 'sm') : ''}
    <span class="grow"></span>${btn(label('monRefresh', 'Refresh', 'Refreshing…'), 'mon-refresh', {}, 'sm', isBusy('monRefresh'))}</div></section>`;
  const rows = l.items.map(x => `<tr class="click" data-act="mon-log" data-id="${x.id}">
    <td>${esc(fmtDateTime(x.created_at))}</td><td>${pill(x.level.toLowerCase(), MON_LEVEL_TONE[x.level] || 'muted')}</td>
    <td>${pill(x.category.replace(/_/g, ' ').toLowerCase(), MON_CATEGORY_TONE[x.category] || 'muted')}</td>
    <td>${esc(x.provider || '—')}</td><td>${esc(x.workflow || '—')}</td>
    <td class="mono xs ellipsis" style="max-width:90px" title="${attr(x.trace_id || '')}">${esc((x.trace_id || '—').slice(0, 8))}</td>
    <td>${esc(x.entity_type ? `${x.entity_type} #${x.entity_id ?? ''}` : '—')}</td>
    <td class="ellipsis" style="max-width:320px" title="${attr(x.message)}">${esc(x.message)}</td>
    <td class="mono">${x.duration_ms == null ? '—' : monMs(x.duration_ms)}</td><td>${esc(x.status || '—')}</td></tr>`).join('');
  return `${filters}<section class="card flush"><div class="row between wrap gap10" style="padding:14px 18px;border-bottom:1px solid var(--line2)">
      <div><div class="card-title">Logs</div><div class="card-sub">${l.total.toLocaleString()} event${l.total === 1 ? '' : 's'} · every LLM call, failed and slow API calls, scheduler runs and workflow milestones · open an LLM line for its prompt and response</div>${monCaptureSwitch()}</div>
      <div class="row gap8 xs muted">${btn('‹ Prev', 'mon-log-page', { dir: -1 }, 'sm', l.page <= 1)}<span>Page ${l.page} of ${l.pages}</span>${btn('Next ›', 'mon-log-page', { dir: 1 }, 'sm', l.page >= l.pages)}</div></div>
    ${l.items.length ? `<div class="mon-table-wrap" data-scroll-key="mon-logs"><table class="mon-table"><thead><tr><th>Time</th><th>Level</th><th>Category</th><th>Provider</th><th>Workflow</th><th>Trace</th><th>Entity</th><th>Message</th><th>Duration</th><th>Status</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div style="padding:18px">${empty('No log entries', 'Events appear here when an external call fails or crawls, and after each scheduled job.')}</div>`}</section>`;
}

const PRIORITY_TONE = { HIGH: 'bad', MEDIUM: 'warn', LOW: 'muted' };
function renderAdvisor() {
  const rec = D.monRec; if (!rec) return '';
  const r = rec.report, active = rec.tasks.find(t => ['PENDING', 'PROCESSING'].includes(t.status));
  const lastFailed = !active && rec.tasks[0]?.status === 'FAILED' ? rec.tasks[0] : null;
  const schedule = `<label class="row gap6 small muted">Scheduled analysis<select class="select mon-select" data-act-change="mon-schedule" aria-label="Scheduled analysis">${[['off', 'Off'], ['daily', 'Daily'], ['weekly', 'Weekly']].map(([v, t]) => `<option value="${v}" ${rec.schedule === v ? 'selected' : ''}>${t}</option>`).join('')}</select></label>`;
  const head = `<div class="row between wrap gap10 mb12"><div><div class="card-title">AI optimization</div><div class="card-sub">${esc(rec.notice)}</div></div>
    <div class="row gap10 wrap">${schedule}${btn(active ? (active.status === 'PENDING' ? 'Analysis queued…' : 'Analyzing…') : label('monAnalyze', 'Analyze AI usage', 'Queueing…'), 'mon-analyze', {}, 'primary', !!active || isBusy('monAnalyze'))}</div></div>`;
  const status = lastFailed ? `<div class="banner warn mb12"><span class="grow">The last analysis failed: ${esc(lastFailed.error || 'unknown error')}</span></div>` : '';
  if (!r) return `<section class="card">${head}${status}${empty('No recommendations yet', `Analyze the ${MON_RANGES.find(([id]) => id === monState().range)?.[1].toLowerCase()} of usage to get cost, latency, context and reliability recommendations.`)}</section>`;
  const recs = r.recommendations || [];
  const groups = ['HIGH', 'MEDIUM', 'LOW'].map(p => [p, recs.filter(x => x.priority === p)]).filter(([, items]) => items.length);
  const field = (name, value) => value ? `<div><div class="caps-sm">${name}</div><div class="small ink2" style="line-height:1.5">${esc(value)}</div></div>` : '';
  const body = groups.map(([p, items]) => `<div class="mt12"><div class="row gap8 mb8">${pill(p, PRIORITY_TONE[p])}<span class="xs muted">${items.length} recommendation${items.length === 1 ? '' : 's'}</span></div>
    ${items.map(x => `<details class="mon-rec"><summary><span class="b small">${esc(x.title)}</span><span class="xs muted">${esc(x.category.toLowerCase())} · ${esc(monStage(x.affected_stage) === 'ALL' ? 'all stages' : monStage(x.affected_stage))}${x.estimated_cost_saving && x.estimated_cost_saving !== 'unknown' ? ` · saving ${esc(x.estimated_cost_saving)}` : ''}</span></summary>
      <div class="mon-rec-body">${field('Finding', x.finding)}${field('Evidence', x.evidence)}${field('Recommended change', x.recommended_change)}${field('Expected benefit', x.expected_benefit)}${field('Estimated cost saving', x.estimated_cost_saving)}${field('Risk', x.risk)}
        <div class="xs faint">Recommendation only — nothing has been changed. Apply it yourself if you agree.</div></div></details>`).join('')}</div>`).join('');
  const source = r.generated_by === 'llm' ? `AI write-up by ${esc(r.model_name || r.model_provider || 'model')} · prompt ${esc(r.prompt_version || '')}` : 'Rule-based findings';
  return `<section class="card">${head}${status}
    <div class="box soft"><div class="small" style="line-height:1.55">${esc(r.summary)}</div>
      <div class="xs muted mt6">${source} · ${esc(fmtDay(r.time_range_start))} – ${esc(fmtDay(r.time_range_end))} · created ${esc(fmtDateTime(r.created_at))}${r.estimated_savings && r.estimated_savings !== 'unknown' ? ` · estimated savings ${esc(r.estimated_savings)}` : ''}</div></div>
    ${body || '<div class="small muted mt12">No recommendations: nothing in this period needs attention.</div>'}</section>`;
}

// Full prompt and response capture: off unless the account (or LLM_PROMPT_DEBUG on the server) turns it on.
function monCaptureSwitch() {
  const c = D.monCapture; if (!c) return '';
  if (c.server_wide) return '<div class="xs muted mt6">Full prompts and responses are captured for every account (LLM_PROMPT_DEBUG is on).</div>';
  return `<label class="check xs mt6" title="Stores the full prompt and response of each new LLM request, and the request and response bodies of external API calls, redacted, for ${c.retention_days} days. They include your resume and job text."><input type="checkbox" data-act-change="mon-capture" data-on="${c.enabled ? '' : '1'}" ${c.enabled ? 'checked' : ''} ${isBusy('monCapture') ? 'disabled' : ''}>Capture full requests and responses (LLM and external APIs)${c.enabled ? ` · kept ${c.retention_days} days` : ''}</label>`;
}
function monPayloads(x) {
  if (!x.debug) {
    const c = D.monCapture;
    return `<div class="box soft"><div class="caps-sm mb4">INPUT AND OUTPUT</div><div class="small muted">The prompt and response were not captured for this request; only metadata and token counts were kept.</div>
      ${c && !c.enabled && !c.server_wide ? `<div class="mt8">${btn('Capture them for new requests', 'mon-capture', { on: 1 }, 'sm')}</div>` : ''}</div>`;
  }
  const block = (title, text, open) => `<details ${open ? 'open' : ''}><summary class="small b row between"><span>${title} <span class="faint" style="font-weight:400">· ${(text || '').length.toLocaleString()} characters</span></span></summary>
    <div class="row mt6" style="justify-content:flex-end">${btn('Copy', 'mon-copy', {}, 'sm')}</div><pre class="mon-debug">${esc(text || '(empty)')}</pre></details>`;
  return `<div class="col gap10"><div class="caps-sm">INPUT AND OUTPUT <span class="faint" style="font-weight:400">· redacted: keys and tokens are masked</span></div>
    ${block('Input · the full prompt sent (system, then user message)', x.debug.prompt, x.status === 'FAILED')}
    ${block('Output · the model’s response as received', x.debug.response || (x.status === 'FAILED' ? 'No response was received.' : ''), true)}</div>`;
}

function renderMonRequestModal() {
  const x = D.monRequest;
  if (!x || x.id !== monState().request) return `<div class="overlay" data-act="mon-close"><div class="modal" style="padding:40px">${loading()}</div></div>`;
  const row = (k, v) => `<div class="mon-kv"><span class="muted">${esc(k)}</span><span>${v}</span></div>`;
  const breakdown = Object.entries(x.context_breakdown || {}).filter(([, v]) => v).map(([k, v]) => ({ _label: k.replace(/_/g, ' '), v }));
  const entity = x.entity ? (x.entity.title ? `${esc(x.entity.title)} · ${esc(x.entity.company)} (${esc(x.entity.type)} #${x.entity.id})` : `${esc(x.entity.type)} #${x.entity.id}`) : '—';
  return `<div class="overlay" data-act="mon-close"><div class="modal" role="dialog" aria-label="LLM request detail" style="max-width:720px">
    <div class="modal-head"><div class="row between gap10"><div><div style="font-size:19px;font-weight:600">${esc(monStage(x.stage))} request</div><div class="muted small mt4">${esc(fmtDateTime(x.created_at))} · ${esc(x.request_id)}</div></div>
      ${x.status === 'FAILED' ? pill(MON_ERRORS[x.error_type] || 'failed', 'bad') : pill('succeeded', 'good')}</div></div>
    <div style="padding:18px 22px" class="col gap14">
      <div class="mon-kv-grid">
        ${row('Workflow', esc(x.workflow_type || '—'))}${row('Job / application', entity)}
        ${row('Provider', esc(x.provider))}${row('Model', esc(x.model || '—') + (x.requested_model && x.requested_model !== x.model ? ` <span class="faint">(requested ${esc(x.requested_model)})</span>` : ''))}
        ${row('Prompt version', esc(x.prompt_name ? `${x.prompt_name} ${x.prompt_version || ''}` : x.prompt_version || '—'))}${row('Timestamp', esc(fmtDateTime(x.created_at)))}
        ${row('Input tokens', `${monTokens(x.input_tokens)}${x.usage_source === 'estimated' ? ' <span class="faint">(estimated)</span>' : x.usage_source === 'none' ? ' <span class="faint">(not reported)</span>' : ''}`)}${row('Output tokens', monTokens(x.output_tokens))}
        ${row('Context utilisation', x.context_utilization_percent == null ? `${monTokens(x.context_tokens)} tokens · window unknown` : `${(x.context_tokens || 0).toLocaleString()} / ${(x.context_window_size || 0).toLocaleString()} tokens · ${x.context_utilization_percent.toFixed(1)}%`)}
        ${row('Cached tokens', x.cache_read_tokens == null ? '—' : `${monTokens(x.cache_read_tokens)} read${x.cache_write_tokens ? ` · ${monTokens(x.cache_write_tokens)} written` : ''}`)}
        ${row('Latency', monMs(x.latency_ms))}${row('Estimated cost', `${monUSD(x.estimated_cost)} <span class="faint">(${esc(x.cost_source.replace(/_/g, ' '))}${x.pricing_effective_from ? `, prices from ${esc(x.pricing_effective_from)}` : ''})</span>`)}
        ${x.worker_type ? `${row('Worker', esc(monWorker(x.worker_type)))}${row('Configured', `${esc(x.configured_model || '—')} <span class="faint">(${esc((x.model_mode || '').replace('_', ' ').toLowerCase() || 'mode unknown')}${x.config_version ? `, config v${x.config_version}` : ''})</span>`)}
        ${row('Requested model', esc(x.requested_model || '—'))}${row('Model group that answered', esc(x.model_group || '—'))}
        ${row('Routing tier', esc(x.routing_tier || '—') + (x.routing_cause ? ` <span class="faint">(${esc(x.routing_cause)})</span>` : ''))}${row('Fallback', x.fallback_used == null ? 'not reported' : x.fallback_used ? 'LiteLLM used the fallback model' : 'no')}
        ${row('Escalation', x.escalated ? `escalated (${esc(x.escalation_reason || '')})` : 'no')}${row('Confidence', x.confidence == null ? '—' : String(x.confidence))}
        ${row('LiteLLM call id', `<span class="mono">${esc(x.litellm_call_id || '—')}</span>`)}${row('External API request', `<span class="mono">${esc(x.external_request_id || '—')}</span>`)}
        ${row('Trace', `<span class="mono">${esc(x.trace_id || '—')}</span>`)}` : ''}
        ${row('Retry count', x.retry_count)}${row('Structured output', x.structured_output_valid == null ? 'not checked' : x.structured_output_valid ? 'valid' : `invalid${x.validation_failure_reason ? ` (${esc(monSchemaReason(x.validation_failure_reason))})` : ''}${x.structured_output_retry_count ? ` · ${x.structured_output_retry_count} earlier failure${x.structured_output_retry_count === 1 ? '' : 's'}` : ''}`)}
        ${row('Schema enforcement', esc(MON_SCHEMA_ENFORCEMENT[x.schema_enforcement] || x.schema_enforcement || 'not recorded'))}${row('Context builder', esc(x.context_builder_version || '—'))}
      </div>
      ${breakdown.length ? `<div><div class="caps-sm mb8">Estimated context by source</div>${monBars(breakdown, b => b.v, b => `${monTokens(b.v)} tokens`, b => `${b._label}: ${b.v.toLocaleString()} estimated tokens`, { labelWidth: 130, valueWidth: 96 })}</div>` : ''}
      ${x.error_message ? `<div class="box bad"><div class="xs b mb4" style="color:var(--bad)">ERROR</div><div class="small mono" style="white-space:pre-wrap;word-break:break-word">${esc(x.error_message)}</div></div>` : ''}
      ${x.trace_id ? `<div class="box soft row between wrap gap10"><div><div class="caps-sm">LOGS</div><div class="small mt4">${x.trace_log_count ? `${x.trace_log_count} log line${x.trace_log_count === 1 ? '' : 's'} written by the same run (trace <span class="mono">${esc(x.trace_id.slice(0, 12))}</span>)` : 'No log lines were written by this run'}</div></div>
        ${x.trace_log_count ? btn('View logs', 'mon-logs-trace', { id: x.trace_id }, 'sm') : ''}</div>` : ''}
      ${monPayloads(x)}
    </div>
    <div class="card-foot row gap8" style="justify-content:flex-end">${x.entity?.job_id ? btn('View job orchestration', 'orch-view-job', { id: x.entity.job_id }) : ''}${x.entity?.job_id ? btn('Open job', 'mon-open-job', { id: x.entity.job_id }) : ''}${btn('Close', 'mon-close')}</div></div></div>`;
}

// The captured request and response of an external call (kept only while capture is on, redacted, for a week).
function monCaptured(p, failed) {
  if (!p || (!p.request && !p.response)) {
    const c = D.monCapture;
    return `<div class="box soft"><div class="caps-sm mb4">REQUEST AND RESPONSE</div><div class="small muted">The request and response bodies were not captured for this call; only its size and status were kept.</div>
      ${c && !c.enabled && !c.server_wide ? `<div class="mt8">${btn('Capture them for new calls', 'mon-capture', { on: 1 }, 'sm')}</div>` : ''}</div>`;
  }
  const pretty = text => { try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; } };
  const headers = h => Object.entries(h || {}).map(([k, v]) => `${k}: ${v}`).join('\n');
  const block = (title, head, part, open) => {
    if (!part) return '';
    const text = [head, headers(part.headers), '', part.body ? pretty(part.body) : '(no body)'].filter(x => x != null).join('\n');
    return `<details ${open ? 'open' : ''}><summary class="small b row between"><span>${title} <span class="faint" style="font-weight:400">· ${(part.body || '').length.toLocaleString()} characters</span></span></summary>
      <div class="row mt6" style="justify-content:flex-end">${btn('Copy', 'mon-copy', {}, 'sm')}</div><pre class="mon-debug">${esc(text)}</pre></details>`;
  };
  return `<div class="col gap10"><div class="caps-sm">REQUEST AND RESPONSE <span class="faint" style="font-weight:400">· redacted: keys, tokens and credential headers are masked</span></div>
    ${block('Request', p.request ? `${p.request.method || ''} ${p.request.url || ''}` : '', p.request, false)}
    ${block('Response', p.response ? `HTTP ${p.response.status ?? ''}` : '', p.response, true)}
    ${p.error ? `<div class="box bad small mono" style="white-space:pre-wrap;word-break:break-word">${esc(p.error)}</div>` : ''}</div>`;
}

/* Detail views: metadata, plus the request and response bodies when capture was on for the call. */
function renderMonExtRequestModal() {
  const x = D.monExtRequest;
  if (!x || x.id !== monState().ext.request) return `<div class="overlay" data-act="mon-close"><div class="modal" style="padding:40px">${loading()}</div></div>`;
  const row = (k, v) => `<div class="mon-kv"><span class="muted">${esc(k)}</span><span>${v}</span></div>`;
  const meta = Object.entries(x.metadata || {}).filter(([, v]) => v !== null && v !== '');
  return `<div class="overlay" data-act="mon-close"><div class="modal" role="dialog" aria-label="External API call detail" style="max-width:720px">
    <div class="modal-head"><div class="row between gap10"><div><div style="font-size:19px;font-weight:600">${esc(x.provider)} · ${esc(x.operation || 'request')}</div>
      <div class="muted small mt4">${esc(fmtDateTime(x.created_at))} · ${esc(x.request_id)}</div></div>
      ${x.status === 'FAILED' ? pill(MON_EXT_ERRORS[x.error_type] || 'failed', 'bad') : pill('succeeded', 'good')}</div></div>
    <div style="padding:18px 22px" class="col gap14">
      <div class="mon-kv-grid">
        ${row('Trace', x.trace_id ? `<a href="#" class="mono" data-act="mon-trace" data-id="${attr(x.trace_id)}">${esc(x.trace_id.slice(0, 12))}…</a>` : '—')}
        ${row('Workflow', esc(x.workflow || '—'))}
        ${row('Service', esc(x.service || '—'))}${row('Operation', esc(x.operation || '—'))}
        ${row('Method', esc(x.http_method || '—'))}${row('Host', esc(x.endpoint_host || '—'))}
        ${row('Path', esc(x.endpoint_path || '—'))}${row('HTTP status', x.http_status_code ?? '—')}
        ${row('Latency', monMs(x.latency_ms))}${row('Attempt', `${x.attempt} (${x.retry_count} retr${x.retry_count === 1 ? 'y' : 'ies'})`)}
        ${row('Request size', x.request_size_bytes == null ? '—' : `${x.request_size_bytes.toLocaleString()} bytes`)}
        ${row('Response size', x.response_size_bytes == null ? '—' : `${x.response_size_bytes.toLocaleString()} bytes`)}
        ${row('Rate limit left', x.rate_limit_remaining ?? '—')}${row('Rate limit resets', x.rate_limit_reset_at ? esc(fmtDateTime(x.rate_limit_reset_at)) : '—')}
        ${row('Cost', x.estimated_cost == null ? '— <span class="faint">(not reported)</span>' : `${monUSD(x.estimated_cost)} <span class="faint">(${esc(x.cost_source.replace(/_/g, ' '))})</span>`)}
        ${row('Job / application', x.entity ? esc(x.entity.label) : x.job_id ? `job #${x.job_id}` : '—')}
      </div>
      ${x.llm ? `<div class="box soft"><div class="caps-sm mb8">LLM request</div><div class="small">${esc(monStage(x.llm.stage))} · ${esc(x.llm.model || '')} · ${monTokens(x.llm.input_tokens)} in / ${monTokens(x.llm.output_tokens)} out · ${monUSD(x.llm.estimated_cost)}</div></div>` : ''}
      ${meta.length ? `<div><div class="caps-sm mb8">Safe metadata</div><div class="mon-kv-grid">${meta.map(([k, v]) => row(k.replace(/_/g, ' '), esc(typeof v === 'object' ? JSON.stringify(v) : String(v)))).join('')}</div></div>` : ''}
      ${x.error_message ? `<div class="box bad"><div class="xs b mb4" style="color:var(--bad)">ERROR</div><div class="small mono" style="white-space:pre-wrap;word-break:break-word">${esc(x.error_message)}</div></div>` : ''}
      ${monCaptured(x.payload, x.status === 'FAILED')}
      <div class="xs faint">Credentials are never stored. Bodies and headers are kept, redacted, only while capture is on (Logs), for a week.</div>
    </div>
    <div class="card-foot row gap8" style="justify-content:flex-end">${btn('Close', 'mon-close')}</div></div></div>`;
}

function renderMonLogModal() {
  const x = D.monLog;
  if (!x || x.id !== monState().logs.entry) return `<div class="overlay" data-act="mon-close"><div class="modal" style="padding:40px">${loading()}</div></div>`;
  const row = (k, v) => `<div class="mon-kv"><span class="muted">${esc(k)}</span><span>${v}</span></div>`;
  const details = Object.entries(x.details || {}).filter(([, v]) => v !== null && v !== '');
  const call = x.external_request;
  return `<div class="overlay" data-act="mon-close"><div class="modal" role="dialog" aria-label="Log entry" style="max-width:720px">
    <div class="modal-head"><div class="row between gap10"><div><div style="font-size:19px;font-weight:600">${esc(x.message)}</div>
      <div class="muted small mt4">${esc(fmtDateTime(x.created_at))}</div></div>${pill(x.level.toLowerCase(), MON_LEVEL_TONE[x.level] || 'muted')}</div></div>
    <div style="padding:18px 22px" class="col gap14">
      <div class="mon-kv-grid">
        ${row('Category', esc(x.category.replace(/_/g, ' ').toLowerCase()))}${row('Provider', esc(x.provider || '—'))}
        ${row('Operation', esc(x.operation || '—'))}${row('Workflow', esc(x.workflow || '—'))}
        ${row('Trace', x.trace_id ? `<span class="mono">${esc(x.trace_id)}</span>` : '—')}
        ${row('Entity', esc(x.entity_type ? `${x.entity_type} #${x.entity_id ?? ''}` : '—'))}
        ${row('Status', esc(x.status || '—'))}${row('Duration', x.duration_ms == null ? '—' : monMs(x.duration_ms))}
      </div>
      ${call ? `<div class="box soft"><div class="caps-sm mb8">The call it describes</div>
        <div class="small">${esc(call.provider)} ${esc(call.http_method || '')} ${esc(call.endpoint_host || '')}${esc(call.endpoint_path || '')} · ${call.http_status_code ?? '—'} · ${monMs(call.latency_ms)}</div>
        <div class="mt8">${btn('Open the call', 'mon-ext-request', { id: call.id }, 'sm')}</div></div>${monCaptured(call.payload, call.status === 'FAILED')}` : ''}
      ${x.llm_requests?.length ? `<div class="box soft"><div class="caps-sm mb8">LLM request${x.llm_requests.length === 1 ? '' : 's'} behind this line</div>
        ${x.llm_requests.map(q => `<div class="row between gap10 small" style="padding:3px 0"><span class="ellipsis">${esc(monStage(q.stage))} · ${esc(q.model || '—')} · ${esc(q.status.toLowerCase())}${q.captured ? ' · prompt and response captured' : ''}</span>${btn('Open request', 'mon-log-request', { id: q.id }, 'sm')}</div>`).join('')}</div>` : ''}
      ${x.llm_requests?.length === 1 ? monPayloads({ status: x.llm_requests[0].status, debug: x.llm_requests[0].payload }) : ''}
      ${details.length ? `<div><div class="caps-sm mb8">Details</div><div class="mon-kv-grid">${details.map(([k, v]) => row(k.replace(/_/g, ' '), esc(typeof v === 'object' ? JSON.stringify(v) : String(v)))).join('')}</div></div>` : ''}
      ${x.related?.length ? `<div><div class="caps-sm mb8">Same trace</div>${x.related.map(r => `<div class="row between gap10 small" style="padding:3px 0"><span class="ellipsis">${esc(r.message)}</span><span class="xs faint none">${esc(fmtDateTime(r.created_at))}</span></div>`).join('')}</div>` : ''}
      <div class="xs faint">Messages and details are redacted before they are stored: no credentials. Prompts, responses and request/response bodies are kept only while capture is on.</div>
    </div>
    <div class="card-foot row gap8" style="justify-content:flex-end">${x.entity_type === 'job' && x.entity_id ? btn('View job orchestration', 'orch-view-job', { id: x.entity_id }) : ''}${btn('Close', 'mon-close')}</div></div></div>`;
}

const MONITORING_ACTIONS = {
  'mon-range': el => { Object.assign(monState(), { range: el.dataset.id, page: 1 }); load(); },
  'mon-filter': el => { monState()[el.dataset.key] = el.dataset.key === 'job_id' ? el.value.replace(/\D/g, '') : el.value; monState().page = 1; load(); },
  'mon-perf-by': el => { monState().perfBy = el.dataset.id; load(); },
  'mon-perf-dim': el => { monState().perfDim = el.dataset.id; render(); },
  'mon-all': el => { Object.assign(monState(), { all: el.checked, page: 1 }); load(); },
  'mon-reset': () => { Object.assign(monState(), { provider: '', model: '', stage: '', prompt_version: '', status: '', job_id: '', all: false, page: 1 }); load(); },
  'mon-refresh': () => busy('monRefresh', () => loadMonitoring()),
  'mon-table': el => { const t = monState().tables; t.has(el.dataset.key) ? t.delete(el.dataset.key) : t.add(el.dataset.key); render(); },
  'mon-page': el => { monState().page = Math.max(1, monState().page + Number(el.dataset.dir)); load(); },
  'mon-request': async el => {
    monState().request = Number(el.dataset.id); D.monRequest = null; render();
    try { D.monRequest = await api(`/api/monitoring/requests/${el.dataset.id}?` + qs({ all_accounts: monState().all ? 'true' : '' })); }
    catch (e) { monState().request = null; toast(e.message, true); }
    render();
  },
  'mon-close': (el, e) => {
    if (e && el.classList.contains('overlay') && e.target !== el) return;
    const m = monState(); m.request = null; m.ext.request = null; m.logs.entry = null; render();
  },
  'mon-tab': (el, e) => { e?.preventDefault?.(); Object.assign(monState(), { tab: el.dataset.id }); load(); },
  'mon-ext-filter': el => { monState().ext[el.dataset.key] = el.value; monState().ext.page = 1; load(); },
  'mon-ext-errors': el => { Object.assign(monState().ext, { error_only: el.checked, page: 1 }); load(); },
  'mon-ext-reset': () => { Object.assign(monState().ext, { provider: '', operation: '', workflow: '', status: '', error_type: '', http_status_code: '', error_only: false, page: 1 }); load(); },
  'mon-ext-clear': el => { const e = monState().ext; e[el.dataset.key] = el.dataset.key === 'error_only' ? false : ''; e.page = 1; load(); },
  // Clicking a value in the table filters by it, which is quicker than finding it in a list.
  'mon-ext-pick': (el, ev) => { ev?.stopPropagation?.(); Object.assign(monState().ext, { [el.dataset.key]: el.dataset.value, page: 1 }); load(); },
  'mon-ext-page': el => { const e = monState().ext; e.page = Math.max(1, e.page + Number(el.dataset.dir)); load(); },
  'mon-ext-request': async el => {
    const m = monState(); m.ext.request = Number(el.dataset.id); m.logs.entry = null; D.monExtRequest = null; render();
    try { D.monExtRequest = await api(`/api/monitoring/external/requests/${el.dataset.id}?` + qs({ all_accounts: m.all ? 'true' : '' })); }
    catch (err) { m.ext.request = null; toast(err.message, true); }
    render();
  },
  'mon-trace': async (el, e) => {
    e?.preventDefault?.();
    const m = monState();
    Object.assign(m.logs, { entry: null }); m.ext.request = null;
    try {
      const trace = await api(`/api/monitoring/external/traces/${el.dataset.id}?` + qs({ all_accounts: m.all ? 'true' : '' }));
      toast(`Trace ${el.dataset.id.slice(0, 8)}: ${trace.calls.length} call${trace.calls.length === 1 ? '' : 's'} across ${trace.providers.join(', ') || 'no providers'} · ${monMs(trace.total_latency_ms)} total`);
    } catch (err) { toast(err.message, true); }
    render();
  },
  'mon-log-filter': el => { monState().logs[el.dataset.key] = el.value; monState().logs.page = 1; load(); },
  'mon-log-reset': () => { Object.assign(monState().logs, { category: '', level: '', provider: '', workflow: '', search: '', trace_id: '', page: 1 }); load(); },
  'mon-log-page': el => { const l = monState().logs; l.page = Math.max(1, l.page + Number(el.dataset.dir)); load(); },
  'mon-log': async el => {
    const m = monState(); m.logs.entry = Number(el.dataset.id); D.monLog = null; render();
    try { D.monLog = await api(`/api/monitoring/logs/${el.dataset.id}?` + qs({ all_accounts: m.all ? 'true' : '' })); }
    catch (err) { m.logs.entry = null; toast(err.message, true); }
    render();
  },
  'mon-open-job': el => { monState().request = null; openJob(el.dataset.id); },
  // From an LLM request to the log lines its run wrote, and back.
  'mon-logs-trace': el => {
    const m = monState();
    m.request = null; D.monRequest = null;
    Object.assign(m.logs, { category: '', level: '', provider: '', workflow: '', search: '', trace_id: el.dataset.id, page: 1, entry: null });
    m.tab = 'logs'; load();
  },
  'mon-log-clear-trace': () => { Object.assign(monState().logs, { trace_id: '', page: 1 }); load(); },
  'mon-log-request': el => { monState().logs.entry = null; MONITORING_ACTIONS['mon-request'](el); },
  'mon-capture': el => busy('monCapture', async () => {
    D.monCapture = await api('/api/monitoring/capture', { method: 'PUT', body: { enabled: !!el.dataset.on } });
    return D.monCapture;
  }, c => c.enabled ? 'New LLM requests keep their full prompt and response' : 'Capture off: new requests keep metadata only'),
  'mon-copy': el => copyText(el.closest('details')?.querySelector('pre')?.textContent || ''),
  'mon-analyze': () => busy('monAnalyze', async () => {
    await api('/api/monitoring/analyze', { method: 'POST', body: { range: monState().range === 'today' ? 'today' : monState().range } });
    await loadMonitoring();
  }, 'Analysis queued. Recommendations appear here when it finishes.'),
  'mon-schedule': el => busy('monSchedule', async () => {
    await api('/api/monitoring/schedule', { method: 'PUT', body: { schedule: el.value } });
    D.monRec = { ...D.monRec, schedule: el.value };
  }, `Scheduled analysis ${el.value === 'off' ? 'turned off' : `set to ${el.value}`}`),
};

// Hover details for chart marks: one tooltip element follows the pointer over any [data-tip].
(() => {
  let tip = null;
  const place = (e, text) => {
    if (!tip) { tip = document.createElement('div'); tip.className = 'viz-tip'; tip.setAttribute('role', 'tooltip'); document.body.appendChild(tip); }
    tip.textContent = text; tip.hidden = false;
    const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = `${Math.min(window.innerWidth - w - 8, e.clientX + pad)}px`;
    tip.style.top = `${Math.max(8, e.clientY - h - pad)}px`;
  };
  document.addEventListener('mousemove', e => {
    const el = e.target.closest?.('[data-tip]');
    if (el) place(e, el.dataset.tip); else if (tip) tip.hidden = true;
  });
  document.addEventListener('scroll', () => { if (tip) tip.hidden = true; }, true);
  // Keyboard: a focused [data-tip] control (e.g. a Running workers ⓘ) shows its tip beside it.
  document.addEventListener('focusin', e => {
    const el = e.target.closest?.('[data-tip]');
    let keyboard = true;
    try { keyboard = el?.matches(':focus-visible'); } catch { /* older engines: show it */ }
    if (!el || !keyboard) return;
    const box = el.getBoundingClientRect();
    place({ clientX: box.right, clientY: box.top }, el.dataset.tip);
  });
  document.addEventListener('focusout', () => { if (tip) tip.hidden = true; });
})();
