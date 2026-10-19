window.AVi18n ||= {t: value => value, tpl: (parts, ...values) => parts.reduce((out, part, i) => out + part + (i < values.length ? values[i] : ''), '')};
(() => {
  const byId = (id) => document.getElementById(id);
  const fileInput = byId('ready-package-file');
  if (!fileInput) return;
  const folderInput = byId('ready-folder-file');
  const startButton = byId('ready-package-start');
  const pauseButton = byId('ready-package-pause');
  const cancelButton = byId('ready-package-cancel');
  const statusNode = byId('ready-package-status');
  const progressNode = byId('ready-package-progress');
  const nameNode = byId('ready-package-name');
  const queueNode = byId('ready-upload-queue');
  const resultNode = byId('ready-package-result');
  const folderStatus = byId('ready-folder-status');
  const CHUNK = 8 * 1024 * 1024;
  const MAX_FILES = 2000;
  const MAX_BYTES = 2 * 1024 * 1024 * 1024;
  let queue = [];
  let active = false;
  let paused = false;
  let stopRequested = false;
  let request = null;
  let currentItem = null;
  let queueCursor = 0;

  const csrf = () => document.cookie.split(';').map(x => x.trim()).find(x => x.startsWith((document.body.dataset.csrfCookieName || 'aboodvideo_csrf') + '='))?.split('=').slice(1).join('=') || '';
  const setStatus = value => { statusNode.textContent = value; };
  const api = async (url, {method='GET', body, headers={}}={}) => {
    const response = await fetch(url, {method, body, credentials:'same-origin', headers:{'X-CSRF-Token':csrf(), ...headers}});
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) { const error=new Error(window.AVi18n.t(payload.detail) || `HTTP ${response.status}`);error.status=response.status;throw error; }
    return payload;
  };
  const safeKey = file => `abv-ready-import:${file.name}:${file.size}:${file.lastModified}`;
  const escaped = value => String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const formatBytes = bytes => `${(bytes / 1024 / 1024).toFixed(1)} MB`;

  function drawQueue() {
    queueNode.replaceChildren();
    for (const [index, item] of queue.entries()) {
      const row = document.createElement('div');
      row.className = 'ready-upload-row';
      row.dataset.index = String(index);
      const label = document.createElement('span');
      label.className = 'ready-upload-row-label';
      label.textContent = `${item.originalName} · ${formatBytes(item.file.size)}${item.fileCount ? window.AVi18n.tpl` · ${item.fileCount} ملف` : ''}`;
      const state = document.createElement('span');
      state.className = 'ready-upload-row-state';
      state.textContent = item.status || window.AVi18n.t('انتظار');
      const retry = document.createElement('button');
      retry.type = 'button'; retry.className = 'btn btn-secondary ready-upload-retry';
      retry.textContent = window.AVi18n.t('إعادة المحاولة'); retry.hidden = true;
      retry.addEventListener('click', () => retryItem(item));
      row.append(label, state, retry);
      queueNode.append(row);
      item.row = row; item.stateNode = state; item.retryButton = retry;
    }
    startButton.disabled = active || queue.length === 0;
    startButton.textContent = queue.length > 1 ? window.AVi18n.tpl`بدء رفع ${queue.length} فيديوهات` : window.AVi18n.t('بدء / استئناف الرفع');
    nameNode.textContent = queue.length ? window.AVi18n.tpl`عدد العناصر: ${queue.length} · الحجم الإجمالي: ${formatBytes(queue.reduce((sum, item) => sum + item.file.size, 0))}` : '';
  }

  function updateProgress(item, sent, total) {
    const done = queue.reduce((sum, candidate) => sum + (candidate.status === 'READY' ? candidate.file.size : 0), 0);
    const bytes = Math.min(total, sent);
    const overall = Math.min(99, (done + bytes) / Math.max(1, queue.reduce((sum, candidate) => sum + candidate.file.size, 0)) * 100);
    progressNode.value = overall;
    const rowState = item.stateNode;
    if (rowState) rowState.textContent = window.AVi18n.tpl`${Math.round(bytes / Math.max(1,total) * 100)}% · ${bytes.toLocaleString()} / ${total.toLocaleString()} بايت`;
    const speed = Math.max(0, (sent - item.lastSent) / Math.max(0.1, (Date.now() - item.lastTick) / 1000));
    item.lastSent = sent; item.lastTick = Date.now();
    const remaining = speed > 0 ? Math.ceil((total - sent) / speed) : null;
    setStatus(window.AVi18n.tpl`${Math.round(overall)}% إجمالي · ${formatBytes(done + bytes)} من ${formatBytes(queue.reduce((sum, candidate) => sum + candidate.file.size, 0))} · ${speed ? `${(speed / 1024 / 1024).toFixed(2)} MB/s` : window.AVi18n.t('جارٍ حساب السرعة')} · ${remaining === null ? window.AVi18n.t('جارٍ حساب الوقت المتبقي') : window.AVi18n.tpl`متبقي ${Math.floor(remaining / 60)}د ${remaining % 60}ث`}`);
  }

  function sendChunk(id, offset, blob, item, startedAt) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest(); request = xhr;
      xhr.open('PUT', `/api/v1/admin/videos/direct-import/resumable/${id}?offset=${offset}`);
      xhr.withCredentials = true;
      xhr.setRequestHeader('X-CSRF-Token', csrf());
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.upload.onprogress = event => {
        if (event.lengthComputable) updateProgress(item, offset + event.loaded, item.file.size);
      };
      xhr.onload = () => { request = null; if (xhr.status >= 200 && xhr.status < 300) resolve(JSON.parse(xhr.responseText)); else { let message=`HTTP ${xhr.status}`;try{message=window.AVi18n.t(JSON.parse(xhr.responseText).detail)||message;}catch(_){} const error=new Error(message);error.status=xhr.status;reject(error); } };
      xhr.onerror = () => { request = null; reject(new Error(window.AVi18n.t('انقطع الاتصال؛ ستتم متابعة الأجزاء التي أكدها الخادم.'))); };
      xhr.onabort = () => { request = null; reject(new Error(paused ? window.AVi18n.t('تم الإيقاف المؤقت.') : window.AVi18n.t('تم إيقاف الجزء الحالي.'))); };
      item.lastTick = startedAt; item.lastSent = offset;
      xhr.send(blob);
    });
  }

  async function uploadOne(item) {
    currentItem = item;
    const key = safeKey(item.file);
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(key) || 'null'); } catch (_) { localStorage.removeItem(key); }
    let uploadId = saved?.uploadId || null;
    item.uploadId = uploadId;
    if (uploadId) {
      const state = await api(`/api/v1/admin/videos/direct-import/resumable/${uploadId}`);
      if (state.size !== item.file.size) throw new Error(window.AVi18n.t('ملف الرفع المحفوظ لا يطابق الملف المحدد.'));
    } else {
      const suffix = item.isFolder ? '.hlsfolder.zip' : item.file.name;
      const session = await api('/api/v1/admin/videos/direct-import/resumable', {method:'POST', body:JSON.stringify({filename:suffix, size:item.file.size, folder_hls:item.isFolder}), headers:{'Content-Type':'application/json'}});
      uploadId = session.upload_id; item.uploadId = uploadId;
      localStorage.setItem(key, JSON.stringify({uploadId}));
    }
    let retries = 0;
    while (!paused && !stopRequested) {
      const state = await api(`/api/v1/admin/videos/direct-import/resumable/${uploadId}`);
      const offset = state.offset;
      if (offset >= item.file.size) break;
      const end = Math.min(offset + CHUNK, item.file.size);
      try {
        await sendChunk(uploadId, offset, item.file.slice(offset, end), item, Date.now());
        retries = 0;
      } catch (error) {
        if (paused || stopRequested) break;
        retries += 1;
        if (retries > 3) throw error;
        setStatus(window.AVi18n.tpl`تعذر تأكيد جزء ${retries}/3؛ استرجاع الإزاحة ثم إعادة المحاولة…`);
      }
    }
    if (paused || stopRequested) return {paused:true, uploadId};
    setStatus(window.AVi18n.t('اكتمل نقل البيانات؛ يجري فحص الملفات والتسجيل…'));
    const imported = await api(`/api/v1/admin/videos/direct-import/resumable/${uploadId}/finalize`, {method:'POST'});
    localStorage.removeItem(key); item.uploadId = null;
    return imported;
  }

  async function runQueue(items = queue) {
    if (active || !items.length) return;
    if (items !== queue) { queue = items; queueCursor = 0; }
    active = true; paused = false; stopRequested = false;
    startButton.disabled = true; pauseButton.disabled = false; cancelButton.disabled = false;
    resultNode.replaceChildren();
    while (queueCursor < queue.length) {
      const item = queue[queueCursor];
      if (item.status === 'READY') { queueCursor += 1; continue; }
      item.status = window.AVi18n.t('جارٍ الرفع'); if (item.stateNode) item.stateNode.textContent = item.status;
      try {
        const result = await uploadOne(item);
        if (result.paused) {
          item.status = stopRequested ? window.AVi18n.t('ملغى') : window.AVi18n.t('متوقف مؤقتًا');
          if (stopRequested && result.uploadId) {
            try { await api(`/api/v1/admin/videos/direct-import/resumable/${result.uploadId}`, {method:'DELETE'}); } catch (_) {}
            localStorage.removeItem(safeKey(item.file)); item.uploadId = null;
          }
          if (item.stateNode) item.stateNode.textContent = item.status;
          if (stopRequested) { for (let i=queueCursor+1;i<queue.length;i++) { if (queue[i].status === 'انتظار') { queue[i].status=window.AVi18n.t('لم يبدأ'); queue[i].stateNode.textContent=queue[i].status; } } }
          break;
        }
        item.status = 'READY'; item.result = result;
        if (item.stateNode) item.stateNode.textContent = window.AVi18n.t('READY · بدون تحويل');
        setStatus(result.media_type === 'mkv' ? window.AVi18n.t('تم التخزين الخاص · READY · MKV للتنزيل؛ التشغيل داخل المتصفح يعتمد على دعمه للصيغة.') : window.AVi18n.t('تم التحقق والتسجيل · READY · بدون تحويل'));
        const link = document.createElement('a'); link.className='btn btn-primary ready-upload-result-link';
        link.href = result.media_type === 'mkv' ? result.download_path : result.playback_path;
        link.textContent = `${item.originalName}: ${result.media_type === 'mkv' ? window.AVi18n.t('تنزيل خاص') : window.AVi18n.t('فتح الفيديو')}`;
        resultNode.append(link);
        queueCursor += 1;
        progressNode.value = Math.min(99, queue.reduce((sum, candidate) => sum + (candidate.status === 'READY' ? candidate.file.size : 0),0) / Math.max(1,queue.reduce((sum,candidate)=>sum+candidate.file.size,0))*100);
      } catch (error) {
        item.status = window.AVi18n.tpl`فشل: ${window.AVi18n.t(error.message)}`;
        if (error.status >= 400 && error.status < 500 && item.uploadId) {
          try { await api(`/api/v1/admin/videos/direct-import/resumable/${item.uploadId}`, {method:'DELETE'}); } catch (_) {}
          localStorage.removeItem(safeKey(item.file)); item.uploadId=null;
        }
        if (item.stateNode) item.stateNode.textContent = item.status;
        if (item.retryButton) item.retryButton.hidden = false;
        queueCursor += 1; // isolate this failure; other queued files continue
      }
    }
    active = false; currentItem = null;
    startButton.disabled = queue.length === 0;
    pauseButton.disabled = true; cancelButton.disabled = false;
    if (!paused && !stopRequested && queue.every(item => item.status === 'READY')) {
      progressNode.value = 100;
      if (queue.length === 1 && queue[0].result?.media_type === 'mkv') setStatus(window.AVi18n.t('تم التخزين الخاص · READY · MKV للتنزيل؛ التشغيل داخل المتصفح يعتمد على دعمه للصيغة.'));
      else setStatus(window.AVi18n.tpl`تم التحقق والتسجيل · اكتمل ${queue.length} فيديو · READY · بدون تحويل`);
    } else if (paused) setStatus(window.AVi18n.t('توقف مؤقتًا؛ أعد الضغط على الاستئناف لمتابعة الجزء الحالي.'));
    else if (stopRequested) setStatus(window.AVi18n.t('أوقف الطابور؛ الملفات المكتملة بقيت مسجلة والملفات غير المكتملة لم تُنشر.'));
  }

  async function retryItem(item) {
    if (active) return;
    item.status = window.AVi18n.t('انتظار'); item.retryButton.hidden = true; item.stateNode.textContent = item.status;
    await runQueue([item]);
  }

  const readyCard = byId('ready-import-card');
  ['dragenter','dragover'].forEach(type => readyCard.addEventListener(type,event=>{event.preventDefault();readyCard.classList.add('is-drag-over');}));
  ['dragleave','drop'].forEach(type => readyCard.addEventListener(type,event=>{event.preventDefault();readyCard.classList.remove('is-drag-over');}));
  readyCard.addEventListener('drop',event=>{
    const files=Array.from(event.dataTransfer?.files||[]);
    if(!files.length)return;
    if(files.some(file=>!['zip','mp4','mkv'].includes(file.name.split('.').pop().toLowerCase()))){setStatus(window.AVi18n.t('صيغة غير مدعومة. استخدم HLS ZIP أو MP4 أو MKV للتنزيل.'));return;}
    queue=files.map(file=>({file,originalName:file.name,isFolder:false,status:window.AVi18n.t('انتظار')}));queueCursor=0;progressNode.value=0;resultNode.replaceChildren();drawQueue();setStatus(window.AVi18n.tpl`تمت إضافة ${queue.length} ملف إلى طابور الرفع.`);
  });

  byId('ready-single-picker').addEventListener('click',()=>{fileInput.multiple=false;fileInput.click();});
  byId('ready-batch-picker').addEventListener('click',()=>{fileInput.multiple=true;fileInput.click();});
  fileInput.addEventListener('change',()=>{
    const files=Array.from(fileInput.files||[]);
    queue=files.map(file=>({file,originalName:file.name,isFolder:false,status:window.AVi18n.t('انتظار')}));
    queueCursor=0;progressNode.value=0;resultNode.replaceChildren();drawQueue();
    setStatus(queue.length>1?window.AVi18n.tpl`طابور جاهز: ${queue.length} فيديوهات. كل ملف يُسجل مستقلًا.`:queue.length?window.AVi18n.t('الملف جاهز للرفع.'):window.AVi18n.t('لم يتم اختيار ملف.'));
  });

  function crcTable(){const table=new Uint32Array(256);for(let n=0;n<256;n++){let c=n;for(let k=0;k<8;k++)c=(c&1)?(0xedb88320^(c>>>1)):(c>>>1);table[n]=c>>>0;}return table;}
  const CRC_TABLE=crcTable();
  async function fileCrc(file,onRead){let crc=0xffffffff;const reader=file.stream().getReader();let read=0;while(true){const {done,value}=await reader.read();if(done)break;for(const byte of value)crc=CRC_TABLE[(crc^byte)&255]^(crc>>>8);read+=value.length;onRead(read);}return (crc^0xffffffff)>>>0;}
  function zipLocalHeader(name,crc,size){const n=new TextEncoder().encode(name),b=new Uint8Array(30+n.length),v=new DataView(b.buffer);v.setUint32(0,0x04034b50,true);v.setUint16(4,20,true);v.setUint16(6,0x800,true);v.setUint16(8,0,true);v.setUint16(10,0,true);v.setUint16(12,33,true);v.setUint32(14,crc,true);v.setUint32(18,size,true);v.setUint32(22,size,true);v.setUint16(26,n.length,true);v.setUint16(28,0,true);b.set(n,30);return b;}
  function zipCentralHeader(name,crc,size,offset){const n=new TextEncoder().encode(name),b=new Uint8Array(46+n.length),v=new DataView(b.buffer);v.setUint32(0,0x02014b50,true);v.setUint16(4,0x0314,true);v.setUint16(6,20,true);v.setUint16(8,0x800,true);v.setUint16(10,0,true);v.setUint16(12,0,true);v.setUint16(14,33,true);v.setUint32(16,crc,true);v.setUint32(20,size,true);v.setUint32(24,size,true);v.setUint16(28,n.length,true);v.setUint16(30,0,true);v.setUint16(32,0,true);v.setUint16(34,0,true);v.setUint16(36,0,true);v.setUint32(38,0,true);v.setUint32(42,offset,true);b.set(n,46);return b;}
  function zipEnd(count,size,offset){const b=new Uint8Array(22),v=new DataView(b.buffer);v.setUint32(0,0x06054b50,true);v.setUint16(4,0,true);v.setUint16(6,0,true);v.setUint16(8,count,true);v.setUint16(10,count,true);v.setUint32(12,size,true);v.setUint32(16,offset,true);v.setUint16(20,0,true);return b;}
  async function makeFolderPackage(files,folderName){
    if(!files.length||files.length>MAX_FILES)throw new Error(window.AVi18n.tpl`عدد الملفات يجب أن يكون بين 1 و${MAX_FILES}.`);
    const prefix=files[0].webkitRelativePath.split('/')[0];
    const entries=[];const seen=new Set();let total=0;
    for(const file of files){const full=file.webkitRelativePath||file.name;const parts=full.split('/');if(parts[0]!==prefix||parts.length<2)throw new Error(window.AVi18n.t('اختر مجلد HLS واحدًا فقط.'));const name=parts.slice(1).join('/');if(!/^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$/.test(name)||name.split('/').some(part=>!part||part==='.'||part==='..'))throw new Error(window.AVi18n.tpl`مسار غير مدعوم داخل الحزمة: ${name}`);if(seen.has(name.toLowerCase()))throw new Error(window.AVi18n.tpl`اسم مكرر داخل مجلد HLS: ${name}`);seen.add(name.toLowerCase());if(!['.m3u8','.ts','.m4s','.mp4','.m4a','.aac','.key','.vtt'].includes(name.slice(name.lastIndexOf('.')).toLowerCase()))throw new Error(window.AVi18n.tpl`امتداد HLS غير مدعوم: ${name}`);total+=file.size;if(total>MAX_BYTES||file.size>0xffffffff)throw new Error(window.AVi18n.t('تجاوز مجلد HLS حد الحجم الآمن 2 GiB.'));entries.push({name,file});}
    if(!seen.has('master.m3u8'))throw new Error(window.AVi18n.t('يجب أن يحتوي جذر المجلد المحدد على master.m3u8.'));
    folderStatus.textContent=window.AVi18n.tpl`فحص ${entries.length} ملفًا · ${formatBytes(total)} · حساب سلامة ZIP…`;
    const parts=[],central=[];let offset=0;
    for(let i=0;i<entries.length;i++){
      const entry=entries[i];const crc=await fileCrc(entry.file,read=>{folderStatus.textContent=window.AVi18n.tpl`فحص الملف ${i+1}/${entries.length} · ${entry.name} · ${formatBytes(read)} / ${formatBytes(entry.file.size)} · الإجمالي ${formatBytes(total)}`;});
      const local=zipLocalHeader(entry.name,crc,entry.file.size);parts.push(local,entry.file);central.push(zipCentralHeader(entry.name,crc,entry.file.size,offset));offset+=local.length+entry.file.size;
      if(offset>0xffffffff)throw new Error(window.AVi18n.t('حزمة HLS أكبر من حد ZIP الآمن.'));
    }
    const centralOffset=offset;let centralSize=0;for(const record of central){parts.push(record);centralSize+=record.length;}
    parts.push(zipEnd(entries.length,centralSize,centralOffset));
    folderStatus.textContent=window.AVi18n.tpl`تم تجهيز ${entries.length} ملفًا (${formatBytes(total)}) مع الحفاظ على المسارات؛ سيجري التحقق النهائي على الخادم.`;
    return new File(parts,`${folderName.replace(/[^A-Za-z0-9._-]/g,'_')||'hls'}.hlsfolder.zip`,{type:'application/zip',lastModified:Date.now()});
  }
  byId('ready-folder-picker').addEventListener('click',()=>folderInput.click());
  folderInput.addEventListener('change',async()=>{
    try{
      const files=Array.from(folderInput.files||[]);if(!files.length)return;
      const first=files[0].webkitRelativePath||'';const folderName=first.split('/')[0]||'hls';
      setStatus(window.AVi18n.t('التحقق من مجلد HLS وإنشاء حزمة نقل بدون تعديل ملفات الوسائط…'));
      const file=await makeFolderPackage(files,folderName);
      queue=[{file,originalName:window.AVi18n.tpl`مجلد HLS: ${folderName}`,fileCount:files.length,isFolder:true,status:window.AVi18n.t('انتظار')}];queueCursor=0;progressNode.value=0;resultNode.replaceChildren();drawQueue();setStatus(window.AVi18n.tpl`تم تجهيز مجلد HLS (${files.length} ملفًا، ${formatBytes(files.reduce((s,f)=>s+f.size,0))}). اضغط بدء الرفع.`);
    }catch(error){folderStatus.textContent=window.AVi18n.tpl`تعذر تجهيز المجلد: ${window.AVi18n.t(error.message)}`;setStatus(window.AVi18n.t('لم يبدأ الرفع بسبب خطأ في مجلد HLS.'));}
  });

  startButton.addEventListener('click',()=>runQueue());
  pauseButton.addEventListener('click',()=>{paused=true;request?.abort();setStatus(window.AVi18n.t('جارٍ الإيقاف عند حدود الجزء الحالي؛ الجلسة قابلة للاستئناف.'));});
  cancelButton.addEventListener('click',async()=>{
    if(active){stopRequested=true;request?.abort();return;}
    const pending=queue.find(item=>item.uploadId);
    if(pending){try{await api(`/api/v1/admin/videos/direct-import/resumable/${pending.uploadId}`,{method:'DELETE'});localStorage.removeItem(safeKey(pending.file));pending.uploadId=null;pending.status=window.AVi18n.t('ملغى');pending.stateNode.textContent=pending.status;}catch(error){setStatus(window.AVi18n.tpl`تعذر إلغاء الجلسة: ${window.AVi18n.t(error.message)}`);return;}}
    setStatus(window.AVi18n.t('أُلغيت جلسة الرفع غير المكتملة؛ الفيديوهات المسجلة لا تتأثر.'));
  });

  const serverSelect=byId('server-import-select'),serverStatus=byId('server-import-status'),serverResult=byId('server-import-result'),serverStart=byId('server-import-start');
  byId('server-import-refresh').addEventListener('click',async()=>{
    serverStatus.textContent=window.AVi18n.t('جارٍ فحص مجلد ABOODVIDEO المسموح فقط…');serverStart.disabled=true;serverSelect.replaceChildren();
    try{const {items}=await api('/api/v1/admin/videos/direct-import/server-items');serverSelect.add(new Option(items.length?window.AVi18n.tpl`اختر ملفًا (${items.length})`:window.AVi18n.t('لا توجد ملفات جاهزة'),''));for(const item of items){const option=new Option(`${item.name} · ${item.media_type.toUpperCase()} · ${formatBytes(item.size_bytes)}${item.file_count?window.AVi18n.tpl` · ${item.file_count} ملف`:''}`,item.name);serverSelect.add(option);}serverStart.disabled=!items.length;serverStatus.textContent=items.length?window.AVi18n.tpl`عُثر على ${items.length} عنصر داخل مجلد الاستيراد المخصص.`:window.AVi18n.t('مجلد الاستيراد المخصص فارغ أو لا يحتوي ملفات صالحة.');}catch(error){serverSelect.add(new Option(window.AVi18n.t('تعذر عرض الملفات'),''));serverStatus.textContent=window.AVi18n.tpl`تعذر فحص مجلد الاستيراد: ${window.AVi18n.t(error.message)}`;}
  });
  serverSelect.addEventListener('change',()=>{serverStart.disabled=!serverSelect.value;});
  serverStart.addEventListener('click',async()=>{
    if(!serverSelect.value)return;serverStart.disabled=true;serverStatus.textContent=window.AVi18n.t('جارٍ التحقق من المسارات والبصمات والتسجيل…');serverResult.replaceChildren();
    try{const imported=await api('/api/v1/admin/videos/direct-import/server',{method:'POST',body:JSON.stringify({name:serverSelect.value}),headers:{'Content-Type':'application/json'}});serverStatus.textContent=window.AVi18n.tpl`تم الاستيراد والتحقق · READY · ${imported.media_type.toUpperCase()} · بدون تحويل`;const link=document.createElement('a');link.className='btn btn-primary';link.href=imported.playback_path;link.textContent=window.AVi18n.t('فتح الفيديو في ABOODVIDEO');serverResult.append(link);}catch(error){serverStatus.textContent=window.AVi18n.tpl`تعذر الاستيراد: ${window.AVi18n.t(error.message)}`;}finally{serverStart.disabled=!serverSelect.value;}
  });
})();
