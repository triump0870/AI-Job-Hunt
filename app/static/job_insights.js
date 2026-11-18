/* Job Insights (app.services.job_insights): the researched package for an approved job — compensation, company,
   interview and questions to ask — in the job modal's Insights tab, as a card on Approvals, and as stage prep in the
   Tracker. Every item says what it is: a source fact, an interpretation, generated preparation or unknown. Generated
   questions are never shown as reported ones, and a market estimate is never shown as the employer's band. */
'use strict';

const INS_SECTIONS = [['overview', 'Overview'], ['compensation', 'Compensation'], ['company', 'Company'],
  ['interview', 'Interview'], ['form', 'Application form'], ['ask', 'Questions to ask'], ['sources', 'Sources']];
// Application-form question status → [label, tone]. A draft is never shown as the person's own answer.
const INS_FORM_STATUS = { LIBRARY: ['From your library', 'good'], DRAFTED: ['Draft', 'warn'], NEEDS_INPUT: ['Needs your answer', 'muted'],
  PENDING: ['Drafting…', 'brand'] };
// [label, tone]: what kind of statement an item is. Colour is never the only signal: the word is always shown.
const INS_KIND = { FACT: ['Source fact', 'good'], DERIVED: ['Interpretation', 'brand'], GENERATED: ['Generated prep', 'warn'],
  UNKNOWN: ['Unknown', 'muted'] };
const INS_SOURCE_TONE = { OK: 'good', NO_DATA: 'muted', FAILED: 'bad', BLOCKED: 'bad', NOT_CONFIGURED: 'muted', NEEDS_REVIEW: 'warn', NOT_RUN: 'muted' };
const INS_SOURCE_WORD = { OK: 'available', NO_DATA: 'no data', FAILED: 'failed', BLOCKED: 'blocked', NOT_CONFIGURED: 'not configured',
  NEEDS_REVIEW: 'needs review', NOT_RUN: 'not researched yet' };
const INS_ASK_STAGES = [['RECRUITER', 'Recruiter'], ['HIRING_MANAGER', 'Hiring manager'], ['TECHNICAL_INTERVIEWER', 'Technical interviewer'],
  ['ENGINEERING_LEADER', 'Engineering leader'], ['FINAL_HR', 'Final / HR']];
const INS_COMP_STATUS = { EMPLOYER_POSTED: 'Employer posted', RECRUITER_PROVIDED: 'Recruiter budget', MULTI_SOURCE_CONSENSUS: 'Multi-source consensus',
  SINGLE_SOURCE_ESTIMATE: 'Single-source estimate', CONFLICTING_ESTIMATES: 'Conflicting estimates', UNKNOWN: 'Unknown' };
const INS_POLL = 4000;
let insTimer = null;

function insState() {
  if (!S.ins) S.ins = { tab: 'overview' };
  return S.ins;
}
const insKind = kind => { const [text, tone] = INS_KIND[kind] || INS_KIND.UNKNOWN; return pill(text, tone); };
const insWords = s => (s || '').toLowerCase().replace(/_/g, ' ');
// 185000 → "185K"; small figures (hourly) keep their digits.
function insAmount(v) {
  if (v == null || v === '') return '?';
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  if (n >= 1e6) return `${+(n / 1e6).toFixed(2)}M`;
  if (n >= 1e4) return `${Math.round(n / 1e3)}K`;
  return n.toLocaleString();
}
// A range exactly as its source states it (currency and period kept), or "Not listed".
function insRange(r) {
  if (!r || r.low == null) return 'Not listed';
  const period = r.period && r.period !== 'year' ? ` / ${r.period}` : '';
  const span = r.high != null && r.high !== r.low ? `${insAmount(r.low)}–${insAmount(r.high)}` : insAmount(r.low);
  return `${r.currency ? r.currency + ' ' : ''}${span}${period}`;
}
const insAgo = iso => {
  if (!iso) return '';
  const mins = Math.max(0, Math.round((Date.now() - toDate(iso).getTime()) / 60000));
  return mins < 60 ? `${mins}m ago` : mins < 2880 ? `${Math.round(mins / 60)}h ago` : `${Math.round(mins / 1440)}d ago`;
};
// A link only for http(s) URLs: a source's `javascript:` or `data:` URL is shown as plain text, never made clickable.
function insLink(url, text, cls = '') {
  let ok = false;
  try { ok = ['http:', 'https:'].includes(new URL(url).protocol); } catch { ok = false; }
  return ok ? `<a class="${cls}" href="${attr(url)}" target="_blank" rel="noopener noreferrer">${esc(text)}</a>` : esc(text);
}
const insSourcesNote = ids => ids?.length ? `<span class="xs faint">${ids.length} source${ids.length === 1 ? '' : 's'}</span>` : '';

/* ───────────── loading ───────────── */
async function loadInsights(jobId) {
  const data = await api(`/api/jobs/${jobId}/insights`);
  D.insights = { ...(D.insights || {}), [jobId]: data };
  scheduleInsPoll(jobId);
  return data;
}
function insVisible(jobId) {
  return (S.jobOpen === jobId && S.jobTab === 'insights') || (S.screen === 'approvals' && S.approvalJob === jobId);
}
// While research runs, look again every few seconds — only while its insights are on screen. A hidden tab or one
// failed request waits for the next tick rather than stopping; a closed view stops, and reopening reloads.
function scheduleInsPoll(jobId) {
  clearTimeout(insTimer); insTimer = null;
  if (!D.insights?.[jobId]?.researching) return;
  insTimer = setTimeout(async () => {
    if (!insVisible(jobId)) return;
    if (document.hidden) { scheduleInsPoll(jobId); return; }
    try { await loadInsights(jobId); } catch { scheduleInsPoll(jobId); return; }
    render();
  }, INS_POLL);
}
const insLoading = new Set();
function insEnsure(jobId) {
  // Cached insights are reused, except a copy that still says research is running: that one is read again.
  if (!jobId || (D.insights?.[jobId] && !D.insights[jobId].researching) || insLoading.has(jobId)) return;
  insLoading.add(jobId);
  loadInsights(jobId).then(render).catch(e => toast(e.message, true)).finally(() => insLoading.delete(jobId));
}

/* ───────────── pieces ───────────── */
function insHeader(x, jobId) {
  const researching = x.researching;
  const never = x.status === 'NONE' && !researching;
  const action = btn(label('insrefresh' + jobId, never ? 'Find insights' : 'Refresh', 'Queueing…'), 'ins-refresh', { id: jobId }, never ? 'primary sm' : 'sm', isBusy('insrefresh' + jobId) || researching);
  const formOnly = x.task?.scope === 'APPLICATION_FORM';
  const state = researching ? `${pill(formOnly ? 'Drafting answers…' : x.task?.status === 'PROCESSING' ? 'Researching…' : 'Queued', 'brand')} <span class="xs muted">${formOnly ? 'Drafting your form answers' : 'Research runs'} in the background; this updates on its own.</span>`
    : x.last_successful_at ? `<span class="xs muted">Last refreshed ${esc(insAgo(x.last_successful_at))}</span>${x.status === 'PARTIAL' ? ` ${pill('Partial', 'warn')}` : ''}`
    : never ? `<span class="xs muted">${x.eligible ? 'Not researched yet.' : 'Research starts automatically when the job is approved; you can start it now.'}</span>` : '';
  const failed = x.refresh_failed ? `<div class="banner warn mt8" style="display:block"><b>Refresh failed.</b> ${x.last_successful_at ? `Last successful data: ${esc(fmtDateTime(x.last_successful_at))}.` : 'No research has succeeded yet.'}${x.last_error ? ` <span class="xs">${esc(x.last_error)}</span>` : ''}</div>` : '';
  return `<div class="ins-head"><div class="row between wrap gap8"><div class="row gap8 wrap">${state}</div>${action}</div>${failed}</div>`;
}
function insTabs(active) {
  return `<div class="tabs under ins-tabs">${INS_SECTIONS.map(([id, t]) => `<button class="${active === id ? 'on' : ''}" data-act="ins-tab" data-id="${id}">${t}</button>`).join('')}</div>`;
}
function insItem(text, kind, extra = '') {
  return `<div class="ins-item"><div class="grow small pretty">${esc(text)}${extra}</div>${insKind(kind)}</div>`;
}

function insOverview(x) {
  const o = x.overview || {}, comp = x.compensation || {}, co = x.company || {}, iv = o.interview || {};
  const range = comp.recommended_discussion_range;
  return `<div class="ins-grid">
    <div class="box"><div class="caps mb8">COMPENSATION</div><div class="base b">${esc(range ? insRange(range) : 'Not known')}</div>
      <div class="xs muted mt4">${esc(range?.label || 'No salary data yet')} · ${esc(INS_COMP_STATUS[comp.status] || 'Unknown')} · confidence ${esc(insWords(comp.confidence || 'none'))}</div></div>
    <div class="box"><div class="caps mb8">INTERVIEW</div><div class="small">${iv.reported_rounds ? `${iv.reported_rounds} reported stage${iv.reported_rounds === 1 ? '' : 's'}` : `${iv.stages || 0} stage${iv.stages === 1 ? '' : 's'} (none reported)`}</div>
      <div class="xs muted mt4">${iv.reported_questions || 0} reported · ${iv.role_derived_questions || 0} role-derived · ${iv.candidate_specific_questions || 0} for your profile</div>
      ${iv.focus_areas?.length ? `<div class="row wrap gap6 mt8">${iv.focus_areas.map(a => pill(a, 'plain')).join('')}</div>` : ''}</div>
    <div class="box"><div class="caps mb8">COMPANY</div><div class="small pretty">${esc(co.summary?.text || 'No summary from sources yet.')}</div>
      <div class="xs muted mt4">${(co.official_values || []).length ? `${co.official_values.length} official value${co.official_values.length === 1 ? '' : 's'}` : 'No official values found'}${(co.culture_themes || []).length ? ` · ${co.culture_themes.length} employee-reported theme${co.culture_themes.length === 1 ? '' : 's'}` : ''}</div></div>
  </div>
  <div class="row wrap gap6 mt12 xs muted">${Object.entries(INS_KIND).map(([k, [t, tone]]) => `${pill(t, tone)} ${esc((x.kinds || {})[k] || '')}`).join(' · ')}</div>`;
}

function insCompensation(x) {
  const c = x.compensation;
  if (!c) return '<div class="small muted">Not researched yet.</div>';
  const prep = x.compensation_prep || {};
  const bySource = {};
  for (const r of c.observations || []) {
    if (['EMPLOYER', 'JOB_DESCRIPTION', 'RECRUITER'].includes(r.source_type)) continue;
    (bySource[r.source_name || r.source_type] = bySource[r.source_name || r.source_type] || []).push(r);
  }
  const line = (name, value, meta = '') => `<div class="ins-line"><span class="small">${esc(name)}</span><span class="grow"></span><span class="small b mono">${esc(value)}</span>${meta ? `<span class="xs faint">${esc(meta)}</span>` : ''}</div>`;
  const sources = Object.entries(bySource).map(([name, rows]) => rows.map(r => line(insWords(name), insRange(r.original), [r.sample_size ? `${r.sample_size} observations` : '', insWords(r.basis), r.included ? '' : 'not used'].filter(Boolean).join(' · '))).join('')).join('');
  const market = c.market_range;
  const conflict = c.conflict ? `<div class="banner warn mt10" style="display:block"><b>Conflicting estimates</b> (${Math.round(c.conflict.spread * 100)}% apart, between ${esc(c.conflict.between)}). They are not averaged:
      <div class="col gap4 mt6">${c.conflict.alternatives.map(a => `<div class="xs">${esc(a.label || a.source_name || '')}: <b>${esc(insRange(a))}</b></div>`).join('')}</div></div>` : '';
  const how = `<details class="ins-how mt12"><summary class="small">How was this estimated?</summary>
      <div class="xs muted mt8 pretty">${esc(market?.method || 'No market estimate was made.')} Pay is compared per year, in the same currency (${esc(c.annualization || '')}); no currency is converted. Base salary, total cash and total compensation are never compared with one another. The employer's own figure always stands.</div>
      <table class="ins-table mt8"><thead><tr><th>Source</th><th>As stated</th><th>Per year</th><th>Basis</th><th>Match</th><th>Used</th></tr></thead><tbody>
      ${(c.observations || []).map(r => `<tr><td>${esc(insWords(r.source_type))}</td><td>${esc(insRange(r.original))}</td><td>${r.annual_low != null ? esc(`${r.currency || ''} ${insAmount(r.annual_low)}–${insAmount(r.annual_high)}`) : '—'}</td><td>${esc(insWords(r.basis))}</td><td>${esc(insWords(r.match_scope || ''))}${r.recency ? ` · ${esc(insWords(r.recency))} recency` : ''}</td><td>${r.included ? '✓' : esc(r.excluded_reason || '—')}</td></tr>`).join('') || '<tr><td colspan="6">No observations.</td></tr>'}</tbody></table>
      ${c.date_range ? `<div class="xs faint mt6">Observed ${esc(fmtDay(c.date_range.from))} – ${esc(fmtDay(c.date_range.to))}</div>` : ''}</details>`;
  return `<div class="col gap4">
      ${line('Employer posting', insRange(c.employer_posted_range))}
      ${c.recruiter_budget ? line('Recruiter budget', insRange(c.recruiter_budget), 'you entered it') : ''}
      ${sources || '<div class="xs muted">No market data (sources not configured or nothing matched).</div>'}
      <div class="ins-line strong mt8"><span class="small b">Market estimate</span>${pill('Estimate, not the employer band', 'muted')}<span class="grow"></span><span class="base b mono">${esc(market ? insRange(market) : '—')}</span></div>
      <div class="xs muted">Status: ${esc(INS_COMP_STATUS[c.status] || c.status)} · confidence ${esc(insWords(c.confidence))}${c.sample_size ? ` · ${c.sample_size} observations` : ''}</div></div>
    ${conflict}${how}
    <div class="mt16"><div class="caps mb8">TALKING ABOUT PAY</div>
      ${prep.candidate_target ? `<div class="small mb6">Your target (from your profile): <b>${esc(prep.candidate_target)}</b></div>` : ''}
      ${prep.discussion_range ? `<div class="small mb6">Discussion anchor: <b>${esc(insRange(prep.discussion_range))}</b> <span class="xs muted">(${esc(prep.discussion_range.label)})</span></div>` : ''}
      <div class="col gap6">${(prep.questions || []).map(q => insItem(q.question, q.kind)).join('')}</div>
      <div class="xs faint mt6">${esc(prep.note || '')}</div></div>`;
}

function insCompany(x) {
  const c = x.company;
  if (!c) return '<div class="small muted">No company facts found yet. Add the company website under Sources and refresh.</div>';
  const values = c.official_values || [], themes = c.culture_themes || [];
  const prep = x.interview?.value_prep || [];
  const s = c.sentiment;
  return `<div class="col gap14">
    ${c.summary ? `<div>${insItem(c.summary.text, c.summary.kind, ' ' + insSourcesNote(c.summary.source_ids))}</div>` : ''}
    ${c.website ? `<div class="xs muted">Website: ${insLink(c.website, c.website)}</div>` : ''}
    ${(c.mission || []).length ? `<div><div class="caps mb8">MISSION</div>${c.mission.map(m => insItem(m.text, m.kind)).join('')}</div>` : ''}
    <div class="ins-two"><div><div class="caps mb8">OFFICIAL VALUES</div>${values.length ? values.map(v => {
        const p = prep.find(q => (q.value_source_ids || []).some(id => (v.source_ids || []).includes(id)));
        return `<div class="ins-item col"><div class="row gap8"><span class="small b grow">${esc(v.value)}</span>${insKind(v.kind)}</div>
          ${v.sources?.[0]?.url ? `<span class="xs">${insLink(v.sources[0].url, `${v.sources[0].title || 'Official page'} ↗`)}</span>` : ''}
          ${p ? `<div class="box soft mt6"><div class="row gap6">${insKind('GENERATED')}<span class="xs">Possible question</span></div><div class="small mt4">${esc(p.question)}</div>${p.evidence?.filter(Boolean).length ? `<div class="xs muted mt4">Your evidence: ${p.evidence.filter(Boolean).map(esc).join(' · ')}</div>` : ''}</div>` : ''}</div>`; }).join('')
        : '<div class="small muted">No official values found on the company\'s own pages.</div>'}</div>
      <div><div class="caps mb8">EMPLOYEE-REPORTED THEMES</div>${themes.length ? themes.map(t => insItem(t.theme, t.kind, ` <span class="xs faint">· ${esc(insWords(t.sentiment))}</span>`)).join('')
        : '<div class="small muted">No employee reviews matched.</div>'}
        ${s ? `<div class="box mt8"><div class="xs b mb4">Employee ratings (${esc(s.label)})</div><div class="xs muted">${['overall_rating', 'work_life_balance', 'culture', 'management', 'career_opportunities', 'compensation_rating'].filter(k => s[k] != null).map(k => `${insWords(k)} ${s[k]}`).join(' · ')}${s.sample_size ? ` · ${s.sample_size} ratings` : ''}</div></div>` : ''}</div></div>
    ${(c.profile_facts || []).length ? `<div><div class="caps mb8">FACTS</div>${c.profile_facts.map(f => insItem(`${insWords(f.field)}: ${f.value}`, f.kind)).join('')}</div>` : ''}
    ${(c.benefits || []).length ? `<div><div class="caps mb8">BENEFITS</div>${c.benefits.map(b => insItem(b.text, b.kind)).join('')}</div>` : ''}
    ${c.remote_policy ? `<div><div class="caps mb8">WORK ARRANGEMENT</div>${insItem(c.remote_policy.text, c.remote_policy.kind)}</div>` : ''}
    ${(x.risks || []).length || (x.unknowns || []).length ? `<div><div class="caps mb8">RISKS AND UNKNOWNS</div>${(x.risks || []).map(r => insItem(r.text, r.kind)).join('')}${(x.unknowns || []).map(u => insItem(u.text, u.kind)).join('')}</div>` : ''}
  </div>`;
}

function insQuestion(q, jobId) {
  const prepared = D.insPrep?.[`${jobId}:${q.id}`];
  const meta = [insWords(q.category), insWords(q.stage), q.difficulty ? insWords(q.difficulty) : ''].filter(x => x && x !== 'unknown').join(' · ');
  return `<div class="ins-q"><div class="row top-align gap8"><div class="grow"><div class="small b pretty">${esc(q.question)}</div>
      <div class="xs muted mt4">${esc(meta)}${q.requirements?.length ? ` · tests: ${esc(q.requirements.join(', '))}` : ''}</div>
      ${q.why_likely ? `<div class="xs mt4"><span class="faint">Why likely:</span> ${esc(q.why_likely)}</div>` : ''}
      ${q.gaps?.length ? `<div class="xs mt4" style="color:var(--warn-deep)">Probes a gap: ${esc(q.gaps.join(', '))}</div>` : ''}
      ${q.evidence_ids?.length ? `<div class="xs muted mt4">Evidence available: ${q.evidence_ids.length} stor${q.evidence_ids.length === 1 ? 'y' : 'ies'}</div>` : ''}</div>
      <div class="col gap6 none">${insKind('GENERATED')}${btn(label(`insprep${jobId}:${q.id}`, 'Prepare answer', 'Preparing…'), 'ins-prepare', { id: jobId, q: q.id }, 'sm ghost', isBusy(`insprep${jobId}:${q.id}`))}</div></div>
    ${prepared ? `<div class="box soft mt8"><div class="xs b mb4">Answer outline (STAR) — from your verified evidence, not saved</div><ol class="xs">${prepared.outline.map(s => `<li>${esc(s)}</li>`).join('')}</ol>
      ${prepared.evidence.length ? `<div class="xs mt6"><b>Evidence to draw on</b>${prepared.evidence.map(e => `<div class="muted">· ${esc(e.claim)}${e.role ? ` (${esc(e.role)})` : ''}</div>`).join('')}</div>` : '<div class="xs muted mt6">No verified evidence matches this question yet.</div>'}
      ${prepared.gaps_not_to_claim.length ? `<div class="xs mt6" style="color:var(--warn-deep)">Do not claim: ${esc(prepared.gaps_not_to_claim.join(', '))}</div>` : ''}
      ${prepared.preparation ? `<div class="xs muted mt6">${esc(prepared.preparation)}</div>` : ''}</div>` : ''}</div>`;
}

function insInterview(x, jobId) {
  const iv = x.interview;
  if (!iv) return '<div class="small muted">Not researched yet.</div>';
  const p = iv.process || {};
  const reported = iv.reported_questions || [];
  return `<div class="col gap16">
    <div><div class="caps mb8">PROCESS</div>${(p.stages || []).length ? `<div class="ins-stages">${p.stages.map(s => `<div class="ins-stage"><span class="mono xs faint">${s.order}</span><span class="small b">${esc(s.name)}</span>${s.origin === 'REPORTED' ? pill(s.source_type === 'RECRUITER' ? 'Recruiter said' : 'Reported', 'good') : pill('Inferred', 'brand')}${s.rationale ? `<span class="xs muted">${esc(s.rationale)}</span>` : ''}</div>`).join('')}</div>`
      : '<div class="small muted">No interview process reported.</div>'}
      ${(p.durations || []).length || (p.difficulty || []).length ? `<div class="xs muted mt6">Reported: ${[...(p.durations || []).map(d => d.text), ...(p.difficulty || []).map(d => `difficulty ${d.text}`)].map(esc).join(' · ')}</div>` : ''}</div>
    <div><div class="row gap8 mb8"><span class="caps">REPORTED BY CANDIDATES</span><span class="xs faint">questions sources say were asked</span></div>
      ${reported.length ? reported.map(q => `<div class="ins-q"><div class="row top-align gap8"><div class="grow"><div class="small b pretty">“${esc(q.question)}”</div>
        <div class="xs muted mt4">${esc([insWords(q.source_type), q.reported_role, q.reported_at ? fmtDay(q.reported_at) : ''].filter(Boolean).join(' · '))}</div></div>${insKind(q.kind)}</div></div>`).join('')
        : '<div class="small muted">No questions reported by candidates were found.</div>'}</div>
    <div><div class="row gap8 mb8"><span class="caps">LIKELY FOR THIS ROLE</span><span class="xs faint">generated from the posting's requirements</span></div>
      ${(iv.role_derived_questions || []).map(q => insQuestion(q, jobId)).join('') || '<div class="small muted">None yet.</div>'}</div>
    <div><div class="row gap8 mb8"><span class="caps">BASED ON YOUR PROFILE</span><span class="xs faint">generated from your fit analysis and evidence</span></div>
      ${(iv.candidate_specific_questions || []).map(q => insQuestion(q, jobId)).join('') || '<div class="small muted">None yet.</div>'}</div>
    ${(iv.focus_areas || []).length ? `<div><div class="caps mb8">LIKELY FOCUS</div>${iv.focus_areas.map(f => insItem(`${f.area} — ${f.why}`, f.kind)).join('')}</div>` : ''}
  </div>`;
}

function insFormQuestion(q, jobId) {
  const [word, tone] = q.status === 'DRAFTED' && q.basis === 'CLOSEST' ? ['Adapted draft', 'warn']
    : INS_FORM_STATUS[q.status] || INS_FORM_STATUS.NEEDS_INPUT;
  const key = `${jobId}:${q.id}`;
  const savable = q.status === 'DRAFTED' || q.status === 'NEEDS_INPUT', editing = savable && !!insState().editing?.[key];
  const actions = editing ? '' : [
    q.answer ? btn('Copy', 'ins-form-copy', { id: jobId, q: q.id }, 'sm ghost') : '',
    savable ? btn(q.status === 'DRAFTED' ? 'Review &amp; save' : 'Answer here', 'ins-form-edit', { id: jobId, q: q.id }, 'sm ghost') : '',
    q.status === 'NEEDS_INPUT' && q.origin === 'FORM' && q.category !== 'PERSONAL' ? btn(label('insformdraft' + key, 'Draft with AI', 'Drafting…'), 'ins-form-redraft', { id: jobId, q: q.id }, 'sm ghost', isBusy('insformdraft' + key)) : '',
    savable ? btn('Open in library', 'ins-form-save', { id: jobId, q: q.id }, 'sm ghost') : '',
    q.origin === 'FORM' ? btn(label('insformrm' + key, 'Remove', 'Removing…'), 'ins-form-remove', { id: jobId, q: q.id }, 'sm ghost danger', isBusy('insformrm' + key)) : '',
  ].filter(Boolean).join('');
  return `<div class="ins-q"><div class="row top-align gap8"><div class="grow"><div class="small b pretty">${esc(q.question)}</div>
      ${q.rationale ? `<div class="xs mt4"><span class="faint">Why likely:</span> ${esc(q.rationale)}</div>` : ''}
      ${q.basis === 'CLOSEST' && q.status === 'DRAFTED' ? `<div class="xs b mt6" style="color:var(--warn-deep)">${esc(q.label)}</div>` : ''}
      ${editing ? insAnswerEditor(q, jobId) : q.answer ? `<div class="box soft mt8 small pretty" style="white-space:pre-wrap">${esc(q.answer)}</div>` : ''}
      ${q.evidence?.filter(Boolean).length ? `<div class="xs muted mt6">From your evidence: ${q.evidence.filter(Boolean).map(esc).join(' · ')}</div>` : ''}
      ${q.resume_facts?.filter(Boolean).length ? `<div class="xs muted mt4">From your resume: ${q.resume_facts.filter(Boolean).map(esc).join(' · ')}</div>` : ''}
      ${q.requirements?.length ? `<div class="xs muted mt4">Relates to: ${esc(q.requirements.join(', '))}</div>` : ''}
      ${q.reason ? `<div class="xs mt6" style="color:var(--warn-deep)">${esc(q.reason)}</div>` : ''}</div>
      <div class="col gap6 none">${pill(word, tone)}${q.kind ? insKind(q.kind) : ''}</div></div>
    ${actions ? `<div class="row wrap gap6 mt8">${actions}</div>` : ''}</div>`;
}

// Edit the draft (or write the answer) in place and save it to the Answer library as verified, without leaving the
// job. "I reviewed this" starts unticked: a draft is saved as the person's answer only once they say they read it.
const insAnswerForm = (jobId, qid) => `insans${jobId}_${qid}`;
function insAnswerEditor(q, jobId) {
  const form = insAnswerForm(jobId, q.id), key = `${jobId}:${q.id}`, personal = q.category === 'PERSONAL';
  return `<div class="box soft mt8 col gap8">
    ${textareaF(form, 'answer', q.answer || '', `style="min-height:110px" aria-label="Your answer to: ${attr(q.question)}" placeholder="Write your answer"`)}
    ${checkF(form, 'reviewed', false, q.status === 'DRAFTED' ? 'I reviewed this answer and it is accurate' : 'This is my answer and it is accurate')}
    ${checkF(form, 'sensitive', personal, 'Sensitive (confirm it on each form; it is never shown to a model)', personal ? 'disabled' : '')}
    <div class="row wrap gap6">${btn(label('insans' + key, 'Save to library as verified', 'Saving…'), 'ins-form-verify', { id: jobId, q: q.id }, 'primary sm', isBusy('insans' + key))}${btn('Cancel', 'ins-form-cancel', { id: jobId, q: q.id }, 'sm ghost')}</div></div>`;
}

function insForm(x, jobId) {
  const f = x.application_form || { from_form: [], likely: [] };
  const form = 'insform' + jobId;
  const full = (f.from_form || []).length >= (f.max_questions || 20);
  return `<div class="col gap16">
    <div class="box"><div class="base b mb4">Questions on the form</div><div class="xs muted mb8">Paste the questions the application asks, one per line. Each is drafted in the background from your resume for this job, your profile, your saved answers and the verified evidence retrieved for that question — when nothing answers it exactly, from your closest projects, labelled so you can check it; questions only you can answer (work authorization, pay, notice period, personal details) are never drafted.</div>
      ${textareaF(form, 'questions', '', `placeholder="Why do you want to join this company?&#10;Describe a project where you led a team." style="min-height:80px" aria-label="Questions from the application form"${full ? ' disabled' : ''}`)}
      <div class="row gap8 mt8">${btn(label('insformadd' + jobId, 'Add and draft', 'Adding…'), 'ins-form-add', { id: jobId }, 'primary sm', isBusy('insformadd' + jobId) || full)}${full ? '<span class="xs muted">Remove a question to add another.</span>' : ''}</div></div>
    <div><div class="row gap8 mb8"><span class="caps">FROM THE FORM</span><span class="xs faint">questions you pasted</span></div>
      ${(f.from_form || []).map(q => insFormQuestion(q, jobId)).join('') || '<div class="small muted">None yet. Paste the questions your application form asks above.</div>'}</div>
    <div><div class="row gap8 mb8"><span class="caps">LIKELY ON THE FORM</span><span class="xs faint">predicted from the posting, drafted from your data</span></div>
      ${(f.likely || []).map(q => insFormQuestion(q, jobId)).join('') || `<div class="small muted">${x.status === 'NONE' ? 'Written when the job is researched.' : 'None yet: refresh to predict them.'}</div>`}</div>
    <div class="xs faint">${esc(f.note || '')}</div>
  </div>`;
}

function insAsk(x) {
  const qs = x.questions_to_ask || [];
  if (!qs.length) return '<div class="small muted">No questions yet: they are written after research.</div>';
  return `<div class="col gap14">${INS_ASK_STAGES.map(([id, title]) => {
    const mine = qs.filter(q => q.stage === id);
    return mine.length ? `<div><div class="caps mb8">${esc(title.toUpperCase())}</div>${mine.map(q => insItem(q.question, q.kind, q.context ? ` <span class="xs faint">· builds on ${esc(q.context)}</span>` : '')).join('')}</div>` : '';
  }).join('')}</div>`;
}

function insSources(x, jobId) {
  const r = x.recruiter_input || {}, id = x.identity || {};
  const f = 'insin' + jobId;
  const prov = D.insProv?.[jobId];
  return `<div class="col gap16">
    <table class="ins-table"><thead><tr><th></th><th>Source</th><th>Status</th><th>Found</th><th>Detail</th></tr></thead><tbody>
      ${(x.sources || []).map(s => `<tr><td class="mono" aria-hidden="true">${esc(s.symbol)}</td><td class="small">${esc(s.label)}</td><td>${pill(INS_SOURCE_WORD[s.status] || insWords(s.status), INS_SOURCE_TONE[s.status] || 'muted')}${s.cached ? ` ${pill('reused', 'plain')}` : ''}</td><td class="xs">${s.observations || 0}${s.rejected ? ` <span class="faint">(${s.rejected} other compan${s.rejected === 1 ? 'y' : 'ies'}/roles set aside)</span>` : ''}</td><td class="xs muted">${esc(s.detail || '')}</td></tr>`).join('')}</tbody></table>
    <div class="xs faint">${Object.entries(x.legend || {}).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(' · ')}</div>
    <div>${prov ? `<div class="caps mb8">EVERY OBSERVATION</div><table class="ins-table"><thead><tr><th>Category</th><th>Source</th><th>Match</th><th>Confidence</th><th>Observed</th><th>Fetched</th></tr></thead><tbody>
        ${prov.observations.map(o => `<tr><td class="xs">${esc(insWords(o.category))}</td><td class="xs">${o.source_url ? insLink(o.source_url, o.title || insWords(o.source_type)) : esc(o.title || insWords(o.source_type))}${o.superseded ? ' <span class="faint">(earlier result, still cited)</span>' : ''}${o.actor_id ? ` <span class="faint">· ${esc(o.actor_id)}</span>` : ''}</td><td class="xs">${esc(insWords(o.match_status))}${o.match_scope ? ` · ${esc(insWords(o.match_scope))}` : ''}${o.match_reason ? `<div class="faint">${esc(o.match_reason)}</div>` : ''}</td><td class="xs mono">${o.confidence != null ? Math.round(o.confidence * 100) + '%' : '—'}</td><td class="xs">${esc(fmtDay(o.observed_at) || '—')}</td><td class="xs">${esc(fmtDay(o.fetched_at))}</td></tr>`).join('') || '<tr><td colspan="6">None.</td></tr>'}</tbody></table>
        <div class="xs faint mt6">Showing ${prov.observations.length} of ${prov.total}.</div>${prov.next_offset != null ? btn(label('insprov' + jobId, 'Show more', 'Loading…'), 'ins-provenance', { id: jobId, more: 1 }, 'sm ghost mt6', isBusy('insprov' + jobId)) : ''}`
      : btn(label('insprov' + jobId, 'Show every observation', 'Loading…'), 'ins-provenance', { id: jobId }, 'sm ghost', isBusy('insprov' + jobId))}</div>
    <div class="box"><div class="base b mb4">Your inputs</div><div class="xs muted mb10">Used from the next refresh. Research never overwrites them, and a recruiter's budget outranks any estimate.</div>
      <label class="xs b">Company website <span class="faint">(${esc(id.website_source === 'USER' ? 'yours' : id.website_source === 'POSTING' ? 'from the posting' : 'not known — never guessed')})</span></label>
      ${inputF(f, 'company_website', id.website_source === 'USER' ? id.website : '', `placeholder="${attr(id.website || 'https://company.com')}" maxlength="500"`)}
      <div class="ins-form mt10">
        <label class="xs b">Recruiter${inputF(f, 'recruiter_name', r.recruiter_name, 'maxlength="200"')}</label>
        <label class="xs b">Budget low${inputF(f, 'budget_low', r.budget_low, 'type="number" min="0"')}</label>
        <label class="xs b">Budget high${inputF(f, 'budget_high', r.budget_high, 'type="number" min="0"')}</label>
        <label class="xs b">Currency${inputF(f, 'currency', r.currency, 'maxlength="3" placeholder="SGD"')}</label>
        <label class="xs b">Per<select class="select" id="${f}-period" data-form="${f}" data-field="period">${['', 'year', 'month', 'hour'].map(p => `<option value="${p}" ${fv(f, 'period', r.period || '') === p ? 'selected' : ''}>${p || '—'}</option>`).join('')}</select></label>
        <label class="xs b">Basis<select class="select" id="${f}-basis" data-form="${f}" data-field="basis">${['', 'BASE', 'TOTAL_CASH', 'TOTAL_COMPENSATION'].map(p => `<option value="${p}" ${fv(f, 'basis', r.basis || '') === p ? 'selected' : ''}>${p ? insWords(p) : '—'}</option>`).join('')}</select></label></div>
      <label class="xs b mt8">Interview process (one stage per line)${textareaF(f, 'interview_process', r.interview_process, 'maxlength="2000" style="min-height:70px"')}</label>
      <label class="xs b mt8">Questions the recruiter mentioned (one per line)${textareaF(f, 'recruiter_questions', (r.recruiter_questions || []).join('\n'), 'style="min-height:60px"')}</label>
      <label class="xs b mt8">Comments${textareaF(f, 'comments', r.comments, 'maxlength="2000" style="min-height:60px"')}</label>
      <div class="row gap8 mt10">${btn(label('insinputs' + jobId, 'Save inputs', 'Saving…'), 'ins-save-inputs', { id: jobId }, 'primary sm', isBusy('insinputs' + jobId))}</div></div>
  </div>`;
}

/* ───────────── screens ───────────── */
function renderJobInsights(jobId) {
  const x = D.insights?.[jobId];
  if (!x) return `<div style="padding:20px 22px">${loading()}</div>`;
  const tab = insState().tab;
  const body = { overview: insOverview, compensation: insCompensation, company: insCompany, interview: insInterview, form: insForm, ask: insAsk, sources: insSources }[tab] || insOverview;
  return `<div class="ins" style="padding:16px 22px 22px">${insHeader(x, jobId)}${insTabs(tab)}<div class="mt14">${body(x, jobId)}</div></div>`;
}
// The Approvals card: the overview, and a way into the full package.
// Approvals: one control that follows the research's state — start it, watch it run, then read the summary and
// open the full report. An empty card stays one line so it never pushes the decision down.
function renderInsightsCard(jobId) {
  const x = D.insights?.[jobId];
  if (!x) return '';
  const never = x.status === 'NONE' && !x.researching;
  const head = (state, action) => `<div class="row between wrap gap8"><div class="row gap8 wrap"><span class="base b">Company research</span>${state}</div>${action}</div>`;
  if (never) return `<div class="appr-sec ins-card">${head('<span class="xs muted">Company, team, pay and red flags for this job · not researched yet</span>', btn(label('insrefresh' + jobId, 'Research this company', 'Queueing…'), 'ins-refresh', { id: jobId }, 'sm', isBusy('insrefresh' + jobId)))}</div>`;
  if (x.researching) return `<div class="appr-sec ins-card">${head(`${pill(x.task?.status === 'PROCESSING' ? 'Researching…' : 'Queued', 'brand')} <span class="xs muted">Runs in the background; this updates on its own.</span>`, '')}</div>`;
  return `<div class="appr-sec ins-card">${head(pill(insWords(x.status), x.status === 'COMPLETED' ? 'good' : x.status === 'FAILED' ? 'bad' : 'warn'), `<span class="row gap6">${btn(label('insrefresh' + jobId, 'Refresh', 'Queueing…'), 'ins-refresh', { id: jobId }, 'sm ghost', isBusy('insrefresh' + jobId))}${btn('Full report', 'ins-open', { id: jobId }, 'sm')}</span>`)}
    ${x.refresh_failed ? `<div class="xs mt6" style="color:var(--bad)">The last refresh failed${x.last_successful_at ? `; showing ${esc(insAgo(x.last_successful_at))}'s research` : ''}.</div>` : ''}<div class="mt10">${insOverview(x)}</div></div>`;
}
// The Tracker: what to prepare next for the application's stage.
function renderStagePrep(app) {
  const x = D.appInsights?.[app.id];
  if (!x) return btn(label('insstage' + app.id, 'Interview prep for this stage', 'Loading…'), 'ins-stage-load', { app: app.id }, 'block', isBusy('insstage' + app.id));
  const list = (title, items) => items?.length ? `<div class="mt8"><div class="xs b mb4">${esc(title)}</div>${items.slice(0, 4).map(q => `<div class="xs pretty mb4">· ${esc(q.question)}</div>`).join('')}</div>` : '';
  return `<div class="box" style="padding:13px"><div class="row gap6 mb8">${insKind('GENERATED')}<span class="xs b">Prep for this stage</span></div>
    ${x.focus.length ? `<div class="row wrap gap6">${x.focus.map(f => `<span title="${attr(f.covers)}">${pill(f.label, 'brand')}</span>`).join('')}</div>` : '<div class="xs muted">No stage-specific prep for this stage.</div>'}
    ${list('Questions to ask', x.questions_to_ask)}${list('Likely questions', x.likely_questions)}
    ${x.compensation_prep?.discussion_range ? `<div class="xs mt8">Pay anchor: <b>${esc(insRange(x.compensation_prep.discussion_range))}</b> <span class="muted">(${esc(x.compensation_prep.discussion_range.label)})</span></div>` : ''}
    <div class="mt8">${btn('Open insights', 'ins-open', { id: app.job_id }, 'sm ghost')}</div></div>`;
}

/* ───────────── actions ───────────── */
function insInputsBody(jobId) {
  const f = 'insin' + jobId, cur = D.insights?.[jobId] || {}, r = cur.recruiter_input || {};
  const num = v => (v === '' || v == null) ? null : Number(v);
  const lines = v => String(v || '').split('\n').map(s => s.trim()).filter(Boolean);
  const site = fv(f, 'company_website', null);
  return {
    ...(site !== null ? { company_website: site } : {}),
    recruiter: {
      recruiter_name: fv(f, 'recruiter_name', r.recruiter_name || ''), budget_low: num(fv(f, 'budget_low', r.budget_low)),
      budget_high: num(fv(f, 'budget_high', r.budget_high)), currency: fv(f, 'currency', r.currency || '') || null,
      period: fv(f, 'period', r.period || '') || null, basis: fv(f, 'basis', r.basis || '') || null,
      interview_process: fv(f, 'interview_process', r.interview_process || ''),
      recruiter_questions: lines(fv(f, 'recruiter_questions', (r.recruiter_questions || []).join('\n'))),
      comments: fv(f, 'comments', r.comments || ''),
    },
  };
}
const INSIGHTS_ACTIONS = {
  'ins-tab': el => { insState().tab = el.dataset.id; render(); },
  'ins-open': async el => {
    const id = Number(el.dataset.id);
    insState().tab = 'overview';
    await openJob(id);
    S.jobTab = 'insights'; render();
    insEnsure(id);
  },
  'ins-refresh': el => {
    const id = Number(el.dataset.id);
    return busy('insrefresh' + id, async () => {
      const out = await api(`/api/jobs/${id}/insights/refresh`, { method: 'POST' });
      D.insights = { ...(D.insights || {}), [id]: out };
      D.insPrep = Object.fromEntries(Object.entries(D.insPrep || {}).filter(([k]) => !k.startsWith(`${id}:`)));
      scheduleInsPoll(id);
      return out;
    }, 'Researching in the background — this updates on its own');
  },
  'ins-save-inputs': el => {
    const id = Number(el.dataset.id);
    return busy('insinputs' + id, async () => {
      const out = await api(`/api/jobs/${id}/insights/inputs`, { method: 'PUT', body: insInputsBody(id) });
      D.insights = { ...(D.insights || {}), [id]: out };
      delete S.forms['insin' + id];
      return out;
    }, 'Saved. Refresh to research with them.');
  },
  'ins-prepare': el => {
    const id = Number(el.dataset.id), q = el.dataset.q;
    return busy(`insprep${id}:${q}`, async () => {
      const out = await api(`/api/jobs/${id}/insights/questions/${encodeURIComponent(q)}/prepare`, { method: 'POST' });
      D.insPrep = { ...(D.insPrep || {}), [`${id}:${q}`]: out };
      return out;
    });
  },
  'ins-form-add': el => {
    const id = Number(el.dataset.id), form = 'insform' + id;
    const questions = String(fv(form, 'questions', '') || '').split('\n').map(s => s.trim()).filter(s => s.length >= 8);
    if (!questions.length) { toast('Paste at least one question (8 characters or more).', true); return; }
    return busy('insformadd' + id, async () => {
      const out = await api(`/api/jobs/${id}/insights/application-questions`, { method: 'POST', body: { questions } });
      D.insights = { ...(D.insights || {}), [id]: out };
      delete S.forms[form];
      scheduleInsPoll(id);
      return out;
    }, 'Added — drafting in the background');
  },
  'ins-form-remove': el => {
    const id = Number(el.dataset.id), q = el.dataset.q;
    return busy(`insformrm${id}:${q}`, async () => {
      D.insights = { ...(D.insights || {}), [id]: await api(`/api/jobs/${id}/insights/application-questions/${encodeURIComponent(q)}`, { method: 'DELETE' }) };
    });
  },
  // Drafts the question again in the background, adapted from the closest verified experience when nothing answers
  // it exactly. The draft is never saved until the person reviews it.
  'ins-form-redraft': el => {
    const id = Number(el.dataset.id), q = el.dataset.q;
    return busy(`insformdraft${id}:${q}`, async () => {
      D.insights = { ...(D.insights || {}), [id]: await api(`/api/jobs/${id}/insights/application-questions/${encodeURIComponent(q)}/draft`, { method: 'POST' }) };
      scheduleInsPoll(id);
    }, 'Drafting in the background');
  },
  'ins-form-copy': el => {
    const f = D.insights?.[Number(el.dataset.id)]?.application_form || {};
    const q = [...(f.from_form || []), ...(f.likely || [])].find(item => item.id === el.dataset.q);
    if (q?.answer) copyText(q.answer);
  },
  'ins-form-edit': el => {
    const st = insState();
    st.editing = { ...(st.editing || {}), [`${el.dataset.id}:${el.dataset.q}`]: true };
    render();
    document.getElementById(`${insAnswerForm(el.dataset.id, el.dataset.q)}-answer`)?.focus();
  },
  'ins-form-cancel': el => {
    delete insState().editing?.[`${el.dataset.id}:${el.dataset.q}`];
    delete S.forms[insAnswerForm(el.dataset.id, el.dataset.q)];
    render();
  },
  'ins-form-verify': el => {
    const id = Number(el.dataset.id), q = el.dataset.q, form = insAnswerForm(id, q), key = `${id}:${q}`;
    const f = D.insights?.[id]?.application_form || {};
    const item = [...(f.from_form || []), ...(f.likely || [])].find(x => x.id === q);
    if (!item) return;
    const answer = String(fv(form, 'answer', item.answer || '')).trim();
    if (!answer) { toast('Write the answer first', true); return; }
    if (!fv(form, 'reviewed', false)) { toast('Tick “I reviewed this answer” to save it as verified', true); return; }
    const sensitive = item.category === 'PERSONAL' || !!fv(form, 'sensitive', false);
    return busy('insans' + key, async () => {
      const out = await api(`/api/jobs/${id}/insights/application-questions/${encodeURIComponent(q)}/save`, { method: 'POST', body: { answer, sensitive, reviewed: true } });
      D.insights = { ...(D.insights || {}), [id]: out };
      delete insState().editing?.[key];
      delete S.forms[form];
      D.knowledge = null; // the Answer library reloads with it next time
      return out;
    }, 'Saved to your Answer library as verified');
  },
  // Opens the Answer library editor with the question (and the draft) filled in. "Human verified" starts unticked:
  // the person reads the draft and confirms it there; nothing is saved from here.
  'ins-form-save': el => {
    const f = D.insights?.[Number(el.dataset.id)]?.application_form || {};
    const q = [...(f.from_form || []), ...(f.likely || [])].find(item => item.id === el.dataset.q);
    if (!q) return;
    S.jobOpen = null;
    delete S.forms.knnew;
    setF('knnew', 'question', q.question);
    setF('knnew', 'answer', q.answer || '');
    setF('knnew', 'verified', false);
    knowledgeState().editors.answers = { id: null, form: 'knnew' };
    go('knowledge', { knowledgeSection: 'answers' });
    document.getElementById('knnew-answer')?.focus();
  },
  'ins-provenance': el => {
    const id = Number(el.dataset.id), prev = el.dataset.more ? D.insProv?.[id] : null;
    return busy('insprov' + id, async () => {
      const page = await api(`/api/jobs/${id}/insights/sources?` + qs({ offset: prev?.next_offset ?? 0 }));
      D.insProv = { ...(D.insProv || {}), [id]: prev ? { ...page, observations: [...prev.observations, ...page.observations] } : page };
    });
  },
  'ins-stage-load': el => {
    const app = Number(el.dataset.app);
    return busy('insstage' + app, async () => {
      D.appInsights = { ...(D.appInsights || {}), [app]: await api(`/api/applications/${app}/insights`) };
    });
  },
};
