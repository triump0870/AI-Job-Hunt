import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

let mp;
const flush = () => new Promise(r => setTimeout(r, 0));
const opt = (value, label, data = {}) => `<option value="${value}"${Object.entries(data).map(([k, v]) => ` data-${k}="${v}"`).join('')}>${label}</option>`;
const html = (selected = '') => `<label for="m"><span>Backup 1</span></label><select class="select" id="m" data-form="aiw" data-field="backup_0" data-picker>
  ${opt('', 'None')}
  <optgroup label="Models">
    ${opt('groq-b4', 'Groq B4 · Cloud', { name: 'Groq B4', sub: 'groq/openai/gpt-oss-120b', host: 'cloud', meta: '128K ctx' })}
    ${opt('gpt-oss:20b', 'gpt-oss:20b · Local', { name: 'gpt-oss:20b', host: 'local', meta: '24K ctx' })}
    ${opt('gemma4:31b-cloud', 'gemma4:31b-cloud · Cloud', { name: 'gemma4:31b-cloud', host: 'cloud' })}
  </optgroup></select>`.replace(`value="${selected}"`, `value="${selected}" selected`);
const select = () => document.getElementById('m');
const trigger = () => document.querySelector('.mp-trigger');
const pop = () => document.querySelector('.mp-pop');
const names = () => [...document.querySelectorAll('.mp-opt:not(.blank) .mp-oname')].map(x => x.textContent);
const key = (el, k) => el.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true }));
const type = q => { const i = pop().querySelector('.mp-q'); i.value = q; i.dispatchEvent(new Event('input', { bubbles: true })); };

beforeAll(() => {
  const context = createContext({ document, window, MutationObserver, CSS: { escape: s => String(s).replace(/["\\#.:\[\]]/g, '\\$&') }, Event, setTimeout, console });
  runInContext(readFileSync('app/static/model_picker.js', 'utf8'), context, { filename: resolve('app/static/model_picker.js') });
  mp = runInContext('({ MP, mpClose })', context);
});
beforeEach(async () => {
  mp.mpClose(false);
  document.body.innerHTML = `<div id="view">${html('groq-b4')}</div>`;
  await flush();
});

describe('model picker', () => {
  it('hides the native select behind a trigger that shows the chosen model and where it runs', () => {
    expect(select().classList.contains('mp-native')).toBe(true);
    expect(trigger().getAttribute('aria-label')).toBe('Backup 1');
    expect(trigger().querySelector('.mp-name').textContent).toBe('Groq B4');
    expect(trigger().querySelector('.aim-host.cloud').textContent).toBe('Cloud');
  });

  it('opens a grouped list with the current choice checked, the alias target and context', () => {
    trigger().click();
    expect(pop().querySelector('.mp-group').textContent).toBe('Models');
    expect(names()).toEqual(['Groq B4', 'gpt-oss:20b', 'gemma4:31b-cloud']);
    const sel = pop().querySelector('.mp-opt.sel');
    expect(sel.getAttribute('aria-selected')).toBe('true');
    expect(sel.querySelector('.mp-sub').textContent).toBe('groq/openai/gpt-oss-120b');
    expect(sel.querySelector('.mp-meta').textContent).toBe('128K ctx');
    expect(pop().querySelector('.mp-count').textContent).toBe('3 options');
  });

  it('searches every word across name, provider model and host, and highlights the hits', () => {
    trigger().click();
    type('oss 120');
    expect(names()).toEqual(['Groq B4']);
    expect(pop().querySelector('.mp-sub mark').textContent).toBe('oss');
    expect(pop().querySelector('.mp-count').textContent).toBe('1 of 3');
    type('local');
    expect(names()).toEqual(['gpt-oss:20b']);
    type('nothing-like-this');
    expect(pop().querySelector('.mp-empty').textContent).toContain('No match');
  });

  it('filters by where the model runs', () => {
    trigger().click();
    pop().querySelector('.mp-chip[data-host="local"]').click();
    expect(names()).toEqual(['gpt-oss:20b']);
    expect(pop().querySelector('.mp-opt.blank')).toBeNull();
  });

  it('chooses with the keyboard and fires the input and change events existing handlers listen for', () => {
    const changed = vi.fn(), input = vi.fn();
    document.addEventListener('change', changed); document.addEventListener('input', input);
    trigger().click();
    type('gemma');
    key(pop().querySelector('.mp-q'), 'Enter');
    expect(select().value).toBe('gemma4:31b-cloud');
    expect(changed).toHaveBeenCalledTimes(1);
    expect(input).toHaveBeenCalledTimes(1);  // the search box's own typing stays inside the picker
    expect(pop()).toBeNull();
    expect(trigger().querySelector('.mp-name').textContent).toBe('gemma4:31b-cloud');
    document.removeEventListener('change', changed); document.removeEventListener('input', input);
  });

  it('moves with the arrows and picks None to clear', () => {
    trigger().click();
    const q = pop().querySelector('.mp-q');
    key(q, 'Home');
    key(q, 'Enter');
    expect(select().value).toBe('');
    expect(trigger().querySelector('.mp-ph').textContent).toBe('None');
    trigger().click();
    key(pop().querySelector('.mp-q'), 'ArrowDown');
    key(pop().querySelector('.mp-q'), 'ArrowDown');
    key(pop().querySelector('.mp-q'), 'Enter');
    expect(select().value).toBe('gpt-oss:20b');
  });

  it('Escape clears the search first, then closes; a click outside closes', () => {
    trigger().click();
    type('groq');
    key(pop().querySelector('.mp-q'), 'Escape');
    expect(pop().querySelector('.mp-q').value).toBe('');
    key(pop().querySelector('.mp-q'), 'Escape');
    expect(pop()).toBeNull();
    trigger().click();
    document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    expect(pop()).toBeNull();
  });

  it('typing on the closed trigger starts a search with that letter', () => {
    key(trigger(), 'g');
    expect(pop().querySelector('.mp-q').value).toBe('g');
  });

  it('keeps working when the page re-renders the select while the list is open', async () => {
    trigger().click();
    document.getElementById('view').innerHTML = html('groq-b4');
    await flush();
    expect(pop()).not.toBeNull();
    expect(trigger().getAttribute('aria-expanded')).toBe('true');
    type('oss:20');
    key(pop().querySelector('.mp-q'), 'Enter');
    expect(select().value).toBe('gpt-oss:20b');
  });

  it('a disabled select gives a disabled trigger', async () => {
    document.getElementById('view').innerHTML = html().replace('data-picker', 'data-picker disabled');
    await flush();
    expect(trigger().disabled).toBe(true);
  });
});
