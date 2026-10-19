// ABOODVIDEO Studio - Client Controller & Real-Time Telemetry
document.addEventListener("DOMContentLoaded", () => {
  // State
  const state = {
    currentStep: 1,
    videoPath: "",
    probeData: null,
    selectedQualities: ["1080p", "720p", "480p", "360p"],
    encoderChoice: "auto",
    subtitleMode: "none",
    subtitlePath: "",
    subtitleLang: "ar",
    subtitleLabel: "العربية",
    embeddedSubIndex: null,   // stream index of selected embedded subtitle
    fontAudit: null,
    activeJobId: null,
    serverUrl: "http://localhost:8000/api/v1",
    apiKey: "",
    isConnected: false,
  };

  function setProgressPercent(element, value) {
    if (!element) return;
    const numeric = Number(value);
    const pct = Number.isFinite(numeric) ? Math.max(0, Math.min(100, Math.round(numeric))) : 0;
    element.dataset.progress = String(pct);
  }

  // ─── Toast System ───────────────────────────────────────────────────
  function showToast(message, type = 'info', duration = 4000) {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    const iconMap = {
      success: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>',
      error: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>',
      info: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5m0-8h.01"/></svg>',
      warning: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m10.3 3.8-8 14a2 2 0 0 0 1.7 3h16a2 2 0 0 0 1.7-3l-8-14a2 2 0 0 0-3.4 0ZM12 9v4m0 3h.01"/></svg>'
    };
    toast.innerHTML = `<span class="studio-toast-icon">${iconMap[type] || iconMap.info}</span><span>${message}</span>`;
    container.appendChild(toast);
    setTimeout(() => {
      toast.classList.add('is-leaving');
      setTimeout(() => toast.remove(), 180);
    }, duration);
  }

  function showConfirm(title, message, onConfirm) {
    const backdrop = document.createElement('div');
    backdrop.className = 'confirm-backdrop';
    backdrop.innerHTML = `
      <div class="confirm-box">
        <h3>${title}</h3>
        <p>${message}</p>
        <div class="confirm-box-actions">
          <button type="button" class="btn btn-danger" id="confirm-ok-btn">تأكيد</button>
          <button type="button" class="btn btn-secondary" id="confirm-cancel-btn">إلغاء</button>
        </div>
      </div>
    `;
    document.body.appendChild(backdrop);
    backdrop.querySelector('#confirm-ok-btn').addEventListener('click', () => {
      backdrop.remove();
      onConfirm();
    });
    backdrop.querySelector('#confirm-cancel-btn').addEventListener('click', () => backdrop.remove());
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) backdrop.remove(); });
  }

  // ─── Disk Space ───────────────────────────────────────────────────────
  async function loadDiskInfo() {
    try {
      const res = await fetch('/api/system/disk-info');
      if (!res.ok) return;
      const data = await res.json();
      const freeEl = document.getElementById('disk-free-space');
      const estEl = document.getElementById('disk-estimated-pkg');
      const barEl = document.getElementById('disk-bar-fill');
      const homeEl = document.getElementById('home-disk-text');
      if (freeEl) freeEl.textContent = data.free_formatted || '—';
      if (homeEl) homeEl.textContent = data.free_formatted ? `${data.free_formatted} حرة` : '...';
      if (estEl && state.probeData) {
        const estBytes = state.probeData.file_size * 0.6;
        estEl.textContent = formatBytes(estBytes);
      }
      if (barEl && data.free_bytes !== undefined && data.total_bytes !== undefined) {
        const usedPct = Math.round(((data.total_bytes - data.free_bytes) / data.total_bytes) * 100);
        setProgressPercent(barEl, usedPct);
        if (usedPct > 85) barEl.classList.add('warn');
        else barEl.classList.remove('warn');
      }
    } catch(e) { console.warn('disk-info error', e); }
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB';
    if (bytes < 1073741824) return (bytes/1048576).toFixed(1) + ' MB';
    return (bytes/1073741824).toFixed(2) + ' GB';
  }

  // ─── Windows Notifications ────────────────────────────────────────────
  function sendWinNotification(title, body) {
    fetch('/api/system/notify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, body }),
    }).catch(() => {});
  }

  // ─── Home Landing ─────────────────────────────────────────────────────
  const homeLanding = document.getElementById('home-landing');
  const mainApp = document.getElementById('main-app');

  function showHome() {
    if (homeLanding) homeLanding.style.display = 'block';
    if (mainApp) mainApp.style.display = 'none';
    loadHomeRecent();
    loadDiskInfo();
  }

  function showMainApp(mode) {
    if (homeLanding) homeLanding.style.display = 'none';
    if (mainApp) mainApp.style.display = 'block';
    if (mode === 'batch') setMode('batch');
    else setMode('single');
    loadDiskInfo();
  }

  const homeBtnSingle = document.getElementById('home-btn-single');
  const homeBtnSeason = document.getElementById('home-btn-season');
  const btnBackToHome = document.getElementById('btn-back-to-home');

  if (homeBtnSingle) homeBtnSingle.addEventListener('click', () => showMainApp('single'));
  if (homeBtnSeason) homeBtnSeason.addEventListener('click', () => showMainApp('batch'));
  if (btnBackToHome) btnBackToHome.addEventListener('click', () => showHome());

  async function loadHomeRecent() {
    const panel = document.getElementById('home-recent-panel');
    const list = document.getElementById('home-recent-list');
    if (!panel || !list) return;
    try {
      const res = await fetch('/api/batch/recent-jobs');
      if (!res.ok) { panel.style.display = 'none'; return; }
      const data = await res.json();
      const items = data.jobs || [];
      if (items.length === 0) { panel.style.display = 'none'; return; }
      list.innerHTML = '';
      items.slice(0, 5).forEach(job => {
        const div = document.createElement('div');
        div.className = 'recent-item';
        const statusClass = job.state === 'READY' ? 'recent-status-ready' : (job.state === 'FAILED' ? 'recent-status-failed' : 'recent-status-pending');
        const statusLabel = job.state === 'READY' ? 'جاهز' : (job.state === 'FAILED' ? 'فشل' : 'معالجة');
        div.innerHTML = `
          <span class="recent-item-name">${job.filename || job.title || 'مهمة'}</span>
          <span class="recent-item-status ${statusClass}">${statusLabel}</span>
        `;
        list.appendChild(div);
      });
      panel.style.display = 'block';
    } catch(e) { panel.style.display = 'none'; }
  }

  // ─── Quality Presets ──────────────────────────────────────────────────
  const PRESETS = {
    balanced: (maxH, maxW = 0) => {
      if (maxH >= 2160 || maxW >= 3800) return ['2160p','1080p','720p','480p','360p'];
      if (maxH >= 1080 || maxW >= 1900) return ['1080p','720p','480p','360p'];
      if (maxH >= 720 || maxW >= 1200)  return ['720p','480p','360p'];
      return ['480p','360p'];
    },
    quality: (maxH, maxW = 0) => {
      if (maxH >= 2160 || maxW >= 3800) return ['2160p','1080p','720p','480p','360p'];
      if (maxH >= 1080 || maxW >= 1900) return ['1080p','720p','480p','360p'];
      if (maxH >= 720 || maxW >= 1200)  return ['720p','480p','360p'];
      return ['480p','360p'];
    },
    storage: (maxH, maxW = 0) => {
      if (maxH >= 2160 || maxW >= 3800) return ['1080p','480p','360p'];
      if (maxH >= 1080 || maxW >= 1900) return ['1080p','360p'];
      if (maxH >= 720 || maxW >= 1200)  return ['720p','360p'];
      return ['360p'];
    },
  };

  function applyQualityPreset(preset, sourceHeight) {
    if (!state.probeData) return;
    const presetFn = PRESETS[preset];
    if (!presetFn) return;
    const desired = presetFn(sourceHeight || state.probeData.height, state.probeData ? state.probeData.width : 0);
    // Update checkboxes and state
    state.selectedQualities = [];
    const ladderItems = document.querySelectorAll('.ladder-item');
    ladderItems.forEach(item => {
      const key = item.dataset.key;
      const chk = item.querySelector('input[type=checkbox]');
      if (!chk) return;
      if (desired.includes(key) && !item.classList.contains('disabled')) {
        chk.checked = true;
        item.classList.add('selected');
        state.selectedQualities.push(key);
      } else {
        chk.checked = false;
        item.classList.remove('selected');
      }
    });
  }

  // Hook preset buttons AFTER they are in the DOM
  function initPresetButtons() {
    const presetBtns = document.querySelectorAll('[data-preset]');
    presetBtns.forEach(btn => {
      btn.addEventListener('click', () => {
        presetBtns.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const preset = btn.dataset.preset;
        if (preset !== 'custom') {
          applyQualityPreset(preset, state.probeData ? state.probeData.height : 1080);
        }
      });
    });
  }

  // ─── Hardsub Designer ────────────────────────────────────────────────
  function initHardsubDesigner() {
    const useOriginal = document.getElementById('hardsub-use-original');
    const useCustom = document.getElementById('hardsub-use-custom');
    const customFields = document.getElementById('hardsub-custom-fields');
    if (!useOriginal || !useCustom || !customFields) return;
    useOriginal.addEventListener('change', () => {
      customFields.style.display = 'none';
    });
    useCustom.addEventListener('change', () => {
      customFields.style.display = 'block';
    });
  }


  // Elements
  const el = {
    // Header
    gpuPill: document.getElementById("gpu-telemetry-pill"),
    gpuPillText: document.getElementById("gpu-telemetry-text"),
    serverPill: document.getElementById("server-connection-pill"),
    serverPillText: document.getElementById("server-connection-text"),
    serverDot: document.getElementById("server-pulse-dot"),

    // Navigation Steps
    stepsNavItems: document.querySelectorAll(".step-nav-item"),
    stepSections: document.querySelectorAll(".step-section"),

    // Step 1: Video File
    videoPathInput: document.getElementById("video-path-input"),
    btnBrowseVideo: document.getElementById("btn-browse-video"),
    dropzoneVideo: document.getElementById("video-dropzone"),
    videoMetaCard: document.getElementById("video-metadata-card"),
    btnGoStep2: document.getElementById("btn-go-step-2"),

    // Step 2: Encoder & Ladder
    encoderButtons: document.querySelectorAll(".encoder-choice-btn"),
    ladderContainer: document.getElementById("ladder-container"),
    btnGoStep3: document.getElementById("btn-go-step-3"),

    // Step 3: Subtitles & Fonts
    subModeButtons: document.querySelectorAll(".sub-mode-btn"),
    embeddedSubsSection: document.getElementById("embedded-subs-section"),
    embeddedSubsList: document.getElementById("embedded-subs-list"),
    subFileSection: document.getElementById("sub-file-section"),
    subPathInput: document.getElementById("sub-path-input"),
    btnBrowseSub: document.getElementById("btn-browse-sub"),
    fontsCard: document.getElementById("fonts-audit-card"),
    fontsTableBody: document.getElementById("fonts-table-body"),
    missingFontsNotice: document.getElementById("missing-fonts-notice"),
    btnUploadFont: document.getElementById("btn-upload-font"),
    fontFileInput: document.getElementById("font-file-input"),
    btnGoStep4: document.getElementById("btn-go-step-4"),

    // Step 4: Queue & Progress
    jobTitleInput: document.getElementById("job-title-input"),
    serverUrlInput: document.getElementById("server-url-input"),
    apiKeyInput: document.getElementById("api-key-input"),
    btnStartJob: document.getElementById("btn-start-job"),
    btnCancelJob: document.getElementById("btn-cancel-job"),
    jobProgressContainer: document.getElementById("job-progress-container"),
    jobStatusBadge: document.getElementById("job-status-badge"),
    jobStatusDesc: document.getElementById("job-status-desc"),
    overallProgressBar: document.getElementById("overall-progress-bar"),
    overallProgressPct: document.getElementById("overall-progress-pct"),
    gaugeFps: document.getElementById("gauge-fps"),
    gaugeSpeed: document.getElementById("gauge-speed"),
    gaugeEta: document.getElementById("gauge-eta"),
    gaugeGpu: document.getElementById("gauge-gpu"),
    gaugeCpu: document.getElementById("gauge-cpu"),
    gaugeVram: document.getElementById("gauge-vram"),

    // Step 5: Verification & Ready
    step5SuccessCard: document.getElementById("step5-success-card"),
    readyVideoId: document.getElementById("ready-video-id"),
    linkWatchVideo: document.getElementById("link-watch-video"),
  };

  // --- Step Navigation ---
  function goToStep(stepNumber) {
    if (stepNumber < 1 || stepNumber > 5) return;
    state.currentStep = stepNumber;

    el.stepsNavItems.forEach((item) => {
      const s = parseInt(item.dataset.step, 10);
      item.classList.toggle("active", s === stepNumber);
      item.classList.toggle("completed", s < stepNumber);
    });

    el.stepSections.forEach((sec) => {
      const s = parseInt(sec.dataset.step, 10);
      sec.style.display = s === stepNumber ? "block" : "none";
    });

    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  document.querySelectorAll("[data-return-step]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = document.querySelector(`[data-step="${button.dataset.returnStep}"]`);
      target?.click();
    });
  });
  document.getElementById("btn-process-new")?.addEventListener("click", () => window.location.reload());

  el.stepsNavItems.forEach((item) => {
    item.addEventListener("click", () => {
      const targetStep = parseInt(item.dataset.step, 10);
      if (targetStep === 1 || (state.probeData && targetStep <= 4)) {
        goToStep(targetStep);
      }
    });
  });

  // --- Initial Config & Telemetry ---
  async function loadConfig() {
    try {
      const res = await fetch("/api/config");
      if (res.ok) {
        const cfg = await res.json();
        state.serverUrl = cfg.server_url || "http://localhost:8000/api/v1";
        state.apiKey = cfg.api_key || "";
        if (el.serverUrlInput) el.serverUrlInput.value = state.serverUrl;
        if (el.apiKeyInput) el.apiKeyInput.value = state.apiKey;
        testServerConnection();
      }
    } catch (e) {
      console.error("Config load error", e);
    }
  }

  async function testServerConnection() {
    if (!state.serverUrl || !state.apiKey) {
      if (el.serverPillText) el.serverPillText.textContent = "غير متصل";
      if (el.serverDot) el.serverDot.className = "connection-dot disconnected";
      return;
    }

    try {
      const res = await fetch("/api/test-connection", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ server_url: state.serverUrl, api_key: state.apiKey }),
      });
      const data = await res.json();
      if (data.ok) {
        state.isConnected = true;
        if (el.serverPillText) el.serverPillText.textContent = "متصل بـ ABOODVIDEO";
        if (el.serverDot) el.serverDot.className = "connection-dot connected";
      } else {
        state.isConnected = false;
        if (el.serverPillText) el.serverPillText.textContent = "غير متصل";
        if (el.serverDot) el.serverDot.className = "connection-dot disconnected";
      }
    } catch (e) {
      state.isConnected = false;
      if (el.serverPillText) el.serverPillText.textContent = "غير متصل";
      if (el.serverDot) el.serverDot.className = "connection-dot disconnected";
    }
  }

  // Live WebSocket Telemetry
  function initWebSocket() {
    const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${wsProto}//${window.location.host}/api/ws/telemetry`;
    const ws = new WebSocket(wsUrl);

    ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        const hw = payload.hardware;
        if (hw && hw.gpu) {
          if (hw.gpu.gpu_detected) {
            el.gpuPillText.textContent = `${hw.gpu.gpu_name} (${hw.gpu.gpu_util_pct}% | ${hw.gpu.gpu_temp_c}°C)`;
            if (el.gaugeGpu) el.gaugeGpu.textContent = `${hw.gpu.gpu_util_pct}%`;
            if (el.gaugeVram) el.gaugeVram.textContent = `${hw.gpu.vram_used_mb} MB (${hw.gpu.vram_util_pct}%)`;
          } else {
            el.gpuPillText.textContent = "معالجة CPU فقط";
          }
        }
        if (hw && hw.cpu_ram) {
          if (el.gaugeCpu) el.gaugeCpu.textContent = `${hw.cpu_ram.cpu_util_pct}%`;
        }

        // Active Job Telemetry
        const job = payload.active_job;
        if (job && job.job_id === state.activeJobId) {
          updateJobUI(job);
        }

        // Season Batch Telemetry
        if (payload.batch && typeof window.updateBatchLiveTelemetry === "function") {
          window.updateBatchLiveTelemetry(payload.batch);
        }
      } catch (e) {
        console.error("Telemetry parse error", e);
      }
    };

    ws.onclose = () => {
      setTimeout(initWebSocket, 2000);
    };
  }

  // --- Step 1: Video File Selection & Probe ---
  async function selectAndProbeVideo(path) {
    if (!path) return;
    state.videoPath = path;
    el.videoPathInput.value = path;

    // Show loading
    el.videoMetaCard.style.display = "block";
    el.videoMetaCard.innerHTML = `
      <div class="p06-js-002">
        جاري فحص مواصفات الفيديو بواسطة ffprobe...
      </div>
    `;

    try {
      const res = await fetch("/api/probe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ video_path: path }),
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "فشل فحص الفيديو");
      }

      const p = await res.json();
      state.probeData = p;
      state.embeddedSubIndex = null;  // reset on new video
      if (el.jobTitleInput && !el.jobTitleInput.value) {
        el.jobTitleInput.value = p.file_name.replace(/\.[^/.]+$/, "");
      }

      renderVideoMetadata(p);
      renderLadderOptions(p);
      renderEmbeddedSubtitles(p);
      el.btnGoStep2.disabled = false;
    } catch (e) {
      el.videoMetaCard.innerHTML = `
        <div class="p06-js-003">
           ${e.message}
        </div>
      `;
      el.btnGoStep2.disabled = true;
    }
  }

  function renderVideoMetadata(p) {
    // Detect HDR
    const hdrLabel = p.hdr_label || (p.is_hdr ? 'HDR' : (p.is_10bit ? '10-bit SDR' : 'SDR'));
    const hdrBadge = p.is_hdr
      ? `<span class="hdr-badge">${hdrLabel}</span>`
      : (p.is_10bit
        ? `<span class="hdr-badge p06-js-004">${hdrLabel}</span>`
        : `<span class="sdr-badge">${hdrLabel}</span>`);

    const escapeHTML = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[char]);
    const audioSummary = p.has_audio
      ? `${(p.audio_codec || "").toUpperCase()} ${p.audio_channels_label || ""}`.trim()
      : "بدون صوت";
    const outputOrder = ["2160p", "1080p", "720p", "480p", "360p"];
    const outputLabels = (p.qualities || []).filter(q => q.allowed).sort((a, b) =>
      outputOrder.indexOf(a.key) - outputOrder.indexOf(b.key)
    ).map(q => ({
      "2160p": "4K", "1080p": "1080p", "720p": "720p", "480p": "480p", "360p": "360p",
    }[q.key] || q.key));

    el.videoMetaCard.innerHTML = `
      <div class="card-title-row">
        <div class="card-title p06-js-005">نتائج فحص المصدر ${hdrBadge}</div>
      </div>
      <div class="source-inspection">
        <section class="source-inspection-group" aria-label="Source">
          <h3>SOURCE</h3>
          <div class="source-inspection-values">
            <span><bdi dir="ltr">${escapeHTML(p.resolution.replace("x", "×"))}</bdi></span>
            <span><bdi dir="ltr">${escapeHTML((p.video_codec || "").toUpperCase())}</bdi></span>
            <span><bdi dir="ltr">${escapeHTML(p.fps_display)} FPS</bdi></span>
            <span><bdi dir="auto">${escapeHTML(hdrLabel)}</bdi></span>
            <span><bdi dir="ltr">${escapeHTML(p.duration_formatted)}</bdi></span>
            <span><bdi dir="ltr">${escapeHTML(p.file_size_formatted)}</bdi></span>
          </div>
        </section>
        <section class="source-inspection-group" aria-label="Audio">
          <h3>AUDIO</h3>
          <div class="source-inspection-values"><span><bdi dir="auto">${escapeHTML(audioSummary)}</bdi></span></div>
        </section>
        <section class="source-inspection-group" aria-label="Output">
          <h3>OUTPUT</h3>
          <div class="source-inspection-values" dir="ltr">${outputLabels.map(label => `<span><bdi dir="ltr">${escapeHTML(label)}</bdi></span>`).join("")}</div>
        </section>
      </div>
    `;
  }

  // Render embedded subtitle tracks (called immediately after probe)
  function renderEmbeddedSubtitles(p) {
    const tracks = (p.subtitle_tracks || []);
    const section = el.embeddedSubsSection;
    const list = el.embeddedSubsList;

    if (!section || !list) return;

    if (tracks.length === 0) {
      section.style.display = "none";
      return;
    }

    // Build codec badge label
    function codecBadge(codec) {
      const MAP = { ass: "ASS", ssa: "SSA", subrip: "SRT", mov_text: "TX3G", webvtt: "VTT", hdmv_pgs_subtitle: "PGS", dvd_subtitle: "VOBSUB" };
      return (MAP[codec] || codec.toUpperCase());
    }

    // Language code → Arabic display label
    function langLabel(lang) {
      const MAP = { ara: "العربية", ar: "العربية", eng: "English", en: "English", jpn: "日本語", ja: "日本語", und: "غير محدد" };
      return MAP[lang] || lang;
    }

    // Determine auto-select: single Arabic track
    const arabicTracks = tracks.filter(t => t.language === "ara" || t.language === "ar");
    let autoSelectIndex = null;
    if (arabicTracks.length === 1) {
      autoSelectIndex = arabicTracks[0].index;
    }

    list.innerHTML = "";

    tracks.forEach(track => {
      const isArabic = (track.language === "ara" || track.language === "ar");
      const isAutoSelected = (track.index === autoSelectIndex);

      const row = document.createElement("div");
      row.classList.add("subtitle-track-row");
      row.dataset.streamIndex = track.index;

      const badge = document.createElement("span");
      badge.className = "p06-js-006";
      badge.textContent = codecBadge(track.codec);
      const lang = document.createElement("span");
      lang.className = "p06-js-007";
      lang.textContent = langLabel(track.language);
      row.append(badge, lang);
      if (track.title && track.title !== `Subtitle ${track.index + 1}`) {
        const title = document.createElement("bdi");
        title.className = "p06-js-008";
        title.dir = "auto";
        title.textContent = `— ${track.title}`;
        row.appendChild(title);
      }
      const embeddedBadge = document.createElement("span");
      embeddedBadge.className = "p06-js-009";
      embeddedBadge.textContent = "مضمنة";
      const check = document.createElement("span");
      check.className = "embedded-check p06-js-010";
      row.append(embeddedBadge, check);

      row.addEventListener("click", () => {
        // Deselect all
        list.querySelectorAll("[data-stream-index]").forEach(r => {
          r.classList.remove("is-selected");
        });
        // Select this
        const alreadySelected = (state.embeddedSubIndex === track.index);
        if (alreadySelected) {
          // Toggle off: deselect embedded
          state.embeddedSubIndex = null;
        } else {
          state.embeddedSubIndex = track.index;
          row.classList.add("is-selected");
          // Hide external file picker when embedded selected
          el.subFileSection.style.display = "none";
          el.subPathInput.value = "";
          state.subtitlePath = "";
        }
      });

      list.appendChild(row);

      // Apply auto-selection visually
      if (isAutoSelected) {
        state.embeddedSubIndex = track.index;
        row.classList.add("is-selected");
        // Auto-select softsub mode when Arabic subtitle found
        const softsub = document.querySelector('[data-mode="softsub"]');
        if (softsub && state.subtitleMode === 'none') {
          el.subModeButtons.forEach(b => b.classList.remove('selected'));
          softsub.classList.add('selected');
          state.subtitleMode = 'softsub';
          // Hide external sub picker — embedded is already selected
          if (el.subFileSection) el.subFileSection.style.display = 'none';
        }
      }
    });

    // Show "no Arabic" notice if needed
    if (arabicTracks.length === 0) {
      const notice = document.createElement("div");
      notice.className = "subtitle-empty-notice";
      notice.textContent = "لا توجد ترجمة عربية مضمنة";
      list.appendChild(notice);
    }

    section.style.display = "block";
  }

  // Browse Native Dialog

  el.btnBrowseVideo.addEventListener("click", async () => {
    try {
      const res = await fetch("/api/system/select-video", { method: "POST" });
      const data = await res.json();
      if (!data.cancelled && data.path) {
        if (el.videoPathInput) el.videoPathInput.value = data.path;
        selectAndProbeVideo(data.path);
      }
    } catch (e) {
      console.error("Browse video error:", e);
    }
  });

  // Path Input Change
  el.videoPathInput.addEventListener("change", (e) => {
    selectAndProbeVideo(e.target.value.trim());
  });

  // Dropzone drag & drop
  el.dropzoneVideo.addEventListener("dragover", (e) => {
    e.preventDefault();
    el.dropzoneVideo.classList.add("dragover");
  });
  el.dropzoneVideo.addEventListener("dragleave", () => {
    el.dropzoneVideo.classList.remove("dragover");
  });
  el.dropzoneVideo.addEventListener("drop", (e) => {
    e.preventDefault();
    el.dropzoneVideo.classList.remove("dragover");
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      const f = e.dataTransfer.files[0];
      if (f.path) {
        selectAndProbeVideo(f.path);
      } else {
        // Automatically trigger native browse picker without alert
        el.btnBrowseVideo.click();
      }
    }
  });

  el.btnGoStep2.addEventListener("click", () => goToStep(2));

  // --- Step 2: Encoder & Quality Ladder ---
  function renderLadderOptions(p) {
    el.ladderContainer.innerHTML = "";
    state.selectedQualities = [];

    p.qualities.forEach((q) => {
      const item = document.createElement("div");
      item.className = `ladder-item ${q.allowed ? "selected" : "disabled"}`;
      item.dataset.key = q.key;

      if (q.allowed) {
        state.selectedQualities.push(q.key);
      }

      item.innerHTML = `
        <div class="ladder-header">
          <span class="ladder-badge">${q.key === '2160p' ? '4K' : q.key}</span>
          <input type="checkbox" ${q.allowed ? "checked" : "disabled"} / class="p06-js-011">
        </div>
        <div class="ladder-dims">${q.width}x${q.height}</div>
        <div class="ladder-bitrate">${Math.round(q.video_bitrate_bps / 1000)} kbps</div>
        ${q.is_upscale ? '<div class="upscale-tag"> ممنوع (ترقية دقة)</div>' : ""}
      `;

      if (q.allowed) {
        item.addEventListener("click", (e) => {
          if (e.target.tagName !== "INPUT") {
            const chk = item.querySelector("input");
            chk.checked = !chk.checked;
          }
          const chk = item.querySelector("input");
          if (chk.checked) {
            item.classList.add("selected");
            if (!state.selectedQualities.includes(q.key)) state.selectedQualities.push(q.key);
          } else {
            item.classList.remove("selected");
            state.selectedQualities = state.selectedQualities.filter((k) => k !== q.key);
          }
        });
      }

      el.ladderContainer.appendChild(item);
    });
  }

  el.encoderButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      el.encoderButtons.forEach((b) => b.classList.remove("selected"));
      btn.classList.add("selected");
      state.encoderChoice = btn.dataset.encoder;
    });
  });

  el.btnGoStep3.addEventListener("click", () => goToStep(3));

  // --- Step 3: Subtitles & ASS Fonts ---
  el.subModeButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      el.subModeButtons.forEach((b) => b.classList.remove("selected"));
      btn.classList.add("selected");
      state.subtitleMode = btn.dataset.mode;

      const hardsubDesigner = document.getElementById('hardsub-designer');

      if (state.subtitleMode === "none") {
        el.subFileSection.style.display = "none";
        el.fontsCard.style.display = "none";
        if (hardsubDesigner) hardsubDesigner.style.display = 'none';
      } else if (state.subtitleMode === "hardsub") {
        // Only show external file picker if no embedded track is selected
        if (state.embeddedSubIndex === null) {
          el.subFileSection.style.display = "block";
        } else {
          el.subFileSection.style.display = "none";
        }
        if (hardsubDesigner) hardsubDesigner.style.display = 'block';
      } else {
        // softsub
        if (state.embeddedSubIndex === null) {
          el.subFileSection.style.display = "block";
        } else {
          el.subFileSection.style.display = "none";
        }
        if (hardsubDesigner) hardsubDesigner.style.display = 'none';
      }
    });
  });

  el.btnBrowseSub.addEventListener("click", async () => {
    try {
      const res = await fetch("/api/system/select-subtitle", { method: "POST" });
      const data = await res.json();
      if (!data.cancelled && data.path) {
        // Clear embedded sub selection — external file takes priority
        state.embeddedSubIndex = null;
        if (el.embeddedSubsList) {
          el.embeddedSubsList.querySelectorAll("[data-stream-index]").forEach(r => {
            r.style.background = "";
            r.style.borderColor = "var(--border-color)";
            const chk = r.querySelector(".embedded-check");
            if (chk) chk.style.display = "none";
          });
        }
        state.subtitlePath = data.path;
        el.subPathInput.value = data.path;
        if (data.path.toLowerCase().endsWith(".ass")) {
          auditAssFonts(data.path);
        } else {
          el.fontsCard.style.display = "none";
        }
      }
    } catch (e) {
      console.error("Browse subtitle error:", e);
    }
  });

  el.subPathInput.addEventListener("change", (e) => {
    const val = e.target.value.trim();
    // Clear embedded sub selection if user types an external path
    if (val) {
      state.embeddedSubIndex = null;
      if (el.embeddedSubsList) {
        el.embeddedSubsList.querySelectorAll("[data-stream-index]").forEach(r => {
          r.style.background = "";
          r.style.borderColor = "var(--border-color)";
          const chk = r.querySelector(".embedded-check");
          if (chk) chk.style.display = "none";
        });
      }
    }
    state.subtitlePath = val;
    if (val.toLowerCase().endsWith(".ass")) {
      auditAssFonts(val);
    } else {
      el.fontsCard.style.display = "none";
    }
  });

  async function auditAssFonts(assPath) {
    el.fontsCard.style.display = "block";
    el.fontsTableBody.innerHTML = `
      <tr><td colspan="4" class="p06-js-012">
        جاري فحص خطوط ASS بالنظام واستوديو الخطوط ومرفقات MKV...
      </td></tr>
    `;

    try {
      const res = await fetch("/api/fonts/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ass_path: assPath, video_path: state.videoPath || null }),
      });
      const data = await res.json();
      state.fontAudit = data;
      renderFontsTable(data);
    } catch (e) {
      el.fontsTableBody.innerHTML = `
        <tr><td colspan="4" class="p06-js-013">
           خطأ في فحص خطوط الترجمة: ${e.message}
        </td></tr>
      `;
    }
  }

  function renderFontsTable(data) {
    el.fontsTableBody.innerHTML = "";

    // Header Summary Count Pill
    const summaryPill = document.getElementById("fonts-summary-counts");
    if (summaryPill) {
      const missingClass = data.missing_count > 0 ? "font-count-warning" : "font-count-neutral";
      summaryPill.innerHTML = `
        <span>الخطوط المطلوبة (Required): <strong>${data.total_fonts}</strong></span> &bull;
        <span class="p06-js-014">المتوفرة (Available): <strong>${data.available_count}</strong></span> &bull;
        <span class="${missingClass}">الناقصة (Missing): <strong>${data.missing_count}</strong></span>
      `;
      summaryPill.style.display = "block";
    }

    if (data.fonts.length === 0) {
      el.fontsTableBody.innerHTML = `
        <tr><td colspan="4" class="p06-js-015">
          لا توجد خطوط مخصصة محددة في ملف الترجمة.
        </td></tr>
      `;
      el.missingFontsNotice.style.display = "none";
      return;
    }

    data.fonts.forEach((f) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="p06-js-016">${f.name}</td>
        <td>
          <span class="font-badge ${f.status}">
            ${f.status === "installed" ? "✓" : (f.status === "mkv_attachment" ? "" : (f.status === "attached" ? "" : ""))}
            ${f.status_label}
          </span>
        </td>
        <td class="p06-js-017">
          ${f.in_styles ? "في الأنماط [Styles]" : ""}
          ${f.in_overrides ? "في الأسطر [Overrides]" : ""}
        </td>
        <td class="p06-js-018">
          ${f.file || "-"}
        </td>
      `;
      el.fontsTableBody.appendChild(tr);
    });

    if (data.missing_count > 0) {
      el.missingFontsNotice.style.display = "block";
      el.missingFontsNotice.innerHTML = `
         يوجد <strong>${data.missing_count}</strong> خط(وط) مفقودة. يمكنك اختيار ملفات الخطوط (.ttf/.otf) أو أرشيف ZIP ليتم تجميعها تلقائياً.
      `;
    } else {
      el.missingFontsNotice.style.display = "none";
    }
  }

  // Upload Fonts (Multiple TTF/OTF or ZIP)
  el.btnUploadFont.addEventListener("click", () => el.fontFileInput.click());
  el.fontFileInput.addEventListener("change", async (e) => {
    if (e.target.files && e.target.files.length > 0) {
      const files = Array.from(e.target.files);
      const isZip = files.length === 1 && files[0].name.toLowerCase().endsWith(".zip");

      try {
        if (isZip) {
          const formData = new FormData();
          formData.append("file", files[0]);
          await fetch("/api/fonts/import-zip", { method: "POST", body: formData });
        } else {
          const formData = new FormData();
          files.forEach((f) => formData.append("files", f));
          await fetch("/api/fonts/upload-multiple", { method: "POST", body: formData });
        }

        // Re-audit fonts
        if (state.subtitlePath) {
          auditAssFonts(state.subtitlePath);
        }
      } catch (err) {
        console.error("Failed to upload fonts:", err);
      }
    }
  });

  const btnBrowseFontFolder = document.getElementById("btn-browse-font-folder");
  if (btnBrowseFontFolder) {
    btnBrowseFontFolder.addEventListener("click", async () => {
      try {
        const res = await fetch("/api/system/select-folder", { method: "POST" });
        const data = await res.json();
        if (!data.cancelled && data.path) {
          await fetch("/api/fonts/import-folder", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ folder_path: data.path }),
          });
          if (state.subtitlePath) {
            auditAssFonts(state.subtitlePath);
          }
        }
      } catch (e) {
        console.error("Browse font folder error:", e);
      }
    });
  }

  el.btnGoStep4.addEventListener("click", () => goToStep(4));

  // --- Step 4: Job Execution & Telemetry ---
  el.btnStartJob.addEventListener("click", async () => {
    if (!state.videoPath) {
      showToast("يرجى اختيار ملف فيديو أولاً.", 'warning');
      goToStep(1);
      return;
    }

    if (state.selectedQualities.length === 0) {
      showToast("يرجى اختيار جودة واحدة على الأقل في الخطوة 2.", 'warning');
      goToStep(2);
      return;
    }

    const title = el.jobTitleInput.value.trim() || state.probeData.file_name;
    const serverUrl = el.serverUrlInput.value.trim() || state.serverUrl;
    const apiKey = el.apiKeyInput.value.trim() || state.apiKey;

    // Save config
    await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ server_url: serverUrl, api_key: apiKey }),
    });

    // Collect hardsub style settings if in hardsub mode
    let hardsubStyle = null;
    if (state.subtitleMode === 'hardsub') {
      const useCustom = document.getElementById('hardsub-use-custom');
      const source = (useCustom && useCustom.checked) ? 'custom' : 'original';
      if (source === 'custom') {
        hardsubStyle = {
          source: 'custom',
          font: (document.getElementById('hardsub-font') || {}).value || 'Cairo',
          size: parseInt((document.getElementById('hardsub-size') || {}).value || '24', 10),
          color: (document.getElementById('hardsub-color') || {}).value || '#ffffff',
          border_color: (document.getElementById('hardsub-border-color') || {}).value || '#000000',
          border_size: parseFloat((document.getElementById('hardsub-border-size') || {}).value || '2'),
          position: (document.getElementById('hardsub-position') || {}).value || 'bottom',
        };
      } else {
        hardsubStyle = { source: 'original' };
      }
    }

    const payload = {
      video_path: state.videoPath,
      title: title,
      selected_qualities: state.selectedQualities,
      encoder_choice: state.encoderChoice,
      subtitle_mode: state.subtitleMode,
      subtitle_path: state.subtitleMode !== "none" && state.embeddedSubIndex === null ? state.subtitlePath : null,
      embedded_subtitle_index: state.subtitleMode !== "none" ? state.embeddedSubIndex : null,
      hardsub_style: hardsubStyle,
      subtitle_lang: "ar",
      subtitle_label: "العربية",
      server_url: serverUrl,
      api_key: apiKey,
      auto_upload: true,
    };

    el.btnStartJob.disabled = true;
    el.btnCancelJob.disabled = false;
    el.jobProgressContainer.style.display = "block";
    el.step5SuccessCard.style.display = "none";

    try {
      const res = await fetch("/api/jobs/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "تعذر بدء المهمة");
      }

      const jobData = await res.json();
      state.activeJobId = jobData.job_id;
    } catch (e) {
      showToast("تعذر معالجة الفيديو. يرجى التحقق من توفر مساحة القرص وصيغة الملف.", 'error');
      el.btnStartJob.disabled = false;
      el.btnCancelJob.disabled = true;
    }
  });

  el.btnCancelJob.addEventListener("click", async () => {
    if (!state.activeJobId) return;
    showConfirm('إلغاء المهمة', 'هل أنت متأكد من إلغاء المهمة الحالية؟', async () => {
      try {
        await fetch(`/api/jobs/${state.activeJobId}/cancel`, { method: "POST" });
        el.btnCancelJob.disabled = true;
        el.btnStartJob.disabled = false;
        showToast('تم إلغاء المهمة.', 'info');
      } catch (e) {
        console.error(e);
      }
    });
  });

  function updateJobUI(job) {
    el.jobStatusBadge.textContent = job.status.toUpperCase();
    el.jobStatusDesc.textContent = job.stage_description || "";

    const pct = job.progress_pct || 0;
    setProgressPercent(el.overallProgressBar, pct);
    el.overallProgressPct.textContent = `${pct}%`;

    if (job.fps) el.gaugeFps.textContent = `${job.fps} fps`;
    if (job.speed) el.gaugeSpeed.textContent = job.speed;
    if (job.eta) el.gaugeEta.textContent = job.eta;

    if (job.status === "completed") {
      el.btnStartJob.disabled = false;
      el.btnCancelJob.disabled = true;
      setProgressPercent(el.overallProgressBar, 100);
      el.overallProgressPct.textContent = "100%";

      // Trigger Step 5 View
      goToStep(5);
      renderSuccessView(job);
      showToast('اكتملت المعالجة بنجاح', 'success', 5000);
      sendWinNotification('ABOODVIDEO Studio', 'اكتملت معالجة الفيديو ونشره بنجاح');
    } else if (job.status === "failed") {
      el.btnStartJob.disabled = false;
      el.btnCancelJob.disabled = true;
      el.jobStatusBadge.style.background = "var(--accent-red-bg)";
      el.jobStatusBadge.style.color = "var(--accent-red)";
      el.jobStatusDesc.textContent = ` فشلت المهمة: ${job.error || ""}`;
    }
  }

  function renderSuccessView(job) {
    el.step5SuccessCard.style.display = "block";
    const res = job.result;
    if (res && res.video_id) {
      el.readyVideoId.textContent = res.video_id;
      el.linkWatchVideo.href = `${res.server_url.replace('/api/v1', '')}/watch/${res.video_id}`;
    } else {
      el.readyVideoId.textContent = "اكتمل التجهيز المحلي بنجاح";
    }
  }

  // --- Season Batch Queue Logic (Phase 2A) ---
  const tabBtnSingle = document.getElementById("tab-btn-single");
  const tabBtnBatch = document.getElementById("tab-btn-batch");
  const singleVideoFlow = document.getElementById("single-video-flow");
  const seasonBatchSection = document.getElementById("season-batch-section");

  const batchFolderInput = document.getElementById("batch-folder-input");
  const batchDestinationInput = document.getElementById("batch-destination-input");
  const btnBrowseSeasonFolder = document.getElementById("btn-browse-season-folder");
  const btnImportSeason = document.getElementById("btn-import-season");

  const btnStartBatch = document.getElementById("btn-start-batch");
  const btnAutoPipeline = document.getElementById("btn-auto-pipeline");
  const btnUploadReady = document.getElementById("btn-upload-ready");
  const btnPauseBatch = document.getElementById("btn-pause-batch");
  const btnResumeBatch = document.getElementById("btn-resume-batch");
  const btnStopAfterCurrent = document.getElementById("btn-stop-after-current");
  const btnClearBatch = document.getElementById("btn-clear-batch");

  const batchFolderIdInput = document.getElementById("batch-folder-id-input");
  const batchSeriesIdInput = document.getElementById("batch-series-id-input");
  const batchSeasonIdInput = document.getElementById("batch-season-id-input");

  const batchQueueCard = document.getElementById("batch-queue-card");
  const batchStatusText = document.getElementById("batch-status-text");
  const batchTotalCount = document.getElementById("batch-total-count");
  const batchTotalBytes = document.getElementById("batch-total-bytes");
  const batchTableBody = document.getElementById("batch-table-body");
  let seasonHistoryLoaded = false;

  const batchOverallStatusLine = document.getElementById("batch-overall-status-line");
  const batchOverallProgressBar = document.getElementById("batch-overall-progress-bar");
  const batchOverallProgressText = document.getElementById("batch-overall-progress-text");

  const badgeTotal = document.getElementById("badge-total");
  const badgeReady = document.getElementById("badge-ready");
  const badgeReadyToUpload = document.getElementById("badge-ready-to-upload");
  const badgeUploading = document.getElementById("badge-uploading");
  const badgeEncoding = document.getElementById("badge-encoding");
  const badgePending = document.getElementById("badge-pending");
  const badgeFailed = document.getElementById("badge-failed");

  const gpuTaskBadge = document.getElementById("gpu-task-badge");
  const gpuTaskFile = document.getElementById("gpu-task-file");
  const gpuTaskProgBar = document.getElementById("gpu-task-prog-bar");
  const gpuTaskMetrics = document.getElementById("gpu-task-metrics");
  const gpuTaskEta = document.getElementById("gpu-task-eta");

  const networkTaskBadge = document.getElementById("network-task-badge");
  const networkTaskFile = document.getElementById("network-task-file");
  const networkTaskProgBar = document.getElementById("network-task-prog-bar");
  const networkTaskSpeed = document.getElementById("network-task-speed");
  const networkTaskEta = document.getElementById("network-task-eta");

  // Phase 4B: Connection Modal elements
  const btnOpenConnectionModal = document.getElementById("btn-open-connection-modal");
  const connectionModal = document.getElementById("connection-modal");
  const modalBtnClose = document.getElementById("modal-btn-close");
  const modalServerUrl = document.getElementById("modal-server-url");
  const modalApiKey = document.getElementById("modal-api-key");
  const modalConnectionStatus = document.getElementById("modal-connection-status");
  const modalScopesDebug = document.getElementById("modal-scopes-debug");
  const modalBtnTest = document.getElementById("modal-btn-test");
  const modalBtnSave = document.getElementById("modal-btn-save");

  // Phase 4B: Destination Card elements
  const destinationDisplayText = document.getElementById("destination-display-text");
  const btnEditDestination = document.getElementById("btn-edit-destination");
  const destinationEditRow = document.getElementById("destination-edit-row");
  const btnSaveDestination = document.getElementById("btn-save-destination");
  const btnCancelDestination = document.getElementById("btn-cancel-destination");

  // Phase 4B: Review Alert Banner elements
  const needsReviewBanner = document.getElementById("needs-review-banner");
  const needsReviewText = document.getElementById("needs-review-text");
  const btnReviewScroll = document.getElementById("btn-review-scroll");

  let batchPollTimer = null;

  function setMode(mode) {
    if (mode === "batch") {
      if (tabBtnBatch) {
        tabBtnBatch.classList.add("active");
        tabBtnBatch.classList.remove("btn-secondary");
        tabBtnBatch.classList.add("btn-primary");
      }
      if (tabBtnSingle) {
        tabBtnSingle.classList.remove("active");
        tabBtnSingle.classList.remove("btn-primary");
        tabBtnSingle.classList.add("btn-secondary");
      }
      if (singleVideoFlow) singleVideoFlow.style.display = "none";
      if (seasonBatchSection) seasonBatchSection.style.display = "block";
    } else {
      if (tabBtnSingle) {
        tabBtnSingle.classList.add("active");
        tabBtnSingle.classList.remove("btn-secondary");
        tabBtnSingle.classList.add("btn-primary");
      }
      if (tabBtnBatch) {
        tabBtnBatch.classList.remove("active");
        tabBtnBatch.classList.remove("btn-primary");
        tabBtnBatch.classList.add("btn-secondary");
      }
      if (singleVideoFlow) singleVideoFlow.style.display = "block";
      if (seasonBatchSection) seasonBatchSection.style.display = "none";
    }
  }

  if (tabBtnSingle) tabBtnSingle.addEventListener("click", () => setMode("single"));
  if (tabBtnBatch) tabBtnBatch.addEventListener("click", () => setMode("batch"));

  if (btnBrowseSeasonFolder) {
    btnBrowseSeasonFolder.addEventListener("click", async () => {
      try {
        const res = await fetch("/api/system/select-folder", { method: "POST" });
        const data = await res.json();
        if (!data.cancelled && data.path) {
          batchFolderInput.value = data.path;
        }
      } catch (e) {
        console.error("Browse folder error:", e);
      }
    });
  }

  function updateStepper(step) {
    for (let i = 1; i <= 4; i++) {
      const stepEl = document.getElementById(`flow-step-${i}`);
      if (stepEl) {
        stepEl.classList.toggle("active", i === step);
        stepEl.classList.toggle("completed", i < step);
      }
    }
  }

  // Connection modal handlers
  if (btnOpenConnectionModal && connectionModal) {
    btnOpenConnectionModal.addEventListener("click", () => {
      if (modalServerUrl) modalServerUrl.value = state.serverUrl;
      if (modalApiKey) modalApiKey.value = state.apiKey;
      if (modalConnectionStatus) modalConnectionStatus.style.display = "none";
      connectionModal.style.display = "flex";
    });
  }

  if (modalBtnClose && connectionModal) {
    modalBtnClose.addEventListener("click", () => {
      connectionModal.style.display = "none";
    });
  }

  if (connectionModal) {
    connectionModal.addEventListener("click", (e) => {
      if (e.target === connectionModal) {
        connectionModal.style.display = "none";
      }
    });
  }

  if (modalBtnTest) {
    modalBtnTest.addEventListener("click", async () => {
      const testUrl = modalServerUrl ? modalServerUrl.value.trim() : "";
      const testKey = modalApiKey ? modalApiKey.value.trim() : "";
      if (!testUrl || !testKey) {
        if (modalConnectionStatus) {
          modalConnectionStatus.style.display = "block";
          modalConnectionStatus.style.background = "var(--accent-red-bg)";
          modalConnectionStatus.style.color = "var(--accent-red)";
          modalConnectionStatus.textContent = "يرجى إدخال عنوان الخادم ومفتاح API";
        }
        return;
      }
      modalBtnTest.disabled = true;
      modalBtnTest.textContent = "جاري الفحص...";
      try {
        const res = await fetch("/api/test-connection", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ server_url: testUrl, api_key: testKey }),
        });
        const data = await res.json();
        if (modalConnectionStatus) {
          modalConnectionStatus.style.display = "block";
          if (data.ok) {
            modalConnectionStatus.style.background = "var(--accent-green-bg)";
            modalConnectionStatus.style.color = "var(--accent-green)";
            modalConnectionStatus.textContent = "تم الاتصال بنجاح ✓";
            if (modalScopesDebug) {
              modalScopesDebug.textContent = `نطاق الصلاحيات: ${data.scopes ? data.scopes.join(", ") : "كامل الصلاحيات"}`;
            }
          } else {
            modalConnectionStatus.style.background = "var(--accent-red-bg)";
            modalConnectionStatus.style.color = "var(--accent-red)";
            modalConnectionStatus.textContent = `فشل الاتصال: ${data.message || "تأكد من عنوان الخادم وصلاحية المفتاح"}`;
          }
        }
      } catch (err) {
        if (modalConnectionStatus) {
          modalConnectionStatus.style.display = "block";
          modalConnectionStatus.style.background = "var(--accent-red-bg)";
          modalConnectionStatus.style.color = "var(--accent-red)";
          modalConnectionStatus.textContent = "تعذر الوصول إلى الخادم عبر الشبكة.";
        }
      } finally {
        modalBtnTest.disabled = false;
        modalBtnTest.innerHTML = `<svg class="icon icon-sm" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg><span>اختبار الاتصال</span>`;
      }
    });
  }

  if (modalBtnSave) {
    modalBtnSave.addEventListener("click", async () => {
      const newUrl = modalServerUrl ? modalServerUrl.value.trim() : "";
      const newKey = modalApiKey ? modalApiKey.value.trim() : "";
      try {
        await fetch("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ server_url: newUrl, api_key: newKey }),
        });
        state.serverUrl = newUrl;
        state.apiKey = newKey;
        if (el.serverUrlInput) el.serverUrlInput.value = newUrl;
        if (el.apiKeyInput) el.apiKeyInput.value = newKey;
        if (connectionModal) connectionModal.style.display = "none";
        testServerConnection();
      } catch (err) {
        showToast("فشل حفظ الإعدادات.", 'error');
      }
    });
  }

  // Destination Card handlers
  if (btnEditDestination && destinationEditRow) {
    btnEditDestination.addEventListener("click", () => {
      destinationEditRow.style.display = getComputedStyle(destinationEditRow).display === "none" ? "block" : "none";
      if (destinationEditRow.style.display === "block" && batchDestinationInput) {
        batchDestinationInput.focus();
      }
    });
  }

  if (btnCancelDestination && destinationEditRow) {
    btnCancelDestination.addEventListener("click", () => {
      destinationEditRow.style.display = "none";
    });
  }

  if (btnSaveDestination && destinationEditRow) {
    btnSaveDestination.addEventListener("click", async () => {
      const val = batchDestinationInput ? batchDestinationInput.value.trim() : "";
      if (destinationDisplayText && val) {
        destinationDisplayText.textContent = val;
      }
      destinationEditRow.style.display = "none";
      await syncAuthoritativeDestination();
    });
  }

  // Open Logs and Data Folder handlers
  const btnOpenLogs = document.getElementById("btn-open-logs");
  const btnOpenData = document.getElementById("btn-open-data");
  const ffmpegDiagInfo = document.getElementById("ffmpeg-diagnostic-info");
  const ffmpegWarningBanner = document.getElementById("ffmpeg-warning-banner");

  if (btnOpenLogs) {
    btnOpenLogs.addEventListener("click", async () => {
      try {
        await fetch("/api/system/open-logs-folder", { method: "POST" });
      } catch (e) {
        console.error("Failed to open logs folder", e);
      }
    });
  }

  if (btnOpenData) {
    btnOpenData.addEventListener("click", async () => {
      try {
        await fetch("/api/system/open-data-folder", { method: "POST" });
      } catch (e) {
        console.error("Failed to open data folder", e);
      }
    });
  }

  async function checkFFmpegDiagnostics() {
    try {
      const resp = await fetch("/api/diagnostics/ffmpeg");
      if (resp.ok) {
        const diag = await resp.json();
        if (ffmpegDiagInfo) {
          if (diag.available) {
            ffmpegDiagInfo.innerHTML = `أداة FFmpeg: <span class="p06-js-014">${diag.status_arabic}</span> • المسار: <span class="ltr-isolate" dir="ltr">${diag.ffmpeg_path}</span>`;
          } else {
            ffmpegDiagInfo.innerHTML = `أداة FFmpeg: <span class="p06-js-019">${diag.status_arabic}</span>`;
          }
        }
        if (ffmpegWarningBanner) {
          ffmpegWarningBanner.style.display = diag.available ? "none" : "flex";
        }
      }
    } catch (e) {
      console.warn("Could not query ffmpeg diagnostics", e);
    }
  }

  checkFFmpegDiagnostics();

  function renderBatchQueue(data) {
    if (!data || !data.active || !data.episodes || data.episodes.length === 0) {
      updateStepper(1);
      if (batchQueueCard) batchQueueCard.style.display = "none";
      if (btnAutoPipeline) btnAutoPipeline.style.display = "none";
      if (btnUploadReady) btnUploadReady.style.display = "none";
      if (btnPauseBatch) btnPauseBatch.style.display = "none";
      if (btnResumeBatch) btnResumeBatch.style.display = "none";
      if (btnStopAfterCurrent) btnStopAfterCurrent.style.display = "none";
      if (btnClearBatch) btnClearBatch.style.display = "none";
      if (batchTableBody) batchTableBody.innerHTML = "";
      if (needsReviewBanner) needsReviewBanner.style.display = "none";
      if (batchPollTimer) {
        clearInterval(batchPollTimer);
        batchPollTimer = null;
      }
      return;
    }

    if (batchQueueCard) batchQueueCard.style.display = "block";
    if (btnClearBatch) btnClearBatch.style.display = "inline-flex";
    if (!seasonHistoryLoaded) loadSeasonHistory();

    if (data.batch) {
      if (batchFolderInput) batchFolderInput.value = data.batch.season_folder_path || "";
      if (batchDestinationInput && data.batch.destination_label) {
        batchDestinationInput.value = data.batch.destination_label;
      }
      if (destinationDisplayText && data.batch.destination_label) {
        destinationDisplayText.textContent = data.batch.destination_label;
      }
      if (batchFolderIdInput) {
        batchFolderIdInput.value = data.batch.target_folder_id || "";
      }
      if (batchSeriesIdInput) {
        batchSeriesIdInput.value = data.batch.target_series_id || "";
      }
      if (batchSeasonIdInput) {
        batchSeasonIdInput.value = data.batch.target_season_id || "";
      }
    }

    const runner = data.runner || {};
    const isRunning = !!runner.is_running;
    const isUploading = !!runner.is_uploading;
    const batchStatus = data.batch ? data.batch.status : "ACTIVE";

    const preflight = (data.summary && data.summary.preflight) || {};
    const durationLabel = (seconds) => {
      if (!Number.isFinite(Number(seconds)) || Number(seconds) <= 0) return "غير متاح";
      const mins = Math.round(Number(seconds) / 60);
      const hours = Math.floor(mins / 60);
      const rest = mins % 60;
      if (hours && rest) return `${hours}س ${rest}د`;
      if (hours) return `${hours}س`;
      return `${mins}د`;
    };
    const clockDuration = (seconds) => {
      if (!Number.isFinite(Number(seconds)) || Number(seconds) < 0) return "—";
      const total = Math.floor(Number(seconds));
      const hours = Math.floor(total / 3600);
      const mins = Math.floor((total % 3600) / 60);
      const secs = total % 60;
      return hours > 0
        ? `${String(hours).padStart(2, "0")}:${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
        : `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
    };
    const setPreflight = (id, value) => {
      const el = document.getElementById(id);
      if (el) el.textContent = value;
    };
    const initialEta = Number(preflight.initial_transcode_seconds || 0);
    const remainingEta = Number(preflight.remaining_transcode_seconds || 0);
    setPreflight("preflight-count", `${data.episodes.length} حلقة`);
    setPreflight("preflight-duration", clockDuration(preflight.total_duration_seconds));
    setPreflight("preflight-source-size", data.summary?.total_source_bytes_formatted || "غير متاح");
    setPreflight("preflight-output-size", preflight.output_estimate_bytes ? `~${formatBytes(preflight.output_estimate_bytes)}` : "غير متاح");
    const safeTranscodeEta = Number(preflight.safe_transcode_seconds || 0);
    setPreflight("preflight-transcode", initialEta > 0
      ? `~${durationLabel(safeTranscodeEta)}`
      : "غير متاح");
    setPreflight("preflight-transcode-detail", initialEta > 0
      ? (preflight.eta_basis === "Studio processing history"
        ? `سجل فعلي · ${preflight.history_sample_count} عينة`
        : preflight.eta_basis === "local benchmark" ? "قياس الجهاز المحلي" : `مثالي ${durationLabel(initialEta)}`)
      : "يظهر التقدير بعد القياس");
    setPreflight("preflight-packaging", preflight.packaging_seconds == null
      ? "غير مقاس"
      : `~${durationLabel(preflight.safe_packaging_seconds)}`);
    setPreflight("preflight-packaging-detail", preflight.packaging_seconds == null
      ? "لا يوجد قياس بعد"
      : `مثالي ${durationLabel(preflight.packaging_seconds)} • تقدير من عينة`);
    setPreflight("preflight-upload", preflight.upload_seconds == null ? "—" : durationLabel(preflight.upload_seconds));
    setPreflight("preflight-upload-detail", preflight.upload_seconds == null ? "يُعرض بعد توفر سجل رفع فعلي" : `حسب ${preflight.upload_basis || "السجل الفعلي"}`);
    const totalIdeal = initialEta + Number(preflight.packaging_seconds || 0);
    const totalSafe = safeTranscodeEta + Number(preflight.safe_packaging_seconds || 0);
    setPreflight("preflight-total", initialEta > 0
      ? `~${durationLabel(totalSafe)} + الرفع`
      : "غير مكتمل القياس");
    setPreflight("preflight-total-detail", initialEta > 0
      ? `${preflight.upload_seconds == null ? "الرفع غير محسوب" : "يشمل تقدير الرفع"} • المثالي ${durationLabel(totalIdeal + Number(preflight.upload_seconds || 0))}`
      : "يشمل التحويل والتجهيز فقط");
    setPreflight("preflight-ready", `${preflight.ready_count || 0} جاهزة`);
    setPreflight("preflight-review", `${preflight.needs_review_count || 0} تحتاج مراجعة`);
    const disk = preflight.disk_preflight || {};
    setPreflight("preflight-disk", disk.free_bytes ? `${disk.work_drive} · ${formatBytes(disk.free_bytes)}` : "تعذر القياس");
    setPreflight("preflight-disk-status", disk.free_bytes
      ? `المطلوب ${formatBytes(disk.required_bytes)} · ${disk.safe ? "المساحة كافية ✓" : "المساحة غير كافية"}`
      : "تعذر فحص مساحة القرص");
    setPreflight("preflight-disk-write", Number.isFinite(Number(runner.disk_write_mbps))
      ? `${Number(runner.disk_write_mbps).toFixed(1)} MB/s (إجمالي الأقراص)`
      : "يقاس أثناء التشغيل");
    const actualTranscode = data.episodes.reduce((sum, ep) => sum + Number(ep.processing_history?.successful?.total_transcode_duration_seconds || ep.encoding_elapsed_seconds || 0), 0);
    const actualPackage = data.episodes.reduce((sum, ep) => sum + Number(ep.processing_history?.successful?.packaging_duration_seconds || 0) + Number(ep.processing_history?.successful?.verification_duration_seconds || 0), 0);
    const actualUpload = data.episodes.reduce((sum, ep) => sum + Number(ep.processing_history?.successful_upload?.upload_duration_seconds || 0), 0);
    const activeHistoryEpisode = data.episodes.find(ep => ["ENCODING", "PACKAGING", "VERIFYING_LOCAL", "UPLOADING", "SERVER_VERIFYING"].includes(ep.state));
    setPreflight("preflight-actual-transcode", actualTranscode > 0 ? clockDuration(actualTranscode) : "لا يوجد قياس بعد");
    setPreflight("preflight-actual-package", actualPackage > 0 ? clockDuration(actualPackage) : "لا يوجد قياس بعد");
    setPreflight("preflight-actual-upload", actualUpload > 0 ? clockDuration(actualUpload) : "لا يوجد قياس بعد");
    setPreflight("preflight-episode-elapsed", activeHistoryEpisode?.pipeline_elapsed_seconds > 0 ? clockDuration(activeHistoryEpisode.pipeline_elapsed_seconds) : "—");
    const fontPool = preflight.font_pool || {};
    const fontPoolSummary = document.getElementById("season-font-pool-summary");
    const fontPoolRows = document.getElementById("season-font-pool-rows");
    if (fontPoolSummary) {
      fontPoolSummary.textContent = `خطوط الموسم — مطلوب ${fontPool.required_families || 0} • متوفر ${fontPool.available_families || 0} • مفقود ${fontPool.missing_families || 0}`;
    }
    if (fontPoolRows) {
      fontPoolRows.replaceChildren();
      (fontPool.fonts || []).forEach((font) => {
        const row = document.createElement("tr");
        [font.family_name, (font.sources || []).join(", ") || "—", (font.episodes || []).join(", "), font.available ? "متوفر ✓" : "مفقود"].forEach((value) => {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.appendChild(cell);
        });
        fontPoolRows.appendChild(row);
      });
    }
    const etaDynamic = document.getElementById("preflight-dynamic-eta");
    const hasObservedThroughput = preflight.eta_basis === "measured throughput";
    if (etaDynamic) etaDynamic.style.display = hasObservedThroughput && isRunning ? "block" : "none";
    if (hasObservedThroughput && isRunning) {
      setPreflight("preflight-initial", durationLabel(preflight.initial_transcode_seconds));
      setPreflight("preflight-remaining", durationLabel(remainingEta));
      setPreflight("preflight-finish", preflight.finish_at || "يتحدث بعد اكتمال القياس");
    }

    if (data.summary) {
      if (batchTotalCount) batchTotalCount.textContent = data.summary.total_episodes || "0";
      if (batchTotalBytes) batchTotalBytes.textContent = data.summary.total_source_bytes_formatted || "0 MB";

      const overallPct = Math.round(data.summary.overall_progress || 0);
      setProgressPercent(batchOverallProgressBar, overallPct);
      if (batchOverallProgressText) batchOverallProgressText.textContent = `${overallPct}%`;

      if (badgeTotal) badgeTotal.textContent = `${data.summary.total_episodes || 0} حلقة`;
      if (badgeReady) badgeReady.textContent = `${data.summary.ready_count || 0} جاهزة`;
      if (badgeReadyToUpload) badgeReadyToUpload.textContent = `${data.summary.ready_to_upload_count || 0} جاهز للرفع`;
      if (badgeUploading) {
        badgeUploading.textContent = `${data.summary.uploading_count || 0} رفع`;
        badgeUploading.style.display = data.summary.uploading_count > 0 ? "inline-block" : "none";
      }
      if (badgeEncoding) {
        badgeEncoding.textContent = `${data.summary.encoding_count || 0} تحويل`;
        badgeEncoding.style.display = data.summary.encoding_count > 0 ? "inline-block" : "none";
      }
      if (badgePending) badgePending.textContent = `${data.summary.pending_count || 0} انتظار`;
      if (badgeFailed) {
        badgeFailed.textContent = `${data.summary.failed_count || 0} فشل`;
        badgeFailed.style.display = data.summary.failed_count > 0 ? "inline-block" : "none";
      }
    }

    if (batchOverallStatusLine && runner.overall_status_line) {
      batchOverallStatusLine.textContent = runner.overall_status_line;
    }

    // Dual Pipelined Live Tasks
    if (runner.gpu_task) {
      if (gpuTaskBadge) {
        gpuTaskBadge.textContent = "جاري التحويل";
        gpuTaskBadge.className = "status-badge active";
      }
      if (gpuTaskFile) {
        const epLabel = runner.gpu_task.display_label ? `الحلقة ${runner.gpu_task.display_label}` : "";
        gpuTaskFile.textContent = epLabel ? `${epLabel} • ${runner.gpu_task.filename}` : runner.gpu_task.filename;
      }
      const prog = Math.round(runner.gpu_task.progress || 0);
      setProgressPercent(gpuTaskProgBar, prog);
      if (gpuTaskMetrics) {
        const hw = runner.gpu_task.hardware || {};
        const gpu = hw.gpu || {};
        const cpu = hw.cpu_ram || {};
        const vram = gpu.vram_total_mb ? `${Math.round(gpu.vram_used_mb || 0)}/${gpu.vram_total_mb} MB VRAM` : "VRAM —";
        gpuTaskMetrics.innerHTML = `<span class="ltr-isolate p06-js-020" dir="ltr">${runner.gpu_task.fps || 0} FPS • ${runner.gpu_task.speed || '1x'} • ${prog}%</span><small class="episode-source-duration">${gpu.gpu_name || "GPU —"} · ${vram} · CPU ${cpu.cpu_util_pct ?? "—"}%</small>`;
      }
      if (gpuTaskEta) {
        const qStr = runner.gpu_task.quality ? `[${runner.gpu_task.quality}] ` : "";
        const estimate = runner.gpu_task.eta && runner.gpu_task.eta !== "00:00:00" ? `${qStr}متبقي ${runner.gpu_task.eta}` : `${qStr}متبقي --:--`;
        gpuTaskEta.textContent = runner.gpu_task.resource_warning ? `${runner.gpu_task.resource_warning} • ${estimate}` : estimate;
      }
    } else {
      if (gpuTaskBadge) {
        gpuTaskBadge.textContent = "خامل";
        gpuTaskBadge.className = "status-badge pending";
      }
      if (gpuTaskFile) gpuTaskFile.textContent = "لا توجد معالجة جارية";
      setProgressPercent(gpuTaskProgBar, 0);
      if (gpuTaskMetrics) gpuTaskMetrics.textContent = "-";
      if (gpuTaskEta) gpuTaskEta.textContent = "-";
    }

    if (runner.network_task) {
      if (networkTaskBadge) {
        networkTaskBadge.textContent = "جاري الرفع";
        networkTaskBadge.className = "status-badge ready";
      }
      if (networkTaskFile) {
        const epLabel = runner.network_task.display_label ? `الحلقة ${runner.network_task.display_label}` : "";
        networkTaskFile.textContent = epLabel ? `${epLabel} • ${runner.network_task.filename}` : runner.network_task.filename;
      }
      const prog = Math.round(runner.network_task.progress || 0);
      setProgressPercent(networkTaskProgBar, prog);
      if (networkTaskSpeed) {
        networkTaskSpeed.innerHTML = `<span class="ltr-isolate p06-js-020" dir="ltr">${runner.network_task.upload_speed || '-'} • ${prog}%</span>`;
      }
      if (networkTaskEta) {
        const remStr = runner.network_task.upload_eta && runner.network_task.upload_eta !== "00:00:00" ? `متبقي ${runner.network_task.upload_eta}` : "";
        const byteStr = runner.network_task.uploaded_bytes_formatted ? `${runner.network_task.uploaded_bytes_formatted} / ${runner.network_task.package_bytes_formatted || ''}` : "";
        networkTaskEta.textContent = remStr ? `${byteStr} (${remStr})` : byteStr || "متبقي --:--";
      }
    } else {
      if (networkTaskBadge) {
        networkTaskBadge.textContent = "خامل";
        networkTaskBadge.className = "status-badge pending";
      }
      if (networkTaskFile) networkTaskFile.textContent = "لا يوجد رفع جاري";
      setProgressPercent(networkTaskProgBar, 0);
      if (networkTaskSpeed) networkTaskSpeed.textContent = "-";
      if (networkTaskEta) networkTaskEta.textContent = "-";
    }

    const hasReadyToUpload = data.episodes.some(ep => ep.state === "READY_TO_UPLOAD");
    const hasUnfinished = data.episodes.some(ep => ep.state !== "READY" && ep.state !== "SKIPPED");
    const hasFailed = data.episodes.some(ep => ep.state === "FAILED");
    const isCompleted = (data.batch && data.batch.status === "COMPLETED") || (!hasUnfinished && data.episodes.length > 0);

    // Update 4-step Stepper
    if (isCompleted) {
      updateStepper(4);
      const s4 = document.getElementById("flow-step-4");
      if (s4) { s4.classList.remove("active"); s4.classList.add("completed"); }
    } else if (isRunning || isUploading) {
      updateStepper(4);
    } else {
      updateStepper(3);
    }

    // Needs Review Alert Banner
    const reviewEp = data.episodes.find(ep =>
      ep.state === "NEEDS_REVIEW" ||
      (ep.font_status && (ep.font_status.includes("مفقود") || ep.font_status.includes("مراجعة"))) ||
      (ep.missing_fonts_list && ep.missing_fonts_list.length > 0)
    );

    if (reviewEp && needsReviewBanner) {
      needsReviewBanner.style.display = "flex";
      if (needsReviewText) {
        const epLabel = reviewEp.display_label || String(reviewEp.order_index).padStart(2, "0");
        needsReviewText.textContent = `الحلقة ${epLabel} تحتاج مراجعة الترجمة أو الخطوط قبل المتابعة.`;
      }
      if (btnReviewScroll) {
        btnReviewScroll.onclick = () => {
          const row = document.getElementById(`ep-row-${reviewEp.id}`);
          if (row) {
            row.scrollIntoView({ behavior: "smooth", block: "center" });
            row.classList.add("row-needs-review");
          }
        };
      }
    } else if (needsReviewBanner) {
      needsReviewBanner.style.display = "none";
    }

    // Manage batch upload button
    if (btnUploadReady) {
      if (hasReadyToUpload || isUploading) {
        btnUploadReady.style.display = "inline-flex";
        btnUploadReady.disabled = isUploading;
        btnUploadReady.innerHTML = `<svg class="icon" viewBox="0 0 24 24"><path d="M18 10h-1.26A8 8 0 1 0 9 20h9a5 5 0 0 0 0-10z"/><polyline points="16 16 12 12 8 16"/><line x1="12" y1="12" x2="12" y2="21"/></svg><span>${isUploading ? "جاري الرفع..." : "رفع الجاهز"}</span>`;
      } else {
        btnUploadReady.style.display = "none";
      }
    }

    // Manage batch control buttons
    if (isRunning || isUploading) {
      if (btnAutoPipeline) btnAutoPipeline.style.display = "none";
      if (btnResumeBatch) btnResumeBatch.style.display = "none";
      if (btnPauseBatch) {
        btnPauseBatch.style.display = "inline-flex";
        btnPauseBatch.disabled = !!runner.pause_requested;
        btnPauseBatch.innerHTML = `<svg class="icon" viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16" fill="currentColor"/><rect x="14" y="4" width="4" height="16" fill="currentColor"/></svg><span>${runner.pause_requested ? "جاري الإيقاف..." : "إيقاف مؤقت"}</span>`;
      }
      if (btnStopAfterCurrent) {
        btnStopAfterCurrent.style.display = "inline-flex";
        btnStopAfterCurrent.disabled = !!runner.stop_after_current;
        btnStopAfterCurrent.innerHTML = `<svg class="icon" viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" fill="currentColor"/></svg><span>${runner.stop_after_current ? "جاري الإيقاف..." : "إيقاف بعد الحالية"}</span>`;
      }
      if (batchStatusText) {
        batchStatusText.textContent = runner.stop_after_current
          ? "جاري الإيقاف بعد الحالية..."
          : (runner.pause_requested
            ? "جاري الإيقاف المؤقت..."
            : (isRunning && isUploading
              ? "معالجة متزامنة (GPU + Network)"
              : (isRunning ? "جاري التحويل" : "جاري الرفع")));
      }
    } else {
      if (btnPauseBatch) btnPauseBatch.style.display = "none";
      if (btnStopAfterCurrent) btnStopAfterCurrent.style.display = "none";

      if (isCompleted) {
        if (btnAutoPipeline) btnAutoPipeline.style.display = "none";
        if (btnResumeBatch) btnResumeBatch.style.display = "none";
        if (batchStatusText) {
          batchStatusText.textContent = "اكتمل الموسم بنجاح ✓";
        }
      } else if (batchStatus === "PAUSED" || hasFailed) {
        if (btnAutoPipeline) btnAutoPipeline.style.display = "none";
        if (btnResumeBatch) {
          btnResumeBatch.style.display = "inline-flex";
          btnResumeBatch.innerHTML = `<svg class="icon" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3" fill="currentColor"/></svg><span>استئناف</span>`;
        }
        if (batchStatusText) batchStatusText.textContent = hasFailed ? "توقف مؤقت بسبب خطأ" : "متوقف مؤقتاً";
      } else {
        if (btnAutoPipeline) {
          btnAutoPipeline.style.display = "inline-flex";
          btnAutoPipeline.innerHTML = `<svg class="icon" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3" fill="currentColor"/></svg><span>تشغيل الموسم تلقائياً</span>`;
        }
        if (btnResumeBatch) btnResumeBatch.style.display = "none";
        if (batchStatusText) batchStatusText.textContent = "جاهز للبدء";
      }
    }

    // Manage polling timer
    if (isRunning || isUploading) {
      if (!batchPollTimer) {
        batchPollTimer = setInterval(loadCurrentBatch, 1200);
      }
    } else {
      if (batchPollTimer) {
        clearInterval(batchPollTimer);
        batchPollTimer = null;
      }
    }

    // Render 9-Column Table Rows
    if (batchTableBody) {
      batchTableBody.innerHTML = "";
      data.episodes.forEach((ep) => {
        const tr = document.createElement("tr");
        tr.id = `ep-row-${ep.id}`;

        const isCurrentActive = runner.current_episode_id === ep.id && isRunning;
        if (isCurrentActive) {
          tr.style.background = "rgba(6, 182, 212, 0.05)";
        }

        // Col 1: Order (#)
        const tdOrder = document.createElement("td");
        tdOrder.style.textAlign = "center";
        tdOrder.style.fontWeight = "700";
        tdOrder.style.fontFamily = "var(--font-mono)";
        tdOrder.textContent = ep.display_label || String(ep.order_index).padStart(2, "0");

        // Col 2: Filename (اسم الحلقة) - LTR isolate
        const tdName = document.createElement("td");
        tdName.style.textAlign = "right";
        const bdiName = document.createElement("bdi");
        bdiName.className = "ltr-isolate";
        bdiName.setAttribute("dir", "ltr");
        bdiName.style.fontFamily = "var(--font-mono)";
        bdiName.textContent = ep.filename;
        tdName.appendChild(bdiName);
        if (Number(ep.duration_seconds) > 0) {
          const sourceDuration = document.createElement("small");
          sourceDuration.className = "episode-source-duration";
          sourceDuration.textContent = `مدة المصدر · ${clockDuration(ep.duration_seconds)}`;
          tdName.appendChild(sourceDuration);
        }

        // Col 3: Subtitles (الترجمة)
        const tdSub = document.createElement("td");
        tdSub.style.textAlign = "center";
        let subClass = "badge-clean sub-none";
        let subText = "بدون";
        const subPath = (ep.subtitle_path || "").toLowerCase();
        const subSource = ep.subtitle_source || "none";
        const hasFontIssue = ep.missing_fonts_list && ep.missing_fonts_list.length > 0;

        if (hasFontIssue) {
          subClass = "badge-clean sub-review";
          subText = "مراجعة الخطوط";
        } else if (subSource === "external" || subPath.endsWith(".ass")) {
          subClass = "badge-clean sub-ass";
          subText = subPath.endsWith(".ass") ? "ASS ✓" : "مرفقة ✓";
        } else if (subPath.endsWith(".srt")) {
          subClass = "badge-clean sub-srt";
          subText = "SRT ✓";
        } else if (subSource === "embedded") {
          subClass = "badge-clean sub-embedded";
          subText = "مضمنة";
        }
        const subBadge = document.createElement("span");
        subBadge.className = subClass;
        subBadge.textContent = subText;
        tdSub.appendChild(subBadge);

        // Subtitle candidate switcher if multiple available
        const candidates = ep.subtitle_candidates_list || [];
        if (candidates.length > 1) {
          const sel = document.createElement("select");
          sel.className = "form-input";
          sel.style.display = "block";
          sel.style.fontSize = "11px";
          sel.style.padding = "2px 4px";
          sel.style.height = "22px";
          sel.style.maxWidth = "110px";
          sel.style.margin = "4px auto 0 auto";
          candidates.forEach((c) => {
            const opt = document.createElement("option");
            if (c.source === "external") {
              opt.value = `ext:${c.path}`;
              opt.textContent = c.label;
              if (ep.subtitle_source === "external" && ep.subtitle_path === c.path) opt.selected = true;
            } else if (c.source === "embedded") {
              opt.value = `emb:${c.stream_index}`;
              opt.textContent = c.label;
              if (ep.subtitle_source === "embedded" && ep.subtitle_stream_index === c.stream_index) opt.selected = true;
            } else {
              opt.value = "none";
              opt.textContent = c.label;
              if (ep.subtitle_source === "none") opt.selected = true;
            }
            sel.appendChild(opt);
          });
          sel.onchange = async (e) => {
            const v = e.target.value;
            let payload = { source: "none" };
            if (v.startsWith("ext:")) {
              payload = { source: "external", path: v.slice(4) };
            } else if (v.startsWith("emb:")) {
              payload = { source: "embedded", stream_index: parseInt(v.slice(4), 10) };
            }
            try {
              await fetch(`/api/batch/episodes/${ep.id}/select-subtitle`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
              });
              loadCurrentBatch();
            } catch (err) {
              showToast("فشل تغيير الترجمة: " + err.message, 'error');
            }
          };
          tdSub.appendChild(sel);
        }

        // Col 4: Current Quality (الجودة الحالية)
        const tdQuality = document.createElement("td");
        tdQuality.style.textAlign = "center";
        if (ep.state === "ENCODING" && ep.current_quality) {
          tdQuality.innerHTML = `<span class="ltr-isolate p06-js-021" dir="ltr">${ep.current_quality}</span>`;
        } else if (ep.state === "READY" || ep.state === "READY_TO_UPLOAD" || ep.state === "UPLOADING" || ep.state === "SERVER_VERIFYING") {
          tdQuality.innerHTML = `<span class="p06-js-022">${ep.processing_history?.successful?.renditions ? Object.keys(ep.processing_history.successful.renditions).length + " جودات" : "مكتملة"}</span>`;
        } else {
          tdQuality.innerHTML = `<span class="p06-js-017">—</span>`;
        }

        // Col 5: State (الحالة)
        const tdState = document.createElement("td");
        tdState.style.textAlign = "center";
        const stateBadge = document.createElement("span");
        let stClass = "status-badge pending";
        let stText = "في الانتظار";

        switch (ep.state) {
          case "READY":
            stClass = "status-badge ready";
            stText = "جاهز ✓";
            break;
          case "READY_TO_UPLOAD":
            stClass = "status-badge attached";
            stText = "جاهز للرفع";
            break;
          case "UPLOADING":
            stClass = "status-badge active";
            stText = "جاري الرفع";
            break;
          case "SERVER_VERIFYING":
            stClass = "status-badge active";
            stText = "تحقق الخادم";
            break;
          case "ENCODING":
            stClass = "status-badge active";
            stText = "جاري التحويل";
            break;
          case "PACKAGING":
          case "VERIFYING_LOCAL":
            stClass = "status-badge active";
            stText = "تجهيز الحزمة";
            break;
          case "NEEDS_REVIEW":
            stClass = "status-badge failed";
            stText = "تحتاج مراجعة";
            break;
          case "FAILED":
            stClass = "status-badge failed";
            stText = "فشل";
            break;
          case "SKIPPED":
            stClass = "status-badge skipped";
            stText = "تم التخطي";
            break;
          case "PAUSED":
            stClass = "status-badge paused";
            stText = "متوقف مؤقتاً";
            break;
          default:
            stClass = "status-badge pending";
            stText = "في الانتظار";
        }
        stateBadge.className = stClass;
        stateBadge.textContent = stText;
        if (ep.error) stateBadge.title = ep.error;
        tdState.appendChild(stateBadge);

        // Col 6: Progress (التقدم)
        const tdProgress = document.createElement("td");
        tdProgress.style.textAlign = "center";
        const pct = Math.max(0, Math.min(100, Math.round(ep.progress || 0)));
        tdProgress.innerHTML = `
          <div class="p06-js-023">
            <div class="progress-bar-wrap p06-js-024">
              <div class="progress-bar-fill" data-progress="${pct}"></div>
            </div>
            <span class="ltr-isolate p06-js-025" dir="ltr">${pct}%</span>
          </div>
        `;

        // Col 7: Speed (السرعة)
        const tdSpeed = document.createElement("td");
        tdSpeed.style.textAlign = "center";
        if (ep.state === "UPLOADING" && ep.upload_speed) {
          tdSpeed.innerHTML = `<span class="ltr-isolate p06-js-026" dir="ltr">${ep.upload_speed}</span>`;
        } else if (ep.fps > 0) {
          tdSpeed.innerHTML = `<span class="ltr-isolate p06-js-026" dir="ltr">${ep.fps} FPS</span>`;
        } else {
          tdSpeed.innerHTML = `<span class="p06-js-017">—</span>`;
        }

        // Col 8: Size (الحجم)
        const tdSize = document.createElement("td");
        tdSize.style.textAlign = "center";
        if (ep.state === "UPLOADING" && ep.uploaded_bytes > 0) {
          const upStr = ep.uploaded_bytes_formatted || (ep.uploaded_bytes / 1048576).toFixed(1) + ' MB';
          tdSize.innerHTML = `<span class="ltr-isolate p06-js-027" dir="ltr">${upStr}</span>`;
        } else if (ep.package_bytes > 0) {
          tdSize.innerHTML = `<span class="ltr-isolate p06-js-028" dir="ltr">${ep.package_bytes_formatted || (ep.package_bytes / 1048576).toFixed(1) + ' MB'}</span>`;
        } else if (ep.file_size_bytes > 0) {
          tdSize.innerHTML = `<span class="ltr-isolate p06-js-029" dir="ltr">${(ep.file_size_bytes / 1048576).toFixed(1)} MB</span>`;
        } else {
          tdSize.innerHTML = `<span class="p06-js-017">—</span>`;
        }

        // Col 9: Actions (الإجراءات)
        const tdActions = document.createElement("td");
        tdActions.style.textAlign = "center";

        if (ep.state === "READY") {
          const retainedSpan = document.createElement("span");
          retainedSpan.className = ep.local_package_cleaned ? "status-badge pending" : "status-badge ready";
          retainedSpan.textContent = ep.local_package_cleaned ? "حُذفت سابقاً" : "الحزمة المحلية جاهزة ✓";
          tdActions.appendChild(retainedSpan);
          if (!ep.local_package_cleaned) appendPackageActions(tdActions, ep.id, true);
        } else if (ep.state === "READY_TO_UPLOAD") {
          const uploadBtn = document.createElement("button");
          uploadBtn.type = "button";
          uploadBtn.className = "btn btn-sm btn-primary";
          uploadBtn.style.marginLeft = "4px";
          uploadBtn.textContent = "رفع الآن";
          uploadBtn.dataset.action = "reupload-package";
          uploadBtn.dataset.episodeId = ep.id;
          uploadBtn.onclick = (event) => window.reuploadBatchPackage(ep.id, event.currentTarget);

          const delBtn = document.createElement("button");
          delBtn.type = "button";
          delBtn.className = "btn btn-sm btn-danger";
          delBtn.textContent = "حذف";
          delBtn.onclick = () => window.deleteBatchEpisode(ep.id);

          tdActions.appendChild(uploadBtn);
          appendPackageActions(tdActions, ep.id, false);
          tdActions.appendChild(delBtn);
        } else if (ep.state === "UPLOADING") {
          const upSpan = document.createElement("span");
          upSpan.style.color = "var(--accent-cyan)";
          upSpan.style.fontSize = "12px";
          upSpan.style.fontWeight = "700";
          upSpan.textContent = "جاري الرفع...";
          tdActions.appendChild(upSpan);
        } else if (ep.state === "SERVER_VERIFYING") {
          const verSpan = document.createElement("span");
          verSpan.style.color = "var(--accent-cyan)";
          verSpan.style.fontSize = "12px";
          verSpan.style.fontWeight = "700";
          verSpan.textContent = "تحقق الخادم...";
          tdActions.appendChild(verSpan);
        } else if (ep.state === "FAILED" || ep.state === "NEEDS_REVIEW") {
          const retryBtn = document.createElement("button");
          retryBtn.type = "button";
          retryBtn.className = "btn btn-sm btn-primary";
          retryBtn.style.marginLeft = "4px";
          retryBtn.textContent = ep.state === "NEEDS_REVIEW" ? "مراجعة" : "إعادة المحاولة";
          retryBtn.onclick = ep.state === "NEEDS_REVIEW"
            ? () => window.reviewBatchEpisode(ep.id)
            : () => window.retryBatchEpisode(ep.id);

          const skipBtn = document.createElement("button");
          skipBtn.type = "button";
          skipBtn.className = "btn btn-sm btn-secondary";
          skipBtn.style.marginLeft = "4px";
          skipBtn.textContent = "تخطي";
          skipBtn.onclick = () => window.skipBatchEpisode(ep.id);

          const delBtn = document.createElement("button");
          delBtn.type = "button";
          delBtn.className = "btn btn-sm btn-danger";
          delBtn.textContent = "حذف";
          delBtn.onclick = () => window.deleteBatchEpisode(ep.id);

          tdActions.appendChild(retryBtn);
          if (ep.state === "FAILED" && !ep.local_package_cleaned && Number(ep.package_bytes || 0) > 0) {
            appendPackageActions(tdActions, ep.id, true);
          }
          if (ep.state === "NEEDS_REVIEW" && ep.missing_fonts_list && ep.missing_fonts_list.length > 0) {
            const fallbackBtn = document.createElement("button");
            fallbackBtn.type = "button";
            fallbackBtn.className = "btn btn-sm btn-secondary";
            fallbackBtn.style.marginLeft = "4px";
            fallbackBtn.textContent = "متابعة بخطوط ASS البديلة";
            fallbackBtn.title = "يحافظ على ملف ASS كما هو ويسمح لمشغل العرض باستخدام خط النظام البديل";
            fallbackBtn.onclick = () => window.approveBatchAssFallback(ep.id, ep.missing_fonts_list);
            tdActions.appendChild(fallbackBtn);
          }
          tdActions.appendChild(skipBtn);
          tdActions.appendChild(delBtn);
        } else if (isCurrentActive) {
          const activeSpan = document.createElement("span");
          activeSpan.style.color = "var(--accent-cyan)";
          activeSpan.style.fontSize = "12px";
          activeSpan.style.fontWeight = "700";
          activeSpan.textContent = "جاري التحويل...";
          tdActions.appendChild(activeSpan);
        } else {
          const skipBtn = document.createElement("button");
          skipBtn.type = "button";
          skipBtn.className = "btn btn-sm btn-secondary";
          skipBtn.style.marginLeft = "4px";
          skipBtn.textContent = "تخطي";
          skipBtn.onclick = () => window.skipBatchEpisode(ep.id);

          const delBtn = document.createElement("button");
          delBtn.type = "button";
          delBtn.className = "btn btn-sm btn-danger";
          delBtn.textContent = "حذف";
          delBtn.onclick = () => window.deleteBatchEpisode(ep.id);

          tdActions.appendChild(skipBtn);
          tdActions.appendChild(delBtn);
        }

        tr.appendChild(tdOrder);
        tr.appendChild(tdName);
        tr.appendChild(tdSub);
        tr.appendChild(tdQuality);
        tr.appendChild(tdState);
        tr.appendChild(tdProgress);
        if (tdSpeed.textContent.trim() || tdSpeed.innerHTML.includes("FPS") || tdSpeed.innerHTML.includes("KB/s")) {
          const speedLine = document.createElement("small");
          speedLine.className = "episode-source-duration quality-speed-line";
          speedLine.innerHTML = tdSpeed.innerHTML;
          tdQuality.appendChild(speedLine);
        }
        const tdElapsed = document.createElement("td");
        tdElapsed.className = "episode-elapsed-time";
        tdElapsed.style.textAlign = "center";
        const inProgress = ["PROBING", "READY_TO_ENCODE", "ENCODING", "PACKAGING", "VERIFYING_LOCAL", "UPLOADING", "SERVER_VERIFYING"].includes(ep.state);
        const elapsedSeconds = Number(inProgress ? (ep.current_stage_elapsed_seconds || ep.encoding_elapsed_seconds || 0) : (ep.encoding_elapsed_seconds || 0));
        const elapsedLabel = clockDuration(elapsedSeconds);
        tdElapsed.textContent = inProgress && elapsedSeconds > 0
          ? `جارٍ · ${elapsedLabel}`
          : elapsedSeconds > 0 ? elapsedLabel : "—";
        tdElapsed.title = elapsedSeconds > 0
          ? (inProgress ? "الوقت المنقضي في المرحلة الحالية" : "مدة التحويل الفعلية")
          : "تظهر المدة بعد بدء التحويل";
        if (["ENCODING", "PACKAGING", "VERIFYING_LOCAL", "UPLOADING", "SERVER_VERIFYING"].includes(ep.state) && ep.eta && ep.eta !== "00:00:00") {
          const eta = document.createElement("small");
          eta.className = "episode-source-duration";
          eta.textContent = `متبقي ~${ep.eta}`;
          tdElapsed.appendChild(eta);
        }
        tr.appendChild(tdElapsed);
        tr.appendChild(tdSize);
        tr.appendChild(tdActions);

        tr.classList.add("episode-row-clickable");
        tr.title = "اضغط لعرض سجل التحويل والتجهيز والرفع";
        tr.addEventListener("click", (event) => {
          if (event.target.closest("button,select,a,input")) return;
          showEpisodeHistory(ep);
        });
        batchTableBody.appendChild(tr);
      });
    }
  }

  function showEpisodeHistory(ep) {
    const modal = document.getElementById("episode-history-modal");
    const title = document.getElementById("episode-history-title");
    const content = document.getElementById("episode-history-content");
    if (!modal || !title || !content) return;
    const history = ep.processing_history || ep.settings?.processing_history || {};
    const successful = history.successful || {};
    const renditions = successful.renditions || {};
    const legacyTimes = ep.settings?.rendition_elapsed_seconds || {};
    const fmt = (value) => {
      const seconds = Number(value || 0);
      if (!Number.isFinite(seconds) || seconds <= 0) return "—";
      const whole = Math.floor(seconds);
      return `${String(Math.floor(whole / 3600)).padStart(2, "0")}:${String(Math.floor((whole % 3600) / 60)).padStart(2, "0")}:${String(whole % 60).padStart(2, "0")}`;
    };
    const esc = (value) => String(value ?? "—").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
    title.textContent = ep.display_label || ep.filename || "الحلقة";
    const qualityRows = ["2160p", "1080p", "720p", "480p", "360p"].filter(q => renditions[q] || legacyTimes[q]).map(q => {
      const record = renditions[q] || {};
      return `<tr><th>${q === "2160p" ? "4K" : q}</th><td class="ltr">${fmt(record.duration_seconds || legacyTimes[q])}</td><td class="ltr">${record.fps ? `${esc(record.fps)} FPS` : "—"}</td><td class="ltr">${record.speed_multiplier ? `${Number(record.speed_multiplier).toFixed(2)}×` : "—"}</td></tr>`;
    }).join("") || `<tr><td colspan="4">لا يوجد سجل تفصيلي لهذه الحلقة بعد</td></tr>`;
    const duration = Number(ep.duration_seconds || history.source_duration_seconds || 0);
    const resolution = ep.source_width && ep.source_height ? `${ep.source_width}×${ep.source_height}` : "—";
    content.innerHTML = `<div class="episode-history-grid">
      <div class="episode-history-cell"><span>المصدر</span><b>${esc(resolution)}</b></div>
      <div class="episode-history-cell"><span>مدة المصدر</span><b>${fmt(duration)}</b></div>
      <div class="episode-history-cell"><span>إجمالي التحويل</span><b>${fmt(successful.total_transcode_duration_seconds || ep.encoding_elapsed_seconds)}</b></div>
      <div class="episode-history-cell"><span>التجهيز والتحقق المحلي</span><b>${fmt(Number(successful.packaging_duration_seconds || 0) + Number(successful.verification_duration_seconds || 0))}</b></div>
      <div class="episode-history-cell"><span>الرفع والتحقق</span><b>${fmt(history.successful_upload?.upload_duration_seconds)}</b></div>
      <div class="episode-history-cell"><span>المسار الكامل</span><b>${fmt(successful.total_pipeline_duration_seconds)}</b></div>
      <div class="episode-history-cell"><span>المحاولات / الإعادات</span><b>${esc(history.attempt_count || (successful.success ? 1 : 0))} / ${esc(history.retry_count || 0)}</b></div>
    </div><table><thead><tr><th>الجودة</th><th>الزمن</th><th>FPS</th><th>السرعة</th></tr></thead><tbody>${qualityRows}</tbody></table>`;
    modal.hidden = false;
    const close = () => { modal.hidden = true; };
    document.getElementById("episode-history-close")?.addEventListener("click", close, { once: true });
    modal.onclick = (event) => { if (event.target === modal) close(); };
  }

  function appendPackageActions(container, episodeId, showReupload) {
    const addButton = (label, action, secondary = true, dataAction = "") => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `btn btn-sm ${secondary ? "btn-secondary" : "btn-primary"}`;
      if (dataAction) {
        button.dataset.action = dataAction;
        button.dataset.episodeId = episodeId;
      }
      button.textContent = label;
      button.style.margin = "2px";
      button.onclick = action;
      container.appendChild(button);
    };
    addButton("فحص الحزمة", () => window.inspectBatchPackage(episodeId));
    addButton("فتح المجلد", () => window.openBatchPackage(episodeId));
    if (showReupload) addButton("إعادة الرفع", (event) => window.reuploadBatchPackage(episodeId, event.currentTarget), false, "reupload-package");
  }

  window.inspectBatchPackage = async function(episodeId) {
    try {
      const response = await fetch(`/api/batch/episodes/${episodeId}/package`);
      const data = await response.json();
      showToast(data.compatible ? "الحزمة المحلية سليمة ومتوافقة ✓" : (data.reason || "الحزمة غير متوافقة"), data.compatible ? "success" : "error");
    } catch (error) {
      showToast("تعذر فحص الحزمة المحلية.", "error");
    }
  };

  window.openBatchPackage = async function(episodeId) {
    try {
      const response = await fetch(`/api/batch/episodes/${episodeId}/open-package`, { method: "POST" });
      if (!response.ok) showToast("تعذر فتح مجلد الحزمة المحلية.", "error");
    } catch (error) {
      showToast("تعذر فتح مجلد الحزمة المحلية.", "error");
    }
  };

  window.reuploadBatchPackage = async function(episodeId, button) {
    const originalLabel = button?.textContent || "إعادة الرفع";
    if (button) {
      button.disabled = true;
      button.textContent = "جاري فحص الحزمة...";
    }
    showToast("جاري فحص الحزمة...", "info");
    try {
      const response = await fetch(`/api/batch/episodes/${episodeId}/reupload`, { method: "POST" });
      let data;
      try { data = await response.json(); } catch (_) { data = {}; }
      if (!response.ok) throw new Error(data.detail || "تعذر إعادة الرفع.");
      showToast("جاري الرفع...", "info");
      if (button) button.textContent = "جاري الرفع...";
      await loadCurrentBatch();

      // Follow this episode only: a delayed worker failure must reach the user,
      // while a paused season queue remains untouched.
      while (true) {
        await new Promise(resolve => setTimeout(resolve, 1200));
        const stateResponse = await fetch("/api/batch/current");
        if (!stateResponse.ok) throw new Error(`تعذر متابعة حالة الرفع (HTTP ${stateResponse.status}).`);
        const batch = await stateResponse.json();
        const episode = (batch.episodes || []).find(item => item.id === episodeId);
        if (!episode) throw new Error("لم تعد الحلقة موجودة في طابور الموسم.");
        renderBatchQueue(batch);
        if (episode.state === "READY") {
          showToast("اكتمل رفع الحزمة والتحقق منها بنجاح.", "success");
          break;
        }
        if (episode.state === "FAILED") {
          throw new Error(episode.last_upload_error || episode.error || "فشل الرفع؛ راجع سجل الحلقة.");
        }
      }
    } catch (error) {
      showToast(error.message || "تعذر إعادة الرفع.", "error");
      await loadCurrentBatch();
    } finally {
      if (button && button.isConnected) {
        button.disabled = false;
        button.textContent = originalLabel;
      }
    }
  };

  async function loadSeasonHistory() {
    try {
      const response = await fetch("/api/batch/history");
      if (!response.ok) return;
      const data = await response.json();
      const panel = document.getElementById("season-history-panel");
      const list = document.getElementById("season-history-list");
      const performance = document.getElementById("season-performance-summary");
      if (!panel || !list || !performance) return;
      panel.hidden = false;
      const fmt = (seconds) => {
        const whole = Math.max(0, Math.floor(Number(seconds || 0)));
        const hours = Math.floor(whole / 3600), minutes = Math.floor((whole % 3600) / 60);
        return hours ? `${hours}س ${minutes}د` : `${minutes}د`;
      };
      performance.innerHTML = Object.entries(data.performance || {}).map(([quality, value]) =>
        `<span>${quality} <strong>${Number(value.average_speed_multiplier).toFixed(2)}×</strong> · ${value.samples} حلقة</span>`
      ).join("");
      if (!data.seasons?.length) {
        list.innerHTML = `<small class="episode-source-duration">سيظهر سجل الموسم بعد اكتمال المعالجة والرفع.</small>`;
      } else {
        list.innerHTML = data.seasons.map(season => {
          const safeName = String(season.name).replace(/[&<>"']/g, "");
          return `<article class="season-history-item">
            <div><strong>${safeName}</strong><small>${season.episodes} حلقة · ${new Date(season.completed_at).toLocaleDateString("ar-SA")}</small></div>
            <div class="season-history-metric">المصدر<b>${fmt(season.source_duration_seconds)}</b></div>
            <div class="season-history-metric">التحويل<b>${fmt(season.transcode_duration_seconds)}</b></div>
            <div class="season-history-metric">الرفع<b>${fmt(season.upload_duration_seconds)}</b></div>
            <div class="season-history-metric">الحجم<b>${formatBytes(season.output_bytes)}</b></div>
          </article>`;
        }).join("");
      }
      seasonHistoryLoaded = true;
    } catch (error) {
      console.debug("Season history unavailable", error);
    }
  }

  // Live WebSocket Telemetry updater
  window.updateBatchLiveTelemetry = function(batchPayload) {
    if (!batchPayload || !batchPayload.episode_id) return;
    const epRow = document.getElementById(`ep-row-${batchPayload.episode_id}`);
    if (!epRow) return;

    const telem = batchPayload.telemetry || {};
    const cells = epRow.querySelectorAll("td");
    if (cells.length >= 8) {
      if (telem.current_quality) {
        cells[3].innerHTML = `<span class="ltr-isolate p06-js-021" dir="ltr">${telem.current_quality}</span>`;
      }
      if (telem.progress !== undefined) {
        const pct = Math.max(0, Math.min(100, Math.round(telem.progress)));
        const fill = cells[5].querySelector(".progress-bar-fill");
        const span = cells[5].querySelector("span");
        setProgressPercent(fill, pct);
        if (span) span.textContent = `${pct}%`;
      }
      if (telem.fps > 0) {
        const qualitySpeed = cells[3].querySelector(".quality-speed-line");
        if (qualitySpeed) qualitySpeed.textContent = `${telem.fps} FPS · ${telem.speed || "1x"}`;
      }
    }
  };

  async function loadCurrentBatch() {
    try {
      const res = await fetch("/api/batch/current");
      if (res.ok) {
        const data = await res.json();
        renderBatchQueue(data);
        if (data.active && data.episodes && data.episodes.length > 0) {
          setMode("batch");
        }
      }
    } catch (e) {
      console.error("Failed to load current batch:", e);
    }
  }

  // Control button event listeners
  if (btnAutoPipeline) {
    btnAutoPipeline.addEventListener("click", async () => {
      try {
        const res = await fetch("/api/batch/auto-pipeline/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pipeline_window: 1 }),
        });
        if (res.ok) {
          await loadCurrentBatch();
        } else {
          const err = await res.json();
          showToast(err.detail || "فشل تشغيل الموسم تلقائياً.", 'error');
        }
      } catch (e) {
        showToast("حدث خطأ أثناء تشغيل الموسم تلقائياً.", 'error');
      }
    });
  }

  if (btnStartBatch) {
    btnStartBatch.addEventListener("click", async () => {
      try {
        const res = await fetch("/api/batch/start", { method: "POST" });
        if (res.ok) {
          await loadCurrentBatch();
        } else {
          const err = await res.json();
          showToast(err.detail || "فشل بدء معالجة الموسم.", 'error');
        }
      } catch (e) {
        showToast("حدث خطأ أثناء بدء المعالجة.", 'error');
      }
    });
  }

  if (btnPauseBatch) {
    btnPauseBatch.addEventListener("click", async () => {
      btnPauseBatch.disabled = true;
      btnPauseBatch.textContent = "جاري الإيقاف المؤقت...";
      try {
        await fetch("/api/batch/pause", { method: "POST" });
        await loadCurrentBatch();
      } catch (e) {
        console.error("Pause error:", e);
      }
    });
  }

  if (btnResumeBatch) {
    btnResumeBatch.addEventListener("click", async () => {
      try {
        const res = await fetch("/api/batch/resume", { method: "POST" });
        if (res.ok) {
          await loadCurrentBatch();
        }
      } catch (e) {
        showToast("حدث خطأ أثناء استئناف المعالجة.", 'error');
      }
    });
  }

  if (btnStopAfterCurrent) {
    btnStopAfterCurrent.addEventListener("click", async () => {
      btnStopAfterCurrent.disabled = true;
      btnStopAfterCurrent.textContent = "جاري الإيقاف بعد الحالية...";
      try {
        await fetch("/api/batch/stop-after-current", { method: "POST" });
        await loadCurrentBatch();
      } catch (e) {
        console.error("Stop after current error:", e);
      }
    });
  }

  window.retryBatchEpisode = async function(episodeId) {
    try {
      const res = await fetch(`/api/batch/episodes/${episodeId}/retry`, { method: "POST" });
      if (res.ok) {
        await loadCurrentBatch();
      } else {
        const err = await res.json();
        showToast(err.detail || "فشل إعادة المحاولة.", 'error');
      }
    } catch (e) {
      showToast("حدث خطأ أثناء إعادة المحاولة.", 'error');
    }
  };

  window.skipBatchEpisode = async function(episodeId) {
    try {
      const res = await fetch(`/api/batch/episodes/${episodeId}/skip`, { method: "POST" });
      if (res.ok) {
        await loadCurrentBatch();
      } else {
        const err = await res.json();
        showToast(err.detail || "فشل تخطي الحلقة.", 'error');
      }
    } catch (e) {
      showToast("حدث خطأ أثناء تخطي الحلقة.", 'error');
    }
  };

  window.reviewBatchEpisode = function(episodeId) {
    const row = document.getElementById(`ep-row-${episodeId}`);
    if (row) {
      row.scrollIntoView({ behavior: "smooth", block: "center" });
      row.classList.add("row-needs-review");
    }
  };

  window.approveBatchAssFallback = async function(episodeId, missingFonts) {
    const names = (missingFonts || []).join(", ");
    if (!window.confirm(`الخطوط المفقودة: ${names}. سيبقى ملف ASS وتنسيقه كما هو، وقد يستخدم المشغل خطاً بديلاً. هل توافق على متابعة هذه الحلقة؟`)) return;
    try {
      const res = await fetch(`/api/batch/episodes/${episodeId}/font-fallback`, { method: "POST" });
      if (res.ok) {
        await loadCurrentBatch();
      } else {
        const err = await res.json();
        showToast(err.detail || "تعذر تسجيل موافقة ASS fallback.", 'error');
      }
    } catch (e) {
      showToast("حدث خطأ أثناء تسجيل قرار الخطوط.", 'error');
    }
  };

  window.deleteBatchEpisode = async function(episodeId) {
    showConfirm('حذف الحلقة', 'هل أنت متأكد من حذف هذه الحلقة من طابور الموسم؟', async () => {
      try {
        const res = await fetch(`/api/batch/episodes/${episodeId}`, { method: "DELETE" });
        if (res.ok) {
          await loadCurrentBatch();
          showToast('تم حذف الحلقة.', 'success');
        } else {
          const err = await res.json();
          showToast(err.detail || "فشل حذف الحلقة.", 'error');
        }
      } catch (e) {
        showToast("حدث خطأ في الاتصال أثناء حذف الحلقة.", 'error');
      }
    });
  };

  window.uploadBatchEpisode = async function(episodeId) {
    try {
      const res = await fetch(`/api/batch/episodes/${episodeId}/upload`, { method: "POST" });
      if (res.ok) {
        await loadCurrentBatch();
      } else {
        const err = await res.json();
        showToast(err.detail || "فشل بدء رفع الحلقة.", 'error');
      }
    } catch (e) {
      showToast("حدث خطأ في الاتصال أثناء بدء الرفع.", 'error');
    }
  };

  window.uploadReadyBatch = async function() {
    try {
      const res = await fetch("/api/batch/upload-ready", { method: "POST" });
      if (res.ok) {
        await loadCurrentBatch();
      } else {
        const err = await res.json();
        showToast(err.detail || "فشل بدء رفع الحلقات الجاهزة.", 'error');
      }
    } catch (e) {
      showToast("حدث خطأ في الاتصال.", 'error');
    }
  };

  if (btnUploadReady) {
    btnUploadReady.addEventListener("click", () => window.uploadReadyBatch());
  }

  async function syncAuthoritativeDestination() {
    const folderInput = document.getElementById("batch-folder-id-input");
    const seriesInput = document.getElementById("batch-series-id-input");
    const seasonInput = document.getElementById("batch-season-id-input");
    const destInput = document.getElementById("batch-destination-input");

    try {
      const curRes = await fetch("/api/batch/current");
      const curData = await curRes.json();
      if (!curData || !curData.batch) return;
      const bid = curData.batch.id;

      await fetch(`/api/batch/${bid}/destination-authoritative`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          folder_id: folderInput ? folderInput.value.trim() : "",
          series_id: seriesInput ? seriesInput.value.trim() : "",
          season_id: seasonInput ? seasonInput.value.trim() : "",
          destination_label: destInput ? destInput.value.trim() : "",
        }),
      });
    } catch (e) {
      console.warn("Failed to sync authoritative destination:", e);
    }
  }

  if (batchFolderIdInput) batchFolderIdInput.addEventListener("change", syncAuthoritativeDestination);
  if (batchSeriesIdInput) batchSeriesIdInput.addEventListener("change", syncAuthoritativeDestination);
  if (batchSeasonIdInput) batchSeasonIdInput.addEventListener("change", syncAuthoritativeDestination);
  if (batchDestinationInput) batchDestinationInput.addEventListener("change", syncAuthoritativeDestination);

  if (btnClearBatch) {
    btnClearBatch.addEventListener("click", () => {
      showConfirm('مسح الموسم', 'هل أنت متأكد من مسح الموسم كاملاً من طابور المعالجة؟', async () => {
        try {
          const res = await fetch("/api/batch/clear", { method: "POST" });
          if (res.ok) {
            await loadCurrentBatch();
            showToast('تم مسح الموسم.', 'success');
          } else {
            showToast("فشل مسح الموسم.", 'error');
          }
        } catch (e) {
          showToast("حدث خطأ في الاتصال.", 'error');
        }
      });
    });
  }

  if (btnImportSeason) {
    btnImportSeason.addEventListener("click", async () => {
      const folderPath = batchFolderInput ? batchFolderInput.value.trim() : "";
      const destLabel = batchDestinationInput ? batchDestinationInput.value.trim() : "";
      if (!folderPath) {
        showToast("يرجى إدخال أو استعراض مسار مجلد الموسم.", 'warning');
        return;
      }

      btnImportSeason.disabled = true;
      btnImportSeason.textContent = "جاري الفحص وإضافة الطابور...";

      try {
        const res = await fetch("/api/batch/import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            folder_path: folderPath,
            destination_label: destLabel,
          }),
        });

        const data = await res.json();
        if (res.ok && data.ok) {
          renderBatchQueue(data);
        } else {
          showToast(data.detail || "فشل فحص أو استيراد مجلد الموسم.", 'error');
        }
      } catch (e) {
        showToast("حدث خطأ في الاتصال أثناء استيراد الموسم.", 'error');
      } finally {
        btnImportSeason.disabled = false;
        btnImportSeason.innerHTML = `<svg class="icon" viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg><span>فحص وإضافة الموسم</span>`;
      }
    });
  }

  // Init
  async function initStudio() {
    try {
      await loadConfig();
    } catch (_) {}
    initWebSocket();
    initPresetButtons();
    initHardsubDesigner();
    loadCurrentBatch();
    loadDiskInfo();

    // Show home landing first
    const hasBatch = await (async () => {
      try {
        const r = await fetch('/api/batch/current');
        if (!r.ok) return false;
        const d = await r.json();
        return !!(d && d.active && d.episodes && d.episodes.length > 0);
      } catch { return false; }
    })();

    if (hasBatch) {
      showMainApp('batch');
    } else {
      showHome();
    }

    const splash = document.getElementById("app-splash-overlay");
    if (splash) {
      splash.style.opacity = "0";
      splash.style.pointerEvents = "none";
      setTimeout(() => { splash.style.display = "none"; }, 300);
    }

    // Sync home status indicators with header
    syncHomeStatus();
  }

  function syncHomeStatus() {
    // Sync connection status to home
    const homeDot = document.getElementById('home-pulse-dot');
    const homeText = document.getElementById('home-connection-text');
    const headerDot = document.getElementById('server-pulse-dot');
    const headerText = document.getElementById('server-connection-text');
    if (homeDot && headerDot) homeDot.className = headerDot.className;
    if (homeText && headerText) homeText.textContent = headerText.textContent;

    // Sync GPU to home
    const homeGpuText = document.getElementById('home-gpu-text');
    const headerGpuText = document.getElementById('gpu-telemetry-text');
    if (homeGpuText && headerGpuText) homeGpuText.textContent = headerGpuText.textContent;

    setTimeout(syncHomeStatus, 3000);
  }

  initStudio();
});
