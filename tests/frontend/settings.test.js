import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// Execute the production scripts together, omitting only automatic network boot.
const settingsSource = readFileSync('app/static/settings.js', 'utf8');
const resumeDataSource = readFileSync('app/static/resume_data.js', 'utf8');
const jobDataSource = readFileSync('app/static/job_data.js', 'utf8');
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'wkState', 'setF', 'render', 'readHash', 'setHash', 'orchState', 'settingsNavigate', 'settingsChanges', 'settingsPayload', 'settingsDirty', 'settingsCanLeave', 'settingsSave', 'settingsUploadResume', 'profileCompleteness', 'addTags', 'commitTag', 'SETTING_ERRORS', 'SETTINGS_SUCCESS', 'SETTINGS_SECTIONS', 'SETTINGS_ACTIONS', 'RD', 'resumeDataDirty', 'rdReset', 'JD', 'renderJobStructured', 'jobDataInput', 'packetLength'];
let ui, fetchMock, context;
function response(data, status = 200) { return { ok: status < 400, status, text: async () => JSON.stringify(data) }; }
function data() {
  return { profile: { name: 'Alex Morgan', headline: 'Engineering leader', email: 'alex@example.com', phone: '123', location: 'Mumbai', linkedin_url: '', github_url: '', master_resume: 'My resume', target_roles: ['Lead'], preferred_locations: ['Mumbai, India'], work_authorization: 'Citizen', compensation_notes: 'INR 75 lakh', search_preferences: { total_experience: 0, notice_period: 0, primary_skills: ['Python'] } },
    summary: { profile: { name: 'Alex Morgan', headline: 'Engineering leader' }, counts: {}, model: 'default-model', llm_provider: 'openai', application_mode: 'manual_approval', auto_search: { schedule: 'daily', setups: 1, roles: 1, locations: 1, next_run: null, last_run: null } },
    resumes: [], prefs: { weekly_target: 10, followup_days: 7, warn_closing: false },
    settings: { secret_storage: true, ranking_concurrency: 5, tailoring_concurrency: 2, processing_maximums: { ranking_concurrency: 5, tailoring_concurrency: 2 }, rate_limits: { enabled: true, base_seconds: 30, multiplier: 2, max_seconds: 900, ladder: [30, 60, 120, 240, 480] }, keys: Object.fromEntries(['openai', 'openrouter', 'anthropic', 'apify'].map(k => [k, { configured: k === 'openai', hint: k === 'openai' ? '…1234' : '' }])), configured: { openai: true, mock: true }, application_mode: 'manual_approval', min_review_score: 70,
      ai: { saved: { llm_provider: '', model: '', require_free: null, stage_models: {} }, env: { llm_provider: 'openai', models: { openai: 'default-model' }, require_free: true, stage_models: {} } } } };
}
function section(id) { ui.S.settingsSection = id; ui.render(); }
// Approvals, tailoring and cadence live on Running workers → Settings (the `workers` form).
function workerSettings() {
  const field = (name, type, def) => ({ name, label: name, help: '', type, min: 1, max: 10, default: def });
  ui.D.wkSettings = { account: { tailoring: { values: { auto: true, max_attempts: 2 }, fields: [field('auto', 'bool', true), field('max_attempts', 'int', 2)] } },
    server: {}, server_editable: false, schedules: {} };
  ui.D.wk = { workers: [], cooldowns: [], active: 0, running: 0 };
  // Saving reloads the workers screen: answer it with what is on screen.
  fetchMock.mockImplementation(async url => response(url === '/api/workers' ? ui.D.wk : url === '/api/workers/settings' ? ui.D.wkSettings
    : url === '/api/workspace/summary' ? ui.D.summary : {}));
  ui.S.screen = 'workers'; ui.wkState().tab = 'settings'; ui.render();
}
function resumeCard(overrides = {}) {
  return { id: 1, name: 'cv.pdf', version: 1, is_active: true, verification_status: 'UNVERIFIED', reviewed_percent: 0, setups: [], domain: null, roles: [], has_original: true, file: { id: 11, filename: 'cv.pdf', size: 2048, media_type: 'application/pdf' }, ...overrides };
}
function resumesLoaded(versions = []) { ui.D.resumeData = { active: null, current: null, versions, policy: {} }; }
function input(id, value) {
  const el = document.getElementById(id); el.value = value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
}
beforeEach(() => {
  // New document event targets prevent event handlers accumulating between test instances.
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"login@example.com"},"max_upload_mb":10}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('scrollTo', vi.fn());
  fetchMock = vi.fn(async url => response(url === '/api/workspace/summary' ? ui.D.summary : {})); vi.stubGlobal('fetch', fetchMock);
  // Each isolated iframe-like jsdom document needs the same production event registration just once.
  if (!ui) {
    context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, setTimeout, clearTimeout, setInterval, clearInterval, console });
    runInContext(readFileSync('app/static/monitoring.js', 'utf8'), context, { filename: 'monitoring.js' });
    runInContext(readFileSync('app/static/orchestration.js', 'utf8'), context, { filename: 'orchestration.js' });
    runInContext(readFileSync('app/static/workers.js', 'utf8'), context, { filename: 'workers.js' });
    runInContext(settingsSource, context, { filename: resolve('app/static/settings.js') });
    runInContext(resumeDataSource, context, { filename: resolve('app/static/resume_data.js') });
    runInContext(jobDataSource, context, { filename: resolve('app/static/job_data.js') });
    runInContext(readFileSync('app/static/knowledge.js', 'utf8'), context, { filename: 'knowledge.js' });
    runInContext(readFileSync('app/static/inbox.js', 'utf8'), context, { filename: 'inbox.js' });
    runInContext(readFileSync('app/static/guide.js', 'utf8'), context, { filename: 'guide.js' });
    runInContext(readFileSync('app/static/job_insights.js', 'utf8'), context, { filename: 'job_insights.js' });
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock; context.confirm = confirm;
  ui.S.forms = {}; ui.S.busy = {}; ui.S.screen = 'settings'; ui.S.settingsSection = 'profile';
  Object.keys(ui.SETTING_ERRORS).forEach(k => delete ui.SETTING_ERRORS[k]);
  ui.rdReset(); ui.RD.viewId = null; ui.RD.tab = 'structured'; ui.RD.changes = null; ui.RD.raw = null; ui.RD.decisions = {}; ui.RD.loading = false;
  Object.assign(ui.D, data(), { settingsLoadError: '', resumeData: undefined }); ui.render();
});

describe('focused settings UI', () => {
  it('renders each section with labels and no unrelated editors', () => {
    for (const [id, title] of ui.SETTINGS_SECTIONS) {
      section(id);
      expect(document.querySelector('h2').textContent).toBe(title);
      expect(document.querySelectorAll('.settings-nav [aria-current]').length).toBe(1);
      for (const el of document.querySelectorAll('#settings-editor input:not([type="radio"]), #settings-editor textarea, #settings-editor select')) {
        expect(el.getAttribute('aria-label') || el.labels?.length).toBeTruthy();
      }
    }
    section('profile');
    expect(document.querySelector('#account-new_password')).toBeNull();
    expect(document.querySelector('#resumes-master_resume')).toBeNull();
  });
  it('supports default and deep-link routes', () => {
    history.replaceState(null, '', '#settings'); ui.readHash(); expect(ui.S.settingsSection).toBe('profile');
    history.replaceState(null, '', '#settings/api-keys'); ui.readHash(); expect(ui.S.settingsSection).toBe('api-keys');
    history.replaceState(null, '', '#settings/unknown'); ui.readHash(); expect(ui.S.settingsSection).toBe('profile');
    ui.settingsNavigate('api-keys'); expect(location.hash).toBe('#settings/api-keys');
    // Workflow & cadence moved to Running workers → Settings; old links land there.
    history.replaceState(null, '', '#settings/workflow'); ui.readHash();
    expect(ui.S.screen).toBe('workers'); expect(ui.wkState().tab).toBe('settings');
    ui.S.screen = 'settings';
    // AI models moved to AI Orchestration → Models & routing; old links land there.
    expect([...document.querySelectorAll('.settings-nav a')].map(x => x.getAttribute('href'))).not.toContain('#settings/ai');
    history.replaceState(null, '', '#settings/ai'); ui.readHash();
    expect(ui.S.screen).toBe('orchestration'); expect(ui.orchState().workspace).toBe('models');
    ui.S.screen = 'settings';
  });
  it('retains independent drafts and discards only the selected editor', () => {
    input('profile-name', 'Edited name'); ui.settingsNavigate('preferences');
    ui.setF('preferences', 'notice_period', '14'); ui.settingsNavigate('profile');
    expect(document.getElementById('profile-name').value).toBe('Edited name');
    ui.A['settings-discard']({ dataset: { form: 'profile' } });
    expect(ui.settingsDirty('profile')).toBe(false);
    expect(ui.settingsDirty('preferences')).toBe(true);
  });
  it('enables save on changes and disables it on revert', () => {
    const save = () => document.querySelector('[data-settings-save]');
    expect(save().disabled).toBe(true);
    input('profile-name', 'Changed'); expect(save().disabled).toBe(false);
    input('profile-name', 'Alex Morgan'); expect(save().disabled).toBe(true);
  });
  it('sends changed profile fields only and preserves failed drafts', async () => {
    input('profile-name', 'Edited');
    fetchMock.mockResolvedValueOnce(response({ detail: 'Try again' }, 503));
    await ui.settingsSave('profile');
    expect(ui.S.forms.profile.name).toBe('Edited');
    expect(document.querySelector('[role="alert"]').textContent).toBe('Try again');
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.profile, name: 'Edited' }));
    await ui.settingsSave('profile');
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ name: 'Edited' });
    expect(fetchMock.mock.calls[1][1].method).toBe('PATCH');
    expect(ui.settingsDirty('profile')).toBe(false);
  });
  it('keeps zero values complete and creates nested preference patches', () => {
    expect(ui.profileCompleteness()).toBe(100);
    ui.setF('preferences', 'notice_period', '0'); expect(ui.settingsDirty('preferences')).toBe(false);
    ui.setF('preferences', 'primary_skills', []); ui.setF('preferences', 'target_roles', ['Staff']);
    expect(ui.settingsPayload('preferences')).toEqual({ target_roles: ['Staff'], search_preferences: { primary_skills: [] } });
  });
  it('preserves location commas, deduplicates tags and enforces role limits', () => {
    ui.addTags('preferences', 'preferred_locations', 'New York, NY\nLondon', 25, []);
    expect(ui.S.forms.preferences.preferred_locations).toEqual(['New York, NY', 'London']);
    ui.addTags('preferences', 'target_roles', 'A,a,B,C,D,E,F', 5, []);
    expect(ui.S.forms.preferences.target_roles).toEqual(['A', 'B', 'C', 'D', 'E']);
  });
  it('commits a pending tag without replacing the save button on blur', () => {
    section('preferences'); const field = document.getElementById('preferences-primary_skills');
    const save = document.querySelector('[data-settings-save]'); field.value = 'Rust'; ui.commitTag(field, false);
    expect(ui.S.forms.preferences.primary_skills).toEqual(['Python', 'Rust']);
    expect(document.querySelector('[data-settings-save]')).toBe(save);
  });
  it('warns before leaving settings and clears secret fields between sections', () => {
    ui.setF('profile', 'name', 'Unsaved'); confirm.mockReturnValueOnce(false);
    expect(ui.settingsCanLeave('overview')).toBe(false);
    section('account'); ui.setF('account', 'new_password', 'temporary password');
    ui.settingsNavigate('api-keys'); expect(ui.S.forms.account).toBeUndefined(); expect(ui.S.forms.profile.name).toBe('Unsaved');
    expect(ui.settingsCanLeave('overview')).toBe(true); expect(ui.S.forms.profile).toBeUndefined();
  });
  it('saves one provider without changing or exposing another', async () => {
    section('api-keys'); ui.A['settings-key-edit']({ dataset: { id: 'openai' } });
    input('keys-openai', 'sk-test-only'); fetchMock.mockResolvedValueOnce(response(ui.D.settings));
    await ui.settingsSave('keys');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ openai: 'sk-test-only' });
    expect(ui.S.forms.keys).toBeUndefined(); expect(document.body.textContent).not.toContain('sk-test-only');
  });
  it('validates password matching and retains server errors', async () => {
    section('account');
    input('account-current_password', 'old password'); input('account-new_password', 'new password'); input('account-confirm_password', 'different password');
    await ui.settingsSave('account'); expect(fetchMock).not.toHaveBeenCalled();
    expect(ui.SETTING_ERRORS.account).toContain('do not match');
    input('account-confirm_password', 'new password');
    fetchMock.mockResolvedValueOnce(response({ detail: 'Current password is incorrect' }, 403));
    await ui.settingsSave('account'); expect(ui.SETTING_ERRORS.account).toContain('incorrect');
    fetchMock.mockResolvedValueOnce(response({ ok: true })); await ui.settingsSave('account');
    expect(ui.S.forms.account).toBeUndefined();
  });
  it('serializes clearing AI overrides as inheritance, including stage clearing', async () => {
    section('ai'); ui.D.settings.ai.saved.stage_models.rank = 'saved-model';
    ui.setF('ai', 'stage_rank', ''); ui.setF('ai', 'model', 'my-model');
    ui.setF('ai', 'require_free', 'false');
    expect(ui.settingsPayload('ai')).toEqual({ model: 'my-model', require_free: false, stage_models: { rank: '' } });
    ui.D.settings.ai.saved.require_free = true; ui.setF('ai', 'require_free', '');
    expect(ui.settingsPayload('ai').require_free).toBeNull();
    fetchMock.mockResolvedValueOnce(response(ui.D.settings)); await ui.settingsSave('ai');
    expect(fetchMock.mock.calls[0][0]).toBe('/api/settings');
  });
  it('requires confirmation for automatic approval before saving it from Running workers', async () => {
    workerSettings(); ui.setF('workers', 'application_mode', 'automatic');
    confirm.mockReturnValueOnce(false); await ui.settingsSave('workers'); expect(fetchMock).not.toHaveBeenCalled();
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.settings, application_mode: 'automatic' }));
    await ui.settingsSave('workers');
    expect(fetchMock.mock.calls[0][0]).toBe('/api/settings');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ application_mode: 'automatic' });
    expect(ui.SETTING_ERRORS.workers).toBeUndefined();
  });
  it('keeps the weekly target and follow-up wait with Job preferences, saved apart from the profile', async () => {
    section('preferences');
    expect(document.getElementById('preferences-weekly_target')).not.toBeNull();
    ui.setF('preferences', 'weekly_target', '20');
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.prefs, weekly_target: 20 }));
    await ui.settingsSave('preferences');
    const writes = fetchMock.mock.calls.filter(([, options]) => options?.method && options.method !== 'GET');
    expect(writes.map(([url, options]) => [url, options.method, JSON.parse(options.body)])).toEqual([['/api/preferences', 'PUT', { weekly_target: 20 }]]);
    expect(ui.D.prefs.weekly_target).toBe(20); expect(ui.S.forms.preferences).toBeUndefined();
    // With a profile change as well, each goes to its own endpoint.
    ui.setF('preferences', 'followup_days', '14'); ui.setF('preferences', 'target_roles', ['Staff']); fetchMock.mockClear();
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.prefs, followup_days: 14 })).mockResolvedValueOnce(response({ ...ui.D.profile, target_roles: ['Staff'] }));
    await ui.settingsSave('preferences');
    expect(fetchMock.mock.calls.slice(0, 2).map(([url]) => url)).toEqual(['/api/preferences', '/api/profile']);
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ target_roles: ['Staff'] });
  });
  it('chooses the tailored resume length and edits its bullet budget', async () => {
    ui.D.settings.tailoring_length = { pages: 2, budgets: { 1: [4, 3, 2, 1, 0], 2: [6, 5, 4, 3, 2, 1], 3: [7, 6, 5, 4, 3, 2] }, presets: { 1: [4, 3, 2, 1, 0], 2: [6, 5, 4, 3, 2, 1], 3: [7, 6, 5, 4, 3, 2] } };
    workerSettings();
    const view = () => document.getElementById('view');
    expect(view().textContent).toContain('Tailored resume length');
    expect(view().textContent).toContain('Not recommended');
    expect(document.querySelector('[data-field="tailor_pages"][value="2"]').checked).toBe(true);
    expect(document.querySelector('[data-tailor-budget]').textContent).toContain('Most recent role: 6 bullets');
    const one = document.querySelector('[data-field="tailor_pages"][value="1"]'); one.checked = true;
    one.dispatchEvent(new Event('change', { bubbles: true }));
    expect(document.querySelector('[data-tailor-budget]').textContent).toContain('5th role and older: heading only');
    ui.setF('workers', 'tailor_budget_1', '5, 2'); ui.render();
    expect(document.querySelector('[data-tailor-budget]').textContent).toContain('2nd role and older: 2 bullets');
    fetchMock.mockResolvedValueOnce(response(ui.D.settings));
    await ui.settingsSave('workers');
    const [url, options] = fetchMock.mock.calls[0];
    expect([url, JSON.parse(options.body)]).toEqual(['/api/settings', { tailoring_length: { pages: 1, budgets: { 1: [5, 2] } } }]);
    ui.setF('workers', 'tailor_budget_1', '5, nine'); fetchMock.mockClear();
    await ui.settingsSave('workers');
    expect(fetchMock).not.toHaveBeenCalled(); expect(ui.SETTING_ERRORS.workers).toContain('whole numbers from 0 to 8');
    ui.A['tailor-budget-reset']({ dataset: { pages: '1' } });
    expect(ui.S.forms.workers.tailor_budget_1).toBe('4, 3, 2, 1, 0');
  });
  it('sends the length chosen for one job, and nothing when it is the default', async () => {
    fetchMock.mockResolvedValue(response({ queued: [{ id: 1, job_id: 7 }], skipped: [] }));
    ui.S.tailorPages = '1'; await ui.A.generate({ dataset: { id: '7' } });
    const first = fetchMock.mock.calls.find(([url, opt]) => url === '/api/tailor-tasks' && opt?.method === 'POST');
    expect(JSON.parse(first[1].body)).toEqual({ ids: [7], mode: 'tailor', regenerate: true, pages: 1 });
    fetchMock.mockClear(); ui.S.tailorPages = '';
    await ui.A.generate({ dataset: { id: '7' } });
    const second = fetchMock.mock.calls.find(([url, opt]) => url === '/api/tailor-tasks' && opt?.method === 'POST');
    expect(JSON.parse(second[1].body)).toEqual({ ids: [7], mode: 'tailor', regenerate: true });
    fetchMock.mockClear(); ui.S.tailorPages = '3';
    await ui.A['skip-tailor']({ dataset: { id: '7' } });
    const skip = fetchMock.mock.calls.find(([url, opt]) => url === '/api/tailor-tasks' && opt?.method === 'POST');
    expect(JSON.parse(skip[1].body).pages).toBeUndefined();  // skipping keeps the resume as it is
    ui.S.tailorPages = '';
    expect(ui.packetLength({ length: { target_pages: 1, estimated_pages: 0.9 } })).toContain('target 1 page · <span data-packet-length>≈0.9 pages');
    expect(ui.packetLength({ length: { target_pages: 1, estimated_pages: 1.6 } })).toContain('over target');
    expect(ui.packetLength({ untailored: true, length: { estimated_pages: 2 } })).toBe('');
  });
  it('turns on automatic tailoring with its own fit score', async () => {
    workerSettings();
    expect(document.getElementById('view').textContent).toContain('qualified jobs wait in the Tailor queue');
    expect(document.getElementById('workers-auto_tailor_min_fit_score')).toBeNull();
    const box = document.querySelector('[data-field="auto_tailor"]'); box.checked = true;
    box.dispatchEvent(new Event('change', { bubbles: true }));  // the checkbox itself reveals the fit score field
    expect(document.getElementById('workers-auto_tailor_min_fit_score')).not.toBeNull();
    expect(document.getElementById('view').textContent).toContain('starts at once when a tailoring slot is free (up to 2 at a time)');
    ui.setF('workers', 'auto_tailor_min_fit_score', '80'); ui.render();
    expect(document.getElementById('view').textContent).toContain('fit score of 80 or more');
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.settings, auto_tailor: { enabled: true, min_fit_score: 80, enabled_at: '2026-09-28T10:00:00' } }));
    await ui.settingsSave('workers');
    const [url, options] = fetchMock.mock.calls[0];
    expect([url, options.method, JSON.parse(options.body)]).toEqual(['/api/settings', 'PATCH', { auto_tailor: true, auto_tailor_min_fit_score: 80 }]);
    ui.D.settings.execution_mode = 'claude_desktop'; workerSettings();
    expect(document.querySelector('[data-field="auto_tailor"]').disabled).toBe(true);
    expect(document.getElementById('view').textContent).toContain('only runs on Job Hunt OS workers');
  });
  it('protects dirty resume text before making a resume active, while an upload keeps the draft', async () => {
    resumesLoaded([resumeCard(), resumeCard({ id: 2, name: 'other.pdf', version: 2, is_active: false })]);
    section('resumes'); ui.setF('resumes', 'master_resume', 'Unsaved resume');
    confirm.mockReturnValue(false); await ui.A['rd-activate']({ dataset: { id: '2' } });
    expect(fetchMock).not.toHaveBeenCalled(); expect(ui.S.forms.resumes.master_resume).toBe('Unsaved resume');
    // An upload never changes the active resume, so it neither asks nor touches the draft; the new resume opens.
    fetchMock.mockResolvedValueOnce(response({ id: 9, resume_id: 3, is_active: false })).mockResolvedValueOnce(response({ active: null, current: null, versions: [], policy: {} }));
    await ui.settingsUploadResume(new File(['test'], 'test.txt'));
    expect(fetchMock.mock.calls[1][0]).toBe('/api/settings/resume?resume_id=3');
    expect(ui.S.forms.resumes.master_resume).toBe('Unsaved resume');
    expect(ui.SETTINGS_SUCCESS.resumes).toContain('make it active when you want it used');
    confirm.mockReturnValue(true);
    fetchMock.mockResolvedValueOnce(response({})).mockResolvedValueOnce(response({ active: null, current: null, versions: [], policy: {} })).mockResolvedValueOnce(response({ ...ui.D.profile, master_resume: 'From file' }));
    await ui.A['rd-activate']({ dataset: { id: '2' } });
    expect(ui.S.forms.resumes).toBeUndefined(); expect(ui.D.profile.master_resume).toBe('From file');
  });
  it('renders dangerous profile and filename content as text', () => {
    ui.D.profile.name = '<img src=x onerror=alert(1)>'; ui.render();
    expect(document.querySelector('.settings-identity img')).toBeNull();
    resumesLoaded([resumeCard({ name: '<svg onload=alert(1)>', file: { id: 1, filename: '<svg onload=alert(1)>.txt', size: 10 } }),
      resumeCard({ id: 2, name: 'other.txt', version: 2, is_active: false, file: null })]); section('resumes');
    expect(document.querySelectorAll('[data-rd-resume]').length).toBe(2);
    expect(document.querySelector('[data-rd-resume] svg')).toBeNull();
    expect(document.body.textContent).toContain('<svg onload=alert(1)>');  // shown as text, never as markup
    expect(document.body.textContent).toContain('Uploading never changes which one is active');
    expect([...document.querySelectorAll('[data-act="rd-activate"]')].map(b => b.dataset.id)).toEqual(['2']);
    expect(document.body.textContent).toContain('Created from text');
  });
  it('saves pending chip text and enables Save before blur', async () => {
    section('preferences'); input('preferences-primary_skills', 'Rust');
    expect(document.querySelector('[data-settings-save]').disabled).toBe(false);
    fetchMock.mockResolvedValueOnce(response(ui.D.profile)); await ui.settingsSave('preferences');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ search_preferences: { primary_skills: ['Python', 'Rust'] } });
  });
  it('captures country-specific work authorization and arrangement choices', () => {
    section('preferences');
    const country = document.getElementById('preferences-auth-country-0');
    const status = document.getElementById('preferences-auth-status-0');
    country.value = 'India'; country.dispatchEvent(new Event('change', { bubbles: true }));
    status.value = 'Citizen'; status.dispatchEvent(new Event('change', { bubbles: true }));
    ui.A['auth-add']({});
    document.getElementById('preferences-auth-country-1').value = 'Singapore';
    document.getElementById('preferences-auth-country-1').dispatchEvent(new Event('change', { bubbles: true }));
    document.getElementById('preferences-auth-status-1').value = 'EP (Employment Pass)';
    document.getElementById('preferences-auth-status-1').dispatchEvent(new Event('change', { bubbles: true }));
    expect(ui.settingsPayload('preferences')).toMatchObject({ work_authorization: 'India — Citizen\nSingapore — EP (Employment Pass)' });
    expect(document.querySelectorAll('[data-auth-row]').length).toBe(2);
  });
  it('removes a saved key only after confirmation and clears cancelled editors', async () => {
    section('api-keys'); ui.A['settings-key-edit']({ dataset: { id: 'openai' } });
    input('keys-openai', 'discard-this');
    ui.A['settings-key-edit']({ dataset: { id: 'openai' } }); expect(ui.S.forms.keys).toBeUndefined();
    confirm.mockReturnValueOnce(false); await ui.A['key-remove']({ dataset: { id: 'openai' } });
    expect(fetchMock).not.toHaveBeenCalled();
    fetchMock.mockResolvedValueOnce(response(ui.D.settings));
    await ui.A['key-remove']({ dataset: { id: 'openai' } });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ openai: '' });
  });
  it('blocks navigation during saves and disables unavailable key storage', () => {
    ui.S.busy['settings-profile'] = true;
    expect(ui.settingsCanLeave('settings', 'ai')).toBe(false);
    ui.settingsNavigate('ai'); expect(ui.S.settingsSection).toBe('profile');
    ui.S.busy = {}; ui.D.settings.secret_storage = false; section('api-keys');
    expect([...document.querySelectorAll('[data-act="settings-key-edit"]')].every(el => el.disabled)).toBe(true);
    expect(document.body.textContent).toContain('Key storage is unavailable');
  });
  it('uploads a first resume as active, and saves edited text as an inactive resume', async () => {
    resumesLoaded(); section('resumes');
    fetchMock.mockResolvedValueOnce(response({ id: 1, resume_id: 1, is_active: true })).mockResolvedValueOnce(response({ ...ui.D.profile, master_resume: 'Uploaded' })).mockResolvedValueOnce(response({ active: null, current: null, versions: [], policy: {} }));
    await ui.settingsUploadResume(new File(['Uploaded'], 'test.txt'));
    expect(fetchMock.mock.calls[0][1].body).toBeInstanceOf(FormData);
    expect(ui.D.profile.master_resume).toBe('Uploaded');
    expect(ui.SETTINGS_SUCCESS.resumes).toContain('set as active');
    ui.setF('resumes', 'master_resume', 'Edited text');
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.profile, master_resume: 'Uploaded', created_resume_id: 7, created_resume_version: 3, created_resume_active: false }));
    await ui.settingsSave('resumes');
    const patch = fetchMock.mock.calls.find(([url, opt]) => url === '/api/profile' && opt?.method === 'PATCH');
    expect(JSON.parse(patch[1].body)).toEqual({ master_resume: 'Edited text' });
    expect(ui.D.profile.master_resume).toBe('Uploaded'); expect(ui.D.profile.created_resume_id).toBeUndefined();
    expect(ui.SETTINGS_SUCCESS.resumes).toBe('Saved as resume v3. It is not used until you make it active');
    expect(ui.RD.viewId).toBe(7);
  });
  it('never carries an open resume data draft over to a new resume', async () => {
    const current = { ...resumeCard(), revision: 4, structured: { summary: { text: 'Old summary' } }, progress: { blocking: [], sections: [], entries: [], percent: 0, counts: {} } };
    resumesLoaded([resumeCard()]); ui.D.resumeData.current = current; section('resumes');
    const editing = () => { ui.RD.editing = 'summary'; ui.RD.draft = { summary: { text: 'Edited summary' } }; };
    editing(); ui.setF('resumes', 'master_resume', 'Pasted text');
    confirm.mockReturnValueOnce(false); await ui.settingsSave('resumes');
    expect(fetchMock).not.toHaveBeenCalled(); expect(ui.RD.draft.summary.text).toBe('Edited summary');
    confirm.mockReturnValueOnce(false); await ui.settingsUploadResume(new File(['x'], 'new.txt'));
    expect(fetchMock).not.toHaveBeenCalled(); expect(ui.RD.editing).toBe('summary');
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.profile, created_resume_id: 9, created_resume_version: 2, created_resume_active: false }));
    confirm.mockReturnValueOnce(true); await ui.settingsSave('resumes');
    expect(ui.RD.draft).toBeNull(); expect(ui.RD.editing).toBeNull(); expect(ui.RD.viewId).toBe(9);
  });
  it('shows the AI structuring while it runs and how it ended, and queues it from Re-parse with AI', async () => {
    const progress = { blocking: [], sections: [], entries: [], percent: 0, counts: { VERIFIED: 0, EDITED: 0, REJECTED: 0 }, needs_review: 0 };
    const current = { ...resumeCard(), revision: 0, structured: {}, progress, ai_parse: { active: true, status: 'PENDING', note: 'Structuring with AI…', dropped: 0 } };
    resumesLoaded([resumeCard()]); ui.D.resumeData.current = current; section('resumes');
    expect(document.querySelector('[data-rd-ai-parse]').dataset.rdAiParse).toBe('active');
    expect(document.querySelector('[data-act="rd-reparse"][data-ai="1"]').disabled).toBe(true);
    ui.D.resumeData.current = { ...current, ai_parse: { active: false, status: 'COMPLETED', outcome: 'filled', note: 'Filled from the AI parse', dropped: 2 } };
    ui.render();
    const line = document.querySelector('[data-rd-ai-parse]');
    expect(line.dataset.rdAiParse).toBe('filled');
    expect(line.textContent).toContain('2 values the resume text does not contain were left out');
    const reparse = document.querySelector('[data-act="rd-reparse"][data-ai="1"]');
    expect(reparse.disabled).toBe(false);
    fetchMock.mockResolvedValueOnce(response({ ...current, ai_parse: { active: true, status: 'PENDING', note: 'Structuring with AI…' } }));
    await ui.A['rd-reparse'](reparse);
    const post = fetchMock.mock.calls.find(([url]) => url === '/api/resumes/1/reparse');
    expect(JSON.parse(post[1].body)).toEqual({ use_ai: true });
    expect(document.querySelector('[data-rd-ai-parse]').dataset.rdAiParse).toBe('active');
  });
  it('deleting a resume asks once, keeps text drafts, and the active one cannot be deleted', async () => {
    resumesLoaded([resumeCard(), resumeCard({ id: 2, name: 'older.pdf', version: 2, is_active: false })]); section('resumes');
    expect([...document.querySelectorAll('[data-act="rd-delete-version"]')].map(b => b.dataset.id)).toEqual(['2']);
    ui.setF('resumes', 'master_resume', 'Draft');
    confirm.mockReturnValueOnce(false);
    await ui.A['rd-delete-version']({ dataset: { id: '2', name: 'older.pdf · v2' } }); expect(fetchMock).not.toHaveBeenCalled();
    fetchMock.mockResolvedValueOnce(response({ deleted: 2, file_deleted: true, jobs: 0 })).mockResolvedValueOnce(response({ active: null, current: null, versions: [resumeCard()], policy: {} }));
    await ui.A['rd-delete-version']({ dataset: { id: '2', name: 'older.pdf · v2' } });
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(ui.S.forms.resumes.master_resume).toBe('Draft');
    expect(ui.D.resumeData.versions.map(v => v.id)).toEqual([1]);
  });
  it('renames a resume in place without touching anything else', async () => {
    resumesLoaded([resumeCard(), resumeCard({ id: 2, name: 'older.pdf', version: 2, is_active: false, roles: ['Lead'] })]); section('resumes');
    ui.A['rd-rename']({ dataset: { id: '2', name: 'older.pdf' } });
    expect(document.getElementById('rdname-name').value).toBe('older.pdf');
    ui.setF('rdname', 'name', '   ');
    await ui.A['rd-rename-save']({ dataset: { id: '2' } }); expect(fetchMock).not.toHaveBeenCalled();
    ui.setF('rdname', 'name', ' Leadership CV ');
    fetchMock.mockResolvedValueOnce(response({ id: 2, name: 'Leadership CV' }));
    await ui.A['rd-rename-save']({ dataset: { id: '2' } });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/resumes/2');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ name: 'Leadership CV' });
    const renamed = ui.D.resumeData.versions.find(v => v.id === 2);
    expect(renamed).toMatchObject({ name: 'Leadership CV', is_active: false, roles: ['Lead'], version: 2 });
    expect(ui.RD.renaming).toBeNull();
    expect(document.querySelector('[data-rd-resume="2"] strong').textContent).toBe('Leadership CV · v2');
  });
  it('requires saved text for evidence extraction', async () => {
    resumesLoaded(); section('resumes'); ui.setF('resumes', 'master_resume', 'Draft');
    await ui.A['parse-evidence'](); expect(fetchMock).not.toHaveBeenCalled();
    expect(ui.SETTING_ERRORS.resumes).toContain('Save your resume');
    delete ui.S.forms.resumes; fetchMock.mockResolvedValueOnce(response({ added: 2 }));
    await ui.A['parse-evidence'](); expect(fetchMock.mock.calls[0][0]).toContain('parse-evidence');
  });
  it('saves the rhythm alone and never writes untouched profile fields', async () => {
    section('preferences'); ui.setF('preferences', 'followup_days', '14');
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.prefs, followup_days: 14 }));
    await ui.settingsSave('preferences'); expect(fetchMock.mock.calls.filter(([, options]) => options.method !== 'GET')).toHaveLength(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/preferences'); expect(ui.D.prefs.followup_days).toBe(14);
  });
  it('supports section selector changes and password reuse validation', async () => {
    ui.A['settings-section']({ value: 'account' });
    input('account-current_password', 'same password'); input('account-new_password', 'same password'); input('account-confirm_password', 'same password');
    await ui.settingsSave('account'); expect(ui.SETTING_ERRORS.account).toContain('different');
  });

  it('shows a recoverable initial load error and loading state', () => {
    ui.D.settingsLoadError = 'Offline'; ui.render();
    expect(document.querySelector('[role="alert"]').textContent).toContain('Offline');
    expect(document.querySelector('[data-act="settings-retry"]')).not.toBeNull();
    ui.D.settingsLoadError = ''; ui.D.profile = null; ui.render();
    expect(document.querySelector('.loading').textContent).toBe('Loading…');
  });
  it('preserves ordinary drafts on Back and Forward routes', async () => {
    const fixture = data();
    fetchMock.mockImplementation(async url => response(({ '/api/profile': fixture.profile, '/api/settings': fixture.settings, '/api/preferences': fixture.prefs, '/api/profile/resumes': [], '/api/workspace/summary': null })[url]));
    section('profile'); ui.setF('profile', 'name', 'Still editing');
    history.replaceState(null, '', '#settings/preferences');
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(ui.S.settingsSection).toBe('preferences');
    history.replaceState(null, '', '#settings/profile');
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(ui.S.forms.profile.name).toBe('Still editing');
  });

  it('refreshes saved identity without clearing other section drafts', async () => {
    input('profile-name', 'Updated identity');
    ui.setF('preferences', 'notice_period', '14');
    const summary = { ...ui.D.summary, profile: { name: 'Updated identity', headline: 'Updated headline' } };
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.profile, name: 'Updated identity' })).mockResolvedValueOnce(response(summary));
    await ui.settingsSave('profile');
    expect(fetchMock.mock.calls[1][0]).toBe('/api/workspace/summary');
    expect(document.getElementById('brand-sub').textContent).toBe('Updated identity · Updated headline');
    expect(ui.S.forms.preferences.notice_period).toBe('14');
  });
  it('refreshes the effective model shown in the shell after an AI save', async () => {
    section('ai'); ui.setF('ai', 'model', 'updated-model');
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.settings, model: 'updated-model' }));
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.summary, model: 'updated-model' }));
    await ui.settingsSave('ai');
    expect(document.getElementById('side-meta').textContent).toContain('updated-model');
  });
  it('refreshes auto-search counts after preference saves', async () => {
    section('preferences'); ui.setF('preferences', 'target_roles', ['Staff', 'Lead']);
    const summary = { ...ui.D.summary, auto_search: { ...ui.D.summary.auto_search, setups: 2, roles: 2, locations: 3 } };
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.profile, target_roles: ['Staff', 'Lead'] })).mockResolvedValueOnce(response(summary));
    await ui.settingsSave('preferences');
    expect(document.getElementById('side-foot').textContent).toContain('2 scheduled setups · 2 roles × 3 locations');
  });
  it('refreshes the shell after approval settings save, and keeps the rhythm change if it cannot be saved', async () => {
    workerSettings(); ui.setF('workers', 'application_mode', 'automatic');
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.settings, application_mode: 'automatic' }));
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.summary, application_mode: 'automatic' }));
    await ui.settingsSave('workers');
    expect(document.getElementById('side-meta').textContent).toContain('automatic');
    ui.S.screen = 'settings'; section('preferences'); ui.setF('preferences', 'followup_days', '14');
    fetchMock.mockResolvedValueOnce(response({ detail: 'Cadence offline' }, 503));
    await ui.settingsSave('preferences');
    expect(ui.SETTING_ERRORS.preferences).toContain('Cadence offline');
    expect(ui.settingsChanges('preferences')).toEqual({ followup_days: '14' });
  });
  it('distinguishes summary-refresh failure from save failure', async () => {
    input('profile-name', 'Already saved');
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.profile, name: 'Already saved' }));
    fetchMock.mockResolvedValueOnce(response({ detail: 'Offline' }, 503));
    await ui.settingsSave('profile');
    expect(ui.D.profile.name).toBe('Already saved');
    expect(ui.S.forms.profile).toBeUndefined();
    expect(ui.SETTING_ERRORS.profile).toContain('Changes saved, but the sidebar could not refresh');
    expect(document.querySelector('[data-settings-save]').disabled).toBe(true);
    expect(ui.D.summary.profile.name).toBe('Alex Morgan');
  });

});

describe('resume data', () => {
  const verification = status => ({ status, updated_at: null });
  function detail(overrides = {}) {
    const structured = {
      personal_information: { id: 'personal', full_name: 'Alex Morgan', headline: 'Lead', location: 'Mumbai', email: '', phone: '', linkedin: '', github: '', portfolio: '', other_links: [], verification: verification('VERIFIED') },
      summary: { id: 'summary', text: 'Builds <platforms>', verification: verification('UNVERIFIED') },
      skills: [{ id: 'skill_python', name: 'Python', category: 'Languages', verification: verification('VERIFIED') }],
      experience: [{ id: 'exp_zendrive_2024', company: 'Zendrive', job_title: 'Lead Engineer', location: '', employment_type: '', start_date: '2024-06', end_date: '2025-10', is_current: false, technologies: ['Python'], verification: verification('EDITED'),
        achievements: [{ id: 'ach_1', text: 'Reduced incidents by 45%', verification: verification('VERIFIED') }], responsibilities: [{ id: 'resp_1', text: 'Owned ingestion', verification: verification('REJECTED') }] }],
      projects: [], education: [], certifications: [], awards: [], languages: [], additional_information: [],
    };
    const progress = { total: 6, counts: { VERIFIED: 3, UNVERIFIED: 1, EDITED: 1, REJECTED: 1 }, reviewed: 4, percent: 67, needs_review: 2,
      sections: [['personal_information', 'VERIFIED'], ['summary', 'UNVERIFIED'], ['skills', 'VERIFIED'], ['experience', 'NEEDS_REVIEW'], ['projects', 'EMPTY']].map(([section, state]) => ({ section, label: section, state, required: true, counts: { UNVERIFIED: state === 'UNVERIFIED' ? 1 : 0, EDITED: state === 'NEEDS_REVIEW' ? 1 : 0 } })),
      entries: [{ section: 'experience', item_id: 'exp_zendrive_2024', label: 'Lead Engineer · Zendrive', state: 'NEEDS_REVIEW', counts: {} }],
      blocking: [{ item_id: 'summary', label: 'Builds platforms' }] };
    return { id: 5, name: 'resume.pdf', version: 2, is_active: true, verification_status: 'NEEDS_REVIEW', verified_at: '2026-09-01T10:00:00', revision: 3, source_filename: 'resume.pdf', has_original: true, structured, progress, pending_reparse: null, ...overrides };
  }
  function settle() { return new Promise(r => setTimeout(r, 0)); }
  function route(routes) {
    fetchMock.mockImplementation(async (url, opt = {}) => {
      const key = `${opt.method || 'GET'} ${url}`;
      const hit = Object.entries(routes).find(([k]) => key === k) || Object.entries(routes).find(([k]) => key.startsWith(k));
      if (!hit) return response({});
      const value = typeof hit[1] === 'function' ? hit[1](JSON.parse(opt.body || 'null')) : hit[1];
      return value && value.__status ? response(value.body, value.__status) : response(value);
    });
  }
  async function open(active = detail(), extra = {}) {
    route({ 'GET /api/settings/resume': { active, versions: [active, { id: 4, name: 'old.pdf', version: 1, is_active: false, verification_status: 'UNVERIFIED', setups: [{ id: 3, name: 'ML roles' }] }], policy: { allow_unverified_resume_facts: false, ranking_uses_unverified: true } }, ...extra });
    section('resume-data'); await settle();
  }

  it('shows verification progress, statuses and blocks Verify Resume while items need review', async () => {
    await open();
    const view = document.getElementById('view').innerHTML;
    expect(document.querySelector('h2').textContent).toBe('Resumes');
    expect(document.querySelector('[data-rd-resume="5"]').getAttribute('aria-current')).toBe('true');
    expect(document.querySelector('[data-rd-percent]').textContent).toContain('67%');
    expect(view).toContain('require review');
    expect(view).toContain('Builds &lt;platforms&gt;');
    expect(document.querySelector('[data-rd-item="ach_1"] [data-rd-status]').dataset.rdStatus).toBe('VERIFIED');
    expect(document.querySelector('[data-rd-item="resp_1"] [data-rd-status]').dataset.rdStatus).toBe('REJECTED');
    expect(document.querySelector('[data-act="rd-verify-resume"]').disabled).toBe(true);
    for (const el of document.querySelectorAll('#settings-editor input, #settings-editor textarea')) expect(el.labels?.length).toBeTruthy();
  });
  it('verifies an item, a section and the whole resume through the API', async () => {
    const verified = detail({ verification_status: 'VERIFIED', progress: { ...detail().progress, blocking: [] } });
    await open(detail(), { 'POST /api/resumes/5/verify-item': body => { expect(body).toEqual({ item_id: 'summary', status: 'VERIFIED' }); return verified; },
      'POST /api/resumes/5/verify-section': verified, 'POST /api/resumes/5/unverify-item': detail({ progress: { ...detail().progress, blocking: [] } }), 'POST /api/resumes/5/verify': verified });
    await ui.A['rd-verify-item']({ dataset: { id: 'summary', status: 'VERIFIED' } });
    expect(ui.D.resumeData.active.verification_status).toBe('VERIFIED');
    const button = () => document.querySelector('[data-act="rd-verify-resume"]');
    expect(button().disabled).toBe(true);
    expect(button().textContent).toContain('Resume verified');
    await ui.A['rd-verify-resume']();  // an already verified resume is not verified again
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/resumes/5/verify')).toBe(false);
    await ui.A['rd-verify-section']({ dataset: { section: 'experience' } });
    await ui.A['rd-unverify']({ dataset: { id: 'ach_1' } });  // back to review: Verify Resume is offered again
    expect(button().disabled).toBe(false);
    await ui.A['rd-verify-resume']();
    expect(fetchMock.mock.calls.map(([url, opt]) => `${opt?.method || 'GET'} ${url}`)).toContain('POST /api/resumes/5/verify');
  });
  it('lists blocking items when the server refuses to verify the resume', async () => {
    await open(detail({ progress: { ...detail().progress, blocking: [] } }), { 'POST /api/resumes/5/verify': { __status: 422, body: { detail: { message: '1 required item(s) still need review', blocking: [{ item_id: 'x', label: 'Unreviewed degree' }] } } } });
    await ui.A['rd-verify-resume']();
    expect(ui.SETTING_ERRORS['resume-data']).toContain('still need review');
    expect(document.getElementById('view').innerHTML).toContain('Unreviewed degree');
  });
  it('edits a section as a form, tracks dirty state and saves typed data with the revision', async () => {
    let sent;
    await open(detail(), { 'PUT /api/resumes/5/structured': body => { sent = body; return detail({ revision: 4 }); } });
    ui.A['rd-edit']({ dataset: { section: 'experience' } });
    expect(document.querySelector('#rd-experience-0-company').value).toBe('Zendrive');
    expect(ui.resumeDataDirty()).toBe(false);
    input('rd-experience-0-company', 'Zendrive Inc');
    input('rd-experience-0-technologies', 'Python, Kafka');
    input('rd-experience-0-start_date', '');
    const current = document.getElementById('rd-experience-0-is_current'); current.checked = true; current.dispatchEvent(new Event('change', { bubbles: true }));
    expect(ui.resumeDataDirty()).toBe(true);
    ui.A['rd-add']({ dataset: { path: 'experience.0.achievements', bullet: '1' } });
    input('rd-experience-0-achievements-1-text', 'Cut cost 30%');
    ui.A['rd-move']({ dataset: { path: 'experience.0.achievements', index: '1', dir: '-1' } });
    ui.A['rd-move']({ dataset: { path: 'experience.0.achievements', index: '0', dir: '-1' } });
    ui.A['rd-add']({ dataset: { path: 'experience', section: 'experience' } });
    ui.A['rd-remove']({ dataset: { path: 'experience', index: '1' } });
    await ui.A['settings-save']({ dataset: { form: 'resume-data' } });
    expect(sent.revision).toBe(3);
    expect(sent.data.experience[0]).toMatchObject({ company: 'Zendrive Inc', technologies: ['Python', 'Kafka'], start_date: null, is_current: true });
    expect(sent.data.experience[0].achievements.map(a => a.text)).toEqual(['Cut cost 30%', 'Reduced incidents by 45%']);
    expect(ui.RD.editing).toBeNull();
  });
  it('reloads and keeps the user informed after an edit conflict', async () => {
    await open(detail(), { 'PUT /api/resumes/5/structured': { __status: 409, body: { detail: 'This resume changed since you opened it.' } } });
    ui.A['rd-edit']({ dataset: { section: 'summary' } });
    input('rd-summary-text', 'New summary');
    await ui.A['settings-save']({ dataset: { form: 'resume-data' } });
    expect(ui.SETTING_ERRORS['resume-data']).toContain('were not saved');
  });
  it('asks before discarding edits when cancelling, switching tabs or leaving settings', async () => {
    await open();
    ui.A['rd-edit']({ dataset: { section: 'skills' } });
    input('rd-skills-0-name', 'Go');
    confirm.mockReturnValueOnce(false);
    ui.A['rd-cancel']();
    expect(ui.RD.editing).toBe('skills');
    ui.A['rd-tab']({ dataset: { tab: 'raw' } });
    expect(ui.RD.editing).toBeNull();
    ui.A['rd-tab']({ dataset: { tab: 'structured' } });
    ui.A['rd-edit']({ dataset: { section: 'skills' } }); input('rd-skills-0-name', 'Go');
    expect(ui.settingsCanLeave('overview')).toBe(true);
    expect(ui.RD.draft).toBeNull();
    ui.A['rd-edit']({ dataset: { section: 'skills' } }); input('rd-skills-0-name', 'Go');
    ui.A['settings-discard']({ dataset: { form: 'resume-data' } });
    expect(ui.RD.editing).toBeNull();
  });
  it('shows raw text read-only, the original file in the menu and change history', async () => {
    await open(detail(), { 'GET /api/resumes/5/raw': { raw_text: 'RAW <b>resume</b>' }, 'GET /api/resumes/5/changes': { total: 1, items: [{ change_type: 'UPDATE', source: 'USER', field_path: 'summary.text', old_value: 'a', new_value: { x: 1 }, changed_at: '2026-09-02T10:00:00' }] } });
    ui.A['rd-tab']({ dataset: { tab: 'raw' } }); await settle();
    const raw = document.querySelector('[data-rd-raw]');
    expect(raw.textContent).toBe('RAW <b>resume</b>');
    expect(document.querySelector('[data-rd-raw] textarea, #rd-detail textarea')).toBeNull();
    expect(document.querySelector('#rd-detail .rd-menu a[href="/api/resumes/5/original"]')).toBeTruthy();
    ui.A['rd-tab']({ dataset: { tab: 'history' } }); await settle();
    expect(document.querySelector('[data-rd-change-type="UPDATE"]').textContent).toContain('summary.text');
  });
  it('compares a re-parse and sends only the choices the user made', async () => {
    let decisions;
    const pending = { parser: 'deterministic', changes: [{ id: 'chg_1', label: 'Work experience · title', current: 'Lead Engineer', proposed: 'Staff Engineer', current_status: 'VERIFIED' }] };
    await open(detail(), { 'POST /api/resumes/5/reparse/resolve': body => { decisions = body.decisions; return detail(); }, 'POST /api/resumes/5/reparse': detail({ pending_reparse: pending }) });
    await ui.A['rd-reparse']({ dataset: { ai: '0' } });
    const view = document.getElementById('view').innerHTML;
    expect(view).toContain('Staff Engineer'); expect(view).toContain('Keep current');
    ui.A['rd-decide']({ dataset: { change: 'chg_1', choice: 'use_new' } });
    await ui.A['rd-resolve']({ dataset: {} });
    expect(decisions).toEqual({ chg_1: 'use_new' });
    await ui.A['rd-reparse']({ dataset: { ai: '1' } });
    ui.A['rd-decide']({ dataset: { change: 'chg_1', choice: 'use_new' } });
    await ui.A['rd-resolve']({ dataset: { all: 'keep' } });
    expect(decisions).toEqual({});
  });
  it('switches versions and saves the candidate policy', async () => {
    let policy;
    const old = detail({ id: 4, name: 'old.pdf', version: 1, is_active: false });
    await open(detail(), { 'GET /api/settings/resume?resume_id=4': { active: detail(), current: old, versions: [detail(), old], policy: {} },
      'POST /api/resumes/4/activate': detail(), 'PUT /api/settings/resume/policy': body => { policy = body; return { allow_unverified_resume_facts: true, ranking_uses_unverified: true }; } });
    expect(document.getElementById('view').textContent).toContain('used by ML roles');
    expect(document.querySelector('[data-act="rd-default"]')).toBeNull();
    // Viewing another version opens it for editing without making it active.
    await ui.A['rd-view']({ dataset: { id: '4' } });
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/settings/resume?resume_id=4')).toBe(true);
    expect(fetchMock.mock.calls.some(([url, opt]) => url.includes('/activate') && opt?.method === 'POST')).toBe(false);
    expect(document.getElementById('view').textContent).toContain('This resume is not active');
    await ui.A['rd-activate']({ dataset: { id: '4' } });
    expect(ui.RD.viewId).toBeNull();
    await ui.A['rd-policy']({ dataset: { key: 'allow_unverified_resume_facts' }, checked: true });
    expect(policy).toEqual({ allow_unverified_resume_facts: true });
    expect(ui.D.resumeData.policy.allow_unverified_resume_facts).toBe(true);
    const sources = [...document.querySelectorAll('#view input[name="rd-resume-source"]')];
    expect(sources.map(r => r.value)).toEqual(['structured', 'master_text']);
    await ui.A['rd-policy-source']({ value: 'master_text' });
    expect(policy).toEqual({ resume_source: 'master_text' });
    ui.A['rd-jump']({ dataset: { section: 'skills' } }, { preventDefault() {} });
    expect(ui.RD.tab).toBe('structured');
  });
  it('structures uploads with AI only by default: no reading toggles on the screen, a waiting version shows no regex data', async () => {
    const waiting = detail({ parser: 'deterministic', ai_parse: { active: true, status: 'PROCESSING', note: 'Structuring with AI…' } });
    await open(waiting, { 'GET /api/settings/resume': { active: waiting, versions: [waiting], policy: {}, ai_only: true, extraction_v2: true } });
    const view = document.getElementById('view');
    expect(view.textContent).toContain('the Resume Parser model is reading this resume');
    expect(view.innerHTML).not.toContain('Builds &lt;platforms&gt;');
    expect(document.getElementById('rd-ai-only')).toBeNull();
    expect(document.getElementById('rd-extraction-v2')).toBeNull();
    expect(view.textContent).not.toContain('Read PDF uploads with the new reader');
    expect(ui.A['rd-ai-only']).toBeUndefined();
    expect(ui.A['rd-extraction-v2']).toBeUndefined();
  });
  it('shows a project’s image, link, details and result, and an additional website link, only for http(s)', async () => {
    const v = verification('UNVERIFIED');
    const base = detail();
    const structured = { ...base.structured,
      projects: [{ id: 'proj_a', name: 'Atlas', url: 'github.com/jane/atlas', image_url: 'https://img.test/a.png', description: 'Podcast search', result: 'Served 2M users', technologies: [], achievements: [], verification: v },
        { id: 'proj_b', name: 'Bad', url: 'javascript:alert(1)', image_url: 'data:image/png;base64,xx', description: '', result: '', technologies: [], achievements: [], verification: v }],
      additional_information: [{ id: 'info_1', text: 'Blog', url: 'https://jane.dev', verification: v }] };
    const active = { ...base, structured };
    await open(active, { 'GET /api/settings/resume': { active, versions: [active], policy: {} } });
    const atlas = document.querySelector('[data-rd-item="proj_a"]');
    expect(atlas.querySelector('img.rd-project-image').getAttribute('src')).toBe('https://img.test/a.png');
    expect(atlas.querySelector('a').getAttribute('href')).toBe('https://github.com/jane/atlas');
    expect(atlas.textContent).toContain('Podcast search');
    expect(atlas.textContent).toContain('Served 2M users');
    const bad = document.querySelector('[data-rd-item="proj_b"]');
    expect(bad.querySelector('img')).toBeNull();
    expect(bad.querySelector('a')).toBeNull();
    expect(document.querySelector('[data-rd-item="info_1"] a').getAttribute('href')).toBe('https://jane.dev');
    ui.A['rd-edit']({ dataset: { section: 'projects' } });
    expect(document.getElementById('rd-projects-0-image_url').value).toBe('https://img.test/a.png');
    expect(document.getElementById('rd-projects-0-result').value).toBe('Served 2M users');
  });
  it('renders every section type and edits single-object sections', async () => {
    const v = verification('UNVERIFIED');
    const base = detail();
    const structured = { ...base.structured, summary: { id: 'summary', text: '', verification: v },
      experience: [{ ...base.structured.experience[0], is_current: true, start_date: null, raw_start_date: '2021', achievements: [], responsibilities: [] }],
      projects: [{ id: 'proj_a', name: 'Atlas', description: 'Search', url: '', technologies: [], achievements: [{ id: 'pach_1', text: 'Served 2M users', verification: v }], verification: v }],
      education: [{ id: 'edu_1', degree: 'BTech', field_of_study: 'CS', institution: 'IIT', end_date: '2016-05', verification: v }],
      certifications: [{ id: 'cert_1', name: 'CKA', issuer: 'CNCF', issue_date: '2022', verification: v }],
      awards: [{ id: 'award_1', title: '', description: 'Hackathon winner', date: '2020', verification: v }],
      languages: [{ id: 'lang_1', name: 'Hindi', proficiency: 'Native', verification: v }, { id: 'lang_2', name: 'German', proficiency: '', verification: v }],
      additional_information: [{ id: 'info_1', text: 'Open source maintainer', verification: v }], skills: [{ id: 'skill_go', name: 'Go', category: '', verification: v }] };
    await open(detail({ structured, verified_at: null, has_original: false, source_filename: null, verification_status: 'SOMETHING_NEW',
      progress: { ...base.progress, blocking: Array.from({ length: 7 }, (_, i) => ({ label: `item ${i}` })) } }));
    const view = document.getElementById('view').innerHTML;
    for (const text of ['Atlas', 'Served 2M users', 'BTech · CS · IIT', 'CKA', 'Hackathon winner', 'Hindi · Native', 'German', 'Open source maintainer', '2021 – Present']) expect(view).toContain(text);
    ui.A['rd-edit']({ dataset: { section: 'personal_information' } });
    input('rd-personal_information-full_name', 'Alex M');
    expect(ui.resumeDataDirty()).toBe(true);
    ui.A['rd-cancel']();
    ui.A['rd-edit']({ dataset: { section: 'summary' } });
    expect(document.getElementById('rd-summary-text').value).toBe('');
    ui.rdReset(); ui.render();
    expect(document.querySelector('#rd-detail a[href$="/original"]')).toBeNull();  // no stored file: nothing to download
  });
  it('shows how the file was read and assigns a heading nobody could place', async () => {
    let sent;
    const choices = [['summary', 'Summary'], ['skills', 'Skills'], ['none', 'Not a section']].map(([kind, label]) => ({ kind, label }));
    const extraction = (method, kind) => ({ source: 'pdfium', quality: { route: 'clean', reasons: [] }, choices, unplaced: method === 'unknown' ? 1 : 0,
      sections: [{ text: 'Summary', kind: 'summary', method: 'synonym', line: 3, preview: 'Builds platforms' },
        { text: 'SELECTED WORK', kind, method, line: 9, preview: 'Kafka, Go' }] });
    const before = detail({ extraction: extraction('unknown', 'other'), progress: { ...detail().progress, blocking: [] } });
    const after = detail({ extraction: extraction('person', 'skills'), progress: { ...detail().progress, blocking: [] } });
    await open(before, { 'POST /api/resumes/5/sections': body => { sent = body; return after; } });
    const panel = document.querySelector('.rd-sections');
    expect(panel.open).toBe(true);
    expect(panel.textContent).toContain('1 heading needs a section');
    expect(document.querySelector('[data-rd-heading="9"]').textContent).toContain('Choose');
    expect(document.querySelector('[data-act="rd-verify-resume"]').disabled).toBe(true);
    await ui.A['rd-section-assign']({ value: 'skills', dataset: { line: '9' } }); await settle();
    expect(sent).toEqual({ line: 9, kind: 'skills' });
    expect(document.querySelector('[data-rd-heading="9"]').textContent).toContain('Yours');
    expect(document.querySelector('[data-act="rd-verify-resume"]').disabled).toBe(false);
    expect(document.querySelector('.rd-sections summary').textContent).not.toContain('to place');
  });
  it('lists values the AI parse could not ground and adds or discards them', async () => {
    let sent;
    const review = [{ id: 'h1', kind: 'skill', value: 'Rust', where: 'Skills', addable: true },
      { id: 'h2', kind: 'project', value: 'Search Ranker', where: '', addable: false }];
    const ex = rv => ({ source: 'pdfium', quality: { route: 'clean', reasons: [] }, choices: [], unplaced: 0, sections: [], review: rv });
    const base = detail();
    const structured = { ...base.structured, skills: [{ id: 'skill_k', name: 'Apache Kafka', raw: 'Kafka', category: '', verification: { status: 'UNVERIFIED', updated_at: null } }] };
    await open(detail({ structured, extraction: ex(review) }), { 'POST /api/resumes/5/review': body => { sent = body; return detail({ structured, extraction: ex(review.slice(1)) }); } });
    expect(document.querySelector('.rd-sections').open).toBe(true);
    expect(document.querySelectorAll('[data-rd-held]').length).toBe(2);
    expect(document.querySelector('[data-rd-held="h2"]').textContent).toContain('add it with Edit');
    expect(document.querySelector('[data-rd-held="h2"] [data-action="add"]')).toBeNull();
    expect(document.querySelector('[data-rd-item="skill_k"]').textContent).toContain('written “Kafka”');
    await ui.A['rd-review']({ dataset: { id: 'h1', action: 'add' } }); await settle();
    expect(sent).toEqual({ id: 'h1', action: 'add' });
    expect(document.querySelectorAll('[data-rd-held]').length).toBe(1);
  });
  it('lists corrections with their evidence and keeps or undoes them', async () => {
    let sent;
    const fix = status => ({ id: 'c1', original: 'Kaga', corrected: 'Kafka', method: 'ocr', evidence: ['the page reads “Kafka” there'], count: 3, status });
    const ex = status => ({ source: 'pdfium', quality: { route: 'poor', reasons: [], ocr: 'tesseract' }, choices: [], unplaced: 0, sections: [], review: [], corrections: [fix(status)] });
    await open(detail({ extraction: ex('applied') }), { 'POST /api/resumes/5/corrections': body => { sent = body; return detail({ extraction: ex('undone') }); } });
    expect(document.querySelector('.rd-sections').open).toBe(true);
    const row = document.querySelector('[data-rd-fix="c1"]');
    expect(row.textContent).toContain('Kaga → Kafka');
    expect(row.textContent).toContain('×3');
    expect(row.textContent).toContain('the page reads “Kafka” there');
    expect(row.textContent).toContain('Page shows it');
    await ui.A['rd-fix']({ dataset: { id: 'c1', action: 'undo' } }); await settle();
    expect(sent).toEqual({ id: 'c1', action: 'undo' });
    expect(document.querySelector('[data-rd-fix="c1"]').textContent).toContain('Correct again');
  });
  it('marks facts the reader trusts less and accepts the high-confidence ones on request', async () => {
    let called = false;
    const confidence = { summary: { level: 'medium', reasons: ['normalized from “Kafka”'] }, resp_1: { level: 'low', reasons: ['“x” is not in the text as written'] }, exp_zendrive_2024: { level: 'high', reasons: ['in the file’s text word for word'] } };
    const base = detail({ confidence });
    const structured = { ...base.structured, experience: [{ ...base.structured.experience[0], verification: { status: 'UNVERIFIED', updated_at: null } }] };
    await open(detail({ confidence, structured }), { 'POST /api/resumes/5/verify-confident': () => { called = true; return detail({ confidence }); } });
    expect(document.querySelector('[data-rd-item="summary"] [data-rd-conf]').dataset.rdConf).toBe('medium');
    expect(document.querySelector('[data-rd-item="summary"] [data-rd-conf]').getAttribute('title')).toContain('normalized from “Kafka”');
    expect(document.querySelector('[data-rd-item="resp_1"] [data-rd-conf]')).toBeNull();  // rejected: confidence no longer matters
    expect(document.querySelector('[data-rd-item="exp_zendrive_2024"] [data-rd-conf]')).toBeNull();  // high: nothing to flag
    const accept = document.querySelector('[data-act="rd-accept-confident"]');
    expect(accept.textContent).toContain('Accept 1 high-confidence');
    await ui.A['rd-accept-confident'](); await settle();
    expect(called).toBe(true);
  });
  it('shows nothing about reading for a version read before the new reader', async () => {
    await open(detail({ extraction: null }));
    expect(document.querySelector('.rd-sections')).toBeNull();
  });
  it('reviews in one place: skill chips, hidden empty sections, Add section and Review next', async () => {
    const base = detail();
    const structured = { ...base.structured, skills: [{ id: 'skill_py', name: 'Python', category: 'Languages', verification: verification('VERIFIED') }, { id: 'skill_k', name: 'Kafka', category: 'Data', verification: verification('UNVERIFIED') }] };
    await open(detail({ structured }));
    const chips = [...document.querySelectorAll('#rd-section-skills .rd-chip')];
    expect(chips.map(c => c.textContent.replace(/[^A-Za-z]/g, ''))).toEqual(expect.arrayContaining([expect.stringContaining('Python'), expect.stringContaining('Kafka')]));
    expect(document.querySelector('[data-rd-item="skill_py"] [aria-label^="Undo"]')).toBeTruthy();  // decided: only undo
    expect(document.querySelector('[data-rd-item="skill_k"] [aria-label="Verify"]')).toBeTruthy();
    expect(document.getElementById('rd-section-certifications')).toBeNull();  // empty and optional: out of the way
    expect(document.querySelector('[data-act="rd-add-section"][data-section="certifications"]')).toBeTruthy();
    ui.A['rd-add-section']({ dataset: { section: 'certifications' } });
    expect(ui.RD.editing).toBe('certifications');
    expect(document.getElementById('rd-certifications-0-name')).toBeTruthy();
    ui.A['rd-cancel']();
    expect(document.querySelector('[data-act="rd-next"]').textContent).toContain('2 left');
    ui.A['rd-next']();
    expect(document.activeElement.getAttribute('aria-label')).toBe('Verify');
    expect(document.activeElement.closest('[data-rd-item]').dataset.rdItem).toBe('summary');
    expect(document.querySelector('label.rd-upload input#resume-file')).toBeTruthy();  // upload from the library header
  });
  it('handles no resume and load failures', async () => {
    route({ 'GET /api/settings/resume': { active: null, versions: [], policy: {} } });
    section('resume-data'); await settle();
    expect(document.getElementById('view').innerHTML).toContain('Your experience starts here');
    expect(document.getElementById('rd-detail')).toBeNull();
    route({ 'GET /api/settings/resume': { __status: 500, body: { detail: 'boom' } } });
    await ui.A['rd-reload'](); 
    expect(document.getElementById('view').innerHTML).toContain('could not be loaded');
  });
});

describe('structured job description', () => {
  const requirement = (id, level, value, source) => ({ id, type: 'SKILL', value, level, confidence: 0.9, derived: false, source_text: source, source_section: 'Requirements' });
  function payload(overrides = {}) {
    return {
      job_id: 7, status: 'PARSED', version: 'v1', parsed_at: '2026-09-18T10:00:00',
      structured_description: {
        job_identity: { title: 'Data Platform Lead', company: 'Globex', seniority: 'Lead', remote_type: 'hybrid' },
        role_summary: 'Own the data platform.', responsibilities: ['Own the Kafka pipelines'],
        required_skills: ['Strong experience with Python is required.'], preferred_skills: ['Familiarity with Kubernetes is a plus.'],
        experience_requirements: { minimum_years: 8, maximum_years: null, leadership_required: true, management_required: null },
        technical_domains: ['streaming'], leadership_expectations: ['Mentor engineers'], education_requirements: [],
        location_requirements: { country: 'Singapore', city: null, relocation_supported: null, visa_sponsorship: null, work_authorization_required: null },
        compensation: { min: 180000, max: 220000, currency: 'USD', period: 'year', source: 'job_description' },
        hard_requirements: [], nice_to_have: [], keywords: [], company_context: [],
        derived_signals: { likely_interview_domains: ['distributed systems'], likely_system_design_topics: [], likely_leadership_topics: [], likely_business_topics: [] },
      },
      requirements: [requirement('req_001', 'REQUIRED', 'Strong experience with Python is required.', 'Strong experience with Python is required.'),
        requirement('req_002', 'PREFERRED', 'Familiarity with Kubernetes is a plus.', 'Familiarity with Kubernetes is a plus.')],
      counts: { REQUIRED: 1, PREFERRED: 1, INFORMATIONAL: 0, UNKNOWN: 0 },
      candidate: null, has_candidate: false, parse_task: { status: 'COMPLETED', attempt_count: 1, last_error: null },
      derived_label: 'likely / inferred preparation topics, not stated by the employer', normalized_description: 'Requirements', structured_hash: 'a'.repeat(64),
      ...overrides,
    };
  }
  function settle() { return new Promise(r => setTimeout(r, 0)); }
  function routes(map) {
    fetchMock.mockImplementation(async (url, opt = {}) => {
      const key = `${opt.method || 'GET'} ${url}`;
      const hit = Object.entries(map).find(([k]) => key === k);
      if (!hit) return response({});
      const value = typeof hit[1] === 'function' ? hit[1](JSON.parse(opt.body || 'null')) : hit[1];
      return response(value);
    });
  }
  async function open(data = payload()) {
    routes({ 'GET /api/jobs/7/structured-description': data });
    ui.S.screen = 'discovery'; ui.S.jobOpen = 7; ui.S.jobTab = 'structured';
    ui.D.jobModal = { id: 7, title: 'Data Platform Lead', company: 'Globex', location: 'Singapore', board: 'linkedin',
      posted: 'today', fit: 80, fit_analysis: {}, matches: [], gaps: [], missing: [], keywords: [], wizard: [],
      gap_notes: [], status: 'discovered', description: 'raw', notes: '', summary: '', resume_id: null, rank_pct: null };
    ui.JD.data = null; ui.JD.draft = null; ui.JD.view = 'summary'; ui.JD.jobId = null; ui.JD.error = '';
    ui.render(); await settle();
  }

  it('shows requirements by level with the employer sentence behind each one', async () => {
    await open();
    const view = document.body.innerHTML;
    expect(view).toContain('Structured JD');
    expect(view).toContain('1 required · 1 preferred');
    expect(document.querySelector('[data-jd-req="req_001"]').textContent).toContain('Required');
    expect(document.querySelector('[data-jd-req="req_002"]').textContent).toContain('Preferred');
    expect(document.querySelector('[data-jd-req="req_001"] blockquote').textContent).toContain('Strong experience with Python');
    // Derived topics are labelled as inferred, never as employer statements.
    const derived = document.querySelector('[data-jd-derived]').textContent;
    expect(derived).toContain('Inferred');
    expect(derived).toContain('not stated by the employer');
    expect(derived).toContain('distributed systems');
    // Unstated facts say so rather than guessing.
    expect(view).toContain('sponsorship not stated');
  });

  it('corrects a value and sends typed structured data', async () => {
    let sent;
    await open();
    routes({ 'PUT /api/jobs/7/structured-description': body => { sent = body; return payload({ status: 'MANUALLY_CORRECTED' }); } });
    ui.A['jd-edit']();
    input('jd-job_identity-seniority', 'Staff');
    input('jd-experience_requirements-minimum_years', '10');
    const tri = document.getElementById('jd-location_requirements-visa_sponsorship');
    tri.value = 'false'; tri.dispatchEvent(new Event('input', { bubbles: true }));
    await ui.A['jd-save']({ dataset: { id: '7' } });
    expect(sent.structured_description.job_identity.seniority).toBe('Staff');
    expect(sent.structured_description.experience_requirements.minimum_years).toBe(10);
    expect(sent.structured_description.location_requirements.visa_sponsorship).toBe(false);
    expect(sent.base_hash).toBe('a'.repeat(64));
    expect(ui.JD.data.status).toBe('MANUALLY_CORRECTED');
    expect(ui.JD.draft).toBeNull();
  });

  it('reloads instead of overwriting when a parse landed while editing', async () => {
    await open();
    const newer = payload({ structured_hash: 'b'.repeat(64) });
    newer.structured_description.job_identity.seniority = 'Principal';
    fetchMock.mockImplementation(async (url, opt = {}) => opt.method === 'PUT'
      ? response({ detail: 'This job description was parsed again while you were editing.' }, 409) : response(newer));
    ui.A['jd-edit']();
    input('jd-job_identity-seniority', 'Staff');
    await ui.A['jd-save']({ dataset: { id: '7' } });
    expect(ui.JD.draft).toBeNull();
    expect(ui.JD.data.structured_description.job_identity.seniority).toBe('Principal');
    expect(document.body.innerHTML).toContain('parsed again while you were editing');
  });

  it('tells the user when a correction could not be applied to the requirements', async () => {
    await open();
    routes({ 'PUT /api/jobs/7/structured-description': () => ({ ...payload({ status: 'MANUALLY_CORRECTED' }), unmatched_corrections: ['Python'] }) });
    ui.A['jd-edit']();
    input('jd-required_skills', 'Go');
    await ui.A['jd-save']({ dataset: { id: '7' } });
    const notice = document.querySelector('[data-jd-unmatched]');
    expect(notice.textContent).toContain('not applied to the requirements');
    expect(notice.textContent).toContain('Python');
    ui.A['jd-edit'](); ui.A['jd-cancel']();
    expect(document.querySelector('[data-jd-unmatched]')).toBeNull();
  });

  it('compares a re-parse of a corrected job and sends only the chosen values', async () => {
    let decisions;
    const candidate = { version: 'v1', changes: [{ id: 'chg_1', path: 'job_identity.seniority', current: 'Staff', proposed: 'Lead', corrected: true }] };
    await open(payload({ status: 'MANUALLY_CORRECTED' }));
    routes({ 'POST /api/jobs/7/structured-description/reparse': payload({ status: 'MANUALLY_CORRECTED', candidate, has_candidate: true }),
      'POST /api/jobs/7/structured-description/reparse/resolve': body => { decisions = body.decisions; return payload(); } });
    await ui.A['jd-reparse']({ dataset: { id: '7' } });
    expect(document.querySelector('[data-jd-compare]').textContent).toContain('your correction');
    ui.A['jd-decide']({ dataset: { change: 'chg_1', choice: 'use_new' } });
    await ui.A['jd-resolve']({ dataset: {} });
    expect(decisions).toEqual({ chg_1: 'use_new' });
  });

  it('explains an unparsed posting and shows the change history', async () => {
    await open(payload({ status: 'RAW', structured_description: null, requirements: [], parse_task: { status: 'PENDING', attempt_count: 0, last_error: null } }));
    expect(document.body.innerHTML).toContain('queued for parsing');
    expect(document.querySelector('[data-act="jd-edit"]').disabled).toBe(true);
    routes({ 'GET /api/jobs/7/structured-description/history': { items: [{ id: 1, field_path: 'job_identity.seniority', old_value: 'Lead', new_value: 'Staff', change_source: 'USER', created_at: '2026-09-18T11:00:00' }] } });
    await ui.A['jd-history']({ dataset: { id: '7' } });
    expect(document.querySelector('[data-jd-change-source="USER"]').textContent).toContain('job_identity.seniority');
  });
});

// Settings → AI agents & automation: what happens now in one sentence, presets that fill the choices, each setting's
// meaning beside its dropdown, prerequisites spelt out, and only the changed values sent.
describe('AI agents & automation', () => {
  const choice = (value, label, help = `${label} means this`) => ({ value, label, help });
  function automation(editable = true, environment = {}) {
    const item = (name, group, kind, value, choices = [], extra = {}) => ({ name, env: name.toUpperCase(), group, label: name, help: `${name} help`, kind, needs: null, min: 1, max: 300, choices, value, default: value, overridden: false, ...extra });
    const bool = [choice('false', 'Off'), choice('true', 'On')];
    return { editable, groups: [{ id: 'graphs', label: 'Agent graphs', help: '' }, { id: 'applications', label: 'Applications', help: '' }, { id: 'browser', label: 'Browser', help: '' }, { id: 'retention', label: 'Checkpoints and tracing', help: '' }],
      settings: [item('agent_graphs_enabled', 'graphs', 'bool', 'false', bool),
        item('qualification_graph_mode', 'graphs', 'choice', 'off', [choice('off', 'Off'), choice('shadow', 'Shadow'), choice('on', 'On')]),
        item('application_execution_mode', 'applications', 'choice', 'off', [choice('off', 'Off'), choice('observe', 'Read forms'), choice('assist', 'Fill forms, I submit'), choice('guarded_auto', 'Auto submit')]),
        item('application_max_actions', 'applications', 'int', '60'),
        item('browser_use_webui_enabled', 'browser', 'bool', 'false', bool),
        item('browser_use_headless', 'browser', 'bool', 'true', [choice('false', 'Visible'), choice('true', 'Hidden')]),
        item('agent_langsmith_export_enabled', 'retention', 'bool', 'false', bool)],
      presets: [{ id: 'manual', label: 'I apply myself', help: '', values: { agent_graphs_enabled: 'false', application_execution_mode: 'off' } },
        { id: 'fill', label: 'Fill forms; I submit', help: '', values: { agent_graphs_enabled: 'true', application_execution_mode: 'assist', browser_use_webui_enabled: 'true', browser_use_headless: 'false' } }],
      environment: { postgres: true, browser_use_python: true, browser_use_ready: true, langsmith_api_key: false, ...environment } };
  }
  const view = () => document.getElementById('view');

  it('says what happens now, marks the matching preset and explains the chosen option beside each control', () => {
    ui.D.automation = automation();
    section('automation');
    expect(view().querySelector('.am-summary-head').textContent).toBe('You apply yourself');
    expect(view().querySelector('.am-preset.is-selected').textContent).toContain('I apply myself');
    expect(view().querySelector('[data-automation="application_execution_mode"] .am-choice').textContent).toBe('Off means this');
    expect(view().querySelector('[data-automation="application_execution_mode"] code').textContent).toBe('APPLICATION_EXECUTION_MODE');
    expect(view().querySelector('[data-settings-details="automation-advanced"] [data-automation="agent_langsmith_export_enabled"]')).not.toBeNull();
    expect(view().querySelector('.am-footer.is-dirty')).toBeNull();  // nothing to save
  });

  it('fills the choices from a preset, warns about what is missing, and saves only the changes', async () => {
    ui.D.automation = automation(true, { browser_use_python: false, browser_use_ready: false });
    section('automation');
    await ui.A['automation-preset']({ dataset: { id: 'fill' } });
    expect(view().querySelector('.am-summary .caps').textContent).toBe('With your unsaved changes');
    expect(view().querySelector('.am-summary-head').textContent).toBe('Forms are filled, you submit');
    expect(view().querySelector('[data-automation="browser_use_webui_enabled"] .am-warn').textContent).toContain('BROWSER_USE_PYTHON');
    expect(view().querySelector('.am-footer.is-dirty span').textContent).toContain('4 unsaved changes');
    await ui.A['automation-reset']({ dataset: { name: 'browser_use_headless' } });
    expect(view().querySelector('.am-footer span').textContent).toContain('3 unsaved changes');
    fetchMock.mockImplementation(async () => response(automation()));
    await ui.A['automation-save']();
    const [, options] = fetchMock.mock.calls.find(([url, o]) => url === '/api/settings/automation' && o?.method === 'PUT');
    expect(JSON.parse(options.body)).toEqual({ values: { agent_graphs_enabled: 'true', application_execution_mode: 'assist', browser_use_webui_enabled: 'true' } });
  });

  it('dims a graph mode while LangGraph is off and says so when it is chosen', () => {
    ui.D.automation = automation();
    ui.S.forms.automation = { qualification_graph_mode: 'on' };
    section('automation');
    const row = view().querySelector('[data-automation="qualification_graph_mode"]');
    expect(row.classList.contains('am-row-inactive')).toBe(true);
    expect(row.querySelector('.am-warn').textContent).toContain('Turn on LangGraph');
  });

  it('takes a LangSmith key as a password, sends only a typed one and removes a saved one', async () => {
    const withKey = extra => { const a = automation(); a.settings.push({ name: 'agent_langsmith_api_key', env: 'AGENT_LANGSMITH_API_KEY', group: 'retention', label: 'LangSmith API key', help: '', kind: 'secret', needs: null, min: 0, max: 0, choices: [], value: '', default: '', overridden: false, set: false, env_set: false, ...extra }); return a; };
    ui.D.automation = withKey();
    ui.S.forms.automation = { agent_langsmith_export_enabled: 'true' };
    section('automation');
    const input = view().querySelector('#automation-agent_langsmith_api_key');
    expect(input.type).toBe('password');
    expect(input.getAttribute('autocomplete')).toBe('new-password');
    expect(view().querySelector('[data-automation="agent_langsmith_export_enabled"] .am-warn').textContent).toContain('Enter a LangSmith API key below');
    ui.S.forms.automation = { agent_langsmith_export_enabled: 'true', agent_langsmith_api_key: '  lsv2_pt_typed  ' };
    section('automation');
    expect(view().querySelector('[data-automation="agent_langsmith_export_enabled"] .am-warn')).toBeNull();
    fetchMock.mockImplementation(async () => response(withKey({ overridden: true, set: true })));
    await ui.A['automation-save']();
    const put = () => fetchMock.mock.calls.filter(([url, o]) => url === '/api/settings/automation' && o?.method === 'PUT').map(([, o]) => JSON.parse(o.body));
    expect(put()[0]).toEqual({ values: { agent_langsmith_export_enabled: 'true', agent_langsmith_api_key: 'lsv2_pt_typed' } });
    section('automation');
    expect(view().querySelector('#automation-agent_langsmith_api_key').value).toBe('');  // never shown again
    expect(view().querySelector('#automation-agent_langsmith_api_key').placeholder).toContain('Saved');
    expect(view().querySelector('.am-footer.is-dirty')).toBeNull();  // an empty key field is not a change
    fetchMock.mockImplementation(async () => response(withKey()));
    await ui.A['automation-clear-secret']({ dataset: { name: 'agent_langsmith_api_key' } });
    expect(put()[1]).toEqual({ values: { agent_langsmith_api_key: '' } });
  });

  it('picks a LangSmith region from a list, or types another address under Custom address', async () => {
    const regions = [choice('https://api.smith.langchain.com', 'US'), choice('https://eu.api.smith.langchain.com', 'EU'), choice('https://apac.api.smith.langchain.com', 'APAC')];
    const withRegion = (value = 'https://api.smith.langchain.com') => { const a = automation(); a.settings.push({ name: 'agent_langsmith_endpoint', env: 'AGENT_LANGSMITH_ENDPOINT', group: 'retention', label: 'LangSmith region', help: '', kind: 'text', needs: null, min: 0, max: 0, choices: regions, value, default: 'https://api.smith.langchain.com', overridden: false }); return a; };
    ui.D.automation = withRegion();
    section('automation');
    const row = () => view().querySelector('[data-automation="agent_langsmith_endpoint"]');
    expect(row().querySelector('label').getAttribute('for')).toBe('automation-agent_langsmith_endpoint-choice');
    expect(row().querySelector('select').value).toBe('https://api.smith.langchain.com');
    expect(row().querySelector('input')).toBeNull();
    await ui.A['automation-choice']({ dataset: { name: 'agent_langsmith_endpoint' }, value: 'https://apac.api.smith.langchain.com' });
    expect(row().querySelector('select').value).toBe('https://apac.api.smith.langchain.com');
    expect(row().querySelector('.am-choice').textContent).toBe('APAC means this');
    await ui.A['automation-choice']({ dataset: { name: 'agent_langsmith_endpoint' }, value: '__custom__' });
    expect(row().querySelector('#automation-agent_langsmith_endpoint').value).toBe('https://apac.api.smith.langchain.com');
    ui.S.forms.automation.agent_langsmith_endpoint = 'https://langsmith.example.com';
    fetchMock.mockImplementation(async () => response(withRegion('https://langsmith.example.com')));
    await ui.A['automation-save']();
    const [, options] = fetchMock.mock.calls.find(([url, o]) => url === '/api/settings/automation' && o?.method === 'PUT');
    expect(JSON.parse(options.body)).toEqual({ values: { agent_langsmith_endpoint: 'https://langsmith.example.com' } });
    section('automation');
    expect(row().querySelector('select').value).toBe('__custom__');  // a saved address that is no region
    expect(row().querySelector('#automation-agent_langsmith_endpoint').value).toBe('https://langsmith.example.com');
  });

  it('is read-only for someone who is not an admin', () => {
    ui.D.automation = automation(false);
    section('automation');
    expect(view().querySelector('.am-readonly')).not.toBeNull();
    expect(view().querySelector('#automation-application_execution_mode').disabled).toBe(true);
    expect(view().querySelector('[data-act="automation-save"]')).toBeNull();
    expect([...view().querySelectorAll('.am-preset')].every(b => b.disabled)).toBe(true);
  });
});
