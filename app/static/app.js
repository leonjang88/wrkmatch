// wrkmatch — mobile-first vanilla JS SPA. No frameworks, no build step, no external deps.

(function () {
  'use strict';

  const viewEl = document.getElementById('view');
  const topbarEl = document.getElementById('topbar');
  const toastEl = document.getElementById('toast');
  const navBtns = Array.from(document.querySelectorAll('.nav-btn'));

  const state = {
    summary: null,
    companies: null,
    coverage: null,
    weights: null,
    detail: null,
    detailId: null,
    showHiddenCompanies: false,
    postingFilters: { remote: false, boston: false, newOnly: false, seniority: new Set() },
    showHiddenPostings: false,
    atsFormOpen: false,
  };

  let toastTimer = null;

  // ---------- helpers ----------

  function esc(str) {
    if (str == null) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function titleCase(str) {
    if (!str) return '';
    return String(str).replace(/\w\S*/g, (w) => w.charAt(0).toUpperCase() + w.slice(1));
  }

  function timeAgo(iso) {
    if (!iso) return null;
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return null;
    const diffSec = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (diffSec < 45) return 'just now';
    const diffMin = Math.round(diffSec / 60);
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffHr = Math.round(diffMin / 60);
    if (diffHr < 24) return `${diffHr}h ago`;
    const diffDay = Math.round(diffHr / 24);
    if (diffDay < 30) return `${diffDay}d ago`;
    const diffMonth = Math.round(diffDay / 30);
    return `${diffMonth}mo ago`;
  }

  async function api(path, opts) {
    const res = await fetch(path, Object.assign({
      headers: { 'Content-Type': 'application/json' },
    }, opts));
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    if (res.status === 204) return null;
    return res.json();
  }

  function post(path, body) {
    return api(path, { method: 'POST', body: JSON.stringify(body || {}) });
  }

  function put(path, body) {
    return api(path, { method: 'PUT', body: JSON.stringify(body || {}) });
  }

  function showToast(message, opts) {
    opts = opts || {};
    toastEl.innerHTML = `<span>${esc(message)}</span>` +
      (opts.actionLabel ? `<button class="toast-action" type="button">${esc(opts.actionLabel)}</button>` : '');
    toastEl.classList.add('visible');
    if (opts.actionLabel && opts.onAction) {
      toastEl.querySelector('.toast-action').onclick = () => { opts.onAction(); hideToast(); };
    }
    clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, 4500);
  }

  function hideToast() {
    toastEl.classList.remove('visible');
  }

  function renderEmptyState(container, { icon, title, desc }) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="icon">${icon}</div>
        <div class="title">${esc(title)}</div>
        <div class="desc">${esc(desc)}</div>
      </div>
    `;
  }

  function renderErrorState(container, { title, desc, onRetry }) {
    container.innerHTML = `
      <div class="error-state">
        <div class="icon">⚠️</div>
        <div class="title">${esc(title)}</div>
        <div class="desc">${esc(desc)}</div>
        <button class="retry-btn" id="retry-btn" type="button">Try again</button>
      </div>
    `;
    document.getElementById('retry-btn').addEventListener('click', onRetry);
  }

  // ---------- router ----------

  function currentRoute() {
    const hash = location.hash.replace(/^#\/?/, '');
    const parts = hash.split('/').filter(Boolean);
    if (!parts.length) return { tab: 'companies' };
    if (parts[0] === 'companies' && parts[1]) return { tab: 'companies', id: parts[1] };
    if (['companies', 'coverage', 'settings'].includes(parts[0])) return { tab: parts[0] };
    return { tab: 'companies' };
  }

  function setActiveNav(tab) {
    navBtns.forEach((btn) => btn.classList.toggle('active', btn.dataset.tab === tab));
  }

  function route() {
    const r = currentRoute();
    setActiveNav(r.tab);
    if (r.tab === 'companies' && r.id) {
      renderCompanyDetail(r.id);
    } else if (r.tab === 'coverage') {
      renderCoverage();
    } else if (r.tab === 'settings') {
      renderSettings();
    } else {
      renderCompanies();
    }
  }

  window.addEventListener('hashchange', route);

  // ---------- companies tab ----------

  async function renderCompanies() {
    topbarEl.innerHTML = `<div class="topbar-title">wrkmatch</div><div class="topbar-stats" id="topbar-stats"></div>`;
    viewEl.innerHTML = `
      <div id="company-list">
        <div class="skeleton skeleton-card"></div>
        <div class="skeleton skeleton-card"></div>
        <div class="skeleton skeleton-card"></div>
      </div>
    `;

    let summary, companies;
    try {
      [summary, companies] = await Promise.all([api('/api/summary'), api('/api/companies')]);
    } catch (err) {
      renderErrorState(viewEl, {
        title: "Can't reach wrkmatch",
        desc: 'Make sure the server is running, then try again.',
        onRetry: renderCompanies,
      });
      return;
    }

    state.summary = summary;
    state.companies = companies.companies || [];

    renderTopbarStats(summary);
    renderCompanyList();
  }

  function renderTopbarStats(summary) {
    const statsEl = document.getElementById('topbar-stats');
    if (!statsEl) return;
    const scanText = summary.last_scan
      ? `scanned ${timeAgo(summary.last_scan.finished_at) || 'recently'}`
      : 'no scan yet';
    statsEl.innerHTML = `
      <span><strong>${summary.open_postings}</strong> open</span>
      <span>${esc(scanText)}</span>
      <span><strong>${summary.contacts}</strong> contacts</span>
    `;
  }

  function renderCompanyList() {
    const all = state.companies || [];

    if (!all.length) {
      renderEmptyState(viewEl, {
        icon: '🕵️',
        title: 'No companies yet',
        desc: 'Run a scan to pull in companies from your contacts and job boards.',
      });
      return;
    }

    const visible = all.filter((c) => c.priority !== 'hidden');
    const hiddenCount = all.length - visible.length;
    const shown = state.showHiddenCompanies ? all : visible;

    let html = `<div class="company-list">${shown.map(companyCardHtml).join('')}</div>`;
    if (hiddenCount > 0) {
      html += `<button class="reveal-toggle" id="toggle-hidden-companies" type="button">
        ${state.showHiddenCompanies ? 'Hide hidden companies' : `${hiddenCount} hidden — show`}
      </button>`;
    }
    viewEl.innerHTML = html;

    viewEl.querySelectorAll('.company-card').forEach((card) => {
      card.addEventListener('click', () => { location.hash = `#companies/${card.dataset.id}`; });
    });

    const toggleBtn = document.getElementById('toggle-hidden-companies');
    if (toggleBtn) {
      toggleBtn.addEventListener('click', () => {
        state.showHiddenCompanies = !state.showHiddenCompanies;
        renderCompanyList();
      });
    }
  }

  function companyCardHtml(c) {
    const muted = c.priority === 'deprioritized' || c.priority === 'hidden' ? ' muted' : '';
    const priorityTag = c.priority === 'deprioritized'
      ? '<span class="tag priority">Deprioritized</span>'
      : c.priority === 'hidden'
        ? '<span class="tag priority">Hidden</span>'
        : '';
    const atsTag = c.ats_platform ? `<span class="tag">${esc(c.ats_platform)}</span>` : `<span class="tag">no ats</span>`;
    const newBit = c.new_postings_7d > 0 ? ` · ✨ ${c.new_postings_7d}` : '';
    return `
      <button class="company-card${muted}" type="button" data-id="${esc(c.id)}">
        <div class="card-main">
          <div class="card-name">${esc(titleCase(c.name))}</div>
          <div class="card-tags">${atsTag}${priorityTag}</div>
        </div>
        <div class="card-side">
          <div class="score-badge">${Math.round(c.score)}</div>
          <div class="component-breakdown">👥 ${c.contact_count} · 📋 ${c.open_postings}${newBit}</div>
        </div>
      </button>
    `;
  }

  // ---------- company detail ----------

  async function renderCompanyDetail(id) {
    topbarEl.innerHTML = `
      <div class="topbar-back">
        <button class="back-btn" id="back-btn" type="button" aria-label="Back">←</button>
        <div class="topbar-heading"><div class="company-title">Loading…</div></div>
      </div>
    `;
    document.getElementById('back-btn').addEventListener('click', () => { location.hash = '#companies'; });
    viewEl.innerHTML = `<div class="skeleton skeleton-card"></div><div class="skeleton skeleton-card"></div>`;

    state.detailId = id;
    state.postingFilters = { remote: false, boston: false, newOnly: false, seniority: new Set() };
    state.showHiddenPostings = false;
    state.atsFormOpen = false;

    let detail;
    try {
      detail = await api(`/api/companies/${encodeURIComponent(id)}`);
    } catch (err) {
      renderErrorState(viewEl, {
        title: "Couldn't load this company",
        desc: 'Check the connection and try again.',
        onRetry: () => renderCompanyDetail(id),
      });
      return;
    }
    state.detail = detail;
    paintCompanyDetail();
  }

  function paintCompanyDetail() {
    const d = state.detail;
    if (!d) return;

    topbarEl.querySelector('.company-title').textContent = titleCase(d.name);
    let sub = topbarEl.querySelector('.company-sub');
    if (!sub) {
      sub = document.createElement('div');
      sub.className = 'company-sub';
      topbarEl.querySelector('.topbar-heading').appendChild(sub);
    }
    sub.textContent = d.ats_platform || 'No ATS linked';

    const contacts = (d.contacts || []).slice().sort(sortContacts);

    viewEl.innerHTML = `
      <div class="section-title">Who you know</div>
      ${contacts.length
        ? `<div class="contact-list">${contacts.map(contactRowHtml).join('')}</div>`
        : `<div class="hint">No LinkedIn contacts recorded at this company.</div>`}

      <div class="section-title">Open roles</div>
      <div class="filter-chips" id="filter-chips">
        ${chipHtml('remote', 'Remote')}
        ${chipHtml('boston', 'Boston')}
        ${chipHtml('newOnly', 'New only')}
        ${chipHtml('seniority:mid', 'Mid')}
        ${chipHtml('seniority:senior', 'Senior')}
        ${chipHtml('seniority:staff', 'Staff+')}
        ${chipHtml('seniority:director', 'Director+')}
      </div>
      <div id="posting-list"></div>

      <div class="section-title">Company</div>
      <div class="actions-row">
        <button class="btn${d.priority === 'deprioritized' ? ' active-state' : ' warn'}" id="deprioritize-btn" type="button">
          ${d.priority === 'deprioritized' ? 'Un-deprioritize' : 'Deprioritize'}
        </button>
        <button class="btn${d.priority === 'hidden' ? ' active-state' : ' danger'}" id="hide-btn" type="button">
          ${d.priority === 'hidden' ? 'Unhide' : 'Hide'}
        </button>
      </div>

      <div class="ats-form">
        <button class="ats-form-toggle" id="ats-toggle" type="button">
          <span id="ats-caret">${state.atsFormOpen ? '▾' : '▸'}</span> Set ATS manually
        </button>
        <div class="ats-form-body${state.atsFormOpen ? '' : ' collapsed'}" id="ats-form-body">
          <div>
            <span class="field-label">Platform</span>
            <select id="ats-platform">
              ${['greenhouse', 'lever', 'ashby', 'workable', 'recruitee', 'smartrecruiters', 'workday', 'personio']
                .map((p) => `<option value="${p}"${d.ats_platform === p ? ' selected' : ''}>${p}</option>`).join('')}
            </select>
          </div>
          <div>
            <span class="field-label">Slug</span>
            <input class="text-input" id="ats-slug" type="text" placeholder="company-slug" value="${esc(d.ats_slug || '')}">
          </div>
          <button class="save-btn" id="ats-save" type="button">Save ATS</button>
        </div>
      </div>
    `;

    renderPostingList();
    wireContactActions();
    wireFilterChips();
    wireCompanyActions();
    wireAtsForm();
  }

  function sortContacts(a, b) {
    const order = { strong: 0, ok: 1, weak: 2 };
    const oa = order[a.rating] ?? 3;
    const ob = order[b.rating] ?? 3;
    if (oa !== ob) return oa - ob;
    return `${a.first_name} ${a.last_name}`.localeCompare(`${b.first_name} ${b.last_name}`);
  }

  function contactRowHtml(c) {
    const name = esc(`${c.first_name || ''} ${c.last_name || ''}`.trim() || 'Unknown');
    const position = esc(c.position || '—');
    const rating = c.rating || '';
    return `
      <div class="contact-row" data-id="${esc(c.id)}">
        <div class="contact-info">
          <div class="contact-name">${name}</div>
          <div class="contact-position">${position}</div>
        </div>
        <div class="contact-actions">
          <div class="segmented" data-contact-id="${esc(c.id)}">
            <button type="button" data-rating="strong" class="${rating === 'strong' ? 'active' : ''}">Strong</button>
            <button type="button" data-rating="ok" class="${rating === 'ok' ? 'active' : ''}">OK</button>
            <button type="button" data-rating="weak" class="${rating === 'weak' ? 'active' : ''}">Weak</button>
          </div>
          ${c.url ? `<a class="li-btn" href="${esc(c.url)}" target="_blank" rel="noopener noreferrer" aria-label="Open LinkedIn">🔗</a>` : ''}
        </div>
      </div>
    `;
  }

  function wireContactActions() {
    viewEl.querySelectorAll('.segmented').forEach((seg) => {
      seg.querySelectorAll('button').forEach((btn) => {
        btn.addEventListener('click', async () => {
          const contactId = seg.dataset.contactId;
          const rating = btn.dataset.rating;
          const prevActive = seg.querySelector('button.active');
          const prevRating = prevActive ? prevActive.dataset.rating : null;

          seg.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));

          try {
            await post(`/api/contacts/${encodeURIComponent(contactId)}/rating`, { rating });
            const c = (state.detail.contacts || []).find((x) => String(x.id) === String(contactId));
            if (c) c.rating = rating;
          } catch (err) {
            seg.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.rating === prevRating));
            showToast("Couldn't save rating — try again");
          }
        });
      });
    });
  }

  const SENIORITY_TESTS = {
    mid: (title) => /\b(product manager|product owner)\b/i.test(title) &&
      !/\b(senior|sr|staff|principal|lead|group|director|vp|head)\b/i.test(title),
    senior: (title) => /\bsenior\b|\bsr\b/i.test(title),
    staff: (title) => /\b(staff|principal|lead|group)\b/i.test(title),
    director: (title) => /\bdirector\b|\bvp\b|head of/i.test(title),
  };

  function chipHtml(key, label) {
    const active = key.startsWith('seniority:')
      ? state.postingFilters.seniority.has(key.split(':')[1])
      : !!state.postingFilters[key];
    return `<button class="chip${active ? ' active' : ''}" type="button" data-chip="${key}">${esc(label)}</button>`;
  }

  function wireFilterChips() {
    const container = document.getElementById('filter-chips');
    if (!container) return;
    container.querySelectorAll('.chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        const key = chip.dataset.chip;
        if (key.startsWith('seniority:')) {
          const band = key.split(':')[1];
          if (state.postingFilters.seniority.has(band)) state.postingFilters.seniority.delete(band);
          else state.postingFilters.seniority.add(band);
        } else {
          state.postingFilters[key] = !state.postingFilters[key];
        }
        chip.classList.toggle('active');
        renderPostingList();
      });
    });
  }

  function postingMatchesFilters(p) {
    const f = state.postingFilters;
    const loc = p.location || '';
    const title = p.title || '';
    if (f.remote && !/remote/i.test(loc)) return false;
    if (f.boston && !/boston|cambridge|somerville|massachusetts|\bma\b/i.test(loc)) return false;
    if (f.newOnly && !p.is_new) return false;
    if (f.seniority.size > 0) {
      const matches = [...f.seniority].some((band) => SENIORITY_TESTS[band](title));
      if (!matches) return false;
    }
    return true;
  }

  function renderPostingList() {
    const listEl = document.getElementById('posting-list');
    if (!listEl) return;
    const all = state.detail.postings || [];
    const filtered = all.filter(postingMatchesFilters);

    const active = filtered.filter((p) => p.user_status === 'none' || !p.user_status);
    const inactive = filtered.filter((p) => p.user_status === 'done' || p.user_status === 'ignored');

    let html = '';
    if (!all.length) {
      html = `<div class="hint">No open postings found for this company.</div>`;
    } else if (!active.length && !inactive.length) {
      html = `<div class="hint">No postings match these filters.</div>`;
    } else {
      html = active.length
        ? active.map(postingRowHtml).join('')
        : `<div class="hint">No open postings match these filters.</div>`;
    }

    if (inactive.length) {
      html += `<button class="hidden-toggle" id="toggle-hidden-postings" type="button">
        ${state.showHiddenPostings ? 'Hide done/ignored' : `${inactive.length} ignored/done hidden — show`}
      </button>`;
      if (state.showHiddenPostings) html += inactive.map(postingRowHtml).join('');
    }

    listEl.innerHTML = html;

    const toggle = document.getElementById('toggle-hidden-postings');
    if (toggle) {
      toggle.addEventListener('click', () => {
        state.showHiddenPostings = !state.showHiddenPostings;
        renderPostingList();
      });
    }
    wirePostingActions();
  }

  function postingRowHtml(p) {
    const isDone = p.user_status === 'done';
    const isIgnored = p.user_status === 'ignored';
    const rowClass = isDone ? ' done' : isIgnored ? ' ignored' : '';
    return `
      <div class="posting-row${rowClass}" data-id="${esc(p.id)}">
        <div class="posting-info">
          <div class="posting-title">
            ${esc(p.title)}
            ${p.is_new ? '<span class="tag new">NEW</span>' : ''}
            ${isIgnored ? '<span class="status-pill">Ignored</span>' : ''}
          </div>
          <div class="posting-location">${esc(p.location || 'Location unknown')}</div>
        </div>
        <div class="posting-actions">
          ${p.url ? `<a class="apply-btn" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">Apply ↗</a>` : ''}
          <button class="icon-btn done-btn${isDone ? ' active' : ''}" type="button" data-action="done" aria-label="Mark done">✓</button>
          <button class="icon-btn ignore-btn${isIgnored ? ' active' : ''}" type="button" data-action="ignore" aria-label="Ignore">✕</button>
        </div>
      </div>
    `;
  }

  function wirePostingActions() {
    document.querySelectorAll('.posting-row').forEach((row) => {
      row.querySelectorAll('[data-action]').forEach((btn) => {
        btn.addEventListener('click', async () => {
          const postingId = row.dataset.id;
          const action = btn.dataset.action;
          const p = (state.detail.postings || []).find((x) => String(x.id) === String(postingId));
          if (!p) return;
          const prevStatus = p.user_status;
          const nextStatus = prevStatus === action ? 'none' : action;

          p.user_status = nextStatus;
          renderPostingList();

          try {
            await post(`/api/postings/${encodeURIComponent(postingId)}/user_status`, { user_status: nextStatus });
          } catch (err) {
            p.user_status = prevStatus;
            renderPostingList();
            showToast("Couldn't update posting — try again");
          }
        });
      });
    });
  }

  function wireCompanyActions() {
    document.getElementById('deprioritize-btn').addEventListener('click', () => {
      setCompanyPriority(state.detail.priority === 'deprioritized' ? 'normal' : 'deprioritized');
    });
    document.getElementById('hide-btn').addEventListener('click', () => {
      setCompanyPriority(state.detail.priority === 'hidden' ? 'normal' : 'hidden');
    });
  }

  async function setCompanyPriority(next) {
    const prev = state.detail.priority;
    state.detail.priority = next;
    paintCompanyDetail();
    try {
      await post(`/api/companies/${encodeURIComponent(state.detail.id)}/priority`, { priority: next });
      const label = next === 'normal' ? 'Priority reset' : next === 'hidden' ? 'Company hidden' : 'Company deprioritized';
      showToast(label, { actionLabel: 'Undo', onAction: () => setCompanyPriority(prev) });
    } catch (err) {
      state.detail.priority = prev;
      paintCompanyDetail();
      showToast("Couldn't update — try again");
    }
  }

  function wireAtsForm() {
    document.getElementById('ats-toggle').addEventListener('click', () => {
      state.atsFormOpen = !state.atsFormOpen;
      document.getElementById('ats-form-body').classList.toggle('collapsed', !state.atsFormOpen);
      document.getElementById('ats-caret').textContent = state.atsFormOpen ? '▾' : '▸';
    });

    document.getElementById('ats-save').addEventListener('click', async () => {
      const platform = document.getElementById('ats-platform').value;
      const slug = document.getElementById('ats-slug').value.trim();
      const saveBtn = document.getElementById('ats-save');
      if (!slug) { showToast('Enter a slug first'); return; }
      saveBtn.disabled = true;
      saveBtn.textContent = 'Saving…';
      try {
        await post(`/api/companies/${encodeURIComponent(state.detail.id)}/ats`, { platform, slug });
        state.detail.ats_platform = platform;
        state.detail.ats_slug = slug;
        state.atsFormOpen = true;
        showToast('ATS saved');
        paintCompanyDetail();
      } catch (err) {
        showToast("Couldn't save ATS — try again");
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save ATS';
      }
    });
  }

  // ---------- coverage tab ----------

  async function renderCoverage() {
    topbarEl.innerHTML = `<div class="topbar-title">Coverage</div>`;
    viewEl.innerHTML = `<div class="skeleton skeleton-card"></div><div class="skeleton skeleton-card"></div>`;

    let coverage;
    try {
      coverage = await api('/api/coverage');
    } catch (err) {
      renderErrorState(viewEl, {
        title: "Can't reach wrkmatch",
        desc: 'Make sure the server is running, then try again.',
        onRetry: renderCoverage,
      });
      return;
    }
    state.coverage = coverage;

    const noAts = coverage.no_ats || [];
    const listHtml = noAts.length
      ? `<div class="no-ats-list">${noAts.map(noAtsRowHtml).join('')}</div>`
      : `<div class="empty-state">
          <div class="icon">✅</div>
          <div class="title">Full coverage</div>
          <div class="desc">Every company has a discovered job board.</div>
        </div>`;

    viewEl.innerHTML = `
      <div class="stat-line">${coverage.companies_with_ats} of ${coverage.companies_total} companies scannable</div>
      <div class="hint">These companies' job boards weren't found — check them manually.</div>
      ${listHtml}
    `;
  }

  function noAtsRowHtml(c) {
    return `
      <div class="no-ats-row">
        <div class="no-ats-info">
          <div class="no-ats-name">${esc(titleCase(c.name))}</div>
          <div class="no-ats-meta">${c.contact_count} contact${c.contact_count === 1 ? '' : 's'}${c.priority && c.priority !== 'normal' ? ` · ${esc(c.priority)}` : ''}</div>
        </div>
        ${c.search_url ? `<a class="find-btn" href="${esc(c.search_url)}" target="_blank" rel="noopener noreferrer">Find jobs ↗</a>` : ''}
      </div>
    `;
  }

  // ---------- settings tab ----------

  const WEIGHT_META = [
    { key: 'contacts', label: 'Contacts', hint: 'How much having LinkedIn contacts at a company boosts its score.' },
    { key: 'postings', label: 'Postings', hint: 'How much open PM roles at a company boost its score.' },
    { key: 'freshness', label: 'Freshness', hint: 'How much recently-posted or newly-found roles boost the score.' },
  ];

  async function renderSettings() {
    topbarEl.innerHTML = `<div class="topbar-title">Settings</div>`;
    viewEl.innerHTML = `<div class="skeleton skeleton-card"></div>`;

    let summary;
    try {
      summary = await api('/api/summary');
    } catch (err) {
      renderErrorState(viewEl, {
        title: "Can't reach wrkmatch",
        desc: 'Make sure the server is running, then try again.',
        onRetry: renderSettings,
      });
      return;
    }
    state.summary = summary;
    state.weights = Object.assign({ contacts: 1, postings: 1, freshness: 1 }, summary.weights || {});

    viewEl.innerHTML = `
      <div class="settings-card">
        ${WEIGHT_META.map((w) => `
          <div class="slider-block">
            <div class="slider-label">
              <span>${w.label}</span>
              <span class="slider-value" id="value-${w.key}">${state.weights[w.key]}</span>
            </div>
            <div class="slider-hint">${w.hint}</div>
            <input type="range" min="0" max="5" step="0.5" id="slider-${w.key}" data-key="${w.key}" value="${state.weights[w.key]}">
          </div>
        `).join('')}
        <button class="save-btn" id="save-weights" type="button">Save</button>
      </div>

      <div class="section-title">Database</div>
      <div class="stats-grid">
        <div class="stat-tile"><div class="stat-value">${summary.contacts}</div><div class="stat-label">Contacts</div></div>
        <div class="stat-tile"><div class="stat-value">${summary.companies_total}</div><div class="stat-label">Companies</div></div>
        <div class="stat-tile"><div class="stat-value">${summary.companies_with_ats}</div><div class="stat-label">With ATS</div></div>
        <div class="stat-tile"><div class="stat-value">${summary.open_postings}</div><div class="stat-label">Open postings</div></div>
      </div>
    `;

    WEIGHT_META.forEach((w) => {
      const slider = document.getElementById(`slider-${w.key}`);
      slider.addEventListener('input', () => {
        document.getElementById(`value-${w.key}`).textContent = slider.value;
      });
    });

    document.getElementById('save-weights').addEventListener('click', async () => {
      const btn = document.getElementById('save-weights');
      const payload = {};
      WEIGHT_META.forEach((w) => { payload[w.key] = parseFloat(document.getElementById(`slider-${w.key}`).value); });
      btn.disabled = true;
      btn.textContent = 'Saving…';
      try {
        await put('/api/settings/weights', payload);
        state.weights = payload;
        state.companies = null;
        showToast('Weights saved');
      } catch (err) {
        showToast("Couldn't save weights — try again");
      } finally {
        btn.disabled = false;
        btn.textContent = 'Save';
      }
    });
  }

  // ---------- init ----------

  route();
})();
