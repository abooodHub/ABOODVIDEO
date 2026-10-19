window.AVi18n ||= {t: value => value, tpl: (parts, ...values) => parts.reduce((out, part, i) => out + part + (i < values.length ? values[i] : ''), '')};
/* ============================================================
   ABOODVIDEO Admin UI V3 — Subtitle Management Logic (Phase H)
   Full subtitle CRUD, format verification, playability status,
   and responsive pagination.
   ============================================================ */

document.addEventListener('DOMContentLoaded', () => {
    let page = 1;
    let total = 0;
    const PAGE_SIZE = 50;
    let pendingDelete = null;
    let cachedSubtitles = [];

    const tbody = document.getElementById('subs-tbody');
    const feedback = document.getElementById('subs-feedback');

    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const fmtDate = (iso) => {
        if (!iso) return '—';
        try {
            return new Date(iso).toLocaleDateString((document.documentElement.lang === 'en' ? 'en-US' : 'ar-EG'), { year: 'numeric', month: 'short', day: 'numeric' });
        } catch (_) {
            return iso.slice(0, 10);
        }
    };
    const el = (tag, text = '', cls = []) => {
        const e = document.createElement(tag);
        if (text !== '') e.textContent = String(text);
        if (cls.length) e.classList.add(...cls);
        return e;
    };

    function showFeedback(text, isError = false) {
        if (!feedback) return;
        feedback.textContent = text;
        feedback.className = `alert ${isError ? 'alert-danger' : 'alert-success'}`;
        feedback.classList.remove('hidden');
        window.clearTimeout(showFeedback._t);
        showFeedback._t = window.setTimeout(() => feedback.classList.add('hidden'), 5000);
    }

    function getCsrf() {
        for (const c of document.cookie.split(';')) {
            const [k, v] = c.trim().split('=');
            if (k === (document.body.dataset.csrfCookieName || 'aboodvideo_csrf')) return v;
        }
        return '';
    }

    /* ------------------------------------------------------------
       1. Load Subtitles Index
       ------------------------------------------------------------ */
    async function load() {
        const q = document.getElementById('subs-search')?.value.trim() || '';
        const lang = document.getElementById('subs-language')?.value || '';
        const params = new URLSearchParams({ page: String(page), limit: String(PAGE_SIZE) });
        if (q) params.set('q', q);
        if (lang) params.set('language', lang);

        try {
            const res = await fetch(`/api/v1/admin/subtitles?${params}`, { credentials: 'include', cache: 'no-store' });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            total = data.total || 0;
            cachedSubtitles = data.items || [];
            applyPlayableFilterAndRender();

            const pageInfo = document.getElementById('subs-page-info');
            if (pageInfo) pageInfo.textContent = window.AVi18n.tpl`صفحة ${page} من ${Math.max(1, Math.ceil(total / PAGE_SIZE))} · ${total} مسار`;
            const prevBtn = document.getElementById('subs-prev');
            const nextBtn = document.getElementById('subs-next');
            if (prevBtn) prevBtn.disabled = page <= 1;
            if (nextBtn) nextBtn.disabled = page * PAGE_SIZE >= total;
        } catch (_) {
            if (tbody) tbody.innerHTML = window.AVi18n.t('<tr><td colspan="8" class="table-empty-cell text-danger">تعذر تحميل مسارات الترجمة — تحقق من الاتصال.</td></tr>');
        }
    }

    function applyPlayableFilterAndRender() {
        const filter = document.getElementById('subs-playable-filter')?.value || '';
        let items = cachedSubtitles;
        if (filter === 'playable') {
            items = items.filter(s => s.is_playable);
        } else if (filter === 'archive') {
            items = items.filter(s => !s.is_playable);
        }
        render(items);
    }

    /* ------------------------------------------------------------
       2. Render Table Rows
       ------------------------------------------------------------ */
    function render(items) {
        if (!tbody) return;
        if (!items.length) {
            tbody.innerHTML = window.AVi18n.t('<tr><td colspan="8" class="table-empty-cell">لا توجد مسارات ترجمة مطابقة للفلاتر الحالية.</td></tr>');
            return;
        }

        tbody.replaceChildren(...items.map(s => {
            const tr = document.createElement('tr');

            // Video Title Link
            const videoTd = el('td');
            const link = el('a', s.video_title, ['subs-title-link']);
            link.href = `/admin/videos/detail?v=${encodeURIComponent(String(s.video_id))}`;
            link.title = s.video_title;
            const strong = el('strong', s.video_title);
            link.replaceChildren(strong);
            videoTd.appendChild(link);

            // Language
            const langTd = el('td');
            const flagBadge = el('span', s.language_code, ['badge', 'badge-muted', 'font-mono', 'flag-badge']);
            const langName = el('span', ` ${s.language_name}`);
            langTd.append(flagBadge, langName);

            // Label
            const labelTd = el('td', s.label);

            // Format
            const formatTd = el('td');
            const fmtStr = String(s.format || '').toUpperCase();
            const formatBadge = el('span', fmtStr, ['badge', fmtStr === 'VTT' || fmtStr === 'WEBVTT' ? 'badge-primary' : 'badge-muted', 'font-mono', 'text-xs']);
            formatTd.appendChild(formatBadge);

            // Playable Status
            const playableTd = el('td');
            if (s.is_playable) {
                const badge = el('span', window.AVi18n.t('قابلة للتشغيل'), ['badge', 'badge-success', 'text-xs']);
                playableTd.appendChild(badge);
            } else {
                const badge = el('span', window.AVi18n.t('أرشيف فقط'), ['badge', 'badge-muted', 'text-xs']);
                playableTd.appendChild(badge);
            }

            // Options (Default / Forced)
            const optsTd = el('td');
            const pills = [];
            if (s.is_default) pills.push(el('span', window.AVi18n.t('افتراضية'), ['badge', 'badge-primary', 'text-xs']));
            if (s.is_forced) pills.push(el('span', window.AVi18n.t('إجبارية'), ['badge', 'badge-warning', 'text-xs']));
            if (!pills.length) pills.push(el('span', '—', ['text-muted', 'text-xs']));
            optsTd.append(...pills);

            // Date
            const dateTd = el('td', fmtDate(s.created_at), ['font-mono', 'text-number', 'text-xs']);

            // Actions
            const actionsTd = el('td');
            actionsTd.style.textAlign = 'center';
            const delBtn = el('button', window.AVi18n.t('حذف'), ['btn', 'btn-sm', 'btn-ghost', 'text-danger']);
            delBtn.type = 'button';
            delBtn.dataset.id = String(s.id);
            delBtn.dataset.videoId = String(s.video_id);
            delBtn.dataset.label = s.label;
            delBtn.title = window.AVi18n.t('حذف هذا المسار');
            delBtn.addEventListener('click', () => askDelete(s));
            actionsTd.appendChild(delBtn);

            tr.append(videoTd, langTd, labelTd, formatTd, playableTd, optsTd, dateTd, actionsTd);
            return tr;
        }));
    }

    /* ------------------------------------------------------------
       3. Delete Subtitle Modal & Flow
       ------------------------------------------------------------ */
    function askDelete(sub) {
        pendingDelete = sub;
        const text = document.getElementById('subs-confirm-text');
        if (text) {
            text.textContent = window.AVi18n.tpl`هل أنت متأكد من حذف مسار الترجمة «${sub.label}» (${sub.language_name}) للفيديو «${sub.video_title}»؟`;
        }
        const modal = document.getElementById('subs-confirm');
        if (modal) modal.classList.add('is-open');
    }

    function closeDeleteModal() {
        pendingDelete = null;
        const modal = document.getElementById('subs-confirm');
        if (modal) modal.classList.remove('is-open');
    }

    async function executeDelete() {
        if (!pendingDelete) return;
        const sub = pendingDelete;
        const yesBtn = document.getElementById('subs-confirm-yes');
        if (yesBtn) yesBtn.disabled = true;

        try {
            const res = await fetch(`/api/v1/admin/videos/${sub.video_id}/subtitles/${sub.id}`, {
                method: 'DELETE',
                credentials: 'include',
                headers: { 'X-CSRF-Token': getCsrf() }
            });
            if (res.ok) {
                showFeedback(window.AVi18n.t('تم حذف مسار الترجمة بنجاح.'));
                closeDeleteModal();
                load();
            } else {
                const err = await res.json().catch(() => ({}));
                showFeedback(window.AVi18n.t('تعذر حذف الترجمة: ') + (window.AVi18n.t(err.detail) || window.AVi18n.t('خطأ غير معروف')), true);
            }
        } catch (_) {
            showFeedback(window.AVi18n.t('تعذر الاتصال بالخادم لحذف الترجمة.'), true);
        } finally {
            if (yesBtn) yesBtn.disabled = false;
        }
    }

    /* ------------------------------------------------------------
       4. Add Subtitle Modal
       ------------------------------------------------------------ */
    const addSubModal = document.getElementById('add-sub-modal');
    const openAddBtn = document.getElementById('btn-open-add-sub');
    const closeAddBtn = document.getElementById('btn-close-add-sub');
    const cancelAddBtn = document.getElementById('btn-cancel-add-sub');
    const addSubForm = document.getElementById('add-sub-form');
    const addSubAlert = document.getElementById('add-sub-alert');

    function openAddModal() {
        if (!addSubModal) return;
        if (addSubAlert) addSubAlert.classList.add('hidden');
        if (addSubForm) addSubForm.reset();
        loadVideosDropdown();
        addSubModal.classList.add('is-open');
    }

    function closeAddModal() {
        if (addSubModal) addSubModal.classList.remove('is-open');
    }

    async function loadVideosDropdown() {
        const select = document.getElementById('add-sub-video');
        if (!select) return;
        select.innerHTML = window.AVi18n.t('<option value="">جاري جلب الفيديوهات...</option>');
        try {
            const res = await fetch('/api/v1/admin/videos?limit=100', { credentials: 'include', cache: 'no-store' });
            if (!res.ok) throw new Error();
            const data = await res.json();
            const items = data.items || [];
            if (!items.length) {
                select.innerHTML = window.AVi18n.t('<option value="">لا توجد فيديوهات مسجلة في المكتبة</option>');
                return;
            }
            select.innerHTML = window.AVi18n.t('<option value="">-- اختر الفيديو المستهدف --</option>') +
                items.map(v => `<option value="${esc(v.id)}">${esc(v.title)} (${esc(v.status)})</option>`).join('');
        } catch (_) {
            select.innerHTML = window.AVi18n.t('<option value="">تعذر جلب الفيديوهات</option>');
        }
    }

    // Auto prefill language name
    const langNames = {
        'ar': window.AVi18n.t('العربية'), 'en': window.AVi18n.t('الإنجليزية'), 'fr': window.AVi18n.t('الفرنسية'), 'es': window.AVi18n.t('الإسبانية'),
        'de': window.AVi18n.t('الألمانية'), 'tr': window.AVi18n.t('التركية'), 'ja': window.AVi18n.t('اليابانية'), 'ko': window.AVi18n.t('الكورية'),
        'it': window.AVi18n.t('الإيطالية'), 'ru': window.AVi18n.t('الروسية'), 'pt': window.AVi18n.t('البرتغالية'), 'zh': window.AVi18n.t('الصينية')
    };
    const codeInput = document.getElementById('add-sub-lang-code');
    const nameInput = document.getElementById('add-sub-lang-name');
    if (codeInput && nameInput) {
        codeInput.addEventListener('input', () => {
            const code = codeInput.value.trim().toLowerCase();
            if (langNames[code] && !nameInput.value) {
                nameInput.value = langNames[code];
            }
        });
    }

    if (addSubForm) {
        addSubForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const videoId = document.getElementById('add-sub-video')?.value;
            const langCode = document.getElementById('add-sub-lang-code')?.value.trim();
            const langName = document.getElementById('add-sub-lang-name')?.value.trim();
            const label = document.getElementById('add-sub-label')?.value.trim();
            const fileInput = document.getElementById('add-sub-file');
            const isDefault = document.getElementById('add-sub-default')?.checked;
            const isForced = document.getElementById('add-sub-forced')?.checked;

            if (!videoId) {
                alert(window.AVi18n.t('الرجاء اختيار الفيديو المستهدف.'));
                return;
            }
            if (!fileInput || !fileInput.files.length) {
                alert(window.AVi18n.t('الرجاء اختيار ملف ترجمة.'));
                return;
            }

            const submitBtn = document.getElementById('btn-submit-sub');
            if (submitBtn) submitBtn.disabled = true;

            const fd = new FormData();
            fd.append('language_code', langCode);
            fd.append('language_name', langName);
            fd.append('label', label);
            fd.append('is_default', String(isDefault));
            fd.append('is_forced', String(isForced));
            fd.append('file', fileInput.files[0]);

            try {
                const res = await fetch(`/api/v1/admin/videos/${videoId}/subtitles`, {
                    method: 'POST',
                    credentials: 'include',
                    headers: { 'X-CSRF-Token': getCsrf() },
                    body: fd
                });
                if (res.ok) {
                    showFeedback(window.AVi18n.t('تم رفع وحفظ مسار الترجمة بنجاح.'));
                    closeAddModal();
                    load();
                } else {
                    const err = await res.json().catch(() => ({}));
                    if (addSubAlert) {
                        addSubAlert.textContent = window.AVi18n.t('فشل الرفع: ') + (window.AVi18n.t(err.detail) || window.AVi18n.t('خطأ غير معروف'));
                        addSubAlert.className = 'alert alert-danger';
                        addSubAlert.classList.remove('hidden');
                    }
                }
            } catch (_) {
                if (addSubAlert) {
                    addSubAlert.textContent = window.AVi18n.t('تعذر الاتصال بالخادم أثناء رفع ملف الترجمة.');
                    addSubAlert.className = 'alert alert-danger';
                    addSubAlert.classList.remove('hidden');
                }
            } finally {
                if (submitBtn) submitBtn.disabled = false;
            }
        });
    }

    /* ------------------------------------------------------------
       5. Event Listeners
       ------------------------------------------------------------ */
    if (openAddBtn) openAddBtn.addEventListener('click', openAddModal);
    if (closeAddBtn) closeAddBtn.addEventListener('click', closeAddModal);
    if (cancelAddBtn) cancelAddBtn.addEventListener('click', closeAddModal);

    const confirmYes = document.getElementById('subs-confirm-yes');
    const confirmNo = document.getElementById('subs-confirm-no');
    const confirmClose = document.getElementById('subs-confirm-close');
    if (confirmYes) confirmYes.addEventListener('click', executeDelete);
    if (confirmNo) confirmNo.addEventListener('click', closeDeleteModal);
    if (confirmClose) confirmClose.addEventListener('click', closeDeleteModal);

    const searchInput = document.getElementById('subs-search');
    if (searchInput) {
        let timer;
        searchInput.addEventListener('input', () => {
            clearTimeout(timer);
            timer = setTimeout(() => { page = 1; load(); }, 250);
        });
    }

    const langSelect = document.getElementById('subs-language');
    if (langSelect) {
        langSelect.addEventListener('change', () => { page = 1; load(); });
    }

    const playableSelect = document.getElementById('subs-playable-filter');
    if (playableSelect) {
        playableSelect.addEventListener('change', () => { applyPlayableFilterAndRender(); });
    }

    const refreshBtn = document.getElementById('subs-refresh');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => { page = 1; load(); });
    }

    const prevBtn = document.getElementById('subs-prev');
    if (prevBtn) {
        prevBtn.addEventListener('click', () => { if (page > 1) { page--; load(); } });
    }

    const nextBtn = document.getElementById('subs-next');
    if (nextBtn) {
        nextBtn.addEventListener('click', () => { if (page * PAGE_SIZE < total) { page++; load(); } });
    }

    // Initial Load
    load();
});
