window.AVi18n ||= {t: value => value, tpl: (parts, ...values) => parts.reduce((out, part, i) => out + part + (i < values.length ? values[i] : ''), '')};
/**
 * ABOODVIDEO Admin — Professional Multi-Video Uploader
 * Concurrency control (default 2), Resumable chunking, Folder destination,
 * Contextual cancellation, and Real-time telemetry.
 */

document.addEventListener('DOMContentLoaded', () => {
    if (window.location.hash === '#transcoding' && window.parent === window) {
        window.location.replace('/admin/settings#encoder');
        return;
    }
    // --- CSRF Token Helper ---
    function getCsrfToken() {
        const cookies = document.cookie.split(';');
        for (let c of cookies) {
            const [k, v] = c.trim().split('=');
            if (k === (document.body.dataset.csrfCookieName || 'aboodvideo_csrf')) return v;
        }
        return '';
    }

    // --- Format Helpers ---
    function formatBytes(bytes) {
        if (!bytes || bytes <= 0) return '0 B';
        const k = 1024;
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        const val = (bytes / Math.pow(k, i)).toFixed(i >= 3 ? 1 : 0);
        return `${val} ${sizes[i]}`;
    }

    function formatSpeed(bytesPerSec) {
        if (!bytesPerSec || bytesPerSec <= 0) return '0 MB/s';
        if (bytesPerSec >= 1024 * 1024) {
            return `${(bytesPerSec / (1024 * 1024)).toFixed(1)} MB/s`;
        }
        return `${(bytesPerSec / 1024).toFixed(0)} KB/s`;
    }

    function formatEta(seconds) {
        if (!seconds || !Number.isFinite(seconds) || seconds <= 0) return '--:--';
        const s = Math.round(seconds);
        if (s < 60) return window.AVi18n.tpl`متبقي ${s} ثانية`;
        const m = Math.floor(s / 60);
        const remS = s % 60;
        if (m < 60) return window.AVi18n.tpl`متبقي ${m}د ${remS}ث`;
        const h = Math.floor(m / 60);
        return window.AVi18n.tpl`متبقي ${h}س ${m % 60}د`;
    }

    function cleanTitle(filename) {
        return filename.replace(/\.[^/.]+$/, '').replace(/[-_]/g, ' ').trim();
    }

    function naturalCompare(a, b) {
        return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
    }

    function detectEpisodeNumber(filename) {
        const ascii = String(filename || '').replace(/[٠-٩]/g, digit => String('٠١٢٣٤٥٦٧٨٩'.indexOf(digit)));
        const stem = ascii.replace(/\.[^/.]+$/, '');
        const patterns = [
            /s\s*\d{1,4}\s*[-._ ]*e\s*(\d{1,5})/i,
            /s\s*\d{1,4}[-._ ]+(\d{1,5})/i,
            /(?:\bep(?:isode)?\s*|الحلقة\s*|ح\s*)(\d{1,5})/i,
            /^\s*(\d{1,5})(?:[\s._-]+|$)/i,
            /(?:^|[\s._-])(?:e|ep)(\d{1,5})(?=$|[\s._-])/i,
        ];
        for (const pattern of patterns) {
            const match = stem.match(pattern);
            if (match) {
                const number = Number(match[1]);
                if (Number.isSafeInteger(number) && number > 0 && number <= 99999) return number;
            }
        }
        return null;
    }

    // --- Supported Video Extensions ---
    const SUPPORTED_EXTS = new Set(['mkv', 'mp4', 'mov', 'ts', 'webm', 'm4v', 'avi', 'flv']);

    function isVideoFile(file) {
        if (file.type && file.type.startsWith('video/')) return true;
        const ext = file.name.split('.').pop().toLowerCase();
        return SUPPORTED_EXTS.has(ext);
    }

    // --- DOM Elements ---
    const dropzoneSection = document.getElementById('dropzone-section');
    const dropzone = document.getElementById('dropzone');
    const btnPickFiles = document.getElementById('btn-pick-files');
    const btnPickFolder = document.getElementById('btn-pick-folder');
    const fileInputFiles = document.getElementById('file-input-files');
    const fileInputFolder = document.getElementById('file-input-folder');
    const seasonUploadHint = document.getElementById('season-upload-hint');

    const destinationCard = document.getElementById('destination-card');
    const destinationPath = document.getElementById('destination-path');
    const btnChangeDest = document.getElementById('btn-change-destination');

    const queueSection = document.getElementById('queue-section');
    const queueList = document.getElementById('queue-list');
    const queueCountBadge = document.getElementById('queue-count-badge');
    const queueSizeBadge = document.getElementById('queue-size-badge');
    const queueStatusSummary = document.getElementById('queue-status-summary');
    const summaryCompletedNum = document.getElementById('summary-completed-num');
    const summaryActiveNum = document.getElementById('summary-active-num');
    const summaryQueuedNum = document.getElementById('summary-queued-num');

    const btnAddMore = document.getElementById('btn-add-more');
    const btnClearQueue = document.getElementById('btn-clear-queue');
    const btnStartBatch = document.getElementById('btn-start-batch');
    const btnStartBatchLabel = document.getElementById('btn-start-batch-label');
    const btnCancelBatch = document.getElementById('btn-cancel-batch');
    const linkProcessingQueue = document.getElementById('link-processing-queue');

    const batchProgressCard = document.getElementById('batch-progress-card');
    const batchDoneCount = document.getElementById('batch-done-count');
    const batchTotalFiles = document.getElementById('batch-total-files');
    const batchOverallPct = document.getElementById('batch-overall-pct');
    const batchProgressBarFill = document.getElementById('batch-progress-bar-fill');
    const batchTelemetrySpeed = document.getElementById('batch-telemetry-speed');
    const batchTelemetryEta = document.getElementById('batch-telemetry-eta');
    const batchTelemetryBytes = document.getElementById('batch-telemetry-bytes');

    // Modals
    const modalDest = document.getElementById('modal-destination');
    const btnCloseDestModal = document.getElementById('btn-close-dest-modal');
    const btnCancelDestModal = document.getElementById('btn-cancel-dest-modal');
    const btnConfirmDestModal = document.getElementById('btn-confirm-dest-modal');
    const folderTreeList = document.getElementById('folder-tree-list');

    const modalConfirmItem = document.getElementById('modal-confirm-item');
    const cancelItemFilename = document.getElementById('cancel-item-filename');
    const btnCancelItemNo = document.getElementById('btn-cancel-item-no');
    const btnCancelItemYes = document.getElementById('btn-cancel-item-yes');

    const modalConfirmBatch = document.getElementById('modal-confirm-batch');
    const btnCancelBatchNo = document.getElementById('btn-cancel-batch-no');
    const btnCancelBatchYes = document.getElementById('btn-cancel-batch-yes');

    // --- State Variables ---
    let concurrencyLimit = 2; // Runtime setting from owner/admin transfer policy
    let uploadLimitKbps = 0;  // Per-file pacing for the official browser uploader
    let transferPolicyLoaded = false;
    const CHUNK_SIZE = 5242880;  // 5 MiB standard chunk

    const transferPolicyReady = fetch('/api/v1/admin/transfer-settings', { credentials: 'include', cache: 'no-store' })
        .then(response => response.ok ? response.json() : null)
        .then(policy => {
            if (!policy) return;
            concurrencyLimit = Math.max(1, Math.min(8, Number(policy.concurrent_uploads) || 2));
            uploadLimitKbps = Math.max(0, Number(policy.upload_kbps) || 0);
        }).catch(() => {}).then(() => {
            transferPolicyLoaded = true;
            if (isBatchRunning) scheduleNextUpload();
        });

    // The selected destination supplies series/season metadata; no duplicate form.
    const rootDestination = () => ({ id: 'root', type: 'root', name: window.AVi18n.t('مكتبتي'), folderId: null, seriesId: null, seasonId: null, seriesName: null, seasonNumber: null, breadcrumbs: [{ id: 'root', type: 'root', name: window.AVi18n.t('مكتبتي') }] });
    let currentFolder = rootDestination();
    let selectedFolderInModal = rootDestination();
    let destinationReady = Promise.resolve();
    let destinationError = false;
    let destinationRequest = 0;
    let pickerRequest = 0;
    let queue = [];
    let activeUploadsCount = 0;
    let isBatchRunning = false;
    let itemPendingCancel = null;

    let uploadEncoderDefaults = null;
    let uploadEncoderLoad = null;
    const processingControls = document.getElementById('upload-processing-controls');
    const encoderProfile = document.getElementById('upload-encoder-profile');
    const encoderRetry = document.getElementById('upload-encoder-retry');
    const outputOptions = document.getElementById('upload-output-options');
    const processingFeedback = document.getElementById('upload-processing-feedback');
    const uploadProfiles = { fast: { preset: 'veryfast', video_bitrate_percent: 100 }, balanced: { preset: 'medium', video_bitrate_percent: 100 }, compact: { preset: 'slow', video_bitrate_percent: 75 } };

    function currentProcessingChoice() {
        if (!uploadEncoderDefaults) return null;
        const policy = encoderProfile.value === 'saved' ? uploadEncoderDefaults : uploadProfiles[encoderProfile.value];
        const enabled = Array.from(outputOptions.querySelectorAll('input')).filter(input => input.checked && !input.disabled).map(input => input.value);
        if (!policy || !enabled.length) return null;
        return { options: { preset: policy.preset, video_bitrate_percent: policy.video_bitrate_percent, enabled_qualities: enabled } };
    }

    function updateProcessingChoice() {
        const choice = currentProcessingChoice();
        if (choice) processingFeedback.textContent = window.AVi18n.tpl`${choice.options.preset} · ${choice.options.video_bitrate_percent}% · ${choice.options.enabled_qualities.join(' / ')} — الرفع ثم التحويل تلقائيًا بهذا الإعداد`;
        else if (uploadEncoderDefaults) processingFeedback.textContent = window.AVi18n.t('اختر دقة إخراج واحدة على الأقل.');
        updateQueueHeaderStats();
    }

    async function loadUploadEncoder() {
        if (uploadEncoderDefaults) return;
        if (uploadEncoderLoad) return uploadEncoderLoad;
        encoderRetry.classList.add('hidden');
        processingFeedback.textContent = window.AVi18n.t('جارٍ تحميل إعداد الإنكودر…');
        uploadEncoderLoad = (async () => {
            try {
                const response = await fetch('/api/v1/admin/quality-settings/encoder', { credentials: 'include', cache: 'no-store' });
                if (!response.ok) throw new Error();
                const data = await response.json();
                outputOptions.replaceChildren();
                for (const quality of data.available_qualities) {
                    const label = document.createElement('label');
                    const input = document.createElement('input'); input.type = 'checkbox'; input.value = quality;
                    input.disabled = quality === '2160p' && !data.four_k_allowed;
                    input.checked = !input.disabled && data.enabled_qualities.includes(quality);
                    const text = document.createElement('span'); text.textContent = quality;
                    label.append(input, text); outputOptions.appendChild(label);
                }
                uploadEncoderDefaults = data; encoderProfile.disabled = false;
                encoderProfile.querySelector('[value="saved"]').textContent = window.AVi18n.tpl`المحفوظ — ${data.preset} · ${data.video_bitrate_percent}%`;
                updateProcessingChoice();
            } catch (_) {
                processingFeedback.textContent = window.AVi18n.t('تعذر تحميل إعداد الإنكودر. أعد المحاولة قبل بدء الرفع.');
                encoderRetry.classList.remove('hidden');
                updateQueueHeaderStats();
            } finally { uploadEncoderLoad = null; }
        })();
        return uploadEncoderLoad;
    }
    encoderRetry.addEventListener('click', loadUploadEncoder);
    encoderProfile.addEventListener('change', updateProcessingChoice);
    outputOptions.addEventListener('change', updateProcessingChoice);


    function destinationFromData(data, id, type) {
        const scope = data.current_scope;
        if (!scope || !['root', 'folder', 'series', 'season'].includes(scope.type) || (type !== 'root' && (scope.id !== id || scope.type !== type)) || (type !== 'root' && !scope.name)) {
            throw new Error(window.AVi18n.t('وجهة الرفع غير متاحة. اختر وجهة أخرى.'));
        }
        const breadcrumbs = data.breadcrumbs || [];
        const folder = [...breadcrumbs].reverse().find(crumb => crumb.type === 'folder');
        if (scope.type === 'season' && (!scope.series_id || !Number.isInteger(Number(scope.season_number)))) throw new Error(window.AVi18n.t('تعذر قراءة بيانات الموسم.'));
        return {
            id: scope.id, type: scope.type, name: scope.name,
            folderId: scope.type === 'folder' ? scope.id : (folder?.id || null),
            seriesId: scope.series_id || (scope.type === 'series' ? scope.id : null),
            seasonId: scope.type === 'season' ? scope.id : null,
            seriesName: scope.series_name || null,
            seasonNumber: scope.type === 'season' ? Number(scope.season_number) : null,
            breadcrumbs,
        };
    }

    async function fetchDestination(id, type) {
        const params = new URLSearchParams({ type });
        if (type !== 'root') params.set('id', id);
        const response = await fetch(`/api/v1/drive/scope?${params}`, { credentials: 'include', cache: 'no-store' });
        if (!response.ok) throw new Error(window.AVi18n.t('تعذر تحميل وجهة الرفع. اختر الوجهة مجددًا.'));
        const data = await response.json();
        return { data, destination: destinationFromData(data, id, type) };
    }

    function applyDestination(destination) {
        currentFolder = destination;
        destinationError = false;
        if (destinationPath) destinationPath.textContent = destination.breadcrumbs.map(crumb => crumb.name).join(' / ') || destination.name;
        if (seasonUploadHint) {
            seasonUploadHint.textContent = destination.seasonNumber
                ? window.AVi18n.t('سيُربط كل ملف بهذا الموسم تلقائيًا، مع استخراج رقم الحلقة من اسمه.')
                : '';
            seasonUploadHint.classList.toggle('hidden', !destination.seasonNumber);
        }
        renderQueue();
    }

    function selectDestination(id = 'root', type = 'root') {
        const request = ++destinationRequest;
        btnStartBatch.disabled = true;
        destinationError = true;
        destinationReady = (type === 'root' ? Promise.resolve(rootDestination()) : fetchDestination(id, type).then(result => result.destination))
            .then(destination => {
                if (request !== destinationRequest) return;
                if (destination.type === 'series') {
                    loadFolderTree(destination);
                    modalDest?.classList.add('is-open');
                    modalDest?.setAttribute('aria-hidden', 'false');
                    throw new Error(window.AVi18n.t('اختر موسمًا داخل المسلسل لرفع الحلقات.'));
                }
                applyDestination(destination);
            }).catch(error => {
                if (request !== destinationRequest) return;
                destinationError = true;
                if (destinationPath) destinationPath.textContent = window.AVi18n.t(error.message);
                seasonUploadHint?.classList.add('hidden');
            }).finally(() => {
                if (request === destinationRequest) btnStartBatch.disabled = destinationError;
            });
        return destinationReady;
    }

    function initDestinationFromUrl() {
        const params = new URLSearchParams(window.location.search);
        const type = params.has('season_id') ? 'season' : params.has('series_id') ? 'series' : params.has('folder_id') ? 'folder' : params.get('scope_type') || 'root';
        const id = params.get('season_id') || params.get('series_id') || params.get('folder_id') || params.get('scope_id') || 'root';
        // Preserve links from the existing legacy series detail page.
        const legacySeason = Number(params.get('season'));
        if (type === 'root' && params.get('series')?.trim() && Number.isInteger(legacySeason) && legacySeason > 0 && legacySeason <= 9999) {
            const destination = rootDestination();
            destination.seriesName = params.get('series').trim();
            destination.seasonNumber = legacySeason;
            destination.breadcrumbs.push({ name: destination.seriesName }, { name: window.AVi18n.tpl`الموسم ${legacySeason}` });
            applyDestination(destination);
            return Promise.resolve();
        }
        return selectDestination(id, type);
    }

    function closeDestModal() {
        modalDest?.classList.remove('is-open');
        modalDest?.setAttribute('aria-hidden', 'true');
    }
    btnChangeDest?.addEventListener('click', () => {
        loadFolderTree(currentFolder);
        modalDest?.classList.add('is-open');
        modalDest?.setAttribute('aria-hidden', 'false');
    });
    btnCloseDestModal?.addEventListener('click', closeDestModal);
    btnCancelDestModal?.addEventListener('click', closeDestModal);
    btnConfirmDestModal?.addEventListener('click', () => {
        if (btnConfirmDestModal.disabled) return;
        selectDestination(selectedFolderInModal.id, selectedFolderInModal.type);
        closeDestModal();
    });

    async function loadFolderTree(target) {
        const request = ++pickerRequest;
        btnConfirmDestModal.disabled = true;
        folderTreeList.textContent = window.AVi18n.t('جارٍ تحميل الوجهات…');
        try {
            const { data, destination } = await fetchDestination(target.id, target.type);
            if (request !== pickerRequest) return;
            selectedFolderInModal = destination;
            const path = document.getElementById('destination-picker-path');
            path.replaceChildren();
            destination.breadcrumbs.forEach(crumb => {
                const button = document.createElement('button');
                button.type = 'button'; button.className = 'btn btn-secondary btn-sm';
                button.textContent = crumb.name;
                button.addEventListener('click', () => loadFolderTree({ id: crumb.id, type: crumb.type || 'root' }));
                path.appendChild(button);
            });
            folderTreeList.replaceChildren();
            const children = [...(data.folders || []).map(node => ({ ...node, type: 'folder' })), ...(data.series || []).map(node => ({ ...node, type: 'series' })), ...(data.seasons || []).map(node => ({ ...node, type: 'season' }))];
            for (const node of children) {
                const button = document.createElement('button');
                button.type = 'button'; button.className = 'folder-picker-item upload-destination-option';
                button.textContent = node.name || node.title;
                button.addEventListener('click', () => {
                    if (node.type === 'season') {
                        selectDestination(node.id, 'season');
                        closeDestModal();
                    } else loadFolderTree(node);
                });
                folderTreeList.appendChild(button);
            }
            if (!children.length) folderTreeList.textContent = destination.type === 'season' ? window.AVi18n.t('ستُرفع الملفات إلى هذا الموسم تلقائيًا.') : window.AVi18n.t('لا توجد وجهات فرعية.');
            if (destination.type === 'series') {
                btnConfirmDestModal.textContent = window.AVi18n.t('اختر موسمًا');
            } else {
                btnConfirmDestModal.textContent = destination.type === 'season' ? window.AVi18n.t('اختيار الموسم') : window.AVi18n.t('اختيار المجلد');
                btnConfirmDestModal.disabled = false;
            }
        } catch (error) {
            if (request === pickerRequest) folderTreeList.textContent = window.AVi18n.t(error.message);
        }
    }

    window.addEventListener('message', event => {
        if (window.parent === window || event.source !== window.parent || event.origin !== window.location.origin || event.data?.type !== 'abv-upload-destination') return;
        // Reopening the drawer must preserve files and their existing destination.
        if (queue.some(item => !['COMPLETED', 'CANCELLED'].includes(item.status))) return;
        const { id, scopeType: type } = event.data;
        if (['root', 'folder', 'series', 'season'].includes(type) && typeof id === 'string') selectDestination(id, type);
    });

    // --- File & Folder Picker Events ---
    btnPickFiles.addEventListener('click', () => fileInputFiles.click());
    btnPickFolder.addEventListener('click', () => fileInputFolder.click());
    btnAddMore.addEventListener('click', () => fileInputFiles.click());

    fileInputFiles.addEventListener('change', (e) => {
        if (e.target.files && e.target.files.length > 0) {
            addFilesToQueue(Array.from(e.target.files));
            fileInputFiles.value = '';
        }
    });

    fileInputFolder.addEventListener('change', (e) => {
        if (e.target.files && e.target.files.length > 0) {
            addFilesToQueue(Array.from(e.target.files));
            fileInputFolder.value = '';
        }
    });

    // --- Drag and Drop Handling (Files & Directories) ---
    dropzone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropzone.classList.add('dragover');
    });

    dropzone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        dropzone.classList.remove('dragover');
    });

    dropzone.addEventListener('drop', async (e) => {
        e.preventDefault();
        dropzone.classList.remove('dragover');

        const items = e.dataTransfer.items;
        if (items && items.length > 0 && typeof items[0].webkitGetAsEntry === 'function') {
            const files = [];
            const queueScan = [];
            for (let i = 0; i < items.length; i++) {
                const entry = items[i].webkitGetAsEntry();
                if (entry) queueScan.push(scanFileSystemEntry(entry));
            }
            const results = await Promise.all(queueScan);
            results.forEach(arr => files.push(...arr));
            if (files.length > 0) addFilesToQueue(files);
        } else if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
            addFilesToQueue(Array.from(e.dataTransfer.files));
        }
    });

    async function scanFileSystemEntry(entry) {
        if (entry.isFile) {
            return new Promise(resolve => {
                entry.file(file => resolve([file]), () => resolve([]));
            });
        }
        if (entry.isDirectory) {
            const reader = entry.createReader();
            const entries = await new Promise(resolve => {
                reader.readEntries(res => resolve(res), () => resolve([]));
            });
            const subFiles = [];
            for (let subEntry of entries) {
                const scanned = await scanFileSystemEntry(subEntry);
                subFiles.push(...scanned);
            }
            return subFiles;
        }
        return [];
    }

    // --- Queue Management ---
    function addFilesToQueue(incomingFiles) {
        // Filter video files
        const validVideos = incomingFiles.filter(isVideoFile);

        if (validVideos.length === 0) {
            alert(window.AVi18n.t('لم يتم العثور على ملفات فيديو مدعومة (MKV, MP4, MOV, TS, WEBM).'));
            return;
        }

        // Natural sort by filename so episodes are queued in proper sequence
        validVideos.sort((a, b) => naturalCompare(a.name, b.name));

        // Deduplicate against existing items in queue
        const existingKeys = new Set(queue.map(q => `${q.filename}__${q.size}`));
        let addedCount = 0;

        validVideos.forEach(file => {
            const key = `${file.name}__${file.size}`;
            if (existingKeys.has(key)) return;
            existingKeys.add(key);

            const item = {
                id: 'item_' + Date.now() + '_' + Math.random().toString(36).substr(2, 6),
                file: file,
                filename: file.name,
                size: file.size,
                status: 'READY', // READY, QUEUED, UPLOADING, PAUSED, FINALIZING, COMPLETED, FAILED, CANCELLED
                uploadId: null,
                videoId: null,
                episodeNumber: detectEpisodeNumber(file.name),
                titleOverride: '',
                processingWarning: '',
                progressPct: 0,
                bytesUploaded: 0,
                speedBytesPerSec: 0,
                etaSeconds: 0,
                errorMessage: null,
                totalChunks: Math.ceil(file.size / CHUNK_SIZE),
                currentChunk: 0,
                isPaused: false,
                isCancelled: false,
                startTime: 0,
                lastTime: 0,
                lastBytes: 0,
                domEl: null
            };

            queue.push(item);
            addedCount++;
        });

        if (addedCount > 0) {
            renderQueue();
            queueSection.classList.remove('hidden');
            loadUploadEncoder();
        }
    }

    function renderQueue() {
        queueList.innerHTML = '';
        queue.forEach(item => {
            const el = createItemElement(item);
            item.domEl = el;
            queueList.appendChild(el);
        });

        updateQueueHeaderStats();
        updateBatchProgress();
    }

    function updateQueueHeaderStats() {
        if (btnChangeDest) btnChangeDest.disabled = isBatchRunning;
        processingControls.disabled = isBatchRunning;
        const totalFiles = queue.length;
        processingControls.classList.toggle('hidden', totalFiles === 0);
        const totalBytes = queue.reduce((sum, item) => sum + item.size, 0);

        queueCountBadge.textContent = `${totalFiles} ${totalFiles === 1 ? window.AVi18n.t('ملف') : (totalFiles <= 10 ? window.AVi18n.t('ملفات') : window.AVi18n.t('ملفاً'))}`;
        queueSizeBadge.textContent = formatBytes(totalBytes);

        const completedCount = queue.filter(q => q.status === 'COMPLETED').length;
        const activeCount = queue.filter(q => q.status === 'UPLOADING' || q.status === 'FINALIZING').length;
        const queuedCount = queue.filter(q => q.status === 'QUEUED' || q.status === 'READY').length;
        if (window.parent !== window) {
            window.parent.postMessage({ type: 'abv-upload-activity', active: activeCount }, window.location.origin);
        }

        summaryCompletedNum.textContent = completedCount;
        summaryActiveNum.textContent = activeCount;
        summaryQueuedNum.textContent = queuedCount;

        if (completedCount > 0 || activeCount > 0) {
            queueStatusSummary.classList.remove('hidden');
        } else {
            queueStatusSummary.classList.add('hidden');
        }

        // Show/hide Processing Queue button
        if (queue.some(item => item.transcodeQueued)) {
            linkProcessingQueue.classList.remove('hidden');
        } else {
            linkProcessingQueue.classList.add('hidden');
        }

        // Start / Cancel Batch button states
        if (isBatchRunning) {
            btnStartBatch.classList.add('hidden');
            btnCancelBatch.classList.remove('hidden');
            batchProgressCard.classList.remove('hidden');
        } else {
            btnStartBatch.classList.remove('hidden');
            btnCancelBatch.classList.add('hidden');
            if (activeCount === 0 && completedCount === 0) {
                batchProgressCard.classList.add('hidden');
            } else if (completedCount > 0) {
                batchProgressCard.classList.remove('hidden');
            }
        }

        const choice = currentProcessingChoice();
        btnStartBatch.disabled = destinationError || !choice || queuedCount === 0;
        btnStartBatchLabel.textContent = !uploadEncoderDefaults ? window.AVi18n.t('تحميل إعدادات الإنكودر…') : !choice ? window.AVi18n.t('اختر دقة إخراج') : window.AVi18n.tpl`بدء رفع وتحويل ${queuedCount} ملفات`;
    }

    function createItemElement(item) {
        const el = document.createElement('div');
        el.className = `upload-item item-status-${item.status.toLowerCase()}`;
        el.id = item.id;

        updateItemContent(item, el);
        return el;
    }

    function updateItemContent(item, el) {
        el.className = `upload-item item-status-${item.status.toLowerCase()}`;
        if (item.status === 'UPLOADING' || item.status === 'FINALIZING') el.classList.add('is-uploading');
        if (item.status === 'COMPLETED') el.classList.add('is-completed');
        if (item.status === 'FAILED') el.classList.add('is-failed');
        if (item.status === 'PAUSED') el.classList.add('is-paused');

        // Status badge label and class
        let badgeCls = 'badge-ready';
        let badgeText = window.AVi18n.t('جاهز للرفع');

        switch (item.status) {
            case 'QUEUED':
                badgeCls = 'badge-queued';
                badgeText = window.AVi18n.t('في الانتظار');
                break;
            case 'UPLOADING':
                badgeCls = 'badge-uploading';
                badgeText = window.AVi18n.t('جارٍ الرفع');
                break;
            case 'FINALIZING':
                badgeCls = 'badge-uploading';
                badgeText = window.AVi18n.t('التحقق الأمني...');
                break;
            case 'PAUSED':
                badgeCls = 'badge-paused';
                badgeText = window.AVi18n.t('متوقف');
                break;
            case 'COMPLETED':
                badgeCls = 'badge-completed';
                badgeText = window.AVi18n.t('مكتمل');
                break;
            case 'FAILED':
                badgeCls = 'badge-failed';
                badgeText = window.AVi18n.t('فشل');
                break;
            case 'CANCELLED':
                badgeCls = 'badge-cancelled';
                badgeText = window.AVi18n.t('ملغي');
                break;
        }

        // Action controls
        let actionsHtml = '';
        if (item.status === 'READY') {
            actionsHtml = window.AVi18n.tpl`<button type="button" class="item-btn-remove" data-action="remove" title="إزالة من القائمة" aria-label="إزالة">✕</button>`;
        } else if (item.status === 'UPLOADING') {
            actionsHtml = window.AVi18n.tpl`
                <button type="button" class="btn btn-secondary btn-sm" data-action="pause">إيقاف مؤقت</button>
                <button type="button" class="btn btn-danger btn-sm" data-action="cancel">إلغاء</button>
            `;
        } else if (item.status === 'PAUSED') {
            actionsHtml = window.AVi18n.tpl`
                <button type="button" class="btn btn-primary btn-sm" data-action="resume">استئناف</button>
                <button type="button" class="btn btn-danger btn-sm" data-action="cancel">إلغاء</button>
            `;
        } else if (item.status === 'FAILED') {
            actionsHtml = window.AVi18n.tpl`<button type="button" class="btn btn-secondary btn-sm" data-action="retry">إعادة المحاولة</button>`;
        }

        // Progress section (shown for UPLOADING, PAUSED, FINALIZING, COMPLETED, FAILED)
        let progressHtml = '';
        if (item.status !== 'READY' && item.status !== 'QUEUED') {
            const pct = item.progressPct.toFixed(1);
            let telemetryHtml = '';

            if (item.status === 'COMPLETED') {
                telemetryHtml = `
                    <div class="${item.processingWarning ? 'item-msg-error' : 'item-msg-success'}">
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
                        <span>${item.processingWarning ? window.AVi18n.t('اكتمل الرفع · تعذر تأكيد بدء التحويل') : window.AVi18n.t('اكتمل الرفع · أُضيف إلى طابور التحويل')}</span>
                    </div>
                `;
            } else if (item.status === 'FAILED') {
                telemetryHtml = window.AVi18n.tpl`
                    <div class="item-msg-error">
                        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                        <span>فشل الرفع: ${escapeHtml(item.errorMessage || window.AVi18n.t('حدث خطأ في الشبكة'))}</span>
                    </div>
                `;
            } else {
                telemetryHtml = `
                    <div class="item-telemetry-row">
                        <span class="item-pct-text dir-ltr">${pct}%</span>
                        <div class="item-metrics">
                            <span class="dir-ltr">${formatBytes(item.bytesUploaded)} / ${formatBytes(item.size)}</span>
                            <span class="dir-ltr">${formatSpeed(item.speedBytesPerSec)}</span>
                            <span>${formatEta(item.etaSeconds)}</span>
                        </div>
                    </div>
                `;
            }

            progressHtml = `
                <div class="item-progress-section">
                    <div class="item-progress-bar-shell">
                        <svg class="item-progress-svg" viewBox="0 0 100 6" preserveAspectRatio="none">
                            <rect class="progress-bg" x="0" y="0" width="100" height="6" rx="3"></rect>
                            <rect class="progress-fill" x="0" y="0" width="${pct}" height="6" rx="3"></rect>
                        </svg>
                    </div>
                    ${telemetryHtml}
                </div>
            `;
        }

        el.innerHTML = `
            <div class="item-main-row">
                <div class="item-file-info">
                    <div class="item-icon" aria-hidden="true">
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
                    </div>
                    <div class="item-names-box">
                        <span class="item-filename" title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</span>
                        <span class="item-filesize dir-ltr">${formatBytes(item.size)}</span>
                        ${(item.destination || currentFolder).seasonNumber ? window.AVi18n.tpl`<div class="season-item-fields"><label>الحلقة <input type="number" min="1" max="99999" data-season-field="episode" value="${item.episodeNumber || ''}" ${item.status !== 'READY' ? 'disabled' : ''} aria-label="رقم الحلقة"></label></div>` : ''}
                    </div>
                </div>
                <div class="item-trailing-box">
                    <span class="badge-status ${badgeCls}">${badgeText}</span>
                    <div class="item-actions">${actionsHtml}</div>
                </div>
            </div>
            ${progressHtml}
        `;

        // Bind control buttons
        el.querySelectorAll('[data-action]').forEach(btn => {
            const action = btn.dataset.action;
            btn.onclick = (e) => {
                e.stopPropagation();
                handleItemAction(item, action);
            };
        });
        el.querySelectorAll('[data-season-field]').forEach(input => {
            input.addEventListener('input', () => {
                if (input.dataset.seasonField === 'episode') item.episodeNumber = input.value ? Number(input.value) : null;
            });
        });
    }

    function handleItemAction(item, action) {
        switch (action) {
            case 'remove':
                queue = queue.filter(q => q.id !== item.id);
                renderQueue();
                if (queue.length === 0) queueSection.classList.add('hidden');
                break;
            case 'pause':
                item.isPaused = true;
                item.status = 'PAUSED';
                updateItemContent(item, item.domEl);
                updateQueueHeaderStats();
                if (item.uploadId) {
                    fetch(`/api/v1/uploads/${item.uploadId}/pause`, {
                        method: 'POST',
                        credentials: 'include',
                        headers: { 'X-CSRF-Token': getCsrfToken() }
                    }).catch(() => {});
                }
                break;
            case 'resume':
                item.isPaused = false;
                item.status = 'UPLOADING';
                updateItemContent(item, item.domEl);
                updateQueueHeaderStats();
                if (item.uploadId) {
                    fetch(`/api/v1/uploads/${item.uploadId}/resume`, {
                        method: 'POST',
                        credentials: 'include',
                        headers: { 'X-CSRF-Token': getCsrfToken() }
                    }).catch(() => {});
                }
                break;
            case 'cancel':
                itemPendingCancel = item;
                cancelItemFilename.textContent = item.filename;
                modalConfirmItem.classList.add('is-open');
                break;
            case 'retry':
                item.destination = item.destination || { ...currentFolder };
                item.status = 'QUEUED';
                item.errorMessage = null;
                item.isCancelled = false;
                item.isPaused = false;
                updateItemContent(item, item.domEl);
                updateQueueHeaderStats();
                if (!isBatchRunning) {
                    startBatch();
                } else {
                    scheduleNextUpload();
                }
                break;
        }
    }

    // Modal Item Cancel Confirmations
    btnCancelItemNo.addEventListener('click', () => {
        itemPendingCancel = null;
        modalConfirmItem.classList.remove('is-open');
    });

    btnCancelItemYes.addEventListener('click', () => {
        if (itemPendingCancel) {
            itemPendingCancel.isCancelled = true;
            itemPendingCancel.status = 'CANCELLED';
            if (itemPendingCancel.uploadId) {
                fetch(`/api/v1/uploads/${itemPendingCancel.uploadId}/cancel`, {
                    method: 'POST',
                    credentials: 'include',
                    headers: { 'X-CSRF-Token': getCsrfToken() }
                }).catch(() => {});
            }
            updateItemContent(itemPendingCancel, itemPendingCancel.domEl);
            updateQueueHeaderStats();
            updateBatchProgress();
        }
        itemPendingCancel = null;
        modalConfirmItem.classList.remove('is-open');
    });

    // --- Batch Execution & Scheduling ---
    btnStartBatch.addEventListener('click', () => {
        startBatch();
    });

    btnClearQueue.addEventListener('click', () => {
        // Clear items that are not actively uploading
        queue = queue.filter(q => q.status === 'UPLOADING' || q.status === 'FINALIZING');
        renderQueue();
        if (queue.length === 0) {
            queueSection.classList.add('hidden');
        }
    });

    btnCancelBatch.addEventListener('click', () => {
        modalConfirmBatch.classList.add('is-open');
    });

    btnCancelBatchNo.addEventListener('click', () => {
        modalConfirmBatch.classList.remove('is-open');
    });

    btnCancelBatchYes.addEventListener('click', () => {
        isBatchRunning = false;
        queue.forEach(item => {
            if (item.status === 'UPLOADING' || item.status === 'PAUSED') {
                item.isCancelled = true;
                item.status = 'CANCELLED';
                if (item.uploadId) {
                    fetch(`/api/v1/uploads/${item.uploadId}/cancel`, {
                        method: 'POST',
                        credentials: 'include',
                        headers: { 'X-CSRF-Token': getCsrfToken() }
                    }).catch(() => {});
                }
                updateItemContent(item, item.domEl);
            } else if (item.status === 'QUEUED') {
                item.status = 'CANCELLED';
                updateItemContent(item, item.domEl);
            }
        });
        activeUploadsCount = 0;
        updateQueueHeaderStats();
        updateBatchProgress();
        modalConfirmBatch.classList.remove('is-open');
    });

    async function startBatch() {
        await destinationReady;
        if (destinationError || isBatchRunning) return;
        await loadUploadEncoder();
        const processing = currentProcessingChoice();
        if (!processing || isBatchRunning) { updateProcessingChoice(); return; }
        const readyItems = queue.filter(item => item.status === 'READY');
        const episodeKeys = new Set();
        for (const item of readyItems) {
            const target = item.uploadId && item.destination ? item.destination : currentFolder;
            if (!target.seasonNumber) continue;
            const episode = Number(item.episodeNumber);
            if (!Number.isInteger(episode) || episode < 1 || episode > 99999) {
                alert(window.AVi18n.tpl`تعذر تحديد رقم الحلقة من اسم الملف. أدخل رقمه:\n${item.filename}`);
                item.domEl?.querySelector('[data-season-field="episode"]')?.focus();
                return;
            }
            const key = `${target.seasonId || target.seriesName}:${target.seasonNumber}:${episode}`;
            if (episodeKeys.has(key)) {
                alert(window.AVi18n.t('يوجد رقم حلقة مكرر في القائمة. صحح الأرقام قبل بدء الرفع.'));
                return;
            }
            episodeKeys.add(key);
        }
        // Mark all READY items as QUEUED
        queue.forEach(item => {
            if (item.status === 'READY') {
                if (!item.uploadId) item.destination = { ...currentFolder };
                if (!item.processing) item.processing = { options: { ...processing.options, enabled_qualities: [...processing.options.enabled_qualities] } };
                item.status = 'QUEUED';
                updateItemContent(item, item.domEl);
            }
        });

        isBatchRunning = true;
        updateQueueHeaderStats();
        scheduleNextUpload();
    }

    function scheduleNextUpload() {
        if (!isBatchRunning) return;
        if (!transferPolicyLoaded) return;

        while (activeUploadsCount < concurrencyLimit) {
            const nextItem = queue.find(q => q.status === 'QUEUED');
            if (!nextItem) break;

            activeUploadsCount++;
            nextItem.status = 'UPLOADING';
            updateItemContent(nextItem, nextItem.domEl);
            updateQueueHeaderStats();

            processItemUpload(nextItem).finally(() => {
                activeUploadsCount = Math.max(0, activeUploadsCount - 1);
                scheduleNextUpload();
                updateQueueHeaderStats();
                updateBatchProgress();
            });
        }

        // Check if entire batch completed
        const pendingItems = queue.filter(q => q.status === 'QUEUED' || q.status === 'UPLOADING' || q.status === 'FINALIZING');
        if (pendingItems.length === 0 && activeUploadsCount === 0) {
            isBatchRunning = false;
            updateQueueHeaderStats();
        }
    }

    // --- Individual Resumable Upload Engine ---
    async function processItemUpload(item) {
        await transferPolicyReady;
        item.startTime = Date.now();
        item.lastTime = item.startTime;
        item.lastBytes = item.bytesUploaded;

        try {
            // Step 1: Initiate session if not exists
            let receivedChunksSet = new Set();
            if (!item.uploadId) {
                const initRes = await fetch('/api/v1/uploads/initiate', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': getCsrfToken()
                    },
                    body: JSON.stringify({
                        title: item.titleOverride?.trim() || cleanTitle(item.filename),
                        filename: item.filename,
                        file_size_bytes: item.size,
                        chunk_size_bytes: CHUNK_SIZE,
                        folder_id: (item.destination || currentFolder).folderId,
                        series_id: (item.destination || currentFolder).seriesId,
                        season_id: (item.destination || currentFolder).seasonId,
                        series_name: (item.destination || currentFolder).seriesName,
                        season_number: (item.destination || currentFolder).seasonNumber,
                        episode_number: (item.destination || currentFolder).seasonNumber ? Number(item.episodeNumber) : null,
                        visibility: 'PRIVATE'
                    })
                });

                if (!initRes.ok) {
                    const errData = await initRes.json().catch(() => ({}));
                    throw new Error(window.AVi18n.t(errData.detail) || errData.error?.message || window.AVi18n.tpl`فشل بدء الجلسة (HTTP ${initRes.status})`);
                }

                const initData = await initRes.json();
                item.uploadId = initData.upload_id;
                item.videoId = initData.video_id;
                item.totalChunks = initData.total_chunks || Math.ceil(item.size / CHUNK_SIZE);
            } else {
                // Query status to discover already received chunks
                const statusRes = await fetch(`/api/v1/uploads/${item.uploadId}/status`, {
                    credentials: 'include'
                });
                if (statusRes.ok) {
                    const statusData = await statusRes.json();
                    receivedChunksSet = new Set(statusData.received_chunks || []);
                    item.bytesUploaded = statusData.bytes_received || 0;
                }
            }

            // Step 2: Upload Chunks Loop
            for (let i = 0; i < item.totalChunks; i++) {
                if (item.isCancelled) return;

                // Handle pause loop
                while (item.isPaused) {
                    await new Promise(r => setTimeout(r, 400));
                    if (item.isCancelled) return;
                }

                // If already uploaded, advance
                if (receivedChunksSet.has(i)) continue;

                item.currentChunk = i;
                const start = i * CHUNK_SIZE;
                const end = Math.min(start + CHUNK_SIZE, item.size);
                const chunkBlob = item.file.slice(start, end);

                // Chunk upload with up to 3 automatic retries
                let chunkUploaded = false;
                let chunkError = null;

                for (let attempt = 1; attempt <= 3; attempt++) {
                    if (item.isCancelled) return;

                    // Pace each official browser upload before sending its next
                    // chunk. This is per file; parallel files each get the cap.
                    if (uploadLimitKbps > 0) {
                        const waitMs = Math.ceil(((end - start) * 8 / (uploadLimitKbps * 1000)) * 1000);
                        const until = Date.now() + waitMs;
                        while (Date.now() < until && !item.isCancelled) {
                            if (item.isPaused) { await new Promise(r => setTimeout(r, 300)); continue; }
                            await new Promise(r => setTimeout(r, Math.min(500, until - Date.now())));
                        }
                        if (item.isCancelled) return;
                    }

                    const formData = new FormData();
                    formData.append('chunk_index', i);
                    formData.append('chunk_file', chunkBlob, `part.${i}`);

                    try {
                        const chunkRes = await fetch(`/api/v1/uploads/${item.uploadId}/chunk`, {
                            method: 'POST',
                            credentials: 'include',
                            headers: { 'X-CSRF-Token': getCsrfToken() },
                            body: formData
                        });

                        if (chunkRes.ok) {
                            chunkUploaded = true;
                            break;
                        } else {
                            const errBody = await chunkRes.json().catch(() => ({}));
                            chunkError = new Error(window.AVi18n.t(errBody.detail) || window.AVi18n.tpl`خطأ جزء ${i + 1} (HTTP ${chunkRes.status})`);
                        }
                    } catch (netErr) {
                        chunkError = netErr;
                    }

                    if (attempt < 3 && !item.isCancelled) {
                        await new Promise(r => setTimeout(r, attempt * 1000));
                    }
                }

                if (!chunkUploaded) {
                    throw chunkError || new Error(window.AVi18n.tpl`تعذر رفع الجزء ${i + 1} بعد 3 محاولات`);
                }

                // Chunk successful, update telemetry
                item.bytesUploaded += (end - start);
                let rawPct = (item.bytesUploaded / item.size) * 100;
                if (i + 1 < item.totalChunks && rawPct >= 100) rawPct = 99.9;
                item.progressPct = Number(rawPct.toFixed(1));

                const now = Date.now();
                const timeDiff = (now - item.lastTime) / 1000;
                if (timeDiff >= 0.5) {
                    const bytesDiff = item.bytesUploaded - item.lastBytes;
                    item.speedBytesPerSec = bytesDiff / timeDiff;
                    const remBytes = Math.max(0, item.size - item.bytesUploaded);
                    item.etaSeconds = item.speedBytesPerSec > 0 ? (remBytes / item.speedBytesPerSec) : 0;
                    item.lastTime = now;
                    item.lastBytes = item.bytesUploaded;
                }

                updateItemContent(item, item.domEl);
                updateBatchProgress();
            }

            // Step 3: Finalize & Validation
            if (item.isCancelled) return;
            item.status = 'FINALIZING';
            updateItemContent(item, item.domEl);

            const finRes = await fetch(`/api/v1/uploads/${item.uploadId}/finalize`, {
                method: 'POST',
                credentials: 'include',
                headers: { 'X-CSRF-Token': getCsrfToken() }
            });

            if (!finRes.ok) {
                const finErr = await finRes.json().catch(() => ({}));
                throw new Error(window.AVi18n.t(finErr.detail) || finErr.error?.message || window.AVi18n.tpl`فشل التحقق الأمني من الفيديو (HTTP ${finRes.status})`);
            }

            const finData = await finRes.json();
            item.videoId = finData.video_id;

            // Every uploaded video is queued using the settings reviewed before Start.
            try {
                const transcodeResponse = await fetch(`/api/v1/admin/videos/${item.videoId}/transcode`, {
                    method: 'POST', credentials: 'include',
                    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrfToken() },
                    body: JSON.stringify(item.processing.options)
                });
                if (!transcodeResponse.ok) item.processingWarning = `HTTP ${transcodeResponse.status}`;
                else item.transcodeQueued = true;
            } catch (_) { item.processingWarning = window.AVi18n.t('تعذر الاتصال بالخادم'); }

            // Completed!
            item.status = 'COMPLETED';
            item.progressPct = 100;
            item.bytesUploaded = item.size;
            updateItemContent(item, item.domEl);

        } catch (err) {
            if (item.isCancelled) {
                item.status = 'CANCELLED';
            } else {
                item.status = 'FAILED';
                item.errorMessage = window.AVi18n.t(err.message) || window.AVi18n.t('حدث خطأ أثناء الرفع');
            }
            updateItemContent(item, item.domEl);
        }
    }

    // --- Batch Telemetry & Progress Display ---
    function updateBatchProgress() {
        // A reload would discard selected File objects and interrupt browser uploads.
        const languageLocked = queue.some(item => !['COMPLETED', 'CANCELLED'].includes(item.status));
        const documents = [document];
        try { if (window.parent !== window) documents.push(window.parent.document); } catch (_) {}
        documents.forEach(doc => doc.querySelectorAll('[data-language-select]').forEach(select => {
            select.disabled = languageLocked;
            select.title = languageLocked ? window.AVi18n.t('أكمل الرفع أو امسح القائمة قبل تغيير اللغة.') : '';
        }));
        const totalFiles = queue.length;
        if (totalFiles === 0) {
            batchProgressCard.classList.add('hidden');
            return;
        }

        const completedFiles = queue.filter(q => q.status === 'COMPLETED').length;
        const totalBytes = queue.reduce((sum, q) => sum + q.size, 0);
        const uploadedBytes = queue.reduce((sum, q) => sum + q.bytesUploaded, 0);

        batchDoneCount.textContent = completedFiles;
        batchTotalFiles.textContent = totalFiles;

        const overallPct = totalBytes > 0 ? ((uploadedBytes / totalBytes) * 100).toFixed(1) : 0;
        batchOverallPct.textContent = `${overallPct}%`;
        if (batchProgressBarFill) {
            batchProgressBarFill.setAttribute('width', Math.min(100, Math.max(0, overallPct)));
        }

        // Calculate aggregate speed and ETA across active items
        const activeItems = queue.filter(q => q.status === 'UPLOADING');
        const aggregateSpeed = activeItems.reduce((sum, q) => sum + (q.speedBytesPerSec || 0), 0);
        batchTelemetrySpeed.textContent = formatSpeed(aggregateSpeed);

        const remainingBytes = Math.max(0, totalBytes - uploadedBytes);
        const overallEta = aggregateSpeed > 0 ? (remainingBytes / aggregateSpeed) : 0;
        batchTelemetryEta.textContent = formatEta(overallEta);

        batchTelemetryBytes.textContent = `${formatBytes(uploadedBytes)} / ${formatBytes(totalBytes)}`;
    }

    function escapeHtml(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    // --- Initialize ---
    initDestinationFromUrl().then(() => {
        const params = new URLSearchParams(window.location.search);
        const pick = params.get('pick');
        if (pick === 'file' || pick === 'multiple') {
            setTimeout(() => {
                if (btnPickFiles) btnPickFiles.click();
            }, 300);
        } else if (pick === 'folder') {
            setTimeout(() => {
                if (btnPickFolder) btnPickFolder.click();
            }, 300);
        }
    });
});
