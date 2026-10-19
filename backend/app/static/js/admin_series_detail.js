/* ABOODVIDEO Admin Series Detail (Phase 9). */
document.addEventListener('DOMContentLoaded', () => {
    const name = new URLSearchParams(window.location.search).get('s');
    const loading = document.getElementById('sd-loading');
    const feedback = document.getElementById('sd-feedback');

    const statusLabels = { READY: 'جاهز', PROCESSING: 'قيد المعالجة', FAILED: 'فشل', CANCELLED: 'ملغى', UPLOADING: 'جارٍ الرفع', UPLOADED: 'تم الرفع', QUEUED: 'بالانتظار', PROBING: 'فحص المصدر', VERIFYING: 'تحقق' };
    const fmtBytes = (bytes) => {
        if (!bytes) return '0 B';
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(1024));
        return (bytes / Math.pow(1024, i)).toFixed(i >= 3 ? 1 : 2) + ' ' + sizes[i];
    };
    const el = (tag, text, cls = []) => { const e = document.createElement(tag); if (text !== '') e.textContent = String(text); e.classList.add(...cls); return e; };

    async function load() {
        if (!name) {
            loading.textContent = 'لم يتم تحديد مسلسل.';
            return;
        }
        try {
            const [seriesRes, videosRes] = await Promise.all([
                fetch('/api/v1/admin/videos/series', { credentials: 'include', cache: 'no-store' }),
                fetch(`/api/v1/admin/videos?series_name=${encodeURIComponent(name)}&limit=100&sort=date&direction=desc`, { credentials: 'include', cache: 'no-store' }),
            ]);
            if (!seriesRes.ok || !videosRes.ok) throw new Error();
            const allSeries = await seriesRes.json();
            const info = (Array.isArray(allSeries) ? allSeries : []).find(s => s.name === name);
            const videos = (await videosRes.json()).items;

            document.title = `${name} | ABOODVIDEO`;
            loading.classList.add('hidden');
            document.getElementById('sd-detail').classList.remove('hidden');
            document.getElementById('sd-title').textContent = name;
            document.getElementById('sd-poster').textContent = name.trim().charAt(0);

            const ready = videos.filter(v => v.status === 'READY').length;
            const processing = videos.filter(v => ['PROCESSING', 'QUEUED', 'PROBING', 'VERIFYING'].includes(v.status)).length;
            const seasonNums = [...new Set(videos.map(v => v.season_number).filter(Boolean))].sort((a, b) => a - b);
            document.getElementById('sd-subtitle').textContent = `مسلسل · ${seasonNums.length || (videos.length ? 1 : 0)} موسم · ${videos.length} حلقة`;
            const badges = document.getElementById('sd-badges');
            badges.replaceChildren(
                el('span', `${ready} جاهز`, ['badge', 'badge-quality']),
                el('span', `${processing} قيد المعالجة`, ['badge', 'status-processing']),
                info ? el('span', `الحجم الإجمالي ${fmtBytes(info.total_size_bytes)}`, ['badge', 'badge-muted']) : el('span', '', ['hidden']),
            );
            document.getElementById('sd-upload-link').href = `/admin/upload?series=${encodeURIComponent(name)}&season=${seasonNums.length ? seasonNums[seasonNums.length - 1] : 1}&episode=${videos.length + 1}`;

            renderSeasons(videos, seasonNums);
        } catch (_) {
            loading.textContent = 'تعذر تحميل بيانات المسلسل.';
        }
    }

    function parseNaturalEpisode(v) {
        if (v.episode_number != null) return parseFloat(v.episode_number) || 0;
        const m = (v.title || '').match(/(?:الحلقة|الحلقه|EP|Episode|E)[\s_.:-]*([0-9]+(?:\.[0-9]+)?)/i);
        if (m) return parseFloat(m[1]) || 0;
        const num = (v.title || '').match(/\b([0-9]+(?:\.[0-9]+)?)\b/);
        if (num) return parseFloat(num[1]) || 0;
        return 0;
    }

    function renderSeasons(videos, seasonNums) {
        const container = document.getElementById('sd-seasons');
        container.replaceChildren();

        const groups = seasonNums.length
            ? seasonNums.map(n => ({ num: n, items: videos.filter(v => v.season_number === n).sort((a, b) => parseNaturalEpisode(a) - parseNaturalEpisode(b)) }))
            : [{ num: null, items: videos.sort((a, b) => parseNaturalEpisode(a) - parseNaturalEpisode(b)) }];

        if (!videos.length) {
            container.appendChild(el('div', 'لا توجد حلقات مرفوعة لهذا المسلسل بعد.', ['empty-state']));
            return;
        }

        for (const group of groups) {
            const block = el('section', '', ['season-block']);
            const header = el('div', '', ['season-header']);
            header.append(
                el('h2', group.num ? `الموسم ${group.num}` : 'حلقات بدون موسم', ['season-title']),
                el('span', `${group.items.length} حلقة`, ['badge', 'badge-muted']),
            );
            const tableCard = el('div', '', ['table-card']);
            const table = document.createElement('table');
            const thead = document.createElement('thead');
            const headRow = document.createElement('tr');
            for (const label of ['الحلقة', 'العنوان', 'الحالة', 'الجودة', 'الترجمة', 'الحجم', 'الإجراءات']) {
                headRow.appendChild(el('th', label));
            }
            thead.appendChild(headRow);
            const tbody = document.createElement('tbody');
            tbody.replaceChildren(...group.items.map(v => {
                const tr = document.createElement('tr');
                const quality = v.available_renditions && v.available_renditions.length
                    ? v.available_renditions[v.available_renditions.length - 1].quality : null;
                const detail = el('a', 'تفاصيل', ['btn', 'btn-sm']);
                detail.href = `/admin/videos/detail?v=${encodeURIComponent(String(v.id))}`;
                tr.append(
                    el('td', v.episode_number ? String(v.episode_number) : '—'),
                    el('td', v.title),
                    el('td', '', []),
                    el('td', quality ? (quality === '2160p' ? '4K' : quality) : '—'),
                    el('td', v.subtitle_count ? `${v.subtitle_count} مسار` : 'بدون'),
                    el('td', fmtBytes(v.source_size_bytes)),
                    (() => { const td = el('td', ''); td.appendChild(detail); return td; })(),
                );
                const statusTd = tr.children[2];
                statusTd.appendChild(el('span', statusLabels[v.status] || v.status, ['badge', `status-${String(v.status).toLowerCase()}`]));
                return tr;
            }));
            table.append(thead, tbody);
            tableCard.appendChild(table);
            block.append(header, tableCard);
            container.appendChild(block);
        }
    }

    load();
});
