window.AVi18n ||= {t: value => value, tpl: (parts, ...values) => parts.reduce((out, part, i) => out + part + (i < values.length ? values[i] : ''), '')};
/* ABOODVIDEO Admin Series index (Phase 9). */
document.addEventListener('DOMContentLoaded', () => {
    const grid = document.getElementById('series-grid');
    const loading = document.getElementById('series-loading');
    const feedback = document.getElementById('series-feedback');

    const fmtBytes = (bytes) => {
        if (!bytes) return '0 B';
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(1024));
        return (bytes / Math.pow(1024, i)).toFixed(i >= 3 ? 1 : 2) + ' ' + sizes[i];
    };
    const el = (tag, text, cls = []) => { const e = document.createElement(tag); if (text !== '') e.textContent = String(text); e.classList.add(...cls); return e; };

    async function load() {
        try {
            const res = await fetch('/api/v1/admin/videos/series', { credentials: 'include', cache: 'no-store' });
            if (!res.ok) throw new Error();
            const series = await res.json();
            loading.classList.add('hidden');
            if (!Array.isArray(series) || !series.length) {
                const empty = el('div', '', ['empty-state']);
                empty.append(
                    el('div', window.AVi18n.t('لا توجد مسلسلات بعد'), ['empty-state-title']),
                    el('p', window.AVi18n.t('ارفع فيديو واربطه باسم مسلسل ليظهر هنا منظماً بالمواسم.'), ['empty-state-desc']),
                );
                const link = el('a', window.AVi18n.t('رفع حلقة جديدة'), ['btn', 'btn-primary']);
                link.href = '/admin/upload';
                empty.appendChild(link);
                grid.replaceChildren(empty);
                grid.classList.remove('hidden');
                return;
            }
            grid.replaceChildren(...series.map(s => {
                const card = el('a', '', ['series-card']);
                card.href = '/admin/series/detail?s=' + encodeURIComponent(s.name);
                const head = el('div', '', ['series-card-header']);
                head.append(
                    el('div', s.name.trim().charAt(0), ['series-poster']),
                    el('h2', s.name),
                );
                const stats = el('div', '', ['series-stats']);
                stats.append(
                    (() => { const d = el('div', '', ['series-stat']); d.append(el('b', s.seasons), el('span', window.AVi18n.t('موسم'))); return d; })(),
                    (() => { const d = el('div', '', ['series-stat']); d.append(el('b', s.episodes), el('span', window.AVi18n.t('حلقة'))); return d; })(),
                    (() => { const d = el('div', '', ['series-stat']); d.append(el('b', fmtBytes(s.total_size_bytes)), el('span', window.AVi18n.t('الحجم'))); return d; })(),
                );
                const foot = el('div', '', ['series-foot']);
                foot.append(
                    el('span', window.AVi18n.tpl`${s.ready_count} جاهز`, ['badge', 'badge-quality']),
                    s.processing_count ? el('span', window.AVi18n.tpl`${s.processing_count} قيد المعالجة`, ['badge', 'status-processing']) : el('span', '', ['hidden']),
                );
                card.append(head, stats, foot);
                return card;
            }));
            grid.classList.remove('hidden');
        } catch (_) {
            loading.classList.add('hidden');
            feedback.textContent = window.AVi18n.t('تعذر تحميل المسلسلات — أعد المحاولة.');
            feedback.classList.remove('hidden');
            feedback.classList.add('is-error');
        }
    }

    load();
});
