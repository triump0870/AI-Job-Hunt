/* Settings → Resumes: every resume with its file, its structured data and the roles it is for, and the review,
   editing and verification of one resume's data. Loaded after settings.js, before app.js.
   The server owns verification state; this editor only sends typed structured data and verify/reject requests. */
'use strict';
const RD_SECTIONS = [
  ['personal_information', 'Personal information'], ['summary', 'Professional summary'], ['skills', 'Skills'],
  ['experience', 'Work experience'], ['projects', 'Projects'], ['education', 'Education'],
  ['certifications', 'Certifications'], ['awards', 'Awards'], ['languages', 'Languages'],
  ['additional_information', 'Additional information'],
];
// Editable fields per section: [key, label, kind]. kind: text | textarea | list (comma separated) | bool | month
const RD_FIELDS = {
  personal_information: [['full_name', 'Full name'], ['headline', 'Headline'], ['location', 'Location'], ['email', 'Email'], ['phone', 'Phone'], ['linkedin', 'LinkedIn'], ['github', 'GitHub'], ['portfolio', 'Portfolio']],
  summary: [['text', 'Summary', 'textarea']],
  skills: [['name', 'Skill'], ['category', 'Category']],
  experience: [['job_title', 'Job title'], ['company', 'Company'], ['location', 'Location'], ['employment_type', 'Employment type'], ['start_date', 'Start (YYYY-MM)', 'month'], ['end_date', 'End (YYYY-MM)', 'month'], ['is_current', 'Current role', 'bool'], ['technologies', 'Technologies', 'list']],
  projects: [['name', 'Name'], ['url', 'Link (optional)'], ['image_url', 'Image link (optional)'], ['description', 'Details', 'textarea'], ['result', 'Result', 'textarea'], ['technologies', 'Technologies', 'list']],
  education: [['degree', 'Degree'], ['field_of_study', 'Field of study'], ['institution', 'Institution'], ['university', 'University'], ['location', 'Location'], ['start_date', 'Start (YYYY-MM)', 'month'], ['end_date', 'End (YYYY-MM)', 'month'], ['grade', 'Grade (GPA / %)']],
  certifications: [['name', 'Name'], ['issuer', 'Issuer'], ['issue_date', 'Issued'], ['expiration_date', 'Expires'], ['credential_id', 'Credential ID'], ['url', 'URL']],
  awards: [['title', 'Title'], ['issuer', 'Issuer'], ['date', 'Date'], ['description', 'Description', 'textarea']],
  languages: [['name', 'Language'], ['proficiency', 'Proficiency']],
  additional_information: [['text', 'Detail', 'textarea'], ['url', 'Website link (optional)']],
};
const RD_BULLETS = { experience: [['achievements', 'Achievements'], ['responsibilities', 'Responsibilities']], projects: [['achievements', 'Achievements']] };
const RD_STATUS = { VERIFIED: ['✓', 'Verified', 'good'], REJECTED: ['✕', 'Rejected', 'bad'], EDITED: ['!', 'Edited · needs review', 'warn'], UNVERIFIED: ['?', 'Unverified', 'muted'] };
const RD_STATE = { VERIFIED: ['✓', 'good'], NEEDS_REVIEW: ['!', 'warn'], PARTIAL: ['…', 'brand'], UNVERIFIED: ['?', 'muted'], EMPTY: ['–', 'muted'] };
const RD_RESUME = { VERIFIED: ['Resume verified', 'good'], NEEDS_REVIEW: ['Needs review', 'warn'], PARTIALLY_VERIFIED: ['Partially verified', 'brand'], UNVERIFIED: ['Unverified', 'muted'] };
const RD = { tab: 'structured', renaming: null, editing: null, draft: null, decisions: {}, blocking: null, changes: null, raw: null, loading: false, viewId: null };

// The version shown and edited here. Viewing or editing a version never makes it active: only "Make active" does.
const rdActive = () => D.resumeData?.current || D.resumeData?.active || null;
const rdResumeUrl = () => RD.viewId ? `/api/settings/resume?resume_id=${RD.viewId}` : '/api/settings/resume';
const rdClone = value => JSON.parse(JSON.stringify(value ?? null));
const rdStatus = node => node?.verification?.status || 'UNVERIFIED';
function rdDate(iso) { return iso ? new Date(iso + (iso.endsWith('Z') ? '' : 'Z')).toLocaleString() : ''; }
function rdReset() { RD.editing = null; RD.draft = null; RD.blocking = null; }
// Before the screen shows another resume (view, activate, delete, a new upload or text): an open section edit
// belongs to the resume it was opened on, so it is discarded — after asking when it has changes. False: stay.
function rdLeaveDraft() {
  if (RD.editing && resumeDataDirty() && !confirm('Discard unsaved resume data edits?')) return false;
  rdReset(); return true;
}
function resumeDataDirty() {
  const active = rdActive();
  return !!(RD.editing && RD.draft && active && JSON.stringify(RD.draft) !== JSON.stringify(active.structured));
}

async function resumeDataLoad(message) {
  RD.loading = true;
  try { D.resumeData = await api(rdResumeUrl()); RD.changes = null; RD.raw = null; if (message) toast(message); }
  catch (err) { D.resumeData = { error: err.message }; }
  finally { RD.loading = false; render(); }
}
function rdApply(detail) {
  const versions = (D.resumeData?.versions || []).map(v => v.id === detail.id ? { ...v, ...detail } : v);
  const current = !D.resumeData?.current || D.resumeData.current.id === detail.id ? detail : D.resumeData.current;
  D.resumeData = { ...D.resumeData, current, active: detail.is_active ? detail : D.resumeData?.active, versions };
  RD.changes = null;
}
async function rdRun(action, success) {
  await settingsRun('resume-data', async () => { await action(); }, success);
}
// A version read before the new reader (no extraction record) whose PDF/DOCX is still stored can be read again.
function rdCanReread(active, version) {
  return !!(D.resumeData?.extraction_v2 && !active.extraction && version.has_original && /\.(pdf|docx)$/i.test(version.source_filename || ''));
}

function rdPost(path, body, success) {
  const active = rdActive();
  return rdRun(async () => { rdApply(await api(`/api/resumes/${active.id}/${path}`, { method: 'POST', body })); }, success);
}

/* ───────────── draft editing ───────────── */
function rdGet(path) { return path.split('.').reduce((node, key) => node?.[key], RD.draft); }
function rdSet(path, value) {
  const keys = path.split('.'), last = keys.pop();
  const parent = keys.reduce((node, key) => node[key], RD.draft);
  parent[last] = value;
}
function resumeDataInput(el) {
  if (!RD.draft) return;
  const kind = el.dataset.rdKind;
  const value = kind === 'bool' ? el.checked : kind === 'list' ? el.value.split(',').map(s => s.trim()).filter(Boolean) : el.value;
  rdSet(el.dataset.rdPath, kind === 'month' && !value ? null : value);
  settingsTouched('resume-data');
}
function rdTemplate(section) {
  const blank = Object.fromEntries((RD_FIELDS[section] || []).map(([key, , kind]) => [key, kind === 'list' ? [] : kind === 'bool' ? false : kind === 'month' ? null : '']));
  for (const [key] of RD_BULLETS[section] || []) blank[key] = [];
  return blank;
}
async function resumeDataSave() {
  const active = rdActive();
  if (!active || !RD.draft) return;
  await settingsRun('resume-data', async () => {
    try { rdApply(await api(`/api/resumes/${active.id}/structured`, { method: 'PUT', body: { revision: active.revision, data: RD.draft } })); }
    catch (err) {
      if (err.status === 409) { await resumeDataLoad(); throw new Error(`${err.message} Your edits were not saved.`); }
      throw err;
    }
    rdReset();
  }, 'Resume data saved');
}

/* ───────────── rendering ───────────── */
// A unit's status as a small icon (the word is in its tooltip and for screen readers); for a fact still waiting, how
// far the reader trusts it when that is less than high (resume extraction V2), with the reasons in its tooltip.
const RD_CONF = { medium: ['Check', 'warn'], low: ['Low', 'bad'] };
function rdBadge(node) {
  const [icon, label, tone] = RD_STATUS[rdStatus(node)] || RD_STATUS.UNVERIFIED;
  const conf = rdStatus(node) === 'UNVERIFIED' ? rdActive()?.confidence?.[node.id] : null;
  const [clabel, ctone] = (conf && RD_CONF[conf.level]) || [];
  const confidence = clabel ? `<span class="pill ${ctone} rd-conf" data-rd-conf="${conf.level}" title="${attr(`${conf.level === 'low' ? 'Low' : 'Medium'} confidence: ${conf.reasons.join('; ')}`)}">${clabel}<span class="sr-only">: ${esc(conf.reasons.join('; '))}</span></span>` : '';
  return `<span class="pill ${tone} rd-badge" title="${attr(label)}" data-rd-status="${rdStatus(node)}"><span aria-hidden="true">${icon}</span><span class="sr-only">${esc(label)}</span></span>${confidence}`;
}
// Unverified facts the reader rates high: what "Accept high-confidence" verifies (the person's own action).
function rdConfident(active) {
  const levels = active?.confidence || {};
  const walk = (node, out) => {
    if (Array.isArray(node)) node.forEach(n => walk(n, out));
    else if (node && typeof node === 'object') {
      if (node.id && node.verification && rdStatus(node) === 'UNVERIFIED' && levels[node.id]?.level === 'high') out.push(node.id);
      Object.values(node).forEach(v => { if (v && typeof v === 'object') walk(v, out); });
    }
    return out;
  };
  return walk(active?.structured, []).length;
}
function rdIcon(icon, label, act, data, disabled) {
  return `<button class="btn sm rd-icon" data-act="${act}" ${Object.entries(data).map(([k, v]) => `data-${k}="${attr(v)}"`).join(' ')} aria-label="${attr(label)}" title="${attr(label)}" ${disabled ? 'disabled' : ''}>${icon}</button>`;
}
function rdItemActions(node) {
  if (!node?.id) return '';
  // Only the decision still open: verify or reject a fact waiting for review; undo one already decided.
  const status = rdStatus(node), busy = isBusy('settings-resume-data') || !!RD.editing;
  const decided = status === 'VERIFIED' || status === 'REJECTED';
  return `<span class="row gap4 rd-item-actions">${decided ? rdIcon('↺', `Undo: ${status === 'VERIFIED' ? 'verified' : 'rejected'}, back to review`, 'rd-unverify', { id: node.id }, busy)
    : `${rdIcon('✓', 'Verify', 'rd-verify-item', { id: node.id, status: 'VERIFIED' }, busy)}${rdIcon('✕', 'Reject', 'rd-verify-item', { id: node.id, status: 'REJECTED' }, busy)}`}</span>`;
}
// Skills as chips, grouped by their category: one glance instead of a card per skill.
function rdSkills(skills) {
  const groups = new Map();
  for (const skill of skills) groups.set(skill.category || '', [...(groups.get(skill.category || '') || []), skill]);
  return [...groups].map(([category, list]) => `<div class="rd-skill-group">${category ? `<div class="caps-sm">${esc(category)}</div>` : ''}<div class="rd-chips">${list.map(s => `<span class="rd-chip is-${rdStatus(s).toLowerCase()}" data-rd-item="${attr(s.id)}"${s.raw ? ` title="The resume writes it “${attr(s.raw)}”"` : ''}>${rdBadge(s)}<span>${esc(s.name)}${s.raw ? `<span class="faint"> · written “${esc(s.raw)}”</span>` : ''}</span>${rdItemActions(s)}</span>`).join('')}</div></div>`).join('');
}
function rdHas(data, section) {
  const value = data[section];
  if (Array.isArray(value)) return value.length > 0;
  if (section === 'summary') return !!value?.text;
  return !!value && (RD_FIELDS[section] || []).some(([key]) => value[key]);
}
function rdDates(item) {
  const start = item.start_date || item.raw_start_date || '', end = item.is_current ? 'Present' : item.end_date || item.raw_end_date || '';
  return [start, end].filter(Boolean).join(' – ');
}
function rdTitle(section, item) {
  if (section === 'experience') return [item.job_title, item.company].filter(Boolean).join(' · ');
  if (section === 'education') return [item.degree, item.field_of_study, item.institution].filter(Boolean).join(' · ');
  if (section === 'languages') return [item.name, item.proficiency].filter(Boolean).join(' · ');
  if (section === 'skills') return item.category ? `${item.name} (${item.category})` : item.name;
  return item.name || item.title || item.text || '';
}
// An http(s) URL for a link or image the person typed ("github.com/x" reads as https); anything else (javascript:,
// data:) is never made clickable or loaded.
function rdHttpUrl(value) {
  const text = String(value || '').trim();
  if (!text || /[\s<>"'`\\]/.test(text)) return '';
  const href = /^https?:\/\//i.test(text) ? text : /^[a-z][a-z0-9+.-]*:/i.test(text) ? '' : `https://${text.replace(/^\/+/, '')}`;
  return /^https?:\/\/[^/?#]+\.[^/?#]+/i.test(href) ? href : '';
}
function rdLink(value, text) {
  const href = rdHttpUrl(value);
  return href ? `<a href="${attr(href)}" target="_blank" rel="noopener noreferrer">${esc(text || value)} ↗</a>` : esc(value || '');
}
function rdViewItem(section, item) {
  const meta = [section === 'experience' || section === 'education' ? rdDates(item) : '', item.university, item.location, item.grade, item.issuer, item.issue_date || item.date,
    (item.technologies || []).join(', '), section === 'projects' ? '' : item.description].filter(Boolean);
  const image = section === 'projects' && rdHttpUrl(item.image_url) ? `<img class="rd-project-image" src="${attr(rdHttpUrl(item.image_url))}" alt="${attr(item.name ? `${item.name} image` : 'Project image')}" loading="lazy" referrerpolicy="no-referrer">` : '';
  const extra = [
    section === 'projects' && item.description ? `<p class="rd-text"><span class="caps-sm">Details</span> ${esc(item.description)}</p>` : '',
    section === 'projects' && item.result ? `<p class="rd-text"><span class="caps-sm">Result</span> ${esc(item.result)}</p>` : '',
    (section === 'projects' || section === 'additional_information') && item.url ? `<p class="small">${rdLink(item.url, section === 'projects' ? 'Project link' : item.url)}</p>` : '',
  ].join('');
  const bullets = (RD_BULLETS[section] || []).map(([key, label]) => (item[key] || []).length ? `<div class="rd-bullets"><div class="caps-sm">${label}</div>${item[key].map(b => `<div class="rd-bullet" data-rd-item="${attr(b.id)}">${rdBadge(b)}<span class="grow">${esc(b.text)}</span>${rdItemActions(b)}</div>`).join('')}</div>` : '').join('');
  return `<article class="rd-item" data-rd-item="${attr(item.id)}"><div class="rd-item-head">${rdBadge(item)}${image}<div class="grow"><strong>${esc(rdTitle(section, item) || (section === 'additional_information' && item.url ? 'Website link' : 'Untitled'))}</strong>${meta.length ? `<p>${esc(meta.join(' · '))}</p>` : ''}${extra}</div>${rdItemActions(item)}</div>${bullets}</article>`;
}
function rdInput(path, label, kind, value) {
  const id = `rd-${path.replace(/\./g, '-')}`;
  if (kind === 'bool') return `<label class="settings-check" for="${id}"><input type="checkbox" id="${id}" data-rd-path="${path}" data-rd-kind="bool" ${value ? 'checked' : ''}> ${esc(label)}</label>`;
  if (kind === 'textarea') return `<label class="field" for="${id}"><span>${esc(label)}</span><textarea class="textarea" id="${id}" data-rd-path="${path}">${esc(value ?? '')}</textarea></label>`;
  const shown = kind === 'list' ? (value || []).join(', ') : value ?? '';
  return `<label class="field" for="${id}"><span>${esc(label)}</span><input class="input" id="${id}" data-rd-path="${path}" ${kind ? `data-rd-kind="${kind}"` : ''} ${kind === 'month' ? 'placeholder="YYYY-MM" pattern="\\d{4}-\\d{2}"' : ''} value="${attr(shown)}"></label>`;
}
function rdListControls(path, index, length) {
  return `<span class="row gap6">${btn('↑', 'rd-move', { path, index, dir: -1 }, 'sm', index === 0)}${btn('↓', 'rd-move', { path, index, dir: 1 }, 'sm', index === length - 1)}${btn('Remove', 'rd-remove', { path, index }, 'sm')}</span>`;
}
function rdEditItem(section, path, item, index, length) {
  const fields = (RD_FIELDS[section] || []).map(([key, label, kind]) => rdInput(`${path}.${key}`, label, kind, item[key])).join('');
  const bullets = (RD_BULLETS[section] || []).map(([key, label]) => `<div class="rd-bullets"><div class="caps-sm">${label}</div>${(item[key] || []).map((b, i) => `<div class="rd-bullet-edit">${rdInput(`${path}.${key}.${i}.text`, `${label.slice(0, -1)} ${i + 1}`, 'textarea', b.text)}${rdListControls(`${path}.${key}`, i, item[key].length)}</div>`).join('')}${btn(`+ Add ${label.toLowerCase().slice(0, -1)}`, 'rd-add', { path: `${path}.${key}`, bullet: 1 }, 'sm')}</div>`).join('');
  const controls = index == null ? '' : `<div class="row between gap8"><span class="xs muted">${item.id ? `Changing a verified value marks it for review.` : 'New · starts unverified'}</span>${rdListControls(path.split('.').slice(0, -1).join('.'), index, length)}</div>`;
  return `<article class="rd-item rd-editing">${controls}<div class="settings-grid">${fields}</div>${bullets}</article>`;
}
// A section with content (or a required one, or the one being edited); empty optional sections stay out of the way
// behind "+ Add section".
function rdShown(section, data, progress) {
  const state = progress.sections.find(s => s.section === section);
  return RD.editing === section || rdHas(data, section) || !!state?.required;
}
function rdSectionCard(section, label, data, progress) {
  if (!rdShown(section, data, progress)) return '';
  const state = progress.sections.find(s => s.section === section) || { state: 'EMPTY', counts: {} };
  const [icon, tone] = RD_STATE[state.state] || RD_STATE.EMPTY;
  const editing = RD.editing === section, value = editing ? RD.draft[section] : data[section];
  const list = Array.isArray(value);
  let body;
  if (editing) {
    body = list ? value.map((item, i) => rdEditItem(section, `${section}.${i}`, item, i, value.length)).join('') + btn(`+ Add ${label.toLowerCase()}`, 'rd-add', { path: section, section }, 'sm')
      : rdEditItem(section, section, value || {}, null, 0);
  } else if (list && section === 'skills' && value.length) {
    body = rdSkills(value);
  } else if (list) {
    body = value.length ? value.map(item => rdViewItem(section, item)).join('') : '<p class="settings-help">Nothing found for this section. Use Edit to add it.</p>';
  } else {
    const text = section === 'summary' ? value?.text : RD_FIELDS[section].map(([key]) => value?.[key]).filter(Boolean).join(' · ');
    body = text ? `<article class="rd-item" data-rd-item="${attr(value?.id || section)}"><div class="rd-item-head">${rdBadge(value)}<div class="grow"><p class="rd-text">${esc(text)}</p></div>${rdItemActions(value)}</div></article>` : '<p class="settings-help">Nothing found for this section. Use Edit to add it.</p>';
  }
  const locked = isBusy('settings-resume-data') || (RD.editing && !editing);
  const pending = (state.counts?.UNVERIFIED || 0) + (state.counts?.EDITED || 0);
  const buttons = editing ? `${btn('Cancel', 'rd-cancel', {}, 'sm')}${btn('Save section', 'settings-save', { form: 'resume-data' }, 'sm primary', !resumeDataDirty())}`
    : `${pending ? btn(`Verify all ${pending}`, 'rd-verify-section', { section }, 'sm', locked) : ''}${btn('Edit', 'rd-edit', { section }, 'sm', locked)}`;
  const count = list && !editing && value.length ? `<span class="faint small">${value.length}</span>` : '';
  return `<section class="settings-group rd-section" id="rd-section-${section}" data-rd-section="${section}"><div class="row between gap10 wrap mb12"><div class="row gap8"><span class="pill ${tone}" aria-label="${attr(state.state)}">${icon}</span><h3>${esc(label)}</h3>${count}${state.required ? '<span class="xs muted">required</span>' : ''}</div><div class="row gap6 wrap">${buttons}</div></div>${body}</section>`;
}
// How the file was read (resume extraction V2): the quality route, and each heading as written with the section it was
// put in and how. Changing a heading's section re-reads the resume with it (filled while nothing is reviewed, else a
// comparison). Folded away unless a heading needs a decision; versions read before V2 show nothing.
const RD_FIX = { ocr: ['Page shows it', 'good'], rule: ['Consistent rule', 'brand'], 'ocr+rule': ['Page + rule', 'good'] };
const RD_HELD = { skill: 'Skill', bullet: 'Bullet', role: 'Role', summary: 'Summary', project: 'Project', certification: 'Certification', award: 'Award', language: 'Language', education: 'Education', additional: 'Additional' };
const RD_METHOD = { synonym: ['Known', 'good'], keyword: ['Guess', 'warn'], unknown: ['Choose', 'bad'], learned: ['Your earlier choice', 'brand'], person: ['Yours', 'brand'] };
const RD_ROUTE = { clean: ['Read cleanly', 'good'], suspicious: ['A few words may be misread', 'warn'], poor: ['Many words may be misread', 'bad'], no_text: ['No text found (a scan?)', 'bad'] };
function rdSectionsPanel(active) {
  const ex = active.extraction;
  if (!ex) return '';
  const locked = rdLocked(), [route, tone] = RD_ROUTE[ex.quality?.route] || ['Read', 'muted'];
  const rows = ex.sections.map(s => {
    const [method, mtone] = RD_METHOD[s.method] || [s.method, 'muted'];
    const options = ex.choices.map(c => `<option value="${attr(c.kind)}" ${c.kind === s.kind && s.method !== 'unknown' ? 'selected' : ''}>${esc(c.label)}</option>`).join('');
    return `<div class="rd-sec-row${s.method === 'unknown' ? ' needs' : ''}" data-rd-heading="${s.line}"><div class="grow"><strong>${esc(s.text)}</strong>${s.preview ? `<div class="faint small ellipsis">${esc(s.preview)}</div>` : ''}</div>${pill(method, mtone)}<select class="select rd-sec-select" data-act-change="rd-section-assign" data-line="${s.line}" aria-label="Section for ${attr(s.text)}" ${locked ? 'disabled' : ''}>${s.method === 'unknown' ? '<option value="" selected disabled>Choose a section…</option>' : ''}${options}</select></div>`;
  }).join('') || '<p class="settings-help">No section headings were found: everything was read as one block.</p>';
  const help = ex.unplaced ? `<div class="settings-notice"><strong>${ex.unplaced} heading${ex.unplaced === 1 ? '' : 's'} need${ex.unplaced === 1 ? 's' : ''} a section</strong><p>The reader could not place ${ex.unplaced === 1 ? 'it' : 'them'}. Choose a section to verify this resume; your choice is remembered for your next uploads.</p></div>` : '';
  const reasons = (ex.quality?.reasons || []).length ? ` · ${esc(ex.quality.reasons.join(' · '))}` : '';
  const review = ex.review || [], fixes = ex.corrections || [], pending = fixes.filter(c => c.status === 'applied').length;
  const fixed = fixes.length ? `<div class="rd-fixes mb12"><div class="caps-sm mb8">Corrections · ${fixes.length}</div><p class="settings-help mb8">Words the file’s text got wrong (a broken font mapping), corrected only where ${ex.quality?.ocr ? 'the page itself shows the word, or ' : ''}one substitution explains several words. The file is unchanged; undo any you disagree with.</p>${fixes.map(c => {
    const [label, tone] = RD_FIX[c.method] || [c.method, 'muted'];
    const undone = c.status === 'undone';
    return `<div class="rd-fix-row${undone ? ' is-undone' : ''}" data-rd-fix="${attr(c.id)}"><div class="grow"><span class="rd-fix-words"><s>${esc(c.original)}</s> → <b>${esc(c.corrected)}</b></span>${c.count > 1 ? `<span class="faint small"> ×${c.count}</span>` : ''}<div class="faint small">${esc((c.evidence || []).join(' · '))}</div></div>${pill(label, tone)}${undone ? `${pill('Undone', 'muted')}${btn('Correct again', 'rd-fix', { id: c.id, action: 'keep' }, 'sm', locked)}` : `${c.status === 'kept' ? pill('Kept', 'good') : btn('Keep', 'rd-fix', { id: c.id, action: 'keep' }, 'sm', locked)}${btn('Undo', 'rd-fix', { id: c.id, action: 'undo' }, 'sm ghost', locked)}`}</div>`;
  }).join('')}</div>` : '';
  const held = review.length ? `<div class="rd-held mt16"><div class="caps-sm mb8">Held back for review · ${review.length}</div><p class="settings-help mb8">The AI parse proposed ${review.length === 1 ? 'this value' : 'these values'}, but the resume’s text does not contain ${review.length === 1 ? 'it' : 'them'} (a reworded or corrected word, or something it inferred). ${review.length === 1 ? 'It is' : 'They are'} not used unless you add ${review.length === 1 ? 'it' : 'them'}.</p>${review.map(item => `<div class="rd-held-row" data-rd-held="${attr(item.id)}"><span class="pill plain">${esc(RD_HELD[item.kind] || item.kind)}</span><div class="grow"><span>${esc(item.value)}</span>${item.where ? `<div class="faint small">${esc(item.where)}</div>` : ''}</div>${item.addable ? btn('Add', 'rd-review', { id: item.id, action: 'add' }, 'sm', locked) : '<span class="faint small">add it with Edit</span>'}${btn('Discard', 'rd-review', { id: item.id, action: 'discard' }, 'sm ghost', locked)}</div>`).join('')}</div>` : '';
  // <small>, not <span>: the details summary turns its <span> (the "+") when open.
  const title = `<small class="rd-sec-sum"><b>How it was read</b>${pill(route, tone)}<em class="faint">${ex.sections.length} section${ex.sections.length === 1 ? '' : 's'}${ex.unplaced ? ` · ${ex.unplaced} to place` : ''}${fixes.length ? ` · ${fixes.length} correction${fixes.length === 1 ? '' : 's'}` : ''}${review.length ? ` · ${review.length} held back` : ''}</em></small>`;
  return `<details class="settings-details rd-sections" data-settings-details="resume-sections" ${ex.unplaced || review.length || pending || SETTING_DETAILS.has('resume-sections') ? 'open' : ''}><summary>${title}<span aria-hidden="true">+</span></summary><div class="settings-details-body">${help}${fixed}<p class="settings-help mb8">Each heading as the file writes it, and the section its lines were put in. Wrong? Pick another: the resume is read again with it${reasons}.</p><div class="rd-sec-list">${rows}</div>${held}</div></details>`;
}
// The sections at a glance, sticky while reviewing: jump to one, or add one the parser did not find.
function rdNav(data, progress) {
  const locked = rdLocked();
  const shown = RD_SECTIONS.filter(([section]) => rdShown(section, data, progress));
  const hidden = RD_SECTIONS.filter(([section]) => !rdShown(section, data, progress));
  const chips = shown.map(([section, label]) => {
    const state = progress.sections.find(s => s.section === section) || { state: 'EMPTY' };
    const [icon, tone] = RD_STATE[state.state] || RD_STATE.EMPTY;
    const n = Array.isArray(data[section]) ? data[section].length : 0;
    return `<a href="#settings/resumes" class="rd-nav-chip" data-act="rd-jump" data-section="${section}"><span class="pill ${tone}" aria-hidden="true">${icon}</span>${esc(label)}${n ? `<span class="faint">${n}</span>` : ''}</a>`;
  }).join('');
  const add = hidden.length ? `<details class="rd-menu"><summary class="btn sm ghost">+ Add section</summary><div class="rd-menu-list">${hidden.map(([section, label]) => btn(esc(label), 'rd-add-section', { section }, 'sm', locked)).join('')}</div></details>` : '';
  return `<nav class="rd-nav" aria-label="Resume sections">${chips}${add}</nav>`;
}
function rdComparison(pending) {
  if (!pending) return '';
  const rows = pending.changes.map(c => {
    const choice = RD.decisions[c.id] || 'keep';
    const show = v => v == null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v);
    return `<div class="rd-compare" data-rd-change="${c.id}"><div class="small b">${esc(c.label)}${c.current_status ? ` · currently ${esc(c.current_status.toLowerCase())}` : ''}</div><div class="rd-compare-values"><div><span class="xs muted">Current value</span><p>${esc(show(c.current))}</p></div><div><span class="xs muted">New parser result</span><p>${esc(show(c.proposed))}</p></div></div><div class="row gap6">${btn('Keep current', 'rd-decide', { change: c.id, choice: 'keep' }, `sm ${choice === 'keep' ? 'primary' : ''}`)}${btn('Use new', 'rd-decide', { change: c.id, choice: 'use_new' }, `sm ${choice === 'use_new' ? 'primary' : ''}`)}</div></div>`;
  }).join('');
  return settingsGroup('Re-parse comparison', `The ${esc(pending.parser)} parser found ${pending.changes.length} difference${pending.changes.length === 1 ? '' : 's'}. Nothing changes until you choose. Values you take from the parser start unverified.`,
    `${rows || '<p class="settings-help">The new parse matches your current data.</p>'}<div class="row gap8 mt12">${btn('Apply choices', 'rd-resolve', {}, 'primary sm', isBusy('settings-resume-data'))}${btn('Keep everything', 'rd-resolve', { all: 'keep' }, 'sm')}</div>`);
}
const RD_POLICY_SOURCES = [
  ['structured', 'Structured data', 'The resume is built from its data: your corrections are used everywhere (titles, employers, dates, education). Rejected facts are left out; unverified facts are claimed only as allowed below.'],
  ['master_text', 'Resume text as written', 'The resume’s own text: its role headings and bullets, used as-is. Corrections to its data are not applied.'],
];
function rdLocked() { return isBusy('settings-resume-data') || !!RD.editing; }
// One resume in the library: name, how far its data is reviewed, its target roles; one button to open it, and the
// rest (rename, make active, download, delete) in its menu.
function rdResumeCard(v, current) {
  const locked = rdLocked(), viewing = current && v.id === current.id;
  const kind = v.file ? esc((v.file.filename.split('.').pop() || 'file').toUpperCase().slice(0, 4)) : 'TXT';
  const source = v.file ? `${esc(v.file.filename)} · ${Math.max(1, Math.round(v.file.size / 1024))} KB` : 'Created from text';
  const status = (RD_RESUME[v.verification_status] || RD_RESUME.UNVERIFIED)[0], percent = v.reviewed_percent ?? 0;
  const menu = `<details class="rd-menu"><summary class="btn sm ghost" aria-label="More actions for ${attr(v.name || 'resume')}">⋯</summary><div class="rd-menu-list">${btn('Rename', 'rd-rename', { id: v.id, name: v.name || '' }, 'sm', locked)}${v.is_active ? '' : btn('Make active', 'rd-activate', { id: v.id }, 'sm', locked)}${v.has_original ? `<a class="btn sm" href="/api/resumes/${v.id}/original">Download original</a>` : ''}${v.is_active ? '' : btn('Delete', 'rd-delete-version', { id: v.id, name: `${v.name || 'Resume'} · v${v.version}` }, 'sm danger', locked)}</div></details>`;
  return `<article class="rd-lib-row${viewing ? ' is-viewing' : ''}" data-rd-resume="${v.id}"${viewing ? ' aria-current="true"' : ''}>
    <div class="settings-file-icon" aria-hidden="true">${kind}</div>
    <div class="grow">${RD.renaming === v.id ? rdRenameEditor(v) : `<div class="row gap8 wrap" style="align-items:center"><strong>${esc(v.name || 'Resume')} · v${v.version}</strong>${v.is_active ? pill('Active', 'good') : ''}</div>`}
      <div class="rd-lib-meta"><span class="rd-mini-progress" aria-hidden="true"><span style="width:${percent}%"></span></span><span>${percent}% reviewed · ${esc(status)}</span><span class="faint">${source}${v.setups?.length ? ` · used by ${esc(v.setups.map(x => x.name).join(', '))}` : ''}</span></div>
      <div class="rd-roles" title="Jobs whose title names one of these roles are ranked and tailored with this resume">${resumeTags('version', v.id, v.domain, v.roles || [], v.domains)}</div>
    </div>
    <div class="row gap6 rd-lib-actions">${viewing ? pill('Open', 'brand') : btn('Review', 'rd-view', { id: v.id }, 'sm', locked)}${menu}</div>
  </article>`;
}
// Renaming changes only the label: the file, data, tags, verification and the active resume stay as they are.
function rdRenameEditor(v) {
  return `<div class="row gap8 wrap" style="align-items:center" data-rd-rename="${v.id}">${inputF('rdname', 'name', v.name || '', `maxlength="300" aria-label="Resume name" style="margin:0;flex:0 1 320px;min-width:0" data-enter="rd-rename-save" data-id="${v.id}"`)}<span class="small muted">· v${v.version}</span>${btn(label('rdRename', 'Save name', 'Saving…'), 'rd-rename-save', { id: v.id }, 'sm primary', isBusy('rdRename'))}${btn('Cancel', 'rd-rename-cancel', {}, 'sm')}</div>`;
}
// Re-matching jobs to the resume tags (POST /api/jobs/resume/rematch): above the list, so it is found right after
// tagging. The person narrows the jobs (stage, when found, search, board); a dry run counts them as the filters change,
// and the run asks first. Pinned choices, approved and applied jobs are never touched.
const RM_STAGES = { new: 'Not ranked yet', ranked: 'Ranked', tailored: 'Tailored', archived: 'Archived' };
const RM_DEFAULT_STAGES = ['new', 'ranked', 'tailored'];
const RM_FOUND = [['', 'Any time'], ['7', 'Last 7 days'], ['30', 'Last 30 days'], ['90', 'Last 90 days']];
const rmStages = R => R.filters.stages ?? RM_DEFAULT_STAGES;
function rdRematchBody(R = RD.rematch) {
  const f = R.filters;
  return { stages: rmStages(R), found_within_days: Number(f.found) || null, saved_search_id: Number(f.setup) || null, board: f.board || null };
}
async function rdRematchPlan() {
  const R = RD.rematch;
  if (!R) return;
  const seq = ++R.seq;
  R.loading = true; render();
  try {
    const plan = await api('/api/jobs/resume/rematch?dry_run=true', { method: 'POST', body: rdRematchBody(R) });
    if (RD.rematch === R && seq === R.seq) R.plan = plan;  // only the answer to the latest filters counts
  } catch (err) { toast(err.message, true); }
  finally { if (seq === R.seq) R.loading = false; render(); }
}
function rdRematchCount(R) {
  const p = R.plan;
  if (!p) return R.loading ? 'Counting jobs…' : '';
  const s = n => (n === 1 ? '' : 's');
  const into = p.into?.length ? ` (${p.into.map(i => `${i.jobs} → ${esc(i.resume)}`).join(', ')})` : '';
  return `<b>${p.jobs}</b> job${s(p.jobs)} selected · <b>${p.changing}</b> would switch resume${into}${p.reranked ? ` · ${p.reranked} ranked again (model calls)` : ''}${R.loading ? ' · updating…' : ''}`;
}
function rdRematchBar(versions) {
  if (!versions.some(v => v.domain || v.roles?.length || v.domains?.length)) return '';
  const R = RD.rematch;
  if (!R) return `<div class="row gap8 wrap mb12" style="align-items:center" data-rd-rematch>${btn('Re-match jobs to resume tags…', 'resume-rematch', {}, 'sm primary')}<span class="settings-help">New jobs use the tags right away. Choose which jobs found earlier to re-match — by stage, when they were found, the search or the board.</span></div>`;
  const opts = R.plan?.options || { setups: [], boards: [] };
  const sel = (key, list, value) => `<select class="select" style="margin:0;width:auto;padding:6px 8px" data-act-change="rm-filter" data-key="${key}" aria-label="${attr(key)}">${list.map(([v, t]) => `<option value="${attr(v)}" ${String(v) === String(value) ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>`;
  const stages = rmStages(R);
  const n = R.plan?.changing || 0;
  return `<div class="mb12" data-rd-rematch style="border:1px solid var(--line);border-radius:12px;padding:14px 16px">
    <div class="row gap16 wrap" style="align-items:center">
      <div class="row gap8 wrap" style="align-items:center"><span class="xs muted">Stage</span>${Object.entries(RM_STAGES).map(([id, text]) => `<label class="settings-check" style="margin:0"><input type="checkbox" data-act-change="rm-stage" value="${id}" ${stages.includes(id) ? 'checked' : ''}> ${text}</label>`).join('')}</div>
      <label class="row gap8" style="align-items:center"><span class="xs muted">Found</span>${sel('found', RM_FOUND, R.filters.found)}</label>
      ${opts.setups.length ? `<label class="row gap8" style="align-items:center"><span class="xs muted">Search</span>${sel('setup', [['', 'All searches'], ...opts.setups.map(x => [x.id, x.name])], R.filters.setup)}</label>` : ''}
      ${opts.boards.length ? `<label class="row gap8" style="align-items:center"><span class="xs muted">Board</span>${sel('board', [['', 'All boards'], ...opts.boards.map(b => [b.name, `${b.name} (${b.jobs})`])], R.filters.board)}</label>` : ''}
    </div>
    <div class="small mt10" data-rm-count>${rdRematchCount(R)}</div>
    <div class="row gap8 wrap mt10" style="align-items:center">${btn(label('resumeRematch', `Re-match ${n} job${n === 1 ? '' : 's'}`, 'Re-matching…'), 'rm-run', {}, 'sm primary', isBusy('resumeRematch') || !n || R.loading)}${btn('Close', 'rm-close', {}, 'sm')}<span class="settings-help">Jobs that switch are ranked again; tailored ones are re-tailored when automatic tailoring is on. Approved, applied and pinned jobs never move.</span></div>
  </div>`;
}
function rdResumeList(current) {
  const versions = D.resumeData.versions || [];
  const accept = 'accept=".txt,.pdf,.docx" aria-label="Upload resume"';
  const upload = versions.length ? `<label class="btn sm primary rd-upload" for="resume-file" title="PDF, DOCX or TXT · up to ${BOOT.max_upload_mb} MB">＋ Upload resume<input type="file" id="resume-file" ${accept}></label>` : '';
  const rows = versions.map(v => rdResumeCard(v, current)).join('')
    || `<div class="settings-empty"><strong>Your experience starts here</strong><p>Upload a resume to make your applications more personal. It is read into data you review and verify here.</p></div><label class="settings-upload" for="resume-file"><strong>＋ Upload your resume</strong><span>PDF, DOCX or TXT · up to ${BOOT.max_upload_mb} MB</span><input type="file" id="resume-file" ${accept}></label>`;
  return `<section class="settings-group rd-library"><div class="row between gap10 wrap mb12"><div class="settings-group-title" style="margin:0"><h3>Your resumes</h3><p>The active resume is used for ranking, tailoring and review, unless a job’s title matches another resume’s target roles. Uploading never changes which one is active.</p></div>${upload}</div>
    ${rdRematchBar(versions)}<div class="rd-library-list">${rows}</div></section>`;
}
// Everything that changes how resumes are read and used, in one place, folded away by default.
function rdOptions() {
  const policy = D.resumeData.policy || {};
  return settingsDetails('resume-options', 'AI and policy options', `
    <div class="caps-sm mb8">What AI works from (every resume)</div>
    <fieldset class="settings-options" style="border:0;padding:0;margin:0 0 10px"><legend class="sr-only">Resume source for AI</legend>${RD_POLICY_SOURCES.map(([value, title, help]) => `<label class="settings-check" style="align-items:flex-start"><input type="radio" name="rd-resume-source" data-act-change="rd-policy-source" value="${value}" ${(policy.resume_source || 'structured') === value ? 'checked' : ''}><span><b>${title}</b><br><span class="settings-help">${help}</span></span></label>`).join('')}</fieldset>
    <div class="settings-checks" style="margin-top:6px"><label class="settings-check" for="rd-policy-allow"><input type="checkbox" id="rd-policy-allow" data-act-change="rd-policy" data-key="allow_unverified_resume_facts" ${policy.allow_unverified_resume_facts ? 'checked' : ''}> Allow unverified facts in tailored resumes and follow-ups</label><label class="settings-check" for="rd-policy-rank"><input type="checkbox" id="rd-policy-rank" data-act-change="rd-policy" data-key="ranking_uses_unverified" ${policy.ranking_uses_unverified ? 'checked' : ''}> Let ranking read unverified facts as lower-confidence context</label></div>`);
}
// The AI structuring of this version (resume_parsing on the server): running, or how it ended. While it runs the
// screen re-reads the resume every few seconds, so the filled data or the comparison shows up without a reload.
const RD_AI_TONE = { filled: 'good', compare: 'brand', kept: 'muted', not_configured: 'warn', gone: 'muted' };
let rdAiTimer = null;
function rdAiParse(ai) {
  if (!ai) return '';
  const tone = ai.active ? 'brand' : ai.status === 'FAILED' ? 'bad' : RD_AI_TONE[ai.outcome] || 'muted';
  const dropped = !ai.active && ai.dropped ? ` ${ai.dropped} value${ai.dropped === 1 ? '' : 's'} the resume text does not contain ${ai.dropped === 1 ? 'was' : 'were'} left out.` : '';
  const detail = !ai.active && ai.detail && ai.outcome !== 'filled' && ai.outcome !== 'compare' ? ` ${esc(ai.detail)}` : '';
  rdAiPoll(ai);
  return `<p class="settings-help mt8" data-rd-ai-parse="${ai.active ? 'active' : esc(ai.outcome || ai.status.toLowerCase())}">${pill(ai.note || 'AI parse', tone)}${dropped}${detail}</p>`;
}
function rdAiPoll(ai) {
  if (rdAiTimer || !ai?.active) return;
  rdAiTimer = setTimeout(async () => {
    rdAiTimer = null;
    if (!document.querySelector('[data-rd-ai-parse="active"]')) return; // the screen moved on
    if (RD.editing) return rdAiPoll(ai); // never replace data under an open edit
    try {
      const fresh = await api(rdResumeUrl());
      if (RD.editing) return rdAiPoll(ai);
      D.resumeData = fresh; RD.changes = null;
      const now = rdActive()?.ai_parse;
      if (now && !now.active && now.note) toast(now.note);
      render();
    } catch { rdAiPoll(ai); }
  }, 4000);
}
// The resume being reviewed: where its review stands and the one next step (review the next fact, then verify the
// resume); re-parsing and the original file are in its menu.
function rdHeader() {
  const active = rdActive(), p = active.progress, [statusLabel, tone] = RD_RESUME[active.verification_status] || RD_RESUME.UNVERIFIED;
  const blocking = RD.blocking || p.blocking;
  const locked = rdLocked();
  // Already verified: nothing to verify until a change (an edit, a re-parse choice) sends it back to review.
  const verified = active.verification_status === 'VERIFIED';
  const left = p.needs_review || 0;
  const next = !verified && left ? btn(`Review next <span class="rd-count">${left} left</span>`, 'rd-next', {}, 'primary', locked) : '';
  const confident = verified ? 0 : rdConfident(active);
  const accept = confident ? btn(`Accept ${confident} high-confidence`, 'rd-accept-confident', {}, '', locked) : '';
  const unplaced = active.extraction?.unplaced || 0;
  const verify = verified ? btn('✓ Resume verified', 'rd-verify-resume', {}, 'good', true)
    : btn('Verify resume', 'rd-verify-resume', {}, next || unplaced ? '' : 'primary', locked || blocking.length > 0 || unplaced > 0);
  const version = (D.resumeData.versions || []).find(v => v.id === active.id) || active;
  const menu = `<details class="rd-menu"><summary class="btn">More ▾</summary><div class="rd-menu-list">${btn('Re-parse (quick)', 'rd-reparse', { ai: 0 }, 'sm', locked)}${btn('Re-parse with AI', 'rd-reparse', { ai: 1 }, 'sm', locked || !!active.ai_parse?.active)}${rdCanReread(active, version) ? btn('Re-read file with the new reader', 'rd-reread', { id: active.id }, 'sm', locked) : ''}${version.has_original ? `<a class="btn sm" href="/api/resumes/${active.id}/original">Download original</a>` : ''}</div></details>`;
  const counts = [`${p.counts.VERIFIED} verified`, `${left} require review`, p.counts.EDITED ? `${p.counts.EDITED} edited` : '', p.counts.REJECTED ? `${p.counts.REJECTED} rejected` : ''].filter(Boolean).join(' · ');
  return `<section class="settings-group rd-header" id="rd-detail">
    <div class="row between gap10 wrap" style="align-items:flex-start"><div class="grow"><span class="settings-eyebrow">REVIEWING</span><div class="row gap8 wrap" style="align-items:center"><strong class="rd-title">${esc(active.name || 'Resume')} · v${active.version}</strong>${active.is_active ? pill('Active', 'good') : `${pill('Not active', 'muted')}${btn('Make active', 'rd-activate', { id: active.id }, 'sm ghost', locked)}`}${pill(statusLabel, tone)}</div></div><span class="settings-complete" data-rd-percent>${p.percent}% reviewed</span></div>
    <div class="rd-progress" role="progressbar" aria-label="Facts reviewed" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${p.percent}"><span style="width:${p.percent}%"></span></div>
    <p class="settings-help">${counts}. Only verified facts are used in tailored applications.${active.verified_at ? ` Last verified ${esc(rdDate(active.verified_at))}.` : ''}</p>
    ${active.is_active ? '' : '<p class="settings-help"><b>This resume is not active.</b> Reviewing it does not change which resume is used: it is used for jobs that match its target roles, or that you or a search setup chose it for.</p>'}
    ${RD.blocking?.length ? `<div class="settings-notice"><strong>${blocking.length} required item${blocking.length === 1 ? '' : 's'} still need review</strong><p>${blocking.slice(0, 5).map(b => esc(b.label)).join(' · ')}${blocking.length > 5 ? ' …' : ''}</p></div>` : ''}
    <div class="rd-head-actions">${next}${accept}${verify}${menu}</div>${confident ? `<p class="settings-help mt8">High-confidence facts are in the file’s text word for word, with nothing corrected. Facts marked Check or Low say why in their tooltip.</p>` : ''}${rdAiParse(active.ai_parse)}</section>` +
    rdComparison(active.pending_reparse);
}
function rdTabs() {
  const tabs = [['structured', 'Review'], ['raw', 'Text'], ['history', 'History']];
  return `<div class="rd-tabbar" role="tablist">${tabs.map(([id, label]) => `<button class="rd-tab${RD.tab === id ? ' is-on' : ''}" role="tab" aria-selected="${RD.tab === id}" data-act="rd-tab" data-tab="${id}">${label}</button>`).join('')}</div>`;
}
function rdTabBody(data) {
  const active = rdActive();
  if (RD.tab === 'raw') {
    if (RD.raw == null) { rdFetch('raw'); return loading(); }
    return settingsGroup('Raw text', 'Read-only. Correct facts in Structured Data; the parsed text itself is never edited.', `<pre class="rd-raw" data-rd-raw tabindex="0">${esc(RD.raw)}</pre>`);
  }
  if (RD.tab === 'history') {
    if (RD.changes == null) { rdFetch('changes'); return loading(); }
    const show = v => v == null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v);
    return settingsGroup('Change history', `${RD.changes.total} change${RD.changes.total === 1 ? '' : 's'}, newest first.`, RD.changes.items.length
      ? `<div class="rd-history">${RD.changes.items.map(c => `<div class="rd-history-row" data-rd-change-type="${c.change_type}"><div class="row between gap8"><span class="small b">${esc(c.change_type.replace(/_/g, ' '))} · ${esc(c.source)}</span><span class="xs muted">${esc(rdDate(c.changed_at))}</span></div><div class="xs mono ellipsis">${esc(c.field_path)}</div><div class="xs muted">${esc(show(c.old_value))} → ${esc(show(c.new_value))}</div></div>`).join('')}</div>`
      : '<p class="settings-help">No changes yet.</p>');
  }
  if (rdAwaitingAi(active)) return settingsGroup('Structuring with AI', 'You structure uploads with AI only: the Resume Parser model is reading this resume, and its data shows here when it is done. If the model cannot answer, the quick parse is shown instead.', rdAiParse(active.ai_parse));
  return `${rdSectionsPanel(active)}<div class="rd-review">${rdNav(data, active.progress)}${RD_SECTIONS.map(([section, label]) => rdSectionCard(section, label, data, active.progress)).join('')}</div>`;
}
// AI only (Running workers → Resume parsing): a version the model has not structured yet shows no regex data to review.
const rdAwaitingAi = active => !!(D.resumeData?.ai_only && active?.parser !== 'llm' && active?.ai_parse?.active);
async function rdFetch(kind) {
  const active = rdActive();
  if (RD.loading || !active) return;
  RD.loading = true;
  try {
    if (kind === 'raw') RD.raw = (await api(`/api/resumes/${active.id}/raw`)).raw_text || '';
    else RD.changes = await api(`/api/resumes/${active.id}/changes?limit=200`);
  } catch (err) { SETTING_ERRORS['resume-data'] = err.message; if (kind === 'raw') RD.raw = ''; else RD.changes = { total: 0, items: [] }; }
  finally { RD.loading = false; render(); }
}
// Settings → Resumes: the library, then the resume being reviewed (its next step, its sections), then the options
// and the text tools, folded away.
function renderResumesScreen() {
  if (D.resumeData === undefined || D.resumeData === null) { if (!RD.loading) resumeDataLoad(); return loading(); }
  if (D.resumeData.error) return `<div class="settings-group"><div class="settings-error" role="alert">Your resumes could not be loaded: ${esc(D.resumeData.error)}</div>${btn('Try again', 'rd-reload', {}, 'mt16')}</div>`;
  const current = rdActive(), data = current?.structured || {};
  const detail = current ? rdHeader() + rdTabs() + rdTabBody(data) + (RD.editing ? settingsFooter('resume-data', 'Save section') : '') : '';
  return rdResumeList(current) + detail + rdOptions() + resumeTextTools();
}
const renderResumeDataSettings = renderResumesScreen;

// The screen's menus (<details class="rd-menu">) close on a click outside them and on Escape.
if (typeof document !== 'undefined') {
  document.addEventListener('click', e => document.querySelectorAll('.rd-menu[open]').forEach(d => { if (!d.contains(e.target)) d.open = false; }));
  document.addEventListener('keydown', e => { if (e.key === 'Escape') document.querySelectorAll('.rd-menu[open]').forEach(d => { d.open = false; d.querySelector('summary')?.focus?.(); }); });
}

const RESUME_DATA_ACTIONS = {
  'resume-rematch': () => { RD.rematch = { filters: { stages: null, found: '', setup: '', board: '' }, plan: null, loading: false, seq: 0 }; return rdRematchPlan(); },
  'rm-stage': el => {
    const R = RD.rematch, stages = new Set(rmStages(R));
    if (el.checked) stages.add(el.value); else stages.delete(el.value);
    R.filters.stages = Object.keys(RM_STAGES).filter(id => stages.has(id));
    return rdRematchPlan();
  },
  'rm-filter': el => { RD.rematch.filters[el.dataset.key] = el.value; return rdRematchPlan(); },
  'rm-close': () => { RD.rematch = null; render(); },
  'rm-run': () => busy('resumeRematch', async () => {
    const R = RD.rematch, plan = R?.plan;
    if (!plan?.changing) return { skipped: true };
    if (!confirm(`${plan.changing} of the ${plan.jobs} selected job${plan.jobs === 1 ? '' : 's'} would switch resume${plan.reranked ? `, and ${plan.reranked} ranked job${plan.reranked === 1 ? '' : 's'} would be ranked again (model calls)` : ''}. Pinned choices are not touched. Continue?`)) return { cancelled: true };
    const out = await api('/api/jobs/resume/rematch', { method: 'POST', body: rdRematchBody(R) });
    await rdRematchPlan();
    return out;
  }, out => out.cancelled ? '' : out.skipped ? 'Every selected job already has its best-matching resume' : `${out.changing} job${out.changing === 1 ? '' : 's'} re-matched · ${out.reranked} queued for ranking`),
  'rd-reload': () => resumeDataLoad(),
  'rd-tab': el => { if (RD.editing && resumeDataDirty() && !confirm('Discard unsaved resume data edits?')) return; rdReset(); RD.tab = el.dataset.tab; render(); },
  'rd-jump': (el, e) => { e?.preventDefault?.(); RD.tab = 'structured'; render(); document.getElementById(`rd-section-${el.dataset.section}`)?.scrollIntoView?.({ block: 'start' }); },
  // The next fact that still needs a decision, in reading order: scrolled to, highlighted and focused.
  'rd-next': () => {
    if (RD.tab !== 'structured') { RD.tab = 'structured'; render(); }
    const badge = document.querySelector('#view [data-rd-section] [data-rd-status="UNVERIFIED"], #view [data-rd-section] [data-rd-status="EDITED"]');
    const item = badge?.closest('[data-rd-item]');
    if (!item) return;
    item.scrollIntoView?.({ block: 'center' });
    item.classList.add('rd-flash'); setTimeout(() => item.classList.remove('rd-flash'), 1600);
    item.querySelector('button:not([disabled])')?.focus?.({ preventScroll: true });
  },
  // A section the parser found nothing for: opened for editing with one blank entry.
  'rd-add-section': el => {
    if (RD.editing && !rdLeaveDraft()) return;
    const section = el.dataset.section;
    RD.editing = section; RD.draft = rdClone(rdActive().structured);
    if (Array.isArray(RD.draft[section])) { if (!RD.draft[section].length) RD.draft[section].push(rdTemplate(section)); }
    else if (!RD.draft[section]) RD.draft[section] = section === 'summary' ? { text: '' } : rdTemplate(section);
    RD.tab = 'structured'; render();
    document.getElementById(`rd-section-${section}`)?.scrollIntoView?.({ block: 'start' });
  },
  'rd-edit': el => { RD.editing = el.dataset.section; RD.draft = rdClone(rdActive().structured); if (!RD.draft[RD.editing] && RD.editing === 'summary') RD.draft.summary = { text: '' }; render(); },
  'rd-cancel': () => { if (resumeDataDirty() && !confirm('Discard unsaved resume data edits?')) return; rdReset(); delete SETTING_ERRORS['resume-data']; render(); },
  'rd-add': el => {
    const list = rdGet(el.dataset.path);
    list.push(el.dataset.bullet ? { text: '' } : rdTemplate(el.dataset.section));
    render(); settingsTouched('resume-data');
  },
  'rd-remove': el => { rdGet(el.dataset.path).splice(Number(el.dataset.index), 1); render(); settingsTouched('resume-data'); },
  'rd-move': el => {
    const list = rdGet(el.dataset.path), from = Number(el.dataset.index), to = from + Number(el.dataset.dir);
    if (to < 0 || to >= list.length) return;
    [list[from], list[to]] = [list[to], list[from]]; render(); settingsTouched('resume-data');
  },
  'rd-verify-item': el => rdPost('verify-item', { item_id: el.dataset.id, status: el.dataset.status }, el.dataset.status === 'VERIFIED' ? 'Verified' : 'Rejected'),
  'rd-unverify': el => rdPost('unverify-item', { item_id: el.dataset.id }, 'Marked unverified'),
  'rd-verify-section': el => rdPost('verify-section', { section: el.dataset.section }, 'Section verified'),
  'rd-verify-resume': () => rdActive()?.verification_status === 'VERIFIED' ? undefined : rdRun(async () => {
    try { rdApply(await api(`/api/resumes/${rdActive().id}/verify`, { method: 'POST' })); RD.blocking = null; }
    catch (err) { if (err.detail?.blocking) RD.blocking = err.detail.blocking; throw err; }
  }, 'Resume verified'),
  // The AI parse runs in the background: it fills the data while nothing is reviewed, else it arrives as a comparison.
  'rd-reparse': el => { RD.decisions = {}; const ai = el.dataset.ai === '1'; return rdPost('reparse', { use_ai: ai }, ai ? 'Structuring with AI…' : 'Re-parse ready to compare'); },
  // A new version from the same file; the one read before (and which resume is active) stays as it is.
  'rd-reread': el => rdLeaveDraft() && rdRun(async () => {
    const out = await api(`/api/resumes/${el.dataset.id}/reread`, { method: 'POST' });
    RD.viewId = out.id; D.resumeData = await api(rdResumeUrl()); D.resumeChoices = null; RD.changes = null; RD.raw = null;
  }, 'File read again with the new reader: review it'),
  'rd-decide': el => { RD.decisions[el.dataset.change] = el.dataset.choice; render(); },
  'rd-resolve': el => {
    const decisions = el.dataset.all ? {} : { ...RD.decisions };
    RD.decisions = {};
    return rdPost('reparse/resolve', { decisions }, 'Re-parse choices applied');
  },
  'rd-activate': el => settingsResumeGuard() && rdLeaveDraft() && rdRun(async () => { await api(`/api/resumes/${el.dataset.id}/activate`, { method: 'POST' }); RD.viewId = null; D.resumeData = await api(rdResumeUrl()); D.profile = await api('/api/profile'); delete S.forms.resumes; D.resumeChoices = null; RD.changes = null; RD.raw = null; }, 'Active resume changed: ranking, tailoring and review now use it'),
  'rd-rename': el => { RD.renaming = Number(el.dataset.id); S.forms.rdname = { name: el.dataset.name || '' }; render(); document.getElementById('rdname-name')?.focus(); },
  'rd-rename-cancel': () => { RD.renaming = null; delete S.forms.rdname; render(); },
  'rd-rename-save': el => {
    const name = String(fv('rdname', 'name', '') || '').trim();
    if (!name) return toast('A resume needs a name', true);
    return busy('rdRename', async () => {
      const out = await api(`/api/resumes/${el.dataset.id}`, { method: 'PUT', body: { name } });
      const rename = r => r && r.id === out.id ? { ...r, name: out.name } : r;
      D.resumeData = { ...D.resumeData, versions: (D.resumeData.versions || []).map(rename), active: rename(D.resumeData.active), current: rename(D.resumeData.current) };
      RD.renaming = null; delete S.forms.rdname; D.resumeChoices = null;
      return out;
    }, 'Resume renamed');
  },
  'rd-view': el => {
    if (!rdLeaveDraft()) return;
    RD.viewId = Number(el.dataset.id); RD.tab = 'structured';
    return rdRun(async () => { D.resumeData = await api(rdResumeUrl()); RD.changes = null; RD.raw = null; })
      .then(() => document.getElementById('rd-detail')?.scrollIntoView?.({ block: 'start' }));
  },
  // The active resume cannot be deleted (the server refuses too): make another one active first.
  'rd-delete-version': el => {
    if (!confirm(`Delete ${el.dataset.name}? Its file, data and verification are removed. Jobs or searches that used it go back to matching by target roles (else the active resume), and facts only it verified stop being used.`)) return;
    if (RD.viewId === Number(el.dataset.id) && !rdLeaveDraft()) return;
    return rdRun(async () => {
      const out = await api(`/api/resumes/${el.dataset.id}`, { method: 'DELETE' });
      if (RD.viewId === Number(el.dataset.id)) RD.viewId = null;
      D.resumeData = await api(rdResumeUrl()); D.resumeChoices = null; RD.changes = null; RD.raw = null;
     
      toast(`Resume deleted${out.jobs ? ` · ${out.jobs} job${out.jobs === 1 ? '' : 's'} now matched by tags` : ''}${out.evidence_retired ? ` · ${out.evidence_retired} fact${out.evidence_retired === 1 ? '' : 's'} no longer verified` : ''}`);
    });
  },
  'rd-policy-source': el => rdRun(async () => { D.resumeData = { ...D.resumeData, policy: await api('/api/settings/resume/policy', { method: 'PUT', body: { resume_source: el.value } }) }; }, el.value === 'master_text' ? 'AI now works from the resume text as written' : 'AI now works from your structured resume data'),
  // A value the AI parse proposed that the text does not contain: added as the person's change (unverified) or discarded.
  'rd-review': el => {
    const active = rdActive(), add = el.dataset.action === 'add';
    return rdRun(async () => {
      rdApply(await api(`/api/resumes/${active.id}/review`, { method: 'POST', body: { id: el.dataset.id, action: el.dataset.action } }));
    }, add ? 'Added: review it like any other fact' : 'Discarded');
  },
  // A correction the reader made: kept (confirmed), undone (the file's own word comes back) or made again.
  'rd-fix': el => {
    const active = rdActive(), keep = el.dataset.action === 'keep';
    return rdRun(async () => {
      rdApply(await api(`/api/resumes/${active.id}/corrections`, { method: 'POST', body: { id: el.dataset.id, action: el.dataset.action } }));
    }, keep ? 'Correction kept' : 'Correction undone: read again with the file’s own word');
  },
  'rd-accept-confident': () => {
    const active = rdActive(), n = rdConfident(active);
    if (!n || !confirm(`Verify ${n} fact${n === 1 ? '' : 's'} the reader rates high (in the file’s text word for word, nothing corrected)? You can undo any of them.`)) return;
    return rdRun(async () => { rdApply(await api(`/api/resumes/${active.id}/verify-confident`, { method: 'POST' })); }, `${n} fact${n === 1 ? '' : 's'} verified`);
  },
  'rd-section-assign': el => {
    if (!el.value) return;
    const active = rdActive();
    return rdRun(async () => {
      rdApply(await api(`/api/resumes/${active.id}/sections`, { method: 'POST', body: { line: Number(el.dataset.line), kind: el.value } }));
    }, () => (rdActive()?.pending_reparse ? 'Read again with that section: compare the changes below' : 'Read again with that section'));
  },
  'rd-policy': el => rdRun(async () => { D.resumeData = { ...D.resumeData, policy: await api('/api/settings/resume/policy', { method: 'PUT', body: { [el.dataset.key]: el.checked } }) }; }, 'Candidate policy saved'),
};
