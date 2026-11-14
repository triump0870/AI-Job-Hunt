/* A searchable picker for long model lists. It enhances any <select data-picker> in place: the native select stays in
   the DOM (hidden) as the single source of truth, so every existing data-form / data-field / data-act-change handler
   keeps working — picking an option sets select.value and fires the same input + change events a person would.
   Options may carry presentation hints, all optional:
     data-name  the primary text (default: the option label)
     data-sub   a secondary line, such as the provider model behind an alias
     data-host  local | cloud | mixed — shown as a coloured tag and offered as a filter
     data-meta  short right-aligned text, such as the context window
   The page re-renders with innerHTML, so a MutationObserver enhances each new select as it appears. Loaded after
   ai_models.js and before app.js. */
'use strict';
const MP = { open: null, seq: 0 };
const MP_HOSTS = { local: 'Local', cloud: 'Cloud', mixed: 'Local + Cloud' };
const mpEsc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const mpHostTag = h => MP_HOSTS[h] ? `<em class="aim-host ${h}">${MP_HOSTS[h]}</em>` : '';

// What an <option> shows; `group` is its <optgroup> label, if any.
function mpItem(o) {
  const d = o.dataset || {};
  return { value: o.value, label: o.textContent, name: d.name || o.textContent, sub: d.sub || '', host: d.host || '', meta: d.meta || '',
    group: o.parentElement?.tagName === 'OPTGROUP' ? o.parentElement.label : '', disabled: o.disabled, blank: o.value === '' };
}
const mpItems = select => Array.from(select.options).map(mpItem);

// Every word must appear somewhere in the name, secondary line, value or host.
function mpMatches(item, query) {
  const hay = `${item.name} ${item.sub} ${item.value} ${MP_HOSTS[item.host] || ''}`.toLowerCase();
  return String(query || '').toLowerCase().split(/\s+/).filter(Boolean).every(w => hay.includes(w));
}
function mpMark(text, query) {
  const words = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return mpEsc(text);
  const lower = text.toLowerCase(), hit = new Array(text.length).fill(false);
  for (const w of words) for (let i = lower.indexOf(w); i !== -1; i = lower.indexOf(w, i + 1)) hit.fill(true, i, i + w.length);
  let out = '', on = false;
  for (let i = 0; i < text.length; i++) {
    if (hit[i] !== on) { out += on ? '</mark>' : '<mark>'; on = hit[i]; }
    out += mpEsc(text[i]);
  }
  return out + (on ? '</mark>' : '');
}

/* ── the trigger that replaces the native select visually ── */
function mpTriggerHtml(select) {
  const o = select.options[select.selectedIndex], item = o ? mpItem(o) : null;
  const main = !item ? '<span class="mp-ph">Choose…</span>'
    : item.blank ? `<span class="mp-ph">${mpEsc(item.name)}</span>`
    : `<span class="mp-val">${item.group && !/^models$/i.test(item.group) ? `<span class="mp-kind">${mpEsc(item.group.replace(/s$/, ''))}</span>` : ''}<span class="mp-name">${mpEsc(item.name)}</span></span>${mpHostTag(item.host)}`;
  return `${main}<svg class="mp-chev" viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}
function mpLabelText(select) {
  if (select.getAttribute('aria-label')) return select.getAttribute('aria-label');
  const label = select.id && document.querySelector(`label[for="${CSS.escape(select.id)}"] > span`);
  return label ? label.textContent : 'Model';
}
function mpEnhance(select) {
  if (select.dataset.mpReady) return;
  select.dataset.mpReady = '1';
  select.classList.add('mp-native');
  select.tabIndex = -1;
  select.setAttribute('aria-hidden', 'true');
  const b = document.createElement('button');
  b.type = 'button';
  b.className = `mp-trigger${select.classList.contains('aim-inline') ? ' inline' : ''}`;
  b.setAttribute('role', 'combobox');
  b.setAttribute('aria-haspopup', 'listbox');
  b.setAttribute('aria-expanded', 'false');
  b.setAttribute('aria-label', mpLabelText(select));
  b.disabled = select.disabled;
  b.innerHTML = mpTriggerHtml(select);
  b.title = select.options[select.selectedIndex]?.textContent || '';
  select.after(b);
  // A <label for> click focuses the hidden select: hand focus to the trigger instead.
  select.addEventListener('focus', () => b.focus());
  select.addEventListener('change', () => { b.innerHTML = mpTriggerHtml(select); });
  b.addEventListener('click', () => (MP.open?.trigger === b ? mpClose(true) : mpOpen(select, b)));
  b.addEventListener('keydown', e => {
    if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) { e.preventDefault(); mpOpen(select, b); return; }
    // Typing on the closed trigger starts a search with that letter.
    if (e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) { e.preventDefault(); mpOpen(select, b, e.key); }
  });
  // Re-open after a re-render that replaced the select the person was picking from.
  if (MP.open && !MP.open.select.isConnected && MP.open.key === mpKey(select)) {
    MP.open.select = select; MP.open.trigger = b; b.setAttribute('aria-expanded', 'true'); mpPlace();
  }
}
// A selector that finds the same select again after the page re-renders it.
function mpKey(select) {
  if (select.id) return `#${CSS.escape(select.id)}`;
  return 'select' + Array.from(select.attributes).filter(a => a.name.startsWith('data-') && a.name !== 'data-mp-ready').map(a => `[${a.name}="${CSS.escape(a.value)}"]`).join('');
}
function mpScan(root = document) {
  if (root.matches?.('select[data-picker]')) mpEnhance(root);
  root.querySelectorAll?.('select[data-picker]:not([data-mp-ready])').forEach(mpEnhance);
}

/* ── the popover ── */
function mpOpen(select, trigger, query = '') {
  if (trigger.disabled) return;
  mpClose(false);
  const items = mpItems(select), hosts = Object.keys(MP_HOSTS).filter(h => items.some(i => i.host === h));
  const pop = document.createElement('div');
  const id = `mp-${++MP.seq}`;
  pop.className = 'mp-pop';
  pop.innerHTML = `<div class="mp-search"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M10.5 10.5L14 14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`
    + `<input type="text" class="mp-q" role="combobox" aria-expanded="true" aria-controls="${id}-list" aria-autocomplete="list" autocomplete="off" spellcheck="false" placeholder="Search ${items.filter(i => !i.blank).length} options…" aria-label="Search ${mpEsc(trigger.getAttribute('aria-label'))}"><kbd>esc</kbd></div>`
    + (hosts.length > 1 ? `<div class="mp-filters" role="group" aria-label="Where it runs">${['', ...hosts].map(h => `<button type="button" class="mp-chip${h ? '' : ' on'}" data-host="${h}" aria-pressed="${h ? 'false' : 'true'}">${h ? MP_HOSTS[h] : 'All'}</button>`).join('')}</div>` : '')
    + `<div class="mp-list" id="${id}-list" role="listbox" aria-label="${mpEsc(trigger.getAttribute('aria-label'))}"></div><div class="mp-foot"><span><kbd>↑</kbd><kbd>↓</kbd> move</span><span><kbd>↵</kbd> choose</span><span class="mp-count"></span></div>`;
  document.body.appendChild(pop);
  MP.open = { select, trigger, pop, id, key: mpKey(select), items, query, host: '', active: -1, shown: [] };
  trigger.setAttribute('aria-expanded', 'true');
  const input = pop.querySelector('.mp-q');
  input.value = query;
  // The search box's typing is the picker's own: keep it from the page's global input and shortcut handlers.
  input.addEventListener('input', e => { e.stopPropagation(); MP.open.query = input.value; MP.open.active = -1; mpDraw(); });
  input.addEventListener('keydown', e => { e.stopPropagation(); mpKeys(e); });
  pop.addEventListener('mousedown', e => { if (!e.target.closest('.mp-q')) e.preventDefault(); });  // keep focus in the search box
  pop.addEventListener('click', e => {
    const chip = e.target.closest('.mp-chip');
    if (chip) {
      MP.open.host = chip.dataset.host; MP.open.active = -1;
      pop.querySelectorAll('.mp-chip').forEach(c => { c.classList.toggle('on', c === chip); c.setAttribute('aria-pressed', String(c === chip)); });
      mpDraw(); return;
    }
    const opt = e.target.closest('.mp-opt');
    if (opt && opt.getAttribute('aria-disabled') !== 'true') mpChoose(Number(opt.dataset.i));
  });
  pop.addEventListener('mousemove', e => {
    const opt = e.target.closest('.mp-opt');
    if (opt && Number(opt.dataset.i) !== MP.open.active) mpActivate(Number(opt.dataset.i), false);
  });
  mpDraw(true);
  mpPlace();
  input.focus();
}
function mpDraw(first = false) {
  const o = MP.open, list = o.pop.querySelector('.mp-list');
  // The blank option ("None", "Not set") always stays reachable at the top; it is not a search hit.
  o.shown = o.items.map((it, i) => ({ ...it, i })).filter(it => it.blank ? !o.query && !o.host : (!o.host || it.host === o.host) && mpMatches(it, o.query));
  const current = o.select.value;
  let html = '', group = null;
  for (const it of o.shown) {
    if (it.group !== group) { group = it.group; if (group) html += `<div class="mp-group" role="presentation">${mpEsc(group)}</div>`; }
    const sel = it.value === current;
    html += `<div class="mp-opt${it.blank ? ' blank' : ''}${sel ? ' sel' : ''}" role="option" id="${o.id}-o${it.i}" data-i="${it.i}" aria-selected="${sel}"${it.disabled ? ' aria-disabled="true"' : ''}>`
      + `<span class="mp-check" aria-hidden="true">${sel ? '<svg viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>' : ''}</span>`
      + `<span class="mp-text"><span class="mp-oname">${mpMark(it.name, o.query)}</span>${it.sub ? `<span class="mp-sub">${mpMark(it.sub, o.query)}</span>` : ''}</span>`
      + `${it.meta ? `<span class="mp-meta">${mpEsc(it.meta)}</span>` : ''}${mpHostTag(it.host)}</div>`;
  }
  const total = o.items.filter(i => !i.blank).length, hits = o.shown.filter(i => !i.blank).length;
  list.innerHTML = html || `<div class="mp-empty">No match for “${mpEsc(o.query)}”${o.host ? ` in ${MP_HOSTS[o.host]}` : ''}.<span>Try part of the name, such as “gpt” or “120b”.</span></div>`;
  o.pop.querySelector('.mp-count').textContent = o.query || o.host ? `${hits} of ${total}` : `${total} options`;
  if (first) {
    const at = o.shown.findIndex(it => it.value === current);
    mpActivate(o.shown[at >= 0 ? at : 0]?.i ?? -1, true, 'center');
  } else mpActivate(o.shown.find(it => !it.disabled && (!it.blank || !o.query))?.i ?? -1, true);
}
function mpActivate(i, scroll = true, block = 'nearest') {
  const o = MP.open; o.active = i;
  o.pop.querySelectorAll('.mp-opt.on').forEach(el => el.classList.remove('on'));
  const el = i >= 0 && o.pop.querySelector(`#${o.id}-o${i}`);
  const input = o.pop.querySelector('.mp-q');
  if (!el) { input.removeAttribute('aria-activedescendant'); return; }
  el.classList.add('on');
  input.setAttribute('aria-activedescendant', el.id);
  if (scroll) el.scrollIntoView?.({ block });
}
function mpKeys(e) {
  const o = MP.open; if (!o) return;
  const usable = o.shown.filter(it => !it.disabled), at = usable.findIndex(it => it.i === o.active);
  const move = n => { e.preventDefault(); if (usable.length) mpActivate(usable[Math.max(0, Math.min(usable.length - 1, at + n))].i); };
  if (e.key === 'ArrowDown') move(1);
  else if (e.key === 'ArrowUp') move(-1);
  else if (e.key === 'PageDown') move(8);
  else if (e.key === 'PageUp') move(-8);
  else if (e.key === 'Home' && !o.query) { e.preventDefault(); if (usable[0]) mpActivate(usable[0].i); }
  else if (e.key === 'End' && !o.query) { e.preventDefault(); if (usable.length) mpActivate(usable.at(-1).i); }
  else if (e.key === 'Enter') { e.preventDefault(); if (o.active >= 0) mpChoose(o.active); }
  else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); if (o.query) { e.target.value = ''; o.query = ''; mpDraw(); } else mpClose(true); }
  else if (e.key === 'Tab') mpClose(true);  // back on the trigger, so Tab moves on from there
}
function mpChoose(i) {
  const o = MP.open, item = o.items[i];
  let select = o.select;
  if (!select.isConnected) select = document.querySelector(o.key) || select;
  mpClose(true);
  if (!item || select.value === item.value) return;
  select.value = item.value;
  select.dispatchEvent(new Event('input', { bubbles: true }));
  select.dispatchEvent(new Event('change', { bubbles: true }));
}
function mpClose(refocus) {
  const o = MP.open; if (!o) return;
  MP.open = null;
  o.pop.remove();
  o.trigger.setAttribute('aria-expanded', 'false');
  if (refocus) {
    // The trigger may have been re-rendered while open: focus the live one.
    const live = o.trigger.isConnected ? o.trigger : document.querySelector(o.key)?.nextElementSibling;
    live?.focus?.();
  }
}
// Below the trigger, or above it when there is more room there; at least as wide as the trigger.
function mpPlace() {
  const o = MP.open; if (!o) return;
  if (!o.trigger.isConnected) {
    const fresh = document.querySelector(o.key);
    if (!fresh) { mpClose(false); return; }
    return;  // the observer re-binds it
  }
  const r = o.trigger.getBoundingClientRect(), vw = window.innerWidth, vh = window.innerHeight, gap = 6;
  const width = Math.min(vw - 16, Math.max(r.width, 340));
  const below = vh - r.bottom - gap - 8, above = r.top - gap - 8, up = below < 280 && above > below;
  const room = Math.max(160, Math.min(440, up ? above : below));
  Object.assign(o.pop.style, { width: `${width}px`, left: `${Math.max(8, Math.min(r.left, vw - width - 8))}px`, maxHeight: `${room}px`,
    top: up ? '' : `${r.bottom + gap}px`, bottom: up ? `${vh - r.top + gap}px` : '' });
  o.pop.classList.toggle('up', up);
}

document.addEventListener('mousedown', e => {
  if (MP.open && !MP.open.pop.contains(e.target) && !MP.open.trigger.contains(e.target)) mpClose(false);
}, true);
window.addEventListener('resize', () => mpPlace());
document.addEventListener('scroll', e => { if (MP.open && !MP.open.pop.contains(e.target)) mpPlace(); }, true);
if (typeof MutationObserver === 'function') {
  new MutationObserver(records => {
    for (const r of records) for (const n of r.addedNodes) if (n.nodeType === 1) mpScan(n);
    if (MP.open) mpPlace();
  }).observe(document.documentElement, { childList: true, subtree: true });
}
mpScan();
