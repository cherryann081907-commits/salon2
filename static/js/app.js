// Lumora Salon — client-side interactions: 3D tilt, live slot picker, schedule board,
// dialogs, password helpers and toasts.

const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const csrf = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
const POLL_MS = 4000;

/* ---------- toasts ---------- */
function toast(message, type = 'success', ms = 4500) {
  let stack = document.querySelector('.toast-stack');
  if (!stack) { stack = document.createElement('div'); stack.className = 'toast-stack'; stack.setAttribute('role', 'status'); document.body.appendChild(stack); }
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = message;
  stack.appendChild(el);
  setTimeout(() => { el.style.transition = 'opacity .4s'; el.style.opacity = '0'; setTimeout(() => el.remove(), 400); }, ms);
}

document.addEventListener('DOMContentLoaded', () => {
  animateCounters();
  setupScrollReveal();
  setupTilt();
  setupScene();
  setupTabs();
  setupStylistPick();
  setupPasswordHelpers();
  setupDialogs();
  document.querySelectorAll('[data-slot-picker]').forEach(f => new SlotPicker(f));
  const board = document.getElementById('board');
  if (board) new ScheduleBoard(board);

  document.querySelectorAll('.flash').forEach(el => setTimeout(() => {
    el.style.transition = 'opacity .4s ease'; el.style.opacity = '0'; setTimeout(() => el.remove(), 400);
  }, 5500));
});

/* ---------- tabs (with #hash support) ---------- */
function setupTabs() {
  const activate = (name) => {
    const btn = document.querySelector(`.tab-btn[data-tab="${name}"]`);
    if (!btn) return;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b === btn));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  };
  document.querySelectorAll('.tab-btn').forEach(btn => btn.addEventListener('click', () => { activate(btn.dataset.tab); history.replaceState(null, '', '#' + btn.dataset.tab); }));
  if (location.hash) activate(location.hash.slice(1));
}

function setupStylistPick() {
  document.querySelectorAll('.stylist-option input[type="radio"]').forEach(input => {
    input.addEventListener('change', () => {
      document.querySelectorAll(`input[name="${input.name}"]`).forEach(r => r.closest('.stylist-option').classList.toggle('selected', r.checked));
    });
  });
}

/* ---------- tables / collapsibles ---------- */
function filterTable(inputId, tableId) {
  const q = document.getElementById(inputId).value.toLowerCase();
  document.querySelectorAll(`#${tableId} tbody tr`).forEach(r => { r.style.display = r.textContent.toLowerCase().includes(q) ? '' : 'none'; });
}
function toggleCollapse(id) { const el = document.getElementById(id); if (el) el.classList.toggle('open'); }

/* ---------- 3D: card tilt + hero scene parallax ---------- */
function setupTilt() {
  if (reducedMotion || matchMedia('(hover: none)').matches) return;
  document.querySelectorAll('.svc-card, .stylist-card .stylist-portrait, .stat-tile, .auth-card').forEach(el => {
    el.classList.add('tilt');
    el.addEventListener('mousemove', e => {
      const r = el.getBoundingClientRect();
      const x = (e.clientX - r.left) / r.width - .5, y = (e.clientY - r.top) / r.height - .5;
      el.style.transition = 'transform .08s linear, box-shadow .25s ease';
      el.style.transform = `perspective(900px) rotateX(${(-y * 7).toFixed(2)}deg) rotateY(${(x * 9).toFixed(2)}deg) translateY(-4px)`;
    });
    el.addEventListener('mouseleave', () => { el.style.transition = 'transform .5s cubic-bezier(.2,.8,.2,1), box-shadow .25s ease'; el.style.transform = ''; });
  });
}
function setupScene() {
  const scene = document.querySelector('.scene');
  if (!scene || reducedMotion) return;
  const stage = scene.querySelector('.scene-stage');
  scene.addEventListener('mousemove', e => {
    const r = scene.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width - .5, y = (e.clientY - r.top) / r.height - .5;
    stage.style.setProperty('--ry', `${(-18 + x * 22).toFixed(1)}deg`);
    stage.style.setProperty('--rx', `${(12 - y * 16).toFixed(1)}deg`);
  });
  scene.addEventListener('mouseleave', () => { stage.style.setProperty('--ry', '-18deg'); stage.style.setProperty('--rx', '12deg'); });
}

function animateCounters() {
  document.querySelectorAll('[data-count]').forEach(el => {
    const target = parseInt(el.dataset.count, 10) || 0;
    if (reducedMotion) { el.textContent = target; return; }
    let start = null;
    const tick = now => {
      if (!start) start = now;
      const p = Math.min((now - start) / 900, 1);
      el.textContent = Math.floor((1 - Math.pow(1 - p, 3)) * target);
      if (p < 1) requestAnimationFrame(tick); else el.textContent = target;
    };
    requestAnimationFrame(tick);
  });
}

function setupScrollReveal() {
  const targets = document.querySelectorAll('.svc-card, .stylist-card, .stat-tile, .card');
  if (!targets.length) return;
  if (reducedMotion || !('IntersectionObserver' in window)) { targets.forEach(el => el.classList.add('in-view')); return; }
  targets.forEach((el, i) => { el.classList.add('reveal'); el.style.transitionDelay = `${(i % 6) * 55}ms`; });
  const io = new IntersectionObserver(entries => entries.forEach(en => { if (en.isIntersecting) { en.target.classList.add('in-view'); io.unobserve(en.target); } }), { threshold: .08 });
  targets.forEach(el => io.observe(el));
}

/* ---------- password helpers: show/hide, strength, generate ---------- */
function scorePassword(pw) {
  let s = 0;
  if (pw.length >= 8) s++;
  if (pw.length >= 12) s++;
  if (/[A-Za-z]/.test(pw) && /\d/.test(pw)) s++;
  if (/[A-Z]/.test(pw) && /[a-z]/.test(pw)) s++;
  if (/[^A-Za-z0-9]/.test(pw)) s++;
  return Math.min(s, 4);
}
function setupPasswordHelpers() {
  document.querySelectorAll('.pw-toggle').forEach(btn => btn.addEventListener('click', () => {
    const input = btn.parentElement.querySelector('input');
    input.type = input.type === 'password' ? 'text' : 'password';
    btn.textContent = input.type === 'password' ? 'Show' : 'Hide';
  }));
  document.querySelectorAll('input[data-meter]').forEach(input => {
    const meter = document.getElementById(input.dataset.meter);
    const hint = meter && meter.nextElementSibling;
    if (!meter) return;
    const bar = meter.firstElementChild;
    const colors = ['#C1503E', '#C1503E', '#D2A12E', '#8DAA55', '#4F7A47'];
    const words = ['Too short', 'Weak', 'Okay', 'Strong', 'Excellent'];
    input.addEventListener('input', () => {
      const pw = input.value, sc = pw ? scorePassword(pw) : 0;
      bar.style.width = pw ? `${(sc + 1) * 20}%` : '0';
      bar.style.background = colors[sc];
      if (hint) hint.textContent = pw ? (pw.length < 8 || !(/[A-Za-z]/.test(pw) && /\d/.test(pw)) ? 'Needs 8+ characters with a letter and a number' : words[sc]) : '';
    });
  });
  document.querySelectorAll('[data-generate-for]').forEach(btn => btn.addEventListener('click', () => {
    const input = document.getElementById(btn.dataset.generateFor);
    const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
    const buf = new Uint32Array(10); crypto.getRandomValues(buf);
    let pw = Array.from(buf, n => chars[n % chars.length]).join('');
    if (!/\d/.test(pw)) pw = pw.slice(0, 9) + '7';
    if (!/[A-Za-z]/.test(pw)) pw = 'k' + pw.slice(1);
    input.value = pw; input.type = 'text';
    input.dispatchEvent(new Event('input'));
  }));
  // confirm-match feedback
  document.querySelectorAll('form[data-pw-form]').forEach(form => {
    form.addEventListener('submit', e => {
      const a = form.querySelector('[name="new_password"], [name="password"]');
      const b = form.querySelector('[name="confirm_password"]');
      if (a && b && a.value !== b.value) { e.preventDefault(); toast('The passwords do not match.', 'error'); b.focus(); }
    });
  });
}

/* ---------- dialogs: edit forms prefilled from data-json, reset password ---------- */
function setupDialogs() {
  document.querySelectorAll('[data-open-dialog]').forEach(btn => btn.addEventListener('click', () => {
    const dlg = document.getElementById(btn.dataset.openDialog);
    if (!dlg) return;
    const form = dlg.querySelector('form');
    if (form) form.reset();
    if (btn.dataset.action && form) form.action = btn.dataset.action;
    if (btn.dataset.json && form) {
      const data = JSON.parse(btn.dataset.json);
      Object.entries(data).forEach(([k, v]) => {
        const els = form.querySelectorAll(`[name="${k}"]`);
        els.forEach(el => {
          if (el.type === 'checkbox') el.checked = Array.isArray(v) ? v.map(String).includes(el.value) : !!v;
          else if (el.type === 'radio') el.checked = String(el.value) === String(v);
          else el.value = v == null ? '' : v;
        });
      });
      const allDay = form.querySelector('[name="all_day"]');
      if (allDay) allDay.dispatchEvent(new Event('change'));
    }
    dlg.querySelectorAll('[data-fill]').forEach(el => { el.textContent = btn.dataset[el.dataset.fill] || ''; });
    dlg.querySelectorAll('.pw-meter > span').forEach(b => b.style.width = '0');
    dlg.showModal();
    const first = dlg.querySelector('input:not([type=hidden]):not([type=checkbox])'); if (first) first.focus();
  }));
  document.querySelectorAll('dialog [data-close]').forEach(b => b.addEventListener('click', () => b.closest('dialog').close()));
  document.querySelectorAll('dialog.modal').forEach(d => d.addEventListener('click', e => { if (e.target === d) d.close(); }));
  document.querySelectorAll('input[name="all_day"]').forEach(cb => {
    const sync = () => cb.closest('form').querySelectorAll('[data-time-field]').forEach(f => { f.disabled = cb.checked; f.closest('.field').style.opacity = cb.checked ? .45 : 1; });
    cb.addEventListener('change', sync); sync();
  });
}

/* =========================================================
   SlotPicker — live availability + double-booking protection
   ========================================================= */
class SlotPicker {
  constructor(form) {
    this.form = form;
    this.select = form.querySelector('[data-slot-select]');
    this.grid = form.querySelector('[data-slot-grid]');
    this.statusEl = form.querySelector('[data-slot-status]');
    this.submitBtn = form.querySelector('[type="submit"]');
    this.exclude = form.dataset.exclude || '';
    this.ajax = form.hasAttribute('data-ajax');
    this.initialTime = this.select.dataset.initial || '';
    this.timer = null; this.busy = false; this.lastSig = ''; this.fetching = false;

    ['change', 'input'].forEach(ev => form.addEventListener(ev, e => {
      if (['stylist_id', 'date', 'service_id'].includes(e.target.name)) { this.initialTime = ''; this.refresh(true); }
    }));
    this.select.addEventListener('change', () => { this.highlight(); });
    form.addEventListener('submit', e => this.onSubmit(e));
    document.addEventListener('visibilitychange', () => { if (!document.hidden) this.refresh(false); });
    this.timer = setInterval(() => { if (!document.hidden) this.refresh(false); }, POLL_MS);
    this.refresh(true);
  }
  val(name) {
    const el = this.form.querySelector(`[name="${name}"]:checked`) || this.form.querySelector(`[name="${name}"]`);
    return el ? el.value : '';
  }
  async refresh(manual) {
    const stylist = this.val('stylist_id'), day = this.val('date'), service = this.val('service_id');
    if (!stylist || !day || !service) {
      this.renderPlaceholder(!service ? 'Choose a service first' : !stylist ? 'Choose a stylist next' : 'Pick a date to see times');
      return;
    }
    if (this.fetching) return; this.fetching = true;
    const params = new URLSearchParams({ stylist_id: stylist, date: day, service_id: service });
    if (this.exclude) params.set('exclude', this.exclude);
    try {
      const res = await fetch('/api/slots?' + params, { headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' } });
      if (res.status === 401) { location.href = '/login'; return; }
      const data = await res.json();
      if (!data.ok) { this.renderPlaceholder(data.message || 'Unable to load times'); return; }
      this.render(data, manual);
    } catch (err) {
      this.setStatus('Connection lost — retrying…', false);
    } finally { this.fetching = false; }
  }
  renderPlaceholder(text) {
    this.select.innerHTML = '';
    const o = new Option(text, ''); this.select.add(o); this.select.disabled = true;
    this.grid.innerHTML = `<div class="slot-msg">${text}</div>`;
    this.setStatus('', true);
    this.lastSig = '';
  }
  render(data, manual) {
    if (!data.slots.length) { this.renderPlaceholder(data.message || 'No times on this day'); return; }
    const sig = JSON.stringify(data.slots.map(s => s.time + s.status));
    const prev = this.select.value || this.initialTime;
    const prevSlot = data.slots.find(s => s.time === prev);
    const lostSelection = prev && this.select.value === prev && prevSlot && prevSlot.status !== 'available';

    if (sig !== this.lastSig) {
      const labels = { available: 'Available', booked: 'Booked', blocked: 'Unavailable', past: 'Passed', too_long: 'Not enough time' };
      this.select.innerHTML = '';
      this.select.add(new Option(`Select a time (${data.available} available)`, ''));
      data.slots.forEach(s => {
        const o = new Option(`${s.label} — ${labels[s.status]}`, s.time);
        if (s.status !== 'available') o.disabled = true;
        this.select.add(o);
      });
      this.select.disabled = false;
      this.grid.innerHTML = '';
      data.slots.forEach(s => {
        const b = document.createElement('button');
        b.type = 'button'; b.dataset.time = s.time; b.className = `slot is-${s.status}`;
        b.innerHTML = `${s.label}<small>${labels[s.status]}</small>`;
        if (s.status === 'available') b.addEventListener('click', () => { this.select.value = s.time; this.highlight(); });
        else { b.disabled = true; b.setAttribute('aria-disabled', 'true'); }
        this.grid.appendChild(b);
      });
      this.lastSig = sig;
    }
    if (prevSlot && prevSlot.status === 'available') this.select.value = prev;
    else this.select.value = '';
    if (lostSelection) {
      toast(`${prevSlot.label} was just taken by someone else — please choose another time.`, 'warn', 6000);
      const chip = this.grid.querySelector(`[data-time="${prev}"]`); if (chip) chip.classList.add('is-flash');
    }
    this.initialTime = '';
    this.highlight();
    this.setStatus(`Live availability · ${data.available} open · updated ${data.server_time}`, true);
  }
  highlight() {
    this.grid.querySelectorAll('.slot').forEach(c => {
      const sel = c.dataset.time === this.select.value && c.classList.contains('is-available');
      c.classList.toggle('is-selected', sel);
    });
  }
  setStatus(text, ok) { if (this.statusEl) this.statusEl.innerHTML = text ? `<span class="live-dot ${ok ? '' : 'off'}"></span>${text}` : ''; }

  async onSubmit(e) {
    if (!this.select.value) { e.preventDefault(); toast('Please select an available time first.', 'error'); this.select.focus(); return; }
    if (!this.ajax) return;
    e.preventDefault();
    if (this.busy) return; this.busy = true;
    const btn = this.submitBtn; const label = btn.textContent; btn.disabled = true; btn.textContent = 'Booking…';
    try {
      const res = await fetch(this.form.action, { method: 'POST', body: new FormData(this.form), headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json', 'X-CSRFToken': csrf() } });
      const data = await res.json();
      if (data.ok) {
        toast(data.message, 'success', 3500);
        this.select.value = ''; await this.refresh(true);   // slot is now shown as Booked straight away
        setTimeout(() => { location.href = '/customer/dashboard#upcoming'; location.reload(); }, 1100);
      } else {
        toast(data.message || 'Could not book that time.', 'error', 6000);
        await this.refresh(true);
      }
    } catch (err) { toast('Network problem — please try again.', 'error'); }
    finally { this.busy = false; btn.disabled = false; btn.textContent = label; }
  }
}

/* =========================================================
   ScheduleBoard — admin / stylist day grid, auto-refreshing
   ========================================================= */
class ScheduleBoard {
  constructor(el) {
    this.el = el; this.dateInput = document.getElementById('board-date');
    this.statusEl = document.getElementById('board-status');
    this.sig = '';
    this.dateInput.addEventListener('change', () => { history.replaceState(null, '', '?date=' + this.dateInput.value); this.sig = ''; this.load(); });
    document.querySelectorAll('[data-day-shift]').forEach(b => b.addEventListener('click', () => {
      const d = new Date(this.dateInput.value + 'T12:00:00'); d.setDate(d.getDate() + parseInt(b.dataset.dayShift, 10));
      this.dateInput.value = d.toISOString().slice(0, 10); this.dateInput.dispatchEvent(new Event('change'));
    }));
    const today = document.querySelector('[data-day-today]');
    if (today) today.addEventListener('click', () => { this.dateInput.value = today.dataset.dayToday; this.dateInput.dispatchEvent(new Event('change')); });
    this.load(); setInterval(() => { if (!document.hidden) this.load(); }, POLL_MS);
  }
  async load() {
    try {
      const res = await fetch('/api/schedule?date=' + this.dateInput.value, { headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' } });
      if (res.status === 401) { location.href = '/login'; return; }
      const data = await res.json(); if (!data.ok) return;
      const sig = JSON.stringify(data.stylists) + data.date;
      if (sig !== this.sig) { this.sig = sig; this.render(data); }
      this.statusEl.innerHTML = `<span class="live-dot"></span>Live · updated ${data.server_time}`;
    } catch (e) { this.statusEl.innerHTML = '<span class="live-dot off"></span>Reconnecting…'; }
  }
  render(data) {
    if (!data.stylists.length || !data.times.length) {
      this.el.innerHTML = '<div class="empty-state"><p>No one is scheduled to work on this day.</p></div>'; return;
    }
    const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const names = { available: 'Available', booked: 'Booked', blocked: 'Unavailable', past: 'Passed', too_long: '' };
    let html = '<table class="board"><thead><tr><th style="width:72px"></th>';
    data.stylists.forEach(s => { html += `<th>${esc(s.name)}<small>${s.off ? 'Day off' : s.closed ? 'Paused' : 'Working'}</small></th>`; });
    html += '</tr></thead><tbody>';
    data.times.forEach(t => {
      html += `<tr><td class="time">${t.label}</td>`;
      data.stylists.forEach(s => {
        const c = s.cells[t.time];
        if (!c) { html += '<td><span class="na">—</span></td>'; return; }
        let inner = `${names[c.status]}`, tag = 'div', attrs = '';
        if (c.status === 'booked') {
          inner = c.start ? `<span class="who">${esc(c.client)}</span><small>${esc(c.service)}</small>` : '<small>Booked · cont.</small>';
          if (data.is_admin) { tag = 'a'; attrs = ` href="/admin/appointments/${c.appt_id}/edit" title="Edit booking"`; }
        } else if (c.status === 'blocked') inner = `Unavailable<small>${esc(c.reason || '')}</small>`;
        else inner = `${names[c.status]}`;
        html += `<td><${tag} class="slot is-${c.status}"${attrs}>${inner}</${tag}></td>`;
      });
      html += '</tr>';
    });
    this.el.innerHTML = html + '</tbody></table>';
  }
}
