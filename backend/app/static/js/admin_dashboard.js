window.AVi18n ||= {t: value => value, tpl: (parts, ...values) => parts.reduce((out, part, i) => out + part + (i < values.length ? values[i] : ''), '')};
/* Admin dashboard: compact, read-only view of the existing aggregate API. */
document.addEventListener('DOMContentLoaded', () => {
  'use strict';
  const transcodingEnabled = document.body.dataset.serverTranscoding !== 'false';
  const API_URL = '/api/v1/admin/dashboard';
  const LIVE_API_URL = '/api/v1/admin/dashboard/live';
  const REFRESH_MS = 15000;
  const LIVE_REFRESH_MS = 1500;
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const icon = (name) => `<svg class="av-icon" aria-hidden="true"><use href="/static/icons.svg#${name}"></use></svg>`;
  const fmtNum = (value) => value == null || Number.isNaN(Number(value)) ? '—' : Number(value).toLocaleString('en-US');
  const fmtRate = (value) => {
    if (value == null || !Number.isFinite(Number(value)) || Number(value) < 0) return '—';
    const units = ['B/s','KB/s','MB/s','GB/s']; let amount = Number(value), index = 0;
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index++; }
    return `${amount.toFixed(amount >= 100 || index === 0 ? 0 : 1)} ${units[index]}`;
  };
  const fmtBytes = (value) => {
    if (value == null || !Number.isFinite(Number(value)) || Number(value) < 0) return '—';
    const units = ['B','KB','MB','GB','TB']; let amount = Number(value), index = 0;
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index++; }
    return `${amount.toFixed(amount >= 100 || index === 0 ? 0 : 1)} ${units[index]}`;
  };
  const fmtDuration = (seconds) => {
    if (!seconds || !Number.isFinite(Number(seconds)) || seconds <= 0) return '';
    const total = Math.round(Number(seconds)), hours = Math.floor(total / 3600), minutes = Math.floor((total % 3600) / 60), remainder = total % 60;
    return hours ? `${hours}:${String(minutes).padStart(2,'0')}:${String(remainder).padStart(2,'0')}` : `${minutes}:${String(remainder).padStart(2,'0')}`;
  };
  const shortDate = (value) => {
    const date = new Date(value || '');
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleDateString(document.documentElement.lang || 'ar', {day:'numeric',month:'short'});
  };
  const relTime = (value) => {
    const date = new Date(value || '');
    if (Number.isNaN(date.getTime())) return '—';
    const minutes = Math.max(0, Math.floor((Date.now() - date.getTime()) / 60000));
    if (minutes < 1) return window.AVi18n.t('الآن');
    if (minutes < 60) return window.AVi18n.tpl`منذ ${minutes} د`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return window.AVi18n.tpl`منذ ${hours} س`;
    return shortDate(value);
  };
  const setText = (id, value) => { if ($(id)) $(id).textContent = value; };
  const statusLabel = (status) => ({READY:window.AVi18n.t('جاهز'),PROCESSING:window.AVi18n.t('قيد المعالجة'),PROBING:window.AVi18n.t('فحص الملف'),VERIFYING:window.AVi18n.t('التحقق'),QUEUED:window.AVi18n.t('في الانتظار'),UPLOADING:window.AVi18n.t('جارٍ الرفع'),UPLOADED:window.AVi18n.t('في الانتظار'),FAILED:window.AVi18n.t('فشل'),CANCELLED:window.AVi18n.t('ملغي')}[status] || status || '—');

  function animateMetric(id, value, formatter) {
    const el = $(id); if (!el) return;
    if (value == null || value === '') { el.textContent = '—'; return; }
    const target = Number(value);
    if (!Number.isFinite(target)) { el.textContent = '—'; return; }
    const previous = Number(el.dataset.metricValue || 0);
    el.dataset.metricValue = String(target);
    const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
    if (reducedMotion || previous === target) { el.textContent = formatter(target); return; }
    const start = performance.now(), duration = 620;
    const tick = (now) => {
      const progress = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      el.textContent = formatter(previous + (target - previous) * eased);
      if (progress < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    el.classList.remove('metric-updated');
    requestAnimationFrame(() => el.classList.add('metric-updated'));
  }

  function renderSummary(data) {
    const videos = data.videos || {}, jobs = data.jobs || {}, storage = data.storage || {};
    animateMetric('kpiVideosVal', videos.total, fmtNum);
    setText('kpiVideosSub', window.AVi18n.tpl`${fmtNum(videos.ready)} جاهز · ${fmtNum(videos.processing)} قيد العمل`);
    animateMetric('kpiStorageVal', storage.used_bytes, fmtBytes);
    const free = storage.free_bytes;
    const total = storage.total_bytes;
    setText('kpiStorageSub', free == null ? window.AVi18n.t('المساحة المتاحة غير متوفرة') : window.AVi18n.tpl`${fmtBytes(free)} متاح${total == null ? '' : window.AVi18n.tpl` من ${fmtBytes(total)}`}`);
    animateMetric('kpiProcessingVal', jobs.active_count, fmtNum);
    setText('kpiProcessingSub', window.AVi18n.tpl`${fmtNum(jobs.queued_count)} في الانتظار`);
    if ($('kpiFailedVal')) setText('kpiFailedVal', fmtNum(videos.failed));
    if ($('kpiFailedSub')) setText('kpiFailedSub', window.AVi18n.tpl`${fmtNum(videos.failed)} فيديو بحالة فشل`);
    ['kpiVideosVal','kpiStorageVal','kpiProcessingVal','kpiFailedVal'].forEach(id => $(id)?.classList.remove('is-loading'));
    document.querySelectorAll('[data-processing-badge]').forEach(badge => {
      const count = Number(jobs.active_count || 0);
      badge.textContent = String(count);
      badge.classList.toggle('hidden', count === 0);
    });
  }

  function renderServices(data) {
    const box = $('dashServices'); if (!box) return;
    const services = data.services || {};
    const entries = [
      [window.AVi18n.t('الواجهة'), services.api],
      [window.AVi18n.t('قاعدة البيانات'), services.database],
      ['Redis', services.redis],
      [window.AVi18n.t('التخزين'), services.storage || data.storage?.status],
      ...(transcodingEnabled ? [[window.AVi18n.t('عامل المعالجة'), services.worker]] : []),
    ];
    const statusInfo = (value) => {
      const raw = value && typeof value === 'object' ? value.status : value;
      const key = String(raw ?? '').toLowerCase();
      if (value === true || ['healthy','online','active','ready','up'].includes(key)) return [window.AVi18n.t('سليم'),'ok'];
      if (value === false || ['offline','down','error','unhealthy','failed','stale'].includes(key)) return [window.AVi18n.t('متعطل'),'fail'];
      if (['degraded','warning','unknown',''].includes(key)) return [window.AVi18n.t('غير مؤكد'),'warn'];
      return [String(raw), 'warn'];
    };
    box.innerHTML = entries.map(([label, value]) => {
      const [status, tone] = statusInfo(value);
      return `<span class="dash-service-chip status-${tone}"><i aria-hidden="true"></i><span>${esc(label)}</span><strong>${esc(status)}</strong></span>`;
    }).join('');

    setText('dashServicesUpdated', data.generated_at ? window.AVi18n.tpl`آخر تحديث ${relTime(data.generated_at)}` : window.AVi18n.t('قراءة مباشرة من الخادم'));
  }

  const diagnostics = $('system-diagnostics');
  let diagnosticsLoading = false;
  const healthLabel = (value) => ({ healthy:window.AVi18n.t('سليم'), unhealthy:window.AVi18n.t('متعطل'), degraded:window.AVi18n.t('يحتاج مراجعة'), ONLINE:window.AVi18n.t('يعمل'), online:window.AVi18n.t('يعمل'), stopped:window.AVi18n.t('متوقف'), OFFLINE:window.AVi18n.t('متوقف'), STALE:window.AVi18n.t('النبض متأخر'), error:window.AVi18n.t('تعذر الفحص') }[value] || value || window.AVi18n.t('غير متاح'));
  const secondsLabel = (value) => value == null ? window.AVi18n.t('غير متاح') : window.AVi18n.tpl`${fmtNum(value)} ثانية`;
  async function loadDiagnostics() {
    if (!diagnostics?.open || document.hidden || diagnosticsLoading) return;
    diagnosticsLoading = true;
    const refresh = $('dashDiagnosticsRefresh'), message = $('dashDiagnosticsStatus');
    refresh.disabled = true;
    diagnostics.setAttribute('aria-busy', 'true');
    message.textContent = window.AVi18n.t('جارٍ فحص تفاصيل التشغيل…');
    message.classList.remove('is-error');
    try {
      const responses = await Promise.allSettled((transcodingEnabled ? ['readiness','worker','reconciler'] : ['readiness']).map(async name => {
        const response = await fetch(`/api/v1/health/${name}`, { credentials:'include', cache:'no-store' });
        // A 503 body carries the stopped/degraded state needed for diagnostics.
        if (!response.ok && response.status !== 503) throw new Error(`HTTP ${response.status}`);
        return { okay:response.ok, data:await response.json() };
      }));
      const data = responses.map(result => result.status === 'fulfilled' ? result.value.data : {});
      const [overall, worker = {}, reconciler = {}] = data;
      const facts = [
        [window.AVi18n.t('صحة التطبيق'), healthLabel(overall.status)], [window.AVi18n.t('إصدار التطبيق'), overall.version || window.AVi18n.t('غير متاح')],
        [window.AVi18n.t('قاعدة البيانات'), healthLabel(overall.services?.database)], ['Redis', healthLabel(overall.services?.redis)],
        [window.AVi18n.t('التخزين'), healthLabel(overall.services?.storage)], [window.AVi18n.t('عامل المعالجة'), healthLabel(worker.worker_status || worker.status)],
        [window.AVi18n.t('مهام في الطابور'), fmtNum(worker.queue_depth)], [window.AVi18n.t('آخر نبض للعامل'), secondsLabel(worker.last_seen_seconds_ago)],
        [window.AVi18n.t('خدمة الصيانة'), healthLabel(reconciler.status)], [window.AVi18n.t('آخر نبض للصيانة'), secondsLabel(reconciler.last_seen_seconds_ago)],
      ];
      if (!transcodingEnabled) facts.splice(5, facts.length - 5, [window.AVi18n.t('نمط الرفع'), window.AVi18n.t('ملفات جاهزة — التحويل معطّل')]);
      $('dashDiagnosticsFacts').innerHTML = facts.map(([name,value]) => `<div class="dash-diagnostics-fact"><dt>${esc(name)}</dt><dd>${esc(value)}</dd></div>`).join('');
      const okay = responses.every(result => result.status === 'fulfilled' && result.value.okay)
        && overall.status === 'healthy' && (!transcodingEnabled || (worker.worker_status === 'ONLINE' && reconciler.status === 'online'));
      message.textContent = okay ? window.AVi18n.t('الخدمات تعمل — تم تحديث الفحص الآن.') : window.AVi18n.t('توجد خدمة متوقفة أو تعذر فحصها. راجع التفاصيل أدناه.');
      message.classList.toggle('is-error', !okay);
    } catch (_) {
      message.textContent = window.AVi18n.t('تعذر تحميل التشخيص. أعد تحديث الفحص.');
      message.classList.add('is-error');
    } finally {
      diagnosticsLoading = false;
      refresh.disabled = false;
      diagnostics.removeAttribute('aria-busy');
    }
  }
  if (diagnostics) {
    diagnostics.addEventListener('toggle', () => { if (diagnostics.open) loadDiagnostics(); });
    $('dashDiagnosticsRefresh').addEventListener('click', loadDiagnostics);
    const openDiagnosticsFromLink = () => {
      if (window.location.hash !== '#system-diagnostics') return;
      diagnostics.open = true;
      requestAnimationFrame(() => diagnostics.scrollIntoView({ block:'start' }));
    };
    window.addEventListener('hashchange', openDiagnosticsFromLink);
    openDiagnosticsFromLink();
    window.setInterval(loadDiagnostics, REFRESH_MS);
  }

  function renderAttention(data) {
    const box = $('dashAttention'); if (!box) return;
    const entries = [];
    const failed = Number(data.videos?.failed || 0);
    if (failed) entries.push(window.AVi18n.tpl`<a href="/admin/videos"><span>محتوى تعذرت معالجته</span><strong>${esc(fmtNum(failed))}</strong></a>`);
    const queued = Number(data.jobs?.queued_count || 0);
    if (queued) entries.push(window.AVi18n.tpl`<a href="/admin/processing"><span>مهام في الانتظار</span><strong>${esc(fmtNum(queued))}</strong></a>`);
    if (Number(data.storage?.used_percentage || 0) >= 85) entries.push(window.AVi18n.t('<a href="/admin/storage"><span>راجع المساحة المتاحة</span><span>التخزين ←</span></a>'));
    const degraded = Object.values(data.services || {}).some(value => {
      const status = String(value && typeof value === 'object' ? value.status : value).toLowerCase();
      return value === false || ['down','error','unhealthy','degraded','offline','stale'].includes(status);
    });
    if (degraded) entries.push(window.AVi18n.t('<p>إحدى الخدمات تحتاج مراجعة؛ حالتها ظاهرة في ملخص الخدمات أعلاه.</p>'));
    box.innerHTML = entries.length ? entries.join('') : window.AVi18n.t('<p>لا توجد تنبيهات في ملخص المكتبة الحالي.</p>');
  }

  function renderVideos(data) {
    const box = $('dashLatestVideos'); if (!box) return;
    const items = Array.isArray(data.latest_videos) ? data.latest_videos : [];
    if (!items.length) {
      box.innerHTML = window.AVi18n.tpl`<div class="dash-empty">${icon('video')}<span class="dash-empty-title">لا توجد فيديوهات</span><span class="dash-empty-desc">ارفع فيديو ليظهر هنا.</span></div>`;
      return;
    }
    box.innerHTML = items.slice(0, 6).map(video => {
      const id = encodeURIComponent(String(video.id || ''));
      const href = `/admin/videos/detail?v=${id}`;
      const details = [video.quality_summary || video.quality, fmtDuration(video.duration_seconds), video.size_bytes != null ? fmtBytes(video.size_bytes) : '', shortDate(video.created_at)].filter(Boolean);
      return window.AVi18n.tpl`<article class="dash-video-row">
        <a class="dash-video-thumb" href="${esc(href)}" data-poster="${esc(id)}" aria-label="فتح ${esc(video.title)}">${icon('video')}</a>
        <div class="dash-video-info"><a class="dash-video-title" dir="auto" href="${esc(href)}" title="${esc(video.title)}"><bdi>${esc(video.title)}</bdi></a><div class="dash-video-meta">${details.map((item,index)=>`${index?'<span aria-hidden="true">·</span>':''}<span>${esc(item)}</span>`).join('')}</div></div>
        <span class="dash-video-state">${esc(statusLabel(video.status))}</span>
      </article>`;
    }).join('');
    box.querySelectorAll('[data-poster]').forEach(slot => {
      const image = new Image(); image.alt = ''; image.loading = 'lazy'; image.decoding = 'async';
      image.onload = () => slot.replaceChildren(image);
      image.onerror = () => {};
      image.src = `/api/v1/admin/videos/${slot.dataset.poster}/poster`;
    });
  }

  function renderQueue(data) {
    const box = $('dashQueue'); if (!box) return;
    const jobs = Array.isArray(data.jobs?.items) ? data.jobs.items : [];
    if (!jobs.length) {
      box.innerHTML = window.AVi18n.tpl`<div class="dash-empty">${icon('clock')}<span class="dash-empty-title">لا توجد مهام معالجة</span></div>`;
      return;
    }
    box.innerHTML = jobs.slice(0, 4).map(job => {
      const percent = Math.max(0, Math.min(100, Math.round(Number(job.progress_percent || 0))));
      const meta = [job.current_rendition, job.ffmpeg_fps ? `${Math.round(job.ffmpeg_fps)} FPS` : '', job.ffmpeg_speed ? `${Number(job.ffmpeg_speed).toFixed(2)}x` : '', job.eta_seconds ? window.AVi18n.tpl`متبقي ${fmtDuration(job.eta_seconds)}` : ''].filter(Boolean);
      return window.AVi18n.tpl`<article class="dash-queue-item"><div class="dash-queue-top"><span class="dash-queue-icon">${icon('processing')}</span><div class="dash-queue-info"><div class="dash-queue-title" dir="auto"><bdi>${esc(job.video_title || window.AVi18n.t('مهمة معالجة'))}</bdi></div><div class="dash-queue-stage"><span>${esc(statusLabel(job.status))}</span><span class="queue-pct" dir="ltr">${percent}%</span></div></div></div><div class="dash-queue-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}" aria-label="تقدم ${esc(job.video_title || window.AVi18n.t('المهمة'))}"><span style="--progress:${percent}%"></span></div>${meta.length ? `<div class="dash-queue-meta">${meta.map(v=>`<span dir="auto">${esc(v)}</span>`).join(' · ')}</div>` : ''}</article>`;
    }).join('');
  }

  function renderActivity(data) {
    const box = $('dashActivity'); if (!box) return;
    const items = Array.isArray(data.activity) ? data.activity : [];
    if (!items.length) {
      box.innerHTML = window.AVi18n.tpl`<li class="dash-empty">${icon('clock')}<span class="dash-empty-title">لا يوجد نشاط حديث</span></li>`;
      return;
    }
    const icons = {upload:'upload',processing:'check',failed:'alert',share:'shares',delete:'trash',edit:'settings',key:'key',subtitle:'video'};
    box.innerHTML = items.slice(0, 5).map(item => `<li class="dash-feed-item"><span class="dash-feed-icon">${icon(icons[item.type] || 'processing')}</span><div class="dash-feed-texts"><span class="dash-feed-text">${esc(item.text)}</span><time class="dash-feed-time" datetime="${esc(item.at || '')}">${esc(relTime(item.at))}</time></div></li>`).join('');
  }

  function renderNotifications(data) {
    const list = $('dashNotifList'), badge = $('dashNotifBadge'); if (!list) return;
    const items = Array.isArray(data.activity) ? data.activity.slice(0, 5) : [];
    const cutoff = Date.now() - 86400000;
    const fresh = (data.activity || []).filter(item => item.at && new Date(item.at).getTime() >= cutoff).length;
    if (badge) { badge.textContent = fresh > 9 ? '9+' : String(fresh); badge.classList.toggle('hidden', !fresh); }
    list.innerHTML = items.length ? items.map(item => `<div class="dash-notif-item" role="menuitem">${icon('processing')}<div class="dash-feed-texts"><span class="dash-feed-text">${esc(item.text)}</span><time class="dash-feed-time">${esc(relTime(item.at))}</time></div></div>`).join('') : window.AVi18n.t('<div class="dash-notif-empty">لا إشعارات جديدة</div>');
  }

  function renderError() {
    const message = window.AVi18n.tpl`<div class="dash-error" role="alert">${icon('alert')}<span>تعذر تحميل البيانات. ستتم المحاولة مجدداً تلقائياً.</span></div>`;
    ['dashLatestVideos','dashQueue','dashActivity','dashAttention'].forEach(id => { if ($(id)) $(id).innerHTML = message; });
    ['kpiVideosVal','kpiStorageVal','kpiProcessingVal','kpiFailedVal'].forEach(id => { setText(id,'—'); $(id)?.classList.remove('is-loading'); });
    ['kpiVideosSub','kpiStorageSub','kpiProcessingSub','kpiFailedSub'].forEach(id => setText(id,window.AVi18n.t('غير متاح مؤقتاً')));
    if ($('dashServices')) $('dashServices').innerHTML = window.AVi18n.t('<span class="dash-service-error">تعذر قراءة حالة الخدمات</span>');
  }

  const notifButton = $('dashNotifBtn'), notifMenu = $('dashNotifMenu');
  if (notifButton && notifMenu) {
    const closeMenu = () => { notifMenu.classList.add('hidden'); notifButton.setAttribute('aria-expanded','false'); };
    notifButton.addEventListener('click', event => { event.stopPropagation(); const open = notifMenu.classList.toggle('hidden'); notifButton.setAttribute('aria-expanded',String(!open)); });
    document.addEventListener('click', event => { if (!notifMenu.classList.contains('hidden') && !event.target.closest('#dashNotif')) closeMenu(); });
    document.addEventListener('keydown', event => { if (event.key === 'Escape' && !notifMenu.classList.contains('hidden')) { closeMenu(); notifButton.focus(); } });
  }

  const tabs = [
    { button: $('dashTabRecent'), panel: $('dashRecentPanel') },
    { button: $('dashTabQueue'), panel: $('dashQueuePanel') },
  ].filter(item => item.button && item.panel);
  function selectTab(index, focus = false) {
    tabs.forEach((item, i) => {
      const active = i === index;
      item.button.classList.toggle('is-active', active);
      item.button.setAttribute('aria-selected', String(active));
      item.button.tabIndex = active ? 0 : -1;
      item.panel.hidden = !active;
    });
    if (focus) tabs[index]?.button.focus();
  }
  tabs.forEach((item, index) => {
    item.button.addEventListener('click', () => selectTab(index));
    item.button.addEventListener('keydown', event => {
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : event.key === 'ArrowRight' ? (index + 1) % tabs.length : (index - 1 + tabs.length) % tabs.length;
      selectTab(next, true);
    });
  });

  let previousNetworkSample = null;
  async function loadLiveNetwork() {
    if (document.hidden) return;
    try {
      const response = await fetch(LIVE_API_URL, { credentials: 'include', cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const sample = await response.json();
      if (!sample.available) throw new Error('Network counters unavailable');
      const now = performance.now();
      if (previousNetworkSample) {
        const seconds = Math.max(0.25, (now - previousNetworkSample.at) / 1000);
        const upload = Math.max(0, Number(sample.received_bytes) - previousNetworkSample.received) / seconds;
        const download = Math.max(0, Number(sample.sent_bytes) - previousNetworkSample.sent) / seconds;
        setText('kpiNetworkUploadVal', fmtRate(upload));
        setText('kpiNetworkDownloadVal', fmtRate(download));
        [$('kpiNetworkUploadVal'), $('kpiNetworkDownloadVal')].forEach(el => {
          if (!el) return;
          el.classList.remove('metric-updated');
          requestAnimationFrame(() => el.classList.add('metric-updated'));
        });
        setText('kpiNetworkNote', window.AVi18n.t('مباشر · شبكة خادم المنصة'));
        $('kpiNetworkNote')?.classList.remove('is-error');
      }
      previousNetworkSample = { received: Number(sample.received_bytes), sent: Number(sample.sent_bytes), at: now };
    } catch (_) {
      setText('kpiNetworkNote', window.AVi18n.t('قياس السرعة غير متاح'));
      $('kpiNetworkNote')?.classList.add('is-error');
    }
  }

  async function load() {
    if (document.hidden) return;
    try {
      const response = await fetch(API_URL,{credentials:'include',cache:'no-store'});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      renderSummary(data); renderServices(data); renderAttention(data); renderVideos(data); renderQueue(data); renderActivity(data); renderNotifications(data);
    } catch (error) {
      renderError(); console.warn('[Dashboard] refresh failed:', error);
    }
  }
  load(); window.setInterval(load, REFRESH_MS);
  loadLiveNetwork(); window.setInterval(loadLiveNetwork, LIVE_REFRESH_MS);
});
