window.AVi18n ||= {t: value => value, tpl: (parts, ...values) => parts.reduce((out, part, i) => out + part + (i < values.length ? values[i] : ''), '')};
/**
 * admin_shell.js — ABOODVIDEO Admin V2
 * Handles: sidebar toggle, logout, processing badge polling,
 * storage meter, global search, toast system, system status pill.
 * RTL-first. ES6+. No external dependencies.
 */

(() => {
  'use strict';

  /* ─────────────────────────────────────────────
   * 1. SIDEBAR TOGGLE (mobile)
   * ───────────────────────────────────────────── */
  const initSidebar = () => {
    const toggleBtn = document.getElementById('sidebarToggleBtn');
    if (!toggleBtn) return;

    // Create backdrop if not already in DOM
    let backdrop = document.getElementById('sidebarBackdrop');
    if (!backdrop) {
      backdrop = document.createElement('div');
      backdrop.id = 'sidebarBackdrop';
      backdrop.className = 'sidebar-backdrop';
      document.body.appendChild(backdrop);
    }

    const sidebar = document.getElementById('adminSidebar');
    if (!sidebar) return;
    const mobile = window.matchMedia('(max-width: 900px)');
    const main = document.querySelector('.main-content');
    const closeBtn = document.getElementById('sidebarCloseBtn');
    let previousFocus = null;
    const focusable = () => [...sidebar.querySelectorAll('a[href], button:not([disabled]), summary')]
      .filter((element) => element.getClientRects().length > 0);

    const sync = () => {
      const open = mobile.matches && document.body.classList.contains('sidebar-open');
      sidebar.inert = mobile.matches && !open;
      if (main) main.inert = open;
      toggleBtn.setAttribute('aria-expanded', String(open));
      toggleBtn.setAttribute('aria-label', open ? window.AVi18n.t('إغلاق القائمة') : window.AVi18n.t('فتح القائمة'));
    };
    const open = () => {
      if (!mobile.matches) return;
      previousFocus = document.activeElement;
      document.body.classList.add('sidebar-open');
      backdrop.classList.add('active');
      sync();
      (closeBtn || focusable()[0] || sidebar).focus();
    };
    const close = (restoreFocus = true) => {
      const wasOpen = document.body.classList.contains('sidebar-open');
      document.body.classList.remove('sidebar-open');
      backdrop.classList.remove('active');
      sync();
      if (wasOpen && restoreFocus) (previousFocus || toggleBtn).focus();
    };
    toggleBtn.addEventListener('click', () => {
      document.body.classList.contains('sidebar-open') ? close() : open();
    });
    closeBtn?.addEventListener('click', () => close());
    backdrop.addEventListener('click', () => close());
    sidebar.addEventListener('click', (event) => {
      if (mobile.matches && event.target.closest('a[href]')) close(false);
    });
    document.addEventListener('keydown', (event) => {
      if (!mobile.matches || !document.body.classList.contains('sidebar-open')) return;
      if (event.key === 'Escape') { event.preventDefault(); close(); return; }
      if (event.key !== 'Tab') return;
      const items = focusable();
      const first = items[0], last = items[items.length - 1];
      if (!first) return;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    mobile.addEventListener('change', () => {
      const focusWasInSidebar = sidebar.contains(document.activeElement);
      close(false);
      if (mobile.matches && focusWasInSidebar) toggleBtn.focus();
    });

    // Match the actual page, including settings tabs and the library trash filter.
    const syncCurrentLink = () => {
      const here = new URL(window.location.href);
      const links = [...sidebar.querySelectorAll('.nav-item[href]')];
      const path = here.pathname;
      let match = null;
      if ((path === '/admin/settings' || path === '/admin/upload') && here.hash) {
        match = links.find((link) => new URL(link.href).pathname === path && new URL(link.href).hash === here.hash);
      } else if (['/admin/videos', '/admin/content', '/admin/library', '/admin/drive'].includes(path)) {
        const trash = here.searchParams.get('filter') === 'trash';
        match = links.find((link) => {
          const target = new URL(link.href);
          return target.pathname === '/admin/videos' && (target.searchParams.get('filter') === 'trash') === trash;
        });
      }
      if (!match) return;
      links.forEach((link) => { link.classList.remove('is-active'); link.removeAttribute('aria-current'); });
      match.classList.add('is-active');
      match.setAttribute('aria-current', 'page');
      const section = match.closest('details');
      if (section) section.open = true;
    };
    document.addEventListener('click', (event) => {
      if (event.target.closest('.settings-tab-btn[data-tab]')) window.requestAnimationFrame(syncCurrentLink);
    });
    window.addEventListener('hashchange', syncCurrentLink);
    window.addEventListener('popstate', syncCurrentLink);
    syncCurrentLink();
    sync();
  };

  /* ─────────────────────────────────────────────
   * 2. LOGOUT
   * ───────────────────────────────────────────── */
  const initLogout = () => {
    const btn = document.getElementById('logout-btn');
    if (!btn) return;

    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      try {
        await fetch('/api/v1/auth/logout', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
        });
      } catch (_) {
        // Proceed to redirect even if the request fails
      } finally {
        window.location.href = '/admin/login';
      }
    });
  };

  /* ─────────────────────────────────────────────
   * 3. PROCESSING BADGE — poll every 15 s
   * ───────────────────────────────────────────── */
  const POLL_INTERVAL_MS = 15_000;

  const updateProcessingBadges = (count) => {
    const badges = document.querySelectorAll('[data-processing-badge]');
    badges.forEach((el) => {
      el.textContent = count;
      if (count > 0) {
        el.classList.remove('hidden');
      } else {
        el.classList.add('hidden');
      }
      if (el.matches('.console-activity-chip b')) el.parentElement.classList.toggle('hidden', count === 0);
    });
  };

  const fetchProcessingCount = async () => {
    try {
      const res = await fetch('/api/v1/admin/transcodes?status=PROCESSING&limit=1', {
        credentials: 'include',
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      // Support both {count: N} and {items: [...]} response shapes
      const count =
        typeof data.count === 'number'
          ? data.count
          : typeof data.total === 'number'
          ? data.total
          : Array.isArray(data.items)
          ? data.items.length
          : Array.isArray(data)
          ? data.length
          : 0;
      updateProcessingBadges(count);
    } catch (err) {
      console.warn('[admin_shell] Processing badge fetch failed:', window.AVi18n.t(err.message));
    }
  };

  const initProcessingBadge = () => {
    fetchProcessingCount();
    setInterval(fetchProcessingCount, POLL_INTERVAL_MS);
  };

  /* ─────────────────────────────────────────────
   * 4. STORAGE METER
   * ───────────────────────────────────────────── */
  const initStorageMeter = async () => {
    const fill = document.getElementById('storageMeterFill');
    const label = document.getElementById('storageMeterVal');
    if (!fill && !label) return;

    const applyStorage = (used, total) => {
      if (!used || !total) return;
      const pct = Math.min(100, Math.round((used / total) * 100));
      if (fill) {
        fill.style.width = `${pct}%`;
        // Visual warning colours
        fill.classList.toggle('meter-warn', pct >= 70 && pct < 90);
        fill.classList.toggle('meter-danger', pct >= 90);
      }
      if (label) {
        label.textContent = `${formatBytes(used)} / ${formatBytes(total)} (${pct}%)`;
      }
    };

    // Try dedicated storage endpoint first, fall back to /api/v1/health
    const endpoints = ['/api/v1/health/storage', '/api/v1/health'];
    for (const url of endpoints) {
      try {
        const res = await fetch(url, { credentials: 'include' });
        if (!res.ok) continue;
        const data = await res.json();

        // Normalise various response shapes
        const storage = data.details ?? data.storage ?? data;
        const used =
          storage.used_bytes ??
          storage.used ??
          data.disk_used_bytes ??
          data.disk_used ??
          null;
        const total =
          storage.total_bytes ??
          storage.total ??
          data.disk_total_bytes ??
          data.disk_total ??
          null;

        if (used !== null && total !== null) {
          applyStorage(used, total);
          break;
        }
      } catch (_) {
        // try next endpoint
      }
    }
  };

  const formatBytes = (bytes) => {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
  };

  /* ─────────────────────────────────────────────
   * 5. GLOBAL SEARCH
   * ───────────────────────────────────────────── */
  const initGlobalSearch = () => {
    const input = document.getElementById('global-search');
    if (!input) return;

    input.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      const q = input.value.trim();
      if (!q) return;
      window.location.href = `/admin/videos?q=${encodeURIComponent(q)}`;
    });
  };

  /* ─────────────────────────────────────────────
   * 6. TOAST SYSTEM
   * ───────────────────────────────────────────── */
  const TOAST_DURATION_MS = 4_000;

  const ensureToastContainer = () => {
    let container = document.getElementById('toast-container');
    if (!container) {
      container = document.createElement('div');
      container.id = 'toast-container';
      container.setAttribute('aria-live', 'polite');
      container.setAttribute('aria-atomic', 'false');
      document.body.appendChild(container);
    }
    return container;
  };

  /**
   * Show a toast notification.
   * @param {string} msg    - Message text (plain text, not HTML).
   * @param {'success'|'error'|'info'|'warning'} [type='info']
   */
  window.showToast = (msg, type = 'info') => {
    const container = ensureToastContainer();

    const icons = {
      success: '✔',
      error: '✖',
      info: 'ℹ',
      warning: '!' ,
    };

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.setAttribute('role', 'alert');
    toast.innerHTML = window.AVi18n.tpl`
      <span class="toast-icon">${icons[type] ?? icons.info}</span>
      <span class="toast-msg">${escapeHtml(msg)}</span>
      <button class="toast-close" aria-label="إغلاق">×</button>
    `;

    const remove = () => {
      toast.classList.add('toast-exit');
      toast.addEventListener('animationend', () => toast.remove(), { once: true });
    };

    toast.querySelector('.toast-close').addEventListener('click', remove);
    container.appendChild(toast);

    // Trigger enter animation on next frame
    requestAnimationFrame(() => toast.classList.add('toast-enter'));

    setTimeout(remove, TOAST_DURATION_MS);
  };

  const escapeHtml = (str) =>
    String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');

  /* ─────────────────────────────────────────────
   * 7. SYSTEM STATUS PILL
   * ───────────────────────────────────────────── */
  const initSystemStatusPill = async () => {
    const pill = document.getElementById('systemStatusPill');
    if (!pill) return;

    try {
      const res = await fetch('/api/v1/health', {
        credentials: 'include',
        signal: AbortSignal.timeout(6_000),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      // Allow backend to explicitly signal degraded state
      if (data.status && data.status !== 'ok' && data.status !== 'healthy') {
        setStatusError(pill);
      } else {
        setStatusOk(pill);
      }
    } catch (_) {
      setStatusError(pill);
    }
  };

  const setStatusOk = (pill) => {
    pill.innerHTML = window.AVi18n.t('<span class="status-dot status-dot--green"></span><span>متصل</span>');
    pill.classList.remove('pill--error');
    pill.classList.add('pill--ok');
  };

  const setStatusError = (pill) => {
    pill.innerHTML = window.AVi18n.t('<span class="status-dot status-dot--red"></span><span>خطأ</span>');
    pill.classList.remove('pill--ok');
    pill.classList.add('pill--error');
  };

  /* ─────────────────────────────────────────────
   * BOOT
   * ───────────────────────────────────────────── */
  const boot = () => {
    initSidebar();
    initLogout();
    initProcessingBadge();
    initStorageMeter();
    initGlobalSearch();
    initSystemStatusPill();
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
