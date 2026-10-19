/**
 * Admin Analytics Dashboard Client Logic
 * Handles telemetry fetching, KPI updating, quality distribution, and one-click embed copying.
 */
document.addEventListener('DOMContentLoaded', () => {
    function escapeHTML(value) {
        return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    const periodSelect = document.getElementById('analytics-period-select');
    const refreshBtn = document.getElementById('btn-refresh-analytics');
    let geoMap = null;

    function formatNumber(num) {
        return new Intl.NumberFormat('ar-EG').format(num || 0);
    }

    function showToast(msg) {
        const toast = document.getElementById('analytics-toast');
        if (!toast) return;
        toast.textContent = msg;
        toast.classList.remove('hidden');
        setTimeout(() => {
            toast.classList.add('hidden');
        }, 3000);
    }

    async function loadAnalytics() {
        const days = periodSelect ? periodSelect.value : 30;
        try {
            const res = await fetch(`/api/v1/admin/analytics/overview?days=${days}`, {
                headers: { 'Accept': 'application/json' }
            });
            if (!res.ok) {
                console.error('Failed to load analytics', res.status);
                return;
            }
            const data = await res.json();
            renderKPIs(data.kpis);
            renderQualityDistribution(data.quality_distribution);
            renderReferrers(data.referrer_domains);
            renderVideosTable(data.top_videos);
            await loadGeoAnalytics(days);
        } catch (err) {
            console.error('Error loading analytics:', err);
        }
    }

    function csrfToken() {
        const match = document.cookie.match(new RegExp('(?:^|;\\s*)' + (document.body.dataset.csrfCookieName || 'aboodvideo_csrf').replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '=([^;]+)'));
        return match ? decodeURIComponent(match[1]) : '';
    }

    function renderGeoMap(countries) {
        const host = document.getElementById('geo-world-map');
        const empty = document.getElementById('geo-map-empty');
        const entries = Object.entries(countries || {}).filter(([code, count]) => /^[A-Z]{2}$/.test(code) && Number(count) > 0);
        const badge = document.getElementById('geo-map-count');
        if (badge) badge.textContent = `${formatNumber(entries.length)} دولة`;
        if (empty) empty.classList.toggle('hidden', entries.length > 0);
        if (!host || !window.jsVectorMap) return;

        const values = Object.fromEntries(entries);
        if (!geoMap) {
            geoMap = new jsVectorMap({
                selector: '#geo-world-map',
                map: 'world',
                zoomButtons: true,
                zoomOnScroll: false,
                regionStyle: {
                    initial: { fill: '#253247', stroke: '#0b1220', strokeWidth: 0.5 },
                    hover: { fill: '#62d9c7' }
                },
                series: { regions: [{
                    values,
                    scale: ['#244c59', '#37c6b1', '#8ae8d7'],
                    normalizeFunction: 'polynomial'
                }] }
            });
        } else {
            geoMap.destroy();
            geoMap = null;
            renderGeoMap(countries);
        }
    }

    async function loadGeoAnalytics(days) {
        try {
            const geoResponse = await fetch(`/api/v1/admin/analytics/geography?days=${days}`, { headers: { 'Accept': 'application/json' } });
            if (geoResponse.ok) renderGeoMap((await geoResponse.json()).countries);
        } catch (error) {
            const status = document.getElementById('geo-policy-status');
            if (status) status.textContent = 'تعذر تحميل إعدادات الموقع الجغرافي.';
        }
    }

    function renderKPIs(kpis) {
        if (!kpis) return;
        document.getElementById('kpi-views').textContent = formatNumber(kpis.views);
        document.getElementById('kpi-visitors').textContent = formatNumber(kpis.unique_visitors);
        document.getElementById('kpi-bandwidth').textContent = kpis.bandwidth_formatted || '0 B';
        document.getElementById('kpi-top-quality').textContent = kpis.top_quality || '—';
    }

    function renderQualityDistribution(qualities) {
        const container = document.getElementById('quality-bars-container');
        if (!container) return;
        if (!qualities || qualities.length === 0) {
            container.innerHTML = '<div class="text-muted py-3 text-center">لا توجد بيانات جودات مسجلة بعد.</div>';
            return;
        }

        container.innerHTML = '';
        qualities.forEach(item => {
            const q = item.quality || 'Auto';
            const pct = item.percentage || 0;
            const cnt = item.count || 0;

            let fillClass = 'progress-fill-1080p';
            if (q.includes('4K') || q.includes('2160')) fillClass = 'progress-fill-4k';
            else if (q.includes('720')) fillClass = 'progress-fill-720p';
            else if (q.includes('480')) fillClass = 'progress-fill-480p';
            else if (q.toLowerCase() === 'auto') fillClass = 'progress-fill-auto';

            const row = document.createElement('div');
            row.className = 'quality-bar-row';
            row.innerHTML = `
                <div class="quality-bar-info">
                    <span class="quality-bar-label dir-ltr">${escapeHTML(q)}</span>
                    <span class="quality-bar-metrics">${escapeHTML(pct)}% (${formatNumber(cnt)})</span>
                </div>
                <div class="progress-track">
                    <div class="progress-fill ${fillClass}" style="width: ${escapeHTML(pct)}%"></div>
                </div>
            `;
            container.appendChild(row);
        });
    }

    function renderReferrers(referrers) {
        const container = document.getElementById('referrers-container');
        if (!container) return;
        if (!referrers || referrers.length === 0) {
            container.innerHTML = `
                <div class="text-muted py-3 text-center">
                    المشغل متاح للتضمين في المواقع المصرّح بها ضمن إعدادات المنصة.
                </div>
            `;
            return;
        }

        container.innerHTML = '';
        referrers.forEach(item => {
            const el = document.createElement('div');
            el.className = 'referrer-item';
            el.innerHTML = `
                <span class="referrer-domain dir-ltr">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
                    ${escapeHTML(item.domain)}
                </span>
                <div class="referrer-stats">
                    <span class="badge-views">${formatNumber(item.views)} مشاهدة</span>
                    <span class="text-muted dir-ltr">${escapeHTML(item.percentage)}%</span>
                </div>
            `;
            container.appendChild(el);
        });
    }

    function renderVideosTable(videos) {
        const tbody = document.getElementById('analytics-videos-tbody');
        if (!tbody) return;
        if (!videos || videos.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" class="text-center py-4 text-muted">لا توجد فيديوهات مسجلة بعد.</td></tr>';
            return;
        }

        tbody.innerHTML = '';
        videos.forEach(v => {
            const tr = document.createElement('tr');
            const epLabel = String(v.title || '').trim() || (v.episode_number ? `الحلقة ${v.episode_number}` : 'فيديو بدون اسم');
            const contextLabel = [v.series_name && v.series_name !== 'عام' ? v.series_name : '', v.episode_number ? `الحلقة ${v.episode_number}` : ''].filter(Boolean).join(' · ') || v.series_name || '—';
            tr.innerHTML = `
                <td><strong>${escapeHTML(epLabel)}</strong></td>
                <td><span class="text-muted">${escapeHTML(contextLabel)}</span></td>
                <td><span class="dir-ltr font-bold">${formatNumber(v.views)}</span></td>
                <td><span class="dir-ltr text-muted">${formatNumber(v.visitors)}</span></td>
                <td><span class="badge-views dir-ltr">${escapeHTML(v.top_quality || "—")}</span></td>
                <td><span class="dir-ltr">${escapeHTML(v.bandwidth_formatted)}</span></td>
            `;
            tbody.appendChild(tr);
        });

    }

    if (periodSelect) {
        periodSelect.addEventListener('change', loadAnalytics);
    }
    if (refreshBtn) {
        refreshBtn.addEventListener('click', loadAnalytics);
    }

    loadAnalytics();
});
