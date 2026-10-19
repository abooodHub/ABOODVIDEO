window.AVi18n ||= {t: value => value, tpl: (parts, ...values) => parts.reduce((out, part, i) => out + part + (i < values.length ? values[i] : ''), '')};
"use strict";

const state = {config:{}, hardware:null, batch:{episodes:[],runner:{}}, server:{}, selection:null, importingFolder:null, encoders:[], openEpisodeDetails:new Set(), selectedEpisodeIds:new Set(), historyFilter:"all", historySearch:"", librarySearch:"", libraryFilter:"all", libraryView:"list", settingsTab:"general", queueFilter:"all", queueWorkspace:"processing"};
const activeBatchActions = new Set();
state.importExcluded=new Set();state.selectionError="";
const pageTitles = {import:window.AVi18n.t("إضافة محتوى"),queue:window.AVi18n.t("التحويل والرفع"),history:window.AVi18n.t("سجل المهام"),library:window.AVi18n.t("المكتبة المحلية"),settings:window.AVi18n.t("الإعدادات")};
const activeStates = new Set(["PROBING","READY_TO_ENCODE","ENCODING","PACKAGING","VERIFYING_LOCAL","UPLOADING","SERVER_VERIFYING"]);
const completedStates = new Set(["READY","READY_TO_UPLOAD"]);

const $ = (selector, root=document) => root.querySelector(selector);
const $$ = (selector, root=document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
const fmtBytes = bytes => {let n=Number(bytes||0);for(const unit of ["B","KB","MB","GB","TB"]){if(n<1024||unit==="TB")return `${n.toFixed(unit==="B"?0:n<10?2:1)} ${unit}`;n/=1024;}return "0 B";};
const fmtDuration = seconds => {const n=Math.max(0,Math.round(Number(seconds||0)));return `${String(Math.floor(n/3600)).padStart(2,"0")}:${String(Math.floor(n%3600/60)).padStart(2,"0")}:${String(n%60).padStart(2,"0")}`;};

async function api(path, options={}){
  const init={...options,headers:{"Content-Type":"application/json",...(options.headers||{})}};
  const response=await fetch(path,init);
  const payload=await response.json().catch(()=>({}));
  if(!response.ok) throw new Error(window.AVi18n.t(payload.detail)||window.AVi18n.t(payload.message)||`HTTP ${response.status}`);
  return payload;
}

function toast(message,type="ok"){
  const item=document.createElement("div");item.className=`toast ${type}`;item.textContent=message;$("#toasts").append(item);setTimeout(()=>item.remove(),4600);
}

function showPage(name){
  const jumpUpload=name==="upload",jumpConnection=name==="connection";
  if(jumpUpload)name="queue";
  if(jumpConnection){name="settings";setSettingsTab("connection");}
  if(!pageTitles[name])name="import";
  $$(".page").forEach(page=>page.classList.toggle("active",page.id===`page-${name}`));
  $$(".nav-item").forEach(item=>{const active=item.dataset.page===name;item.classList.toggle("active",active);item.setAttribute("aria-current",active?"page":"false");});
  $("#page-title").textContent=pageTitles[name];
  $("#page-description").textContent={import:window.AVi18n.t("جهّز حلقاتك للتحويل، ثم ارفعها عندما تكون جاهزة."),queue:window.AVi18n.t("تابع التحويل محليًا، ثم انقل الحزم الجاهزة إلى الخادم."),history:window.AVi18n.t("راجع النتائج، وافتح الملفات، وتابع المهام التي تحتاج إجراءً."),library:window.AVi18n.t("كل حلقاتك المحوّلة، منظمة حسب المسلسل والموسم."),settings:window.AVi18n.t("إعدادات جهازك ومجلداتك واتصال Studio بالخادم.")}[name];
  if(name==="settings"){loadTempInfo();loadFontLibrary();}
  if(name==="queue")setQueueWorkspace(jumpUpload?"upload":state.queueWorkspace);
  if(jumpConnection)requestAnimationFrame(()=>$("#server-url-input").scrollIntoView({block:"start",behavior:"smooth"}));
  else window.scrollTo({top:0,behavior:"instant"});
}

function setQueueWorkspace(name){
  state.queueWorkspace=name==="upload"?"upload":"processing";
  const upload=state.queueWorkspace==="upload";
  $("#processing-workspace").classList.toggle("hidden",upload);
  $("#page-upload").classList.toggle("hidden",!upload);
  $$("[data-workspace-tab]").forEach(button=>{const active=button.dataset.workspaceTab===state.queueWorkspace;button.classList.toggle("active",active);button.setAttribute("aria-selected",String(active));button.tabIndex=active?0:-1;});
}

function setSettingsTab(name,scroll=false){
  const buttons=$$("[data-settings-tab]");
  if(!buttons.some(button=>button.dataset.settingsTab===name))name="paths";
  state.settingsTab=name;
  buttons.forEach(button=>{const active=button.dataset.settingsTab===name;button.classList.toggle("active",active);if(active)button.setAttribute("aria-current","location");else button.removeAttribute("aria-current");});
  if(scroll){const section=$(`[data-settings-section="${CSS.escape(name)}"]`);if(section)requestAnimationFrame(()=>section.scrollIntoView({block:"start",behavior:"smooth"}));}
}

function statusLabel(value){
  return ({hashing_source:window.AVi18n.t("التحقق من المصدر"),hashing_package:window.AVi18n.t("التحقق من الحزمة"),creating_archive:window.AVi18n.t("تجهيز الأرشيف"),hashing_archive:window.AVi18n.t("فحص الأرشيف"),PENDING:window.AVi18n.t("بانتظار البدء"),PROBING:window.AVi18n.t("فحص المصدر"),READY_TO_ENCODE:window.AVi18n.t("تحضير"),ENCODING:window.AVi18n.t("قيد التحويل"),PACKAGING:window.AVi18n.t("تجهيز HLS"),VERIFYING_LOCAL:window.AVi18n.t("التحقق"),READY_TO_UPLOAD:window.AVi18n.t("جاهز للرفع"),UPLOADING:window.AVi18n.t("رفع"),SERVER_VERIFYING:window.AVi18n.t("تحقق الخادم"),READY:window.AVi18n.t("مكتمل"),PAUSED:window.AVi18n.t("متوقف مؤقتاً"),FAILED:window.AVi18n.t("فشل"),CANCELLED:window.AVi18n.t("ملغي"),SKIPPED:window.AVi18n.t("متخطى"),NEEDS_REVIEW:window.AVi18n.t("تحتاج مراجعة")})[value]||value||window.AVi18n.t("انتظار");
}

function statusClass(value){return completedStates.has(value)?"ready":value==="FAILED"?"failed":activeStates.has(value)?"active":"";}
function episodeEncoder(ep){return ep.settings?.encoder_choice||state.config.default_encoder||"auto";}
function presetLabel(value){return ({economy:window.AVi18n.t("اقتصادي"),fast:window.AVi18n.t("اقتصادي"),balanced:window.AVi18n.t("متوازن"),quality:window.AVi18n.t("جودة عالية")})[value]||window.AVi18n.t("متوازن");}
function presetOptions(selected){const options=[["balanced",window.AVi18n.t("متوازن")],["quality",window.AVi18n.t("جودة عالية")]];if(selected==="economy")options.unshift(["economy",window.AVi18n.t("اقتصادي — مهمة قديمة"),true]);return options.map(([value,label,disabled])=>`<option value="${value}" ${selected===value?"selected":""} ${disabled?"disabled":""}>${label}</option>`).join("");}
function bitrateMbps(value){return value?`${(Number(value)/1000000).toFixed(Number(value)%1000000?1:0)} Mbps`:"—";}
function stageLabel(ep){if(ep.state==="PENDING")return window.AVi18n.t("جاهزة للبدء");if(ep.state==="PAUSED")return window.AVi18n.t("متوقفة مؤقتاً");if(ep.state==="ENCODING"&&ep.current_quality)return ep.current_quality;if(ep.state==="PACKAGING")return window.AVi18n.t("تجهيز HLS");if(ep.state==="VERIFYING_LOCAL")return window.AVi18n.t("تحقق");if(ep.state==="UPLOADING")return statusLabel(ep.current_stage||"UPLOADING");if(ep.state==="SERVER_VERIFYING")return window.AVi18n.t("تحقق الخادم");return statusLabel(ep.current_stage||ep.state);}
function queueFilterMatch(ep,filter=state.queueFilter){if(filter==="active")return activeStates.has(ep.state);if(filter==="waiting")return !activeStates.has(ep.state)&&!['READY_TO_UPLOAD','FAILED','NEEDS_REVIEW','READY','SKIPPED','CANCELLED'].includes(ep.state);if(filter==="ready")return ep.state==="READY_TO_UPLOAD";if(filter==="attention")return ep.state==="FAILED"||ep.state==="NEEDS_REVIEW";return ep.state!=="READY";}
function encoderLabel(ep){const id=ep.encoder_used||episodeEncoder(ep);if(String(id).includes("nvenc"))return "NVIDIA NVENC";if(String(id).includes("qsv"))return "Intel QSV";if(String(id).includes("amf"))return "AMD AMF";if(String(id).includes("264"))return "CPU · x264";if(String(id).includes("265"))return "CPU · x265";return id==="gpu"?window.AVi18n.t("GPU · تلقائي"):id==="cpu"?window.AVi18n.t("CPU · تلقائي"):id||"—";}
function pipelineStates(ep){const selected=Array.isArray(ep.renditions)&&ep.renditions.length?ep.renditions:(ep.settings?.processing_history?.successful?.renditions?Object.keys(ep.settings.processing_history.successful.renditions):[]);const completed=Array.isArray(ep.completed_renditions)?ep.completed_renditions:Object.keys(ep.settings?.processing_history?.successful?.renditions||{});const all=selected.length?selected:[window.AVi18n.t("جاري الفحص")];const current=ep.current_quality;return all.map(quality=>{let status="PENDING";let pct=0;if(ep.state==="FAILED"&&current===quality)status="FAILED";else if(completed.includes(quality)){status="COMPLETED";pct=100;}else if(ep.state==="ENCODING"&&current===quality){status="ACTIVE";pct=Number(ep.rendition_progress||0);}return {quality,status,pct};});}

function isQueueSelectable(ep){return !activeStates.has(ep.state);}
function pathKey(path){return String(path||"").replace(/\\/g,"/").replace(/\/{2,}/g,"/").replace(/\/$/,"").toLocaleLowerCase();}
function countQueuedInFolder(folder,episodes=state.batch.episodes||[]){const prefix=`${pathKey(folder)}/`;return episodes.filter(ep=>pathKey(ep.source_path).startsWith(prefix)).length;}
function updateImportProgress(){
  if(!state.importingFolder)return;
  const button=$("#queue-selection"),queued=countQueuedInFolder(state.importingFolder),total=Number(state.selection?.discovery?.episode_count||0);
  const kind=state.selection?.type==="series"?window.AVi18n.t("المسلسل"):state.selection?.type==="file"?window.AVi18n.t("الملف"):window.AVi18n.t("الموسم");
  button.textContent=total?window.AVi18n.tpl`جارٍ فحص ${kind} وإضافته… ${queued}/${total}`:window.AVi18n.tpl`جارٍ فحص ${kind} وإضافته… (${queued})`;
}
function syncQueueSelection(episodes=state.batch.episodes||[],scope=episodes){
  const selectable=episodes.filter(isQueueSelectable),selectableIds=new Set(selectable.map(ep=>String(ep.id)));
  state.selectedEpisodeIds=new Set([...state.selectedEpisodeIds].filter(id=>selectableIds.has(id)));
  const selected=state.selectedEpisodeIds.size,scopeSelectable=scope.filter(isQueueSelectable),selectedInScope=scopeSelectable.filter(ep=>state.selectedEpisodeIds.has(String(ep.id))).length,all=$("#select-all-queue");
  if(all){all.checked=scopeSelectable.length>0&&selectedInScope===scopeSelectable.length;all.indeterminate=selectedInScope>0&&selectedInScope<scopeSelectable.length;all.disabled=scopeSelectable.length===0;}
  const button=$("#delete-selected-queue");if(button)button.disabled=selected===0;
  const count=$("#selected-queue-count");if(count)count.textContent=selected?window.AVi18n.tpl`تم تحديد ${selected} حلقة`:window.AVi18n.t("لم يتم تحديد حلقات");
}

async function deleteSelectedQueue(){
  const ids=[...state.selectedEpisodeIds];if(!ids.length)return;
  if(!confirm(window.AVi18n.tpl`سيُحذف ${ids.length} سجل من قائمة الانتظار. ستبقى ملفات الحزم المحوّلة محفوظة. هل تريد المتابعة؟`))return;
  const button=$("#delete-selected-queue");button.disabled=true;
  try{const result=await api("/api/batch/episodes/delete-bulk",{method:"POST",body:JSON.stringify({episode_ids:ids,expected_count:ids.length})});state.selectedEpisodeIds.clear();toast(window.AVi18n.tpl`حُذف ${result.removed} سجل من القائمة، وبقيت ملفات الإخراج محفوظة.`);await refreshBatch();}
  catch(error){toast(window.AVi18n.t(error.message),"error");await refreshBatch();}
  finally{syncQueueSelection();}
}

function renderHardware(hw){
  if(!hw)return;state.hardware=hw;
  const cpu=hw.cpu_ram||{},gpu=hw.gpu||{};
  $("#cpu-value").textContent=`${cpu.cpu_util_pct||0}%`;$("#cpu-meter").style.width=`${cpu.cpu_util_pct||0}%`;$("#cpu-detail").textContent=`${cpu.cpu_cores_physical||0} cores / ${cpu.cpu_cores_logical||0} threads`;
  $("#gpu-value").textContent=gpu.gpu_detected?`${gpu.gpu_util_pct||0}%`:"—";$("#gpu-meter").style.width=`${gpu.gpu_util_pct||0}%`;$("#gpu-name").textContent=gpu.gpu_name||window.AVi18n.t("غير متوفر");
  $("#vram-value").textContent=gpu.vram_total_mb?`${(gpu.vram_used_mb/1024).toFixed(1)} / ${(gpu.vram_total_mb/1024).toFixed(1)} GB`:"—";$("#vram-meter").style.width=`${gpu.vram_util_pct||0}%`;$("#gpu-temp").textContent=gpu.gpu_temp_c?`${gpu.gpu_temp_c}°C`:"—";
  $("#ram-value").textContent=`${(Number(cpu.ram_used_mb||0)/1024).toFixed(1)} / ${(Number(cpu.ram_total_mb||0)/1024).toFixed(1)} GB`;$("#ram-meter").style.width=`${cpu.ram_util_pct||0}%`;$("#ram-detail").textContent=window.AVi18n.tpl`${cpu.ram_util_pct||0}% مستخدم`;
  if(hw.encoders){state.encoders=hw.encoders;renderEncoders();}
}

function markTelemetryUnavailable(){
  for(const id of ["cpu-value","gpu-value","ram-value","vram-value"])$("#"+id).textContent="—";
  for(const id of ["cpu-meter","gpu-meter","ram-meter","vram-meter"])$("#"+id).style.width="0%";
  $("#cpu-detail").textContent=window.AVi18n.t("القياس غير متاح");
}

function renderServer(){
  const server=state.server||{},ok=Boolean(server.connected);const url=state.config.server_url||"—";
  $("#server-url").textContent=url;
  for(const id of ["server-url-input","queue-server-url-input"]){const input=$("#"+id);if(document.activeElement!==input)input.value=url==="—"?"":url;}
  $("#auth-state").textContent=server.authenticated?window.AVi18n.t("موثّق"):window.AVi18n.t("غير موثّق");
  for(const id of ["upload-server-chip"]){const el=$("#"+id);if(el){el.textContent=ok?window.AVi18n.t("متصل"):window.AVi18n.t("غير متصل");el.classList.add(ok?"ok":"bad");el.classList.remove(ok?"bad":"ok");}}
  $("#server-label").textContent=ok?window.AVi18n.t("الخادم متصل"):window.AVi18n.t("الخادم غير متصل");$(".server-pill").classList.toggle("ok",ok);
  $("#upload-health").textContent=server.health||"—";$("#upload-readiness").textContent=server.readiness||"—";$("#upload-scopes").textContent=(server.scopes||[]).join(" · ")||"—";$("#upload-server-copy").textContent=ok?window.AVi18n.t("الخادم جاهز لاستقبال الحزم."):window.AVi18n.t("تعذر تأكيد جاهزية الخادم.");
}

function renderBatch(){
  const allEpisodes=[...(state.batch.episodes||[])],currentBatchId=state.batch.batch?.id;
  const episodes=allEpisodes.filter(ep=>!currentBatchId||String(ep.batch_id)===String(currentBatchId)).sort((a,b)=>{const x=a.settings||{},y=b.settings||{};const sx=x.series_name||"",sy=y.series_name||"";const seriesOrder=sx.localeCompare(sy,undefined,{numeric:true,sensitivity:"base"});if(seriesOrder)return seriesOrder;const seasonOrder=String(x.season_number||"").localeCompare(String(y.season_number||""),undefined,{numeric:true,sensitivity:"base"});if(seasonOrder)return seasonOrder;const folderOrder=String(x.season_name||"").localeCompare(String(y.season_name||""),undefined,{numeric:true,sensitivity:"base"});if(folderOrder)return folderOrder;const number=ep=>ep.episode_number!==""&&Number.isFinite(Number(ep.episode_number??ep.order_index))?Number(ep.episode_number??ep.order_index):Infinity;return (number(a)-number(b))||String(a.filename||"").localeCompare(String(b.filename||""),undefined,{numeric:true});});const counts={active:0,waiting:0,ready_to_upload:0,completed:0,failed:0};
  episodes.forEach(ep=>{if(activeStates.has(ep.state))counts.active++;else if(ep.state==="READY_TO_UPLOAD")counts.ready_to_upload++;else if(ep.state==="READY")counts.completed++;else if(ep.state==="FAILED"||ep.state==="NEEDS_REVIEW")counts.failed++;else if(!["SKIPPED","CANCELLED"].includes(ep.state))counts.waiting++;});
  for(const [key,value] of Object.entries(counts)){const stat=$("#stat-"+key);if(stat)stat.textContent=value;}
  $("#nav-queue-count").textContent=counts.active+counts.waiting+counts.ready_to_upload;const uploadsReady=allEpisodes.filter(ep=>ep.state==="READY_TO_UPLOAD").length,uploadsInProgress=allEpisodes.filter(ep=>ep.state==="UPLOADING"||ep.state==="SERVER_VERIFYING").length;$("#upload-ready-count").textContent=window.AVi18n.tpl`${uploadsReady} جاهزة${uploadsInProgress?window.AVi18n.tpl` · ${uploadsInProgress} قيد الرفع`:""}`;
  const uploadButton=$("#upload-ready");if(uploadButton)uploadButton.disabled=uploadsReady===0;
  const historyCompleted=allEpisodes.filter(ep=>ep.state==="READY").length,historyFailed=allEpisodes.filter(ep=>ep.state==="FAILED"||ep.state==="NEEDS_REVIEW").length;
  $("#nav-history-count").textContent=historyCompleted+historyFailed;$("#history-all-count").textContent=historyCompleted+historyFailed;$("#history-completed-count").textContent=historyCompleted;$("#history-failed-count").textContent=historyFailed;
  const retryHistory=$("#retry-failed-history");retryHistory.classList.toggle("hidden",state.historyFilter!=="failed");retryHistory.disabled=historyFailed===0;
  $("#running-count").textContent=counts.active;$("#running-indicator").classList.toggle("ok",counts.active>0);
  const queueEpisodes=episodes.filter(ep=>ep.state!=="READY");
  const visibleQueueEpisodes=queueEpisodes.filter(ep=>queueFilterMatch(ep));
  const queueCounts={all:queueEpisodes.length,active:queueEpisodes.filter(ep=>activeStates.has(ep.state)).length,waiting:queueEpisodes.filter(ep=>queueFilterMatch(ep,"waiting")).length,ready:queueEpisodes.filter(ep=>ep.state==="READY_TO_UPLOAD").length,attention:queueEpisodes.filter(ep=>ep.state==="FAILED"||ep.state==="NEEDS_REVIEW").length};
  for(const [key,value] of Object.entries(queueCounts)){const counter=$("#queue-filter-"+key+"-count");if(counter)counter.textContent=value;}
  $("#queue-status-filters").querySelectorAll("[data-queue-filter]").forEach(button=>{const selected=button.dataset.queueFilter===state.queueFilter;button.classList.toggle("active",selected);button.setAttribute("aria-selected",String(selected));});
  const active=episodes.find(ep=>activeStates.has(ep.state));
  $("#active-job-card").className=`active-job ${active?"":"empty"}`;
  $("#active-job-card").innerHTML=active?`<div><b>${esc(active.filename)}</b><small>${esc(statusLabel(active.state))} · ${esc(active.current_quality||"")}</small></div><strong>${Number(active.progress||0).toFixed(0)}%</strong>`:window.AVi18n.t("لا توجد مهمة قيد المعالجة");
  const groupStats=new Map();for(const ep of episodes){const settings=ep.settings||{};const series=settings.series_name||window.AVi18n.t("مسلسل");const season=settings.season_name||settings.season_number||window.AVi18n.t("موسم");const key=`${series}\u0000${season}`;const value=groupStats.get(key)||{total:0,active:0,converted:0,waiting:0,current:null,progress:0};value.total++;if(activeStates.has(ep.state)){value.active++;if(!value.current||ep.state==="ENCODING"){value.current=ep;value.progress=Number(ep.progress||0);}}else if(completedStates.has(ep.state))value.converted++;else if(!["FAILED","NEEDS_REVIEW","SKIPPED","CANCELLED"].includes(ep.state))value.waiting++;groupStats.set(key,value);}
  const body=$("#queue-body");for(const row of body.querySelectorAll(".episode-details:not(.hidden)"))state.openEpisodeDetails.add(row.dataset.details);const renderedIds=new Set(visibleQueueEpisodes.map(ep=>ep.id));for(const id of state.openEpisodeDetails)if(!renderedIds.has(id))state.openEpisodeDetails.delete(id);let lastGroup="";body.innerHTML=visibleQueueEpisodes.map(ep=>{
    const settings=ep.settings||{};const series=settings.series_name||((ep.season_folder_path||"").split(/[\\/]/).slice(-2,-1)[0]||window.AVi18n.t("مسلسل"));const season=settings.season_name||settings.season_number||((ep.season_folder_path||"").split(/[\\/]/).pop()||window.AVi18n.t("موسم"));const group=`${series}\u0000${season}`;let heading="";if(group!==lastGroup){lastGroup=group;const stats=groupStats.get(group)||{total:0,active:0,converted:0,waiting:0};const batchPct=stats.total?stats.converted/stats.total*100:0;const now=stats.current?window.AVi18n.tpl`<span>الحالية: ${esc(stats.current.display_label||stats.current.episode_number||"")} · ${esc(stageLabel(stats.current))} · ${Number(stats.current.progress||0).toFixed(0)}%</span>`:"";heading=window.AVi18n.tpl`<tr class="queue-group"><th colspan="8"><b>${esc(series)} <span>· ${esc(season)}</span></b><div class="queue-batch-progress"><span>${stats.total} حلقة</span><span>${stats.active} نشطة</span><span>${stats.waiting} انتظار</span><span>${stats.converted} / ${stats.total} محوّلة</span><div class="progress-track"><i style="width:${batchPct}%"></i></div><span>${batchPct.toFixed(0)}%</span>${now}</div></th></tr>`;}
    const pct=Math.max(0,Math.min(100,Number(ep.progress||0))),renditionPct=Math.max(0,Math.min(100,Number(ep.rendition_progress||0)));const outputPath=ep.work_dir||ep.settings?.output_directory||"—";const overallLabel=completedStates.has(ep.state)?100:pct;const qualities=pipelineStates(ep);const stages=qualities.map(q=>`<span class="rendition-stage ${q.status.toLowerCase()}">${q.status==="COMPLETED"?"✓":q.status==="ACTIVE"?"▶":q.status==="FAILED"?"✕":"○"} ${esc(q.quality)} ${q.status==="ACTIVE"?`${q.pct.toFixed(0)}%`:""}</span>`).join("");const packageStatus=completedStates.has(ep.state)?window.AVi18n.t("✓ جاهز"):ep.state==="PACKAGING"?window.AVi18n.t("▶ جارٍ"):ep.state==="FAILED"&&/package|hls|manifest/i.test(ep.error||"")?window.AVi18n.t("✕ فشل"):window.AVi18n.t("○ انتظار");const sub=ep.subtitle_label||ep.subtitle_format||window.AVi18n.t("بدون ترجمة");const enc=encoderLabel(ep);const elapsed=Number(ep.elapsed_seconds||ep.encoding_elapsed_seconds||0);const eta=ep.eta&&ep.eta!=="00:00:00"?ep.eta:"—";const failedQuality=ep.state==="FAILED"?ep.current_quality||ep.current_stage||window.AVi18n.t("مرحلة غير محددة"):"";
    const jobSettings=ep.settings||{};const rates=jobSettings.video_bitrate_bps_per_rendition||{};const audios=jobSettings.audio_bitrate_bps_per_rendition||{};const selectedPreset=jobSettings.preset_name||jobSettings.default_preset||"balanced";const expectedQualities=Array.isArray(jobSettings.selected_qualities)?jobSettings.selected_qualities:[];const byRendition=jobSettings.expected_output_by_rendition_bytes||{};const estimate=Number(jobSettings.expected_output_bytes||ep.output_estimate_bytes||0);const rateCards=expectedQualities.map(q=>window.AVi18n.tpl`<span class="detail-chip"><b dir="ltr">${esc(q)}</b><small>فيديو ${esc(bitrateMbps(rates[q]))} · صوت ${esc(bitrateMbps(audios[q]))}</small></span>`).join("")||window.AVi18n.tpl`<small>بانتظار فحص المصدر</small>`;const sizeCards=Object.entries(byRendition).map(([q,n])=>`<span class="detail-chip"><b dir="ltr">${esc(q)}</b><small>${fmtBytes(n)}</small></span>`).join("");const presetControl=["PENDING","NEEDS_REVIEW","READY_TO_ENCODE"].includes(ep.state)?window.AVi18n.tpl`<label class="job-preset">جودة المهمة<select class="row-preset" data-id="${esc(ep.id)}">${presetOptions(selectedPreset)}</select></label>`:window.AVi18n.tpl`<small>يثبت الإعداد عند بدء المعالجة</small>`;
    const detail=window.AVi18n.tpl`<tr class="episode-details hidden" data-details="${esc(ep.id)}"><td colspan="8"><div class="details-layout"><section class="detail-card detail-paths"><h4>مسارات الملفات</h4><div class="detail-fields"><div class="detail-field"><b>المصدر</b><small class="ltr">${esc(ep.source_path||"—")}</small></div><div class="detail-field"><b>مجلد الإخراج</b><small class="ltr">${esc(outputPath)}</small></div></div></section><section class="detail-card"><h4>إعدادات المعالجة</h4><div class="detail-fields detail-fields-three"><div class="detail-field"><b>المشفّر الفعلي</b><small class="ltr">${esc(ep.encoder_used||episodeEncoder(ep))}</small></div><div class="detail-field"><b>السرعة / FPS</b><small dir="ltr">${ep.state==="ENCODING"&&ep.speed&&ep.speed!=="0x"?esc(ep.speed):"—"} · ${ep.state==="ENCODING"&&Number(ep.fps)>0?`${Number(ep.fps).toFixed(1)} FPS`:"—"}</small></div><div class="detail-field"><b>الجودة</b><small>${presetLabel(selectedPreset)}</small>${presetControl}</div><div class="detail-field"><b>الترجمة</b><small>${esc(sub)}</small></div><div class="detail-field"><b>إعداد NVENC</b><small dir="ltr">${esc(settings.nvenc_preset||"p5")} · CQ ${esc(settings.cq??22)}</small></div><div class="detail-field"><b>فيديو 4K</b><small>${settings.four_k_video_mode==="COPY"?window.AVi18n.t("نسخ المصدر"):window.AVi18n.t("تحويل")}</small></div><div class="detail-field"><b>الخطوط</b><small>${Number(ep.font_audit?.available_count||0)} من ${Number(ep.font_audit?.total_fonts||0)} متوفرة${Number(ep.font_audit?.missing_count||0)?window.AVi18n.tpl` · مفقود ${Number(ep.font_audit.missing_count)}`:ep.font_status?` · ${esc(ep.font_status)}`:""}</small></div></div></section><section class="detail-card"><h4>معدلات البت المستهدفة</h4><div class="detail-chips">${rateCards}</div></section><section class="detail-card"><h4>الحجم المتوقع <small>تقديري</small></h4><strong class="detail-total ltr">${fmtBytes(estimate)}</strong><div class="detail-chips">${sizeCards||window.AVi18n.tpl`<span class="detail-chip">لا يوجد تقدير تفصيلي</span>`}</div></section><section class="detail-card detail-wide"><h4>مراحل الحلقة</h4><div class="stage-list"><span class="rendition-stage ${["ENCODING","PACKAGING","VERIFYING_LOCAL","READY_TO_UPLOAD","READY"].includes(ep.state)?"completed":"pending"}">✓ فحص المصدر</span><span class="rendition-stage ${ep.state==="FAILED"&&failedQuality==="PROBING"?"failed":ep.state==="PROBING"?"active":"completed"}">${ep.state==="PROBING"?"▶":"✓"} استخراج البيانات</span>${stages}<span class="rendition-stage ${String(packageStatus).startsWith("✓")?"completed":ep.state==="PACKAGING"?"active":String(packageStatus).startsWith("✕")?"failed":"pending"}">${esc(packageStatus)} · HLS</span><span class="rendition-stage ${ep.state==="VERIFYING_LOCAL"?"active":completedStates.has(ep.state)?"completed":"pending"}">${completedStates.has(ep.state)||ep.state==="VERIFYING_LOCAL"?"✓":"○"} التحقق</span><span class="rendition-stage ${completedStates.has(ep.state)?"completed":"pending"}">${completedStates.has(ep.state)?"✓":"○"} جاهز</span></div>${ep.state==="FAILED"?window.AVi18n.tpl`<small class="failure-stage">الفشل عند: ${esc(failedQuality)}${ep.error?` · ${esc(ep.error)}`:""}</small>`:""}</section></div></td></tr>`;
    const qualityBadge=ep.state==="PACKAGING"?window.AVi18n.t("تجهيز HLS"):ep.state==="VERIFYING_LOCAL"?window.AVi18n.t("تحقق"):ep.state==="UPLOADING"?window.AVi18n.t("رفع"):ep.state==="SERVER_VERIFYING"?window.AVi18n.t("تحقق"):ep.current_quality||"—";const speed=ep.state==="ENCODING"&&ep.speed&&ep.speed!=="0x"?ep.speed:"—";const fps=ep.state==="ENCODING"&&Number(ep.fps)>0?`${Number(ep.fps).toFixed(1)} FPS`:"—";
    return window.AVi18n.tpl`${heading}<tr class="episode-row"><td><label class="queue-row-select"><input type="checkbox" class="queue-episode-checkbox" data-id="${esc(ep.id)}" aria-label="تحديد الحلقة ${esc(ep.display_label||ep.episode_number||ep.order_index)}" ${state.selectedEpisodeIds.has(String(ep.id))?"checked":""} ${isQueueSelectable(ep)?"":"disabled"}><span class="sr-only">تحديد الحلقة</span></label><button class="expand-button" aria-expanded="false" data-toggle-details="${esc(ep.id)}">＋</button><b>الحلقة ${esc(ep.display_label||ep.episode_number||ep.order_index)}</b><small class="source-cell" title="${esc(ep.source_path)}">${esc(ep.filename)}</small></td><td><span class="state ${statusClass(ep.state)}">${esc(statusLabel(ep.state))}</span>${ep.state==="FAILED"&&failedQuality?`<small class="failure-stage">${esc(failedQuality)}</small>`:""}</td><td><span class="quality-badge ${activeStates.has(ep.state)?"active":""}">${esc(qualityBadge)}</span><small class="stage-copy">${esc(stageLabel(ep))}</small></td><td class="progress-cell"><div class="overall-progress"><div class="progress-track"><i style="width:${overallLabel}%"></i></div><small>كلي ${overallLabel.toFixed(0)}%</small></div><div class="current-progress"><div class="progress-track secondary-progress"><i style="width:${renditionPct}%"></i></div><small>${ep.current_quality?`${esc(ep.current_quality)} ${renditionPct.toFixed(0)}%`:completedStates.has(ep.state)?window.AVi18n.t("الجودات مكتملة"):"—"}</small></div></td><td><span class="encoder-badge">${esc(enc)}</span><select class="row-encoder" data-id="${esc(ep.id)}" aria-label="المشفّر للحلقة ${esc(ep.episode_number||ep.order_index)}"><option value="auto" ${episodeEncoder(ep)==="auto"?"selected":""}>تلقائي</option><option value="gpu" ${episodeEncoder(ep)==="gpu"?"selected":""}>GPU</option><option value="cpu" ${episodeEncoder(ep)==="cpu"?"selected":""}>CPU</option>${state.encoders.filter(x=>x.available).map(x=>`<option value="${esc(x.id)}" ${episodeEncoder(ep)===x.id?"selected":""}>${esc(x.name)}</option>`).join("")}</select></td><td><b>${esc(speed)}</b><small class="metric-sub">${esc(fps)}</small></td><td><b class="ltr">${fmtDuration(elapsed)}</b><small class="metric-sub">ETA ${esc(eta)}</small></td><td><div class="row-actions">${ep.state==="READY_TO_UPLOAD"?window.AVi18n.tpl`<button class="icon-button upload-row-action" data-row-action="upload" data-id="${esc(ep.id)}">رفع الآن</button><button class="icon-button" data-row-action="open" data-id="${esc(ep.id)}">فتح الحزمة</button>`:""}${ep.state==="PENDING"?window.AVi18n.tpl`<button class="icon-button" data-row-action="start" data-id="${esc(ep.id)}">بدء</button>`:""}${ep.state==="FAILED"||ep.state==="CANCELLED"?window.AVi18n.tpl`<button class="icon-button" data-row-action="retry" data-id="${esc(ep.id)}">إعادة المحاولة</button>`:""}${activeStates.has(ep.state)?window.AVi18n.tpl`<button class="icon-button" data-row-action="cancel" data-id="${esc(ep.id)}">إلغاء</button>`:window.AVi18n.tpl`<button class="icon-button" data-row-action="remove" data-id="${esc(ep.id)}">حذف</button>`}</div></td></tr>${detail}`;
  }).join("");
  body.querySelectorAll(".episode-row").forEach((row,index)=>{
    const ep=visibleQueueEpisodes[index];if(!ep||!["UPLOADING","SERVER_VERIFYING"].includes(ep.state))return;
    const total=Number(ep.package_bytes||0),uploaded=Number(ep.uploaded_bytes||0);
    const uploadPct=ep.state==="SERVER_VERIFYING"?100:Math.max(0,Math.min(100,total>0?uploaded/total*100:Number(ep.progress||0)));
    row.querySelector(".overall-progress .progress-track i").style.width="100%";
    row.querySelector(".overall-progress small").textContent=window.AVi18n.t("التحويل 100%");
    row.querySelector(".current-progress .progress-track i").style.width=`${uploadPct}%`;
    row.querySelector(".current-progress small").textContent=window.AVi18n.tpl`الرفع ${uploadPct.toFixed(0)}%`;
  });
  body.querySelectorAll(".episode-row").forEach((row,index)=>{
    const detail=body.querySelector(`[data-details="${CSS.escape(String(visibleQueueEpisodes[index].id))}"]`);
    const encoder=row.querySelector(".row-encoder");
    const encoderDetail=detail?.querySelector(".detail-card:nth-child(2) .detail-field:first-child");
    if(encoder&&encoderDetail)encoderDetail.append(encoder);
    for(const action of row.querySelectorAll("[data-row-action]")){
      if(action.dataset.rowAction==="upload"){action.title=window.AVi18n.t("رفع الآن");action.textContent=window.AVi18n.t("رفع");}
      if(action.dataset.rowAction==="open"){action.title=window.AVi18n.t("فتح الحزمة");action.textContent=window.AVi18n.t("فتح");}
    }
  });
  for(const id of state.openEpisodeDetails){const detail=body.querySelector(`[data-details="${CSS.escape(id)}"]`),toggle=body.querySelector(`[data-toggle-details="${CSS.escape(id)}"]`);if(detail&&toggle){detail.classList.remove("hidden");toggle.setAttribute("aria-expanded","true");toggle.textContent="−";}}
  $("#queue-empty").classList.toggle("hidden",queueEpisodes.length>0);$("#queue-filter-empty").classList.toggle("hidden",queueEpisodes.length===0||visibleQueueEpisodes.length>0);$(".table-wrap").classList.toggle("hidden",visibleQueueEpisodes.length===0);
  updateImportProgress();
  syncQueueSelection(queueEpisodes,visibleQueueEpisodes);
  renderResultLists(allEpisodes);
}

function renderResultLists(episodes){
  const ready=episodes.filter(ep=>ep.state==="READY_TO_UPLOAD");const uploading=episodes.filter(ep=>ep.state==="UPLOADING"||ep.state==="SERVER_VERIFYING");const completed=episodes.filter(ep=>ep.state==="READY");const failed=episodes.filter(ep=>ep.state==="FAILED"||ep.state==="NEEDS_REVIEW");
  const expandedUploads=new Set([...$("#upload-ready-list").querySelectorAll(".upload-row:has(details[open])")].map(row=>row.dataset.uploadId));
  $("#upload-ready-list").innerHTML=ready.length||uploading.length?[...uploading,...ready].map(ep=>uploadRow(ep,expandedUploads.has(String(ep.id)))).join(""):window.AVi18n.tpl`<div class="empty-state"><b>لا توجد حزم جاهزة للرفع</b><span>ستظهر هنا الحلقات بعد اكتمال تحويلها والتحقق منها.</span><button class="button secondary" type="button" data-workspace-tab="processing">عرض مهام التحويل</button></div>`;
  const allHistory=[...completed,...failed];
  const historyScope=state.historyFilter==="completed"?completed:state.historyFilter==="failed"?failed:allHistory;
  const visibleHistory=historyScope.filter(ep=>`${ep.filename||""} ${ep.display_label||""}`.toLocaleLowerCase().includes(state.historySearch));
  $("#history-list").innerHTML=visibleHistory.length?visibleHistory.map(historyRow).join(""):`<div class="empty-state"><b>${state.historyFilter==="failed"?window.AVi18n.t("لا توجد مهام تحتاج إجراء"):state.historyFilter==="completed"?window.AVi18n.t("لا توجد حلقات مرفوعة ومتحقق منها"):window.AVi18n.t("لا يوجد سجل مهام بعد")}</b><span>${state.historyFilter==="failed"?window.AVi18n.t("ستظهر هنا المهام التي تعثرت أو تحتاج مراجعة."):state.historyFilter==="completed"?window.AVi18n.t("ستظهر هنا الحلقات بعد نجاح رفعها والتحقق منها."):window.AVi18n.t("ستظهر هنا نتائج المهام المكتملة أو التي تحتاج تدخلاً.")}</span></div>`;
  $("#history-filters").querySelectorAll("[data-history-filter]").forEach(button=>{const selected=button.dataset.historyFilter===state.historyFilter;button.classList.toggle("active",selected);button.setAttribute("aria-selected",String(selected));});
  const localPackages=[...ready,...completed].filter(ep=>(state.libraryFilter==="all"||ep.state===(state.libraryFilter==="ready"?"READY_TO_UPLOAD":"READY"))&&`${ep.filename||""} ${ep.work_dir||""} ${ep.settings?.series_name||""} ${ep.settings?.season_name||""} ${ep.settings?.season_number??""}`.toLocaleLowerCase().includes(state.librarySearch));
  renderLibrary(localPackages,ready.length,completed.length);

}

function renderLibrary(packages,readyCount,completedCount){
  const root=$("#library-list"),groupStates=new Map([...root.querySelectorAll(".library-season")].map(el=>[el.dataset.libraryGroup,el.open])),expanded=new Set([...root.querySelectorAll(".library-path[open]")].map(el=>el.dataset.libraryPath));
  root.classList.toggle("list-view",state.libraryView==="list");
  $("#library-summary").innerHTML=window.AVi18n.tpl`<div class="library-stat ready-stat"><span>جاهزة للرفع</span><strong>${readyCount}</strong></div><div class="library-stat"><span>مرفوعة</span><strong>${completedCount}</strong></div><div class="library-stat"><span>الحلقات المعروضة</span><strong>${packages.length}</strong></div>`;
  const groups=new Map();
  for(const ep of packages){const cfg=ep.settings||{},series=cfg.series_name||window.AVi18n.t("حزم غير مصنفة"),season=cfg.season_name||(cfg.season_number!=null?window.AVi18n.tpl`الموسم ${cfg.season_number}`:window.AVi18n.t("دون موسم مسجل")),key=JSON.stringify([series,season]);if(!groups.has(key))groups.set(key,{series,season,items:[]});groups.get(key).items.push(ep);}
  root.innerHTML=groups.size?[...groups.entries()].sort((a,b)=>b[1].items.filter(ep=>ep.state==="READY_TO_UPLOAD").length-a[1].items.filter(ep=>ep.state==="READY_TO_UPLOAD").length||a[0].localeCompare(b[0],undefined,{numeric:true})).map(([key,group])=>{
    const rows=group.items.sort((a,b)=>String(a.episode_number??a.filename).localeCompare(String(b.episode_number??b.filename),undefined,{numeric:true}));
    const ready=rows.filter(ep=>ep.state==="READY_TO_UPLOAD").length;
    return window.AVi18n.tpl`<details class="library-season" data-library-group="${esc(key)}" ${(groupStates.has(key)?groupStates.get(key):ready>0)?"open":""}><summary><span class="library-season-title"><strong dir="auto">${esc(group.series)}</strong><span dir="auto">${esc(group.season)}</span></span><span class="library-season-count">${rows.length} حلقة · ${ready} جاهزة للرفع</span></summary><div class="library-items">${rows.map(ep=>libraryItem(ep,expanded.has(String(ep.id)))).join("")}</div></details>`;
  }).join(""):window.AVi18n.tpl`<div class="empty-state"><b>لا توجد حزم تطابق العرض الحالي</b><span>تظهر الحلقات هنا بعد اكتمال التحويل. جرّب تغيير البحث أو فلتر الحالة.</span></div>`;
}
function libraryItem(ep,expanded){
  const ready=ep.state==="READY_TO_UPLOAD",when=String(ep.completed_at||ep.updated_at||"").replace("T"," ").slice(0,16),size=Number(ep.package_bytes||0);
  return window.AVi18n.tpl`<article class="library-item"><div class="library-file"><span class="library-episode">${ep.episode_number!=null&&ep.episode_number!==""?window.AVi18n.tpl`الحلقة ${esc(ep.episode_number)}`:window.AVi18n.t("حلقة")}</span><h3 dir="auto" title="${esc(ep.filename)}">${esc(ep.filename)}</h3></div><span class="state ${ready?"ready":"uploaded"}">${ready?window.AVi18n.t("جاهزة للرفع"):window.AVi18n.t("مرفوعة")}</span><span class="library-size" dir="ltr">${size?fmtBytes(size):"—"}</span><time dir="ltr">${esc(when||"—")}</time><div class="row-actions">${ready?window.AVi18n.tpl`<button class="button primary" data-row-action="upload" data-id="${esc(ep.id)}">رفع</button>`:""}<button class="button secondary" data-row-action="open" data-id="${esc(ep.id)}">فتح المجلد</button></div><details class="library-path" data-library-path="${esc(ep.id)}" ${expanded?"open":""}><summary>مسار الحزمة</summary><bdi>${esc(ep.work_dir||window.AVi18n.t("لا يوجد مسار مسجل"))}</bdi></details></article>`;
}

function historyRow(ep){
  const needsAction=ep.state==="FAILED"||ep.state==="NEEDS_REVIEW";
  const when=ep.completed_at||ep.updated_at||"";
  const date=when?String(when).replace("T"," ").slice(0,16):"—";
  const details=needsAction&&ep.error?window.AVi18n.tpl`<details class="history-error"><summary>تفاصيل المشكلة</summary><p>${esc(ep.error)}</p></details>`:"";
  return `<article class="history-row"><div class="history-filename" dir="auto" title="${esc(ep.filename)}">${esc(ep.filename)}</div><time class="ltr" title="${esc(when)}">${esc(date)}</time><span class="state ${needsAction?"failed":"ready"}">${esc(statusLabel(ep.state))}</span><div class="row-actions">${needsAction?window.AVi18n.tpl`<button class="icon-button" data-row-action="retry" data-id="${esc(ep.id)}">إعادة</button>`:window.AVi18n.tpl`<button class="icon-button" data-row-action="open" data-id="${esc(ep.id)}">فتح</button>`}</div>${details}</article>`;
}

function uploadRow(ep,expanded=false){
  const uploading=ep.state==="UPLOADING"||ep.state==="SERVER_VERIFYING";
  const verifying=ep.state==="SERVER_VERIFYING";
  const percent=Math.max(0,Math.min(100,Number(ep.progress||0)));
  const settings=ep.settings||{},path=ep.work_dir||settings.output_directory||"";
  const group=[settings.series_name,settings.season_name||settings.season_number].filter(Boolean).join(" · ");
  const when=ep.completed_at||ep.updated_at||"",date=when?String(when).replace("T"," ").slice(0,16):"—";
  const total=Number(ep.package_bytes||0),uploaded=Number(ep.uploaded_bytes||0);
  const progress=uploading?window.AVi18n.tpl`<div class="upload-row-progress"><div class="upload-progress-line"><span class="state active">${verifying?window.AVi18n.t("يتحقق الخادم"):window.AVi18n.t("جارٍ الرفع")}</span><b dir="ltr">${percent.toFixed(0)}%</b></div><div class="progress-track"><i style="width:${percent}%"></i></div><small><bdi>${esc(ep.upload_speed||"—")}</bdi><span>المتبقي <bdi>${esc(ep.upload_eta||"—")}</bdi></span></small></div>`:window.AVi18n.tpl`<span class="state ready">جاهز للرفع</span>`;
  return window.AVi18n.tpl`<article class="upload-row ${uploading?"is-uploading":""}" data-upload-id="${esc(ep.id)}"><div class="upload-file"><bdi class="upload-filename" title="${esc(ep.filename)}">${esc(ep.filename)}</bdi>${group?`<small class="upload-group" dir="auto">${esc(group)}</small>`:""}</div><div class="upload-row-status">${progress}</div><div class="upload-row-size" dir="ltr">${total?fmtBytes(total):"—"}${uploading?`<small>${fmtBytes(uploaded)} / ${fmtBytes(total)}</small>`:""}</div><time dir="ltr" title="${esc(when)}">${esc(date)}</time><div class="row-actions">${ep.state==="READY_TO_UPLOAD"?window.AVi18n.tpl`<button class="button primary" data-row-action="upload" data-id="${esc(ep.id)}">رفع</button>`:""}<button class="button secondary" data-row-action="open" data-id="${esc(ep.id)}">فتح</button></div><details class="upload-file-details" ${expanded?"open":""}><summary>المسار والتفاصيل</summary><div><bdi>${esc(path||window.AVi18n.t("مسار الحزمة غير مسجل"))}</bdi>${uploading?window.AVi18n.tpl`<span>الوقت المنقضي <bdi>${fmtDuration(ep.upload_elapsed_seconds||0)}</bdi></span>`:""}</div></details></article>`;
}

function renderConfig(){
  const cfg=state.config;$("#jobs-db-path").textContent=cfg.jobs_db_path||"—";$("#appdata-path").textContent=cfg.appdata_root||"—";$("#logs-path").textContent=cfg.logs_path||"—";$("#output-folder").value=cfg.converted_output_folder||"";
  $("#font-library-folder").value=cfg.font_library_folder||"";
  const encoderValue=["auto","cpu","gpu"].includes(cfg.default_encoder)?cfg.default_encoder:cfg.default_encoder==="libx264"?"cpu":cfg.default_encoder?"gpu":"auto";
  const defaultPreset=cfg.default_preset==="quality"?"quality":"balanced";$("#output-organization").value=cfg.output_organization||"automatic";$("#default-encoder").value=encoderValue;$$('[name="import-encoder"]').forEach(input=>input.checked=input.value===encoderValue);$("#default-preset").value=defaultPreset;$("#cpu-jobs").value=cfg.cpu_concurrent_jobs||1;$("#gpu-jobs").value=cfg.gpu_concurrent_jobs||1;
  syncImportDevice();
  $("#first-run-modal").classList.toggle("visible",!cfg.converted_output_folder);$("#first-run-modal").setAttribute("aria-hidden",String(Boolean(cfg.converted_output_folder)));
}

function renderEncoders(){
  const list=$("#encoder-list");if(!list)return;
  list.innerHTML=(state.encoders||[]).map(item=>`<article class="encoder-card ${item.available?"available":""}" title="${esc(window.AVi18n.t(item.reason)||"")}"><b>${esc(item.name)}</b><span>${item.available?window.AVi18n.t("متاح ومختبر"):window.AVi18n.t("غير متاح على هذا الجهاز")}</span></article>`).join("");
  const select=$("#benchmark-encoder");const available=state.encoders.filter(item=>item.available);select.innerHTML=available.map(item=>`<option value="${esc(item.id)}">${esc(item.name)}</option>`).join("");
}

async function refreshAll(){
  const results=await Promise.allSettled([api("/api/config"),api("/api/hardware"),api("/api/batch/current"),api("/api/server/status")]);
  if(results[0].status==="fulfilled"){state.config=results[0].value;renderConfig();}
  if(results[1].status==="fulfilled")renderHardware(results[1].value.hardware);else markTelemetryUnavailable();
  if(results[2].status==="fulfilled"){state.batch=results[2].value;renderBatch();}
  if(results[3].status==="fulfilled"){state.server=results[3].value;renderServer();}
}

async function selectOutput(){
  try{const result=await api("/api/system/select-output-folder",{method:"POST",body:"{}"});if(!result.cancelled){state.config.converted_output_folder=result.path;state.config.first_run_complete=true;renderConfig();if(state.selection){state.selection.discovery=await inspectSelection(state.selection.type,state.selection.path);renderSelection();}toast(window.AVi18n.t("تم حفظ مجلد الإخراج الدائم."));}}catch(error){toast(window.AVi18n.t(error.message),"error");}
}

async function inspectSelection(kind,path){
  const endpoint=kind==="file"?"/api/batch/inspect-file":kind==="series"?"/api/batch/inspect-series":"/api/batch/inspect-season";
  return api(endpoint,{method:"POST",body:JSON.stringify(kind==="file"?{file_path:path}:{folder_path:path})});
}
async function chooseSource(kind){
  if(state.importBusy||state.selectionBusy)return;
  state.selectionBusy=true;state.selectionError="";renderSelection();
  $$("#drop-zone button").forEach(el=>el.disabled=true);
  try{
    const picked=await api(kind==="file"?"/api/system/select-video":`/api/batch/browse-folder?kind=${kind}`,{method:"POST",body:"{}"});
    if(picked.cancelled)return;
    $("#import-preview").innerHTML=window.AVi18n.t('<div class="review-loading" role="status">جارٍ اكتشاف المواسم والحلقات والتحقق من الملفات…</div>');
    const discovery=await inspectSelection(kind,picked.path);
    state.selection={type:kind,path:picked.path,discovery};state.importExcluded=new Set();
  }catch(error){state.selectionError=window.AVi18n.t(error.message);toast(window.AVi18n.t(error.message),"error");}
  finally{state.selectionBusy=false;$$("#drop-zone button").forEach(el=>el.disabled=false);renderSelection();}
}
async function chooseFile(){return chooseSource("file");}
async function chooseFolder(kind="season"){return chooseSource(kind);}
function importEntries(){const d=state.selection?.discovery;return d?.episodes?.length?d.episodes:(d?.seasons||[]).flatMap(season=>season.episodes||[]);}
function importEntryKey(ep){return pathKey(ep.source_path||ep.relative_source_path||ep.filename);}
function selectedImportEntries(){return importEntries().filter(ep=>!state.importExcluded.has(importEntryKey(ep)));}
function importPreset(){return ["economy","balanced","quality"].includes(state.config.default_preset)?state.config.default_preset:"balanced";}
function importEncoder(){return document.querySelector('input[name="import-encoder"]:checked')?.value||"";}
function syncImportDevice(){
  $("#import-device-help").textContent={cpu:window.AVi18n.t("CPU: يستخدم المعالج لتحويل الحلقات."),gpu:window.AVi18n.t("GPU: يستخدم بطاقة الرسوميات عند توفر مشفّر مدعوم.")}[importEncoder()]||window.AVi18n.t("اختر CPU أو GPU للتحويل.");
  syncImportSelection();
}
function syncImportSelection(){
  const d=state.selection?.discovery,entries=importEntries(),selected=selectedImportEntries(),count=entries.length?selected.length:Number(d?.episode_count||0);
  const errors=d?.errors||[],valid=Boolean(state.selection)&&count>0&&!errors.length&&Boolean(importEncoder());
  const busy=state.importBusy||state.selectionBusy;
  $("#queue-selection").disabled=busy||!valid;
  const countLabel=count===1?window.AVi18n.t("حلقة واحدة محددة"):count===2?window.AVi18n.t("حلقتان محددتان"):count<=10?window.AVi18n.tpl`${count} حلقات محددة`:window.AVi18n.tpl`${count} حلقة محددة`;
  $("#import-selected-count").textContent=count?`${countLabel}${entries.length&&count!==entries.length?window.AVi18n.tpl` من ${entries.length}`:""}`:window.AVi18n.t("لم تختر حلقات بعد");
  const review=selected.filter(ep=>ep.issues?.length).length;
  $("#import-validation").textContent=busy?window.AVi18n.t("جارٍ فحص المحتوى؛ انتظر اكتمال العملية."):state.selectionError?state.selectionError:errors.length?window.AVi18n.t("تعذر التحقق من بعض الملفات؛ راجع التنبيهات."):!state.selection?window.AVi18n.t("اختر مصدرًا لعرض الحلقات والتحقق منها."):!count?window.AVi18n.t("حدد حلقة واحدة على الأقل."):!importEncoder()?window.AVi18n.t("اختر CPU أو GPU من إعدادات المهمة."):review?window.AVi18n.tpl`${review} حلقة تحتاج مراجعة؛ ستُحفظ للمراجعة دون بدء التحويل.`:window.AVi18n.t("التالي: حفظ الحلقات وفتح قسم التحويل.");
  $("#import-validation").classList.toggle("warning",Boolean(state.selectionError||errors.length||review));
  $$(".dock-fields fieldset").forEach(el=>el.disabled=busy);$$("#drop-zone button").forEach(el=>el.disabled=busy);
  $("#import-clear").disabled=busy||!state.selection;$("#import-expand-all").disabled=busy||!entries.length;
  $$("[data-import-select]").forEach(box=>{box.checked=!state.importExcluded.has(box.dataset.importSelect);box.disabled=busy;});
  $$("[data-import-season]").forEach(box=>{const group=$$("[data-import-select]",box.closest(".review-season")),n=group.filter(el=>el.checked).length;box.checked=n===group.length&&n>0;box.indeterminate=n>0&&n<group.length;box.disabled=busy;});
  const all=$("#import-select-all");if(all){all.checked=count===entries.length&&count>0;all.indeterminate=count>0&&count<entries.length;all.disabled=busy;}
}
function renderSelection(){
  const box=$("#import-preview"),sel=state.selection,d=sel?.discovery;
  const collapsed=new Set([...box.querySelectorAll('.review-season:not([open])')].map(el=>el.dataset.seasonKey));
  box.className="review-tree";box.setAttribute("aria-busy",String(Boolean(state.selectionBusy)));
  $("#import-source-label").textContent=d?.series_name||window.AVi18n.t("لم يتم اختيار مصدر");$("#import-source-label").title=sel?.path||"";
  if(!sel){box.classList.add("empty");box.innerHTML=state.selectionBusy?window.AVi18n.t('<div class="review-loading" role="status">اختر المصدر من نافذة Windows…</div>'):`<div class="review-empty"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="6" width="18" height="15" rx="2"/><path d="M8 3h8M10 11l5 3-5 3z"/></svg><div><strong>${state.selectionError?window.AVi18n.t("تعذر قراءة المصدر"):window.AVi18n.t("أضف الحلقات لبدء تجهيزها")}</strong><p>${esc(state.selectionError||window.AVi18n.t("اختر فيديو أو مجلدًا من شريط الإضافة. راجع الحلقات هنا، ثم جهّزها للتحويل."))}</p></div></div>`;syncImportDevice();return;}
  const entries=importEntries(),count=d?.episode_count||0;
  const existing=new Set((state.batch.episodes||[]).map(ep=>pathKey(ep.source_path)));
  box.innerHTML=window.AVi18n.tpl`<div class="review-overview"><label><input id="import-select-all" type="checkbox"> تحديد الكل</label><span>${count} ملف فيديو</span><span>${d?.season_count||0} موسم</span><span dir="ltr">${fmtBytes(d?.source_bytes||0)}</span>${d?.duplicate_count?window.AVi18n.tpl`<span class="warning">${d.duplicate_count} رقم مكرر</span>`:""}${d?.ambiguous_count?window.AVi18n.tpl`<span class="warning">${d.ambiguous_count} تحتاج مراجعة</span>`:""}</div>
    ${(d?.errors||[]).map(e=>`<p class="review-error">${esc(window.AVi18n.t(e.message))} <bdi>${esc(e.path)}</bdi></p>`).join("")}
    ${!count?window.AVi18n.t('<div class="review-empty"><strong>لا توجد ملفات فيديو مدعومة في المصدر المحدد.</strong></div>'):""}
    ${(d?.seasons||[]).map((season,index)=>window.AVi18n.tpl`<details class="review-season" data-season-key="${esc(String(index)+season.name)}" ${collapsed.has(String(index)+season.name)?"":"open"}><summary><input type="checkbox" data-import-season="${index}" aria-label="تحديد ${esc(season.name)}"><strong dir="auto">${esc(season.name)}</strong>${season.season_number===0?window.AVi18n.t('<span class="review-badge">حلقات خاصة</span>'):""}<span class="season-count">${season.episode_count} حلقة</span></summary><div class="review-columns"><span>الحلقة والملف</span><span>التحقق</span><span>الحجم</span><span>المسار</span></div>${(season.episodes||[]).map(ep=>window.AVi18n.tpl`<div class="review-row"><label class="review-filename"><input type="checkbox" data-import-select="${esc(importEntryKey(ep))}" aria-label="تحديد ${esc(ep.filename)}"><span class="review-number">${ep.episode_number==null?'؟':esc(ep.episode_number)}</span><bdi title="${esc(ep.filename)}">${esc(ep.filename)}</bdi></label><div class="review-flags">${ep.duplicate?window.AVi18n.t('<span class="review-badge warning">رقم مكرر</span>'):""}${existing.has(pathKey(ep.source_path))?window.AVi18n.t('<span class="review-badge">في الطابور</span>'):""}${ep.issues?.length?ep.issues.map(issue=>`<span class="review-issue" title="${esc(issue)}">${esc(issue)}</span>`).join(""):window.AVi18n.t('<span class="review-ok">جاهز للفحص</span>')}</div><span class="review-size" dir="ltr">${fmtBytes(ep.source_bytes)}</span><details class="review-path"><summary>التفاصيل</summary><div><span>المصدر</span><bdi>${esc(ep.relative_source_path)}</bdi><span>الإخراج المخطط</span><bdi>${esc(ep.output_directory||window.AVi18n.t('اختر مجلد الإخراج'))}</bdi></div></details></div>`).join("")}</details>`).join("")}
    ${d?.ignored_file_count?window.AVi18n.tpl`<p class="review-footnote">${d.ignored_file_count} ملف مساعد؛ الترجمات والخطوط تُفحص عند إضافة المهام.</p>`:""}`;
  syncImportDevice();
}

async function queueSelection(){
  if(state.importBusy||state.selectionBusy||!state.selection||$("#queue-selection").disabled)return;

  const selectedReference=state.selection,selection={...selectedReference},entries=importEntries(),chosen=selectedImportEntries(),partial=entries.length>0&&chosen.length<entries.length;state.importBusy=true;
  syncImportSelection();
  const button=$("#queue-selection"),buttonLabel=button.textContent;state.importingFolder=selection.type==="file"?null:selection.path;button.disabled=true;button.setAttribute("aria-busy","true");button.textContent=window.AVi18n.tpl`جارٍ فحص ${selection.type==="series"?window.AVi18n.t("المسلسل"):selection.type==="file"?window.AVi18n.t("الملف"):window.AVi18n.t("الموسم")} وإضافته…`;
  let importPolling=true,importStartedAt=Date.now();
  const progressPoll=(async()=>{
    while(importPolling){
      await new Promise(resolve=>setTimeout(resolve,2000));
      if(!importPolling)break;
      try{state.batch=await api("/api/batch/current");renderBatch();}catch(_progressError){}
      const queued=(state.batch.episodes||[]).filter(ep=>selection.type==="file"?pathKey(ep.source_path)===pathKey(selection.path):pathKey(ep.source_path).startsWith(`${pathKey(selection.path)}/`));
      const expected=Number(selection.discovery?.episode_count||0),elapsed=Math.floor((Date.now()-importStartedAt)/1000);
      if(queued.length){button.textContent=window.AVi18n.tpl`جارٍ الفحص والإضافة — حُفظ ${queued.length}${expected?window.AVi18n.tpl` من ${expected}`:""} حلقة · ${fmtDuration(elapsed)}`;}
      else{button.textContent=window.AVi18n.tpl`جارٍ فحص أول ملف قبل إضافته… · ${fmtDuration(elapsed)}`;}
    }
  })();
  try{
    await saveSettings({silent:true,source:"import"});
    const destination="";
    let result;
    if(partial){
      if(chosen.some(ep=>!ep.source_path||ep.issues?.length||ep.duplicate))throw new Error(window.AVi18n.t("لتحديد بعض الملفات التي تحتاج مراجعة، عالج تنبيهاتها أولًا أو أضف المصدر كاملًا للحفاظ على معلومات المراجعة."));
      for(const ep of chosen){const context=await inspectSelection("file",ep.source_path);if(context.source_root&&selection.discovery?.source_root&&pathKey(context.source_root)!==pathKey(selection.discovery.source_root))throw new Error(window.AVi18n.t("هذا المجلد لا يحدد المسلسل والموسم لكل ملف منفرد. أضف المصدر كاملًا للحفاظ على تنظيمه."));}
      let added=0,skipped=0;const saved=[];
      for(const ep of chosen){const reply=await api("/api/batch/import-file",{method:"POST",body:JSON.stringify({file_path:ep.source_path,destination_label:destination})});if(!Array.isArray(reply.episodes)||!reply.episodes.length)throw new Error(window.AVi18n.t("تعذر تأكيد حفظ الحلقة في الطابور."));saved.push(...reply.episodes);added+=Number(reply.new_episode_count??reply.episodes.length);skipped+=Number(reply.skipped_episode_count||0);button.textContent=window.AVi18n.tpl`حُفظ ${saved.length} من ${chosen.length} حلقة…`;}
      result={episodes:saved,new_episode_count:added,skipped_episode_count:skipped};
    }
    else if(selection.type==="file")result=await api("/api/batch/import-file",{method:"POST",body:JSON.stringify({file_path:selection.path,destination_label:destination})});
    else if(selection.type==="series")result=await api("/api/batch/import-series",{method:"POST",body:JSON.stringify({folder_path:selection.path,destination_label:destination,encoder_choice:importEncoder(),default_preset:importPreset()})});
    else result=await api("/api/batch/import",{method:"POST",body:JSON.stringify({folder_path:selection.path,destination_label:destination,mode:"SEQUENTIAL",encoder_choice:importEncoder(),default_preset:importPreset()})});
    if(!Array.isArray(result.episodes)||result.episodes.length===0)throw new Error(window.AVi18n.t("لم يُرجع الاستيراد أي حلقات محفوظة."));
    const queue=await api("/api/batch/current");
    const expected=Number(result.new_episode_count??result.episodes.length);
    const queuedSources=(queue.episodes||[]).filter(ep=>selection.type==="file"?pathKey(ep.source_path)===pathKey(selection.path):pathKey(ep.source_path).startsWith(`${pathKey(selection.path)}/`));
    const selectedFiles=partial?chosen.length:selection.discovery?.episode_count;
    if(!queuedSources.length)throw new Error(window.AVi18n.t("اكتمل الفحص لكن لم تظهر حلقات هذا المجلد محفوظة في قائمة الانتظار. حدّث القائمة وحاول مجدداً."));
    state.batch=queue;renderBatch();showPage("queue");
    if(state.selection===selectedReference)state.selection=null;renderSelection();const skipped=result.skipped_seasons||[];const added=expected;toast(added?window.AVi18n.tpl`تمت إضافة ${added} حلقة. انتقل الآن إلى «التحويل والرفع» واضغط «بدء تحويل هذه الدفعة»؛ لن يبدأ التحويل تلقائياً.${selectedFiles?window.AVi18n.tpl` تم فحص ${selectedFiles} ملفاً.`:""}${result.skipped_episode_count?window.AVi18n.tpl` ${result.skipped_episode_count} ملف موجود مسبقاً؛ لم يتكرر.`:""}${skipped.length?window.AVi18n.tpl` تم تجاوز مواسم مضافة مسبقاً: ${skipped.join("، ")}`:""}`:window.AVi18n.t("هذه الحلقات موجودة بالفعل في قائمة الانتظار؛ لم تتم إضافة نسخ مكررة."),added?"ok":"error");
  }catch(error){
    const detail=String(error?.message||error||window.AVi18n.t("خطأ غير معروف"));
    try{const queue=await api("/api/batch/current");const partial=(queue.episodes||[]).filter(ep=>selection.type==="file"?pathKey(ep.source_path)===pathKey(selection.path):pathKey(ep.source_path).startsWith(`${pathKey(selection.path)}/`));if(partial.length){state.batch=queue;renderBatch();showPage("queue");toast(window.AVi18n.tpl`توقف الاستيراد بعد حفظ ${partial.length} حلقة. يمكنك متابعة ما حُفظ؛ السبب: ${detail}`,"error");return;}}catch(_refreshError){}
    toast(window.AVi18n.tpl`تعذر إضافة الوسائط إلى قائمة الانتظار: ${detail}`,"error");
  }finally{importPolling=false;state.importBusy=false;state.importingFolder=null;button.textContent=buttonLabel;button.removeAttribute("aria-busy");syncImportSelection();}
}

let batchRefreshPromise=null;
async function refreshBatch(){
  if(batchRefreshPromise)return batchRefreshPromise;
  batchRefreshPromise=(async()=>{try{state.batch=await api("/api/batch/current");renderBatch();}catch(error){toast(window.AVi18n.t(error.message),"error");}})();
  try{return await batchRefreshPromise;}finally{batchRefreshPromise=null;}
}

async function batchAction(action, button=null){
  const routes={"start-all":["/api/batch/start","POST"],"start-auto-pipeline":["/api/batch/auto-pipeline/start","POST"],"pause-all":["/api/batch/pause","POST"],"resume-all":["/api/batch/resume","POST"],"stop-after-current":["/api/batch/stop-after-current","POST"],"retry-failed":["/api/batch/retry-failed","POST"],"clear-completed":["/api/batch/clear-completed","POST"]};
  const route=routes[action];if(!route)return;
  if(activeBatchActions.has(action))return;
  activeBatchActions.add(action);
  if(button){button.disabled=true;button.setAttribute("aria-busy","true");}
  try{
    const result=await api(route[0],{method:route[1],body:"{}"});
    if(action==="clear-completed"){
      const removed=Number(result.removed||0);
      toast(removed>0?window.AVi18n.tpl`حُذفت ${removed} سجلات مكتملة، وبقيت ملفات الإخراج محفوظة.`:window.AVi18n.t("لا توجد سجلات مكتملة للحذف؛ المهام غير المكتملة بقيت في قائمة الانتظار."));
    }else if(action==="start-auto-pipeline")toast(window.AVi18n.t("بدأ التحويل والرفع التلقائي. سيُرفع كل ملف بعد اكتمال تحويله."));
    else toast(window.AVi18n.t("تم تنفيذ الأمر."));
    await refreshBatch();
  }catch(error){toast(window.AVi18n.t(error.message),"error");}
  finally{activeBatchActions.delete(action);if(button){button.disabled=false;button.removeAttribute("aria-busy");}}
}

async function rowAction(action,id){
  const routes={open:[`/api/batch/episodes/${id}/open-package`,"POST"],start:[`/api/batch/episodes/${id}/start`,"POST"],upload:[`/api/batch/episodes/${id}/upload`,"POST"],retry:[`/api/batch/episodes/${id}/retry`,"POST"],cancel:[`/api/batch/episodes/${id}/cancel`,"POST"],remove:[`/api/batch/episodes/${id}`,"DELETE"]};const route=routes[action];if(!route)return;
  try{const result=await api(route[0],{method:route[1],...(route[1]==="POST"?{body:"{}"}:{})});if(action==="upload")toast(window.AVi18n.t("اكتمل رفع الحلقة والتحقق منها."));await refreshBatch();}catch(error){toast(window.AVi18n.t(error.message),"error");}
}

async function saveSettings({silent=false,source="settings"}={}){
  const fromImport=source==="import";
  const payload={
    processing_mode:fromImport?"local":(state.config.processing_mode||"local"),
    default_encoder:(fromImport?importEncoder():$("#default-encoder").value)||"auto",
    default_preset:(fromImport?importPreset():$("#default-preset").value)||"balanced",
    include_360p:true,
    output_organization:$("#output-organization").value||"automatic",
    cpu_concurrent_jobs:Number($("#cpu-jobs").value||1),gpu_concurrent_jobs:Number($("#gpu-jobs").value||1),
  };
  const result=await api("/api/config",{method:"POST",body:JSON.stringify(payload)});state.config=result.config;renderConfig();if(!silent)toast(window.AVi18n.t("تم حفظ الإعدادات."));
}

async function saveConnection(source="settings"){
  const prefix=source==="queue"?"queue-":"";
  try{const payload={server_url:$("#"+prefix+"server-url-input").value.trim()};const key=$("#"+prefix+"api-key-input").value.trim();if(key)payload.api_key=key;const result=await api("/api/config",{method:"POST",body:JSON.stringify(payload)});state.config=result.config;$("#api-key-input").value="";$("#queue-api-key-input").value="";toast(window.AVi18n.t("تم حفظ إعداد الاتصال بأمان."));await refreshServer();}catch(error){toast(window.AVi18n.t(error.message),"error");}
}

async function testConnection(source="settings"){
  const prefix=source==="queue"?"queue-":"";
  const target=$("#"+prefix+"connection-result");target.textContent=window.AVi18n.t("جارٍ الاختبار…");try{const result=await api("/api/test-connection",{method:"POST",body:JSON.stringify({server_url:$("#"+prefix+"server-url-input").value.trim(),api_key:$("#"+prefix+"api-key-input").value})});target.textContent=window.AVi18n.t(result.message);target.style.color=result.ok?"#5fe6ce":"#ff899b";}catch(error){target.textContent=window.AVi18n.t(error.message);target.style.color="#ff899b";}
}

async function refreshServer(){try{state.server=await api("/api/server/status");renderServer();}catch(error){state.server={connected:false,health:"—",readiness:"—",authenticated:false};renderServer();}}
async function loadTempInfo(){try{const info=await api("/api/system/temp-info");$("#temp-path").textContent=info.path;$("#temp-size").textContent=fmtBytes(info.size_bytes);$("#temp-files").textContent=window.AVi18n.tpl`${info.file_count} ملف`;}catch(error){toast(window.AVi18n.t(error.message),"error");}}

async function renderFontLibrary(info){
  $("#font-library-folder").value=info.root||"";
  $("#font-library-status").textContent=window.AVi18n.tpl`${info.file_count||0} ملف · ${info.family_count||0} عائلة · ${info.broken_count||0} ملف غير صالح`;
}
async function loadFontLibrary(){try{await renderFontLibrary(await api("/api/font-library"));}catch(error){$("#font-library-status").textContent=window.AVi18n.t(error.message);}}
async function selectFontLibrary(){
  try{const selected=await api("/api/system/select-font-folder",{method:"POST",body:"{}"});if(selected.cancelled)return;const saved=await api("/api/config",{method:"POST",body:JSON.stringify({font_library_folder:selected.path})});state.config=saved.config;await rescanFontLibrary();}
  catch(error){toast(window.AVi18n.t(error.message),"error");}
}
async function openFontLibrary(){try{await api("/api/system/open-font-folder",{method:"POST",body:"{}"});}catch(error){toast(window.AVi18n.t(error.message),"error");}}
async function rescanFontLibrary(){
  try{const info=await api("/api/font-library/rescan",{method:"POST",body:"{}"});await renderFontLibrary(info);const audit=await api("/api/batch/font-audits/refresh",{method:"POST",body:"{}"});await refreshBatch();toast(window.AVi18n.tpl`تم فحص ${info.file_count} ملف خط وتحديث ${audit.updated} مهمة.`);}
  catch(error){toast(window.AVi18n.t(error.message),"error");}
}

function bindEvents(){
  $("#main-nav").addEventListener("click",e=>{const item=e.target.closest("[data-page]");if(item)showPage(item.dataset.page);});
  $("#settings-tabs").addEventListener("click",e=>{const button=e.target.closest("[data-settings-tab]");if(button)setSettingsTab(button.dataset.settingsTab,true);});
  $("#settings-tabs").addEventListener("keydown",e=>{if(!["ArrowLeft","ArrowRight","Home","End"].includes(e.key))return;const tabs=$$("[data-settings-tab]");let index=tabs.findIndex(tab=>tab.dataset.settingsTab===state.settingsTab);if(e.key==="Home")index=0;else if(e.key==="End")index=tabs.length-1;else index=(index+(e.key==="ArrowRight"?1:-1)+tabs.length)%tabs.length;e.preventDefault();setSettingsTab(tabs[index].dataset.settingsTab,true);tabs[index].focus();});
  $("#history-search").addEventListener("input",e=>{state.historySearch=e.target.value.trim().toLocaleLowerCase();renderResultLists(state.batch.episodes||[]);});
  $("#library-search").addEventListener("input",e=>{state.librarySearch=e.target.value.trim().toLocaleLowerCase();renderResultLists(state.batch.episodes||[]);});
  $("#library-filter").addEventListener("change",e=>{state.libraryFilter=e.target.value;renderResultLists(state.batch.episodes||[]);});
  for(const view of ["grid","list"]){$("#library-"+view+"-view").addEventListener("click",()=>{state.libraryView=view;for(const mode of ["grid","list"]){const button=$("#library-"+mode+"-view");button.classList.toggle("active",mode===view);button.setAttribute("aria-pressed",String(mode===view));}renderResultLists(state.batch.episodes||[]);});}
  $("#history-filters").addEventListener("click",e=>{const button=e.target.closest("[data-history-filter]");if(!button)return;state.historyFilter=button.dataset.historyFilter;renderBatch();});
  $("#queue-status-filters").addEventListener("click",e=>{const button=e.target.closest("[data-queue-filter]");if(!button)return;state.queueFilter=button.dataset.queueFilter;renderBatch();});
  $("#select-all-queue").addEventListener("change",e=>{const episodes=(state.batch.episodes||[]).filter(ep=>ep.state!=="READY"&&queueFilterMatch(ep));if(e.target.checked)episodes.filter(isQueueSelectable).forEach(ep=>state.selectedEpisodeIds.add(String(ep.id)));else episodes.forEach(ep=>state.selectedEpisodeIds.delete(String(ep.id)));renderBatch();});
  $("#delete-selected-queue").addEventListener("click",deleteSelectedQueue);
  $("#queue-body").addEventListener("click",e=>{const button=e.target.closest("[data-toggle-details]");if(!button)return;const id=button.dataset.toggleDetails,row=$("#queue-body").querySelector(`[data-details="${CSS.escape(id)}"]`);if(!row)return;const expanded=button.getAttribute("aria-expanded")==="true";button.setAttribute("aria-expanded",String(!expanded));button.textContent=expanded?"＋":"−";row.classList.toggle("hidden",expanded);if(expanded)state.openEpisodeDetails.delete(id);else state.openEpisodeDetails.add(id);});
  document.addEventListener("click",e=>{const go=e.target.closest("[data-go]");if(go)showPage(go.dataset.go);const action=e.target.closest("[data-action]");if(action)batchAction(action.dataset.action,action);const row=e.target.closest("[data-row-action]");if(row)rowAction(row.dataset.rowAction,row.dataset.id);});
  document.addEventListener("click",e=>{const button=e.target.closest("[data-workspace-tab]");if(button)setQueueWorkspace(button.dataset.workspaceTab);});
  $(".queue-workspace-tabs").addEventListener("keydown",e=>{if(!["ArrowLeft","ArrowRight","Home","End"].includes(e.key))return;e.preventDefault();const name=e.key==="Home"?"processing":e.key==="End"?"upload":state.queueWorkspace==="upload"?"processing":"upload";setQueueWorkspace(name);$("#"+(name==="upload"?"upload-tab":"processing-tab")).focus();});
  $("#import-preview").addEventListener("change",e=>{
    if(state.importBusy||state.selectionBusy)return;
    const box=e.target;if(box.id==="import-select-all"){state.importExcluded=box.checked?new Set():new Set(importEntries().map(importEntryKey));}
    else if(box.matches("[data-import-season]")){for(const item of $$("[data-import-select]",box.closest(".review-season"))){if(box.checked)state.importExcluded.delete(item.dataset.importSelect);else state.importExcluded.add(item.dataset.importSelect);}}
    else if(box.matches("[data-import-select]")){if(box.checked)state.importExcluded.delete(box.dataset.importSelect);else state.importExcluded.add(box.dataset.importSelect);}else return;
    syncImportSelection();
  });
  $("#import-preview").addEventListener("click",e=>{if(e.target.matches("[data-import-season]"))e.stopPropagation();});
  $("#import-clear").addEventListener("click",()=>{if(state.importBusy||state.selectionBusy)return;state.selection=null;state.selectionError="";state.importExcluded.clear();renderSelection();});
  $("#import-expand-all").addEventListener("click",()=>{const groups=$$(".review-season"),open=groups.some(el=>!el.open);groups.forEach(el=>el.open=open);$("#import-expand-all").textContent=open?window.AVi18n.t("طيّ المواسم"):window.AVi18n.t("توسيع المواسم");});
  $$('input[name="import-encoder"]').forEach(input=>input.addEventListener("change",syncImportDevice));
  $("#refresh-button").addEventListener("click",refreshAll);$("#add-file").addEventListener("click",chooseFile);$("#add-season").addEventListener("click",()=>chooseFolder("season"));$("#add-series").addEventListener("click",()=>chooseFolder("series"));$("#queue-selection").addEventListener("click",queueSelection);
  $("#browse-output").addEventListener("click",selectOutput);$("#first-run-browse").addEventListener("click",selectOutput);$("#open-output").addEventListener("click",()=>api("/api/system/open-output-folder",{method:"POST",body:"{}"}).catch(error=>toast(window.AVi18n.t(error.message),"error")));$("#settings-open-output").addEventListener("click",()=>$("#open-output").click());
  $("#browse-font-library").addEventListener("click",selectFontLibrary);$("#open-font-library").addEventListener("click",openFontLibrary);$("#rescan-font-library").addEventListener("click",rescanFontLibrary);
  $("#save-settings").addEventListener("click",()=>saveSettings().catch(error=>toast(window.AVi18n.t(error.message),"error")));$("#save-connection").addEventListener("click",()=>saveConnection("settings"));$("#test-connection").addEventListener("click",()=>testConnection("settings"));$("#queue-save-connection").addEventListener("click",()=>saveConnection("queue"));$("#queue-test-connection").addEventListener("click",()=>testConnection("queue"));
  $("#upload-ready").addEventListener("click",async event=>{const button=event.currentTarget,label=button.textContent;button.disabled=true;button.textContent=window.AVi18n.t("جارٍ بدء الرفع…");try{const result=await api("/api/batch/upload-ready",{method:"POST",body:"{}"});await refreshBatch();toast(result.status==="ALREADY_RUNNING"?window.AVi18n.t("يوجد رفع جارٍ بالفعل."):window.AVi18n.t("بدأ رفع الحزم الجاهزة."));}catch(error){toast(window.AVi18n.t(error.message),"error");}finally{button.textContent=label;button.disabled=!((state.batch.episodes||[]).some(ep=>ep.state==="READY_TO_UPLOAD"));}});
  $("#refresh-encoders").addEventListener("click",async()=>{try{const data=await api("/api/encoders?force_refresh=true");state.encoders=data.encoders;renderEncoders();renderBatch();toast(window.AVi18n.t("اكتمل فحص المشفّرات."));}catch(error){toast(window.AVi18n.t(error.message),"error");}});
  $("#test-encoder").addEventListener("click",async()=>{const result=$("#benchmark-result");result.textContent=window.AVi18n.t("جارٍ تشغيل اختبار قصير…");try{const data=await api("/api/encoder/test",{method:"POST",body:JSON.stringify({encoder:$("#benchmark-encoder").value})});result.textContent=`${data.encoder_name} · ${data.fps} FPS · ${data.speed_multiplier}x · PASS`;}catch(error){result.textContent=window.AVi18n.t(error.message);}});
  $("#clear-temp").addEventListener("click",async()=>{if(!confirm(window.AVi18n.t("سيُحذف محتوى مجلد الملفات المؤقتة فقط. هل تريد المتابعة؟")))return;try{const data=await api("/api/system/clear-temp",{method:"POST",body:JSON.stringify({confirmed:true})});toast(window.AVi18n.tpl`حُذف ${data.removed_files} ملف مؤقت.`);loadTempInfo();}catch(error){toast(window.AVi18n.t(error.message),"error");}});
  $("#queue-body").addEventListener("change",async e=>{if(e.target.matches(".queue-episode-checkbox")){const id=String(e.target.dataset.id);if(e.target.checked)state.selectedEpisodeIds.add(id);else state.selectedEpisodeIds.delete(id);syncQueueSelection();return;}if(e.target.matches(".row-encoder")){try{await api(`/api/batch/episodes/${e.target.dataset.id}/encoder`,{method:"POST",body:JSON.stringify({encoder:e.target.value})});toast(window.AVi18n.t("تم تحديث مشفر المهمة."));await refreshBatch();}catch(error){toast(window.AVi18n.t(error.message),"error");}return;}if(!e.target.matches(".row-preset"))return;try{await api(`/api/batch/episodes/${e.target.dataset.id}/preset`,{method:"POST",body:JSON.stringify({preset:e.target.value})});toast(window.AVi18n.t("تم تحديث جودة المهمة وتقدير حجمها."));await refreshBatch();}catch(error){toast(window.AVi18n.t(error.message),"error");}});
  const drop=$("#drop-zone");["dragenter","dragover"].forEach(name=>drop.addEventListener(name,e=>{e.preventDefault();drop.classList.add("drag");}));["dragleave","drop"].forEach(name=>drop.addEventListener(name,e=>{e.preventDefault();drop.classList.remove("drag");}));drop.addEventListener("drop",()=>toast(window.AVi18n.t("استخدم زر إضافة ملف لضمان الحصول على المسار الكامل بأمان.")));
}

function connectTelemetry(){
  const scheme=location.protocol==="https:"?"wss":"ws";const socket=new WebSocket(`${scheme}://${location.host}/api/ws/telemetry`);
  socket.onmessage=event=>{try{const payload=JSON.parse(event.data);renderHardware(payload.hardware);if(payload.batch){state.batch={...state.batch,...payload.batch};if(payload.batch.episodes)renderBatch();}const live=payload.batch;if(live?.episode_id&&live.episode){const episodes=state.batch.episodes||[];const index=episodes.findIndex(ep=>ep.id===live.episode_id);if(index>=0){episodes[index]={...episodes[index],...live.episode,...(live.telemetry||{})};renderBatch();}}}catch(_){}};
  socket.onclose=()=>{markTelemetryUnavailable();setTimeout(connectTelemetry,3000);};
}

// HTTP snapshots reconcile terminal states even when the last WebSocket tick
// is missed. refreshBatch coalesces slow requests instead of stacking polls.
document.addEventListener("DOMContentLoaded",async()=>{setSettingsTab("general");bindEvents();await refreshAll();renderSelection();connectTelemetry();setInterval(refreshBatch,1000);setInterval(refreshServer,30000);});
