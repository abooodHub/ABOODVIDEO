document.addEventListener('DOMContentLoaded', () => {
  'use strict';
  const list = document.getElementById('detail-subs-list');
  if (!list) return;
  const get = id => document.getElementById(`vs-${id}`);
  const editor = get('editor'), removeDialog = get('delete-dialog');
  let videoId = null, editing = null, pendingDelete = null, busy = false;
  const csrf = () => {const match = document.cookie.match(new RegExp('(?:^|;\\s*)' + (document.body.dataset.csrfCookieName || 'aboodvideo_csrf').replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '=([^;]+)')); return match ? decodeURIComponent(match[1]) : '';};
  const endpoint = id => `/api/v1/admin/videos/${encodeURIComponent(videoId)}/subtitles${id ? '/' + encodeURIComponent(id) : ''}`;
  const node = (tag, text, className) => {const element = document.createElement(tag); element.textContent = text; if (className) element.className = className; return element;};
  const feedback = (text, error = false) => {get('feedback').textContent = text; get('feedback').classList.toggle('text-danger', error);};
  function syncBusy(value) {busy = value; [get('add'),get('refresh'),get('save'),get('delete-confirm')].forEach(button => button.disabled = value || !videoId); list.querySelectorAll('button').forEach(button => button.disabled = value);}
  function changed() {document.dispatchEvent(new CustomEvent('abv-video-subtitles-changed'));}
  async function load() {
    list.setAttribute('aria-busy','true');
    try {
      const response = await fetch(endpoint(), {credentials:'include',cache:'no-store'});
      if (!response.ok) throw new Error();
      const subtitles = await response.json();
      list.replaceChildren();
      if (!subtitles.length) list.append(node('p','لا توجد ترجمات لهذا الفيديو بعد.','empty-hint-sm'));
      subtitles.forEach(subtitle => {
        const row = node('article','','video-subtitle-row');
        const info = node('div','','video-subtitle-info');
        info.append(node('strong',subtitle.label),node('span',`${subtitle.language_name} (${subtitle.language_code}) · ${String(subtitle.format).toUpperCase()}`));
        const flags = [subtitle.is_playable ? 'قابلة للتشغيل' : 'أرشيف فقط', subtitle.is_default ? 'افتراضية' : '', subtitle.is_forced ? 'إجبارية' : ''].filter(Boolean);
        info.append(node('small',flags.join(' · ')));
        const actions = node('div','','video-subtitle-actions');
        const edit = node('button','تعديل','btn btn-secondary'); edit.type='button'; edit.addEventListener('click',() => openEditor(subtitle));
        const remove = node('button','حذف','btn btn-ghost text-danger'); remove.type='button'; remove.addEventListener('click',() => {if (busy) return; pendingDelete=subtitle; get('delete-label').textContent=subtitle.label; get('delete-feedback').textContent=''; removeDialog.showModal();});
        actions.append(edit,remove); row.append(info,actions); list.append(row);
      });
      const count = document.getElementById('spec-subtitles'); if (count) count.textContent = subtitles.length ? `${subtitles.length} مسار متوفر` : 'لا يوجد';
    } catch (_) {list.replaceChildren(node('p','تعذر تحميل الترجمات. استخدم تحديث لإعادة المحاولة.','text-danger'));}
    finally {list.setAttribute('aria-busy','false');}
  }
  function openEditor(subtitle = null) {
    if (!videoId || busy) return;
    editing=subtitle; get('form').reset(); get('editor-feedback').textContent='';
    get('editor-title').textContent=subtitle ? 'تعديل مسار الترجمة' : 'إضافة ترجمة لهذا الفيديو';
    get('label').value=subtitle?.label || ''; get('language-code').value=subtitle?.language_code || ''; get('language-name').value=subtitle?.language_name || '';
    get('default').checked=!!subtitle?.is_default; get('forced').checked=!!subtitle?.is_forced;
    get('language-code').disabled=get('language-name').disabled=!!subtitle;
    get('file-field').hidden=!!subtitle; get('file').required=!subtitle;
    editor.showModal();
  }
  get('add').addEventListener('click',() => openEditor());
  get('refresh').addEventListener('click',() => {if (!busy && videoId) load();});
  document.querySelectorAll('[data-close-editor]').forEach(button => button.addEventListener('click',() => {if (!busy) editor.close();}));
  get('delete-cancel').addEventListener('click',() => {if (!busy) removeDialog.close();});
  [editor,removeDialog].forEach(dialog => dialog.addEventListener('cancel',event => {if (busy) event.preventDefault();}));
  get('form').addEventListener('submit',async event => {
    event.preventDefault(); if (busy || !videoId) return;
    if (!get('label').value.trim() || !get('language-code').value.trim() || !get('language-name').value.trim()) {get('editor-feedback').textContent='أكمل التسمية واللغة.'; return;}
    const patch={label:get('label').value.trim(),is_default:get('default').checked,is_forced:get('forced').checked};
    const headers={'X-CSRF-Token':csrf()};
    let body;
    if (editing) {body=JSON.stringify(patch);headers['Content-Type']='application/json';}
    else {body=new FormData(); body.append('file',get('file').files[0]); Object.entries({...patch,language_code:get('language-code').value.trim(),language_name:get('language-name').value.trim()}).forEach(([key,value]) => body.append(key,String(value)));}
    syncBusy(true);get('editor-feedback').textContent='جارٍ الحفظ…';
    try {
      const response=await fetch(endpoint(editing?.id),{method:editing ? 'PATCH':'POST',credentials:'include',headers,body});
      if (!response.ok) throw new Error();
      editor.close();feedback('تم حفظ مسار الترجمة.');changed();await load();
    } catch (_) {get('editor-feedback').textContent='تعذر حفظ الترجمة. تحقق من الملف والصلاحيات ثم أعد المحاولة.';}
    finally {syncBusy(false);}
  });
  get('delete-confirm').addEventListener('click',async () => {
    if (!pendingDelete || busy || !videoId) return;
    syncBusy(true);get('delete-feedback').textContent='جارٍ الحذف…';
    try {
      const response=await fetch(endpoint(pendingDelete.id),{method:'DELETE',credentials:'include',headers:{'X-CSRF-Token':csrf()}});
      if (!response.ok) throw new Error();
      removeDialog.close();pendingDelete=null;feedback('تم حذف مسار الترجمة.');changed();await load();
    } catch (_) {get('delete-feedback').textContent='تعذر حذف المسار. أعد المحاولة.';}
    finally {syncBusy(false);}
  });
  document.addEventListener('abv-video-loaded',event => {videoId=String(event.detail.id);syncBusy(false);load();});
});
