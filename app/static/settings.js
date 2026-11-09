/* Focused settings editors. Loaded before app.js; functions use the workspace's shared helpers. */
'use strict';
const SETTINGS_SECTIONS = [
  ['profile', 'Profile', 'Your professional identity', '01'],
  ['preferences', 'Job preferences', 'What your next role looks like', '02'],
  // One screen for resumes: each resume with its file, its structured data and the roles it is for.
  ['resumes', 'Resumes', 'Each resume, its data and the roles it is for', '03'],
  ['account', 'Account & security', 'Manage access to your workspace', '04'],
  ['api-keys', 'API keys', 'Connect your own providers', '05'],
  ['domain', 'Domain filter', 'Which discovered jobs are kept, and what it learned from you', '06'],
  ['gmail', 'Gmail inbox', 'Read-only updates on the applications you submitted', '07'],
  // Phase 6: whether the application worker may submit for you, what you approve it to attest, and saved logins.
  ['applying', 'Auto submit', 'Submit applications for you, saved ATS logins and routes', '08'],
  // Server-wide (admin): the LangGraph switch and graph modes, the application worker, browser-use and tracing.
  ['automation', 'AI agents & automation', 'LangGraph, the application worker and browser-use', '09'],
];
// AI models moved to AI Orchestration → Models (ai_models.js); its direct-provider form ('ai') is still saved here.
const SETTINGS_FORMS = ['profile', 'preferences', 'resumes', 'resume-data', 'account', 'keys', 'domain'];
// Everything that paces and steers the background work is edited in Running workers → Settings (app/static/workers.js)
// and saved with one PATCH /api/settings: processing limits, the ranking cadence, rate-limit backoff, approvals, who
// tailors and reviews, the tailored resume length and automatic tailoring.
const WORKER_SETTING_FIELDS = ['ranking_concurrency', 'tailoring_concurrency', 'ranking_interval_minutes', 'rate_limit_backoff', 'rate_limit_base_seconds', 'rate_limit_multiplier', 'rate_limit_max_seconds',
  'application_mode', 'min_review_score', 'execution_mode', 'auto_tailor', 'auto_tailor_min_fit_score', 'tailor_pages', 'tailor_budget_1', 'tailor_budget_2', 'tailor_budget_3'];
// Your weekly target and follow-up wait are personal pacing, not a worker setting: they live with Job preferences.
const RHYTHM_FIELDS = ['weekly_target', 'followup_days'];
const DOMAIN_FIELDS = ['enabled', 'learn_from_feedback', 'exclude_terms', 'skill_excludes', 'include_terms'];
const API_KEYS = [['openrouter', 'OpenRouter', 'AI model marketplace'], ['openai', 'OpenAI', 'GPT models'], ['anthropic', 'Anthropic', 'Claude models'], ['gemini', 'Gemini', 'Gemini models (Google AI Studio key)'], ['apify', 'Apify', 'Job discovery']];
const CHIP_FIELDS = [
  ['target_roles', 'Target roles', 'Choose up to 5 roles', 'Add a role…', '', 'top'],
  ['preferred_locations', 'Target locations', 'Cities, regions or Remote', 'Add a location…', '', 'top'],
  ['countries', 'Countries', 'Where you would consider working', 'Add a country…', '', 'sp'],
  ['primary_skills', 'Primary skills', 'Must-have matches', 'Add a skill…', '', 'sp'],
  ['secondary_skills', 'Secondary skills', 'Nice-to-have matches', 'Add a skill…', '', 'sp'],
  ['industries', 'Industries', 'Preferred domains', 'Add an industry…', '', 'sp'],
  ['exclude_companies', 'Excluded companies', 'Keep these out of your results', 'Add a company…', '', 'sp'],
  ['languages', 'Languages', 'Used in application answers', 'Add a language…', '', 'sp'],
];
const PROFILE_FIELDS = ['name', 'headline', 'email', 'phone', 'location', 'linkedin_url', 'github_url'];
const PREFERENCE_TOP = ['target_roles', 'preferred_locations', 'work_authorization', 'compensation_notes'];
const PREFERENCE_NESTED = ['total_experience', 'notice_period', 'open_to_relocation', 'open_to_hybrid', 'open_to_contract', ...CHIP_FIELDS.filter(c => c[5] === 'sp').map(c => c[0])];
const SETTING_ERRORS = {};
const SETTINGS_SUCCESS = {};
const SETTING_DETAILS = new Set();
let editingKey = null;
let authorizationDraft = null;
const AUTH_COUNTRIES = ['India', 'Singapore', 'Dubai (UAE)', 'United States', 'United Kingdom', 'Canada', 'Australia', 'Other'];
const AUTH_STATUSES = ['Citizen', 'Permanent resident', 'EP (Employment Pass)', 'Work permit', 'Foreigner / visa required', 'Other'];

function authorizationRows(value) {
  const lines = String(value || '').split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  const notes = lines.filter(line => /^notes?:/i.test(line)).map(line => line.replace(/^notes?:\s*/i, '')).join('\n');
  const rows = lines.filter(line => !/^notes?:/i.test(line)).map(line => {
    const match = line.match(/^(.+?)\s*(?:—|-|,)\s*(.+)$/);
    if (match) return { country: match[1].trim(), status: match[2].trim() };
    return AUTH_STATUSES.includes(line) ? { country: '', status: line } : { country: line, status: '' };
  });
  return { rows: rows.length ? rows : [{ country: '', status: '' }], notes };
}
function authorizationValue() {
  const rows = [...document.querySelectorAll('[data-auth-row]')].map(row => ({
    country: row.querySelector('[data-auth-country]')?.value.trim() || '',
    status: row.querySelector('[data-auth-status]')?.value.trim() || '',
  })).filter(row => row.country || row.status);
  const notes = document.getElementById('preferences-authorization-notes')?.value.trim() || '';
  return [...rows.map(row => [row.country || 'Other', row.status || 'Other'].join(' — ')), notes ? `Notes: ${notes}` : ''].filter(Boolean).join('\n');
}
function settingsAuthUpdate() {
  const rows = [...document.querySelectorAll('[data-auth-row]')].map(row => ({
    country: row.querySelector('[data-auth-country]')?.value.trim() || '',
    status: row.querySelector('[data-auth-status]')?.value.trim() || '',
  }));
  const notes = document.getElementById('preferences-authorization-notes')?.value.trim() || '';
  authorizationDraft = { rows, notes };
  setF('preferences', 'work_authorization', authorizationValue());
}
function authorizationOptions(options, selected) {
  return options.map(option => `<option value="${attr(option)}" ${option === selected ? 'selected' : ''}>${esc(option)}</option>`).join('');
}
function authorizationEditor(value) {
  const parsed = authorizationRows(value), { rows, notes } = authorizationDraft || parsed;
  return `<div class="authorization-editor"><p class="settings-help">Add one entry for each country or market. This helps match roles to your actual right to work.</p><div class="authorization-rows">${rows.map((row, index) => `<div class="authorization-row" data-auth-row><label class="field" for="preferences-auth-country-${index}"><span>Country / market</span><select class="select" id="preferences-auth-country-${index}" data-auth-country data-auth-entry="1" data-auth-index="${index}"><option value="">Select a country</option>${authorizationOptions(AUTH_COUNTRIES, row.country)}</select></label><label class="field" for="preferences-auth-status-${index}"><span>Authorization</span><select class="select" id="preferences-auth-status-${index}" data-auth-status data-auth-entry="1" data-auth-index="${index}"><option value="">Select status</option>${authorizationOptions(AUTH_STATUSES, row.status)}</select></label>${rows.length > 1 ? btn('Remove', 'auth-remove', { index }, 'sm') : ''}</div>`).join('')}</div>${btn('+ Add country', 'auth-add', {}, 'sm')}<label class="field authorization-notes" for="preferences-authorization-notes"><span>Visa notes (optional)</span><textarea class="textarea" id="preferences-authorization-notes" data-auth-notes placeholder="e.g. Need sponsorship after EP expiry">${esc(notes)}</textarea></label></div>`;
}

// Resume Data was its own section; old links (#settings/resume-data) open the merged Resumes screen.
const SETTINGS_ALIASES = { 'resume-data': 'resumes' };
function settingsSection(value) { value = SETTINGS_ALIASES[value] || value; return SETTINGS_SECTIONS.some(([id]) => id === value) ? value : 'profile'; }
function settingsForm() { return S.settingsSection === 'api-keys' ? 'keys' : S.settingsSection; }
function settingsBaseline(form) {
  const p = D.profile || {}, sp = p.search_preferences || {}, st = D.settings || {}, ai = st.ai?.saved || {};
  if (form === 'profile') return Object.fromEntries(PROFILE_FIELDS.map(k => [k, p[k] ?? '']));
  if (form === 'resumes') return { master_resume: p.master_resume || '' };
  if (form === 'preferences') return { weekly_target: D.prefs?.weekly_target, followup_days: D.prefs?.followup_days, ...Object.fromEntries(PREFERENCE_TOP.map(k => [k, p[k] ?? (k === 'target_roles' || k === 'preferred_locations' ? [] : '')])), ...Object.fromEntries(PREFERENCE_NESTED.map(k => [k, sp[k] ?? (k.startsWith('open_') ? false : ['total_experience', 'notice_period'].includes(k) ? '' : [])])) };
  if (form === 'ai') return { llm_provider: ai.llm_provider || '', model: ai.model || '', require_free: ai.require_free == null ? '' : String(ai.require_free), ...Object.fromEntries(['rank', 'tailor', 'review', 'followup'].map(k => [`stage_${k}`, ai.stage_models?.[k] || ''])) };
  if (form === 'workers') {
    return { application_mode: st.application_mode, min_review_score: st.min_review_score, execution_mode: st.execution_mode || 'backend', ranking_concurrency: st.ranking_concurrency ?? st.processing_maximums?.ranking_concurrency, tailoring_concurrency: st.tailoring_concurrency ?? st.processing_maximums?.tailoring_concurrency,
      ...tailorLengthBaseline(st.tailoring_length),
      ranking_interval_minutes: st.ranking_interval_minutes ?? 0, rate_limit_backoff: st.rate_limits?.enabled ?? true, rate_limit_base_seconds: st.rate_limits?.base_seconds ?? 30, rate_limit_multiplier: st.rate_limits?.multiplier ?? 2, rate_limit_max_seconds: st.rate_limits?.max_seconds ?? 900, auto_tailor: !!st.auto_tailor?.enabled, auto_tailor_min_fit_score: st.auto_tailor?.min_fit_score ?? 0,
      ...wkSettingsBaseline() };
  }
  if (form === 'domain') return Object.fromEntries(DOMAIN_FIELDS.map(k => [k, D.domain?.[k] ?? (k === 'enabled' || k === 'learn_from_feedback' ? true : [])]));
  return {};
}
function sameSetting(a, b) {
  if (Array.isArray(a) || Array.isArray(b)) return JSON.stringify(a) === JSON.stringify(b);
  return String(a ?? '') === String(b ?? '');
}
function settingsChanges(form) {
  const baseline = settingsBaseline(form);
  return Object.fromEntries(Object.entries(S.forms[form] || {}).filter(([k, v]) => !sameSetting(v, baseline[k])));
}
function settingsPendingTags(form) {
  return [...document.querySelectorAll(`input[data-tag][data-form="${form}"]`)].filter(el => el.value.trim());
}
function settingsDirty(form) {
  if (form === 'resume-data') return resumeDataDirty();
  return Object.keys(settingsChanges(form)).length > 0 || settingsPendingTags(form).length > 0;
}
function settingsAnyDirty() { return SETTINGS_FORMS.some(settingsDirty); }
function settingsSaving() { return SETTINGS_FORMS.some(f => isBusy(`settings-${f}`)); }
function settingsPayload(form) {
  const changes = settingsChanges(form);
  if (form === 'preferences') {
    const search_preferences = Object.fromEntries(Object.entries(changes).filter(([k]) => PREFERENCE_NESTED.includes(k)));
    return { ...Object.fromEntries(Object.entries(changes).filter(([k]) => PREFERENCE_TOP.includes(k))), ...(Object.keys(search_preferences).length ? { search_preferences } : {}) };
  }
  if (form === 'ai') {
    const body = {}, stages = {};
    for (const [k, v] of Object.entries(changes)) {
      if (k.startsWith('stage_')) stages[k.slice(6)] = v.trim();
      else body[k] = k === 'require_free' ? (v === '' ? null : v === 'true') : v.trim();
    }
    return { ...body, ...(Object.keys(stages).length ? { stage_models: stages } : {}) };
  }
  return changes;
}
function profileCompleteness() {
  const p = { ...D.profile, ...S.forms.profile }, sp = { ...D.profile?.search_preferences, ...S.forms.preferences };
  const values = [p.name, p.headline, p.email, p.phone, p.location, sp.total_experience, sp.notice_period,
    fv('preferences', 'compensation_notes', p.compensation_notes), fv('preferences', 'target_roles', p.target_roles), sp.primary_skills,
    fv('preferences', 'work_authorization', p.work_authorization), fv('resumes', 'master_resume', p.master_resume)];
  return Math.round(100 * values.filter(v => Array.isArray(v) ? v.length : v !== null && v !== undefined && String(v).trim() !== '').length / values.length);
}
function settingsClearSecrets() {
  delete S.forms.keys; delete S.forms.account; delete S.forms.applyingLogin; editingKey = null;
  delete SETTING_ERRORS.keys; delete SETTING_ERRORS.account;
}
// Settings forms that live on other screens: Running workers → Settings and AI Orchestration → Models & routing.
const SCREEN_FORMS = { workers: 'workers', orchestration: 'ai' };
const screenFormDirty = () => { const f = SCREEN_FORMS[S.screen]; return !!f && !!S.forms[f] && settingsDirty(f); };
function settingsCanLeave(nextScreen, nextSection) {
  const own = SCREEN_FORMS[S.screen];
  if (own) {
    if (nextScreen === S.screen) return true;  // another tab of the same screen keeps the draft
    if (isBusy(`settings-${own}`)) { toast('Wait for this change to finish.'); return false; }
    if (screenFormDirty()) {
      if (!confirm(`Leave and discard your unsaved ${own === 'ai' ? 'model' : 'worker'} settings?`)) return false;
      delete S.forms[own]; delete SETTING_ERRORS[own];
    }
    return true;
  }
  if (S.screen !== 'settings') return true;
  if (settingsSaving()) { toast('Wait for this change to finish.'); return false; }
  if (nextScreen !== 'settings' && settingsAnyDirty()) {
    if (!confirm('Leave Settings and discard unsaved changes?')) return false;
    SETTINGS_FORMS.forEach(f => { delete S.forms[f]; delete SETTING_ERRORS[f]; });
    authorizationDraft = null; rdReset();
  }
  if (nextScreen !== 'settings' || nextSection !== S.settingsSection) settingsClearSecrets();
  return true;
}
function settingsNavigate(section) {
  const next = settingsSection(section);
  if (!settingsCanLeave('settings', next)) { render(); return; }
  S.settingsSection = next;
  history.pushState(null, '', `#settings/${next}`);
  render(); document.getElementById('settings-heading')?.focus();
}
function settingsTouched(form) {
  if (!(S.screen === 'settings' && SETTINGS_FORMS.includes(form)) && !(S.screen === 'workers' && form === 'workers')
      && !(S.screen === 'orchestration' && form === 'ai')) return;
  delete SETTINGS_SUCCESS[form];
  document.querySelectorAll('[data-settings-save]').forEach(el => { el.disabled = !settingsDirty(el.dataset.settingsSave) || isBusy(`settings-${el.dataset.settingsSave}`); });
  document.querySelectorAll('[data-settings-discard]').forEach(el => { el.disabled = !settingsDirty(el.dataset.settingsDiscard); });
  const status = document.getElementById('settings-save-status');
  if (status) status.textContent = settingsDirty(form) ? 'Unsaved changes' : 'All changes saved';
}
function settingsField(form, key, labelText, extra = '', help = '') {
  return `<label class="field" for="${form}-${key}"><span>${labelText}</span>${inputF(form, key, settingsBaseline(form)[key], extra)}${help ? `<span class="settings-help">${help}</span>` : ''}</label>`;
}
function settingsGroup(title, description, content) {
  return `<section class="settings-group"><div class="settings-group-title"><h3>${title}</h3>${description ? `<p>${description}</p>` : ''}</div>${content}</section>`;
}
function settingsDetails(id, title, body) {
  return `<details class="settings-details" data-settings-details="${id}" ${SETTING_DETAILS.has(id) ? 'open' : ''}><summary>${title}<span aria-hidden="true">+</span></summary><div class="settings-details-body">${body}</div></details>`;
}
function settingsFooter(form, text = 'Save changes') {
  return `<footer class="settings-footer"><span id="settings-save-status" role="status">${isBusy(`settings-${form}`) ? 'Saving…' : SETTINGS_SUCCESS[form] || (settingsDirty(form) ? 'Unsaved changes' : 'All changes saved')}</span><div class="row gap8"><button class="btn" data-act="settings-discard" data-form="${form}" data-settings-discard="${form}" ${settingsDirty(form) ? '' : 'disabled'}>Discard</button><button class="btn primary" data-act="settings-save" data-form="${form}" data-settings-save="${form}" ${settingsDirty(form) && !isBusy(`settings-${form}`) ? '' : 'disabled'}>${isBusy(`settings-${form}`) ? 'Saving…' : text}</button></div></footer>`;
}
function renderProfileSettings() {
  const p = D.profile;
  return `<div class="settings-identity"><div class="settings-avatar" aria-hidden="true">${esc(initials(p.name))}</div><div class="grow"><strong>${esc(p.name || 'Make it yours')}</strong><p>${esc(p.headline || 'Add your professional details to get started.')}</p></div><span class="settings-complete">${profileCompleteness()}% complete</span></div>` +
    settingsGroup('Personal details', 'Help employers put a name to your experience.', `<div class="settings-grid">${settingsField('profile', 'name', 'Full name', 'autocomplete="name" maxlength="200"')}${settingsField('profile', 'headline', 'Professional headline', 'maxlength="500"')}${settingsField('profile', 'email', 'Contact email', 'type="email" autocomplete="email"', 'For applications. This does not change your login email.')}${settingsField('profile', 'phone', 'Phone', 'type="tel" autocomplete="tel"')}${settingsField('profile', 'location', 'Current location', 'autocomplete="address-level2"')}</div>`) +
    settingsGroup('Around the web', 'Give your experience a little more context.', `<div class="settings-grid">${settingsField('profile', 'linkedin_url', 'LinkedIn', 'type="url" placeholder="https://linkedin.com/in/…"')}${settingsField('profile', 'github_url', 'GitHub', 'type="url" placeholder="https://github.com/…"')}</div>`) + settingsFooter('profile');
}
function settingsChips(keys) {
  return `<div class="settings-grid">${CHIP_FIELDS.filter(([k]) => keys.includes(k)).map(([k, title, hint, placeholder]) => `<div class="settings-chip-field"><label for="preferences-${k}">${title}</label><p id="help-${k}" class="settings-help">${hint}</p>${tagBox('preferences', k, settingsBaseline('preferences')[k], placeholder, k === 'target_roles' ? 5 : 25)}</div>`).join('')}</div>`;
}
function renderPreferenceSettings() {
  const sp = settingsBaseline('preferences');
  return settingsGroup('Your next role', 'These preferences guide discovery and ranking.', settingsChips(['target_roles', 'preferred_locations', 'countries'])) +
    settingsGroup('Skills & interests', '', settingsChips(['primary_skills', 'secondary_skills', 'industries', 'exclude_companies', 'languages'])) +
    settingsGroup('Availability & expectations', '', `<div class="settings-grid">${settingsField('preferences', 'total_experience', 'Experience (years)', 'type="number" min="0" step="0.5"')}${settingsField('preferences', 'notice_period', 'Notice period (days)', 'type="number" min="0" step="1"', 'Use 0 if you are available immediately.')}${settingsField('preferences', 'compensation_notes', 'Expected compensation', 'placeholder="e.g. INR 75 lakh / year"', 'Include currency and pay period.')}</div>`) +
    settingsGroup('Your rhythm', 'Set realistic targets and a helpful follow-up cadence.', `<div class="settings-grid">${settingsField('preferences', 'weekly_target', 'Weekly application target', 'type="number" min="1" max="50" required')}${settingsField('preferences', 'followup_days', 'Follow-up after (days)', 'type="number" min="1" max="60" required')}</div><p class="settings-help">Follow-up drafts are never sent automatically.</p>`) +
    settingsGroup('Work arrangements', '', `<div class="field"><span>Work authorization by country</span>${authorizationEditor(sp.work_authorization)}</div><div class="settings-checks">${checkF('preferences', 'open_to_relocation', sp.open_to_relocation, 'Open to relocation')}${checkF('preferences', 'open_to_hybrid', sp.open_to_hybrid, 'Open to hybrid')}${checkF('preferences', 'open_to_contract', sp.open_to_contract, 'Open to contract')}</div>`) + settingsFooter('preferences');
}
function renderResumeSettings() { return renderResumesScreen(); }
// Below the resumes: a resume added by pasting its text, and evidence extraction. Rendered by renderResumesScreen.
function resumeTextTools() {
  return settingsDetails('resume-text', 'Add a resume from text', `<p class="settings-help">Saving creates a new resume from this text. It does not change the active resume: make it active when you want it used. To correct a resume’s facts, edit its data above instead. The text starts as the active resume’s.</p><label class="field" for="resumes-master_resume">Resume text${textareaF('resumes', 'master_resume', D.profile.master_resume)}</label>${settingsDetails('resume-format', 'Formatting tips', '<p>Use Summary, Skills, and Experience headings, a role heading for each position, and short bullet points. Include title, company, location and dates for each role.</p>')}${settingsFooter('resumes', 'Save as new resume')}`) +
    settingsGroup('Evidence for your applications', 'Extract claims from the active resume, then verify them in the Knowledge bank.', btn('Re-parse resume into evidence', 'parse-evidence', {}, '', isBusy('settings-resumes')));
}
function renderAccountSettings() {
  return settingsGroup('Signed-in account', 'Your login is separate from the contact email on your profile.', `<div class="settings-account-row"><div><span class="settings-help">Login email</span><strong>${esc(BOOT.user.email)}</strong></div>${btn('Sign out', 'logout', {}, '')}</div>`) +
    settingsGroup('Change password', 'You’ll stay signed in here. Your other sessions will be signed out.', `<div class="settings-password-fields">${settingsField('account', 'current_password', 'Current password', 'type="password" autocomplete="current-password" maxlength="256" required')}${settingsField('account', 'new_password', 'New password', 'type="password" autocomplete="new-password" minlength="10" maxlength="256" required', 'Use 10–256 characters. Choose a password you do not use elsewhere.')}${settingsField('account', 'confirm_password', 'Confirm new password', 'type="password" autocomplete="new-password" maxlength="256" required')}</div>`) + settingsFooter('account', 'Update password');
}
function renderKeySettings() {
  const st = D.settings;
  return `<p class="settings-intro">Keys are encrypted and used only for your workspace. Saved keys are never shown again.</p>${st.secret_storage ? '' : '<div class="settings-notice">Key storage is unavailable. Ask your server administrator to enable encrypted key storage.</div>'}<div class="settings-providers">${API_KEYS.map(([id, name, purpose]) => {
    const key = st.keys[id] || { configured: false, hint: '' };
    return `<section class="settings-provider"><div class="settings-provider-row"><div class="settings-provider-mark" aria-hidden="true">${name.slice(0, 1)}</div><div class="grow"><h3>${name}</h3><p>${purpose}</p></div><span class="settings-key-status ${key.configured ? 'saved' : ''}">${key.configured ? `Saved ${esc(key.hint)}` : 'Not configured'}</span>${btn(editingKey === id ? 'Cancel' : key.configured ? 'Replace' : 'Add key', 'settings-key-edit', { id }, 'sm', !st.secret_storage)}</div>${editingKey === id ? `<div class="settings-key-editor"><label class="field" for="keys-${id}">${name} API key${inputF('keys', id, '', 'type="password" autocomplete="off" spellcheck="false" maxlength="500" required')}</label><p class="settings-help">Saving replaces only this provider’s key.</p><div class="row gap8">${btn('Save key', 'settings-save', { form: 'keys', id }, 'primary', isBusy('settings-keys'))}${key.configured ? btn('Remove saved key', 'key-remove', { id }, '') : ''}</div></div>` : ''}</section>`;
  }).join('')}</div>`;
}
function renderAISettings() {
  const ai = D.settings.ai, base = settingsBaseline('ai'), provider = fv('ai', 'llm_provider', base.llm_provider) || ai.env.llm_provider;
  const defaultModel = String(fv('ai', 'model', base.model)).trim() || ai.env.models[provider] || '';
  const stages = ['rank', 'tailor', 'review', 'followup'].map(k => {
    const fallback = provider === 'mock' ? 'Mock (no API calls)' : (provider === 'openrouter' && !String(fv('ai', 'model', base.model)).trim() && ai.env.stage_models[k]) || defaultModel;
    return settingsField('ai', `stage_${k}`, { rank: 'Ranking', tailor: 'Tailoring', review: 'Review', followup: 'Follow-up' }[k], `placeholder="${attr(fallback)}" ${provider === 'mock' ? 'disabled' : ''}`, `Default: ${esc(fallback || 'Not set')}`);
  }).join('');
  return settingsGroup('Model defaults', 'Leave an override blank to use the server default.', `<div class="settings-grid"><label class="field" for="ai-llm_provider">Provider${selectF('ai', 'llm_provider', base.llm_provider, [['', `Server default (${ai.env.llm_provider})`], ['openrouter', 'OpenRouter'], ['openai', 'OpenAI'], ['anthropic', 'Anthropic'], ['gemini', 'Gemini'], ['mock', 'Mock / testing']], 'data-live="1"')}</label>${settingsField('ai', 'model', 'Default model', `placeholder="${attr(ai.env.models[provider] || '')}" data-live="1"`)}</div><p class="settings-help">${provider === 'mock' ? 'Mock mode makes no provider calls.' : `${esc(provider)}: ${D.settings.configured[provider] ? 'API key saved' : 'API key needed'}.`} <a href="#settings/api-keys">Manage API keys</a></p>`) +
    settingsDetails('ai-stages', 'Advanced · models per stage', `<div class="settings-grid">${stages}</div>`) +
    settingsGroup('Cost control', 'The free-only guard applies to OpenRouter.', `<label class="field" for="ai-require_free">Free-only guard${selectF('ai', 'require_free', base.require_free, [['', `Server default (${ai.env.require_free ? 'on' : 'off'})`], ['true', 'On — free models only'], ['false', 'Off — allow paid models']])}</label>`) + settingsFooter('ai');
}
// The first few waits, worked out from what is in the form rather than from what was last saved, so the
// example moves as you type.
const settingsSeconds = s => s == null ? '—' : s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`;
function backoffLadder(b, steps = 4, form = 'workers') {
  const base = Number(fv(form, 'rate_limit_base_seconds', b.rate_limit_base_seconds)) || 30;
  const factor = Number(fv(form, 'rate_limit_multiplier', b.rate_limit_multiplier)) || 2;
  const cap = Number(fv(form, 'rate_limit_max_seconds', b.rate_limit_max_seconds)) || 900;
  return [...Array(steps)].map((_, i) => settingsSeconds(Math.min(cap, base * factor ** i)));
}
// Approvals and who tailors: shown on Running workers → Settings (the Approvals card and the Tailoring card).
function renderApprovalSettings(b) {
  const mode = fv('workers', 'application_mode', b.application_mode);
  return `<div class="settings-options">${[['manual_approval', 'Review every packet', 'Approve each tailored packet before it reaches your apply kit.'], ['automatic', 'Auto-approve reviewed packets', 'Packets that meet your minimum score can skip the approval queue.']].map(([id, title, desc]) => `<label class="settings-option ${mode === id ? 'selected' : ''}"><input type="radio" name="settings-mode" data-form="workers" data-field="application_mode" value="${id}" ${mode === id ? 'checked' : ''}><span><strong>${title}</strong><small>${desc}</small></span></label>`).join('')}</div><p class="settings-help">Submitting applications always stays your action.</p>${settingsField('workers', 'min_review_score', 'Minimum reviewer score', 'type="number" min="0" max="100" required')}`;
}
function renderExecutionSettings(b) {
  const execution = fv('workers', 'execution_mode', b.execution_mode);
  return `<div class="settings-options">${[['backend', 'Job Hunt OS workers', 'Your configured model API tailors and reviews in the background.'], ['claude_desktop', 'Claude Desktop', 'You tailor and review in bulk from Claude Desktop over MCP; the workers make no model calls for it.']].map(([id, title, desc]) => `<label class="settings-option ${execution === id ? 'selected' : ''}"><input type="radio" name="settings-execution" data-form="workers" data-field="execution_mode" data-rerender value="${id}" ${execution === id ? 'checked' : ''}><span><strong>${title}</strong><small>${desc}</small></span></label>`).join('')}</div><p class="settings-help">Every saved result is validated by Job Hunt OS in both modes. See the README section "Claude Desktop + MCP" to connect.</p>`;
}
// Tailored resume length (app.services.resume_length): a page target and bullets per role, newest role first.
const TAILOR_PAGES = [[1, '1 page', 'The most focused: recent roles only get detail.'], [2, '2 pages', 'Recommended for most experienced candidates.'], [3, '3 pages', 'Only for long careers where every role matters: most recruiters read one or two pages.']];
const ORDINALS = ['Most recent role', '2nd role', '3rd role', '4th role', '5th role', '6th role', '7th role', '8th role', '9th role', '10th role', '11th role', '12th role'];
function tailorLengthBaseline(length) {
  const l = length || { pages: 2, budgets: {} };
  return { tailor_pages: String(l.pages || 2), ...Object.fromEntries(['1', '2', '3'].map(p => [`tailor_budget_${p}`, (l.budgets?.[p] || []).join(', ')])) };
}
function parseBudget(text) {
  const values = String(text || '').split(/[\s,]+/).filter(Boolean).map(Number);
  if (!values.length || values.length > 12 || values.some(v => !Number.isInteger(v) || v < 0 || v > 8)) throw new Error('Bullets per role: up to 12 whole numbers from 0 to 8, newest role first (for example 6, 5, 4, 3).');
  return values;
}
function renderTailorLengthSettings(b) {
  const pages = String(fv('workers', 'tailor_pages', b.tailor_pages)), presets = D.settings.tailoring_length?.presets || {};
  const field = `tailor_budget_${pages}`, text = fv('workers', field, b[field]);
  let budget = [];
  try { budget = parseBudget(text); } catch { budget = []; }
  const rows = budget.map((n, i) => `<span class="pill ${n ? 'plain' : 'muted'}">${i === budget.length - 1 && budget.length < 12 ? `${esc(ORDINALS[i])} and older` : esc(ORDINALS[i])}: ${n ? `${n} bullet${n === 1 ? '' : 's'}` : 'heading only'}</span>`).join('');
  return settingsGroup('Tailored resume length', 'How long tailored resumes are. Your resume data is the foundation: tailoring picks the bullets that fit each job, up to each role’s budget, with the most detail on your latest roles. You can pick another length for one job when you tailor it.',
    `<div class="settings-options">${TAILOR_PAGES.map(([id, title, desc]) => `<label class="settings-option ${pages === String(id) ? 'selected' : ''}"><input type="radio" name="settings-tailor-pages" data-form="workers" data-field="tailor_pages" data-rerender value="${id}" ${pages === String(id) ? 'checked' : ''}><span><strong>${title}${id === 3 ? ' <span class="pill warn">Not recommended</span>' : ''}</strong><small>${desc}</small></span></label>`).join('')}</div>
    <div class="settings-grid mt12">${settingsField('workers', field, `Bullets per role for ${pages === '1' ? '1 page' : `${pages} pages`}, newest role first`, `placeholder="${attr((presets[pages] || []).join(', '))}" data-rerender`, 'The last number repeats for every older role. 0 keeps a role’s title, employer and dates with no bullets.')}</div>
    <div class="row wrap gap6 mt8" data-tailor-budget>${rows}</div>
    <div class="mt8">${btn('Reset to preset', 'tailor-budget-reset', { pages }, 'sm ghost')}</div>`);
}
// Automatic tailoring (app.services.auto_tailor): ranking queues the jobs it qualifies; the tailor worker runs them.
function renderAutoTailorSettings(b, execution) {
  const desktop = execution === 'claude_desktop', on = !desktop && !!fv('workers', 'auto_tailor', b.auto_tailor);
  const slots = fv('workers', 'tailoring_concurrency', b.tailoring_concurrency);
  const minimum = Number(fv('workers', 'auto_tailor_min_fit_score', b.auto_tailor_min_fit_score)) || 0;
  return settingsGroup('Automatic tailoring', 'What happens when ranking qualifies a job.', `<label class="check mb10"><input type="checkbox" data-form="workers" data-field="auto_tailor" data-rerender ${on ? 'checked' : ''} ${desktop ? 'disabled' : ''}>Tailor qualified jobs automatically after ranking</label>
    ${desktop ? '<p class="settings-help">Automatic tailoring only runs on Job Hunt OS workers. In Claude Desktop mode you tailor from Claude Desktop.</p>'
    : on ? `<div class="settings-grid">${settingsField('workers', 'auto_tailor_min_fit_score', 'Only when fit score is at least', 'type="number" min="0" max="100" required', '0 tailors every qualified job.')}</div>
      <p class="settings-help">${minimum ? `A job the ranking qualifies with a fit score of ${minimum} or more` : 'Every job the ranking qualifies'} goes into the tailoring queue. It starts at once when a tailoring slot is free (up to ${esc(slots)} at a time), otherwise it waits and starts as soon as one frees up. Jobs already waiting in the Tailor queue are not included, and a job whose tailoring you cancel is never queued again.</p>`
    : '<p class="settings-help">Off: qualified jobs wait in the Tailor queue until you start them.</p>'}`);
}
/* Domain filter (app.services.domain_match, domain_classifier). Hidden jobs are in Discovery → Not matching. */
const LEARNED_ACTIONS = { active: [['disable', 'Disable'], ['promote', 'Always exclude'], ['delete', 'Forget']], disabled: [['enable', 'Enable'], ['delete', 'Forget']] };
function renderDomainSettings() {
  const d = D.domain;
  if (d === undefined) { api('/api/settings/domain').then(v => { D.domain = v; render(); }).catch(err => { D.domain = null; SETTING_ERRORS.domain = err.message; render(); }); return loading(); }
  if (!d) return '<p class="settings-help">The domain filter could not be loaded.</p>';
  const chipList = list => list.length ? `<div class="row wrap gap6">${list.map(t => pill(t, 'plain')).join('')}</div>` : '<span class="settings-help">Nothing yet.</span>';
  const learned = (d.learned || []).filter(e => !(e.user_disabled && e.status === 'disabled' && e.forgotten));
  const rules = learned.length ? learned.map(e => `<div class="lrow" style="align-items:flex-start"><div class="grow"><div class="row gap6 wrap"><span class="small b">${esc(e.term)}</span>${pill(e.kind === 'company' ? 'Company' : 'Title', 'plain')}${pill(e.status === 'active' ? (d.classifier_mode === 'on' && e.kind !== 'company' ? 'Asks AI' : 'Active') : 'Off', e.status === 'active' ? 'warn' : 'muted')}</div>
      <div class="xs muted mt4">Dismissed ${e.evidence}×${e.positive ? ` · kept ${e.positive}×` : ''}${e.examples?.length ? ` · e.g. ${esc(e.examples.join(' · '))}` : ''}</div></div>
      <div class="row gap6">${(LEARNED_ACTIONS[e.status] || []).map(([action, text]) => btn(text, 'domain-learned', { term: e.term, kind: e.kind, action }, 'sm')).join('')}</div></div>`).join('')
    : `<p class="settings-help">Nothing learned yet. After you dismiss, reject or delete several similar jobs, their common title words (or company) appear here and ${d.classifier_mode === 'on' ? 'similar new jobs get the AI check' : 'similar new jobs are hidden'}.</p>`;
  return `${settingsGroup('Filter', d.classifier_mode === 'on' ? 'Rules hide only what is unambiguous. A fast AI check reads the rest before anything expensive runs, and hides a job only when its evidence holds. Hidden jobs are listed in Discovery → table → “Not matching”, where you can keep one.' : 'Discovery hides jobs outside your domain before any description is fetched or any model is used. Hidden jobs are listed in Discovery → table → “Not matching”, where you can keep one.',
    `<div class="col gap10">${checkF('domain', 'enabled', d.enabled, 'Hide jobs outside my domain at discovery')}${checkF('domain', 'learn_from_feedback', d.learn_from_feedback, 'Learn from the jobs I dismiss, reject or delete')}
      ${d.server_enabled ? '' : '<p class="settings-help">The server has the domain filter turned off (DOMAIN_FILTER_ENABLED=false).</p>'}</div>`)}
  ${settingsGroup('Outside my domain', 'A job whose title names one of these is hidden — unless it also names one of your include terms.', tagBox('domain', 'exclude_terms', fv('domain', 'exclude_terms', d.exclude_terms), 'Add an area, e.g. sales…', 200))}
  ${settingsGroup('Other skill sets', 'Hidden when the title names one of these and none of your own skills (so “Python/Java developer” stays).', tagBox('domain', 'skill_excludes', fv('domain', 'skill_excludes', d.skill_excludes), 'Add a skill, e.g. PHP…', 200))}
  ${settingsGroup('Always keep', 'A title with any of these is always kept.', tagBox('domain', 'include_terms', fv('domain', 'include_terms', d.include_terms), 'Add a term, e.g. machine learning…', 200))}
  ${settingsGroup('From your profile', 'Read from your target roles, headline, resume and saved searches. A term used here is never excluded.', `<div class="col gap8"><div class="xs muted">Role words</div>${chipList(d.derived.role_words)}<div class="xs muted">Your skills</div>${chipList(d.derived.skills)}${d.overridden.length ? `<div class="xs muted">Excludes cancelled by your profile</div>${chipList(d.overridden)}` : ''}</div>`)}
  ${settingsGroup('Learned from your choices', 'Deterministic counts of what you dismissed; keeping a job pushes back.', `<div class="col">${rules}</div>`)}
  ${settingsGroup('Existing jobs', 'After changing the lists, check the jobs that are not ranked yet again. Jobs you kept are never touched.', btn(label('domainReapply', 'Apply to existing jobs', 'Checking…'), 'domain-reapply', {}, 'soft', isBusy('domainReapply')))}
  ${d.classifier_mode === 'on' ? settingsGroup('Re-check hidden jobs', 'Puts jobs a rule hid through the AI check, which uses your model for each one. Jobs you kept, jobs in a place your search did not cover and jobs the AI check hid stay as they are.', btn(label('domainRecheck', 'Re-check hidden jobs', 'Queuing…'), 'domain-recheck', {}, 'soft', isBusy('domainRecheck'))) : ''}
  ${settingsFooter('domain')}`;
}
// Auto submit (app.services.submission) and ATS logins (app.services.ats_auth). Passwords are write-only.
function renderApplyingSettings() {
  const a = D.applying;
  if (a === undefined) {
    Promise.all([api('/api/settings/guarded-application'), api('/api/settings/ats-credentials'), api('/api/application-routes')])
      .then(([consent, logins, routes]) => { D.applying = { consent, logins, routes: routes.routes }; render(); })
      .catch(err => { D.applying = { error: err.message }; render(); });
    return loading();
  }
  if (a.error) return `<p class="settings-help">${esc(a.error)}</p>`;
  const c = a.consent, on = c.server_mode === 'guarded_auto';
  const consent = settingsGroup('Auto submit', 'When this is on, every job you approve is applied to for you: the application worker fills the form from your verified answers, attaches your approved resume and submits it, on any site the browser can complete. It records Applied only when the site confirms. It stops and opens the browser window for you only when something needs you — a question with no saved answer, a login, a code or CAPTCHA, or a result it cannot confirm.',
    `<div class="row wrap gap10">${pill(c.enabled ? 'On for your account' : 'Off for your account', c.enabled ? 'good' : 'muted')}${on ? '' : pill(`Server: ${c.server_mode} (auto submit needs APPLICATION_EXECUTION_MODE=guarded_auto)`, 'warn')}</div>
    <label class="field mt12" for="applying-attest">Attestations you approve, one per line (exact wording, as the form shows it)<textarea class="input" id="applying-attest" rows="4" data-applying="attestations">${esc((c.attestations || []).join('\n'))}</textarea></label>
    <p class="settings-help">A form that asks you to certify, agree or consent to anything not listed here waits for you to tick it in the browser window.</p>
    <div class="row gap8 mt8">${btn(c.enabled ? 'Turn off' : 'Turn on', 'applying-consent', { on: c.enabled ? '' : '1' }, c.enabled ? '' : 'primary', isBusy('applying'))}${btn('Save attestations', 'applying-consent', { on: c.enabled ? '1' : '' }, 'ghost', isBusy('applying'))}</div>`);
  const logins = settingsGroup('Saved ATS logins', 'A login is used only on its own site, and only where the server allows logins to be filled. Job Hunt OS never shows a saved password again.',
    `${(a.logins.credentials || []).map(l => `<div class="row gap8 mt6"><span class="small mono grow">${esc(l.domain)}</span><span class="small">${esc(l.username)}</span>${pill(l.status === 'ACTIVE' ? (l.login_allowed ? 'Used' : 'Saved (not allowed here)') : 'Pending signup', l.status === 'ACTIVE' && l.login_allowed ? 'good' : 'muted')}${btn('Remove', 'applying-remove-login', { domain: l.domain }, 'sm ghost')}</div>`).join('') || '<p class="settings-help">No saved logins.</p>'}
    ${(a.logins.sessions || []).filter(x => x.active).map(x => `<div class="row gap8 mt6"><span class="xs muted grow">Signed-in session · ${esc(x.domain)} · until ${esc(fmtDay(x.expires_at))}</span>${btn('Sign out', 'applying-revoke-session', { domain: x.domain }, 'sm ghost')}</div>`).join('')}
    <div class="grid-auto mt12" style="grid-template-columns:1fr 1fr 1fr;gap:8px">${inputF('applyingLogin', 'domain', '', 'placeholder="acme.wd5.myworkdayjobs.com"')}${inputF('applyingLogin', 'username', '', 'placeholder="Email or username" autocomplete="off"')}${inputF('applyingLogin', 'password', '', 'type="password" placeholder="Password" autocomplete="new-password"')}</div>
    ${btn('Save login', 'applying-save-login', {}, 'sm mt8', isBusy('applying'))}`);
  const routes = settingsGroup('Application routes', 'Where your approved applications go, read from each posting and each run. LinkedIn, Indeed and Ashby applications are never automated.',
    `${(a.routes || []).map(r => `<div class="row gap8 mt6 wrap"><span class="small b">${esc(r.label)}</span><span class="small mono">${esc(r.route)}</span><span class="xs muted grow">${esc(r.company || '')}${r.source_board ? ` · via ${esc(r.source_board)}` : ''}</span>${r.auth_required !== 'none' ? pill(r.has_session ? 'signed in' : r.has_credential ? 'login saved' : `needs ${r.auth_required}`, r.has_session || r.has_credential ? 'good' : 'warn') : ''}${pill(r.auto_submit ? 'auto submit' : 'you apply', r.auto_submit ? 'good' : 'muted')}${r.last_outcome ? `<span class="xs muted">${esc(r.last_outcome)}</span>` : ''}</div>`).join('') || '<p class="settings-help">No approved applications yet.</p>'}`);
  return consent + logins + routes;
}
// Settings → AI agents & automation (app.services.server_settings.CONFIG): every option is a choice that says what it
// does. A preset fills the choices for a way of working; nothing is saved until Save. Only an admin may change them.
const AUTOMATION_GRAPH_MODES = ['qualification_graph_mode', 'preparation_graph_mode', 'job_insights_graph_mode', 'application_execution_mode'];
function automationValue(a, item) {
  const draft = S.forms.automation || {};
  return item.name in draft ? String(draft[item.name]) : item.value;
}
function automationOf(a, name) {
  const item = a.settings.find(s => s.name === name);
  return item ? automationValue(a, item) : '';
}
// A text setting shows its address field when its value is none of its choices, or once Custom address is picked.
const AUTOMATION_CUSTOM = '__custom__';
function automationCustom(item, v) { return !!S.automationCustom?.[item.name] || !item.choices.some(c => c.value === v); }
// A key field counts as changed only when something was typed in it: its saved value is never sent back.
function automationChanged(item, v) { return item.kind === 'secret' ? String(v).trim() !== '' : String(v) !== item.value; }
function automationChanges(a) {
  return Object.entries(S.forms.automation || {}).filter(([k, v]) => { const item = a.settings.find(s => s.name === k); return item && automationChanged(item, v); });
}
// What happens to an approved job with the choices as they stand (unsaved ones included), in one sentence.
function automationSummary(a) {
  const graphs = automationOf(a, 'agent_graphs_enabled') === 'true' && a.environment.postgres;
  const mode = graphs ? automationOf(a, 'application_execution_mode') : 'off';
  const visible = automationOf(a, 'browser_use_headless') === 'false';
  const browser = automationOf(a, 'browser_use_webui_enabled') === 'true' && a.environment.browser_use_python;
  return {
    off: ['You apply yourself', 'Approved jobs wait for you in the Apply kit; nothing runs on its own.'],
    observe: ['Forms are read, not filled', 'For each approved job the worker reads the form and lists the questions that still need an answer.'],
    assist: ['Forms are filled, you submit', `Each approved job’s form is filled${browser ? (visible ? ' in a visible browser window, left open for you to check and submit' : ' in a hidden browser; you submit on the site') : ' where a built-in adapter exists (Greenhouse); other sites wait for you'}.`],
    guarded_auto: ['Applications are submitted for you', `Approving a job submits it, for accounts with Auto submit on${browser ? '' : ' (Greenhouse only until browser-use is on)'}. It stops for you only when something needs you.`],
  }[mode] || ['You apply yourself', ''];
}
function automationWarnings(a, item, v) {
  const out = [];
  const graphsOn = automationOf(a, 'agent_graphs_enabled') === 'true';
  if (AUTOMATION_GRAPH_MODES.includes(item.name) && v !== 'off' && !graphsOn) out.push('Turn on LangGraph agent graphs above, or this stays off.');
  if ((AUTOMATION_GRAPH_MODES.includes(item.name) && v !== 'off' || item.name === 'agent_graphs_enabled' && v === 'true') && !a.environment.postgres) out.push('Needs PostgreSQL: this server uses another database, so it acts as off.');
  if (item.name === 'application_execution_mode' && v === 'guarded_auto') out.push('Each account also turns on Auto submit in Settings → Auto submit.');
  if (item.name === 'browser_use_webui_enabled' && v === 'true' && !a.environment.browser_use_python) out.push('Set BROWSER_USE_PYTHON in .env to the browser-use Web UI’s Python; until then it stays off.');
  if (item.name === 'agent_langsmith_export_enabled' && v === 'true' && !a.environment.langsmith_api_key && !String(automationOf(a, 'agent_langsmith_api_key')).trim()) out.push('Enter a LangSmith API key below (or set AGENT_LANGSMITH_API_KEY in .env); until then nothing is sent.');
  return out;
}
function automationControl(a, item, v) {
  const lock = a.editable ? '' : 'disabled';
  const id = `aria-describedby="am-help-${item.name}"`;
  if (item.kind === 'secret') {
    const hint = item.overridden ? 'Saved · paste a new key to replace it' : item.env_set ? 'Using the key in .env · paste one to override it' : 'Paste your API key';
    return inputF('automation', item.name, v, `type="password" autocomplete="new-password" spellcheck="false" placeholder="${attr(hint)}" ${id} ${lock}`);
  }
  // A text setting with choices (the LangSmith region): pick one, or type any other address under Custom.
  if (item.kind === 'text' && item.choices.length) {
    const custom = automationCustom(item, v);
    const options = [...item.choices.map(c => [c.value, c.label]), [AUTOMATION_CUSTOM, 'Custom address']];
    const select = `<select class="select" id="automation-${item.name}-choice" data-act-change="automation-choice" data-name="${attr(item.name)}" ${id} ${lock}>${options.map(([value, text]) => `<option value="${attr(value)}" ${value === (custom ? AUTOMATION_CUSTOM : v) ? 'selected' : ''}>${esc(text)}</option>`).join('')}</select>`;
    return custom ? `${select}<div class="mt6">${inputF('automation', item.name, v, `spellcheck="false" autocomplete="off" placeholder="https://" aria-label="${attr(`${item.label}: custom address`)}" ${lock}`)}</div>` : select;
  }
  if (item.choices.length) {
    const options = item.choices.map(c => [c.value, c.label]);
    if (!options.some(([value]) => value === v)) options.push([v, `${v} (custom)`]);
    return selectF('automation', item.name, v, options, `data-rerender ${id} ${lock}`);
  }
  const type = item.kind === 'int' ? `type="number" inputmode="numeric" min="${item.min}" max="${item.max}"` : 'spellcheck="false" autocomplete="off"';
  return inputF('automation', item.name, v, `${type} ${id} ${lock}`);
}
function automationSecretOrigin(a, item) {
  const env = item.env_set ? '.env has a key' : '.env has none';
  if (!item.overridden) return `<span class="am-origin">${item.env_set ? 'From .env' : 'Not set'}</span>`;
  return `<span class="am-origin am-origin-set">Saved here, encrypted · ${env}</span>${a.editable ? btn(item.env_set ? 'Use .env key' : 'Remove saved key', 'automation-clear-secret', { name: item.name }, 'am-link') : ''}`;
}
function automationRow(a, item) {
  const v = automationValue(a, item);
  const chosen = item.choices.find(c => c.value === v);
  const inactive = AUTOMATION_GRAPH_MODES.includes(item.name) && automationOf(a, 'agent_graphs_enabled') !== 'true';
  const control = item.kind === 'text' && item.choices.length ? `automation-${item.name}-choice` : `automation-${item.name}`;
  const envLabel = (item.choices.find(c => c.value === item.default) || {}).label || item.default || 'empty';
  const changed = v !== item.default;
  const origin = item.kind === 'secret' ? automationSecretOrigin(a, item) : changed
    ? `<span class="am-origin am-origin-set">Set here · .env says ${esc(envLabel)}</span>${a.editable ? btn('Use .env value', 'automation-reset', { name: item.name }, 'am-link') : ''}`
    : '<span class="am-origin">Same as .env</span>';
  const warnings = automationWarnings(a, item, v);
  return `<div class="am-row${inactive ? ' am-row-inactive' : ''}" data-automation="${item.name}">
    <div class="am-row-text"><label class="am-label" for="${control}">${esc(item.label)}</label>
      <p class="am-help" id="am-help-${item.name}">${esc(item.help)}</p>
      <div class="am-meta"><code>${esc(item.env)}</code>${origin}</div></div>
    <div class="am-row-control">${automationControl(a, item, v)}${chosen && chosen.help ? `<p class="am-choice">${esc(chosen.help)}</p>` : ''}
      ${warnings.map(w => `<p class="am-warn" role="note">${esc(w)}</p>`).join('')}</div></div>`;
}
function automationStatus(a) {
  const on = automationOf(a, 'agent_graphs_enabled') === 'true';
  const chip = (ok, yes, no, title) => `<span class="am-chip ${ok ? 'am-ok' : 'am-no'}" title="${attr(title)}"><span aria-hidden="true">${ok ? '●' : '○'}</span>${esc(ok ? yes : no)}</span>`;
  return `<div class="am-chips">${chip(on, 'LangGraph on', 'LangGraph off', 'AGENT_GRAPHS_ENABLED')}${chip(a.environment.postgres, 'PostgreSQL', 'Not PostgreSQL', 'Agent graphs need PostgreSQL')}${chip(a.environment.browser_use_ready, 'browser-use ready', a.environment.browser_use_python ? 'browser-use off' : 'BROWSER_USE_PYTHON not set', 'The browser-use runner')}${chip(a.environment.langsmith_api_key, 'LangSmith key set', 'No LangSmith key', 'The LangSmith API key: saved below or AGENT_LANGSMITH_API_KEY in .env')}</div>`;
}
function renderAutomationSettings() {
  const a = D.automation;
  if (a === undefined) {
    api('/api/settings/automation').then(v => { D.automation = v; render(); }).catch(err => { D.automation = { error: err.message }; render(); });
    return loading();
  }
  if (a.error) return `<p class="settings-help">${esc(a.error)}</p>`;
  const [headline, sentence] = automationSummary(a);
  const changes = automationChanges(a);
  const current = a.presets.find(p => Object.entries(p.values).every(([k, v]) => automationOf(a, k) === v));
  const summary = `<section class="am-summary" aria-live="polite"><div class="caps">${changes.length ? 'With your unsaved changes' : 'What happens now'}</div>
    <div class="am-summary-head">${esc(headline)}</div><p>${esc(sentence)}</p>${automationStatus(a)}
    ${a.editable ? '' : '<p class="am-readonly">Only an admin can change these server-wide settings.</p>'}</section>`;
  const presets = settingsGroup('Choose a way of working', 'This fills in the settings below. Adjust anything, then save.',
    `<div class="am-presets" role="radiogroup" aria-label="Way of working">${a.presets.map(p => {
      const selected = current && current.id === p.id;
      return `<button type="button" role="radio" aria-checked="${selected ? 'true' : 'false'}" class="am-preset${selected ? ' is-selected' : ''}" data-act="automation-preset" data-id="${attr(p.id)}" ${a.editable ? '' : 'disabled'}>
        <span class="am-radio" aria-hidden="true"></span><span class="am-preset-label">${esc(p.label)}</span><span class="am-preset-help">${esc(p.help)}</span></button>`;
    }).join('')}</div>${current ? '' : '<p class="am-custom">Custom: your settings don’t match a preset exactly.</p>'}`);
  const rows = id => a.settings.filter(s => s.group === id).map(s => automationRow(a, s)).join('');
  const main = a.groups.filter(g => g.id !== 'retention').map(g => settingsGroup(esc(g.label), esc(g.help), `<div class="am-rows">${rows(g.id)}</div>`)).join('');
  const advanced = a.groups.filter(g => g.id === 'retention').map(g => settingsDetails('automation-advanced', `Advanced: ${esc(g.label.toLowerCase())}`, `<p class="settings-help">${esc(g.help)}</p><div class="am-rows">${rows(g.id)}</div>`)).join('');
  const footer = a.editable ? `<footer class="settings-footer am-footer${changes.length ? ' is-dirty' : ''}"><span role="status">${isBusy('automation') ? 'Saving…' : changes.length ? `${changes.length} unsaved change${changes.length === 1 ? '' : 's'} · they apply as soon as you save, no restart` : 'Saved. These override .env for the whole server; a key saved here is encrypted and never shown again.'}</span>
    <div class="row gap8">${btn('Discard', 'automation-discard', {}, '', !changes.length)}${btn(label('automation', 'Save changes', 'Saving…'), 'automation-save', {}, 'primary', !changes.length || isBusy('automation'))}</div></footer>` : '';
  return `<div class="am">${summary}${presets}${main}${advanced}${footer}</div>`;
}
const SETTINGS_RENDERERS = { automation: renderAutomationSettings, applying: renderApplyingSettings, profile: renderProfileSettings, preferences: renderPreferenceSettings, resumes: renderResumeSettings, 'resume-data': () => renderResumeDataSettings(), account: renderAccountSettings, 'api-keys': renderKeySettings, domain: renderDomainSettings, gmail: () => renderInboxSettings() };
function renderSettings() {
  if (D.settingsLoadError) return `<div class="settings-content"><div class="settings-error" role="alert">Settings could not be loaded: ${esc(D.settingsLoadError)}</div>${btn('Try again', 'settings-retry', {}, 'mt16')}</div>`;
  if (!D.profile || !D.settings || !D.prefs) return loading();
  const section = settingsSection(S.settingsSection), form = section === 'api-keys' ? 'keys' : section;
  const forms = section === 'resumes' ? ['resumes', 'resume-data'] : [form], error = forms.map(f => SETTING_ERRORS[f]).find(Boolean);
  const [, title, description] = SETTINGS_SECTIONS.find(([id]) => id === section);
  return `<div class="settings-shell"><nav class="settings-nav" aria-label="Settings sections"><div class="settings-nav-caption">YOUR WORKSPACE</div>${SETTINGS_SECTIONS.map(([id, name, , number]) => `<a href="#settings/${id}" ${id === section ? 'aria-current="page"' : ''}><span class="settings-nav-number" aria-hidden="true">${number}</span>${name}</a>`).join('')}<p>Fine-tune your next chapter.</p></nav><div class="settings-content"><label class="settings-mobile-nav" for="settings-section">Settings section<select class="select" id="settings-section" data-act-change="settings-section">${SETTINGS_SECTIONS.map(([id, name]) => `<option value="${id}" ${id === section ? 'selected' : ''}>${name}</option>`).join('')}</select></label><header class="settings-section-heading"><span class="settings-eyebrow">SETTINGS / ${esc(title.toUpperCase())}</span><h2 id="settings-heading" tabindex="-1">${title}</h2><p>${description}</p></header><form id="settings-editor" novalidate><fieldset ${forms.some(f => isBusy(`settings-${f}`)) ? 'disabled' : ''}><div id="settings-error" class="settings-error" role="alert" ${error ? '' : 'hidden'}>${esc(error || '')}</div>${SETTINGS_RENDERERS[section]()}</fieldset></form></div></div>`;
}
function settingsValidate(form) {
  if (!document.getElementById('settings-editor')?.reportValidity()) return false;
  if (form === 'account') {
    const p = S.forms.account || {};
    if (p.new_password !== p.confirm_password) throw new Error('The new passwords do not match.');
    if (p.current_password === p.new_password) throw new Error('Choose a different new password.');
  }
  return true;
}
async function settingsRun(form, action, success) {
  const key = `settings-${form}`;
  if (isBusy(key)) return;
  S.busy[key] = true; delete SETTING_ERRORS[form]; delete SETTINGS_SUCCESS[form]; render();
  try { await action(); const text = typeof success === 'function' ? success() : success; if (text) { SETTINGS_SUCCESS[form] = text; toast(text); } guideRefresh(true); }  // a save may finish a setup quest
  catch (err) { SETTING_ERRORS[form] = err.message; }
  finally { delete S.busy[key]; render(); if (SETTING_ERRORS[form]) document.getElementById('settings-error')?.scrollIntoView?.({ block: 'nearest' }); }
}
async function refreshSettingsSummary(form) {
  try { D.summary = await api('/api/workspace/summary'); }
  catch (err) { SETTING_ERRORS[form] = `Changes saved, but the sidebar could not refresh: ${err.message}. Reload to refresh it.`; }
}
async function settingsSave(form) {
  if (form === 'resume-data') return resumeDataSave();
  if (form === 'resumes' && !rdLeaveDraft()) return;  // the new resume is shown next: never with the old one's draft
  settingsPendingTags(form).forEach(el => commitTag(el, false));
  try { if (!settingsValidate(form)) return; }
  catch (err) { SETTING_ERRORS[form] = err.message; render(); return; }
  if (form === 'workers' && fv(form, 'application_mode', D.settings.application_mode) === 'automatic' && D.settings.application_mode !== 'automatic' && !confirm('Auto-approval skips the manual queue once a packet passes review. Submitting stays your action. Continue?')) return;
  let saved = 'Changes saved';
  await settingsRun(form, async () => {
    if (form === 'preferences') {
      // Weekly target and follow-up wait are saved apart from the profile (PUT /api/preferences).
      const rhythm = Object.fromEntries(Object.entries(settingsChanges(form)).filter(([k]) => RHYTHM_FIELDS.includes(k)).map(([k, v]) => [k, Number(v)]));
      if (Object.keys(rhythm).length) D.prefs = await api('/api/preferences', { method: 'PUT', body: rhythm });
    }
    if (['profile', 'resumes'].includes(form) || (form === 'preferences' && Object.keys(settingsPayload(form)).length)) {  // not when only the rhythm changed
      const { created_resume_id: created, created_resume_version: version, created_resume_active: active, ...profile } = await api('/api/profile', { method: 'PATCH', body: settingsPayload(form) });
      D.profile = profile;
      // Saved text is a new resume version. It is used only once you make it active: the selection never moves by itself.
      if (created) { saved = active ? `Saved as resume v${version} and set as active` : `Saved as resume v${version}. It is not used until you make it active`; RD.viewId = created; }
    }
    if (form === 'ai') D.settings = await api('/api/settings', { method: 'PATCH', body: settingsPayload(form) });
    if (form === 'keys') {
      const value = String(S.forms.keys?.[editingKey] || '').trim();
      if (!value) throw new Error('Enter a key before saving.');
      D.settings = await api('/api/settings/keys', { method: 'PUT', body: { [editingKey]: value } }); editingKey = null;
    }
    if (form === 'account') await api('/api/auth/password', { method: 'POST', body: S.forms.account });
    if (form === 'workers') await saveWorkflowSettings(form);
    if (form === 'workers') { await wkSaveExtra(settingsChanges('workers')); await loadWorkers().catch(() => null); }  // the queues show the new limits
    if (form === 'domain') D.domain = await api('/api/settings/domain', { method: 'PUT', body: { ...settingsBaseline('domain'), ...settingsChanges('domain') } });
    delete S.forms[form];
    if (form === 'preferences') authorizationDraft = null;
    if (form === 'resumes') D.resumeData = undefined;
    if (['profile', 'preferences', 'ai'].includes(form)) await refreshSettingsSummary(form);
    if (form === 'ai' && typeof aimLoad === 'function') await aimLoad();  // the Route card shows the direct model
  }, () => form === 'account' ? 'Password updated. Other sessions are signed out.' : saved);
}
async function saveWorkflowSettings(form = 'workers') {
  const changes = settingsChanges(form);
  const numeric = ['min_review_score', 'ranking_concurrency', 'tailoring_concurrency', 'ranking_interval_minutes', 'rate_limit_base_seconds', 'rate_limit_multiplier', 'rate_limit_max_seconds', 'auto_tailor_min_fit_score'];
  const settingsKeys = ['application_mode', 'min_review_score', 'execution_mode', 'ranking_concurrency', 'tailoring_concurrency', 'ranking_interval_minutes', 'rate_limit_backoff', 'auto_tailor', ...numeric.slice(4)];
  const body = Object.fromEntries(Object.entries(changes).filter(([k]) => settingsKeys.includes(k)).map(([k, v]) => [k, numeric.includes(k) ? Number(v) : v]));
  // The default length of tailored resumes: the page target and any edited budget ("" restores the preset).
  const length = {};
  if ('tailor_pages' in changes) length.pages = Number(changes.tailor_pages);
  for (const p of ['1', '2', '3']) if (`tailor_budget_${p}` in changes) (length.budgets ||= {})[p] = String(changes[`tailor_budget_${p}`]).trim() ? parseBudget(changes[`tailor_budget_${p}`]) : [];
  if (Object.keys(length).length) body.tailoring_length = length;
  if (!Object.keys(body).length) return;
  D.settings = await api('/api/settings', { method: 'PATCH', body });
  await refreshSettingsSummary(form);
}
function settingsResumeGuard() {
  return !settingsDirty('resumes') || confirm('This action replaces your unsaved resume text. Continue?');
}
async function settingsUploadResume(file) {
  if (!rdLeaveDraft()) return;  // the uploaded resume is shown next: never with another one's open draft
  let active = false;
  await settingsRun('resumes', async () => {
    const form = new FormData(); form.append('file', file);
    const out = await api('/api/profile/resume', { method: 'POST', form });
    // An upload never changes the active resume (only your first one becomes active), so unsaved text stays.
    active = !!out.is_active; RD.viewId = out.resume_id ?? null;
    if (active) D.profile = await api('/api/profile');
    D.resumeData = await api(rdResumeUrl()); D.resumeChoices = null; RD.changes = null; RD.raw = null;
  }, () => active ? 'Resume uploaded and set as active. Review its data below'
    : 'Resume uploaded. Review its data below, and make it active when you want it used');
}
const SETTINGS_ACTIONS = {
  'automation-preset': el => {
    const preset = (D.automation?.presets || []).find(p => p.id === el.dataset.id);
    if (!preset) return;
    S.forms.automation = { ...(S.forms.automation || {}), ...preset.values };
    render();
  },
  'automation-discard': () => { delete S.forms.automation; delete S.automationCustom; render(); },
  'automation-reset': el => {
    const item = (D.automation?.settings || []).find(x => x.name === el.dataset.name);
    if (item) { S.forms.automation = { ...(S.forms.automation || {}), [item.name]: item.default }; if (S.automationCustom) delete S.automationCustom[item.name]; render(); }
  },
  // A region sets the address; Custom address keeps the current one in a field to edit.
  'automation-choice': el => {
    const name = el.dataset.name, custom = el.value === AUTOMATION_CUSTOM;
    S.automationCustom = { ...(S.automationCustom || {}), [name]: custom };
    if (!custom) S.forms.automation = { ...(S.forms.automation || {}), [name]: el.value };
    render();
  },
  // An empty value removes the saved key at once; the key itself is never in the page.
  'automation-clear-secret': el => busy('automation', async () => {
    D.automation = await api('/api/settings/automation', { method: 'PUT', body: { values: { [el.dataset.name]: '' } } });
    if (S.forms.automation) delete S.forms.automation[el.dataset.name];
  }, 'Saved key removed'),
  'automation-save': () => busy('automation', async () => {
    const byName = Object.fromEntries((D.automation?.settings || []).map(s => [s.name, s]));
    const values = Object.fromEntries(Object.entries(S.forms.automation || {}).filter(([k, v]) => byName[k] && automationChanged(byName[k], v)).map(([k, v]) => [k, byName[k].kind === 'secret' ? String(v).trim() : v]));
    D.automation = await api('/api/settings/automation', { method: 'PUT', body: { values } });
    delete S.forms.automation; delete S.automationCustom;
  }, 'Saved: in effect now'),
  'applying-consent': el => busy('applying', async () => {
    const text = document.getElementById('applying-attest')?.value || '';
    const lines = text.split('\n').map(x => x.trim()).filter(Boolean);
    D.applying.consent = { ...D.applying.consent, ...(await api('/api/settings/guarded-application', { method: 'PUT', body: { enabled: !!el.dataset.on, attestations: lines } })) };
  }, 'Saved'),
  'applying-save-login': () => busy('applying', async () => {
    const f = S.forms.applyingLogin || {};
    await api(`/api/settings/ats-credentials/${encodeURIComponent((f.domain || '').trim())}`, { method: 'PUT', body: { username: f.username || '', password: f.password || '' } });
    delete S.forms.applyingLogin; D.applying.logins = await api('/api/settings/ats-credentials');
  }, 'Login saved (encrypted)'),
  'applying-remove-login': el => busy('applying', async () => {
    await api(`/api/settings/ats-credentials/${encodeURIComponent(el.dataset.domain)}`, { method: 'DELETE' });
    D.applying.logins = await api('/api/settings/ats-credentials');
  }, 'Removed'),
  'applying-revoke-session': el => busy('applying', async () => {
    await api(`/api/settings/ats-sessions/${encodeURIComponent(el.dataset.domain)}`, { method: 'DELETE' });
    D.applying.logins = await api('/api/settings/ats-credentials');
  }, 'Signed out'),
  'tailor-budget-reset': el => { setF('workers', `tailor_budget_${el.dataset.pages}`, (D.settings.tailoring_length?.presets?.[el.dataset.pages] || []).join(', ')); render(); },
  'settings-retry': async () => { D.settingsLoadError = ''; render(); await load(); },
  'settings-section': el => settingsNavigate(el.value),
  'settings-save': el => settingsSave(el.dataset.form),
  'settings-discard': el => { const f = el.dataset.form; delete S.forms[f]; if (f === 'preferences') authorizationDraft = null; if (f === 'resume-data') rdReset(); delete SETTING_ERRORS[f]; delete SETTINGS_SUCCESS[f]; render(); },
  'auth-add': () => { settingsAuthUpdate(); authorizationDraft.rows.push({ country: '', status: '' }); render(); document.querySelector('[data-auth-row]:last-child [data-auth-country]')?.focus(); },
  'auth-remove': el => { settingsAuthUpdate(); authorizationDraft.rows.splice(Number(el.dataset.index), 1); render(); },
  'settings-key-edit': el => { delete S.forms.keys; delete SETTING_ERRORS.keys; editingKey = editingKey === el.dataset.id ? null : el.dataset.id; render(); document.getElementById(`keys-${editingKey}`)?.focus(); },
  'key-remove': el => {
    if (!confirm('Remove this saved key? Work needing it pauses until a replacement is added.')) return;
    return settingsRun('keys', async () => { D.settings = await api('/api/settings/keys', { method: 'PUT', body: { [el.dataset.id]: '' } }); delete S.forms.keys; editingKey = null; }, 'Key removed');
  },
  'parse-evidence': () => {
    if (settingsDirty('resumes')) { SETTING_ERRORS.resumes = 'Save your resume text before extracting evidence.'; render(); return; }
    return settingsRun('resumes', async () => { const out = await api('/api/profile/resume/parse-evidence', { method: 'POST' }); toast(`${out.added} unverified claims added to the Knowledge bank`); });
  },
  logout: () => {
    if (!settingsCanLeave('logout')) return;
    settingsClearSecrets();
    return busy('logout', async () => { await api('/api/auth/logout', { method: 'POST' }); window.location.href = '/login'; });
  },
};
