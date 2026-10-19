/* Diagnostics for the video already authorized and loaded by the detail page. */
document.addEventListener('DOMContentLoaded', () => {
    'use strict';
    const root = document.getElementById('video-inspector');
    if (!root) return;
    const find = id => document.getElementById('vi-' + id);
    const btnRun = document.getElementById('vi-run');
    const probeNetworkCheck = document.getElementById('vi-network');
    const loadingEl = find('inspector-loading');
    const resultsContainer = find('inspector-results-container');
    const errorAlert = find('inspector-error-alert');
    let videoId = null, running = false, inspected = false, revision = 0;
    btnRun.addEventListener('click', runInspection);
    root.addEventListener('toggle', () => {if (root.open && !inspected) runInspection();});
    document.addEventListener('abv-video-loaded', event => {
        videoId = String(event.detail.id); btnRun.disabled = false;
        if (root.open) runInspection();
    });
    document.addEventListener('abv-video-subtitles-changed', () => {
        revision++; inspected = false; resultsContainer.classList.add('hidden');
        showError('تغيرت مسارات الترجمة. أعد الفحص لتحديث النتائج.');
    });
    probeNetworkCheck.addEventListener('change', () => {revision++; inspected = false; resultsContainer.classList.add('hidden');});
    const viewportBtns = root.querySelectorAll('.viewport-btn');
    const previewContainer = find('preview-frame-container');
    viewportBtns.forEach(button => button.addEventListener('click', () => {
        viewportBtns.forEach(item => item.classList.remove('is-active')); button.classList.add('is-active');
        if (previewContainer) previewContainer.style.maxWidth = button.dataset.width;
    }));

    async function runInspection() {
        if (!videoId || running) return;
        running = true; const currentRevision = revision;
        showError(null);
        if (loadingEl) loadingEl.classList.remove('hidden');
        if (resultsContainer) resultsContainer.classList.add('hidden');
        if (btnRun) btnRun.disabled = true;

        try {
            const probeNetwork = probeNetworkCheck ? probeNetworkCheck.checked : true;
            const res = await fetch('/api/v1/admin/inspector/check', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': getCsrfToken(),
                },
                credentials: 'include',
                body: JSON.stringify({
                    identifier: videoId,
                    probe_network: probeNetwork,
                }),
            });

            const data = await res.json();
            if (!res.ok) {
                throw new Error(data.detail || data.error || 'حدث خطأ أثناء إجراء الفحص');
            }

            if (currentRevision !== revision) {showError('تغيرت الترجمات أثناء الفحص. أعد الفحص لعرض النتيجة الحالية.'); return;}
            renderResults(data); inspected = true; btnRun.textContent = 'إعادة الفحص';
        } catch (err) {
            showError(err.message || 'فشل الاتصال بخدمة الفحص.');
        } finally {
            if (loadingEl) loadingEl.classList.add('hidden');
            running = false;
            if (btnRun) btnRun.disabled = false;
        }
    }

    function renderResults(data) {
        if (resultsContainer) resultsContainer.classList.remove('hidden');

        // Status banner
        const statusVal = find('stat-overall-status');
        if (statusVal) {
            statusVal.textContent = data.status === 'READY' ? 'جاهز للبث (READY)' : (data.status === 'WARN' ? 'تحذير (WARNING)' : 'غير جاهز (FAILED)');
            statusVal.className = 'summary-stat-value ' + (data.status === 'READY' ? 'status-pass' : (data.status === 'WARN' ? 'status-warn' : 'status-fail'));
        }

        const scoreVal = find('stat-score-pct');
        if (scoreVal && data.summary) {
            scoreVal.textContent = `${data.summary.score_pct}% (${data.summary.passed}/${data.summary.total})`;
        }

        const durationVal = find('stat-duration-ms');
        if (durationVal) {
            durationVal.textContent = `${data.duration_ms} ms`;
        }

        const inspectedAtVal = find('stat-inspected-at');
        if (inspectedAtVal && data.inspected_at) {
            const dt = new Date(data.inspected_at);
            inspectedAtVal.textContent = dt.toLocaleTimeString('ar-SA', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        }

        // Video info
        if (data.video) {
            find('video-info-title').textContent = data.video.title || '—';
            find('video-info-meta').textContent = `${data.video.resolution} · ${data.video.codec} · ${(Number(data.video.duration_seconds) || 0).toFixed(1)} ثانية`;
            const watchLink = find('video-info-watch');
            if (watchLink && data.video.watch_url) {
                watchLink.href = data.video.watch_url;
                watchLink.textContent = data.video.watch_url;
                watchLink.classList.remove('hidden');
            }
            const embedLink = find('video-info-embed');
            if (embedLink && data.video.embed_url) {
                embedLink.href = data.video.embed_url;
                embedLink.textContent = data.video.embed_url;
                embedLink.classList.remove('hidden');
            }
        }

        // Checks table
        const tbody = find('checks-tbody');
        if (tbody && data.checks) {
            tbody.innerHTML = '';
            data.checks.forEach(c => {
                const tr = document.createElement('tr');
                const pillClass = c.status === 'PASS' ? 'pill-pass' : (c.status === 'WARN' ? 'pill-warn' : 'pill-fail');
                const pillLabel = c.status === 'PASS' ? 'ناجح' : (c.status === 'WARN' ? 'تنبيه' : 'فشل');
                const latencyText = c.latency_ms ? `<span class="latency-meter">${c.latency_ms} ms</span>` : '—';

                tr.innerHTML = `
                    <td><strong>${escapeHtml(c.title)}</strong></td>
                    <td><span class="status-pill ${pillClass}">${pillLabel}</span></td>
                    <td>${escapeHtml(c.message || '')}</td>
                    <td><small class="text-muted">${escapeHtml(c.detail || '')}</small></td>
                    <td>${latencyText}</td>
                `;
                tbody.appendChild(tr);
            });
        }

        // HLS Renditions Table
        const hlsTbody = find('renditions-tbody');
        if (hlsTbody && data.hls && data.hls.renditions) {
            hlsTbody.innerHTML = '';
            if (data.hls.renditions.length === 0) {
                hlsTbody.innerHTML = '<tr><td colspan="5" class="text-muted text-center">لا توجد جودات مولّدة.</td></tr>';
            } else {
                data.hls.renditions.forEach(r => {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td><strong>${escapeHtml(r.quality)}</strong></td>
                        <td>${escapeHtml(r.resolution)}</td>
                        <td>${r.bandwidth ? Math.round(r.bandwidth / 1000) + ' kbps' : '—'}</td>
                        <td><code>${escapeHtml(r.codec)}</code></td>
                        <td><span class="status-pill ${r.exists_on_disk ? 'pill-pass' : 'pill-fail'}">${r.exists_on_disk ? 'متوفر' : 'مفقود'}</span></td>
                    `;
                    hlsTbody.appendChild(tr);
                });
            }
        }

        // Subtitles Table
        const subsTbody = find('subtitles-tbody');
        if (subsTbody && data.subtitles) {
            subsTbody.innerHTML = '';
            if (data.subtitles.length === 0) {
                subsTbody.innerHTML = '<tr><td colspan="5" class="text-muted text-center">لا توجد مسارات ترجمة.</td></tr>';
            } else {
                data.subtitles.forEach(s => {
                    const tr = document.createElement('tr');
                    tr.innerHTML = `
                        <td><strong>${escapeHtml(s.label)}</strong> ${s.is_default ? '<span class="badge badge-primary">افتراضي</span>' : ''}</td>
                        <td>${escapeHtml(s.language_name || s.language_code)}</td>
                        <td><code>${escapeHtml(s.format.toUpperCase())}</code></td>
                        <td>${s.file_size ? (s.file_size / 1024).toFixed(1) + ' KB' : '—'}</td>
                        <td><span class="status-pill ${s.exists_on_disk ? 'pill-pass' : 'pill-fail'}">${s.exists_on_disk ? 'جاهز' : 'غير متوفر'}</span></td>
                    `;
                    subsTbody.appendChild(tr);
                });
            }
        }

        // Interactive Preview Frame
        const previewIframe = find('inspector-preview-iframe');
        if (previewIframe && data.video && data.video.share_id) {
            previewIframe.src = `/embed/${encodeURIComponent(data.video.share_id)}`;
        }
    }

    function showError(msg) {
        if (!errorAlert) return;
        if (msg) {
            errorAlert.textContent = msg;
            errorAlert.classList.remove('hidden');
        } else {
            errorAlert.textContent = '';
            errorAlert.classList.add('hidden');
        }
    }

    function getCsrfToken() {
        const match = document.cookie.match(new RegExp('(?:^|;\\s*)' + (document.body.dataset.csrfCookieName || 'aboodvideo_csrf').replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '=([^;]+)'));
        return match ? decodeURIComponent(match[1]) : '';
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }
});
