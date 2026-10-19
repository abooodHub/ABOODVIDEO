(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = { userOffset: 0, auditOffset: 0, limit: 50, userQuery: '', auditAction: '', auditResult: '' };
  const labels = {
    database: 'PostgreSQL', redis: 'Redis', storage: 'التخزين',
    published_media: 'الوسائط المنشورة', trash_media: 'سلة المحذوفات',
    unpublished_video_storage: 'وسائط غير منشورة', orphan_video_storage: 'وسائط بلا سجل فيديو',
    upload_staging: 'رفع الملفات', studio_package_staging: 'حزم Studio المؤقتة',
    temporary_processing: 'المعالجة المؤقتة', source_import: 'استيراد المصدر',
    other_managed_storage: 'ملفات مُدارة أخرى',
  };

  function csrfToken() {
    const prefix = (document.body.dataset.csrfCookieName || 'aboodvideo_csrf') + '=';
    const item = document.cookie.split('; ').find((part) => part.startsWith(prefix));
    return item ? decodeURIComponent(item.slice(prefix.length)) : '';
  }

  async function api(url, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set('Accept', 'application/json');
    if (options.body) headers.set('Content-Type', 'application/json');
    if (options.method && !['GET', 'HEAD'].includes(options.method.toUpperCase())) {
      headers.set('X-CSRF-Token', csrfToken());
    }
    const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...options, headers });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    return body;
  }

  function status(el, message, isError = false) {
    if (!el) return;
    el.textContent = message;
    el.dataset.state = isError ? 'error' : 'ok';
  }

  function cell(row, value) {
    const td = document.createElement('td');
    td.textContent = value == null || value === '' ? '—' : String(value);
    row.append(td);
    return td;
  }

  async function loadUsers() {
    const root = document.querySelector('[data-page="users"] .platform-page');
    if (!root) return;
    const body = $('users-rows');
    const stat = $('users-status');
    status(stat, 'جارٍ تحميل الحسابات…');
    try {
      const query = new URLSearchParams({ limit: String(state.limit), offset: String(state.userOffset) });
      if (state.userQuery) query.set('q', state.userQuery);
      const data = await api(`/api/v1/admin/users?${query}`);
      body.replaceChildren();
      for (const user of data.items) {
        const tr = document.createElement('tr');
        cell(tr, user.username);
        cell(tr, user.email);
        const roleCell = cell(tr, user.role);
        cell(tr, user.active ? 'نشط' : 'معطّل');
        cell(tr, user.created_at ? new Date(user.created_at).toLocaleDateString('ar') : '—');
        if (root.dataset.canManage === 'true') {
          const actions = document.createElement('td');
          const rowActions = document.createElement('div');
          rowActions.className = 'platform-inline-action';
          if (user.id !== root.dataset.userId) {
            const select = document.createElement('select');
            select.setAttribute('aria-label', `رتبة ${user.username}`);
            for (const [value, label] of [['USER', 'مستخدم'], ['VIP', 'VIP'], ['ADMIN', 'مدير'], ['OWNER', 'مالك'], ['BANNED', 'محظور']]) {
              const option = document.createElement('option'); option.value = value; option.textContent = label;
              option.selected = user.role === value; select.append(option);
            }
            const save = document.createElement('button'); save.type = 'button'; save.className = 'btn btn-secondary'; save.textContent = 'حفظ الرتبة';
            save.addEventListener('click', async () => {
              try { await api(`/api/v1/admin/users/${encodeURIComponent(user.id)}/role`, { method: 'PATCH', body: JSON.stringify({ role: select.value }) }); await loadUsers(); }
              catch (error) { status(stat, error.message, true); }
            });
            const toggle = document.createElement('button'); toggle.type = 'button'; toggle.className = 'btn btn-secondary'; toggle.textContent = user.active ? 'تعطيل' : 'تفعيل';
            toggle.addEventListener('click', async () => {
              try { await api(`/api/v1/admin/users/${encodeURIComponent(user.id)}/status`, { method: 'PATCH', body: JSON.stringify({ active: !user.active }) }); await loadUsers(); }
              catch (error) { status(stat, error.message, true); }
            });
            rowActions.append(select, save, toggle);
          } else {
            rowActions.textContent = 'الحساب الحالي';
          }
          actions.append(rowActions); tr.append(actions);
        }
        body.append(tr);
      }
      const end = Math.min(state.userOffset + data.items.length, data.total);
      $('users-page').textContent = `${data.total ? state.userOffset + 1 : 0}–${end} من ${data.total}`;
      $('users-prev').disabled = state.userOffset === 0;
      $('users-next').disabled = end >= data.total;
      status(stat, data.items.length ? `تم عرض ${data.items.length} حسابات.` : 'لا توجد حسابات مطابقة.');
    } catch (error) { status(stat, error.message, true); }
  }

  function setupUsers() {
    const search = $('users-search');
    if (!search) return;
    let timer;
    search.addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => { state.userQuery = search.value.trim(); state.userOffset = 0; loadUsers(); }, 250);
    });
    $('users-refresh').addEventListener('click', loadUsers);
    $('users-prev').addEventListener('click', () => { state.userOffset = Math.max(0, state.userOffset - state.limit); loadUsers(); });
    $('users-next').addEventListener('click', () => { state.userOffset += state.limit; loadUsers(); });
    const dialog = $('user-create-dialog');
    const form = $('user-create-form');
    if (dialog && form) {
      $('user-create-open').addEventListener('click', () => {
        dialog.showModal();
        dialog.querySelector('input:not([type="hidden"]):not([disabled])')?.focus();
      });
      dialog.addEventListener('keydown', (event) => {
        if (event.key !== 'Tab') return;
        const focusable = [...dialog.querySelectorAll(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        )].filter((element) => element.getClientRects().length > 0);
        if (!focusable.length) {
          event.preventDefault();
          dialog.focus();
          return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
          event.preventDefault();
          first.focus();
        }
      });
      $('user-create-cancel').addEventListener('click', () => dialog.close());
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const data = new FormData(form);
        const payload = Object.fromEntries(data.entries());
        const error = $('user-create-error'); error.textContent = '';
        try {
          await api('/api/v1/admin/users', { method: 'POST', body: JSON.stringify(payload) });
          form.reset(); dialog.close(); state.userOffset = 0; await loadUsers();
        } catch (err) { error.textContent = err.message; }
      });
    }
    loadUsers();
  }

  async function loadAudit() {
    if (!$('audit-rows')) return;
    const target = $('audit-rows'); const stat = $('audit-status');
    status(stat, 'جارٍ تحميل الأحداث…');
    try {
      const query = new URLSearchParams({ limit: String(state.limit), offset: String(state.auditOffset) });
      if (state.auditAction) query.set('action', state.auditAction);
      if (state.auditResult) query.set('result', state.auditResult);
      const data = await api(`/api/v1/admin/audit?${query}`);
      target.replaceChildren();
      for (const event of data.items) {
        const tr = document.createElement('tr');
        cell(tr, new Date(event.created_at).toLocaleString('ar'));
        cell(tr, event.action);
        cell(tr, event.actor_id);
        cell(tr, [event.target_type, event.target_id].filter(Boolean).join(' · '));
        cell(tr, event.result);
        cell(tr, event.request_id);
        target.append(tr);
      }
      const end = Math.min(state.auditOffset + data.items.length, data.total);
      $('audit-page').textContent = `${data.total ? state.auditOffset + 1 : 0}–${end} من ${data.total}`;
      $('audit-prev').disabled = state.auditOffset === 0;
      $('audit-next').disabled = end >= data.total;
      status(stat, data.items.length ? `تم عرض ${data.items.length} حدثًا.` : 'لا توجد أحداث مطابقة.');
    } catch (error) { status(stat, error.message, true); }
  }

  function setupAudit() {
    const form = $('audit-filters'); if (!form) return;
    form.addEventListener('submit', (event) => {
      event.preventDefault(); state.auditOffset = 0;
      state.auditAction = $('audit-action').value.trim(); state.auditResult = $('audit-result').value;
      loadAudit();
    });
    $('audit-prev').addEventListener('click', () => { state.auditOffset = Math.max(0, state.auditOffset - state.limit); loadAudit(); });
    $('audit-next').addEventListener('click', () => { state.auditOffset += state.limit; loadAudit(); });
    loadAudit();
  }

  function renderFacts(target, facts) {
    target.replaceChildren();
    for (const [key, value] of Object.entries(facts)) {
      const wrap = document.createElement('div'); const dt = document.createElement('dt'); const dd = document.createElement('dd');
      dt.textContent = labels[key] || key; dd.textContent = String(value); wrap.append(dt, dd); target.append(wrap);
    }
  }

  const bytes = (value) => {
    if (value == null) return 'غير متاح';
    const units = ['بايت', 'ك.بايت', 'م.بايت', 'ج.بايت', 'ت.بايت'];
    let n = Number(value); let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return `${n.toFixed(i ? 2 : 0)} ${units[i]}`;
  };

  async function loadStorage() {
    if (!$('storage-categories')) return;
    status($('storage-status'), 'جارٍ فحص التخزين…');
    try {
      const data = await api('/api/v1/admin/storage/summary');
      const cards = $('storage-categories'); cards.replaceChildren();
      for (const [name, value] of Object.entries(data.categories || {})) {
        const card = document.createElement('article'); card.className = 'platform-card';
        const h = document.createElement('h2'); h.textContent = labels[name] || name;
        const strong = document.createElement('strong'); strong.textContent = bytes(value);
        card.append(h, strong); cards.append(card);
      }
      const orphanList = $('storage-orphans');
      orphanList.replaceChildren();
      const orphanRows = [
        ...(data.orphan_video_directories || []).map((identity) => ({ kind: 'video', identity })),
        ...(data.orphan_temporary_resources?.candidates || []),
      ];
      if (!orphanRows.length) {
        const item = document.createElement('li'); item.textContent = 'لم تُكتشف عناصر مؤقتة يتيمة ضمن المسارات المعروفة.'; orphanList.append(item);
      } else {
        for (const row of orphanRows) {
          const item = document.createElement('li'); item.textContent = `${row.kind}: ${row.identity}`; orphanList.append(item);
        }
      }
      renderFacts($('storage-filesystem'), {
        الحالة: data.filesystem_status,
        الحجم_الإجمالي: bytes(data.filesystem?.total_bytes),
        المستخدم: bytes(data.filesystem?.used_bytes),
        المتاح: bytes(data.filesystem?.free_bytes),
        اكتمال_الفحص: data.scan_complete ? 'مكتمل' : 'غير مكتمل',
        الملفات_المعدودة: data.file_count,
      });
      status($('storage-status'), data.scan_complete ? 'اكتمل الفحص من الملفات الحالية.' : 'الفحص غير مكتمل؛ راجع توفر مسارات التخزين. ', !data.scan_complete);
    } catch (error) { status($('storage-status'), error.message, true); }
  }

  async function loadSystem() {
    if (!$('system-services')) return;
    status($('system-status'), 'جارٍ فحص الخدمات…');
    const [overall, worker, reconciler] = await Promise.allSettled([
      api('/api/v1/health/readiness'), api('/api/v1/health/worker'), api('/api/v1/health/reconciler'),
    ]);
    const container = $('system-services'); container.replaceChildren();
    const services = overall.status === 'fulfilled' ? overall.value : null;
    for (const [name, result] of Object.entries(services?.services || {})) {
      const card = document.createElement('article'); card.className = 'platform-card';
      const h = document.createElement('h2'); h.textContent = labels[name] || name;
      const strong = document.createElement('strong'); strong.textContent = String(result);
      card.append(h, strong); container.append(card);
    }
    const facts = {};
    facts['صحة التطبيق'] = services?.status || 'غير متاح';
    facts['إصدار التطبيق'] = services?.version || 'غير متاح';
    facts['حالة العامل'] = worker.status === 'fulfilled' ? worker.value.worker_status : 'غير متاح';
    facts['عمق الطابور'] = worker.status === 'fulfilled' ? worker.value.queue_depth : 'غير متاح';
    facts['حالة المصالح'] = reconciler.status === 'fulfilled' ? reconciler.value.status : 'غير متاح';
    facts['آخر نبض للمصالح (ثانية)'] = reconciler.status === 'fulfilled' ? reconciler.value.last_seen_seconds_ago : 'غير متاح';
    renderFacts($('system-workers'), facts);
    const okay = services?.status === 'healthy' && worker.status === 'fulfilled' && reconciler.status === 'fulfilled';
    status($('system-status'), okay ? 'الخدمات المطلوبة ومكوّنات التشغيل متاحة.' : 'هناك خدمة غير متاحة أو لم تستجب للفحص.', !okay);
  }

  setupUsers(); setupAudit();
  if ($('storage-refresh')) { $('storage-refresh').addEventListener('click', loadStorage); loadStorage(); }
  if ($('system-refresh')) { $('system-refresh').addEventListener('click', loadSystem); loadSystem(); }
})();
