window.AVi18n ||= {t: value => value, tpl: (parts, ...values) => parts.reduce((out, part, i) => out + part + (i < values.length ? values[i] : ''), '')};
/* ABOODVIDEO Admin Video Library (Phase 4). */
function getCsrfToken() {
    const cookie = document.cookie.split(';').map(value => value.trim())
        .find(value => value.startsWith((document.body.dataset.csrfCookieName || 'aboodvideo_csrf') + '='));
    if (!cookie) return '';
    try { return decodeURIComponent(cookie.slice(((document.body.dataset.csrfCookieName || 'aboodvideo_csrf') + '=').length)); }
    catch (_) { return ''; }
}

async function driveMutation(url, items) {
    const csrfToken = getCsrfToken();
    if (!csrfToken) throw new Error(window.AVi18n.t('انتهت جلسة الإدارة أو رمز الحماية؛ حدّث الصفحة وسجّل الدخول مجدداً.'));
    const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
        credentials: 'include',
        cache: 'no-store',
        body: JSON.stringify({ items }),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
        const detail = typeof window.AVi18n.t(result.detail) === 'string' ? window.AVi18n.t(result.detail) : `HTTP ${response.status}`;
        throw new Error(detail);
    }
    if (result.success !== true || !Number.isInteger(result.count) || result.count < 1) {
        throw new Error(window.AVi18n.t('لم يؤكد الخادم تنفيذ الإجراء. لم يتم تحديث العرض.'));
    }
    return result;
}

let currentVideos = [];
let editingVideo = null;
let currentSubVideoId = null;
let pendingDeleteVideo = null;
let libraryPage = 1;
let libraryTotal = 0;
const PAGE_SIZE = 50;
let libraryView = localStorage.getItem('aboodvideo-library-view') === 'cards' ? 'cards' : 'table';
const selectedVideoIds = new Set();
let openMenuFor = null;

const statusLabels = { READY: window.AVi18n.t('جاهز'), PROCESSING: window.AVi18n.t('قيد المعالجة'), FAILED: window.AVi18n.t('فشل'), CANCELLED: window.AVi18n.t('ملغى'), UPLOADING: window.AVi18n.t('جارٍ الرفع'), UPLOADED: window.AVi18n.t('تم الرفع'), QUEUED: window.AVi18n.t('بالانتظار'), PROBING: window.AVi18n.t('فحص المصدر'), VERIFYING: window.AVi18n.t('تحقق') };
const visibilityLabels = {
    PRIVATE: [window.AVi18n.t('خاص'), 'badge-private'],
    UNLISTED: [window.AVi18n.t('غير مدرج'), 'badge-unlisted'],
    PUBLIC: [window.AVi18n.t('عام'), 'badge-public'],
    PASSWORD_PROTECTED: [window.AVi18n.t('محمي بكلمة سر'), 'badge-password'],
};

function showLibraryMessage(message, isError = false) {
    const box = document.getElementById('library-feedback');
    if (!box) return;
    box.textContent = message;
    box.classList.remove('hidden');
    box.classList.toggle('is-error', isError);
    window.clearTimeout(showLibraryMessage._t);
    showLibraryMessage._t = window.setTimeout(() => box.classList.add('hidden'), 6000);
}

function showSubtitleMessage(message, isError = false) {
    const box = document.getElementById('subs-feedback');
    if (!box) return;
    box.textContent = message;
    box.classList.remove('hidden');
    box.classList.toggle('is-error', isError);
}

function formatBytes(bytes) {
    if (!bytes || bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return (bytes / Math.pow(k, i)).toFixed(i >= 3 ? 1 : 2) + ' ' + sizes[i];
}

function formatDate(iso) {
    if (!iso) return '—';
    try {
        const d = new Date(iso);
        return d.toLocaleDateString((document.documentElement.lang === 'en' ? 'en-US' : 'ar'), { year: 'numeric', month: 'short', day: 'numeric' });
    } catch (_) { return iso.slice(0, 10); }
}

function createElement(tagName, text = '', classNames = []) {
    const element = document.createElement(tagName);
    if (text !== '') element.textContent = String(text);
    element.classList.add(...classNames);
    return element;
}

function createActionButton(label, action, id, classNames = ['btn', 'btn-sm']) {
    const button = createElement('button', label, classNames);
    button.type = 'button';
    button.dataset.action = action;
    button.dataset.id = String(id);
    return button;
}

function showTextContainer(container, message, tagName = 'div') {
    container.replaceChildren(createElement(tagName, message));
}

function topQuality(video) {
    if (!video.available_renditions || !video.available_renditions.length) return null;
    return video.available_renditions[video.available_renditions.length - 1].quality;
}

/* ---------------- URL params init ---------------- */

function initFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const map = { q: 'search-input', visibility: 'visibility-filter', category: 'category-filter', series_name: 'series-filter' };
    for (const [key, id] of Object.entries(map)) {
        const el = document.getElementById(id);
        if (el && params.get(key)) el.value = params.get(key);
    }
    if (params.get('uncategorized') === '1') {
        const el = document.getElementById('category-filter');
        if (el) el.placeholder = window.AVi18n.t('غير مصنف (فعّال)');
        initFromUrl.uncategorized = true;
    }
    if (params.get('panel') === 'folders') document.body.classList.add('folders-open');
    const foldersBtn = document.getElementById('btn-toggle-folders');
    if (foldersBtn) foldersBtn.setAttribute('aria-expanded', document.body.classList.contains('folders-open') ? 'true' : 'false');
}

/* ---------------- Data loading ---------------- */

async function loadVideos() {
    const searchEl = document.getElementById('search-input');
    const q = searchEl ? searchEl.value.trim() : '';
    const params = new URLSearchParams({ page: String(libraryPage), limit: String(PAGE_SIZE) });
    if (q) params.set('q', q);
    if (initFromUrl.uncategorized) params.set('uncategorized', 'true');
    for (const [id, key] of [
        ['status-filter', 'status_filter'],
        ['visibility-filter', 'visibility'],
        ['quality-filter', 'quality'],
        ['subtitle-filter', 'subtitled'],
        ['category-filter', 'category'],
        ['series-filter', 'series_name'],
        ['season-filter', 'season_number'],
    ]) {
        const value = document.getElementById(id)?.value.trim();
        if (value) params.set(key, value);
    }
    const [sort, direction] = document.getElementById('sort-select').value.split(':');
    params.set('sort', sort);
    params.set('direction', direction || 'desc');
    const tbody = document.getElementById('videos-tbody');
    try {
        const res = await fetch(`/api/v1/admin/videos?${params}`, { credentials: 'include', cache: 'no-store' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        currentVideos = data.items;
        libraryTotal = data.total;
        const hubCount = document.getElementById('hubVideosCount');
        if (hubCount) hubCount.textContent = String(libraryTotal);
        renderTable(data.items);
        document.getElementById('page-info').textContent = window.AVi18n.tpl`صفحة ${libraryPage} من ${Math.max(1, Math.ceil(libraryTotal / PAGE_SIZE))} · ${libraryTotal} فيديو`;
        document.getElementById('prev-page').disabled = libraryPage <= 1;
        document.getElementById('next-page').disabled = libraryPage * PAGE_SIZE >= libraryTotal;
    } catch (_) {
        showLibraryMessage(window.AVi18n.t('تعذر تحميل المكتبة. تحقق من الاتصال ثم أعد المحاولة.'), true);
        if (tbody) tbody.innerHTML = window.AVi18n.t('<tr><td colspan="10" class="table-empty-cell">تعذر التحميل — أعد المحاولة</td></tr>');
    }
}

/* ---------------- Rendering ---------------- */

function renderTable(videos) {
    const tbody = document.getElementById('videos-tbody');
    const cards = document.getElementById('videos-cards');
    if (!tbody || !cards) return;
    const cardMode = libraryView === 'cards';
    document.querySelector('.table-card').classList.toggle('hidden', cardMode);
    cards.classList.toggle('hidden', !cardMode);
    document.getElementById('view-table').classList.toggle('is-selected', !cardMode);
    document.getElementById('view-table').setAttribute('aria-pressed', String(!cardMode));
    document.getElementById('view-cards').classList.toggle('is-selected', cardMode);
    document.getElementById('view-cards').setAttribute('aria-pressed', String(cardMode));

    if (videos.length === 0) {
        const row = document.createElement('tr');
        const cell = createElement('td', window.AVi18n.t('لا توجد فيديوهات مطابقة للفلاتر الحالية.'));
        cell.colSpan = 10;
        row.appendChild(cell);
        tbody.replaceChildren(row);
        const empty = document.createElement('div');
        empty.className = 'empty-state';
        empty.innerHTML = window.AVi18n.t('<div class="empty-state-title">لا توجد فيديوهات هنا</div>') +
            window.AVi18n.t('<p class="empty-state-desc">جرّب تعديل الفلاتر. لإضافة فيديو جديد، استخدم قسم الرفع والاستيراد.</p>');
        cards.replaceChildren(empty);
        return;
    }

    const groupKey = document.getElementById('group-select').value;
    const sorted = groupKey ? [...videos].sort((a, b) => String(a[groupKey] || '').localeCompare(String(b[groupKey] || ''), 'ar')) : videos;
    const rows = [];
    const cardNodes = [];
    let previousGroup = null;

    for (const video of sorted) {
        const group = groupKey ? (video[groupKey] || window.AVi18n.t('بدون تصنيف')) : null;
        if (groupKey && group !== previousGroup) {
            const groupRow = document.createElement('tr');
            const groupCell = createElement('td', group, ['group-heading']);
            groupCell.colSpan = 10;
            groupRow.appendChild(groupCell);
            rows.push(groupRow);
            cardNodes.push(createElement('h3', group, ['group-heading']));
            previousGroup = group;
        }

        const [visibilityLabel, visibilityClass] = visibilityLabels[video.visibility] || ['-', 'badge-private'];
        const quality = topQuality(video);

        /* --- table row --- */
        const row = document.createElement('tr');

        const selectCell = document.createElement('td');
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'video-select';
        checkbox.dataset.id = video.id;
        checkbox.checked = selectedVideoIds.has(video.id);
        checkbox.setAttribute('aria-label', window.AVi18n.tpl`تحديد ${video.title}`);
        selectCell.appendChild(checkbox);

        const videoCell = document.createElement('td');
        const cellWrap = document.createElement('div');
        cellWrap.className = 'video-cell';
        const thumb = document.createElement('div');
        thumb.className = 'video-thumb';
        thumb.dataset.poster = video.status === 'READY' ? String(video.id) : '';
        const titleBox = document.createElement('div');
        const titleLink = createElement('a', '', ['video-title-link']);
        titleLink.href = `/admin/videos/detail?v=${encodeURIComponent(String(video.id))}`;
        const titleStrong = createElement('strong', video.title);
        titleLink.appendChild(titleStrong);
        titleBox.append(titleLink, createElement('small', video.source_filename, ['muted-line']));
        cellWrap.append(thumb, titleBox);
        videoCell.appendChild(cellWrap);

        const statusCell = document.createElement('td');
        const statusStack = document.createElement('div');
        const statusLabel = video.status === 'READY' && quality === '2160p' ? window.AVi18n.t('4K جاهز') : (statusLabels[video.status] || video.status);
        statusStack.append(
            createElement('span', statusLabel, ['badge', `status-${video.status.toLowerCase()}`]),
            createElement('span', visibilityLabel, ['badge', visibilityClass]),
        );
        statusCell.appendChild(statusStack);

        const location = [video.category, video.series_name, video.season_number ? window.AVi18n.tpl`م${video.season_number}` : '', video.episode_number ? window.AVi18n.tpl`ح${video.episode_number}` : ''].filter(Boolean).join(' · ') || '—';
        const locationCell = createElement('td', location);

        const qualityCell = createElement('td', quality ? (quality === '2160p' ? '4K' : quality) : '—');

        const subsCell = createElement('td', video.subtitle_count ? window.AVi18n.tpl`${video.subtitle_count} مسار` : window.AVi18n.t('بدون'));
        if (!video.subtitle_count) subsCell.className = 'muted-line';

        const sizeCell = createElement('td', formatBytes(video.source_size_bytes));
        const viewsCell = createElement('td', String(video.view_count));
        const dateCell = createElement('td', formatDate(video.created_at));

        const actionsCell = document.createElement('td');
        actionsCell.className = 'video-actions';
        if (video.status === 'READY') {
            const preview = createElement('a', window.AVi18n.t('معاينة'), ['btn', 'btn-sm']);
            preview.href = `/admin/videos/${encodeURIComponent(String(video.id))}/watch`;
            preview.target = '_blank';
            preview.rel = 'noopener';
            actionsCell.appendChild(preview);

            if (video.share_id) {
                const embedBtn = createElement('button', window.AVi18n.t('تضمين'), ['btn', 'btn-sm']);
                embedBtn.type = 'button';
                embedBtn.dataset.action = 'open-embed';
                embedBtn.dataset.id = String(video.id);
                actionsCell.appendChild(embedBtn);
            }
        }
        const menuBtn = createElement('button', '⋮', ['btn', 'btn-sm']);
        menuBtn.type = 'button';
        menuBtn.dataset.action = 'open-menu';
        menuBtn.dataset.id = String(video.id);
        menuBtn.setAttribute('aria-haspopup', 'menu');
        menuBtn.setAttribute('aria-label', window.AVi18n.tpl`إجراءات ${video.title}`);
        actionsCell.appendChild(menuBtn);

        row.append(selectCell, videoCell, statusCell, locationCell, qualityCell, subsCell, sizeCell, viewsCell, dateCell, actionsCell);
        rows.push(row);

        /* --- card --- */
        const card = document.createElement('article');
        card.className = 'video-card';
        const art = document.createElement('div');
        art.className = 'video-card-art';
        if (video.status === 'READY' && video.active_generation_id) {
            const poster = document.createElement('img');
            poster.src = `/api/v1/admin/videos/${encodeURIComponent(String(video.id))}/poster`;
            poster.alt = window.AVi18n.tpl`صورة الفيديو: ${video.title}`;
            poster.loading = 'lazy';
            art.appendChild(poster);
        } else {
            art.appendChild(createElement('span', '▶', ['video-card-art-icon']));
        }
        const cardHeader = document.createElement('div');
        cardHeader.className = 'video-card-header';
        const cardSelect = checkbox.cloneNode();
        cardSelect.checked = checkbox.checked;
        const cardTitle = createElement('h3', '');
        const cardTitleLink = createElement('a', video.title, ['video-title-link']);
        cardTitleLink.href = `/admin/videos/detail?v=${encodeURIComponent(String(video.id))}`;
        cardTitle.appendChild(cardTitleLink);
        cardHeader.append(cardSelect, cardTitle);

        const cardBadges = document.createElement('div');
        cardBadges.className = 'card-badges';
        cardBadges.append(
            createElement('span', statusLabel, ['badge', `status-${video.status.toLowerCase()}`]),
            createElement('span', visibilityLabel, ['badge', visibilityClass]),
            quality ? createElement('span', quality === '2160p' ? '4K' : quality, ['badge', 'badge-quality']) : createElement('span', window.AVi18n.t('بدون جودات'), ['badge', 'badge-muted']),
        );

        const cardActions = document.createElement('div');
        cardActions.className = 'video-actions';
        const cardDetailsBtn = createActionButton(window.AVi18n.t('تفاصيل'), 'details-video', video.id, ['btn', 'btn-secondary', 'btn-sm']);
        cardActions.append(cardDetailsBtn);
        if (video.status === 'READY' && video.share_id) {
            const cardEmbedBtn = createElement('button', window.AVi18n.t('تضمين'), ['btn', 'btn-sm']);
            cardEmbedBtn.type = 'button';
            cardEmbedBtn.dataset.action = 'open-embed';
            cardEmbedBtn.dataset.id = String(video.id);
            cardActions.append(cardEmbedBtn);
        }
        const cardMenuBtn = menuBtn.cloneNode(true);
        cardActions.append(cardMenuBtn);

        card.append(
            art, cardHeader, cardBadges,
            createElement('p', location, ['muted-line']),
            createElement('p', window.AVi18n.tpl`${formatBytes(video.source_size_bytes)} · ${video.subtitle_count ? video.subtitle_count + window.AVi18n.t(' ترجمة') : window.AVi18n.t('بدون ترجمة')} · ${video.view_count} مشاهدة`, ['muted-line']),
            cardActions,
        );
        cardNodes.push(card);
    }

    tbody.replaceChildren(...rows);
    cards.replaceChildren(...cardNodes);
    attachThumbs(document);
    updateBulkBar();
}

function attachThumbs(root) {
    root.querySelectorAll('.video-thumb[data-poster]').forEach(slot => {
        const id = slot.getAttribute('data-poster');
        if (!id) {
            slot.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m10 9 5 3-5 3z"/></svg>';
            return;
        }
        const img = new Image();
        img.alt = '';
        img.onload = () => { slot.replaceChildren(img); };
        img.onerror = () => {
            slot.removeAttribute('data-poster');
            slot.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m10 9 5 3-5 3z"/></svg>';
        };
        img.src = `/api/v1/admin/videos/${id}/poster`;
    });
}

/* ---------------- Actions menu ---------------- */

function menuItemsFor(video) {
    const items = [];
    if (video.status === 'READY') {
        items.push({ label: window.AVi18n.t('معاينة خاصة'), href: `/admin/videos/${video.id}/watch`, external: true });
        if (video.share_id) {
            items.push({ label: window.AVi18n.t('كود التضمين للمواقع (Embed)'), action: 'open-embed' });
        }
    }
    items.push({ label: window.AVi18n.t('فتح صفحة التفاصيل'), href: `/admin/videos/detail?v=${encodeURIComponent(String(video.id))}` });
    items.push({ label: window.AVi18n.t('نسخ رابط المشاركة'), action: 'copy-video-link' });
    items.push({ label: window.AVi18n.t('إضافة ترجمة'), action: 'open-subtitles' });
    items.push({ label: window.AVi18n.t('المعالجة'), action: 'processing-video' });
    items.push({ label: window.AVi18n.t('تحويل HLS'), action: 'transcode' });
    items.push({ label: window.AVi18n.t('تعديل'), action: 'edit-video' });
    items.push({ label: video.status === 'UPLOADING' ? window.AVi18n.t('إلغاء الرفع وحذف') : window.AVi18n.t('حذف'), action: 'delete-video', danger: true });
    return items;
}

function openActionsMenu(video, anchorBtn) {
    const menu = document.getElementById('actions-menu');
    if (!menu) return;
    menu.replaceChildren();
    for (const item of menuItemsFor(video)) {
        let el;
        if (item.href) {
            el = createElement('a', item.label, ['menu-item']);
            el.href = item.href;
            if (item.external) { el.target = '_blank'; el.rel = 'noopener'; }
        } else {
            el = createElement('button', item.label, ['menu-item']);
            el.type = 'button';
            el.dataset.action = item.action;
            el.dataset.id = String(video.id);
            if (item.danger) el.classList.add('is-danger');
        }
        menu.appendChild(el);
    }
    const rect = anchorBtn.getBoundingClientRect();
    menu.classList.add('is-open');
    menu.setAttribute('aria-hidden', 'false');
    anchorBtn.setAttribute('aria-expanded', 'true');
    const menuHeight = menu.offsetHeight || 300;
    let top = rect.bottom + 4;
    if (top + menuHeight > window.innerHeight - 8) top = Math.max(8, rect.top - menuHeight - 4);
    menu.style.top = top + 'px';
    menu.style.left = Math.min(rect.left, window.innerWidth - 220) + 'px';
    openMenuFor = anchorBtn;
}

function closeActionsMenu() {
    const menu = document.getElementById('actions-menu');
    if (!menu) return;
    menu.classList.remove('is-open');
    menu.setAttribute('aria-hidden', 'true');
    if (openMenuFor) openMenuFor.setAttribute('aria-expanded', 'false');
    openMenuFor = null;
}

/* ---------------- Folders tree ---------------- */

async function loadFoldersTree() {
    const tree = document.getElementById('folders-tree');
    if (!tree) return;
    try {
        const [seriesRes, videosRes] = await Promise.all([
            fetch('/api/v1/admin/videos/series', { credentials: 'include', cache: 'no-store' }),
            fetch('/api/v1/admin/videos?limit=100&sort=title&direction=asc', { credentials: 'include', cache: 'no-store' }),
        ]);
        const series = seriesRes.ok ? await seriesRes.json() : [];
        const videos = videosRes.ok ? (await videosRes.json()).items : [];
        const categories = [...new Set(videos.map(v => v.category).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'ar'));
        const uncategorized = videos.filter(v => !v.category && !v.series_name).length;

        tree.replaceChildren();
        tree.appendChild(createElement('div', window.AVi18n.t('المجلدات'), ['tree-group-title']));
        const allBtn = createElement('button', window.AVi18n.t('كل الفيديوهات'), ['tree-item']);
        allBtn.type = 'button';
        allBtn.addEventListener('click', () => applyTreeFilter({}));
        tree.appendChild(allBtn);
        const uncBtn = createElement('button', window.AVi18n.t('غير مصنف'), ['tree-item']);
        uncBtn.type = 'button';
        uncBtn.appendChild(createElement('span', String(uncategorized), ['tree-count']));
        uncBtn.addEventListener('click', () => {
            initFromUrl.uncategorized = true;
            document.getElementById('category-filter').value = '';
            document.getElementById('series-filter').value = '';
            libraryPage = 1;
            loadVideos();
        });
        tree.appendChild(uncBtn);
        for (const cat of categories) {
            const count = videos.filter(v => v.category === cat).length;
            const btn = createElement('button', cat, ['tree-item']);
            btn.type = 'button';
            btn.appendChild(createElement('span', String(count), ['tree-count']));
            btn.addEventListener('click', () => applyTreeFilter({ category: cat }));
            tree.appendChild(btn);
        }

        tree.appendChild(createElement('div', window.AVi18n.t('المسلسلات'), ['tree-group-title']));
        if (!series.length) {
            tree.appendChild(createElement('div', window.AVi18n.t('لا توجد مسلسلات بعد'), ['empty-hint-sm']));
        }
        for (const s of (Array.isArray(series) ? series : [])) {
            const wrap = document.createElement('div');
            const head = document.createElement('div');
            head.style.display = 'flex';
            head.style.alignItems = 'center';
            const toggle = createElement('button', '▸', ['tree-toggle']);
            toggle.type = 'button';
            toggle.setAttribute('aria-label', window.AVi18n.tpl`توسيع ${s.name}`);
            const btn = createElement('button', s.name, ['tree-item']);
            btn.type = 'button';
            btn.style.flex = '1';
            btn.appendChild(createElement('span', window.AVi18n.tpl`${s.episodes} حلقة`, ['tree-count']));
            btn.addEventListener('click', () => applyTreeFilter({ series_name: s.name }));
            const children = document.createElement('div');
            children.className = 'tree-children';
            children.hidden = true;
            toggle.addEventListener('click', () => {
                children.hidden = !children.hidden;
                toggle.textContent = children.hidden ? '▸' : '▾';
                if (!children.hidden && !children.dataset.loaded) {
                    children.dataset.loaded = '1';
                    loadSeasons(s.name, children);
                }
            });
            head.append(toggle, btn);
            wrap.append(head, children);
            tree.appendChild(wrap);
        }
    } catch (_) {
        tree.replaceChildren(createElement('div', window.AVi18n.t('تعذر تحميل شجرة التنظيم'), ['empty-hint-sm']));
    }
}

async function loadSeasons(name, container) {
    try {
        const res = await fetch(`/api/v1/admin/videos?series_name=${encodeURIComponent(name)}&limit=100&sort=date&direction=asc`, { credentials: 'include', cache: 'no-store' });
        if (!res.ok) throw new Error();
        const items = (await res.json()).items;
        const seasons = [...new Map(items.filter(v => v.season_number).map(v => [v.season_number, v.season_number])).keys()].sort((a, b) => a - b);
        container.replaceChildren();
        if (!seasons.length) {
            container.appendChild(createElement('div', window.AVi18n.t('بدون مواسم محددة'), ['empty-hint-sm']));
            return;
        }
        for (const season of seasons) {
            const count = items.filter(v => v.season_number === season).length;
            const btn = createElement('button', window.AVi18n.tpl`الموسم ${season}`, ['tree-item']);
            btn.type = 'button';
            btn.appendChild(createElement('span', window.AVi18n.tpl`${count} حلقة`, ['tree-count']));
            btn.addEventListener('click', () => applyTreeFilter({ series_name: name, season_number: season }));
            container.appendChild(btn);
        }
    } catch (_) {
        container.replaceChildren(createElement('div', window.AVi18n.t('تعذر التحميل'), ['empty-hint-sm']));
    }
}

function applyTreeFilter({ category = '', series_name = '', season_number = '' }) {
    initFromUrl.uncategorized = false;
    document.getElementById('category-filter').value = category;
    document.getElementById('series-filter').value = series_name;
    document.getElementById('season-filter').value = season_number || '';
    libraryPage = 1;
    loadVideos();
    if (window.innerWidth <= 1100) document.body.classList.remove('folders-open');
}

/* ---------------- Bulk ---------------- */

function updateBulkBar() {
    document.getElementById('bulk-bar').classList.toggle('hidden', selectedVideoIds.size === 0);
    document.getElementById('selected-count').textContent = window.AVi18n.tpl`${selectedVideoIds.size} فيديو محدد`;
    document.querySelectorAll('.video-select').forEach(input => { input.checked = selectedVideoIds.has(input.dataset.id); });
}

function askConfirmation(message) {
    return new Promise(resolve => {
        const modal = document.getElementById('confirm-modal');
        const yes = document.getElementById('confirm-yes');
        const no = document.getElementById('confirm-no');
        document.getElementById('confirm-message').textContent = message;
        modal.classList.add('is-open');
        const finish = answer => {
            modal.classList.remove('is-open');
            yes.removeEventListener('click', onYes);
            no.removeEventListener('click', onNo);
            resolve(answer);
        };
        const onYes = () => finish(true);
        const onNo = () => finish(false);
        yes.addEventListener('click', onYes);
        no.addEventListener('click', onNo);
        no.focus();
    });
}

async function applyBulkAction() {
    const action = document.getElementById('bulk-action').value;
    if (!action || !selectedVideoIds.size) return;
    const payload = { ids: [...selectedVideoIds], action };
    if (action === 'visibility') payload.visibility = document.getElementById('bulk-visibility').value;
    if (action === 'category') payload.category = document.getElementById('bulk-value').value.trim() || null;
    if (action === 'series') {
        payload.series_name = document.getElementById('bulk-value').value.trim() || null;
        payload.season_number = Number(document.getElementById('bulk-season').value) || null;
    }
    if (!await askConfirmation(window.AVi18n.tpl`تطبيق «${document.getElementById('bulk-action').selectedOptions[0].textContent}» على ${payload.ids.length} فيديو؟`)) return;
    const button = document.getElementById('bulk-apply');
    button.disabled = true;
    try {
        const response = await fetch('/api/v1/admin/videos/bulk', {
            method: 'POST', credentials: 'include',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
            body: JSON.stringify(payload),
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof window.AVi18n.t(result.detail) === 'string' ? window.AVi18n.t(result.detail) : `HTTP ${response.status}`);
        const failed = result.results?.filter(item => !item.ok).length || 0;
        showLibraryMessage(window.AVi18n.tpl`اكتمل الإجراء على ${result.updated} فيديو.${failed ? window.AVi18n.tpl` تعذر تنفيذ ${failed} فيديو؛ راجع حالتها.` : ''}`, !!failed);
        selectedVideoIds.clear();
        await loadVideos();
    } catch (error) {
        showLibraryMessage(window.AVi18n.tpl`تعذر تنفيذ الإجراء: ${window.AVi18n.t(error.message)}`, true);
    } finally { button.disabled = false; }
}

/* ---------------- Details modal (quick view) ---------------- */

function openDetailsModal(video) {
    const details = document.getElementById('details-content');
    const fields = [
        [window.AVi18n.t('المصدر'), video.source_filename], [window.AVi18n.t('الحجم'), formatBytes(video.source_size_bytes)],
        [window.AVi18n.t('الأبعاد'), video.source_resolution_width ? `${video.source_resolution_width}×${video.source_resolution_height}` : window.AVi18n.t('غير معروف')],
        [window.AVi18n.t('الكوديك'), video.source_codec || window.AVi18n.t('غير معروف')],
        [window.AVi18n.t('معدل البت'), video.source_bitrate ? `${Math.round(video.source_bitrate / 1000)} kbps` : window.AVi18n.t('غير معروف')],
        [window.AVi18n.t('المدة'), video.source_duration_seconds ? window.AVi18n.tpl`${Math.round(video.source_duration_seconds)} ثانية` : window.AVi18n.t('غير معروف')],
        [window.AVi18n.t('الحالة'), statusLabels[video.status] || video.status], [window.AVi18n.t('الظهور'), visibilityLabels[video.visibility]?.[0] || video.visibility],
        [window.AVi18n.t('المجلد'), video.category || '—'], [window.AVi18n.t('المسلسل'), video.series_name || '—'],
        [window.AVi18n.t('الموسم / الحلقة'), `${video.season_number || '—'} / ${video.episode_number || '—'}`],
        [window.AVi18n.t('المشاهدات'), video.view_count], [window.AVi18n.t('الترجمات'), video.subtitle_count],
        [window.AVi18n.t('رابط المشاركة'), video.share_id && !video.share_revoked ? `${window.location.origin}/watch/${video.share_id}` : window.AVi18n.t('غير متاح')],
    ];
    const grid = document.createElement('dl');
    grid.className = 'details-grid';
    for (const [label, value] of fields) grid.append(createElement('dt', label), createElement('dd', value));
    const renditions = createElement('h4', window.AVi18n.t('الجودات الجاهزة'));
    const list = document.createElement('ul');
    for (const rendition of video.available_renditions) {
        list.appendChild(createElement('li', `${rendition.quality === '2160p' ? '4K · ' : ''}${rendition.quality} · ${rendition.width}×${rendition.height} · ${rendition.codec} · ${rendition.bitrate ? Math.round(rendition.bitrate / 1000) + ' kbps' : window.AVi18n.t('معدل غير معروف')}`));
    }
    if (!video.available_renditions.length) list.appendChild(createElement('li', window.AVi18n.t('لم تُنشر جودات بعد')));
    details.replaceChildren(grid, renditions, list);
    document.getElementById('details-modal').classList.add('is-open');
}

/* ---------------- Edit modal ---------------- */

function openEditModal(vidId) {
    editingVideo = currentVideos.find(v => v.id === vidId);
    if (!editingVideo) return;
    document.getElementById('modal-title').value = editingVideo.title;
    document.getElementById('modal-category').value = editingVideo.category || '';
    document.getElementById('modal-series').value = editingVideo.series_name || '';
    document.getElementById('modal-season').value = editingVideo.season_number || '';
    document.getElementById('modal-episode').value = editingVideo.episode_number || '';
    document.getElementById('modal-visibility').value = editingVideo.visibility;

    toggleVisibilityBoxes(editingVideo.visibility);
    document.getElementById('modal-share-link').value = editingVideo.share_id
        ? `${window.location.origin}/watch/${editingVideo.share_id}` : '';

    document.getElementById('edit-modal').classList.add('is-open');
}

function closeEditModal() {
    document.getElementById('edit-modal').classList.remove('is-open');
    editingVideo = null;
}

function toggleVisibilityBoxes(vis) {
    document.getElementById('modal-password-box').classList.toggle('hidden', vis !== 'PASSWORD_PROTECTED');
    document.getElementById('modal-unlisted-box').classList.toggle('hidden', vis !== 'UNLISTED');
}

/* ---------------- Delete ---------------- */

async function deleteVideo(vidId) {
    const video = currentVideos.find(v => v.id === vidId);
    if (!video) return;
    pendingDeleteVideo = video;
    const isUploading = video.status === 'UPLOADING';
    document.getElementById('delete-modal-title').textContent = isUploading ? window.AVi18n.t('إلغاء الرفع وحذف الفيديو') : window.AVi18n.t('حذف الفيديو');
    document.getElementById('delete-modal-message').textContent = isUploading
        ? window.AVi18n.t('الرفع ما زال نشطاً. سيؤدي هذا الإجراء إلى إلغاء الرفع وتنظيف الملفات المؤقتة ثم حذف الفيديو.')
        : window.AVi18n.t('سيتم حذف الفيديو وملفاته المرتبطة نهائياً.');
    document.getElementById('btn-cancel-delete').textContent = isUploading ? window.AVi18n.t('احتفظ بالرفع') : window.AVi18n.t('إلغاء');
    document.getElementById('btn-confirm-delete').textContent = isUploading ? window.AVi18n.t('إلغاء الرفع وحذف') : window.AVi18n.t('حذف الفيديو');
    document.getElementById('delete-modal').classList.add('is-open');
}

function closeDeleteModal() {
    document.getElementById('delete-modal').classList.remove('is-open');
    pendingDeleteVideo = null;
}

async function confirmDeleteVideo() {
    if (!pendingDeleteVideo) return;
    const video = pendingDeleteVideo;
    const isUploading = video.status === 'UPLOADING';
    const button = document.getElementById('btn-confirm-delete');
    button.disabled = true;
    try {
        const res = await fetch(
            isUploading ? `/api/v1/admin/videos/${video.id}/cancel-and-delete` : `/api/v1/admin/videos/${video.id}`,
            { method: isUploading ? 'POST' : 'DELETE', credentials: 'include', headers: { 'X-CSRF-Token': getCsrfToken() } },
        );
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            document.getElementById('delete-modal-message').textContent = window.AVi18n.t(err.detail) || window.AVi18n.t('تعذر إتمام العملية بأمان.');
            return;
        }
        closeDeleteModal();
        await loadVideos();
    } catch (_) {
        document.getElementById('delete-modal-message').textContent = window.AVi18n.t('تعذر الاتصال بالخادم. لم يتم حذف الفيديو.');
    } finally {
        button.disabled = false;
    }
}

/* ---------------- Transcode ---------------- */

async function triggerTranscode(vidId) {
    if (!await askConfirmation(window.AVi18n.t('هل ترغب في بدء تحويل هذا الفيديو لتقنية Adaptive HLS؟'))) return;
    try {
        const res = await fetch(`/api/v1/admin/videos/${vidId}/transcode`, {
            method: 'POST',
            credentials: 'include',
            headers: { 'X-CSRF-Token': getCsrfToken() }
        });
        if (res.ok) {
            window.location.href = '/admin/processing';
        } else {
            const err = await res.json();
            showLibraryMessage(window.AVi18n.t('تعذر بدء التحويل: ') + (window.AVi18n.t(err.detail) || window.AVi18n.t('خطأ غير معروف')), true);
        }
    } catch (e) {
        showLibraryMessage(window.AVi18n.t('خطأ في الاتصال أثناء بدء التحويل.'), true);
    }
}

/* ---------------- Subtitles modal ---------------- */

async function openSubtitlesModal(vidId, vidTitle) {
    currentSubVideoId = vidId;
    document.getElementById('subs-modal-title').textContent = window.AVi18n.tpl`إدارة ترجمات: ${vidTitle}`;
    document.getElementById('subtitles-modal').classList.add('is-open');
    document.getElementById('subs-upload-form').reset();
    document.getElementById('subs-feedback').classList.add('hidden');
    await loadSubtitles(vidId);
}

function closeSubtitlesModal() {
    document.getElementById('subtitles-modal').classList.remove('is-open');
    currentSubVideoId = null;
}

async function loadSubtitles(vidId) {
    const listEl = document.getElementById('subs-list');
    showTextContainer(listEl, window.AVi18n.t('جاري التحميل...'));
    try {
        const res = await fetch(`/api/v1/admin/videos/${vidId}/subtitles`, { credentials: 'include' });
        if (!res.ok) {
            showTextContainer(listEl, window.AVi18n.t('فشل تحميل الترجمات'));
            return;
        }
        const subs = await res.json();
        if (subs.length === 0) {
            showTextContainer(listEl, window.AVi18n.t('لا توجد ملفات ترجمة مضافة لهذا الفيديو.'));
            return;
        }
        const rows = subs.map((subtitle) => {
            const row = document.createElement('div');
            const details = document.createElement('div');
            const badges = document.createElement('div');
            badges.style.display = 'flex';
            badges.style.gap = '6px';
            badges.style.marginTop = '4px';
            badges.appendChild(createElement('span', subtitle.is_playable ? window.AVi18n.t('قابلة للتشغيل') : window.AVi18n.t('غير قابلة للتشغيل'), ['badge']));
            if (subtitle.is_default) badges.appendChild(createElement('span', window.AVi18n.t('افتراضية'), ['badge']));
            if (subtitle.is_forced) badges.appendChild(createElement('span', window.AVi18n.t('إجبارية'), ['badge']));
            details.append(
                createElement('strong', subtitle.label),
                createElement('span', ` (${subtitle.language_name} - ${subtitle.language_code}) [${String(subtitle.format).toUpperCase()}]`),
                badges,
            );

            const actions = document.createElement('div');
            actions.appendChild(createActionButton(window.AVi18n.t('إعادة تسمية'), 'rename-subtitle', subtitle.id));
            if (subtitle.is_playable) actions.appendChild(createActionButton(
                subtitle.is_default ? window.AVi18n.t('إلغاء الافتراضية') : window.AVi18n.t('تعيين كافتراضية'),
                'set-default-subtitle', subtitle.id,
            ));
            actions.appendChild(createActionButton(window.AVi18n.t('حذف'), 'delete-subtitle', subtitle.id, ['btn', 'btn-sm', 'btn-danger']));
            row.append(details, actions);
            return row;
        });
        listEl.replaceChildren(...rows);
    } catch (e) {
        showTextContainer(listEl, window.AVi18n.t('خطأ في الاتصال'));
    }
}

async function updateSubtitle(subId, changes, successMessage) {
    if (!currentSubVideoId) return;
    try {
        const res = await fetch(`/api/v1/admin/videos/${currentSubVideoId}/subtitles/${subId}`, {
            method: 'PATCH',
            credentials: 'include',
            headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
            body: JSON.stringify(changes)
        });
        if (res.ok) {
            await loadSubtitles(currentSubVideoId);
            showSubtitleMessage(successMessage);
        } else {
            showSubtitleMessage(window.AVi18n.t('فشل تعديل الترجمة.'), true);
        }
    } catch (e) {
        showSubtitleMessage(window.AVi18n.t('تعذر الاتصال أثناء تعديل الترجمة.'), true);
    }
}

async function setDefaultSubtitle(subId) {
    const row = document.querySelector(`#subs-list [data-action="set-default-subtitle"][data-id="${subId}"]`);
    const makeDefault = row?.textContent !== 'إلغاء الافتراضية';
    await updateSubtitle(subId, { is_default: makeDefault }, window.AVi18n.t('تم تحديث الترجمة الافتراضية.'));
}

async function renameSubtitle(subId) {
    const row = document.querySelector(`#subs-list [data-action="rename-subtitle"][data-id="${subId}"]`)?.closest('div');
    const currentLabel = row?.parentElement?.querySelector('strong')?.textContent || '';
    const label = window.prompt(window.AVi18n.t('اسم مسار الترجمة الجديد'), currentLabel);
    if (label === null) return;
    const trimmed = label.trim();
    if (!trimmed || trimmed.length > 100) {
        showSubtitleMessage(window.AVi18n.t('اسم الترجمة يجب أن يكون بين 1 و100 حرف.'), true);
        return;
    }
    if (trimmed !== currentLabel) await updateSubtitle(subId, { label: trimmed }, window.AVi18n.t('تمت إعادة تسمية الترجمة.'));
}

async function deleteSubtitle(subId) {
    if (!currentSubVideoId) return;
    if (!await askConfirmation(window.AVi18n.t('هل أنت متأكد من حذف ملف الترجمة هذا؟'))) return;
    try {
        const res = await fetch(`/api/v1/admin/videos/${currentSubVideoId}/subtitles/${subId}`, {
            method: 'DELETE',
            credentials: 'include',
            headers: { 'X-CSRF-Token': getCsrfToken() }
        });
        if (res.ok) {
            await loadSubtitles(currentSubVideoId);
        } else {
            showSubtitleMessage(window.AVi18n.t('فشل حذف ملف الترجمة.'), true);
        }
    } catch (e) {
        showSubtitleMessage(window.AVi18n.t('تعذر الاتصال أثناء حذف الترجمة.'), true);
    }
}

window.openSubtitlesModal = openSubtitlesModal;
window.closeSubtitlesModal = closeSubtitlesModal;
window.setDefaultSubtitle = setDefaultSubtitle;
window.renameSubtitle = renameSubtitle;
window.deleteSubtitle = deleteSubtitle;
window.openEditModal = openEditModal;
window.closeEditModal = closeEditModal;
window.deleteVideo = deleteVideo;
window.triggerTranscode = triggerTranscode;

/* ---------------- Init ---------------- */

document.addEventListener('DOMContentLoaded', () => {
    initFromUrl();
    loadFoldersTree();

    document.addEventListener('click', async (event) => {
        const menuEl = event.target.closest('#actions-menu');
        if (!menuEl && !event.target.closest('[data-action="open-menu"]')) closeActionsMenu();

        const button = event.target.closest('[data-action]');
        if (!button) return;
        const id = button.dataset.id;
        const video = currentVideos.find(item => String(item.id) === id);
        const action = button.dataset.action;

        if (action === 'open-menu' && video) {
            openActionsMenu(video, button);
            return;
        }
        closeActionsMenu();
        if (action === 'open-subtitles') openSubtitlesModal(id, video ? video.title : '');
        if (action === 'transcode') triggerTranscode(id);
        if (action === 'edit-video') openEditModal(id);
        if (action === 'delete-video') deleteVideo(id);
        if (action === 'details-video' && video) openDetailsModal(video);
        if (action === 'open-embed' && video) openEmbedModal(video);
        if (action === 'processing-video') window.location.href = '/admin/processing';
        if (action === 'copy-video-link' && video) {
            const link = video.share_id && !video.share_revoked ? `${window.location.origin}/watch/${video.share_id}` : `${window.location.origin}/admin/videos/${video.id}/watch`;
            try { await navigator.clipboard.writeText(link); showLibraryMessage(window.AVi18n.t('تم نسخ رابط المشاهدة.')); }
            catch (_) { showLibraryMessage(window.AVi18n.t('تعذر نسخ الرابط من المتصفح.'), true); }
        }
        if (action === 'set-default-subtitle') setDefaultSubtitle(id);
        if (action === 'rename-subtitle') renameSubtitle(id);
        if (action === 'delete-subtitle') deleteSubtitle(id);
    });

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') closeActionsMenu();
    });

    document.addEventListener('change', event => {
        if (!event.target.matches('.video-select')) return;
        if (event.target.checked) selectedVideoIds.add(event.target.dataset.id);
        else selectedVideoIds.delete(event.target.dataset.id);
        updateBulkBar();
    });

    document.getElementById('select-all-videos').addEventListener('change', event => {
        currentVideos.forEach(video => event.target.checked ? selectedVideoIds.add(video.id) : selectedVideoIds.delete(video.id));
        updateBulkBar();
    });

    document.getElementById('bulk-apply').addEventListener('click', applyBulkAction);
    document.getElementById('bulk-action').addEventListener('change', event => {
        const action = event.target.value;
        document.getElementById('bulk-value').classList.toggle('hidden', !['category', 'series'].includes(action));
        document.getElementById('bulk-season').classList.toggle('hidden', action !== 'series');
        document.getElementById('bulk-visibility').classList.toggle('hidden', action !== 'visibility');
    });

    document.getElementById('close-details').addEventListener('click', () => document.getElementById('details-modal').classList.remove('is-open'));

    for (const [id, view] of [['view-table', 'table'], ['view-cards', 'cards']]) {
        document.getElementById(id).addEventListener('click', () => {
            libraryView = view;
            localStorage.setItem('aboodvideo-library-view', view);
            renderTable(currentVideos);
        });
    }

    document.getElementById('prev-page').addEventListener('click', () => { if (libraryPage > 1) { libraryPage--; loadVideos(); } });
    document.getElementById('next-page').addEventListener('click', () => { if (libraryPage * PAGE_SIZE < libraryTotal) { libraryPage++; loadVideos(); } });

    const cancelEditButton = document.getElementById('btn-cancel-edit');
    const closeSubtitlesButton = document.getElementById('btn-close-subtitles');
    if (cancelEditButton) cancelEditButton.addEventListener('click', closeEditModal);
    document.getElementById('btn-cancel-delete').addEventListener('click', closeDeleteModal);
    document.getElementById('btn-confirm-delete').addEventListener('click', confirmDeleteVideo);
    if (closeSubtitlesButton) closeSubtitlesButton.addEventListener('click', closeSubtitlesModal);

    const foldersBtn = document.getElementById('btn-toggle-folders');
    if (foldersBtn) {
        foldersBtn.addEventListener('click', () => {
            const open = document.body.classList.toggle('folders-open');
            foldersBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
        });
    }
    const closeFolders = document.getElementById('btn-close-folders');
    if (closeFolders) {
        closeFolders.addEventListener('click', () => {
            document.body.classList.remove('folders-open');
            const btn = document.getElementById('btn-toggle-folders');
            if (btn) btn.setAttribute('aria-expanded', 'false');
        });
    }
    const treeSearch = document.getElementById('tree-search');
    if (treeSearch) {
        treeSearch.addEventListener('input', () => {
            const q = treeSearch.value.trim();
            document.querySelectorAll('#folders-tree .tree-item').forEach(item => {
                item.style.display = !q || item.textContent.includes(q) ? '' : 'none';
            });
        });
    }

    const searchInput = document.getElementById('search-input');
    if (searchInput) {
        let searchTimer;
        searchInput.addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { libraryPage = 1; loadVideos(); }, 250); });
    }
    // Quick Status Pills & Advanced Drawer Logic
    const quickPills = document.querySelectorAll('.quick-pill');
    const statusSelect = document.getElementById('status-filter');
    const advancedDrawer = document.getElementById('advanced-filters-drawer');
    const advancedToggle = document.getElementById('btn-toggle-advanced');
    const activeBadge = document.getElementById('activeFiltersBadge');
    const clearFiltersBtn = document.getElementById('btn-clear-filters');

    function updateActiveFilterCount() {
        if (!activeBadge) return;
        let count = 0;
        for (const id of ['status-filter', 'visibility-filter', 'quality-filter', 'subtitle-filter', 'category-filter', 'series-filter', 'season-filter']) {
            const val = document.getElementById(id)?.value?.trim();
            if (val) count++;
        }
        activeBadge.textContent = String(count);
        activeBadge.classList.toggle('hidden', count === 0);
    }

    quickPills.forEach(pill => {
        pill.addEventListener('click', () => {
            quickPills.forEach(p => p.classList.remove('is-active'));
            pill.classList.add('is-active');
            if (statusSelect) {
                statusSelect.value = pill.dataset.status || '';
                statusSelect.dispatchEvent(new Event('change'));
            }
        });
    });

    if (advancedToggle && advancedDrawer) {
        advancedToggle.addEventListener('click', () => {
            const isHidden = advancedDrawer.classList.toggle('hidden');
            advancedToggle.setAttribute('aria-expanded', String(!isHidden));
            advancedToggle.classList.toggle('btn-primary', !isHidden);
        });
    }

    if (clearFiltersBtn) {
        clearFiltersBtn.addEventListener('click', () => {
            for (const id of ['status-filter', 'visibility-filter', 'quality-filter', 'subtitle-filter', 'category-filter', 'series-filter', 'season-filter']) {
                const el = document.getElementById(id);
                if (el) el.value = '';
            }
            quickPills.forEach(p => p.classList.toggle('is-active', p.dataset.status === ''));
            updateActiveFilterCount();
            libraryPage = 1;
            loadVideos();
        });
    }

    for (const id of ['status-filter', 'visibility-filter', 'quality-filter', 'subtitle-filter', 'category-filter', 'series-filter', 'season-filter', 'sort-select', 'group-select']) {
        const field = document.getElementById(id);
        if (!field) continue;
        field.addEventListener(['category-filter', 'series-filter', 'season-filter'].includes(id) ? 'input' : 'change', () => {
            if (id === 'category-filter' || id === 'series-filter') initFromUrl.uncategorized = false;
            if (id === 'status-filter') {
                const val = field.value;
                quickPills.forEach(p => p.classList.toggle('is-active', p.dataset.status === val));
            }
            updateActiveFilterCount();
            libraryPage = 1;
            loadVideos();
        });
    }

    const modalVis = document.getElementById('modal-visibility');
    if (modalVis) {
        modalVis.addEventListener('change', (e) => toggleVisibilityBoxes(e.target.value));
    }

    const copyShareBtn = document.getElementById('btn-copy-share');
    if (copyShareBtn) {
        copyShareBtn.addEventListener('click', () => {
            const link = document.getElementById('modal-share-link').value;
            if (link) {
                navigator.clipboard.writeText(link).then(() => showLibraryMessage(window.AVi18n.t('تم نسخ رابط المشاهدة.')), () => showLibraryMessage(window.AVi18n.t('تعذر نسخ الرابط.'), true));
            }
        });
    }

    const rotateShareBtn = document.getElementById('btn-rotate-share');
    if (rotateShareBtn) {
        rotateShareBtn.addEventListener('click', async () => {
            if (!editingVideo) return;
            const res = await fetch(`/api/v1/admin/videos/${editingVideo.id}`, {
                method: 'PATCH',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
                body: JSON.stringify({ rotate_share_link: true })
            });
            if (res.ok) {
                const updated = await res.json();
                document.getElementById('modal-share-link').value = `${window.location.origin}/watch/${updated.share_id}`;
                showLibraryMessage(window.AVi18n.t('تم تجديد الرابط وإبطال الرابط القديم.'));
                loadVideos();
            }
        });
    }

    const saveEditBtn = document.getElementById('btn-save-edit');
    if (saveEditBtn) {
        saveEditBtn.addEventListener('click', async () => {
            if (!editingVideo) return;
            const newTitle = document.getElementById('modal-title').value.trim();
            const newVis = document.getElementById('modal-visibility').value;
            const newPass = document.getElementById('modal-password').value.trim();

            const payload = {
                title: newTitle,
                visibility: newVis,
                category: document.getElementById('modal-category').value.trim() || null,
                series_name: document.getElementById('modal-series').value.trim() || null,
                season_number: Number(document.getElementById('modal-season').value) || null,
                episode_number: Number(document.getElementById('modal-episode').value) || null,
            };
            if (newPass) payload.viewer_password = newPass;

            const res = await fetch(`/api/v1/admin/videos/${editingVideo.id}`, {
                method: 'PATCH',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
                body: JSON.stringify(payload)
            });

            if (res.ok) {
                closeEditModal();
                loadVideos();
            } else {
                showLibraryMessage(window.AVi18n.t('فشل حفظ التعديلات. راجع الحقول وحاول مجددًا.'), true);
            }
        });
    }

    const subsForm = document.getElementById('subs-upload-form');
    if (subsForm) {
        subsForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            if (!currentSubVideoId) return;

            const fileInput = document.getElementById('sub-file');
            if (!fileInput.files.length) {
                showSubtitleMessage(window.AVi18n.t('الرجاء اختيار ملف ترجمة.'), true);
                return;
            }

            const formData = new FormData();
            formData.append('file', fileInput.files[0]);
            formData.append('language_code', document.getElementById('sub-lang-code').value.trim());
            formData.append('language_name', document.getElementById('sub-lang-name').value.trim());
            formData.append('label', document.getElementById('sub-label').value.trim());
            formData.append('is_default', document.getElementById('sub-is-default').checked);
            formData.append('is_forced', document.getElementById('sub-is-forced').checked);

            const submitBtn = document.getElementById('btn-upload-sub');
            submitBtn.disabled = true;
            submitBtn.textContent = window.AVi18n.t('جاري الرفع والمعالجة...');

            try {
                const res = await fetch(`/api/v1/admin/videos/${currentSubVideoId}/subtitles`, {
                    method: 'POST',
                    credentials: 'include',
                    headers: { 'X-CSRF-Token': getCsrfToken() },
                    body: formData
                });

                if (res.ok) {
                    document.getElementById('subs-upload-form').reset();
                    await loadSubtitles(currentSubVideoId);
                } else {
                    const err = await res.json();
                    showSubtitleMessage(window.AVi18n.t('فشل رفع الترجمة: ') + (window.AVi18n.t(err.detail) || window.AVi18n.t('خطأ غير معروف')), true);
                }
            } catch (e) {
                showSubtitleMessage(window.AVi18n.t('تعذر الاتصال أثناء رفع الترجمة.'), true);
            } finally {
                submitBtn.disabled = false;
                submitBtn.textContent = window.AVi18n.t('رفع وحفظ الترجمة');
            }
        });
    }

    // --- Quick Embed Modal Logic ---
    let activeEmbedVideo = null;

    function updateModalEmbedCode() {
        if (!activeEmbedVideo) return;
        const shareId = activeEmbedVideo.share_id;
        if (!shareId) return;

        const rawColor = (document.getElementById('modal-embed-color')?.value || '00d2be').replace('#', '').trim();
        const watermark = (document.getElementById('modal-embed-watermark')?.value || '').trim();
        const skip = parseInt(document.getElementById('modal-embed-skip')?.value, 10);
        const autoplay = document.getElementById('modal-embed-autoplay')?.checked;
        const banner = document.getElementById('modal-embed-banner')?.checked;

        const baseOrigin = window.location.origin;
        const query = new URLSearchParams();
        if (rawColor && rawColor !== '00d2be') query.set('color', rawColor);
        if (watermark && watermark !== 'ABOODVIDEO') query.set('watermark', watermark);
        if (Number.isFinite(skip) && skip !== 85) query.set('skip', String(skip));
        if (autoplay) query.set('autoplay', '1');
        if (banner) query.set('banner', '1');

        const qs = query.toString() ? `?${query.toString()}` : '';
        const embedUrl = `${baseOrigin}/embed/${shareId}${qs}`;

        const iframeCode = `<iframe src="${embedUrl}" width="100%" height="100%" frameborder="0" allowfullscreen allow="autoplay; encrypted-media; picture-in-picture"></iframe>`;
        const textarea = document.getElementById('modal-embed-code');
        if (textarea) textarea.value = iframeCode;

        const previewLink = document.getElementById('btn-preview-modal-embed');
        if (previewLink) previewLink.href = embedUrl;
    }

    function openEmbedModal(video) {
        activeEmbedVideo = video;
        const modal = document.getElementById('embed-modal');
        if (!modal) return;

        const titleEl = document.getElementById('embed-modal-title');
        const subtitleEl = document.getElementById('embed-modal-subtitle');
        if (titleEl) titleEl.textContent = window.AVi18n.tpl`كود تضمين: ${video.title}`;
        if (subtitleEl) {
            const parts = [
                video.series_name,
                video.season_number ? window.AVi18n.tpl`الموسم ${video.season_number}` : '',
                video.episode_number ? window.AVi18n.tpl`الحلقة ${video.episode_number}` : ''
            ].filter(Boolean);
            subtitleEl.textContent = parts.join(' • ');
        }

        updateModalEmbedCode();
        modal.classList.add('is-open');
    }

    function closeEmbedModal() {
        const modal = document.getElementById('embed-modal');
        if (modal) modal.classList.remove('is-open');
        activeEmbedVideo = null;
    }

    document.getElementById('btn-close-modal-embed')?.addEventListener('click', closeEmbedModal);

    document.querySelectorAll('#embed-modal .btn-color-dot').forEach(btn => {
        btn.addEventListener('click', () => {
            const col = btn.getAttribute('data-col');
            const inp = document.getElementById('modal-embed-color');
            if (inp && col) {
                inp.value = col;
                updateModalEmbedCode();
            }
        });
    });

    ['modal-embed-color', 'modal-embed-watermark', 'modal-embed-skip', 'modal-embed-autoplay', 'modal-embed-banner'].forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            el.addEventListener('input', updateModalEmbedCode);
            el.addEventListener('change', updateModalEmbedCode);
        }
    });

    document.getElementById('btn-copy-modal-embed')?.addEventListener('click', async () => {
        const code = document.getElementById('modal-embed-code')?.value;
        const feedback = document.getElementById('modal-embed-feedback');
        if (!code) return;
        try {
            await navigator.clipboard.writeText(code);
            if (feedback) {
                feedback.textContent = window.AVi18n.t('تم نسخ الكود بنجاح ✓');
                setTimeout(() => { feedback.textContent = ''; }, 3000);
            }
        } catch (_) {
            if (feedback) {
                feedback.textContent = window.AVi18n.t('تعذر النسخ تلقائياً');
                setTimeout(() => { feedback.textContent = ''; }, 3000);
            }
        }
    });

    // --- Content Hub Controller ---
    let allSeriesData = [];

    function initContentHub() {
        const tabBtns = document.querySelectorAll('.hub-tab-btn');
        const panes = {
            videos: document.getElementById('pane-videos'),
            series: document.getElementById('pane-series'),
            drive: document.getElementById('pane-drive'),
            trash: document.getElementById('pane-trash'),
        };

        function switchHubTab(tabName, updateHash = true) {
            if (!panes[tabName]) tabName = 'videos';

            tabBtns.forEach(btn => {
                const isMatch = btn.getAttribute('data-hub-tab') === tabName;
                btn.classList.toggle('is-active', isMatch);
                btn.setAttribute('aria-selected', isMatch ? 'true' : 'false');
            });

            Object.keys(panes).forEach(k => {
                if (panes[k]) panes[k].classList.toggle('hidden', k !== tabName);
            });

            if (updateHash) {
                history.replaceState(null, '', `#${tabName}`);
            }

            if (tabName === 'series') {
                loadSeriesHubData();
            } else if (tabName === 'drive') {
                loadFoldersHubData();
            } else if (tabName === 'trash') {
                loadTrashHubData();
            }
        }

        tabBtns.forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                const tabName = btn.getAttribute('data-hub-tab');
                switchHubTab(tabName);
            });
        });

        // Initialize from URL hash
        const initialHash = window.location.hash.replace('#', '');
        if (['videos', 'series', 'drive', 'trash'].includes(initialHash)) {
            switchHubTab(initialHash, false);
        }

        window.addEventListener('hashchange', () => {
            const hash = window.location.hash.replace('#', '');
            if (['videos', 'series', 'drive', 'trash'].includes(hash)) {
                switchHubTab(hash, false);
            }
        });

        // Prefetch counts in background
        fetch('/api/v1/admin/videos/series', { credentials: 'include' })
            .then(r => r.json())
            .then(data => {
                if (Array.isArray(data)) {
                    allSeriesData = data;
                    const b = document.getElementById('hubSeriesCount');
                    if (b) b.textContent = String(data.length);
                }
            }).catch(() => {});

        fetch('/api/v1/drive/scope?content_filter=trash', { credentials: 'include', cache: 'no-store' })
            .then(r => r.json())
            .then(data => {
                const trashItems = Array.isArray(data?.trash_items) ? data.trash_items : data?.items;
                const count = Array.isArray(trashItems) ? trashItems.length : 0;
                const b = document.getElementById('hubTrashCount');
                if (b && count > 0) {
                    b.textContent = String(count);
                    b.classList.remove('hidden');
                }
            }).catch(() => {});

        // Series search
        document.getElementById('series-search-input')?.addEventListener('input', (e) => {
            const q = e.target.value.trim().toLowerCase();
            if (!q) {
                renderSeriesCards(allSeriesData);
                return;
            }
            const filtered = allSeriesData.filter(s => s.name.toLowerCase().includes(q));
            renderSeriesCards(filtered);
        });
    }

    async function loadSeriesHubData() {
        const grid = document.getElementById('series-grid');
        const loading = document.getElementById('series-loading');
        const feedback = document.getElementById('series-feedback');
        const countBadge = document.getElementById('hubSeriesCount');
        const countInd = document.getElementById('seriesCountIndicator');
        if (!grid || !loading) return;

        loading.classList.remove('hidden');
        grid.classList.add('hidden');

        try {
            const res = await fetch('/api/v1/admin/videos/series', { credentials: 'include', cache: 'no-store' });
            if (!res.ok) throw new Error();
            allSeriesData = await res.json();
            loading.classList.add('hidden');

            const totalSeries = Array.isArray(allSeriesData) ? allSeriesData.length : 0;
            if (countBadge) countBadge.textContent = String(totalSeries);
            if (countInd) countInd.textContent = window.AVi18n.tpl`${totalSeries} مسلسل`;

            renderSeriesCards(allSeriesData);
        } catch (_) {
            loading.classList.add('hidden');
            if (feedback) {
                feedback.textContent = window.AVi18n.t('تعذر تحميل المسلسلات — أعد المحاولة.');
                feedback.classList.remove('hidden');
            }
        }
    }

    function renderSeriesCards(seriesList) {
        const grid = document.getElementById('series-grid');
        if (!grid) return;

        if (!Array.isArray(seriesList) || !seriesList.length) {
            grid.innerHTML = window.AVi18n.tpl`
                <div class="empty-state" style="grid-column: 1 / -1; padding: 48px 24px; text-align: center;">
                    <div class="empty-state-title" style="font-size: 1.15rem; font-weight: 700; margin-bottom: 8px;">لا توجد مسلسلات مسجلة بعد</div>
                    <p class="empty-state-desc" style="color: var(--text-muted); margin-bottom: 16px;">ستظهر المسلسلات هنا بعد ربط الفيديوهات بها. لإضافة محتوى جديد، استخدم قسم الرفع والاستيراد.</p>
                </div>
            `;
            grid.classList.remove('hidden');
            return;
        }

        grid.replaceChildren(...seriesList.map(s => {
            const card = document.createElement('a');
            card.className = 'series-card';
            card.href = '/admin/series/detail?s=' + encodeURIComponent(s.name);

            const head = document.createElement('div');
            head.className = 'series-card-header';

            const poster = document.createElement('div');
            poster.className = 'series-poster';
            poster.textContent = (s.name || '').trim().charAt(0) || '📺';

            const title = document.createElement('h2');
            title.textContent = s.name;

            head.append(poster, title);

            const stats = document.createElement('div');
            stats.className = 'series-stats';

            const stSeasons = document.createElement('div');
            stSeasons.className = 'series-stat';
            stSeasons.innerHTML = window.AVi18n.tpl`<b>${s.seasons}</b><span>موسم</span>`;

            const stEpisodes = document.createElement('div');
            stEpisodes.className = 'series-stat';
            stEpisodes.innerHTML = window.AVi18n.tpl`<b>${s.episodes}</b><span>حلقة</span>`;

            const stSize = document.createElement('div');
            stSize.className = 'series-stat';
            stSize.innerHTML = window.AVi18n.tpl`<b>${formatBytes(s.total_size_bytes)}</b><span>الحجم</span>`;

            stats.append(stSeasons, stEpisodes, stSize);

            const foot = document.createElement('div');
            foot.className = 'series-foot';

            const badgeReady = document.createElement('span');
            badgeReady.className = 'badge badge-quality';
            badgeReady.textContent = window.AVi18n.tpl`${s.ready_count} جاهز`;
            foot.appendChild(badgeReady);

            if (s.processing_count) {
                const badgeProc = document.createElement('span');
                badgeProc.className = 'badge status-processing';
                badgeProc.textContent = window.AVi18n.tpl`${s.processing_count} قيد المعالجة`;
                foot.appendChild(badgeProc);
            }

            card.append(head, stats, foot);
            return card;
        }));
        grid.classList.remove('hidden');
    }

    // --- Folders & Drive Hub Data ---
    let allFoldersData = [];

    async function loadFoldersHubData() {
        const grid = document.getElementById('folders-grid');
        const loading = document.getElementById('folders-loading');
        const feedback = document.getElementById('folders-feedback');
        const countInd = document.getElementById('foldersCountIndicator');
        if (!grid || !loading) return;

        loading.classList.remove('hidden');
        grid.classList.add('hidden');

        try {
            const res = await fetch('/api/v1/drive/scope?content_filter=content', { credentials: 'include', cache: 'no-store' });
            if (!res.ok) throw new Error();
            const data = await res.json();
            allFoldersData = Array.isArray(data.folders) ? data.folders : [];
            loading.classList.add('hidden');

            if (countInd) countInd.textContent = window.AVi18n.tpl`${allFoldersData.length} مجلد`;
            renderFoldersCards(allFoldersData);
        } catch (_) {
            loading.classList.add('hidden');
            if (feedback) {
                feedback.textContent = window.AVi18n.t('تعذر تحميل المجلدات — أعد المحاولة.');
                feedback.classList.remove('hidden');
            }
        }
    }

    function renderFoldersCards(foldersList) {
        const grid = document.getElementById('folders-grid');
        if (!grid) return;

        if (!Array.isArray(foldersList) || !foldersList.length) {
            grid.innerHTML = window.AVi18n.tpl`
                <div class="empty-state" style="grid-column: 1 / -1; padding: 48px 24px; text-align: center;">
                    <div class="empty-state-title" style="font-size: 1.15rem; font-weight: 700; margin-bottom: 8px;">لا توجد مجلدات بعد</div>
                    <p class="empty-state-desc" style="color: var(--text-muted); margin-bottom: 16px;">يمكنك إنشاء مجلدات جديدة لتنظيم وتجميع حلقات ومسلسلات الأنمي بكل سهولة.</p>
                    <button type="button" class="btn btn-primary" id="btn-create-first-folder">+ إنشاء أول مجلد</button>
                </div>
            `;
            grid.classList.remove('hidden');
            document.getElementById('btn-create-first-folder')?.addEventListener('click', () => {
                document.getElementById('btn-create-folder')?.click();
            });
            return;
        }

        grid.replaceChildren(...foldersList.map(f => {
            const card = document.createElement('div');
            card.className = 'folder-card';
            card.setAttribute('role', 'button');
            card.tabIndex = 0;
            card.title = window.AVi18n.tpl`انقر لعرض محتويات مجلد ${f.name}`;

            const icon = document.createElement('div');
            icon.className = 'folder-card-icon';
            icon.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';

            const info = document.createElement('div');
            info.className = 'folder-card-info';

            const name = document.createElement('div');
            name.className = 'folder-card-name';
            name.textContent = f.name;

            const count = document.createElement('div');
            count.className = 'folder-card-count';
            count.textContent = window.AVi18n.tpl`${f.items_count || 0} عنصر`;

            info.append(name, count);
            card.append(icon, info);

            card.addEventListener('click', () => {
                const catInput = document.getElementById('search-input');
                if (catInput) catInput.value = f.name;
                const tabVideosBtn = document.querySelector('.hub-tab-btn[data-hub-tab="videos"]');
                if (tabVideosBtn) tabVideosBtn.click();
                loadVideos();
            });

            return card;
        }));
        grid.classList.remove('hidden');
    }

    document.getElementById('btn-create-folder')?.addEventListener('click', async () => {
        const name = window.prompt(window.AVi18n.t('أدخل اسم المجلد الجديد:'));
        if (!name || !name.trim()) return;

        try {
            const res = await fetch('/api/v1/drive/folders', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': getCsrfToken()
                },
                credentials: 'include',
                body: JSON.stringify({ name: name.trim() })
            });
            if (res.ok) {
                showLibraryMessage(window.AVi18n.tpl`تم إنشاء المجلد "${name.trim()}" بنجاح ✓`);
                loadFoldersHubData();
            } else {
                const err = await res.json().catch(() => ({}));
                alert(window.AVi18n.t(err.detail) || window.AVi18n.t('تعذر إنشاء المجلد'));
            }
        } catch (_) {
            alert(window.AVi18n.t('حدث خطأ أثناء إنشاء المجلد'));
        }
    });

    document.getElementById('folders-search-input')?.addEventListener('input', (e) => {
        const q = e.target.value.trim().toLowerCase();
        if (!q) {
            renderFoldersCards(allFoldersData);
            return;
        }
        renderFoldersCards(allFoldersData.filter(f => f.name.toLowerCase().includes(q)));
    });

    // --- Trash Hub Data ---
    let allTrashData = [];

    async function loadTrashHubData() {
        const tbody = document.getElementById('trash-tbody');
        const loading = document.getElementById('trash-loading');
        const feedback = document.getElementById('trash-feedback');
        const content = document.getElementById('trash-content');
        const empty = document.getElementById('trash-empty');
        const countInd = document.getElementById('trashCountIndicator');
        const badge = document.getElementById('hubTrashCount');
        const btnEmpty = document.getElementById('btn-empty-trash');
        if (!tbody || !loading) return;

        loading.classList.remove('hidden');
        content.classList.add('hidden');
        empty.classList.add('hidden');

        try {
            const res = await fetch('/api/v1/drive/scope?content_filter=trash', { credentials: 'include', cache: 'no-store' });
            if (!res.ok) throw new Error();
            const data = await res.json();
            const trashItems = Array.isArray(data?.trash_items) ? data.trash_items : [];
            const scopedItems = Array.isArray(data?.items) ? data.items : [];
            allTrashData = (trashItems.length ? trashItems : scopedItems).filter(item => item && item.id && item.type);
            loading.classList.add('hidden');

            const total = allTrashData.length;
            if (countInd) countInd.textContent = window.AVi18n.tpl`${total} عنصر في سلة المحذوفات`;
            if (badge) {
                badge.textContent = String(total);
                badge.classList.toggle('hidden', total === 0);
            }
            if (btnEmpty) btnEmpty.classList.toggle('hidden', total === 0);

            if (total === 0) {
                empty.classList.remove('hidden');
                return;
            }

            renderTrashTable(allTrashData);
            content.classList.remove('hidden');
        } catch (_) {
            loading.classList.add('hidden');
            if (feedback) {
                feedback.textContent = window.AVi18n.t('تعذر تحميل سلة المحذوفات — أعد المحاولة.');
                feedback.classList.remove('hidden');
            }
        }
    }

    function renderTrashTable(items) {
        const tbody = document.getElementById('trash-tbody');
        if (!tbody) return;

        tbody.replaceChildren(...items.map(item => {
            const tr = document.createElement('tr');
            tr.className = 'table-row-modern';

            const tdName = document.createElement('td');
            tdName.className = 'cell-video-main';
            const nameWrap = document.createElement('div');
            nameWrap.style.cssText = 'display:flex;align-items:center;gap:10px';
            const icon = document.createElement('div');
            icon.style.cssText = 'width:32px;height:32px;border-radius:6px;background:rgba(239,68,68,.15);color:#f87171;display:grid;place-items:center;flex-shrink:0';
            icon.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="23 7 16 12 23 17 23 7"/><rect width="14" height="14" x="1" y="5" rx="2"/></svg>';
            const textWrap = document.createElement('div');
            const title = createElement('div', item.name || window.AVi18n.t('عنصر محذوف'), ['video-row-title']);
            title.style.cssText = 'font-weight:600;color:#f8fafc';
            const location = createElement('div', window.AVi18n.tpl`الموقع السابق: ${item.previous_location || window.AVi18n.t('المكتبة')}`, ['video-row-filename']);
            location.style.cssText = 'font-size:11px;color:#94a3b8';
            textWrap.append(title, location);
            nameWrap.append(icon, textWrap);
            tdName.appendChild(nameWrap);

            const tdType = document.createElement('td');
            const typeBadge = createElement('span', item.type_label || item.type || window.AVi18n.t('عنصر'), ['badge']);
            typeBadge.style.background = 'rgba(255,255,255,0.08)';
            tdType.appendChild(typeBadge);

            const tdSize = document.createElement('td');
            tdSize.className = 'cell-text-muted';
            tdSize.textContent = item.size_formatted || formatBytes(item.size_bytes);

            const tdDate = document.createElement('td');
            tdDate.className = 'cell-text-muted';
            tdDate.textContent = item.trashed_at || '—';

            const tdActions = document.createElement('td');
            tdActions.style.textAlign = 'left';

            const btnRestore = document.createElement('button');
            btnRestore.type = 'button';
            btnRestore.className = 'trash-restore-btn';
            btnRestore.innerHTML = window.AVi18n.t('<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg> استعادة');
            btnRestore.addEventListener('click', () => restoreTrashItem(item));

            const btnPurge = document.createElement('button');
            btnPurge.type = 'button';
            btnPurge.className = 'trash-purge-btn';
            btnPurge.innerHTML = window.AVi18n.t('<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg> حذف نهائي');
            btnPurge.addEventListener('click', () => purgeTrashItem(item));

            tdActions.append(btnRestore, btnPurge);
            tr.append(tdName, tdType, tdSize, tdDate, tdActions);
            return tr;
        }));
    }

    async function restoreTrashItem(item) {
        if (!confirm(window.AVi18n.tpl`هل أنت متأكد من استعادة "${item.name}" إلى المكتبة؟`)) return;
        try {
            await driveMutation('/api/v1/drive/restore', [{ id: item.id, type: item.type }]);
            showLibraryMessage(window.AVi18n.tpl`تمت استعادة "${item.name}" بنجاح ✓`);
            await Promise.all([loadTrashHubData(), loadVideos()]);
        } catch (error) {
            showLibraryMessage(window.AVi18n.tpl`تعذرت الاستعادة: ${window.AVi18n.t(error.message)}`, true);
        }
    }

    async function purgeTrashItem(item) {
        if (!confirm(window.AVi18n.tpl`تحذير نهائي: هل تريد حذف "${item.name}" بشكل دائم؟ لا يمكن التراجع عن هذا الإجراء.`)) return;
        try {
            await driveMutation('/api/v1/drive/trash/delete-permanent', [{ id: item.id, type: item.type }]);
            showLibraryMessage(window.AVi18n.tpl`تم الحذف النهائي لـ "${item.name}" ✓`);
            await loadTrashHubData();
        } catch (error) {
            showLibraryMessage(window.AVi18n.tpl`تعذر الحذف النهائي: ${window.AVi18n.t(error.message)}`, true);
        }
    }

    initContentHub();
    loadVideos();
});
