window.AVi18n ||= {t: value => value, tpl: (parts, ...values) => parts.reduce((out, part, i) => out + part + (i < values.length ? values[i] : ''), '')};
(function() {
    let config = {};
    const configEl = document.getElementById('player-config');
    if (configEl) {
        try {
            config = JSON.parse(configEl.textContent || '{}');
        } catch (e) {
            console.error('Failed to parse player config:', e);
        }
    }

    const playerConfig = config;
    const PLAYBACK_TOKEN = config.playbackToken || "";
    const SHARE_ID = config.shareId || "";
    const SUBTITLES_LIST = config.subtitles || [];
    const IS_PASSWORD_REQUIRED = !!config.isPasswordRequired;

    // Password form handler
    const pwForm = document.getElementById('password-form');
    if (pwForm) {
        pwForm.addEventListener('submit', async function(e) {
            e.preventDefault();
            const pw = document.getElementById('pw-input').value;
            const btn = document.getElementById('pw-btn');
            const errBox = document.getElementById('pw-error');

            btn.disabled = true;
            btn.innerText = window.AVi18n.t("جاري التحقق...");
            errBox.classList.add("hidden");

            try {
                const res = await fetch(`/api/v1/watch/${SHARE_ID}/verify-password`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ password: pw })
                });
                const data = await res.json();
                if (res.ok && data.success) {
                    window.location.reload();
                } else {
                    errBox.innerText = window.AVi18n.t(data.detail) || window.AVi18n.t("رمز المرور غير صحيح");
                    errBox.classList.remove("hidden");
                }
            } catch (err) {
                errBox.innerText = window.AVi18n.t("تعذر الاتصال بالخادم، يرجى المحاولة لاحقاً");
                errBox.classList.remove("hidden");
            } finally {
                btn.disabled = false;
                btn.innerText = window.AVi18n.t("دخول للمشاهدة");
            }
        });
    }

    let hls = null;
    let isNativeHLS = false;
    let controlsTimeout = null;
    let jassubInstance = null;
    let assActivationGeneration = 0;
    const sessionFontCache = new Map();
    const subtitleElements = [];
    let selectedSubtitleIndex = -1;
    let selectedQualityIndex = -1;
    let actualQualityIndex = -1;
    let pendingQualityIndex = null;
    let isQualitySwitching = false;
    let qualityToastTimeout = null;

    // Smart QoE Engine State
    const QoEState = {
        IDLE: 'IDLE',
        SWITCHING: 'SWITCHING',
        STABLE: 'STABLE',
        DEGRADED: 'DEGRADED',
        STALLED: 'STALLED'
    };
    let currentQoEState = QoEState.IDLE;
    let manualSwitchTimestamp = 0;
    const SWITCH_GRACE_PERIOD_MS = 8000;
    let ewmaBandwidthBps = 0;
    let lastFragStats = {
        bytes: 0,
        downloadTimeMs: 0,
        throughputBps: 0,
        sn: 0,
        bitrateBps: 0
    };
    let consecutiveLowBwCount = 0;
    let stallCount = 0;
    let stallTimer = null;
    let stallStartTime = null;
    let diagnosticsInterval = null;
    let diagnosticsVisible = false;

    function getPlaybackEndpoint(relPath) {
        const cleanRel = relPath.startsWith('/') ? relPath.slice(1) : relPath;
        if (PLAYBACK_TOKEN.startsWith('s/')) {
            return `/api/v1/playback/${PLAYBACK_TOKEN}/${cleanRel}`;
        }
        return `/api/v1/playback/${encodeURIComponent(PLAYBACK_TOKEN)}/${cleanRel}`;
    }

    function getForwardBuffer(videoEl) {
        if (!videoEl || !videoEl.buffered || videoEl.buffered.length === 0) return 0;
        const cur = videoEl.currentTime;
        for (let i = 0; i < videoEl.buffered.length; i++) {
            const start = videoEl.buffered.start(i);
            const end = videoEl.buffered.end(i);
            if (cur >= start && cur <= end) {
                return Math.max(0, end - cur);
            }
        }
        return 0;
    }

    function showQualityToast(text, type = 'info', duration = 3000, actionCallback = null, actionText = null) {
        let toast = document.getElementById('quality-toast');
        if (!toast) {
            const container = document.getElementById('player-container');
            if (!container) return;
            toast = document.createElement('div');
            toast.id = 'quality-toast';
            toast.className = 'quality-toast';
            toast.setAttribute('role', 'status');
            toast.setAttribute('aria-live', 'polite');
            container.appendChild(toast);
        }
        clearTimeout(qualityToastTimeout);
        toast.innerHTML = '';

        const spanText = document.createElement('span');
        spanText.textContent = text;
        toast.appendChild(spanText);

        if (actionCallback && actionText) {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'toast-action-btn';
            btn.textContent = actionText;
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                clearTimeout(qualityToastTimeout);
                toast.classList.add('hidden');
                actionCallback();
            });
            toast.appendChild(btn);
        }

        toast.className = 'quality-toast';
        if (type === 'warn' || type === true) {
            toast.classList.add('warn');
        } else if (type === 'error') {
            toast.classList.add('error');
        }

        toast.classList.remove('hidden');
        toast.style.opacity = '1';
        toast.style.transform = 'translateY(0)';
        qualityToastTimeout = setTimeout(() => {
            toast.style.opacity = '0';
            toast.style.transform = 'translateY(-6px)';
            setTimeout(() => { toast.classList.add('hidden'); }, 300);
        }, duration);
    }

    function getQualityShortLabel(lvlIdx) {
        if (lvlIdx === -1 || lvlIdx === null || lvlIdx === undefined) return 'Auto';
        const lvl = hls?.levels?.[lvlIdx];
        if (!lvl) return window.AVi18n.t('يدوي');
        if (lvl.height >= 2160) return '4K';
        if (lvl.height) return `${lvl.height}p`;
        return `${Math.round(lvl.bitrate / 1000)}k`;
    }
    const captionDefaults = { size: 'medium', color: 'white', background: 'dark', opacity: '75', position: 'bottom', outline: 'off' };
    let captionPrefs = { ...captionDefaults };

    if (!IS_PASSWORD_REQUIRED && PLAYBACK_TOKEN) {
        initPlayer();
    }

    function initPlayer() {
        const video = document.getElementById('video-element');
        const container = document.getElementById('player-container');
        const controls = document.getElementById('player-controls');
        const bigPlayBtn = document.getElementById('big-play-btn');
        const spinner = document.getElementById('buffering-spinner');
        const playBtn = document.getElementById('btn-play');
        const iconPlay = document.getElementById('icon-play');
        const iconPause = document.getElementById('icon-pause');
        const muteBtn = document.getElementById('btn-mute');
        const volHigh = document.getElementById('icon-vol-high');
        const volMute = document.getElementById('icon-vol-mute');
        const volSlider = document.getElementById('volume-slider');
        const timeline = document.getElementById('timeline');
        const progressBar = document.getElementById('progress-bar');
        const bufferedBar = document.getElementById('buffered-bar');
        const timeDisplay = document.getElementById('time-display');
        const timeTooltip = document.getElementById('time-tooltip');
        const tooltipCanvas = document.getElementById('timeline-tooltip-canvas');
        const fsBtn = document.getElementById('btn-fullscreen');
        const pipBtn = document.getElementById('btn-pip');

        if (!video || !container) return;
        // Direct-import MP4 uses browser-native controls and the same
        // session-authorized playback endpoint (including HTTP Range).
        if (playerConfig.directMediaUrl) {
            container.classList.add('direct-mp4-mode');
            video.controls = true;
            video.src = playerConfig.directMediaUrl;
            video.preload = 'metadata';
            bigPlayBtn?.remove();
            controls?.remove();
            video.load();
            return;
        }

        initAssViewport(video);

        // Strictly disable browser native video controls to prevent duplicate toolbars
        video.controls = false;
        video.removeAttribute('controls');

        // Explicitly set initial states of toggle SVGs to guarantee exactly ONE icon is visible
        if (iconPlay) { iconPlay.classList.remove('hidden'); iconPlay.style.display = ''; }
        if (iconPause) { iconPause.classList.add('hidden'); iconPause.style.display = 'none'; }
        if (volHigh) { volHigh.classList.remove('hidden'); volHigh.style.display = ''; }
        if (volMute) { volMute.classList.add('hidden'); volMute.style.display = 'none'; }
        const iconFsEnter = document.getElementById('icon-fs-enter');
        const iconFsExit = document.getElementById('icon-fs-exit');
        if (iconFsEnter) { iconFsEnter.classList.remove('hidden'); iconFsEnter.style.display = ''; }
        if (iconFsExit) { iconFsExit.classList.add('hidden'); iconFsExit.style.display = 'none'; }

        const masterUrl = getPlaybackEndpoint('master.m3u8');
        initMenus(video, container, controls);
        initCaptionPreferences(video, container);
        populateSubtitlesMenu();
        video.textTracks.addEventListener?.('addtrack', () => syncSubtitleModes(video));

        if (window.Hls && Hls.isSupported()) {
            hls = new Hls({
                capLevelToPlayerSize: true,
                autoStartLoad: true,
                renderTextTracksNatively: false,
                maxBufferLength: 30,
                maxMaxBufferLength: 60
            });
            window.hls = hls;
            hls.loadSource(masterUrl);
            hls.attachMedia(video);

            hls.on(Hls.Events.MANIFEST_PARSED, (event, data) => {
                populateQualityMenu(hls.levels);
                hls.subtitleDisplay = false;
                hls.subtitleTrack = -1;
                syncSubtitleModes(video);
            });

            hls.on(Hls.Events.LEVEL_SWITCHED, (event, data) => {
                actualQualityIndex = data.level;
                if (!isQualitySwitching) {
                    updateQualityMenuSelection(data.level);
                }
            });

            if (Hls.Events && Hls.Events.FRAG_LOADED) {
                hls.on(Hls.Events.FRAG_LOADED, (event, data) => {
                    if (!data || !data.frag || !data.stats) return;
                    const stats = data.stats;
                    const bytes = stats.total || stats.loaded || 0;
                    const dtMs = (stats.tload && stats.tfirst) ? (stats.tload - stats.tfirst) : ((stats.tload - stats.trequest) || 100);
                    const dtSec = Math.max(dtMs / 1000, 0.05);
                    const throughputBps = (bytes * 8) / dtSec;

                    if (ewmaBandwidthBps === 0) {
                        ewmaBandwidthBps = throughputBps;
                    } else {
                        ewmaBandwidthBps = 0.7 * ewmaBandwidthBps + 0.3 * throughputBps;
                    }

                    const lvl = hls.levels?.[data.frag.level];
                    const variantBitrateBps = (lvl?.bitrate) || (lvl?.averageBitrate) || 0;

                    lastFragStats = {
                        bytes: bytes,
                        downloadTimeMs: Math.round(dtMs),
                        throughputBps: throughputBps,
                        sn: data.frag.sn,
                        bitrateBps: variantBitrateBps
                    };

                    evaluateQualityHealth();
                });
            }

            if (Hls.Events && Hls.Events.FRAG_CHANGED) {
                hls.on(Hls.Events.FRAG_CHANGED, (event, data) => {
                    if (!data || !data.frag) return;
                    actualQualityIndex = data.frag.level;
                    if (pendingQualityIndex !== null && data.frag.level === pendingQualityIndex) {
                        const targetShort = getQualityShortLabel(pendingQualityIndex);
                        pendingQualityIndex = null;
                        isQualitySwitching = false;
                        currentQoEState = QoEState.STABLE;
                        updateQualityMenuSelection(data.frag.level, `${targetShort} ✓`);
                        showQualityToast(`${targetShort} ✓`, false, 3000);
                    } else if (selectedQualityIndex === -1) {
                        updateQualityMenuSelection(data.frag.level);
                    }
                });
            }

            video.addEventListener('waiting', () => {
                handlePlaybackStall();
            });

            video.addEventListener('playing', () => {
                if (stallTimer) {
                    clearTimeout(stallTimer);
                    stallTimer = null;
                }
                stallStartTime = null;
            });

            let fatalRecoveryAttempts = { network: 0, media: 0 };
            if (Hls.Events && Hls.Events.ERROR) {
                hls.on(Hls.Events.ERROR, (event, data) => {
                    if (!data) return;
                    if (data.details === 'bufferStalledError') {
                        handlePlaybackStall();
                    } else if (data.fatal) {
                        handleFatalHlsError(data);
                    }
                });
            }

            function handleFatalHlsError(data) {
                console.warn("HLS Fatal Error encountered:", data);
                const errorType = data.type;
                if (errorType === (Hls.ErrorTypes && Hls.ErrorTypes.NETWORK_ERROR)) {
                    fatalRecoveryAttempts.network++;
                    if (fatalRecoveryAttempts.network <= 2) {
                        console.warn(`HLS Network Error, attempting recovery ${fatalRecoveryAttempts.network}/2...`);
                        hls.startLoad();
                        return;
                    }
                } else if (errorType === (Hls.ErrorTypes && Hls.ErrorTypes.MEDIA_ERROR)) {
                    fatalRecoveryAttempts.media++;
                    if (fatalRecoveryAttempts.media <= 2) {
                        console.warn(`HLS Media Error, recovering media error ${fatalRecoveryAttempts.media}/2...`);
                        hls.recoverMediaError();
                        return;
                    }
                }
                showFatalPlaybackError(window.AVi18n.t("حدث خطأ أثناء تحميل البث أو انقطع الاتصال بالخادم."));
            }

            function showFatalPlaybackError(msg) {
                let errOverlay = document.getElementById('player-fatal-error');
                if (!errOverlay) {
                    errOverlay = document.createElement('div');
                    errOverlay.id = 'player-fatal-error';
                    errOverlay.className = 'player-fatal-error';
                    container.appendChild(errOverlay);
                }
                errOverlay.innerHTML = window.AVi18n.tpl`
                    <div class="player-error-content">
                        <svg viewBox="0 0 24 24" width="48" height="48" fill="none" stroke="#ef4444" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                            <circle cx="12" cy="12" r="10"></circle>
                            <line x1="12" y1="8" x2="12" y2="12"></line>
                            <line x1="12" y1="16" x2="12.01" y2="16"></line>
                        </svg>
                        <p class="player-error-message">${msg}</p>
                        <button type="button" class="player-retry-btn" id="btn-player-retry">
                            إعادة المحاولة
                        </button>
                    </div>
                `;
                errOverlay.classList.remove('hidden');
                const retryBtn = document.getElementById('btn-player-retry');
                if (retryBtn) {
                    retryBtn.addEventListener('click', () => {
                        errOverlay.classList.add('hidden');
                        fatalRecoveryAttempts = { network: 0, media: 0 };
                        if (hls && typeof hls.loadSource === 'function') {
                            try {
                                hls.destroy();
                            } catch (_) {}
                            initHLS();
                        } else {
                            window.location.reload();
                        }
                    });
                }
            }

            function handlePlaybackStall() {
                // In Auto mode: let ABR adapt silently, no warning
                if (selectedQualityIndex === -1) return;

                const now = Date.now();
                if (!stallStartTime) {
                    stallStartTime = now;
                }

                // If currently switching or within 8s grace period, postpone stall warning until grace period ends
                let checkDelay = 2500;
                if (isQualitySwitching || (now - manualSwitchTimestamp < SWITCH_GRACE_PERIOD_MS)) {
                    const remainingGrace = SWITCH_GRACE_PERIOD_MS - (now - manualSwitchTimestamp);
                    checkDelay = Math.max(2500, remainingGrace + 500);
                }

                if (!stallTimer) {
                    stallTimer = setTimeout(() => {
                        const forwardBuffer = getForwardBuffer(video);
                        const elapsedStall = (Date.now() - stallStartTime) / 1000;

                        if (elapsedStall >= 2.4 && forwardBuffer < 2.0 && selectedQualityIndex !== -1 && !video.paused) {
                            stallCount++;
                            currentQoEState = QoEState.STALLED;
                            // LEVEL 2 Warning with [العودة إلى تلقائي] action button
                            showQualityToast(
                                window.AVi18n.t('الاتصال غير كافٍ لهذه الجودة'),
                                'error',
                                8000,
                                () => selectQuality(-1),
                                window.AVi18n.t('العودة إلى تلقائي')
                            );
                        }
                        stallTimer = null;
                    }, checkDelay);
                }
            }

            // Periodic stall watchdog
            setInterval(() => {
                if (video && !video.paused && video.readyState < 3 && selectedQualityIndex !== -1) {
                    handlePlaybackStall();
                }
            }, 1000);

            function evaluateQualityHealth() {
                // In Auto mode: let ABR handle adaptation silently
                if (selectedQualityIndex === -1) {
                    consecutiveLowBwCount = 0;
                    return;
                }

                // Within 8-second grace period after manual switch: suppress warnings
                const now = Date.now();
                if (isQualitySwitching || (now - manualSwitchTimestamp < SWITCH_GRACE_PERIOD_MS)) {
                    return;
                }

                const lvl = hls?.levels?.[selectedQualityIndex];
                const variantBitrate = (lvl?.bitrate) || (lvl?.averageBitrate) || 0;
                const estBw = (hls?.bandwidthEstimate > 0) ? hls.bandwidthEstimate : ewmaBandwidthBps;

                if (variantBitrate > 0 && estBw > 0 && estBw < 1.20 * variantBitrate) {
                    consecutiveLowBwCount++;
                } else {
                    consecutiveLowBwCount = 0;
                    if (currentQoEState === QoEState.DEGRADED) {
                        currentQoEState = QoEState.STABLE;
                    }
                }

                // LEVEL 1: Bandwidth repeatedly low for >= 2 consecutive segments while playing
                if (consecutiveLowBwCount >= 2 && !video.paused && currentQoEState === QoEState.STABLE) {
                    currentQoEState = QoEState.DEGRADED;
                    showQualityToast(window.AVi18n.t('قد لا يكون الاتصال مستقراً لهذه الجودة'), 'warn', 5000);
                }
            }
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
            // Native Safari HLS
            isNativeHLS = true;
            video.src = masterUrl;
            populateQualityMenu([]);
        }

        // Controls Visibility
        function showControls() {
            if (!controls) return;
            controls.classList.remove('hidden');
            clearTimeout(controlsTimeout);
            if (!video.paused && !document.getElementById('player-menu-panel')?.classList.contains('is-open')) {
                controlsTimeout = setTimeout(() => {
                    controls.classList.add('hidden');
                }, 3000);
            }
        }

        container.addEventListener('mousemove', showControls);
        container.addEventListener('touchstart', showControls, { passive: true });

        // Play / Pause Toggle
        function togglePlay() {
            if (video.paused) {
                video.play();
            } else {
                video.pause();
            }
        }

        if (bigPlayBtn) bigPlayBtn.addEventListener('click', togglePlay);
        if (playBtn) playBtn.addEventListener('click', togglePlay);
        video.addEventListener('click', togglePlay);

        video.addEventListener('play', () => {
            if (iconPlay) { iconPlay.classList.add('hidden'); iconPlay.style.display = 'none'; }
            if (iconPause) { iconPause.classList.remove('hidden'); iconPause.style.display = ''; }
            if (bigPlayBtn) { bigPlayBtn.classList.add('hidden'); bigPlayBtn.style.display = 'none'; }
            if (playBtn) playBtn.setAttribute('title', window.AVi18n.t('إيقاف مؤقت (Space/K)'));
            showControls();
            trackAnalytics('start');
        });

        video.addEventListener('pause', () => {
            if (iconPlay) { iconPlay.classList.remove('hidden'); iconPlay.style.display = ''; }
            if (iconPause) { iconPause.classList.add('hidden'); iconPause.style.display = 'none'; }
            if (bigPlayBtn) { bigPlayBtn.classList.remove('hidden'); bigPlayBtn.style.display = ''; }
            if (playBtn) playBtn.setAttribute('title', window.AVi18n.t('تشغيل (Space/K)'));
            showControls();
        });

        video.addEventListener('waiting', () => { if (spinner) spinner.classList.add('is-visible'); });
        video.addEventListener('stalled', () => { if (spinner) spinner.classList.add('is-visible'); });
        video.addEventListener('playing', () => { if (spinner) spinner.classList.remove('is-visible'); });
        video.addEventListener('canplay', () => { if (spinner) spinner.classList.remove('is-visible'); });

        // Timeline & Time update
        function paintTimeline() {
            if (!timeline || !progressBar || !bufferedBar) return;
            const width = timeline.clientWidth;
            if (!width) return;
            const height = timeline.clientHeight;
            const scale = window.devicePixelRatio || 1;
            for (const canvas of [bufferedBar, progressBar]) {
                const pixelWidth = Math.round(width * scale);
                const pixelHeight = Math.round(height * scale);
                if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) {
                    canvas.width = pixelWidth;
                    canvas.height = pixelHeight;
                }
            }
            const buffered = bufferedBar.getContext('2d');
            const played = progressBar.getContext('2d');
            buffered.clearRect(0, 0, bufferedBar.width, bufferedBar.height);
            played.clearRect(0, 0, progressBar.width, progressBar.height);
            const duration = video.duration;
            if (!Number.isFinite(duration) || duration <= 0) return;
            buffered.fillStyle = '#a9c5de';
            for (let i = 0; i < video.buffered.length; i++) {
                const start = Math.max(0, Math.min(duration, video.buffered.start(i)));
                const end = Math.max(start, Math.min(duration, video.buffered.end(i)));
                buffered.fillRect(start / duration * bufferedBar.width, 0, (end - start) / duration * bufferedBar.width, bufferedBar.height);
            }
            played.fillStyle = (playerConfig && playerConfig.primaryColor) || '#56a9ff';
            played.fillRect(0, 0, Math.max(0, Math.min(1, video.currentTime / duration)) * progressBar.width, progressBar.height);
            if (timeDisplay) timeDisplay.textContent = formatTime(video.currentTime) + ' / ' + formatTime(duration);
            timeline.setAttribute('aria-valuenow', String(Math.round(video.currentTime / duration * 100)));
            timeline.setAttribute('aria-valuetext', formatTime(video.currentTime) + ' / ' + formatTime(duration));
        }
        ['timeupdate', 'progress', 'seeking', 'seeked', 'durationchange', 'loadedmetadata'].forEach(name => video.addEventListener(name, paintTimeline));
        window.addEventListener('resize', paintTimeline);
        paintTimeline();

        // Playback Resume Position
        const RESUME_STORAGE_KEY = `aboodvideo_resume_${SHARE_ID}`;
        let resumeChecked = false;
        let lastSaveTime = 0;

        function savePlaybackPosition() {
            if (!SHARE_ID || !video || !Number.isFinite(video.duration) || video.duration <= 0) return;
            const cur = video.currentTime;
            const dur = video.duration;
            if (cur > 5 && cur < dur - 15) {
                try {
                    localStorage.setItem(RESUME_STORAGE_KEY, JSON.stringify({
                        time: Math.floor(cur),
                        duration: Math.floor(dur),
                        savedAt: Date.now()
                    }));
                } catch (_) {}
            } else if (cur >= dur - 15) {
                try {
                    localStorage.removeItem(RESUME_STORAGE_KEY);
                } catch (_) {}
            }
        }

        function checkResumePosition() {
            if (resumeChecked || !SHARE_ID || !video) return;
            try {
                const raw = localStorage.getItem(RESUME_STORAGE_KEY);
                if (!raw) return;
                const data = JSON.parse(raw);
                const savedTime = data && data.time;
                if (savedTime && savedTime > 5 && savedTime < (video.duration || Infinity) - 15) {
                    resumeChecked = true;
                    const m = Math.floor(savedTime / 60);
                    const s = Math.floor(savedTime % 60);
                    const timeStr = `${m}:${s < 10 ? '0' : ''}${s}`;
                    showQualityToast(
                        window.AVi18n.tpl`استئناف المشاهدة من ${timeStr}`,
                        'info',
                        6000,
                        () => {
                            video.currentTime = savedTime;
                        },
                        window.AVi18n.t('استئناف')
                    );
                }
            } catch (_) {}
        }

        const skipIntroBtn = document.getElementById('btn-skip-intro');
        const customSkipSeconds = (playerConfig && Number.isFinite(playerConfig.skipIntroSeconds))
            ? playerConfig.skipIntroSeconds
            : 85;

        if (skipIntroBtn) {
            if (customSkipSeconds <= 0) {
                skipIntroBtn.style.display = 'none';
            } else {
                skipIntroBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    video.currentTime = customSkipSeconds + 3;
                    skipIntroBtn.classList.add('hidden');
                });
            }
        }

        // ─── Pause Promo Banner Logic ─────────────────────────────────────────
        const pauseBannerCard = document.getElementById('pause-banner-card');
        const btnBannerClose = document.getElementById('btn-banner-close');
        let pauseBannerDismissed = false;

        if (btnBannerClose && pauseBannerCard) {
            btnBannerClose.addEventListener('click', (e) => {
                e.stopPropagation();
                pauseBannerDismissed = true;
                pauseBannerCard.classList.add('hidden');
            });
        }

        // ─── Autoplay & Custom Start Time ─────────────────────────────────────
        if (playerConfig && playerConfig.autoPlay) {
            video.play().catch(() => {
                video.muted = true;
                video.play().catch(() => {});
            });
        }

        if (playerConfig && Number.isFinite(playerConfig.startTime) && playerConfig.startTime > 0) {
            video.addEventListener('loadedmetadata', () => {
                if (playerConfig.startTime > 0 && playerConfig.startTime < (video.duration || Infinity)) {
                    video.currentTime = playerConfig.startTime;
                }
            }, { once: true });
        }

        // ─── Dynamic Anti-Piracy Watermark Shifting ───────────────────────────
        const watermarkEl = document.getElementById('player-watermark');
        if (watermarkEl) {
            const wmCorners = [
                { top: '24px', left: '24px', right: 'auto', bottom: 'auto' },
                { top: '24px', right: '24px', left: 'auto', bottom: 'auto' },
                { bottom: '85px', right: '24px', left: 'auto', top: 'auto' },
                { bottom: '85px', left: '24px', right: 'auto', top: 'auto' },
            ];
            let wmIndex = 0;
            setInterval(() => {
                wmIndex = (wmIndex + 1) % wmCorners.length;
                Object.assign(watermarkEl.style, wmCorners[wmIndex]);
            }, 40000);
        }

        // ─── Next Episode Auto-Play Logic ─────────────────────────────────────
        const nextCard = document.getElementById('next-episode-card');
        const nextCountdown = document.getElementById('next-countdown-text');
        const nextPlayBtn = document.getElementById('btn-next-play-now');
        const nextCancelBtn = document.getElementById('btn-next-cancel');
        let nextCancelled = false;

        function triggerNextEpisode() {
            try {
                window.parent.postMessage({ type: 'ABOODVIDEO_NEXT_EPISODE' }, '*');
            } catch (_) {}
            if (nextCard) nextCard.classList.add('hidden');
        }

        if (nextPlayBtn) nextPlayBtn.addEventListener('click', (e) => { e.stopPropagation(); triggerNextEpisode(); });
        if (nextCancelBtn) nextCancelBtn.addEventListener('click', (e) => { e.stopPropagation(); nextCancelled = true; if (nextCard) nextCard.classList.add('hidden'); });

        video.addEventListener('timeupdate', () => {
            const now = Date.now();
            if (now - lastSaveTime > 4000) {
                lastSaveTime = now;
                savePlaybackPosition();
            }
            if (skipIntroBtn && customSkipSeconds > 0) {
                if (video.currentTime >= 2 && video.currentTime <= (customSkipSeconds + 3) && (video.duration > 180 || !video.duration)) {
                    skipIntroBtn.classList.remove('hidden');
                } else {
                    skipIntroBtn.classList.add('hidden');
                }
            }
            if (nextCard && !nextCancelled && Number.isFinite(video.duration) && video.duration > 30) {
                const remaining = video.duration - video.currentTime;
                if (remaining <= 15 && remaining > 0.5) {
                    nextCard.classList.remove('hidden');
                    if (nextCountdown) nextCountdown.textContent = window.AVi18n.tpl`تبدأ خلال ${Math.ceil(remaining)} ثوانٍ`;
                } else if (remaining <= 0.5) {
                    triggerNextEpisode();
                } else {
                    nextCard.classList.add('hidden');
                }
            }
        });

        video.addEventListener('pause', () => {
            savePlaybackPosition();
            if (playerConfig && playerConfig.hasPauseBanner && !pauseBannerDismissed && pauseBannerCard) {
                if (video.currentTime > 5 && video.currentTime < (video.duration || Infinity) - 15) {
                    pauseBannerCard.classList.remove('hidden');
                }
            }
        });

        video.addEventListener('play', () => {
            checkResumePosition();
            if (pauseBannerCard) {
                pauseBannerCard.classList.add('hidden');
            }
        });

        video.addEventListener('loadedmetadata', checkResumePosition);
        window.addEventListener('beforeunload', savePlaybackPosition);

        if (timeline) {
            timeline.addEventListener('click', (e) => {
                const rect = timeline.getBoundingClientRect();
                const pos = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
                if (Number.isFinite(video.duration)) {
                    video.currentTime = pos * video.duration;
                }
            });
            timeline.addEventListener('keydown', (e) => {
                if (!Number.isFinite(video.duration)) return;
                if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
                    e.preventDefault();
                    video.currentTime = Math.max(0, Math.min(video.duration, video.currentTime + (e.key === 'ArrowRight' ? 5 : -5)));
                }
            });

            timeline.addEventListener('mousemove', (e) => {
                const rect = timeline.getBoundingClientRect();
                const pos = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
                if (Number.isFinite(video.duration) && timeTooltip && tooltipCanvas) {
                    const targetTime = pos * video.duration;
                    const label = formatTime(targetTime);
                    timeTooltip.textContent = label;
                    const scale = window.devicePixelRatio || 1;
                    const width = Math.round(rect.width * scale);
                    if (tooltipCanvas.width !== width || tooltipCanvas.height !== 34 * scale) {
                        tooltipCanvas.width = width;
                        tooltipCanvas.height = 34 * scale;
                    }
                    const ctx = tooltipCanvas.getContext('2d');
                    ctx.clearRect(0, 0, tooltipCanvas.width, tooltipCanvas.height);
                    ctx.font = `${12 * scale}px sans-serif`;
                    const boxWidth = ctx.measureText(label).width + 18 * scale;
                    const center = Math.max(boxWidth / 2, Math.min(width - boxWidth / 2, pos * width));
                    ctx.fillStyle = '#101b2c';
                    ctx.fillRect(center - boxWidth / 2, 0, boxWidth, 26 * scale);
                    ctx.fillStyle = '#fff';
                    ctx.textAlign = 'center';
                    ctx.fillText(label, center, 18 * scale);
                    tooltipCanvas.classList.add('is-visible');
                }
            });
            timeline.addEventListener('mouseleave', () => { if (tooltipCanvas) tooltipCanvas.classList.remove('is-visible'); });
        }

        // Volume & Mute
        if (volSlider) {
            volSlider.addEventListener('input', (e) => {
                video.volume = e.target.value;
                video.muted = (video.volume === 0);
                updateVolIcons();
            });
        }

        if (muteBtn) {
            muteBtn.addEventListener('click', () => {
                video.muted = !video.muted;
                updateVolIcons();
            });
        }

        function updateVolIcons() {
            if (!volHigh || !volMute) return;
            const isMuted = (video.muted || video.volume === 0);
            if (isMuted) {
                volHigh.classList.add('hidden');
                volHigh.style.display = 'none';
                volMute.classList.remove('hidden');
                volMute.style.display = '';
                if (muteBtn) muteBtn.setAttribute('title', window.AVi18n.t('إلغاء كتم الصوت (M)'));
            } else {
                volHigh.classList.remove('hidden');
                volHigh.style.display = '';
                volMute.classList.add('hidden');
                volMute.style.display = 'none';
                if (muteBtn) muteBtn.setAttribute('title', window.AVi18n.t('كتم الصوت (M)'));
            }
        }

        // Fullscreen
        if (fsBtn) {
            fsBtn.addEventListener('click', async () => {
                try {
                    if (!document.fullscreenElement) {
                        if (container.requestFullscreen) await container.requestFullscreen();
                        else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen();
                    } else if (document.exitFullscreen) await document.exitFullscreen();
                } catch (_) { /* Browser may disallow fullscreen; playback is unaffected. */ }
            });
            document.addEventListener('fullscreenchange', () => {
                const isFs = !!document.fullscreenElement;
                const iconFsEnter = document.getElementById('icon-fs-enter');
                const iconFsExit = document.getElementById('icon-fs-exit');
                if (iconFsEnter) {
                    iconFsEnter.classList.toggle('hidden', isFs);
                    iconFsEnter.style.display = isFs ? 'none' : '';
                }
                if (iconFsExit) {
                    iconFsExit.classList.toggle('hidden', !isFs);
                    iconFsExit.style.display = isFs ? '' : 'none';
                }
                if (fsBtn) fsBtn.setAttribute('title', isFs ? window.AVi18n.t('الخروج من ملء الشاشة (F)') : window.AVi18n.t('ملء الشاشة (F)'));
            });
        }

        // PiP
        if (pipBtn) {
            if (document.pictureInPictureEnabled) {
                pipBtn.addEventListener('click', async () => {
                    if (document.pictureInPictureElement) {
                        await document.exitPictureInPicture();
                    } else {
                        await video.requestPictureInPicture();
                    }
                });
            } else {
                pipBtn.classList.add('hidden');
            }
        }

        // Keyboard Shortcuts
        window.addEventListener('keydown', (e) => {
            if (['input', 'textarea', 'select', 'button'].includes(document.activeElement.tagName.toLowerCase())) return;
            if (e.key === ' ' || e.key === 'k' || e.key === 'K') { e.preventDefault(); togglePlay(); }
            if (e.key === 'f' || e.key === 'F') { if (fsBtn) { e.preventDefault(); fsBtn.click(); } }
            if (e.key === 'm' || e.key === 'M') { if (muteBtn) { e.preventDefault(); muteBtn.click(); } }
            if (e.key === 'ArrowRight') { e.preventDefault(); video.currentTime = Math.min(video.duration, video.currentTime + 5); }
            if (e.key === 'ArrowLeft') { e.preventDefault(); video.currentTime = Math.max(0, video.currentTime - 5); }
            if (e.key === 'ArrowUp') {
                e.preventDefault();
                video.volume = Math.min(1, video.volume + 0.1);
                if (volSlider) volSlider.value = video.volume;
                updateVolIcons();
            }
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                video.volume = Math.max(0, video.volume - 0.1);
                if (volSlider) volSlider.value = video.volume;
                updateVolIcons();
            }
        });

        populateSpeedMenu();

        // Advanced Diagnostics Panel (QoE telemetry)
        initDiagnostics();

        // Periodic Heartbeat Analytics
        setInterval(() => {
            if (!video.paused) {
                trackAnalytics('heartbeat', 15.0);
            }
        }, 15000);
    }

    function initMenus(video, container, controls) {
        const panel = document.getElementById('player-menu-panel');
        const settingsBtn = document.getElementById('btn-settings');
        const ccBtn = document.getElementById('btn-subs');
        if (!panel) return;
        [['quality-menu', 'quality-options'], ['speed-menu', 'speed-options'], ['subs-menu', 'subtitle-options']].forEach(([menuId, targetId]) => {
            const menu = document.getElementById(menuId);
            const target = document.getElementById(targetId);
            if (menu && target) target.appendChild(menu);
        });
        function open(screen) {
            panel.classList.add('is-open');
            panel.setAttribute('aria-hidden', 'false');
            settingsBtn?.setAttribute('aria-expanded', 'true');
            panel.querySelectorAll('.player-menu-screen').forEach(el => el.classList.toggle('is-active', el.dataset.screen === screen));
            controls?.classList.remove('hidden');
        }
        function close() {
            panel.classList.remove('is-open');
            panel.setAttribute('aria-hidden', 'true');
            settingsBtn?.setAttribute('aria-expanded', 'false');
        }
        settingsBtn?.addEventListener('click', e => {
            e.stopPropagation();
            if (panel.classList.contains('is-open')) close(); else open('root');
        });
        ccBtn?.addEventListener('click', e => {
            e.stopPropagation();
            if (panel.classList.contains('is-open') && panel.querySelector('[data-screen="subtitles"]')?.classList.contains('is-active')) close();
            else open('subtitles');
        });
        panel.querySelectorAll('[data-open-screen]').forEach(button => button.addEventListener('click', () => open(button.dataset.openScreen)));
        document.addEventListener('click', e => {
            if (!panel.contains(e.target) && !settingsBtn?.contains(e.target) && !ccBtn?.contains(e.target)) close();
        });
        document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
        container.addEventListener('click', e => { if (e.target === video) close(); });
    }

    function populateQualityMenu(levels) {
        const menu = document.getElementById('quality-menu');
        if (!menu) return;
        menu.replaceChildren();
        const autoOpt = document.createElement('button');
        autoOpt.type = 'button';
        autoOpt.className = 'menu-item';
        autoOpt.id = 'qual-opt-auto';
        autoOpt.addEventListener('click', () => selectQuality(-1));
        menu.appendChild(autoOpt);

        const seenHeights = new Set();
        const available = levels.map((lvl, idx) => ({ lvl, idx }))
            .sort((a, b) => (b.lvl.height || 0) - (a.lvl.height || 0) || (b.lvl.bitrate || 0) - (a.lvl.bitrate || 0))
            .filter(({ lvl }) => {
                const key = lvl.height || `bitrate-${lvl.bitrate}`;
                if (seenHeights.has(key)) return false;
                seenHeights.add(key);
                return true;
            });
        available.forEach(({ lvl, idx }) => {
            const label = lvl.height >= 2160 ? '4K · 2160p' : (lvl.height ? `${lvl.height}p` : `${Math.round(lvl.bitrate / 1000)}k`);
            const item = document.createElement('button');
            item.type = 'button';
            item.className = 'menu-item';
            item.id = `qual-opt-${idx}`;
            item.textContent = label;
            item.addEventListener('click', () => selectQuality(idx));
            menu.appendChild(item);
        });
        updateQualityMenuSelection(actualQualityIndex);
    }

    function selectQuality(lvlIdx) {
        if (!hls) return;
        const video = document.getElementById('video-element');

        if (lvlIdx === -1) {
            // Auto Mode: Keep ABR active
            selectedQualityIndex = -1;
            pendingQualityIndex = null;
            isQualitySwitching = false;
            manualSwitchTimestamp = 0;
            currentQoEState = QoEState.IDLE;
            consecutiveLowBwCount = 0;
            if (stallTimer) {
                clearTimeout(stallTimer);
                stallTimer = null;
            }
            stallStartTime = null;
            hls.nextLevel = -1; // Let ABR adapt freely
            updateQualityMenuSelection(actualQualityIndex);
            showQualityToast(window.AVi18n.t('تلقائي (Auto)'), false, 2500);
            return;
        }

        // Manual Mode: Disable automatic level selection, lock requested level
        selectedQualityIndex = lvlIdx;
        pendingQualityIndex = lvlIdx;
        isQualitySwitching = true;
        manualSwitchTimestamp = Date.now();
        currentQoEState = QoEState.SWITCHING;
        consecutiveLowBwCount = 0;
        if (stallTimer) {
            clearTimeout(stallTimer);
            stallTimer = null;
        }
        stallStartTime = null;
        const targetLabel = getQualityShortLabel(lvlIdx);

        // Immediate UI feedback: "جاري التبديل إلى 4K..."
        updateQualityMenuSelection(actualQualityIndex, window.AVi18n.tpl`جاري التبديل إلى ${targetLabel}...`);
        showQualityToast(window.AVi18n.tpl`جاري التبديل إلى ${targetLabel}...`, false, 4000);

        // Safe Buffer Strategy:
        // Do not blindly destroy the entire buffer or stall playback.
        // Determine safe boundary (current fragment end, or +1.5s forward).
        const v = video;
        const curFrag = (hls.streamController?.fragPlaying) ||
                        (hls.streamController?.getBufferedFrag && hls.streamController.getBufferedFrag(v ? v.currentTime : 0));
        const curEnd = curFrag ? (curFrag.end || (curFrag.start + curFrag.duration)) : (v ? v.currentTime : 0);

        let flushStart = curEnd;
        if (v && (curEnd - v.currentTime < 1.5)) {
            const nextFrag = hls.streamController?.followingBufferedFrag ? hls.streamController.followingBufferedFrag(curFrag) : null;
            if (nextFrag) {
                flushStart = nextFrag.end || (nextFrag.start + nextFrag.duration);
            } else {
                flushStart = v.currentTime + 1.5;
            }
        }

        // Abort in-flight request if loading beyond flush boundary
        if (hls.streamController?.fragCurrent && hls.streamController.fragCurrent.start >= flushStart) {
            if (typeof hls.streamController.abortCurrentFrag === 'function') {
                hls.streamController.abortCurrentFrag();
            }
        }

        // Flush prebuffered tail beyond safe boundary
        if (Hls.Events && Hls.Events.BUFFER_FLUSHING && typeof hls.trigger === 'function') {
            hls.trigger(Hls.Events.BUFFER_FLUSHING, {
                startOffset: flushStart,
                endOffset: Number.POSITIVE_INFINITY,
                type: null
            });
        }

        // Set nextLevel to requested target level so next segment fetched is of the target rendition
        hls.nextLevel = lvlIdx;
    }

    function updateQualityMenuSelection(currentIdx, overrideSummary = null) {
        const auto = document.getElementById('qual-opt-auto');
        const actual = hls?.levels?.[currentIdx];
        const actualLabel = actual ? (actual.height >= 2160 ? '4K · 2160p' : (actual.height ? `${actual.height}p` : `${Math.round(actual.bitrate / 1000)}k`)) : '';
        if (auto) auto.textContent = window.AVi18n.tpl`تلقائي (Auto)${actualLabel ? window.AVi18n.tpl` · الآن ${actualLabel}` : ''}`;
        document.querySelectorAll('#quality-menu .menu-item').forEach(el => {
            const active = el.id === (selectedQualityIndex === -1 ? 'qual-opt-auto' : `qual-opt-${selectedQualityIndex}`);
            el.classList.toggle('active', active);
            el.setAttribute('aria-checked', String(active));
            const mark = el.querySelector('.menu-check');
            if (mark) mark.remove();
            if (active) {
                const check = document.createElement('span');
                check.className = 'menu-check';
                check.textContent = '✓';
                el.appendChild(check);
            }
        });
        const summary = document.getElementById('quality-summary');
        if (summary) {
            if (overrideSummary) {
                summary.textContent = overrideSummary;
            } else if (selectedQualityIndex === -1) {
                summary.textContent = `Auto${actualLabel ? ` · ${actualLabel}` : ''}`;
            } else if (isQualitySwitching && pendingQualityIndex !== null) {
                summary.textContent = window.AVi18n.tpl`جاري التبديل إلى ${getQualityShortLabel(pendingQualityIndex)}...`;
            } else {
                summary.textContent = `${getQualityShortLabel(selectedQualityIndex)} ✓`;
            }
        }
    }

    function populateSpeedMenu() {
        const menu = document.getElementById('speed-menu');
        if (!menu) return;
        menu.replaceChildren();
        [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2].forEach(speed => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'menu-item';
            button.dataset.speed = String(speed);
            button.textContent = speed === 1 ? window.AVi18n.t('طبيعي · 1x') : `${speed}x`;
            button.addEventListener('click', () => setSpeed(speed));
            menu.appendChild(button);
        });
        setSpeed(1);
    }

    function setSpeed(speed) {
        const video = document.getElementById('video-element');
        if (!video) return;
        video.playbackRate = speed;
        document.querySelectorAll('#speed-menu .menu-item').forEach(el => {
            const active = Number(el.dataset.speed) === speed;
            el.classList.toggle('active', active);
            el.setAttribute('aria-checked', String(active));
            el.querySelector('.menu-check')?.remove();
            if (active) { const check = document.createElement('span'); check.className = 'menu-check'; check.textContent = '✓'; el.appendChild(check); }
        });
        const summary = document.getElementById('speed-summary');
        if (summary) summary.textContent = speed === 1 ? window.AVi18n.t('طبيعي') : `${speed}x`;
    }

    function readEmbeddedFontFamily(bytes) {
        // SFNT name ID 1 is what libass's embedded font provider matches.
        // Attached ASS aliases can differ from this name (e.g. Adobe Arabic).
        try {
            const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
            if (bytes.length < 12 || ![0x00010000, 0x4f54544f].includes(view.getUint32(0))) return '';
            const tables = view.getUint16(4);
            if (tables > 512 || 12 + tables * 16 > bytes.length) return '';
            for (let i = 0; i < tables; i++) {
                const entry = 12 + i * 16;
                if (view.getUint32(entry) !== 0x6e616d65) continue;
                const start = view.getUint32(entry + 8), length = view.getUint32(entry + 12);
                if (length < 6 || start + length > bytes.length) return '';
                const count = view.getUint16(start + 2), strings = view.getUint16(start + 4);
                if (count > 4096 || 6 + count * 12 > length) return '';
                const candidates = [];
                for (let j = 0; j < count; j++) {
                    const record = start + 6 + j * 12;
                    const platform = view.getUint16(record), language = view.getUint16(record + 4);
                    if (![0, 3].includes(platform) || view.getUint16(record + 6) !== 1) continue;
                    const size = view.getUint16(record + 8), offset = strings + view.getUint16(record + 10);
                    if (!size || size % 2 || offset + size > length) continue;
                    const family = new TextDecoder('utf-16be', { fatal: true }).decode(bytes.subarray(start + offset, start + offset + size)).replace(/\0/g, '').trim();
                    if (family && family.length <= 255 && !/[,\\\r\n]/.test(family)) candidates.push({ family, priority: platform === 3 && language === 1033 ? 0 : platform === 0 ? 1 : 2 });
                }
                candidates.sort((a, b) => a.priority - b.priority);
                return candidates[0]?.family || '';
            }
        } catch (_) { /* An unsupported font retains its declared family. */ }
        return '';
    }

    function resolveAssFontAliases(content, aliases) {
        const resolve = name => {
            const value = name.trim(), vertical = value.startsWith('@');
            return (vertical ? '@' : '') + (aliases.get((vertical ? value.slice(1) : value).toLowerCase()) || (vertical ? value.slice(1) : value));
        };
        let styles = false, fontIndex = -1;
        return content.split(/\r?\n/).map(line => {
            if (/^\s*\[/.test(line)) { styles = /^\s*\[V4\+? Styles\]\s*$/i.test(line); fontIndex = -1; }
            if (styles && /^Format:/i.test(line)) fontIndex = line.slice(line.indexOf(':') + 1).split(',').findIndex(field => field.trim().toLowerCase() === 'fontname');
            if (styles && fontIndex >= 0 && /^Style:/i.test(line)) {
                const prefix = line.slice(0, line.indexOf(':') + 1), fields = line.slice(line.indexOf(':') + 1).split(',');
                if (fontIndex < fields.length) fields[fontIndex] = resolve(fields[fontIndex]);
                line = prefix + fields.join(',');
            }
            return line.replace(/\{([^{}]*)\}/g, (_, tags) => '{' + tags.replace(/\\fn([^\\}\r\n]+)/g, (_, family) => '\\fn' + resolve(family)) + '}');
        }).join('\n');
    }

    async function loadFontsForTrack(sub) {
        const fontsToLoad = sub.fonts || [];
        const requiredCount = fontsToLoad.length;
        let loadedCount = 0;
        let missingCount = 0;
        const fontBuffers = [];
        const familyAliases = new Map();

        for (const f of fontsToLoad) {
            try {
                if (sessionFontCache.has(f.id)) {
                    fontBuffers.push(sessionFontCache.get(f.id));
                    loadedCount++;
                } else {
                    const fontUrl = getPlaybackEndpoint(`fonts/${encodeURIComponent(f.id)}`);
                    const res = await fetch(fontUrl);
                    if (!res.ok) {
                        throw new Error(`HTTP ${res.status}`);
                    }
                    const buf = await res.arrayBuffer();
                    const uint8 = new Uint8Array(buf);
                    sessionFontCache.set(f.id, uint8);
                    fontBuffers.push(uint8);
                    loadedCount++;
                }
                const family = readEmbeddedFontFamily(sessionFontCache.get(f.id));
                if (family && f.family) familyAliases.set(f.family.toLowerCase(), family);
            } catch (err) {
                console.warn(`Could not load font "${f.family}" (${f.filename}):`, err);
                missingCount++;
            }
        }

        console.log(`Required fonts: ${requiredCount} | Loaded: ${loadedCount} | Missing: ${missingCount}`);

        const warningBanner = document.getElementById('sub-warning-banner');
        if (warningBanner) {
            if (missingCount > 0) {
                warningBanner.classList.remove('hidden');
                setTimeout(() => warningBanner.classList.add('hidden'), 5000);
            } else {
                warningBanner.classList.add('hidden');
            }
        }

        return { fontBuffers, familyAliases, requiredCount, loadedCount, missingCount };
    }

    function initAssViewport(video) {
        const viewport = document.getElementById('ass-viewport');
        if (!viewport) return;
        const sync = () => {
            const boxWidth = video.offsetWidth;
            const boxHeight = video.offsetHeight;
            if (!video.videoWidth || !video.videoHeight || !boxWidth || !boxHeight) {
                viewport.style.display = 'none';
                return;
            }
            // Match JASSUB's contain sizing. Its supplied-canvas mode anchors
            // the canvas at (0, 0), so its parent must own the picture offset.
            const aspect = video.videoWidth / video.videoHeight;
            let width = boxWidth, height = boxHeight;
            if (boxWidth / boxHeight > aspect) width = Math.floor(boxHeight * aspect);
            else height = Math.floor(boxWidth / aspect);
            viewport.style.left = `${video.offsetLeft + (boxWidth - width) / 2}px`;
            viewport.style.top = `${video.offsetTop + (boxHeight - height) / 2}px`;
            viewport.style.width = `${width}px`;
            viewport.style.height = `${height}px`;
            viewport.style.display = 'block';
        };
        video.addEventListener('loadedmetadata', sync);
        video.addEventListener('resize', sync);
        window.addEventListener('resize', sync);
        document.addEventListener('fullscreenchange', sync);
        if (typeof ResizeObserver !== 'undefined') {
            const observer = new ResizeObserver(sync);
            observer.observe(video);
        }
        sync();
    }

    async function activateAssSubtitle(video, sub) {
        destroyAssSubtitle();
        const activationGeneration = assActivationGeneration;

        // Disable all HTML5 text tracks
        for (let i = 0; i < video.textTracks.length; i++) {
            video.textTracks[i].mode = 'disabled';
        }

        // Load fonts for this track
        const { fontBuffers, familyAliases } = await loadFontsForTrack(sub);
        if (activationGeneration !== assActivationGeneration) return;

        const subFileUrl = getPlaybackEndpoint(`subtitles/${encodeURIComponent(sub.id)}/file`);
        let subContent = null;
        try {
            const subRes = await fetch(subFileUrl);
            if (subRes.ok) {
                subContent = resolveAssFontAliases(await subRes.text(), familyAliases);
            }
        } catch (e) {
            console.warn('Failed to fetch subtitle content in main thread:', e);
        }
        if (activationGeneration !== assActivationGeneration) return;

        const canvas = document.getElementById('ass-canvas');

        if (window.JASSUB) {
            try {
                jassubInstance = new JASSUB({
                    video: video,
                    canvas: canvas,
                    subUrl: subContent ? undefined : subFileUrl,
                    subContent: subContent || undefined,
                    fonts: fontBuffers,
                    // Use the attached Arabic-capable family for unmatched glyphs;
                    // a Latin-only default can otherwise render Arabic as boxes.
                    fallbackFont: (sub.fonts || []).some(f => /^arial$/i.test(f.family || '')) ? 'Arial' : undefined,
                    workerUrl: '/static/vendor/jassub/jassub-worker.js',
                    wasmUrl: '/static/vendor/jassub/jassub-worker.wasm',
                    onDemandRender: false,
                    debug: false,
                });
                window.jassubInstance = jassubInstance;
            } catch (err) {
                console.error('Failed to initialize JASSUB:', err);
            }
        } else {
            console.warn('JASSUB is not available in window');
        }

        toggleCaptionSettingsForAss(true);
    }

    function destroyAssSubtitle() {
        // Invalidate pending font/subtitle fetches as well as an existing worker.
        // A late completion must not override a more recent subtitle selection.
        assActivationGeneration++;
        if (jassubInstance) {
            try {
                jassubInstance.destroy();
            } catch (e) {
                console.warn('Error destroying JASSUB:', e);
            }
            jassubInstance = null;
            window.jassubInstance = null;
        }
        const canvas = document.getElementById('ass-canvas');
        if (canvas) {
            try {
                const ctx = canvas.getContext('2d');
                if (ctx) {
                    ctx.clearRect(0, 0, canvas.width, canvas.height);
                }
            } catch (e) {
                // OffscreenCanvas control transferred to worker
            }
        }
        toggleCaptionSettingsForAss(false);
    }

    function toggleCaptionSettingsForAss(isAssActive) {
        const captionScreen = document.querySelector('.caption-style-screen');
        if (!captionScreen) return;
        const inputs = captionScreen.querySelectorAll('select, input, button');
        inputs.forEach(el => {
            if (el.classList.contains('player-menu-back')) return;
            el.disabled = isAssActive;
        });
        let notice = captionScreen.querySelector('.caption-style-disabled-notice');
        if (isAssActive) {
            if (!notice) {
                notice = document.createElement('div');
                notice.className = 'caption-style-disabled-notice';
                notice.textContent = window.AVi18n.t('ترجمة ASS تحتفظ بتنسيقها، خطوطها ومؤثراتها الأصلية. إعدادات المظهر تطبق على ترجمات WebVTT فقط.');
                const backBtn = captionScreen.querySelector('.player-menu-back');
                if (backBtn && backBtn.nextSibling) {
                    captionScreen.insertBefore(notice, backBtn.nextSibling);
                } else {
                    captionScreen.prepend(notice);
                }
            }
        } else {
            if (notice) notice.remove();
        }
    }

    function populateSubtitlesMenu() {
        const video = document.getElementById('video-element');
        const menu = document.getElementById('subs-menu');
        if (!video || !menu) return;
        menu.replaceChildren();

        const subOff = document.createElement('button');
        subOff.type = 'button';
        subOff.className = 'menu-item active';
        subOff.id = 'sub-opt-off';
        subOff.textContent = window.AVi18n.t('إيقاف');
        subOff.addEventListener('click', () => selectSubtitle(-1));
        menu.appendChild(subOff);

        SUBTITLES_LIST.forEach((sub, idx) => {
            const isAss = (sub.format || '').toLowerCase() === 'ass' || (sub.format || '').toLowerCase() === 'ssa' || sub.renderer === 'libass';

            if (sub.is_playable && !isAss) {
                const track = document.createElement('track');
                track.kind = 'subtitles';
                track.label = sub.label || sub.name;
                track.srclang = sub.lang;
                track.src = getPlaybackEndpoint(`subtitles/${encodeURIComponent(sub.id)}.vtt`);
                track.addEventListener('load', () => applyCaptionPosition(video));
                video.appendChild(track);
                subtitleElements[idx] = track;
            }

            const item = document.createElement('button');
            item.type = 'button';
            item.className = 'menu-item';
            item.id = `sub-opt-${idx}`;

            let displayLabel = sub.name || sub.label;
            if (isAss) {
                displayLabel = `${displayLabel} ASS`;
            } else if (sub.label && sub.label !== sub.name) {
                displayLabel = `${displayLabel} (${sub.label})`;
            }
            if (sub.is_default) displayLabel += window.AVi18n.t(' · افتراضية');
            if (sub.is_forced) displayLabel += window.AVi18n.t(' · إجبارية');
            if (!sub.is_playable) displayLabel += window.AVi18n.t(' · أرشيف فقط');

            item.textContent = displayLabel;
            if (sub.is_playable) {
                item.addEventListener('click', () => selectSubtitle(idx));
            } else {
                item.disabled = true;
            }
            menu.appendChild(item);
        });

        let defaultIndex = SUBTITLES_LIST.findIndex(sub => sub.is_playable && sub.is_default);
        if (defaultIndex < 0) defaultIndex = SUBTITLES_LIST.findIndex(sub => sub.is_playable && String(sub.lang || '').toLowerCase().startsWith('ar'));
        selectSubtitle(defaultIndex);
    }

    function selectSubtitle(subIdx) {
        const video = document.getElementById('video-element');
        if (!video) return;
        selectedSubtitleIndex = subIdx;

        document.querySelectorAll('#subs-menu .menu-item').forEach(el => {
            const active = el.id === (subIdx === -1 ? 'sub-opt-off' : `sub-opt-${subIdx}`);
            el.classList.toggle('active', active);
            el.setAttribute('aria-checked', String(active));
            el.querySelector('.menu-check')?.remove();
            if (active) {
                const check = document.createElement('span');
                check.className = 'menu-check';
                check.textContent = '✓';
                el.appendChild(check);
            }
        });

        // HLS.js disables native tracks when its own subtitle selection changes.
        // Turn its renderer off before applying our independently served VTT
        // selection, otherwise a valid selected track ends up disabled.
        if (hls) {
            hls.subtitleDisplay = false;
            hls.subtitleTrack = -1;
        }

        if (subIdx === -1) {
            destroyAssSubtitle();
            for (let i = 0; i < video.textTracks.length; i++) {
                video.textTracks[i].mode = 'disabled';
            }
        } else {
            const sub = SUBTITLES_LIST[subIdx];
            const isAss = (sub?.format || '').toLowerCase() === 'ass' || (sub?.format || '').toLowerCase() === 'ssa' || sub?.renderer === 'libass';
            if (isAss) {
                activateAssSubtitle(video, sub);
            } else {
                destroyAssSubtitle();
                syncSubtitleModes(video);
            }
        }

        const summary = document.getElementById('subtitle-summary');
        if (summary) {
            if (subIdx === -1) {
                summary.textContent = window.AVi18n.t('إيقاف');
            } else {
                const s = SUBTITLES_LIST[subIdx];
                const isAss = (s?.format || '').toLowerCase() === 'ass' || (s?.format || '').toLowerCase() === 'ssa' || s?.renderer === 'libass';
                summary.textContent = isAss ? `${s?.name || s?.label} ASS` : (s?.label || s?.name || window.AVi18n.t('تشغيل'));
            }
        }
    }

    function syncSubtitleModes(video) {
        const selectedTrack = subtitleElements[selectedSubtitleIndex]?.track;
        for (let i = 0; i < video.textTracks.length; i++) {
            const track = video.textTracks[i];
            track.mode = track === selectedTrack ? 'showing' : 'disabled';
        }
        applyCaptionPosition(video);
    }

    function initCaptionPreferences(video, container) {
        const names = Object.keys(captionDefaults);
        try {
            const saved = JSON.parse(localStorage.getItem('aboodvideo-caption-prefs') || '{}');
            for (const name of names) {
                const input = document.getElementById(`caption-${name}`);
                if (input && [...(input.options || [])].some(option => option.value === String(saved[name]))) captionPrefs[name] = String(saved[name]);
                if (name === 'opacity' && ['0', '25', '50', '75', '100'].includes(String(saved[name]))) captionPrefs[name] = String(saved[name]);
            }
        } catch (_) { /* Invalid or unavailable local preference storage. */ }
        function apply() {
            for (const name of names) {
                const input = document.getElementById(`caption-${name}`);
                if (input) input.value = captionPrefs[name];
                for (const value of [captionDefaults[name], ...(name === 'size' ? ['small', 'large', 'xlarge'] : name === 'color' ? ['yellow', 'cyan'] : name === 'background' ? ['off', 'light', 'medium'] : name === 'opacity' ? ['0', '25', '50', '100'] : name === 'position' ? ['top', 'center'] : name === 'outline' ? ['on'] : [])]) {
                    container.classList.remove(`caption-${name}-${value}`);
                }
                container.classList.add(`caption-${name}-${captionPrefs[name]}`);
            }
            applyCaptionPosition(video);
            try { localStorage.setItem('aboodvideo-caption-prefs', JSON.stringify(captionPrefs)); } catch (_) { /* Playback remains available. */ }
        }
        for (const name of names) {
            const input = document.getElementById(`caption-${name}`);
            input?.addEventListener('input', () => { captionPrefs[name] = input.value; apply(); });
        }
        document.getElementById('caption-reset')?.addEventListener('click', () => { captionPrefs = { ...captionDefaults }; apply(); });
        document.getElementById('caption-match-embedded')?.addEventListener('click', () => { captionPrefs = { ...captionPrefs, size: 'large', color: 'white', background: 'off', opacity: '0', position: 'center', outline: 'on' }; apply(); });
        apply();
        video.textTracks.addEventListener?.('change', () => applyCaptionPosition(video));
    }

    function applyCaptionPosition(video) {
        const track = subtitleElements[selectedSubtitleIndex]?.track;
        if (!track?.cues) return;
        for (let i = 0; i < track.cues.length; i++) {
            const cue = track.cues[i];
            if (!('line' in cue)) continue;
            if (captionPrefs.position === 'center') {
                cue.snapToLines = false;
                cue.line = 68;
            } else {
                cue.snapToLines = true;
                cue.line = captionPrefs.position === 'top' ? 0 : 'auto';
            }
        }
    }

    function formatTime(seconds) {
        if (!seconds || isNaN(seconds)) return "00:00";
        const m = Math.floor(seconds / 60);
        const s = Math.floor(seconds % 60);
        return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
    }

    function trackAnalytics(eventType, dur = 0.0) {
        if (!PLAYBACK_TOKEN) return;
        const video = document.getElementById('video-element');

        // 1. Detect active quality label
        let qualityLabel = 'Auto';
        if (typeof hls !== 'undefined' && hls) {
            if (hls.currentLevel >= 0) {
                qualityLabel = getQualityShortLabel(hls.currentLevel);
            } else if (typeof actualQualityIndex !== 'undefined' && actualQualityIndex >= 0) {
                qualityLabel = getQualityShortLabel(actualQualityIndex);
            }
        }

        // 2. Detect device type
        const ua = navigator.userAgent || '';
        let deviceType = 'desktop';
        if (/Mobi|Android|iPhone|iPod/i.test(ua)) {
            deviceType = 'mobile';
        } else if (/iPad|Tablet/i.test(ua)) {
            deviceType = 'tablet';
        }

        // 3. Detect referrer / embedding domain
        let referrer = '';
        try {
            referrer = document.referrer || (window.parent !== window ? (document.location.ancestorOrigins ? document.location.ancestorOrigins[0] : '') : '') || '';
        } catch (e) {
            referrer = '';
        }

        // 4. Estimate bytes received during this interval based on current level bitrate
        let estBytes = 0;
        if (dur > 0 && typeof hls !== 'undefined' && hls && hls.levels && hls.currentLevel >= 0 && hls.levels[hls.currentLevel]) {
            const br = hls.levels[hls.currentLevel].bitrate || 2500000;
            estBytes = Math.round((br / 8) * dur);
        }

        fetch(getPlaybackEndpoint('event'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                event: eventType,
                position: video ? video.currentTime : 0.0,
                duration: dur,
                bytes_received: estBytes,
                quality: qualityLabel,
                device_type: deviceType,
                referrer: referrer
            })
        }).catch(() => {});
    }

    // ─── Advanced Diagnostics (Stats for Nerds / QoE Panel) ───────────────────
    function initDiagnostics() {
        const panel = document.getElementById('player-diagnostics-panel');
        const closeBtn = document.getElementById('btn-diag-close');
        if (!panel) return;

        if (closeBtn) {
            closeBtn.addEventListener('click', () => {
                panel.classList.add('hidden');
                diagnosticsVisible = false;
            });
        }

        // Toggle on 'D' keypress (unless in text inputs) or Ctrl+Shift+D
        window.addEventListener('keydown', (e) => {
            if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable) return;
            if ((e.key === 'd' || e.key === 'D') && !e.ctrlKey && !e.altKey && !e.metaKey) {
                toggleDiagnostics();
            } else if (e.ctrlKey && e.shiftKey && (e.key === 'D' || e.key === 'd')) {
                toggleDiagnostics();
            }
        });

        if (diagnosticsInterval) clearInterval(diagnosticsInterval);
        diagnosticsInterval = setInterval(updateDiagnostics, 1000);
    }

    function toggleDiagnostics() {
        const panel = document.getElementById('player-diagnostics-panel');
        if (!panel) return;
        diagnosticsVisible = !diagnosticsVisible;
        panel.classList.toggle('hidden', !diagnosticsVisible);
        if (diagnosticsVisible) updateDiagnostics();
    }

    function updateDiagnostics() {
        const panel = document.getElementById('player-diagnostics-panel');
        if (!panel || panel.classList.contains('hidden')) return;

        const video = document.getElementById('video-element');
        const modeEl = document.getElementById('diag-mode');
        const reqEl = document.getElementById('diag-req-quality');
        const playEl = document.getElementById('diag-play-quality');
        const estBwEl = document.getElementById('diag-est-bw');
        const variantBitrateEl = document.getElementById('diag-variant-bitrate');
        const bufferEl = document.getElementById('diag-buffer');
        const snEl = document.getElementById('diag-sn');
        const dtEl = document.getElementById('diag-download-time');
        const dropEl = document.getElementById('diag-dropped-frames');
        const stallsEl = document.getElementById('diag-stalls');
        const sessionEl = document.getElementById('diag-session');

        if (modeEl) {
            modeEl.textContent = selectedQualityIndex === -1 ? 'Auto (ABR)' : `Manual (${getQualityShortLabel(selectedQualityIndex)})`;
        }
        if (reqEl) {
            reqEl.textContent = selectedQualityIndex === -1 ? 'Auto' : getQualityShortLabel(selectedQualityIndex);
        }
        if (playEl) {
            const actualLvl = hls?.levels?.[actualQualityIndex];
            playEl.textContent = actualLvl ? `${actualLvl.height}p (${actualLvl.width}x${actualLvl.height})` : '-';
        }
        if (estBwEl) {
            const bw = (hls?.bandwidthEstimate > 0) ? hls.bandwidthEstimate : ewmaBandwidthBps;
            estBwEl.textContent = bw > 0 ? `${(bw / 1000000).toFixed(2)} Mbps` : '-';
        }
        if (variantBitrateEl) {
            const actualLvl = hls?.levels?.[actualQualityIndex];
            const br = actualLvl?.bitrate || actualLvl?.averageBitrate || lastFragStats.bitrateBps;
            variantBitrateEl.textContent = br > 0 ? `${(br / 1000000).toFixed(2)} Mbps` : '-';
        }
        if (bufferEl) {
            const fb = getForwardBuffer(video);
            bufferEl.textContent = `${fb.toFixed(2)}s`;
        }
        if (snEl) {
            snEl.textContent = (lastFragStats.sn !== undefined && lastFragStats.sn !== 0) ? String(lastFragStats.sn) : '-';
        }
        if (dtEl) {
            dtEl.textContent = lastFragStats.downloadTimeMs ? `${lastFragStats.downloadTimeMs} ms` : '-';
        }
        if (dropEl && video) {
            if (typeof video.getVideoPlaybackQuality === 'function') {
                const q = video.getVideoPlaybackQuality();
                dropEl.textContent = String(q.droppedVideoFrames || 0);
            }
        }
        if (stallsEl) {
            stallsEl.textContent = String(stallCount);
        }
        if (sessionEl) {
            sessionEl.textContent = PLAYBACK_TOKEN.startsWith('s/') ? 'Active (Session)' : 'Active (Token)';
        }
    }

    // Expose for debugging if needed
    window.selectSubtitle = selectSubtitle;
    window.selectQuality = selectQuality;
    window.setSpeed = setSpeed;
    window.toggleDiagnostics = toggleDiagnostics;
})();
