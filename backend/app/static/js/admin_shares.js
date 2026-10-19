/**
 * admin_shares.js – ABOODVIDEO Admin V2
 * Manages the shares listing page: fetch, render, filter, copy & revoke.
 */

(function () {
  'use strict';

  /* ── State ──────────────────────────────────────────────────── */
  let allShares    = [];   // current bounded API page
  let activeTab    = 'all';
  let currentPage  = 1;
  let pageSize     = 20;
  let pageCount    = 0;
  let shareTotal   = 0;
  let searchTerm   = '';
  let sortBy       = 'created_at';
  let sortDirection = 'desc';
  let searchTimer  = null;
  let pendingId    = null; // share ID queued for revoke
  let modalReturnFocus = null;

  /* ── DOM Refs ───────────────────────────────────────────────── */
  const tableBody      = document.getElementById('sharesTableBody');
  const emptyRow       = document.getElementById('sharesEmptyRow');
  const statActive     = document.getElementById('stat-shares-active');
  const statExpired    = document.getElementById('stat-shares-expired');
  const statRevoked    = document.getElementById('stat-shares-revoked');
  const tabsBar        = document.getElementById('sharesTabs');
  const revokeModal    = document.getElementById('revokeModal');
  const revokeConfirm  = document.getElementById('revokeConfirmBtn');
  const revokeCancel   = document.getElementById('revokeCancelBtn');
  const revokeClose    = document.getElementById('revokeModalClose');
  const searchInput    = document.getElementById('sharesSearch');
  const sortSelect     = document.getElementById('sharesSort');
  const pageSizeSelect = document.getElementById('sharesPageSize');
  const pageStatus     = document.getElementById('sharesPageStatus');
  const prevButton     = document.getElementById('sharesPrev');
  const nextButton     = document.getElementById('sharesNext');

  /* ── Helpers ────────────────────────────────────────────────── */

  /**
   * Show a toast notification.
   * Falls back gracefully if the global showToast is unavailable.
   */
  function showToast(message, type = 'info') {
    if (typeof window.showToast === 'function') {
      window.showToast(message, type);
      return;
    }
    // Minimal fallback toast
    const toast = document.createElement('div');
    toast.textContent = message;
    Object.assign(toast.style, {
      position: 'fixed',
      bottom: '24px',
      right: '24px',
      background: type === 'success' ? '#22c55e'
                : type === 'error'   ? '#ef4444'
                :                      '#7c6af7',
      color: '#fff',
      padding: '10px 18px',
      borderRadius: '8px',
      fontSize: '14px',
      zIndex: '99999',
      boxShadow: '0 4px 16px rgba(0,0,0,.4)',
      fontFamily: 'inherit',
      direction: 'rtl',
    });
    document.body.appendChild(toast);
    setTimeout(() => toast.remove(), 3000);
  }

  /** Format an ISO date string to a readable Arabic locale date. */
  function formatDate(isoStr) {
    if (!isoStr) return '—';
    try {
      return new Date(isoStr).toLocaleDateString('ar-SA', {
        year: 'numeric', month: 'short', day: 'numeric',
      });
    } catch {
      return isoStr;
    }
  }

  /** Determine share status: active | expired | revoked */
  function resolveStatus(share) {
    if (share.is_revoked || share.status === 'revoked') return 'revoked';
    if (share.expires_at && new Date(share.expires_at) < new Date()) return 'expired';
    if (share.status === 'expired') return 'expired';
    return 'active';
  }

  /** Arabic label for a status key */
  const STATUS_LABELS = { active: 'نشط', expired: 'منتهي', revoked: 'ملغى' };

  /** CSS class for type badge */
  function typeBadgeClass(type) {
    const t = (type || '').toLowerCase();
    if (t.includes('series')) return 'type-series';
    if (t.includes('season')) return 'type-season';
    if (t.includes('video'))  return 'type-video';
    if (t.includes('folder')) return 'type-folder';
    if (t.includes('file'))   return 'type-file';
    return 'type-default';
  }

  /** Arabic label for content type */
  function typeLabel(type) {
    const t = (type || '').toLowerCase();
    if (t.includes('series')) return 'مسلسل';
    if (t.includes('season')) return 'موسم';
    if (t.includes('video'))  return 'فيديو';
    if (t.includes('folder')) return 'مجلد';
    if (t.includes('file'))   return 'ملف';
    return type || '—';
  }

  /* ── Render ─────────────────────────────────────────────────── */

  function renderTable(shares) {
    // Remove all rows except the empty-state row
    Array.from(tableBody.querySelectorAll('tr:not(#sharesEmptyRow)')).forEach(r => r.remove());

    if (!shares || shares.length === 0) {
      emptyRow.hidden = false;
      emptyRow.querySelector('.empty-state-text').textContent = 'لا توجد مشاركات في هذه الفئة';
      return;
    }

    emptyRow.hidden = true;

    shares.forEach(share => {
      const status   = resolveStatus(share);
      const name     = share.target_name || share.name || share.title || share.file_name || 'غير محدد';
      const type     = share.target_type || share.type || share.content_type || '';
      const url      = share.share_url || share.url || share.link || '';
      const created  = formatDate(share.created_at);
      const expires  = formatDate(share.expires_at);
      const views = share.views ?? share.view_count ?? '—';

      const tr = document.createElement('tr');
      tr.dataset.status = status;
      tr.dataset.id     = share.token || share.id || '';

      tr.innerHTML = `
        <td>
          <div class="share-content-cell">
            <span class="share-content-name" title="${escHtml(name)}">${escHtml(name)}</span>
            <span class="share-type-badge ${typeBadgeClass(type)}">${escHtml(typeLabel(type))}</span>
          </div>
        </td>
        <td><span class="status-badge ${status}">${escHtml(STATUS_LABELS[status] || status)}</span></td>
        <td><span class="share-link-value" dir="ltr" title="${escAttr(url)}">${escHtml(url || '—')}</span></td>
        <td>${escHtml(created)}</td>
        <td>${escHtml(expires)}</td>
        <td>${escHtml(String(views))}</td>
        <td>
          <div class="shares-actions">
            <button
              class="icon-btn copy-btn"
              title="نسخ الرابط"
              aria-label="نسخ الرابط"
              data-url="${escAttr(url)}"
              ${!url ? 'disabled' : ''}
            ><svg class="av-icon" aria-hidden="true"><use href="/static/icons.svg#shares"></use></svg></button>
            <button
              class="icon-btn danger revoke-btn"
              title="إلغاء الرابط"
              aria-label="إلغاء الرابط"
              data-id="${escAttr(String(share.token || share.id || ''))}"
              ${status === 'revoked' ? 'disabled' : ''}
            ><svg class="av-icon" aria-hidden="true"><use href="/static/icons.svg#trash"></use></svg></button>
          </div>
        </td>
      `;

      tableBody.appendChild(tr);
    });

    // Bind row-level events
    bindRowEvents();
  }

  /** Simple HTML escaping */
  function escHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/`/g, '&#96;')
      .replace(/\$/g, '&#36;');
  }
  function escAttr(str) { return escHtml(str); }

  function csrfToken() {
    const prefix = (document.body.dataset.csrfCookieName || 'aboodvideo_csrf') + '=';
    const cookie = document.cookie.split(';').map(part => part.trim()).find(part => part.startsWith(prefix));
    if (!cookie) return '';
    try { return decodeURIComponent(cookie.slice(prefix.length)); }
    catch (_) { return cookie.slice(prefix.length); }
  }

  /* ── Stat Counts ─────────────────────────────────────────────── */

  function updateStats(counts) {
    if (counts) {
      statActive.textContent = counts.active ?? 0;
      statExpired.textContent = counts.expired ?? 0;
      statRevoked.textContent = counts.revoked ?? 0;
      updateTabCounts(counts);
      return;
    }
    const fallbackCounts = { active: 0, expired: 0, revoked: 0 };
    (allShares || []).forEach(s => {
      const st = resolveStatus(s);
      if (fallbackCounts[st] !== undefined) fallbackCounts[st]++;
    });
    statActive.textContent = fallbackCounts.active;
    statExpired.textContent = fallbackCounts.expired;
    statRevoked.textContent = fallbackCounts.revoked;
    updateTabCounts(fallbackCounts);
  }

  function updateTabCounts(counts) {
    const values = {
      all: Number(counts.active || 0) + Number(counts.expired || 0) + Number(counts.revoked || 0),
      active: Number(counts.active || 0),
      expired: Number(counts.expired || 0),
      revoked: Number(counts.revoked || 0),
    };
    tabsBar.querySelectorAll('[data-share-count]').forEach(badge => {
      const count = values[badge.dataset.shareCount];
      if (count !== undefined) badge.textContent = count.toLocaleString('ar-SA');
    });
  }

  /* ── Tab Filtering ───────────────────────────────────────────── */

  function applyFilter(tab) {
    activeTab = tab;
    currentPage = 1;
    fetchShares();
  }

  tabsBar.addEventListener('click', e => {
    const btn = e.target.closest('.tab-btn');
    if (!btn) return;

    tabsBar.querySelectorAll('.tab-btn').forEach(b => {
      b.classList.remove('is-active');
      b.setAttribute('aria-selected', 'false');
    });
    btn.classList.add('is-active');
    btn.setAttribute('aria-selected', 'true');
    applyFilter(btn.dataset.tab);
  });

  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      searchTerm = searchInput.value.trim();
      currentPage = 1;
      fetchShares();
    }, 250);
  });
  sortSelect.addEventListener('change', () => {
    [sortBy, sortDirection] = sortSelect.value.split(':');
    currentPage = 1;
    fetchShares();
  });
  pageSizeSelect.addEventListener('change', () => {
    pageSize = Number(pageSizeSelect.value) || 20;
    currentPage = 1;
    fetchShares();
  });
  prevButton.addEventListener('click', () => {
    if (currentPage > 1) { currentPage--; fetchShares(); }
  });
  nextButton.addEventListener('click', () => {
    if (currentPage < pageCount) { currentPage++; fetchShares(); }
  });

  /* ── Row Events (copy + revoke) ──────────────────────────────── */

  function bindRowEvents() {
    // Copy link buttons
    tableBody.querySelectorAll('.copy-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const url = btn.dataset.url;
        if (!url) {
          showToast('لا يوجد رابط متاح', 'error');
          return;
        }
        navigator.clipboard.writeText(url)
          .then(() => showToast('تم نسخ الرابط', 'success'))
          .catch(() => showToast('فشل نسخ الرابط', 'error'));
      });
    });

    // Revoke buttons → open modal
    tableBody.querySelectorAll('.revoke-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        pendingId = btn.dataset.id;
        openRevokeModal();
      });
    });
  }

  /* ── Revoke Modal ────────────────────────────────────────────── */

  function openRevokeModal() {
    modalReturnFocus = document.activeElement;
    revokeModal.removeAttribute('hidden');
    revokeConfirm.disabled = false;
    revokeConfirm.textContent = 'تأكيد الإلغاء';
    document.addEventListener('keydown', onEscClose);
    revokeClose.focus();
  }

  function closeRevokeModal() {
    revokeModal.setAttribute('hidden', '');
    pendingId = null;
    document.removeEventListener('keydown', onEscClose);
    if (modalReturnFocus && modalReturnFocus.isConnected) modalReturnFocus.focus();
    modalReturnFocus = null;
  }

  function onEscClose(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      closeRevokeModal();
      return;
    }
    if (e.key !== 'Tab') return;
    const focusable = [...revokeModal.querySelectorAll('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')]
      .filter((element) => element.getClientRects().length > 0);
    if (!focusable.length) {
      e.preventDefault();
      revokeModal.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && (document.activeElement === first || !revokeModal.contains(document.activeElement))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (document.activeElement === last || !revokeModal.contains(document.activeElement))) {
      e.preventDefault();
      first.focus();
    }
  }

  revokeCancel.addEventListener('click', closeRevokeModal);
  revokeClose.addEventListener('click',  closeRevokeModal);

  revokeModal.addEventListener('click', e => {
    if (e.target === revokeModal) closeRevokeModal();
  });

  revokeConfirm.addEventListener('click', async () => {
    if (!pendingId) return;

    revokeConfirm.disabled = true;
    revokeConfirm.textContent = 'جاري الإلغاء…';

    try {
      const res = await fetch(`/api/v1/drive/shares/${pendingId}/revoke`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
        credentials: 'same-origin',
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      await fetchShares();
      showToast('تم إلغاء الرابط بنجاح', 'success');
      closeRevokeModal();
    } catch (err) {
      console.error('[admin_shares] revoke error:', err);
      showToast('فشل إلغاء الرابط، حاول مجدداً', 'error');
      revokeConfirm.disabled = false;
      revokeConfirm.textContent = 'تأكيد الإلغاء';
    }
  });

  /* ── Data Fetch ──────────────────────────────────────────────── */

  async function fetchShares() {
    try {
      const params = new URLSearchParams({ page: String(currentPage), limit: String(pageSize), sort: sortBy, direction: sortDirection });
      if (activeTab !== 'all') params.set('status', activeTab);
      if (searchTerm) params.set('q', searchTerm);
      const res = await fetch(`/api/v1/drive/shares?${params}`, { credentials: 'same-origin' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      allShares = Array.isArray(json.shares) ? json.shares : [];
      shareTotal = Number(json.total) || 0;
      pageCount = Number(json.pages) || 0;
      if (currentPage > Math.max(pageCount, 1)) {
        currentPage = Math.max(pageCount, 1);
        return fetchShares();
      }
      updateStats(json.status_counts);
      renderTable(allShares);
      pageStatus.textContent = `${currentPage} / ${pageCount} · ${shareTotal}`;
      prevButton.disabled = currentPage <= 1;
      nextButton.disabled = currentPage >= pageCount;
    } catch (err) {
      console.error('[admin_shares] list request failed:', err);
      allShares = [];
      shareTotal = 0;
      pageCount = 0;
      renderTable([]);
      pageStatus.textContent = '—';
      prevButton.disabled = true;
      nextButton.disabled = true;
      showToast('تعذّر تحميل المشاركات', 'error');
    }
  }

  /* ── Init ────────────────────────────────────────────────────── */
  fetchShares();

})();
