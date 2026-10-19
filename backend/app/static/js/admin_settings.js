window.AVi18n ||= {t: value => value, tpl: (parts, ...values) => parts.reduce((out, part, i) => out + part + (i < values.length ? values[i] : ''), '')};
document.addEventListener('DOMContentLoaded', () => {
    if (window.location.hash === '#quality') history.replaceState(null, '', '#encoder');
    // --- CSRF Helper ---
    const csrf = () => document.cookie.split(';').map(part => part.trim().split('=')).find(([key]) => key === (document.body.dataset.csrfCookieName || 'aboodvideo_csrf'))?.[1] || '';

    // --- DOM Elements ---
    const tabPanes = document.querySelectorAll('.tab-pane');
    const feedbackEl = document.getElementById('settings-feedback');
    const stickySaveBar = document.getElementById('sticky-save-bar');
    const btnReset = document.getElementById('btn-reset-settings');
    const btnSave = document.getElementById('btn-save-settings');
    let settingsBaselineReady = false;

    // Embed & CDN Elements
    const embedOnlyModeCb = document.getElementById('embed-only-mode');
    const embedRedirectUrlInput = document.getElementById('embed-redirect-url');
    const allowedDomainsInput = document.getElementById('allowed-domains-input');
    const embedDefaultColorInput = document.getElementById('embed-default-color');
    const embedDefaultColorPicker = document.getElementById('embed-default-color-picker');
    const embedDefaultWatermarkInput = document.getElementById('embed-default-watermark');
    const embedDefaultSkipInput = document.getElementById('embed-default-skip');
    const embedBannerEnabledCb = document.getElementById('embed-banner-enabled');
    const embedBannerTitleInput = document.getElementById('embed-banner-title');
    const embedBannerLinkInput = document.getElementById('embed-banner-link');
    const embedBannerTextInput = document.getElementById('embed-banner-text');

    // The embed save bar tracks its own fields; encoder controls save separately.
    let initialEmbedState = {
        allowed_domains_raw: '',
        embed_only_mode: false,
        external_redirect_url: '',
        default_color: '#00d2be',
        default_watermark: 'ABOODVIDEO',
        default_skip_intro: 85,
        pause_banner: {
            enabled: false,
            title: window.AVi18n.t('تابعنا على قنواتنا الرسمية'),
            text: window.AVi18n.t('انضم إلينا لمتابعة جميع الحلقات والإصدارات الجديدة بجودة 4K'),
            link: ''
        }
    };

    // --- Feedback Display Helper ---
    function showFeedback(message, type = 'success') {
        if (!feedbackEl) return;
        feedbackEl.className = `alert alert-${type}`;
        feedbackEl.textContent = message;
        feedbackEl.classList.remove('hidden');
        setTimeout(() => {
            feedbackEl.classList.add('hidden');
        }, 5000);
    }

    // --- Tab Switching Logic ---
    function switchTab(tabKey) {
        if (stickySaveBar) stickySaveBar.hidden = tabKey === 'encoder';
        document.querySelectorAll('.settings-tab-btn').forEach(btn => {
            const isActive = btn.dataset.tab === tabKey;
            btn.classList.toggle('is-active', isActive);
            btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
        });

        tabPanes.forEach(pane => {
            const isActive = pane.id === `tab-${tabKey}`;
            pane.classList.toggle('is-active', isActive);
            pane.setAttribute('aria-hidden', isActive ? 'false' : 'true');
        });

        if (window.location.hash !== `#${tabKey}`) {
            history.replaceState(null, '', `#${tabKey}`);
        }
    }

    document.querySelectorAll('.settings-tab-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            if (window.location.pathname.startsWith('/admin/settings')) {
                e.preventDefault();
                const tabKey = btn.dataset.tab;
                if (tabKey) switchTab(tabKey);
            }
        });
    });

    // Check initial hash
    const currentHash = (window.location.hash || '').replace('#', '') || 'storage';
    if (document.getElementById(`tab-${currentHash}`)) {
        switchTab(currentHash);
    }

    window.addEventListener('hashchange', () => {
        const hash = (window.location.hash || '').replace('#', '') || 'storage';
        if (document.getElementById(`tab-${hash}`)) switchTab(hash);
    });

    // --- Color Picker Sync ---
    if (embedDefaultColorPicker && embedDefaultColorInput) {
        embedDefaultColorPicker.addEventListener('input', () => {
            embedDefaultColorInput.value = embedDefaultColorPicker.value;
            checkDirty();
        });
        embedDefaultColorInput.addEventListener('input', () => {
            let val = embedDefaultColorInput.value.trim();
            if (!val.startsWith('#')) val = '#' + val;
            if (/^#[0-9a-fA-F]{6}$/.test(val)) {
                embedDefaultColorPicker.value = val;
            }
            checkDirty();
        });
    }

    // Attach dirty checks to embed elements
    [
        embedOnlyModeCb,
        embedRedirectUrlInput,
        allowedDomainsInput,
        embedDefaultColorInput,
        embedDefaultWatermarkInput,
        embedDefaultSkipInput,
        embedBannerEnabledCb,
        embedBannerTitleInput,
        embedBannerLinkInput,
        embedBannerTextInput,
    ].forEach(el => {
        if (el) {
            el.addEventListener('input', checkDirty);
            el.addEventListener('change', checkDirty);
        }
    });

    // --- Dirty State Detection ---
    function getEmbedFormState() {
        return {
            allowed_domains_raw: allowedDomainsInput ? allowedDomainsInput.value.trim() : '',
            embed_only_mode: embedOnlyModeCb ? embedOnlyModeCb.checked : false,
            external_redirect_url: embedRedirectUrlInput ? embedRedirectUrlInput.value.trim() : '',
            default_color: embedDefaultColorInput ? embedDefaultColorInput.value.trim() : '#00d2be',
            default_watermark: embedDefaultWatermarkInput ? embedDefaultWatermarkInput.value.trim() : 'ABOODVIDEO',
            default_skip_intro: embedDefaultSkipInput ? parseInt(embedDefaultSkipInput.value, 10) || 85 : 85,
            pause_banner: {
                enabled: embedBannerEnabledCb ? embedBannerEnabledCb.checked : false,
                title: embedBannerTitleInput ? embedBannerTitleInput.value.trim() : '',
                link: embedBannerLinkInput ? embedBannerLinkInput.value.trim() : '',
                text: embedBannerTextInput ? embedBannerTextInput.value.trim() : '',
            }
        };
    }

    function checkDirty() {
        if (!settingsBaselineReady) {
            stickySaveBar?.classList.remove('is-visible');
            return false;
        }
        const currentEmbed = getEmbedFormState();
        const embedChanged =
            currentEmbed.allowed_domains_raw !== initialEmbedState.allowed_domains_raw ||
            currentEmbed.embed_only_mode !== initialEmbedState.embed_only_mode ||
            currentEmbed.external_redirect_url !== initialEmbedState.external_redirect_url ||
            currentEmbed.default_color !== initialEmbedState.default_color ||
            currentEmbed.default_watermark !== initialEmbedState.default_watermark ||
            currentEmbed.default_skip_intro !== initialEmbedState.default_skip_intro ||
            currentEmbed.pause_banner.enabled !== initialEmbedState.pause_banner.enabled ||
            currentEmbed.pause_banner.title !== initialEmbedState.pause_banner.title ||
            currentEmbed.pause_banner.link !== initialEmbedState.pause_banner.link ||
            currentEmbed.pause_banner.text !== initialEmbedState.pause_banner.text;

        const isDirty = embedChanged;
        if (stickySaveBar) {
            stickySaveBar.classList.toggle('is-visible', isDirty);
        }
        return isDirty;
    }

    // --- Reset Action ---
    if (btnReset) {
        btnReset.addEventListener('click', () => {
            // Restore embed inputs
            if (allowedDomainsInput) allowedDomainsInput.value = initialEmbedState.allowed_domains_raw;
            if (embedOnlyModeCb) embedOnlyModeCb.checked = initialEmbedState.embed_only_mode;
            if (embedRedirectUrlInput) embedRedirectUrlInput.value = initialEmbedState.external_redirect_url;
            if (embedDefaultColorInput) embedDefaultColorInput.value = initialEmbedState.default_color;
            if (embedDefaultColorPicker) embedDefaultColorPicker.value = initialEmbedState.default_color;
            if (embedDefaultWatermarkInput) embedDefaultWatermarkInput.value = initialEmbedState.default_watermark;
            if (embedDefaultSkipInput) embedDefaultSkipInput.value = initialEmbedState.default_skip_intro;
            if (embedBannerEnabledCb) embedBannerEnabledCb.checked = initialEmbedState.pause_banner.enabled;
            if (embedBannerTitleInput) embedBannerTitleInput.value = initialEmbedState.pause_banner.title;
            if (embedBannerLinkInput) embedBannerLinkInput.value = initialEmbedState.pause_banner.link;
            if (embedBannerTextInput) embedBannerTextInput.value = initialEmbedState.pause_banner.text;

            checkDirty();
            showFeedback(window.AVi18n.t('تم التراجع عن جميع التغييرات غير المحفوظة.'), 'info');
        });
    }

    // --- Save Action ---
    if (btnSave) {
        btnSave.addEventListener('click', async () => {
            btnSave.disabled = true;
            try {
                // Save only settings that remain on the settings page.
                const currentEmbed = getEmbedFormState();
                const embedRes = await fetch('/api/v1/admin/embed-settings', {
                    method: 'PUT',
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': csrf()
                    },
                    body: JSON.stringify(currentEmbed)
                });

                if (!embedRes.ok) {
                    throw new Error(`Embed settings error HTTP ${embedRes.status}`);
                }
                const savedEmbed = await embedRes.json();

                // Update initial states
                initialEmbedState = {
                    allowed_domains_raw: savedEmbed.allowed_domains_raw,
                    embed_only_mode: savedEmbed.embed_only_mode,
                    external_redirect_url: savedEmbed.external_redirect_url,
                    default_color: savedEmbed.default_color,
                    default_watermark: savedEmbed.default_watermark,
                    default_skip_intro: savedEmbed.default_skip_intro,
                    pause_banner: { ...savedEmbed.pause_banner }
                };

                checkDirty();
                showFeedback(window.AVi18n.t('تم حفظ جميع الإعدادات وإعدادات التضمين بنجاح.'), 'success');
            } catch (err) {
                console.error('Failed to save settings:', err);
                showFeedback(window.AVi18n.t('تعذر حفظ الإعدادات. يرجى التحقق من الاتصال بالخادم.'), 'danger');
            } finally {
                btnSave.disabled = false;
            }
        });
    }

    // --- Storage Telemetry Loader ---
    const fmtBytes = (bytes) => {
        if (bytes === undefined || bytes === null) return '—';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let v = bytes, i = 0;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + ' ' + units[i];
    };

    const setStorageUnavailable = () => {
        const status = document.getElementById('storage-status');
        const values = ['storage-used', 'storage-free', 'storage-total'];
        if (status) status.textContent = window.AVi18n.t('تعذر جلب الحالة');
        values.forEach(id => {
            const el = document.getElementById(id);
            if (el) el.textContent = window.AVi18n.t('غير متاح');
        });
    };

    fetch('/api/v1/health/storage', { credentials: 'include', cache: 'no-store' })
        .then(r => {
            if (!r.ok) throw new Error(`Storage health HTTP ${r.status}`);
            return r.json();
        })
        .then(data => {
            const d = data.details || data;
            const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
            set('storage-status', data.status === 'healthy' ? window.AVi18n.t('سليم') : (data.status || window.AVi18n.t('غير معروف')));
            set('storage-used', d.used_bytes == null ? window.AVi18n.t('غير متاح') : fmtBytes(d.used_bytes) + (d.used_percentage == null ? '' : ` (${d.used_percentage}٪)`));
            set('storage-free', fmtBytes(d.free_bytes));
            set('storage-total', fmtBytes(d.total_bytes));
            const usageProgress = document.getElementById('storage-usage-progress');
            const usagePercent = document.getElementById('storage-usage-percent');
            const pct = d.used_percentage == null ? null : Math.max(0, Math.min(100, Number(d.used_percentage)));
            if (usageProgress && pct != null) usageProgress.value = pct;
            if (usagePercent) usagePercent.textContent = pct == null ? window.AVi18n.t('غير متاح') : `${new Intl.NumberFormat('ar').format(pct)}٪`;
        })
        .catch(() => setStorageUnavailable());

    // --- Embed Settings Loader ---
    async function loadEmbedSettings() {
        try {
            const res = await fetch('/api/v1/admin/embed-settings', { credentials: 'include', cache: 'no-store' });
            if (!res.ok) throw new Error(`Embed settings HTTP ${res.status}`);
            const data = await res.json();

            if (allowedDomainsInput) allowedDomainsInput.value = data.allowed_domains_raw || '';
            if (embedOnlyModeCb) embedOnlyModeCb.checked = Boolean(data.embed_only_mode);
            if (embedRedirectUrlInput) embedRedirectUrlInput.value = data.external_redirect_url || '';
            if (embedDefaultColorInput) embedDefaultColorInput.value = data.default_color || '#00d2be';
            if (embedDefaultColorPicker) embedDefaultColorPicker.value = data.default_color || '#00d2be';
            if (embedDefaultWatermarkInput) embedDefaultWatermarkInput.value = data.default_watermark || 'ABOODVIDEO';
            if (embedDefaultSkipInput) embedDefaultSkipInput.value = data.default_skip_intro !== undefined ? data.default_skip_intro : 85;

            const pb = data.pause_banner || {};
            if (embedBannerEnabledCb) embedBannerEnabledCb.checked = Boolean(pb.enabled);
            if (embedBannerTitleInput) embedBannerTitleInput.value = pb.title || window.AVi18n.t('تابعنا على قنواتنا الرسمية');
            if (embedBannerLinkInput) embedBannerLinkInput.value = pb.link || '';
            if (embedBannerTextInput) embedBannerTextInput.value = pb.text || window.AVi18n.t('انضم إلينا لمتابعة جميع الحلقات والإصدارات الجديدة بجودة 4K');

            initialEmbedState = {
                allowed_domains_raw: data.allowed_domains_raw || '',
                embed_only_mode: Boolean(data.embed_only_mode),
                external_redirect_url: data.external_redirect_url || '',
                default_color: data.default_color || '#00d2be',
                default_watermark: data.default_watermark || 'ABOODVIDEO',
                default_skip_intro: data.default_skip_intro !== undefined ? data.default_skip_intro : 85,
                pause_banner: {
                    enabled: Boolean(pb.enabled),
                    title: pb.title || window.AVi18n.t('تابعنا على قنواتنا الرسمية'),
                    link: pb.link || '',
                    text: pb.text || window.AVi18n.t('انضم إلينا لمتابعة جميع الحلقات والإصدارات الجديدة بجودة 4K')
                }
            };
            settingsBaselineReady = true;
            checkDirty();
        } catch (e) {
            console.warn('Failed to load embed settings:', e);
            initialEmbedState = getEmbedFormState();
            settingsBaselineReady = true;
            checkDirty();
        }
    }

    async function loadGeoPolicy() {
        const status = document.getElementById('geo-policy-status');
        try {
            const response = await fetch('/api/v1/admin/analytics/geo-policy', { credentials: 'include', cache: 'no-store' });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const policy = await response.json();
            const enabled = document.getElementById('geo-block-enabled');
            const countries = document.getElementById('geo-block-countries');
            enabled.checked = !!policy.enabled;
            enabled.disabled = !policy.source_ready;
            countries.value = (policy.blocked_countries || []).join(', ');
            if (!policy.source_ready && status) status.textContent = window.AVi18n.t('الحجب غير متاح حتى يمرّر Cloudflare رمز الدولة الموثوق عبر Nginx.');
        } catch (_) { if (status) status.textContent = window.AVi18n.t('تعذر تحميل سياسة الدول.'); }
    }
    async function loadTransferPolicy() {
        try {
            const response = await fetch('/api/v1/admin/transfer-settings', { credentials: 'include', cache: 'no-store' });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const policy = await response.json();
            setTransferRate('playback', policy.playback_kbps);
            setTransferRate('upload', policy.upload_kbps);
            document.getElementById('transfer-concurrency').value = policy.concurrent_uploads;
        } catch (_) { document.getElementById('transfer-policy-status').textContent = window.AVi18n.t('تعذر تحميل حدود النقل.'); }
    }
    function setTransferRate(kind, rate) {
        const select = document.getElementById(`transfer-${kind}-rate`);
        const input = document.getElementById(`transfer-${kind}-kbps`);
        const known = [...select.options].some(option => option.value !== 'custom' && Number(option.value) === Number(rate));
        if (!known && Number(rate) > 0 && ![...select.options].some(option => option.value === 'custom-value')) {
            const customOption = document.createElement('option'); customOption.value = 'custom-value'; customOption.textContent = window.AVi18n.tpl`مخصص (${rate} Kbps)`; select.insertBefore(customOption, select.lastElementChild);
        }
        select.value = known ? String(rate) : (Number(rate) > 0 ? 'custom-value' : '0');
        input.value = Number(rate) || 0;
        input.classList.toggle('is-visible', select.value === 'custom' || select.value === 'custom-value');
    }
    ['playback', 'upload'].forEach(kind => document.getElementById(`transfer-${kind}-rate`)?.addEventListener('change', event => {
        const input = document.getElementById(`transfer-${kind}-kbps`);
        input.classList.toggle('is-visible', ['custom', 'custom-value'].includes(event.target.value));
        if (!['custom', 'custom-value'].includes(event.target.value)) input.value = event.target.value;
    }));
    document.getElementById('transfer-policy-save')?.addEventListener('click', async () => {
        const status = document.getElementById('transfer-policy-status');
        const button = document.getElementById('transfer-policy-save');
        const payload = {
            playback_kbps: transferRateValue('playback'),
            upload_kbps: transferRateValue('upload'),
            concurrent_uploads: Number(document.getElementById('transfer-concurrency').value)
        };
        if (!Number.isInteger(payload.playback_kbps) || !Number.isInteger(payload.upload_kbps) || !Number.isInteger(payload.concurrent_uploads) || payload.playback_kbps < 0 || payload.upload_kbps < 0 || payload.concurrent_uploads < 1 || payload.concurrent_uploads > 8) { status.textContent = window.AVi18n.t('تحقق من القيم: السرعات صفر أو أكثر والتزامن من 1 إلى 8.'); return; }
        button.disabled = true;
        try {
            const response = await fetch('/api/v1/admin/transfer-settings', { method: 'PUT', credentials: 'include', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() }, body: JSON.stringify(payload) });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            status.textContent = window.AVi18n.t('حُفظت الإعدادات؛ حد المشاهدة لكل استجابة، وحد الرفع لواجهة الإدارة الرسمية.');
        } catch (_) { status.textContent = window.AVi18n.t('تعذر حفظ حدود النقل.'); }
        finally { button.disabled = false; }
    });
    function transferRateValue(kind) {
        const selected = document.getElementById(`transfer-${kind}-rate`).value;
        return ['custom', 'custom-value'].includes(selected) ? Number(document.getElementById(`transfer-${kind}-kbps`).value) : Number(selected);
    }
    document.getElementById('geo-block-save')?.addEventListener('click', async () => {
        const status = document.getElementById('geo-policy-status');
        const button = document.getElementById('geo-block-save');
        const codes = document.getElementById('geo-block-countries').value.split(',').map(code => code.trim().toUpperCase()).filter(Boolean);
        if (codes.some(code => !/^[A-Z]{2}$/.test(code) || ['XX', 'T1'].includes(code))) { status.textContent = window.AVi18n.t('اكتب رموز دول من حرفين مثل US, CA.'); return; }
        button.disabled = true;
        try {
            const response = await fetch('/api/v1/admin/analytics/geo-policy', { method: 'PUT', credentials: 'include', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() }, body: JSON.stringify({ enabled: document.getElementById('geo-block-enabled').checked, blocked_countries: [...new Set(codes)] }) });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const policy = await response.json();
            document.getElementById('geo-block-enabled').checked = policy.enabled;
            document.getElementById('geo-block-countries').value = policy.blocked_countries.join(', ');
            status.textContent = window.AVi18n.t('حُفظت السياسة وتُطبّق على طلبات المشاهدة.');
        } catch (_) { status.textContent = window.AVi18n.t('تعذر حفظ السياسة. تحقق من جلسة الإدارة ورمز الحماية.'); }
        finally { button.disabled = false; }
    });

    const qualityStatus = document.getElementById('playback-quality-status');
    const qualityContainer = document.getElementById('playback-quality-options');
    async function loadPlaybackPolicy() {
        if (!qualityContainer) return;
        try {
            const response = await fetch('/api/v1/admin/quality-settings/playback', { credentials: 'include', cache: 'no-store' });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const policy = await response.json();
            qualityContainer.innerHTML = policy.available_qualities.map(q => `<label class="quality-option"><input type="checkbox" value="${q}" ${policy.enabled_qualities.includes(q) ? 'checked' : ''} ${q === '2160p' && !policy.available_qualities.includes(q) ? 'disabled' : ''}> ${q}</label>`).join('');
            if (qualityStatus) qualityStatus.textContent = window.AVi18n.t('السياسة الحالية مطبقة على قوائم HLS والطلبات المباشرة.');
        } catch (_) { qualityContainer.textContent = window.AVi18n.t('تعذر تحميل سياسة المشاهدة.'); }
    }
    document.getElementById('playback-quality-save')?.addEventListener('click', async () => {
        const button = document.getElementById('playback-quality-save');
        const selected = [...qualityContainer.querySelectorAll('input:checked')].map(input => input.value);
        if (!selected.length) { qualityStatus.textContent = window.AVi18n.t('اختر جودة واحدة على الأقل.'); return; }
        button.disabled = true;
        try {
            const response = await fetch('/api/v1/admin/quality-settings/playback', { method: 'PUT', credentials: 'include', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() }, body: JSON.stringify({ enabled_qualities: selected }) });
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            qualityStatus.textContent = window.AVi18n.t('حُفظت السياسة؛ تسري فوراً على طلبات التشغيل الجديدة.');
        } catch (_) { qualityStatus.textContent = window.AVi18n.t('تعذر حفظ سياسة الجودة.'); }
        finally { button.disabled = false; }
    });
    loadGeoPolicy();
    loadPlaybackPolicy();
    loadTransferPolicy();

    loadEmbedSettings();
});
