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
    showFilteredOutPostings: false,
    atsFormOpen: false,
    filters: null,
    tempAllLocations: false,
    companiesExcluded: 0,
    showHiddenCoverage: false,
    jobsList: null,
    jobsTotal: 0,
    jobsMatching: 0,
    jobsShowAll: false,
    jobsShowFilteredOut: false,
    jobsShowDone: false,
  };

  let salarySheetState = null;

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

  function daysAgoLabel(iso) {
    if (!iso) return null;
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return null;
    const diffDay = Math.max(0, Math.floor((Date.now() - then) / 86400000));
    return diffDay === 0 ? 'today' : `${diffDay}d ago`;
  }

  function formatSalary(p) {
    const min = p.salary_min, max = p.salary_max;
    if (min == null && max == null) return null;
    const fmt = (n) => `$${Math.round(n / 1000)}k`;
    if (min != null && max != null) return `${fmt(min)}–${fmt(max)}`;
    if (min != null) return `${fmt(min)}+`;
    return `≤${fmt(max)}`;
  }

  const REMOTE_LABELS = { remote: 'Remote', hybrid: 'Hybrid', onsite: 'Onsite' };

  const SALARY_PRESETS = [
    { value: null, label: 'None' },
    { value: 100000, label: '$100k' },
    { value: 125000, label: '$125k' },
    { value: 150000, label: '$150k' },
    { value: 175000, label: '$175k' },
    { value: 200000, label: '$200k' },
  ];

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

  // Single source of truth for filters — Companies tab and Settings tab both
  // read/write through this, so a change made in either place is reflected
  // consistently once the caller refetches/repaints.
  async function saveFilters(patch) {
    const next = Object.assign({}, state.filters, patch);
    try {
      const res = await put('/api/settings/filters', next);
      state.filters = (res && res.filters) || next;
      return state.filters;
    } catch (err) {
      showToast("Couldn't save filters — try again");
      throw err;
    }
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
    if (['companies', 'jobs', 'coverage', 'settings'].includes(parts[0])) return { tab: parts[0] };
    return { tab: 'companies' };
  }

  function setActiveNav(tab) {
    navBtns.forEach((btn) => btn.classList.toggle('active', btn.dataset.tab === tab));
  }

  function route() {
    closeSalarySheet();
    const r = currentRoute();
    setActiveNav(r.tab);
    if (r.tab === 'companies' && r.id) {
      renderCompanyDetail(r.id);
    } else if (r.tab === 'jobs') {
      renderJobs();
    } else if (r.tab === 'coverage') {
      renderCoverage();
    } else if (r.tab === 'settings') {
      renderSettings();
    } else {
      renderCompanies();
    }
  }

  window.addEventListener('hashchange', route);

  // ---------- global filters (shared by Companies tab + Settings tab) ----------

  function globalFilterChipsHtml() {
    const f = state.filters || {};
    const temp = state.tempAllLocations;
    const locLabel = temp
      ? '📍 All locations (temp)'
      : (f.location_enabled ? '📍 Remote + Boston' : '📍 All locations');
    const locClass = temp ? ' temp' : (f.location_enabled ? ' active' : '');
    const salaryLabel = f.salary_min ? `💰 $${Math.round(f.salary_min / 1000)}k+` : '💰 Any pay';
    const salaryClass = f.salary_min ? ' active' : '';
    return `
      <button class="filter-chip${locClass}" id="location-chip" type="button">${locLabel}</button>
      <button class="filter-chip${salaryClass}" id="salary-chip" type="button">${salaryLabel}</button>
    `;
  }

  function wireGlobalFilterChips(onChange) {
    const locBtn = document.getElementById('location-chip');
    const salBtn = document.getElementById('salary-chip');
    if (locBtn) {
      locBtn.addEventListener('click', async () => {
        if (state.tempAllLocations) {
          state.tempAllLocations = false;
          onChange();
          return;
        }
        const f = state.filters || {};
        try {
          await saveFilters({ location_enabled: !f.location_enabled });
          onChange();
        } catch (err) { /* toast already shown by saveFilters */ }
      });
    }
    if (salBtn) {
      salBtn.addEventListener('click', () => openSalarySheet(onChange));
    }
  }

  function openSalarySheet(onApplied) {
    const f = state.filters || {};
    salarySheetState = {
      min: f.salary_min ?? null,
      includeUnknown: f.include_unknown_salary !== false,
      onApplied,
    };
    paintSalarySheet();
  }

  function closeSalarySheet() {
    salarySheetState = null;
    paintSalarySheet();
  }

  function paintSalarySheet() {
    const root = document.getElementById('sheet-root');
    if (!root) return;
    if (!salarySheetState) { root.innerHTML = ''; return; }
    const s = salarySheetState;
    root.innerHTML = `
      <div class="sheet-backdrop" id="sheet-backdrop">
        <div class="sheet" role="dialog" aria-label="Minimum salary">
          <div class="sheet-handle"></div>
          <div class="sheet-title">Minimum salary</div>
          <div class="sheet-options">
            ${SALARY_PRESETS.map((p) => `<button class="sheet-option${s.min === p.value ? ' active' : ''}" type="button" data-value="${p.value === null ? '' : p.value}">${p.label}</button>`).join('')}
          </div>
          <label class="checkbox-row">
            <input type="checkbox" id="sheet-include-unknown"${s.includeUnknown ? ' checked' : ''}>
            <span>Include postings without listed pay</span>
          </label>
          <div class="sheet-actions">
            <button class="btn" id="sheet-cancel" type="button">Cancel</button>
            <button class="save-btn" id="sheet-apply" type="button">Apply</button>
          </div>
        </div>
      </div>
    `;

    root.querySelectorAll('.sheet-option').forEach((btn) => {
      btn.addEventListener('click', () => {
        salarySheetState.min = btn.dataset.value === '' ? null : parseInt(btn.dataset.value, 10);
        paintSalarySheet();
      });
    });
    document.getElementById('sheet-include-unknown').addEventListener('change', (e) => {
      salarySheetState.includeUnknown = e.target.checked;
    });
    document.getElementById('sheet-backdrop').addEventListener('click', (e) => {
      if (e.target.id === 'sheet-backdrop') closeSalarySheet();
    });
    document.getElementById('sheet-cancel').addEventListener('click', closeSalarySheet);
    document.getElementById('sheet-apply').addEventListener('click', async () => {
      const applyBtn = document.getElementById('sheet-apply');
      applyBtn.disabled = true;
      const onApplied = salarySheetState.onApplied;
      const payload = { salary_min: salarySheetState.min, include_unknown_salary: salarySheetState.includeUnknown };
      try {
        await saveFilters(payload);
        closeSalarySheet();
        onApplied();
      } catch (err) {
        applyBtn.disabled = false;
      }
    });
  }

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
    state.tempAllLocations = false;
    await loadCompanies();
  }

  async function loadCompanies() {
    const qs = new URLSearchParams({ include_hidden: '1' });
    if (state.tempAllLocations) qs.set('all_locations', '1');

    let summary, companiesRes;
    try {
      [summary, companiesRes] = await Promise.all([api('/api/summary'), api(`/api/companies?${qs.toString()}`)]);
    } catch (err) {
      renderErrorState(viewEl, {
        title: "Can't reach wrkmatch",
        desc: 'Make sure the server is running, then try again.',
        onRetry: renderCompanies,
      });
      return;
    }

    state.summary = summary;
    state.companies = companiesRes.companies || [];
    state.filters = companiesRes.filters || state.filters;
    state.companiesExcluded = companiesRes.companies_excluded_by_filters || 0;

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

  function filterBarHtml() {
    const excluded = state.companiesExcluded || 0;
    let note = '';
    if (excluded > 0 && !state.tempAllLocations) {
      note = `<div class="filter-bar-note">${excluded} companies hidden by filters — <button class="link-btn" id="show-all-locations-btn" type="button">show all locations</button></div>`;
    } else if (excluded > 0 && state.tempAllLocations) {
      note = `<div class="filter-bar-note">${excluded} companies still hidden by filters</div>`;
    }
    return `<div class="filter-bar" id="global-filter-bar"><div class="filter-bar-chips">${globalFilterChipsHtml()}</div>${note}</div>`;
  }

  function wireShowAllLocations() {
    const btn = document.getElementById('show-all-locations-btn');
    if (btn) {
      btn.addEventListener('click', () => {
        state.tempAllLocations = true;
        loadCompanies();
      });
    }
  }

  function renderCompanyList() {
    const all = state.companies || [];

    if (!all.length) {
      const desc = state.companiesExcluded > 0
        ? 'Try broadening your filters, or run a scan to pull in more companies.'
        : 'Run a scan to pull in companies from your contacts and job boards.';
      viewEl.innerHTML = `
        ${filterBarHtml()}
        <div class="empty-state">
          <div class="icon">🕵️</div>
          <div class="title">No companies match</div>
          <div class="desc">${esc(desc)}</div>
        </div>
      `;
      wireGlobalFilterChips(loadCompanies);
      wireShowAllLocations();
      return;
    }

    const visible = all.filter((c) => c.priority !== 'hidden');
    const hiddenCount = all.length - visible.length;
    const shown = state.showHiddenCompanies ? all : visible;

    let html = filterBarHtml();
    html += `<div class="score-legend">Score = 👥 contacts + 📋 open roles + ✨ new this week — weighted, tune in <a href="#settings">Settings</a></div>`;
    html += `<div class="company-list">${shown.map(companyCardHtml).join('')}</div>`;
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

    wireGlobalFilterChips(loadCompanies);
    wireShowAllLocations();
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
    state.showFilteredOutPostings = false;
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
        : `<div class="hint">No LinkedIn contacts recorded at this company. Contacts appear here once a LinkedIn export mentioning this company is loaded.</div>`}

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

    const { active, filteredOut, inactive } = bucketByStatusAndFilters(filtered);

    const clearFiltersBtnHtml = `<button class="clear-filters-btn" id="clear-filters-btn" type="button">Clear filters</button>`;

    let html = '';
    if (!all.length) {
      html = `<div class="hint">No open postings found for this company.</div>`;
    } else if (!filtered.length) {
      html = `<div class="hint">No postings match these filters.</div>${clearFiltersBtnHtml}`;
    } else {
      html = active.length
        ? active.map(postingRowHtml).join('')
        : `<div class="hint">No open postings match these filters.</div>${clearFiltersBtnHtml}`;
    }

    if (filteredOut.length) {
      html += `<button class="hidden-toggle" id="toggle-filtered-postings" type="button">
        ${state.showFilteredOutPostings ? 'Hide filtered out' : `${filteredOut.length} filtered out — show`}
      </button>`;
      if (state.showFilteredOutPostings) html += filteredOut.map(postingRowHtml).join('');
    }

    if (inactive.length) {
      html += `<button class="hidden-toggle" id="toggle-hidden-postings" type="button">
        ${state.showHiddenPostings ? 'Hide done/ignored' : `${inactive.length} ignored/done hidden — show`}
      </button>`;
      if (state.showHiddenPostings) html += inactive.map(postingRowHtml).join('');
    }

    listEl.innerHTML = html;

    const filteredToggle = document.getElementById('toggle-filtered-postings');
    if (filteredToggle) {
      filteredToggle.addEventListener('click', () => {
        state.showFilteredOutPostings = !state.showFilteredOutPostings;
        renderPostingList();
      });
    }
    const toggle = document.getElementById('toggle-hidden-postings');
    if (toggle) {
      toggle.addEventListener('click', () => {
        state.showHiddenPostings = !state.showHiddenPostings;
        renderPostingList();
      });
    }
    const clearFiltersBtn = document.getElementById('clear-filters-btn');
    if (clearFiltersBtn) {
      clearFiltersBtn.addEventListener('click', resetPostingFilters);
    }
    wirePostingActions();
  }

  function resetPostingFilters() {
    state.postingFilters = { remote: false, boston: false, newOnly: false, seniority: new Set() };
    const chipsEl = document.getElementById('filter-chips');
    if (chipsEl) chipsEl.querySelectorAll('.chip').forEach((chip) => chip.classList.remove('active'));
    renderPostingList();
  }

  // Shared by company-detail posting rows and Jobs-tab rows — keeps the
  // salary/remote/department/age formatting identical in both places.
  function metaChipParts(p) {
    const parts = [];
    const salary = formatSalary(p);
    if (salary) parts.push(`<span class="meta-chip salary">${esc(salary)}</span>`);
    if (p.remote && REMOTE_LABELS[p.remote]) {
      parts.push(`<span class="meta-chip remote remote-${esc(p.remote)}">${REMOTE_LABELS[p.remote]}</span>`);
    }
    if (p.department) parts.push(`<span class="meta-chip dept" title="${esc(p.department)}">${esc(p.department)}</span>`);
    const age = daysAgoLabel(p.posted_at || p.first_seen);
    if (age) parts.push(`<span class="meta-chip age">${esc(age)}</span>`);
    return parts;
  }

  function postingMetaHtml(p) {
    const parts = metaChipParts(p);
    if (!parts.length) return '';
    return `<div class="posting-meta">${parts.join('')}</div>`;
  }

  function contactChipHtml(p) {
    const n = p.contact_count || 0;
    return `<span class="meta-chip contacts${n === 0 ? ' cold' : ''}">👥 ${n}</span>`;
  }

  function jobMetaHtml(p) {
    const parts = metaChipParts(p);
    parts.push(contactChipHtml(p));
    return `<div class="posting-meta">${parts.join('')}</div>`;
  }

  // Groups a flat posting list the same way everywhere it's needed: done/ignored
  // wins over filtered-out when a posting is both, matching the reveal-toggle
  // idiom used in both company detail and the Jobs tab.
  function bucketByStatusAndFilters(list) {
    const inactive = list.filter((p) => p.user_status === 'done' || p.user_status === 'ignored');
    const remaining = list.filter((p) => p.user_status !== 'done' && p.user_status !== 'ignored');
    const filteredOut = remaining.filter((p) => p.matches_filters === false);
    const active = remaining.filter((p) => p.matches_filters !== false);
    return { active, filteredOut, inactive };
  }

  function postingRowHtml(p) {
    const isDone = p.user_status === 'done';
    const isIgnored = p.user_status === 'ignored';
    const isFilteredOut = !isDone && !isIgnored && p.matches_filters === false;
    const rowClass = isDone ? ' done' : isIgnored ? ' ignored' : isFilteredOut ? ' filtered-out' : '';
    return `
      <div class="posting-row${rowClass}" data-id="${esc(p.id)}">
        <div class="posting-info">
          <div class="posting-title">
            <span class="posting-title-text" title="${esc(p.title)}">${esc(p.title)}</span>
            ${p.is_new ? '<span class="tag new">NEW</span>' : ''}
            ${isIgnored ? '<span class="status-pill">Ignored</span>' : ''}
          </div>
          <div class="posting-location">${esc(p.location || 'Location unknown')}</div>
          ${postingMetaHtml(p)}
        </div>
        <div class="posting-actions">
          ${p.url ? `<a class="apply-btn" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">Apply ↗</a>` : ''}
          <button class="icon-btn done-btn${isDone ? ' active' : ''}" type="button" data-action="done" aria-label="Mark done">✓<span class="btn-label">Done</span></button>
          <button class="icon-btn ignore-btn${isIgnored ? ' active' : ''}" type="button" data-action="ignore" aria-label="Ignore">✕<span class="btn-label">Ignore</span></button>
        </div>
      </div>
    `;
  }

  // Maps a button's data-action to the API's user_status value. The API only
  // accepts none|done|ignored — 'ignore' is a UI-only label, not a status.
  const POSTING_ACTION_STATUS = { done: 'done', ignore: 'ignored' };

  function wirePostingActions() {
    document.querySelectorAll('.posting-row').forEach((row) => {
      row.querySelectorAll('[data-action]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const postingId = row.dataset.id;
          const targetStatus = POSTING_ACTION_STATUS[btn.dataset.action];
          if (!targetStatus) return;
          const p = (state.detail.postings || []).find((x) => String(x.id) === String(postingId));
          if (!p) return;
          const nextStatus = p.user_status === targetStatus ? 'none' : targetStatus;
          setPostingStatus(postingId, nextStatus);
        });
      });
    });
  }

  // Shared by company-detail posting rows and Jobs-tab rows — same optimistic
  // update + toast/Undo + rollback-on-error behavior, just parameterized by
  // which list holds the posting and how to repaint after mutating it.
  async function setPostingStatusGeneric(list, postingId, nextStatus, repaint) {
    const p = (list || []).find((x) => String(x.id) === String(postingId));
    if (!p) return;
    const prevStatus = p.user_status;
    p.user_status = nextStatus;
    repaint();

    try {
      await post(`/api/postings/${encodeURIComponent(postingId)}/user_status`, { user_status: nextStatus });
      if (nextStatus === 'done') {
        showToast('Marked done', { actionLabel: 'Undo', onAction: () => setPostingStatusGeneric(list, postingId, 'none', repaint) });
      } else if (nextStatus === 'ignored') {
        showToast('Ignored', { actionLabel: 'Undo', onAction: () => setPostingStatusGeneric(list, postingId, 'none', repaint) });
      }
    } catch (err) {
      p.user_status = prevStatus;
      repaint();
      showToast("Couldn't update posting — try again");
    }
  }

  function setPostingStatus(postingId, nextStatus) {
    return setPostingStatusGeneric(state.detail.postings || [], postingId, nextStatus, renderPostingList);
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

  // ---------- jobs tab ----------

  async function renderJobs() {
    topbarEl.innerHTML = `<div class="topbar-title">Jobs</div>`;
    viewEl.innerHTML = `
      <div class="skeleton skeleton-card"></div>
      <div class="skeleton skeleton-card"></div>
      <div class="skeleton skeleton-card"></div>
    `;
    state.jobsShowAll = false;
    state.jobsShowFilteredOut = false;
    state.jobsShowDone = false;
    await loadJobs();
  }

  async function loadJobs() {
    const qs = new URLSearchParams();
    if (state.jobsShowAll) qs.set('all', '1');

    let filters, jobsRes;
    try {
      [filters, jobsRes] = await Promise.all([
        api('/api/settings/filters'),
        api(`/api/postings${qs.toString() ? '?' + qs.toString() : ''}`),
      ]);
    } catch (err) {
      renderErrorState(viewEl, {
        title: "Can't reach wrkmatch",
        desc: 'Make sure the server is running, then try again.',
        onRetry: renderJobs,
      });
      return;
    }

    state.filters = filters;
    state.jobsList = jobsRes.postings || [];
    state.jobsTotal = jobsRes.total || 0;
    state.jobsMatching = jobsRes.matching || 0;
    paintJobs();
  }

  function paintJobs() {
    viewEl.innerHTML = `
      <div class="filter-bar" id="global-filter-bar"><div class="filter-bar-chips">${globalFilterChipsHtml()}</div></div>
      ${jobsHeaderHtml()}
      <div id="jobs-list"></div>
    `;
    wireGlobalFilterChips(loadJobs);
    wireJobsHeader();
    renderJobsList();
  }

  function jobsHeaderHtml() {
    const matching = state.jobsMatching || 0;
    const total = state.jobsTotal || 0;
    let html = `<div class="jobs-count-line">${matching} role${matching === 1 ? '' : 's'} match your filters</div>`;
    if (total > matching || state.jobsShowAll) {
      html += `<button class="reveal-toggle" id="toggle-jobs-show-all" type="button">
        ${state.jobsShowAll ? 'Show matching only' : `+${total - matching} more hidden by filters — show`}
      </button>`;
    }
    return html;
  }

  function wireJobsHeader() {
    const btn = document.getElementById('toggle-jobs-show-all');
    if (btn) {
      btn.addEventListener('click', () => {
        state.jobsShowAll = !state.jobsShowAll;
        state.jobsShowFilteredOut = false;
        state.jobsShowDone = false;
        loadJobs();
      });
    }
  }

  function jobRowHtml(p) {
    const isDone = p.user_status === 'done';
    const isIgnored = p.user_status === 'ignored';
    const isFilteredOut = !isDone && !isIgnored && p.matches_filters === false;
    const rowClass = isDone ? ' done' : isIgnored ? ' ignored' : isFilteredOut ? ' filtered-out' : '';
    return `
      <div class="posting-row job-row${rowClass}" data-id="${esc(p.id)}">
        <div class="posting-info">
          <div class="posting-title">
            <span class="posting-title-text" title="${esc(p.title)}">${esc(p.title)}</span>
            ${p.is_new ? '<span class="tag new">NEW</span>' : ''}
            ${isIgnored ? '<span class="status-pill">Ignored</span>' : ''}
          </div>
          <button class="job-company-link" type="button" data-company-id="${esc(p.company_id)}">${esc(titleCase(p.company_name))}</button>
          ${jobMetaHtml(p)}
        </div>
        <div class="posting-actions">
          ${p.url ? `<a class="apply-btn" href="${esc(p.url)}" target="_blank" rel="noopener noreferrer">Apply ↗</a>` : ''}
          <button class="icon-btn done-btn${isDone ? ' active' : ''}" type="button" data-action="done" aria-label="Mark done">✓<span class="btn-label">Done</span></button>
          <button class="icon-btn ignore-btn${isIgnored ? ' active' : ''}" type="button" data-action="ignore" aria-label="Ignore">✕<span class="btn-label">Ignore</span></button>
        </div>
      </div>
    `;
  }

  function renderJobsList() {
    const listEl = document.getElementById('jobs-list');
    if (!listEl) return;
    const all = state.jobsList || [];

    let html = '';
    if (!all.length) {
      const desc = state.jobsShowAll
        ? 'No roles found at all — try running a scan.'
        : 'No roles match your current filters.';
      html = `
        <div class="empty-state">
          <div class="icon">💼</div>
          <div class="title">No roles ${state.jobsShowAll ? 'found' : 'match'}</div>
          <div class="desc">${esc(desc)}</div>
        </div>
      `;
      if (!state.jobsShowAll) html += `<button class="clear-filters-btn" id="jobs-clear-filters-btn" type="button">Clear filters</button>`;
    } else if (!state.jobsShowAll) {
      // Default fetch already returns only matching + user_status='none' rows.
      // A row marked done/ignored optimistically just drops out here — the
      // next real fetch wouldn't have included it either.
      const visible = all.filter((p) => p.user_status !== 'done' && p.user_status !== 'ignored');
      html = visible.length ? visible.map(jobRowHtml).join('') : `<div class="hint">No open roles match these filters.</div>`;
    } else {
      const { active, filteredOut, inactive } = bucketByStatusAndFilters(all);
      html = active.length ? active.map(jobRowHtml).join('') : `<div class="hint">No open roles match these filters.</div>`;

      if (filteredOut.length) {
        html += `<button class="hidden-toggle" id="toggle-jobs-filtered" type="button">
          ${state.jobsShowFilteredOut ? 'Hide filtered out' : `${filteredOut.length} filtered out — show`}
        </button>`;
        if (state.jobsShowFilteredOut) html += filteredOut.map(jobRowHtml).join('');
      }
      if (inactive.length) {
        html += `<button class="hidden-toggle" id="toggle-jobs-done" type="button">
          ${state.jobsShowDone ? 'Hide done/ignored' : `${inactive.length} ignored/done hidden — show`}
        </button>`;
        if (state.jobsShowDone) html += inactive.map(jobRowHtml).join('');
      }
    }

    listEl.innerHTML = html;

    const filteredToggle = document.getElementById('toggle-jobs-filtered');
    if (filteredToggle) {
      filteredToggle.addEventListener('click', () => {
        state.jobsShowFilteredOut = !state.jobsShowFilteredOut;
        renderJobsList();
      });
    }
    const doneToggle = document.getElementById('toggle-jobs-done');
    if (doneToggle) {
      doneToggle.addEventListener('click', () => {
        state.jobsShowDone = !state.jobsShowDone;
        renderJobsList();
      });
    }
    const clearBtn = document.getElementById('jobs-clear-filters-btn');
    if (clearBtn) {
      clearBtn.addEventListener('click', async () => {
        try {
          await saveFilters({ location_enabled: false, salary_min: null });
        } catch (err) {
          return;
        }
        state.jobsShowAll = false;
        await loadJobs();
      });
    }

    listEl.querySelectorAll('.job-company-link').forEach((btn) => {
      btn.addEventListener('click', () => { location.hash = `#companies/${btn.dataset.companyId}`; });
    });

    wireJobRowActions();
  }

  function setJobStatus(postingId, nextStatus) {
    return setPostingStatusGeneric(state.jobsList || [], postingId, nextStatus, renderJobsList);
  }

  function wireJobRowActions() {
    document.querySelectorAll('#jobs-list .job-row').forEach((row) => {
      row.querySelectorAll('[data-action]').forEach((btn) => {
        btn.addEventListener('click', () => {
          const postingId = row.dataset.id;
          const targetStatus = POSTING_ACTION_STATUS[btn.dataset.action];
          if (!targetStatus) return;
          const p = (state.jobsList || []).find((x) => String(x.id) === String(postingId));
          if (!p) return;
          const nextStatus = p.user_status === targetStatus ? 'none' : targetStatus;
          setJobStatus(postingId, nextStatus);
        });
      });
    });
  }

  // ---------- coverage tab ----------

  async function renderCoverage() {
    topbarEl.innerHTML = `<div class="topbar-title">Coverage</div>`;
    viewEl.innerHTML = `<div class="skeleton skeleton-card"></div><div class="skeleton skeleton-card"></div>`;
    state.showHiddenCoverage = false;
    await loadCoverage();
  }

  async function loadCoverage() {
    let coverage;
    try {
      coverage = await api(`/api/coverage?include_hidden=${state.showHiddenCoverage ? 1 : 0}`);
    } catch (err) {
      renderErrorState(viewEl, {
        title: "Can't reach wrkmatch",
        desc: 'Make sure the server is running, then try again.',
        onRetry: renderCoverage,
      });
      return;
    }
    state.coverage = coverage;
    paintCoverage();
  }

  function paintCoverage() {
    const coverage = state.coverage;
    const noAts = coverage.no_ats || [];
    const hiddenCount = coverage.hidden_count || 0;

    const listHtml = noAts.length
      ? `<div class="no-ats-list">${noAts.map(noAtsRowHtml).join('')}</div>`
      : `<div class="empty-state">
          <div class="icon">✅</div>
          <div class="title">Full coverage</div>
          <div class="desc">Every company has a discovered job board.</div>
        </div>`;

    let html = `
      <div class="stat-line">${coverage.companies_with_ats} of ${coverage.companies_total} companies scannable</div>
      <div class="hint">These companies' job boards weren't found — check them manually.</div>
      ${listHtml}
    `;
    if (hiddenCount > 0) {
      html += `<button class="reveal-toggle" id="toggle-hidden-coverage" type="button">
        ${state.showHiddenCoverage ? 'Hide ignored' : `${hiddenCount} ignored — show`}
      </button>`;
    }
    viewEl.innerHTML = html;
    wireCoverageActions();
  }

  function noAtsRowHtml(c) {
    const isHidden = c.priority === 'hidden';
    const actionBtn = isHidden
      ? `<button class="icon-btn unhide-btn" type="button" data-action="unhide" data-id="${esc(c.id)}">↺<span class="btn-label">Unhide</span></button>`
      : `<button class="icon-btn ignore-btn" type="button" data-action="ignore" data-id="${esc(c.id)}" aria-label="Ignore">✕<span class="btn-label">Ignore</span></button>`;
    return `
      <div class="no-ats-row${isHidden ? ' muted' : ''}">
        <div class="no-ats-info">
          <div class="no-ats-name">${esc(titleCase(c.name))}</div>
          <div class="no-ats-meta">${c.contact_count} contact${c.contact_count === 1 ? '' : 's'}${c.priority && c.priority !== 'normal' ? ` · ${esc(c.priority)}` : ''}</div>
        </div>
        <div class="no-ats-actions">
          ${c.search_url ? `<a class="find-btn" href="${esc(c.search_url)}" target="_blank" rel="noopener noreferrer">Find jobs ↗</a>` : ''}
          ${actionBtn}
        </div>
      </div>
    `;
  }

  function wireCoverageActions() {
    viewEl.querySelectorAll('.no-ats-row [data-action="ignore"]').forEach((btn) => {
      btn.addEventListener('click', () => ignoreCoverageCompany(btn.dataset.id));
    });
    viewEl.querySelectorAll('.no-ats-row [data-action="unhide"]').forEach((btn) => {
      btn.addEventListener('click', () => unhideCoverageCompany(btn.dataset.id));
    });
    const toggle = document.getElementById('toggle-hidden-coverage');
    if (toggle) {
      toggle.addEventListener('click', () => {
        state.showHiddenCoverage = !state.showHiddenCoverage;
        loadCoverage();
      });
    }
  }

  async function ignoreCoverageCompany(id) {
    const list = state.coverage.no_ats || [];
    const idx = list.findIndex((c) => String(c.id) === String(id));
    if (idx === -1) return;
    const removed = list[idx];
    list.splice(idx, 1);
    state.coverage.hidden_count = (state.coverage.hidden_count || 0) + 1;
    paintCoverage();

    try {
      await post(`/api/companies/${encodeURIComponent(id)}/priority`, { priority: 'hidden' });
      showToast('Company ignored', { actionLabel: 'Undo', onAction: () => undoIgnoreCoverageCompany(id) });
    } catch (err) {
      list.splice(idx, 0, removed);
      state.coverage.hidden_count = Math.max(0, (state.coverage.hidden_count || 1) - 1);
      paintCoverage();
      showToast("Couldn't update — try again");
    }
  }

  async function undoIgnoreCoverageCompany(id) {
    try {
      await post(`/api/companies/${encodeURIComponent(id)}/priority`, { priority: 'normal' });
    } catch (err) {
      showToast("Couldn't undo — try again");
    }
    await loadCoverage();
  }

  async function unhideCoverageCompany(id) {
    try {
      await post(`/api/companies/${encodeURIComponent(id)}/priority`, { priority: 'normal' });
      showToast('Company unhidden');
    } catch (err) {
      showToast("Couldn't update — try again");
    }
    await loadCoverage();
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

    let summary, filters;
    try {
      [summary, filters] = await Promise.all([api('/api/summary'), api('/api/settings/filters')]);
    } catch (err) {
      renderErrorState(viewEl, {
        title: "Can't reach wrkmatch",
        desc: 'Make sure the server is running, then try again.',
        onRetry: renderSettings,
      });
      return;
    }
    state.summary = summary;
    state.filters = filters;
    state.weights = Object.assign({ contacts: 1, postings: 1, freshness: 1 }, summary.weights || {});

    paintSettings();
  }

  function paintSettings() {
    const summary = state.summary;

    viewEl.innerHTML = `
      <div class="section-title">Filters</div>
      <div class="settings-card" id="filters-card">
        <div class="filter-bar-chips">${globalFilterChipsHtml()}</div>
        <div class="slider-hint">Location narrows matches to Remote + Boston-area postings. Salary sets a minimum pay floor. Both apply everywhere — Companies list, scores, and counts.</div>
      </div>

      <div class="section-title">Score weights</div>
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

    wireGlobalFilterChips(paintSettings);

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
