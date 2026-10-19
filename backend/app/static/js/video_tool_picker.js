document.addEventListener('DOMContentLoaded', async () => {
  'use strict';
  const root = document.getElementById('video-tool-picker');
  if (!root) return;
  const tool = root.dataset.tool === 'subtitles' ? 'subtitles' : 'inspection';
  const list = document.getElementById('video-picker-list');
  const feedback = document.getElementById('video-picker-feedback');
  const previous = document.getElementById('video-picker-prev');
  const next = document.getElementById('video-picker-next');
  const query = document.getElementById('video-picker-query');
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const href = id => `/admin/videos/detail?v=${encodeURIComponent(id)}#${tool}`;
  let page = 1, requestNumber = 0;
  async function load() {
    const request = ++requestNumber;
    list.setAttribute('aria-busy', 'true');
    previous.disabled = next.disabled = true;
    feedback.textContent = 'جارٍ تحميل الفيديوهات…';
    try {
      const params = new URLSearchParams({page: String(page), limit: '20', q: query.value.trim()});
      const response = await fetch(`/api/v1/admin/videos?${params}`, {credentials:'include', cache:'no-store'});
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (request !== requestNumber) return;
      list.replaceChildren();
      for (const video of data.items || []) {
        if (video.trashed_at) continue;
        const link = document.createElement('a');
        link.className = 'video-picker-item'; link.href = href(String(video.id));
        const title = document.createElement('strong'); title.textContent = video.title || video.source_filename || 'فيديو';
        const action = document.createElement('span'); action.textContent = tool === 'subtitles' ? 'إدارة الترجمات ←' : 'فحص التشغيل ←';
        link.append(title, action); list.append(link);
      }
      feedback.textContent = list.children.length ? 'اختر فيديو للانتقال إلى القسم المطلوب داخله.' : 'لا توجد فيديوهات مطابقة. جرّب بحثًا آخر أو افتح المكتبة.';
      const total = Number(data.total) || 0;
      document.getElementById('video-picker-page').textContent = `صفحة ${page} من ${Math.max(1, Math.ceil(total / 20))}`;
      previous.disabled = page <= 1; next.disabled = page * 20 >= total;
    } catch (_) {
      if (request === requestNumber) { list.replaceChildren(); feedback.textContent = 'تعذر تحميل الفيديوهات. تحقق من تسجيل الدخول ثم أعد البحث.'; }
    } finally { if (request === requestNumber) list.setAttribute('aria-busy', 'false'); }
  }
  document.getElementById('video-picker-search').addEventListener('submit', event => {event.preventDefault(); page = 1; load();});
  previous.addEventListener('click', () => {if (page > 1) {page--; load();}});
  next.addEventListener('click', () => {page++; load();});
  const params = new URLSearchParams(location.search);
  const target = (params.get('v') || params.get('video_id') || params.get('id') || params.get('s') || '').trim();
  if (target && uuid.test(target)) {location.replace(href(target)); return;}
  // Old inspector bookmarks also accepted share IDs. Resolve them with the existing protected API.
  if (target && target.length <= 128) {
    feedback.textContent = 'جارٍ تحديد الفيديو المرتبط بالرابط…';
    try {
      const csrf = document.cookie.match(new RegExp('(?:^|;\\s*)' + (document.body.dataset.csrfCookieName || 'aboodvideo_csrf').replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '=([^;]+)'));
      const response = await fetch('/api/v1/admin/inspector/check', {method:'POST',credentials:'include',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf ? decodeURIComponent(csrf[1]) : ''},body:JSON.stringify({identifier:target,probe_network:false})});
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (data.video?.id && uuid.test(data.video.id)) {location.replace(href(data.video.id)); return;}
    } catch (_) { /* Keep the authorized video selector available for expired links. */ }
  }
  load();
});
