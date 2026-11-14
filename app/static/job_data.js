/* Job detail → Structured JD: what the posting requires, where each requirement came from, and corrections.
   Loaded after settings.js, before app.js. The raw description is never edited here; only the parsed values are. */
'use strict';
const JD_LEVELS = { REQUIRED: ['Required', 'bad'], PREFERRED: ['Preferred', 'brand'], INFORMATIONAL: ['Context', 'muted'], UNKNOWN: ['Unclear', 'warn'] };
const JD_STATUS = { RAW: ['Not parsed yet', 'muted'], NORMALIZED: ['Waiting to be parsed', 'muted'], PARSED: ['Parsed', 'good'], NEEDS_REVIEW: ['Parsing failed — check it', 'warn'], MANUALLY_CORRECTED: ['Corrected by you', 'brand'] };
// Editable fields of the structured description: [path, label, kind]. kind: text | textarea | list | number | tri
const JD_FIELDS = [
  ['job_identity.title', 'Title'], ['job_identity.company', 'Company'], ['job_identity.location', 'Location'],
  ['job_identity.employment_type', 'Employment type'], ['job_identity.remote_type', 'Remote type'], ['job_identity.seniority', 'Seniority'],
  ['role_summary', 'Role summary', 'textarea'],
  ['responsibilities', 'Responsibilities', 'lines'], ['required_skills', 'Required skills', 'lines'],
  ['preferred_skills', 'Preferred skills', 'lines'], ['technical_domains', 'Technical domains', 'list'],
  ['leadership_expectations', 'Leadership expectations', 'lines'], ['education_requirements', 'Education', 'lines'],
  ['experience_requirements.minimum_years', 'Minimum years', 'number'], ['experience_requirements.maximum_years', 'Maximum years', 'number'],
  ['experience_requirements.leadership_required', 'Leadership required', 'tri'], ['experience_requirements.management_required', 'People management required', 'tri'],
  ['location_requirements.country', 'Country'], ['location_requirements.city', 'City'],
  ['location_requirements.relocation_supported', 'Relocation supported', 'tri'],
  ['location_requirements.visa_sponsorship', 'Visa sponsorship', 'tri'],
  ['location_requirements.work_authorization_required', 'Work authorization required', 'tri'],
  ['compensation.min', 'Salary from', 'number'], ['compensation.max', 'Salary to', 'number'],
  ['compensation.currency', 'Currency'], ['compensation.period', 'Period'],
  ['hard_requirements', 'Hard requirements', 'lines'], ['nice_to_have', 'Nice to have', 'lines'],
];
const JD = { data: null, jobId: null, loading: false, draft: null, decisions: {}, history: null, view: 'summary', error: '', unmatched: [] };

const jdGet = (object, path) => path.split('.').reduce((node, key) => (node == null ? node : node[key]), object);
function jdSet(object, path, value) {
  const keys = path.split('.'), last = keys.pop();
  keys.reduce((node, key) => (node[key] = node[key] || {}), object)[last] = value;
}
function jdReset() { JD.draft = null; JD.decisions = {}; JD.error = ''; JD.unmatched = []; }

async function jobDataLoad(jobId, force = false) {
  if (JD.loading || (!force && JD.jobId === jobId && JD.data)) return;
  JD.loading = true; JD.jobId = jobId;
  try { JD.data = await api(`/api/jobs/${jobId}/structured-description`); JD.history = null; }
  catch (err) { JD.error = err.message; JD.data = null; }
  finally { JD.loading = false; render(); }
}
async function jdRun(action, success) {
  if (isBusy('jd')) return;
  S.busy.jd = true; JD.error = ''; render();
  try { await action(); if (success) toast(success); }
  catch (err) { JD.error = err.message; }
  finally { delete S.busy.jd; render(); }
}

/* ───────────── rendering ───────────── */
function jdPill(level) { const [label, tone] = JD_LEVELS[level] || JD_LEVELS.UNKNOWN; return pill(label, tone); }
function jdValue(value) {
  if (value == null || value === '') return '<span class="muted">not stated</span>';
  if (Array.isArray(value)) return value.length ? value.map(v => esc(String(v))).join(' · ') : '<span class="muted">not stated</span>';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return esc(String(value));
}
function jdRequirementRow(item) {
  return `<div class="jd-req" data-jd-req="${attr(item.id)}"><div class="row gap8 top-align">${jdPill(item.level)}<span class="grow small">${esc(item.value)}</span><span class="xs faint mono">${Math.round((item.confidence || 0) * 100)}%</span></div>${item.source_text ? `<details class="jd-source"><summary class="xs">Why?</summary><blockquote class="xs">${esc(item.source_text)}${item.source_section ? ` <span class="faint">— ${esc(item.source_section)}</span>` : ''}</blockquote></details>` : ''}</div>`;
}
// Requirements the posting offers as alternatives ("one of Python, Java or Go") share an alternative_group: they are
// shown as one requirement that any one of them meets, in the place of the first.
function jdRequirementGroups(requirements) {
  const out = [], groups = {};
  for (const item of requirements) {
    // A correction can move some options to another level: only options at the same level are one requirement.
    const key = item.alternative_group && `${item.alternative_group}|${item.level}`;
    if (!key) { out.push(item); continue; }
    if (!groups[key]) { groups[key] = { ...item, id: item.alternative_group, alternatives: [] }; out.push(groups[key]); }
    groups[key].alternatives.push(item.value);
    groups[key].confidence = Math.min(groups[key].confidence ?? 1, item.confidence ?? 1);
  }
  return out.map(item => item.alternatives && item.alternatives.length < 2 ? { ...item, alternatives: undefined, value: item.alternatives[0] } : item);
}
function jdRequirementItem(item) {
  if (!item.alternatives) return jdRequirementRow(item);
  return `<div class="jd-req" data-jd-group="${attr(item.id)}"><div class="row gap8 top-align">${jdPill(item.level)}<span class="grow small"><span class="muted">Any one of:</span> ${item.alternatives.map(v => esc(v)).join(' · ')}</span><span class="xs faint mono">${Math.round((item.confidence || 0) * 100)}%</span></div>${item.source_text ? `<details class="jd-source"><summary class="xs">Why?</summary><blockquote class="xs">${esc(item.source_text)}${item.source_section ? ` <span class="faint">— ${esc(item.source_section)}</span>` : ''}</blockquote></details>` : ''}</div>`;
}
function jdField(path, label, kind, value) {
  const id = `jd-${path.replace(/\./g, '-')}`;
  if (kind === 'tri') {
    const current = value === true ? 'true' : value === false ? 'false' : '';
    return `<label class="field" for="${id}"><span>${esc(label)}</span><select class="select" id="${id}" data-jd-path="${path}" data-jd-kind="tri"><option value="" ${current === '' ? 'selected' : ''}>Not stated</option><option value="true" ${current === 'true' ? 'selected' : ''}>Yes</option><option value="false" ${current === 'false' ? 'selected' : ''}>No</option></select></label>`;
  }
  if (kind === 'textarea' || kind === 'lines') {
    const text = kind === 'lines' ? (value || []).join('\n') : value ?? '';
    return `<label class="field" for="${id}"><span>${esc(label)}${kind === 'lines' ? ' (one per line)' : ''}</span><textarea class="textarea" id="${id}" data-jd-path="${path}" data-jd-kind="${kind}">${esc(text)}</textarea></label>`;
  }
  const shown = kind === 'list' ? (value || []).join(', ') : value ?? '';
  return `<label class="field" for="${id}"><span>${esc(label)}</span><input class="input" id="${id}" data-jd-path="${path}" ${kind ? `data-jd-kind="${kind}"` : ''} value="${attr(shown)}"></label>`;
}
function jdSummary(data) {
  const s = data.structured_description || {};
  const rows = [
    ['Role', [s.job_identity?.title, s.job_identity?.seniority, s.job_identity?.employment_type].filter(Boolean).join(' · ')],
    ['Responsibilities', s.responsibilities], ['Required skills', s.required_skills], ['Preferred skills', s.preferred_skills],
    ['Experience', [s.experience_requirements?.minimum_years != null ? `${s.experience_requirements.minimum_years}+ years` : null,
      s.experience_requirements?.leadership_required ? 'leadership required' : null,
      s.experience_requirements?.management_required ? 'people management required' : null].filter(Boolean).join(' · ')],
    ['Leadership', s.leadership_expectations], ['Education', s.education_requirements],
    ['Location', [s.location_requirements?.city, s.location_requirements?.country, s.job_identity?.remote_type].filter(Boolean).join(' · ')],
    ['Visa / work authorization', [s.location_requirements?.visa_sponsorship == null ? 'sponsorship not stated' : s.location_requirements.visa_sponsorship ? 'sponsorship offered' : 'no sponsorship',
      s.location_requirements?.work_authorization_required ? 'authorization required' : null].filter(Boolean).join(' · ')],
    ['Compensation', s.compensation?.min || s.compensation?.max
      ? `${[s.compensation.min, s.compensation.max].filter(v => v != null).map(v => Number(v).toLocaleString()).join(' – ')} ${esc(s.compensation.currency || '')} ${s.compensation.period ? 'per ' + esc(s.compensation.period) : ''}${s.compensation.source === 'job_board' ? ' (from the job board)' : ''}` : null],
  ];
  return `<div class="jd-grid">${rows.map(([label, value]) => `<div class="jd-row" data-jd-field="${attr(label)}"><span class="caps-sm">${esc(label)}</span><div class="small">${jdValue(value)}</div></div>`).join('')}</div>`;
}
function jdDerived(data) {
  const d = data.structured_description?.derived_signals || {};
  const groups = [['likely_interview_domains', 'Interview domains'], ['likely_system_design_topics', 'System design'],
    ['likely_leadership_topics', 'Leadership'], ['likely_business_topics', 'Business']];
  const any = groups.some(([key]) => (d[key] || []).length);
  return `<div class="box soft" data-jd-derived><div class="row gap8 mb8">${pill('Inferred', 'warn')}<span class="xs muted">${esc(data.derived_label || 'likely / inferred preparation topics, not stated by the employer')}</span></div>${any
    ? groups.filter(([key]) => (d[key] || []).length).map(([key, label]) => `<div class="small mb4"><span class="b">${label}:</span> ${esc((d[key] || []).join(', '))}</div>`).join('')
    : '<div class="small muted">No signals inferred from this posting yet.</div>'}</div>`;
}
function jdComparison(data) {
  const candidate = data.candidate;
  if (!candidate) return '';
  return `<div class="box warn mb12" data-jd-compare><div class="small b mb8">A new parse is waiting: you corrected ${candidate.changes.filter(c => c.corrected).length} of these fields, so nothing was overwritten.</div>
    ${candidate.changes.map(c => `<div class="jd-compare" data-jd-change="${c.id}"><div class="xs mono mb4">${esc(c.path)}${c.corrected ? ' · your correction' : ''}</div><div class="jd-compare-values"><div><span class="xs muted">Current</span><div class="small">${jdValue(c.current)}</div></div><div><span class="xs muted">New parse</span><div class="small">${jdValue(c.proposed)}</div></div></div><div class="row gap6 mt6">${btn('Keep', 'jd-decide', { change: c.id, choice: 'keep' }, `sm ${(JD.decisions[c.id] || 'keep') === 'keep' ? 'primary' : ''}`)}${btn('Use new', 'jd-decide', { change: c.id, choice: 'use_new' }, `sm ${JD.decisions[c.id] === 'use_new' ? 'primary' : ''}`)}</div></div>`).join('')}
    <div class="row gap8 mt10">${btn('Apply choices', 'jd-resolve', {}, 'primary sm', isBusy('jd'))}${btn('Keep everything', 'jd-resolve', { all: 'keep' }, 'sm')}</div></div>`;
}
function renderJobStructured(job) {
  if (JD.error && !JD.data) return `<div style="padding:20px 22px"><div class="settings-error" role="alert">${esc(JD.error)}</div>${btn('Try again', 'jd-reload', { id: job.id }, 'mt12 sm')}</div>`;
  if (!JD.data || JD.data.job_id !== job.id) { jobDataLoad(job.id); return `<div style="padding:20px 22px">${loading()}</div>`; }
  const data = JD.data, [label, tone] = JD_STATUS[data.status] || JD_STATUS.RAW;
  const task = data.parse_task;
  const editing = !!JD.draft;
  const counts = data.counts || {};
  const requirements = (data.requirements || []).filter(r => !r.derived);
  const head = `<div class="row between gap10 wrap mb12"><div class="row gap8 wrap">${pill(label, tone)}<span class="xs muted">${counts.REQUIRED || 0} required · ${counts.PREFERRED || 0} preferred${data.parsed_at ? ` · parsed ${esc(new Date(data.parsed_at + 'Z').toLocaleDateString())}` : ''}</span></div>
    <div class="row gap6 wrap">${editing ? `${btn('Cancel', 'jd-cancel', {}, 'sm')}${btn('Save corrections', 'jd-save', { id: job.id }, 'sm primary', isBusy('jd'))}`
      : `${btn('Correct values', 'jd-edit', {}, 'sm', !data.structured_description || isBusy('jd'))}${btn('Re-parse', 'jd-reparse', { id: job.id }, 'sm', isBusy('jd'))}${btn(JD.view === 'history' ? 'Back' : 'History', 'jd-history', { id: job.id }, 'sm')}`}</div></div>`;
  const error = JD.error ? `<div class="settings-error mb12" role="alert">${esc(JD.error)}</div>` : '';
  if (JD.view === 'history') {
    const items = JD.history?.items;
    return `<div style="padding:20px 22px">${head}${error}${items == null ? loading() : items.length
      ? `<div class="jd-history">${items.map(c => `<div class="jd-history-row" data-jd-change-source="${c.change_source}"><div class="row between gap8"><span class="small b">${esc(c.change_source)}</span><span class="xs muted">${esc(new Date(c.created_at + 'Z').toLocaleString())}</span></div><div class="xs mono ellipsis">${esc(c.field_path)}</div><div class="xs muted">${jdValue(c.old_value)} → ${jdValue(c.new_value)}</div></div>`).join('')}</div>`
      : '<div class="small muted">No changes recorded yet.</div>'}</div>`;
  }
  if (!data.structured_description) {
    return `<div style="padding:20px 22px">${head}${error}<div class="small muted">${task && task.status !== 'COMPLETED'
      ? `This posting is queued for parsing (${esc(task.status.toLowerCase())}${task.attempt_count ? `, attempt ${task.attempt_count}` : ''}).${task.last_error ? ` Last error: ${esc(task.last_error)}` : ''}`
      : 'This posting has not been parsed into requirements yet.'} Ranking and tailoring use the posting as written until it is.</div></div>`;
  }
  if (editing) {
    return `<div style="padding:20px 22px">${head}${error}<p class="settings-help mb12">Correct anything the parser got wrong. Corrected fields are never overwritten by a later parse.</p><div class="settings-grid">${JD_FIELDS.map(([path, fieldLabel, kind]) => jdField(path, fieldLabel, kind, jdGet(JD.draft, path))).join('')}</div></div>`;
  }
  const unmatched = JD.unmatched.length ? `<div class="box warn small" data-jd-unmatched role="status"><strong>Saved, but not applied to the requirements:</strong> ${JD.unmatched.map(v => esc(v)).join(' · ')}. Each matches no single requirement from the posting (or several), so ranking and tailoring still read the parsed requirements for it.</div>` : '';
  const stale = data.stale ? `<div class="box warn small" data-jd-stale>The posting changed after it was parsed. Ranking and tailoring use the posting as written until it is parsed again.</div>` : '';
  return `<div style="padding:20px 22px" class="col gap14">${head}${error}${unmatched}${stale}${jdComparison(data)}${jdSummary(data)}
    <div><div class="caps mb8">REQUIREMENTS · IN THE EMPLOYER'S WORDS</div>${requirements.length
      ? `<div class="col gap6">${jdRequirementGroups(requirements).map(jdRequirementItem).join('')}</div>`
      : '<div class="small muted">No requirements were extracted from this posting.</div>'}</div>
    <div><div class="caps mb8">LIKELY PREPARATION TOPICS</div>${jdDerived(data)}</div></div>`;
}

const JOB_DATA_ACTIONS = {
  'jd-reload': el => { JD.data = null; JD.error = ''; return jobDataLoad(Number(el.dataset.id), true); },
  'jd-edit': () => { JD.draft = JSON.parse(JSON.stringify(JD.data.structured_description)); JD.view = 'summary'; render(); },
  'jd-cancel': () => { jdReset(); render(); },
  'jd-save': el => jdRun(async () => {
    try {
      JD.data = await api(`/api/jobs/${el.dataset.id}/structured-description`, { method: 'PUT', body: { structured_description: JD.draft, base_hash: JD.data.structured_hash } });
    } catch (err) {
      if (err.status !== 409) throw err;
      // A parse landed while editing: show the new values instead of overwriting them with an older document.
      jdReset();
      JD.data = await api(`/api/jobs/${el.dataset.id}/structured-description`);
      throw err;
    }
    jdReset();
    JD.unmatched = JD.data.unmatched_corrections || [];
  }, 'Corrections saved'),
  'jd-reparse': el => jdRun(async () => {
    JD.data = await api(`/api/jobs/${el.dataset.id}/structured-description/reparse`, { method: 'POST' });
    JD.decisions = {};
  }, 'Job description parsed again'),
  'jd-decide': el => { JD.decisions[el.dataset.change] = el.dataset.choice; render(); },
  'jd-resolve': el => {
    const decisions = el.dataset.all ? {} : { ...JD.decisions };
    return jdRun(async () => {
      JD.data = await api(`/api/jobs/${JD.jobId}/structured-description/reparse/resolve`, { method: 'POST', body: { decisions } });
      JD.decisions = {};
      JD.unmatched = JD.data.unmatched_corrections || [];
    }, 'Choices applied');
  },
  'jd-history': el => {
    if (JD.view === 'history') { JD.view = 'summary'; render(); return; }
    JD.view = 'history'; JD.history = null; render();
    return jdRun(async () => { JD.history = await api(`/api/jobs/${el.dataset.id}/structured-description/history`); });
  },
};

function jobDataInput(el) {
  if (!JD.draft) return;
  const kind = el.dataset.jdKind, raw = el.value;
  const value = kind === 'tri' ? (raw === '' ? null : raw === 'true')
    : kind === 'number' ? (raw.trim() === '' ? null : Number(raw))
      : kind === 'list' ? raw.split(',').map(s => s.trim()).filter(Boolean)
        : kind === 'lines' ? raw.split('\n').map(s => s.trim()).filter(Boolean)
          : raw;
  jdSet(JD.draft, el.dataset.jdPath, value);
}
