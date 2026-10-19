/* ============================================================
   ABOODVIDEO Admin UI V3 — Processing Queue Logic (Phase F)
   Tab filtering, operational live rows, sanitized error display,
   context-sensitive action menus, and structured diagnostics modal.
   ============================================================ */

(function () {
    'use strict';

    const UI = window.AboodUI || {};

    function getCookie(name) {
        const v = document.cookie.match('(^|;) ?' + name + '=([^;]*)(;|$)');
        return v ? v[2] : null;
    }
    const csrfToken = getCookie(document.body.dataset.csrfCookieName || 'aboodvideo_csrf') || '';

    const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

    let currentTab = 'active';
    let showTestJobs = false;
    let statusFilter = '';
    let searchFilter = '';
    let cachedJobs = [];
    let cachedStudioItems = [];
    let jobsPage = 1;
    let jobsTotal = 0;
    const jobsPageSize = 25;
    let jobsRequestInFlight = false;
    let workerRequestInFlight = false;
    let localStudioRequestInFlight = false;
    let currentDiagData = null;

    /* ------------------------------------------------------------
       1. Toast & Confirmation Handlers
       ------------------------------------------------------------ */
    function showFeedback(message, isError = false) {
        if (UI.showToast) {
            UI.showToast(message, isError ? 'danger' : 'success');
        }
        const box = document.getElementById('processing-feedback');
        if (box) {
            box.textContent = message;
            box.className = `alert ${isError ? 'alert-danger' : 'alert-success'}`;
            box.classList.remove('hidden');
            window.clearTimeout(showFeedback._t);
            showFeedback._t = window.setTimeout(() => box.classList.add('hidden'), 5000);
        }
    }

    function confirmAction(message, isDanger = true) {
        if (UI.confirm) {
            return UI.confirm({
                title: 'تأكيد الإجراء',
                message: message,
                confirmText: 'تأكيد',
                cancelText: 'إلغاء',
                danger: isDanger
            });
        }
        return new Promise(resolve => {
            const modal = document.getElementById('processing-confirm');
            const yes = document.getElementById('processing-confirm-yes');
            const no = document.getElementById('processing-confirm-no');
            const closeBtn = document.getElementById('processing-confirm-close');
            document.getElementById('processing-confirm-text').textContent = message;
            modal.classList.add('is-open');

            const finish = (val) => {
                modal.classList.remove('is-open');
                yes.removeEventListener('click', onYes);
                no.removeEventListener('click', onNo);
                if (closeBtn) closeBtn.removeEventListener('click', onNo);
                resolve(val);
            };
            const onYes = () => finish(true);
            const onNo = () => finish(false);
            yes.addEventListener('click', onYes);
            no.addEventListener('click', onNo);
            if (closeBtn) closeBtn.addEventListener('click', onNo);
            yes.focus();
        });
    }

    /* ------------------------------------------------------------
       2. Worker Health Telemetry
       ------------------------------------------------------------ */
    async function fetchWorkerStatus() {
        if (workerRequestInFlight) return;
        workerRequestInFlight = true;
        try {
            const res = await fetch('/api/v1/admin/transcodes/worker-status', {
                cache: 'no-store',
                credentials: 'include',
                headers: { 'X-CSRF-Token': csrfToken }
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            const dot = document.getElementById('worker-dot');
            const txt = document.getElementById('worker-text');
            if (dot && txt) {
                const status = (data.status || 'OFFLINE').toUpperCase();
                if (status === 'ONLINE') {
                    dot.className = 'status-dot is-online';
                    txt.textContent = `معالج التحويل: متصل نشط (${data.worker_id || 'RQ Worker'})`;
                } else if (status === 'STALE') {
                    dot.className = 'status-dot is-warning';
                    txt.textContent = `معالج التحويل: خامل (${data.last_seen_seconds_ago || 0} ثانية)`;
                } else {
                    dot.className = 'status-dot is-offline';
                    txt.textContent = 'معالج التحويل: غير متصل';
                }
            }
        } catch (_) {
            const dot = document.getElementById('worker-dot');
            const txt = document.getElementById('worker-text');
            if (dot) dot.className = 'status-dot is-offline';
            if (txt) txt.textContent = 'تعذر الاتصال بمعالج التحويل';
        } finally {
            workerRequestInFlight = false;
        }
    }

    /* ------------------------------------------------------------
       3. Queue Summary Statistics
       ------------------------------------------------------------ */
    async function fetchQueueStats() {
        try {
            const testParam = showTestJobs ? '&include_test=true' : '';
            const [activeRes, queuedRes, probingRes, failedRes, completedRes, cancelledRes] = await Promise.all([
                fetch(`/api/v1/admin/transcodes?status=PROCESSING&limit=1${testParam}`, { cache: 'no-store', credentials: 'include' }),
                fetch(`/api/v1/admin/transcodes?status=QUEUED&limit=1${testParam}`, { cache: 'no-store', credentials: 'include' }),
                fetch(`/api/v1/admin/transcodes?status=PROBING&limit=1${testParam}`, { cache: 'no-store', credentials: 'include' }),
                fetch(`/api/v1/admin/transcodes?status=FAILED&limit=1${testParam}`, { cache: 'no-store', credentials: 'include' }),
                fetch(`/api/v1/admin/transcodes?status=COMPLETED&limit=1${testParam}`, { cache: 'no-store', credentials: 'include' }),
                fetch(`/api/v1/admin/transcodes?status=CANCELLED&limit=1${testParam}`, { cache: 'no-store', credentials: 'include' })
            ]);

            let activeCount = 0;
            let queuedCount = 0;
            let failedCount = 0;
            let completedCount = 0;
            let cancelledCount = 0;

            if (activeRes.ok) {
                const d = await activeRes.json();
                activeCount = d.total || 0;
                const el = document.getElementById('stat-active');
                if (el) el.textContent = activeCount;
            }

            let qTotal = 0, pTotal = 0;
            if (queuedRes.ok) qTotal = (await queuedRes.json()).total || 0;
            if (probingRes.ok) pTotal = (await probingRes.json()).total || 0;
            queuedCount = qTotal + pTotal;
            const qEl = document.getElementById('stat-queued');
            if (qEl) qEl.textContent = queuedCount;

            if (failedRes.ok) {
                failedCount = (await failedRes.json()).total || 0;
                const el = document.getElementById('stat-failed');
                if (el) el.textContent = failedCount;
            }

            if (completedRes.ok) {
                completedCount = (await completedRes.json()).total || 0;
                const el = document.getElementById('stat-completed');
                if (el) el.textContent = completedCount;
            }

            if (cancelledRes.ok) {
                cancelledCount = (await cancelledRes.json()).total || 0;
            }

            // Update primary tab badges
            updateTabBadge('active', activeCount);
            updateTabBadge('waiting', queuedCount);
            updateTabBadge('completed', completedCount);
            updateTabBadge('failed', failedCount + cancelledCount);

            // Update topbar processing badge
            document.querySelectorAll('[data-processing-badge]').forEach(b => {
                b.textContent = activeCount;
                b.classList.toggle('hidden', activeCount === 0);
            });
        } catch (err) {
            console.warn('[Processing] Error fetching stats:', err);
        }
    }

    function updateTabBadge(tabName, count) {
        const badge = document.getElementById(`tab-badge-${tabName}`);
        if (badge) badge.textContent = count;
    }

    function updateJobsPagination() {
        const suffix = ['completed', 'failed'].includes(currentTab) ? 'history' : 'active';
        const pageCount = Math.max(1, Math.ceil(jobsTotal / jobsPageSize));
        const label = document.getElementById(`jobs-page-${suffix}`);
        const previous = document.getElementById(`jobs-previous-${suffix}`);
        const next = document.getElementById(`jobs-next-${suffix}`);
        if (label) label.textContent = `صفحة ${jobsPage} من ${pageCount} · ${jobsTotal} مهمة`;
        if (previous) previous.disabled = jobsPage <= 1;
        if (next) next.disabled = jobsPage >= pageCount;
    }

    /* ------------------------------------------------------------
       4. Fetch & Filter Jobs
       ------------------------------------------------------------ */
    async function fetchJobs() {
        if (jobsRequestInFlight) return;
        jobsRequestInFlight = true;
        try {
            const testParam = showTestJobs ? '&include_test=true' : '';
            const apiTab = ['waiting'].includes(currentTab) ? 'queued' : ['completed', 'failed'].includes(currentTab) ? 'history' : 'active';
            const res = await fetch(`/api/v1/admin/transcodes?tab=${apiTab}&page=${jobsPage}&limit=${jobsPageSize}${testParam}`, {
                credentials: 'include',
                cache: 'no-store',
                headers: { 'X-CSRF-Token': csrfToken }
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            cachedJobs = Array.isArray(data.items) ? data.items : [];
            jobsTotal = Number(data.total) || 0;
            updateJobsPagination();
            renderCurrentTab();

            const polledAt = document.getElementById('jobs-last-updated');
            if (polledAt) polledAt.textContent = new Date().toLocaleTimeString('ar-EG');
        } catch (err) {
            const tbody = getJobsBody();
            if (tbody) {
                tbody.innerHTML = '<tr><td colspan="9" class="table-empty-cell text-danger">تعذر الاتصال بخدمة وظائف التحويل.</td></tr>';
            }
        } finally {
            jobsRequestInFlight = false;
        }
    }

    async function fetchLocalStudioProcessing() {
        if (localStudioRequestInFlight) return;
        localStudioRequestInFlight = true;
        const body = document.getElementById('local-studio-tbody');
        const statusEl = document.getElementById('local-studio-status');
        try {
            const res = await fetch('/api/v1/admin/transcodes/local-studio', {
                credentials: 'include', cache: 'no-store',
                headers: { 'X-CSRF-Token': csrfToken }
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            const items = Array.isArray(data.items) ? data.items : [];
            cachedStudioItems = items;
            updateTabBadge('ready', items.filter(x => String(x.status || '').toUpperCase() === 'READY_TO_UPLOAD').length);
            const active = items.filter(x => ['connected', 'stale'].includes(x.connection_state));
            const connectedCount = active.filter(x => x.connection_state === 'connected').length;
            const staleCount = active.length - connectedCount;
            if (statusEl) {
                if (connectedCount) statusEl.textContent = `Studio متصل — ${connectedCount} مهمة نشطة${staleCount ? `، و${staleCount} حالة قديمة بانتظار استئناف الإرسال` : ''}.`;
                else if (staleCount) statusEl.textContent = `لا يوجد اتصال حديث من Studio — ${staleCount} مهمة توقفت تقاريرها؛ آخر حالة محفوظة أدناه.`;
                else statusEl.textContent = 'لا يوجد تقرير حديث من Studio. تظهر الحالات هنا عند التحويل أو الرفع.';
            }
            if (!body) return;
            renderStudioForTab();
        } catch (_) {
            if (statusEl) statusEl.textContent = 'تعذر جلب حالة Studio المحلي.';
            if (body) body.innerHTML = '<tr><td colspan="7" class="table-empty-cell text-danger">تعذر الاتصال بتقارير Studio المحلية.</td></tr>';
        } finally {
            localStudioRequestInFlight = false;
        }
    }

    function renderStudioForTab() {
            const body = document.getElementById('local-studio-tbody');
            if (!body) return;
            const allowed = {
                active: ['ENCODING', 'PACKAGING', 'VERIFYING_LOCAL', 'UPLOADING', 'SERVER_VERIFYING', 'READY_TO_ENCODE', 'PROBING'],
                waiting: ['PROBING', 'READY_TO_ENCODE'],
                ready: ['READY_TO_UPLOAD'],
                completed: ['READY'],
                failed: ['FAILED', 'CANCELLED', 'NEEDS_REVIEW'],
            }[currentTab] || [];
            const items = cachedStudioItems.filter(item => allowed.includes(String(item.status || '').toUpperCase()) || (currentTab === 'failed' && item.connection_state === 'stale'));
            if (!items.length) {
                const msg = currentTab === 'ready' ? 'لا توجد ملفات جاهزة للرفع من Studio.' : 'لا توجد مهام Studio في هذا القسم.';
                body.innerHTML = `<tr><td colspan="7" class="table-empty-cell">${msg}</td></tr>`;
                return;
            }
            const labels = {
                PROBING: 'فحص المصدر', READY_TO_ENCODE: 'جاهز للتحويل', ENCODING: 'قيد التحويل',
                PACKAGING: 'تجهيز الحزمة', VERIFYING_LOCAL: 'التحقق المحلي', UPLOADING: 'جارٍ الرفع',
                SERVER_VERIFYING: 'تحقق الخادم', READY_TO_UPLOAD: 'جاهز للرفع', READY: 'اكتمل',
                FAILED: 'فشل', CANCELLED: 'أُلغي', NEEDS_REVIEW: 'يتطلب مراجعة'
            };
            body.innerHTML = items.slice(0, 50).map(item => {
                const pct = Math.max(0, Math.min(100, Number(item.progress) || 0));
                const when = item.updated_at ? new Date(Number(item.updated_at) * 1000).toLocaleTimeString('ar') : '—';
                const location = [item.batch_name, item.season_number ? `موسم ${item.season_number}` : '', item.episode_number ? `حلقة ${item.episode_number}` : ''].filter(Boolean).join(' · ');
                const connectionLabel = item.connection_state === 'stale' ? ' · انقطع التحديث' : '';
                return `<tr><td><strong>${esc(item.title || '—')}</strong><div class="text-muted">${esc(location || 'Studio المحلي')}</div></td>
                    <td>${esc(labels[String(item.status || '').toUpperCase()] || item.status || '—')}${esc(connectionLabel)}<div class="text-muted">${esc(item.stage || '')}</div></td>
                    <td><div class="job-progress-row" aria-label="${pct}%"><div class="progress-bar"><div class="progress-bar__fill" style="width:${pct}%"></div></div><span class="progress-pct">${pct.toFixed(1)}%</span></div></td>
                    <td>${esc(item.quality || '—')}</td><td>${esc(item.speed || '—')} ${item.fps ? `· ${esc(item.fps)} FPS` : ''}</td>
                    <td>${esc(item.eta || '—')}</td><td>${esc(when)}</td></tr>`;
            }).join('');
    }

    function filterJobs(tab, jobs) {
        let result = jobs;

        if (tab === 'active') result = result.filter(j => ['PROCESSING', 'VERIFYING'].includes(j.status));
        else if (tab === 'waiting') result = result.filter(j => ['QUEUED', 'PROBING'].includes(j.status));
        else if (tab === 'ready') result = [];
        else if (tab === 'completed') result = result.filter(j => j.status === 'COMPLETED');
        else if (tab === 'failed') result = result.filter(j => ['FAILED', 'CANCELLED'].includes(j.status));

        if (statusFilter) {
            result = result.filter(j => j.status === statusFilter);
        }

        if (searchFilter) {
            result = result.filter(j => (j.video_title || '').toLowerCase().includes(searchFilter));
        }

        return result;
    }

    function getJobsBody() {
        return document.getElementById(['completed', 'failed'].includes(currentTab) ? 'jobs-tbody-history' : 'jobs-tbody-active') || document.getElementById('jobs-tbody');
    }

    function renderCurrentTab() {
        const tbody = getJobsBody();
        if (!tbody) return;

        const filtered = filterJobs(currentTab, cachedJobs);
        renderStudioForTab();
        if (!filtered.length) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="9" class="table-empty-cell">
                        <div class="state-empty state-empty-padded">
                            <div class="empty-title">لا توجد وظائف معالجة في هذا التبويب (${esc(currentTab)})</div>
                            <div class="empty-desc">طابور المهام محدث وخالٍ من العمليات المعلقة حالياً.</div>
                        </div>
                    </td>
                </tr>`;
            return;
        }

        tbody.innerHTML = filtered.map(job => renderJobRow(job)).join('');
        attachPosters(tbody);
    }

    /* ------------------------------------------------------------
       5. Render Individual Job Row (Table V3)
       ------------------------------------------------------------ */
    function renderJobRow(job) {
        const status = (job.status || 'UNKNOWN').toUpperCase();
        const progress = Number(job.progress_percent) || 0;
        const boundedProgress = Math.max(0, Math.min(100, Math.round(progress)));

        // Rendition label
        const renditionLabel = job.current_rendition === '2160p' ? '4K · 2160p' : (job.current_rendition || '—');

        // Telemetry
        const speed = job.ffmpeg_speed ? `${Number(job.ffmpeg_speed).toFixed(2)}x` : '—';
        const fps = job.ffmpeg_fps ? `${Math.round(job.ffmpeg_fps)} FPS` : '—';
        const eta = job.eta_seconds != null ? (UI.formatEta ? UI.formatEta(job.eta_seconds) : `${Math.round(job.eta_seconds / 60)} د`) : '—';
        const worker = job.worker_id ? `<span class="font-mono text-xs dir-ltr">${esc(job.worker_id)}</span>` : '—';
        const attempt = `<span class="dir-ltr font-mono text-number">${job.attempt} / ${job.max_attempts}</span>`;

        // 1. Status Badge
        let statusBadge = '';
        if (status === 'PROCESSING' || status === 'PROBING' || status === 'VERIFYING') {
            statusBadge = `
                <span class="status-badge status-processing text-xs">
                    <span class="badge-dot"></span>
                    <span>${status === 'VERIFYING' ? 'قيد التحقق' : status === 'PROBING' ? 'فحص المصدر' : 'قيد المعالجة'}</span>
                </span>`;
        } else if (status === 'QUEUED') {
            statusBadge = `
                <span class="status-badge status-queued text-xs">
                    <span class="badge-dot"></span>
                    <span>في الانتظار</span>
                </span>`;
        } else if (status === 'COMPLETED') {
            statusBadge = `
                <span class="status-badge status-ready text-xs">
                    <span class="badge-dot"></span>
                    <span>مكتمل</span>
                </span>`;
        } else if (status === 'FAILED') {
            statusBadge = `
                <span class="status-badge status-failed text-xs">
                    <span class="badge-dot"></span>
                    <span>فشل</span>
                </span>`;
        } else if (status === 'CANCELLED') {
            statusBadge = `
                <span class="status-badge status-warning text-xs">
                    <span class="badge-dot"></span>
                    <span>ملغى</span>
                </span>`;
        } else {
            statusBadge = `<span class="status-badge status-queued text-xs">${esc(status)}</span>`;
        }

        // 2. Progress / Diagnostic Cell
        let progressCellContent = '';
        if (status === 'PROCESSING' || status === 'PROBING' || status === 'VERIFYING') {
            progressCellContent = `
                <div class="job-progress-cell">
                    <div class="job-progress-header">
                        <span class="font-mono text-number dir-ltr">${boundedProgress}٪</span>
                        <span class="text-xs text-secondary font-mono">${esc(renditionLabel)}</span>
                    </div>
                    <div class="job-progress-bar">
                        <svg class="job-progress-svg" viewBox="0 0 100 6" preserveAspectRatio="none"><rect x="0" y="0" width="${boundedProgress}" height="6" rx="3"></rect></svg>
                    </div>
                </div>`;
        } else if (status === 'FAILED') {
            // Clean Arabic explanation without raw backend/FFmpeg error in table!
            const friendlyReason = UI.translateErrorCode ? UI.translateErrorCode(job.error_code) : 'انقطع عامل المعالجة أو أعيد تشغيل الخادم قبل اكتمال العملية.';
            progressCellContent = `
                <div class="job-failed-summary">
                    <span class="job-failed-text">${esc(friendlyReason)}</span>
                    <button type="button" class="btn-view-diag-link" data-action="view-diag" data-id="${esc(job.id)}">
                        <span>عرض التفاصيل والتشخيص ←</span>
                    </button>
                </div>`;
        } else if (status === 'COMPLETED') {
            progressCellContent = `
                <div class="job-progress-cell">
                    <div class="job-progress-header">
                        <span class="font-mono text-success">100٪</span>
                        <span class="text-xs text-muted">جميع الجودات جاهزة</span>
                    </div>
                    <div class="job-progress-bar">
                        <svg class="job-progress-svg is-success" viewBox="0 0 100 6" preserveAspectRatio="none"><rect x="0" y="0" width="100" height="6" rx="3"></rect></svg>
                    </div>
                </div>`;
        } else {
            progressCellContent = `<span class="text-muted text-xs font-mono">${boundedProgress}٪</span>`;
        }

        // 3. Context Menu Actions (Canonical V3 Dropdown)
        const actionsMenu = renderContextMenu(job);
        const thumbSlot = job.has_poster
            ? `<div class="job-cell-thumb" data-poster="${esc(job.video_id)}"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m10 9 5 3-5 3z"/></svg></div>`
            : `<div class="job-cell-thumb"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m10 9 5 3-5 3z"/></svg></div>`;

        return `
            <tr data-job-id="${esc(job.id)}">
                <td>
                    <div class="job-video-cell">
                        ${thumbSlot}
                        <div class="job-cell-info">
                            <strong class="job-cell-title" title="${esc(job.video_title || 'فيديو')}">${esc(job.video_title || 'فيديو')}</strong>
                            <span class="job-cell-id">${esc(job.id.substring(0, 8))}...</span>
                        </div>
                    </div>
                </td>
                <td>${statusBadge}</td>
                <td>${progressCellContent}</td>
                <td class="font-mono text-xs">${esc(renditionLabel)}</td>
                <td class="font-mono text-xs dir-ltr">${speed} · ${fps}</td>
                <td class="text-xs">${eta}</td>
                <td>${worker}</td>
                <td>${attempt}</td>
                <td class="td-action">${actionsMenu}</td>
            </tr>`;
    }

    /* ------------------------------------------------------------
       6. Context-Sensitive Action Menu Generator
       ------------------------------------------------------------ */
    function renderContextMenu(job) {
        const status = (job.status || '').toUpperCase();
        let itemsHtml = '';

        if (status === 'PROCESSING' || status === 'PROBING') {
            itemsHtml += `
                <button type="button" class="dropdown-item" data-action="view-diag" data-id="${esc(job.id)}">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
                    <span>عرض التفاصيل</span>
                </button>
                <div class="dropdown-divider"></div>
                <button type="button" class="dropdown-item is-danger" data-action="cancel-job" data-id="${esc(job.id)}">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
                    <span>إلغاء المعالجة</span>
                </button>`;
        } else if (status === 'FAILED') {
            const regenAttr = job.regenerate_renditions && job.regenerate_renditions.length ? `data-regenerate="${esc(job.regenerate_renditions.join(','))}"` : '';
            itemsHtml += `
                <button type="button" class="dropdown-item" data-action="retry-job" data-id="${esc(job.id)}" ${regenAttr}>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 2v6h-6M3 12a9 9 0 0 1 15-6.7L21 8M3 22v-6h6M21 12a9 9 0 0 1-15 6.7L3 16"/></svg>
                    <span>إعادة المحاولة</span>
                </button>
                <button type="button" class="dropdown-item" data-action="view-diag" data-id="${esc(job.id)}">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
                    <span>التشخيص الفني</span>
                </button>
                <button type="button" class="dropdown-item" data-action="view-log" data-id="${esc(job.id)}">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
                    <span>عرض السجل</span>
                </button>`;
        } else if (status === 'QUEUED') {
            itemsHtml += `
                <button type="button" class="dropdown-item is-danger" data-action="cancel-job" data-id="${esc(job.id)}">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
                    <span>إلغاء من الطابور</span>
                </button>`;
        } else if (status === 'COMPLETED') {
            itemsHtml += `
                <a href="/admin/videos/detail?v=${esc(job.video_id)}" class="dropdown-item">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                    <span>عرض الفيديو</span>
                </a>
                <button type="button" class="dropdown-item" data-action="view-diag" data-id="${esc(job.id)}">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
                    <span>تفاصيل المهمة</span>
                </button>`;
        } else if (status === 'CANCELLED') {
            itemsHtml += `
                <button type="button" class="dropdown-item" data-action="retry-job" data-id="${esc(job.id)}">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 2v6h-6M3 12a9 9 0 0 1 15-6.7L21 8M3 22v-6h6M21 12a9 9 0 0 1-15 6.7L3 16"/></svg>
                    <span>إعادة المحاولة</span>
                </button>
                <button type="button" class="dropdown-item" data-action="view-diag" data-id="${esc(job.id)}">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>
                    <span>عرض التفاصيل</span>
                </button>`;
        }

        return `
            <div class="dropdown">
                <button type="button" class="btn btn-icon btn-ghost dropdown-trigger" data-dropdown-toggle aria-haspopup="true" aria-expanded="false" title="إجراءات">
                    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/><circle cx="5" cy="12" r="1.5"/></svg>
                </button>
                <div class="dropdown-menu">
                    ${itemsHtml}
                </div>
            </div>`;
    }

    /* ------------------------------------------------------------
       7. Job Actions: Cancel & Retry
       ------------------------------------------------------------ */
    async function cancelJob(jobId) {
        if (!await confirmAction('هل أنت متأكد من إلغاء وظيفة التحويل هذه فورياً؟', true)) return;
        try {
            const res = await fetch(`/api/v1/admin/transcodes/${jobId}/cancel`, {
                method: 'POST',
                headers: { 'X-CSRF-Token': csrfToken }
            });
            if (res.ok) {
                showFeedback('تم إلغاء وظيفة المعالجة بنجاح.');
                fetchJobs();
                fetchQueueStats();
            } else {
                const err = await res.json().catch(() => ({}));
                showFeedback('تعذر إلغاء الوظيفة: ' + (err.detail || 'خطأ غير معروف'), true);
            }
        } catch (_) {
            showFeedback('تعذر الاتصال بالخادم أثناء الإلغاء.', true);
        }
    }

    async function retryJob(jobId, regenerateList) {
        let message = 'إنشاء محاولة تحويل جديدة لهذه الوظيفة؟';
        if (regenerateList && regenerateList.length) {
            message += '\nسيتم توليد الجودات التالية فقط: ' + regenerateList.join('، ');
        }
        if (!await confirmAction(message, false)) return;

        try {
            const res = await fetch(`/api/v1/admin/transcodes/${jobId}/retry`, {
                method: 'POST',
                headers: { 'X-CSRF-Token': csrfToken }
            });
            if (res.ok) {
                showFeedback('تمت إعادة إدراج الوظيفة في طابور المعالجة بنجاح.');
                closeModal();
                fetchJobs();
                fetchQueueStats();
            } else {
                const err = await res.json().catch(() => ({}));
                showFeedback('تعذر إعادة تشغيل الوظيفة: ' + (err.detail || 'خطأ غير معروف'), true);
            }
        } catch (_) {
            showFeedback('تعذر الاتصال بالخادم أثناء إعادة المحاولة.', true);
        }
    }

    /* ------------------------------------------------------------
       8. Technical Diagnostics Modal (Structured View)
       ------------------------------------------------------------ */
    async function viewDiag(jobId) {
        try {
            const res = await fetch(`/api/v1/admin/transcodes/${jobId}`, {
                headers: { 'X-CSRF-Token': csrfToken }
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            currentDiagData = data;

            // Populate metadata
            const setText = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
            setText('diag-job-id-header', data.id);
            setText('diag-job-id', data.id);
            setText('diag-error-code', data.error_code || 'NONE (سليم)');
            const attemptEl = document.getElementById('diag-attempt');
            if (attemptEl) attemptEl.innerHTML = `<span class="dir-ltr font-mono text-number">${data.attempt || 1} / ${data.max_attempts || 3}</span>`;
            setText('diag-worker', data.worker_id || 'RQ Worker');
            setText('diag-started-at', data.started_at ? new Date(data.started_at).toLocaleString('ar-EG') : '—');
            setText('diag-completed-at', data.completed_at ? new Date(data.completed_at).toLocaleString('ar-EG') : '—');

            // Populate Checkpoint Recovery Card
            const recoveryBox = document.getElementById('diag-recovery-box');
            if (recoveryBox) {
                const recRenditions = data.recoverable_renditions || [];
                const regenRenditions = data.regenerate_renditions || [];
                if (recRenditions.length > 0 || regenRenditions.length > 0) {
                    const totalRenditions = recRenditions.length + regenRenditions.length;
                    setText('recovery-summary-text', `${recRenditions.length} من ${totalRenditions} جودات محفوظة ومكتملة`);
                    const listEl = document.getElementById('recovery-renditions-list');
                    if (listEl) {
                        listEl.innerHTML = recRenditions.map(r => `<span class="recovery-rendition-pill">✓ ${esc(r)}</span>`).join('');
                    }
                    setText('recovery-target-rendition', regenRenditions.length ? regenRenditions.join(', ') : 'جميع الجودات مكتملة');
                    recoveryBox.classList.remove('hidden');
                } else {
                    recoveryBox.classList.add('hidden');
                }
            }

            // Populate Human-readable Diagnosis Banner
            const summaryBox = document.getElementById('diag-summary-box');
            const summaryTitle = document.getElementById('diag-summary-title');
            const summaryDesc = document.getElementById('diag-summary-desc');
            if (summaryBox && summaryTitle && summaryDesc) {
                summaryBox.className = 'diag-summary-box';
                if (data.status === 'FAILED') {
                    summaryBox.classList.add('is-danger');
                    summaryTitle.textContent = `فشل التحويل (${data.error_code || 'TRANSCODE_ERROR'})`;
                    if (data.error_code === 'TRANSCODE_ABANDONED') {
                        summaryDesc.textContent = 'توقف المعالج نتيجة إعادة تشغيل الخادم أو انقطاع الحاوية قبل إتمام كافة الجودات. الجودات السابقة محفوظة في نقطة الاسترداد ويمكن استئناف المعالجة دون إعادة إنشائها.';
                    } else if (data.error_code === 'CHECKPOINT_MISMATCH') {
                        summaryDesc.textContent = 'فشل التحقق من تطابق وسلامة ملفات الجودات المخزنة في نقطة الاسترداد.';
                    } else {
                        summaryDesc.textContent = data.error_message || 'حدث خطأ أثناء معالجة ملف الفيديو، يمكنك مراجعة السجل التقني أدناه.';
                    }
                } else if (data.status === 'PROCESSING' || data.status === 'PROBING') {
                    summaryBox.classList.add('is-info');
                    summaryTitle.textContent = `قيد المعالجة النشطة (${data.current_rendition || 'الترميز'})`;
                    summaryDesc.textContent = `المعالج يعمل بسرعة ${data.ffmpeg_speed || 1}x بمعدل ${data.ffmpeg_fps || 0} إطار/ثانية.`;
                } else if (data.status === 'COMPLETED') {
                    summaryBox.classList.add('is-success');
                    summaryTitle.textContent = 'اكتملت جميع مراحل التحويل بنجاح';
                    summaryDesc.textContent = 'الفيديو متاح للمشاهدة بتقنية HLS التكيفية بجميع الجودات المستهدفة.';
                } else {
                    summaryBox.classList.add('is-info');
                    summaryTitle.textContent = `حالة الوظيفة: ${data.status}`;
                    summaryDesc.textContent = 'الوظيفة مدرجة في طابور انتظار المعالجة وستبدأ فور توفر موارد العامل.';
                }
            }

            // Raw Monospace Block inside Collapsed Details
            const logDetails = document.getElementById('diag-log-details');
            if (logDetails) logDetails.open = false;

            const pre = document.getElementById('diag-content');
            if (pre) {
                pre.textContent = JSON.stringify(data, null, 2);
            }

            // Retry Button Visibility
            const retryBtn = document.getElementById('btn-diag-retry');
            if (retryBtn) {
                if (['FAILED', 'CANCELLED'].includes(data.status) && (data.attempt < data.max_attempts)) {
                    retryBtn.classList.remove('hidden');
                    retryBtn.onclick = () => retryJob(data.id, data.regenerate_renditions);
                } else {
                    retryBtn.classList.add('hidden');
                }
            }

            const modal = document.getElementById('diag-modal');
            if (modal) modal.classList.add('is-open');
        } catch (_) {
            showFeedback('تعذر جلب تفاصيل التشخيص الفني.', true);
        }
    }

    function closeModal() {
        const modal = document.getElementById('diag-modal');
        if (modal) modal.classList.remove('is-open');
    }

    function attachPosters(root) {
        root.querySelectorAll('[data-poster]').forEach(slot => {
            const id = slot.getAttribute('data-poster');
            if (!id) return;
            const img = new Image();
            img.alt = '';
            img.onload = () => { slot.innerHTML = ''; slot.appendChild(img); };
            img.onerror = () => {};
            img.src = `/api/v1/admin/videos/${id}/poster`;
        });
    }

    /* ------------------------------------------------------------
       9. Event Listeners & Tab Handlers
       ------------------------------------------------------------ */
    document.addEventListener('DOMContentLoaded', () => {
        // Tab switching
        document.querySelectorAll('.tab-btn[data-tab]').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.tab-btn').forEach(b => {
                    b.classList.remove('is-active');
                    b.setAttribute('aria-selected', 'false');
                });
                btn.classList.add('is-active');
                btn.setAttribute('aria-selected', 'true');
                currentTab = btn.getAttribute('data-tab') || 'active';
                jobsPage = 1;
                document.querySelectorAll('.tab-panel').forEach(panel => {
                    const isCurrent = panel.id === btn.getAttribute('aria-controls');
                    panel.classList.toggle('hidden', !isCurrent);
                });
                btn.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'center' });
                fetchJobs();
                fetchLocalStudioProcessing();
            });
        });

        [['previous', 'active', -1], ['next', 'active', 1], ['previous', 'history', -1], ['next', 'history', 1]].forEach(([direction, group, delta]) => {
            document.getElementById(`jobs-${direction}-${group}`)?.addEventListener('click', () => {
                jobsPage = Math.max(1, jobsPage + delta);
                fetchJobs();
            });
        });

        // Refresh button
        const refreshBtn = document.getElementById('btn-refresh-jobs');
        if (refreshBtn) {
            refreshBtn.addEventListener('click', () => {
                fetchJobs();
                fetchQueueStats();
                fetchWorkerStatus();
            });
        }

        // Status Filter dropdown
        const statusSelect = document.getElementById('filter-job-status');
        if (statusSelect) {
            statusSelect.addEventListener('change', (e) => {
                statusFilter = e.target.value;
                renderCurrentTab();
            });
        }

        // Title search input
        const titleInput = document.getElementById('filter-job-title');
        if (titleInput) {
            titleInput.addEventListener('input', (e) => {
                searchFilter = e.target.value.trim().toLowerCase();
                renderCurrentTab();
            });
        }

        // Test jobs toggle
        const testToggle = document.getElementById('toggle-test-jobs');
        if (testToggle) {
            testToggle.checked = showTestJobs;
            testToggle.addEventListener('change', (e) => {
                showTestJobs = e.target.checked;
                fetchJobs();
                fetchQueueStats();
            });
        }

        // Close diag modal
        const closeDiagBtn = document.getElementById('btn-close-diag');
        const closeDiagFooter = document.getElementById('btn-diag-close');
        if (closeDiagBtn) closeDiagBtn.addEventListener('click', closeModal);
        if (closeDiagFooter) closeDiagFooter.addEventListener('click', closeModal);

        // Copy buttons
        const copyJobIdBtn = document.getElementById('btn-copy-job-id');
        if (copyJobIdBtn) {
            copyJobIdBtn.addEventListener('click', () => {
                if (currentDiagData && currentDiagData.id) {
                    navigator.clipboard.writeText(currentDiagData.id).then(() => {
                        showFeedback('تم نسخ معرّف الوظيفة.');
                    });
                }
            });
        }

        const copyDiagLogBtn = document.getElementById('btn-copy-diag-log');
        if (copyDiagLogBtn) {
            copyDiagLogBtn.addEventListener('click', () => {
                const pre = document.getElementById('diag-content');
                if (pre) {
                    navigator.clipboard.writeText(pre.textContent).then(() => {
                        showFeedback('تم نسخ سجل التشخيص بالكامل.');
                    });
                }
            });
        }

        // Event delegation for actions
        document.addEventListener('click', (e) => {
            const target = e.target.closest('[data-action]');
            if (!target) return;
            const action = target.getAttribute('data-action');
            const id = target.getAttribute('data-id');
            if (action === 'view-diag' || action === 'view-log') {
                viewDiag(id);
            } else if (action === 'cancel-job') {
                cancelJob(id);
            } else if (action === 'retry-job') {
                const regen = (target.getAttribute('data-regenerate') || '').split(',').filter(Boolean);
                retryJob(id, regen);
            }
        });

        // Initial fetch
        fetchWorkerStatus();
        fetchJobs();
        fetchQueueStats();
        fetchLocalStudioProcessing();

        // Intervals
        window.setInterval(fetchWorkerStatus, 5000);
        window.setInterval(fetchJobs, 3000);
        window.setInterval(fetchQueueStats, 10000);
        window.setInterval(fetchLocalStudioProcessing, 3000);
    });

    // Global exports for legacy test compatibility
    window.viewDiag = viewDiag;
    window.cancelJob = cancelJob;
    window.retryJob = retryJob;
    window.closeModal = closeModal;

})();
