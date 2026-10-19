/**
 * ABOODVIDEO — Video Drive & Media File Manager Client Controller (V5)
 * Modern, high performance, full Drag & Drop, Context Menus, Breadcrumb sibling navigation,
 * Authoritative Content Filtering, and Soft-Delete / Trash management.
 * Strict CSP compliant: Zero inline styles.
 */

(function () {
    'use strict';

    // State
    const state = {
        currentScopeId: 'root',
        currentScopeType: 'root',
        contentFilter: 'content', // 'content' (default), 'uncategorized', 'test', 'all', 'trash'
        sortKey: 'name',
        sortAsc: true,
        searchQuery: '',
        currentData: null,
        visibleItems: [],
        page: 1,
        pageSize: 100,
        treeData: null,
        selectedItems: new Map(), // type:id -> visible item
        contextTarget: null,
        undoAction: null,
        pendingDeleteItems: [],
        pendingPermanentDeleteItems: [],
        trashBusy: false,
        clipboard: null, // { action: 'cut'|'copy', items: [] }
        viewMode: 'list', // 'list' | 'grid'
    };

    // DOM Elements
    const elements = {
        breadcrumbContainer: document.getElementById('breadcrumbContainer'),
        tableBody: document.getElementById('driveTableBody'),
        tableHeaderRow: document.getElementById('tableHeaderRow'),
        selectAllCheckbox: document.getElementById('selectAllCheckbox'),
        mobileSelectAllCheckbox: document.getElementById('mobileSelectAllCheckbox'),
        multiActionBar: document.getElementById('multiActionBar'),
        selectedCountText: document.getElementById('selectedCountText'),
        btnMultiMove: document.getElementById('btnMultiMove'),
        btnMultiShare: document.getElementById('btnMultiShare'),
        btnMultiTrash: document.getElementById('btnMultiTrash'),
        btnMultiRestore: document.getElementById('btnMultiRestore'),
        btnMultiPermanentDelete: document.getElementById('btnMultiPermanentDelete'),
        btnCancelSelection: document.getElementById('btnCancelSelection'),

        unifiedContextMenu: document.getElementById('unifiedContextMenu'),
        ctxMenuOpen: document.getElementById('ctxMenuOpen'),
        ctxMenuPlay: document.getElementById('ctxMenuPlay'),
        ctxMenuWatchPage: document.getElementById('ctxMenuWatchPage'),
        ctxMenuCopyWatchLink: document.getElementById('ctxMenuCopyWatchLink'),
        ctxMenuShare: document.getElementById('ctxMenuShare'),
        ctxMenuSubtitles: document.getElementById('ctxMenuSubtitles'),
        ctxMenuRename: document.getElementById('ctxMenuRename'),
        ctxMenuMove: document.getElementById('ctxMenuMove'),
        ctxMenuCopy: document.getElementById('ctxMenuCopy'),
        ctxMenuCut: document.getElementById('ctxMenuCut'),
        ctxMenuPolicy: document.getElementById('ctxMenuPolicy'),
        ctxMenuDetails: document.getElementById('ctxMenuDetails'),
        ctxMenuRestore: document.getElementById('ctxMenuRestore'),
        ctxMenuPermanentDelete: document.getElementById('ctxMenuPermanentDelete'),
        ctxMenuTrash: document.getElementById('ctxMenuTrash'),
        ctxMenuDividerMedia: document.getElementById('ctxMenuDividerMedia'),
        ctxMenuDividerTrash: document.getElementById('ctxMenuDividerTrash'),
        ctxMenuDivider: document.getElementById('ctxMenuDivider'),
        ctxMenuMultiMove: document.getElementById('ctxMenuMultiMove'),
        ctxMenuMultiCopy: document.getElementById('ctxMenuMultiCopy'),
        ctxMenuMultiCut: document.getElementById('ctxMenuMultiCut'),
        ctxMenuMultiShare: document.getElementById('ctxMenuMultiShare'),
        ctxMenuMultiTrash: document.getElementById('ctxMenuMultiTrash'),
        ctxMenuMultiDeselect: document.getElementById('ctxMenuMultiDeselect'),

        // Empty Area Context Menu
        emptyContextMenu: document.getElementById('emptyContextMenu'),
        emptyCtxNew: document.getElementById('emptyCtxNew'),
        emptyCtxNewSubmenu: document.getElementById('emptyCtxNewSubmenu'),
        emptyCtxNewFolder: document.getElementById('emptyCtxNewFolder'),
        emptyCtxNewSeries: document.getElementById('emptyCtxNewSeries'),
        emptyCtxNewSeason: document.getElementById('emptyCtxNewSeason'),
        emptyCtxNewDisabled: document.getElementById('emptyCtxNewDisabled'),


        emptyCtxPaste: document.getElementById('emptyCtxPaste'),
        emptyCtxRefresh: document.getElementById('emptyCtxRefresh'),

        emptyCtxSort: document.getElementById('emptyCtxSort'),
        emptyCtxSortSubmenu: document.getElementById('emptyCtxSortSubmenu'),
        sortByName: document.getElementById('sortByName'),
        sortByDate: document.getElementById('sortByDate'),
        sortBySize: document.getElementById('sortBySize'),
        sortByType: document.getElementById('sortByType'),
        sortDirAsc: document.getElementById('sortDirAsc'),
        sortDirDesc: document.getElementById('sortDirDesc'),

        emptyCtxView: document.getElementById('emptyCtxView'),
        emptyCtxViewSubmenu: document.getElementById('emptyCtxViewSubmenu'),
        viewModeList: document.getElementById('viewModeList'),
        viewModeGrid: document.getElementById('viewModeGrid'),

        // View Mode & Container
        tableContainer: document.querySelector('.table-container'),
        pagination: document.getElementById('drivePagination'),
        pageInfo: document.getElementById('drivePageInfo'),
        prevPage: document.getElementById('drivePrevPage'),
        nextPage: document.getElementById('driveNextPage'),
        btnViewList: document.getElementById('btnViewList'),
        btnViewGrid: document.getElementById('btnViewGrid'),

        // Modal location elements
        folderModalLocation: document.getElementById('folderModalLocation'),
        seriesModalLocation: document.getElementById('seriesModalLocation'),
        seasonModalLocation: document.getElementById('seasonModalLocation'),
        renameModalLocation: document.getElementById('renameModalLocation'),

        itemCountBadge: document.getElementById('itemCountBadge'),
        trashBanner: document.getElementById('trashBanner'),
        btnRestoreAllTrash: document.getElementById('btnRestoreAllTrash'),
        btnEmptyTrash: document.getElementById('btnEmptyTrash'),
        searchInput: document.getElementById('driveSearchInput'),

        // Unified New Dropdown
        newDropdownWrap: document.getElementById('newDropdownWrap'),
        btnNewDropdown: document.getElementById('btnNewDropdown'),
        newDropdownMenu: document.getElementById('newDropdownMenu'),
        menuItemNewFolder: document.getElementById('menuItemNewFolder'),
        menuItemNewSeries: document.getElementById('menuItemNewSeries'),
        menuItemNewSeason: document.getElementById('menuItemNewSeason'),
        btnQuickTrash: document.getElementById('btnQuickTrash'),
        trashCountBadge: document.getElementById('trashCountBadge'),

        // Content Filter Dropdown
        filterDropdownWrap: document.getElementById('filterDropdownWrap'),
        btnFilterDropdown: document.getElementById('btnFilterDropdown'),
        filterDropdownMenu: document.getElementById('filterDropdownMenu'),
        currentFilterLabel: document.getElementById('currentFilterLabel'),

        // Sort Dropdown
        sortDropdownWrap: document.getElementById('sortDropdownWrap'),
        btnSortDropdown: document.getElementById('btnSortDropdown'),
        sortDropdownMenu: document.getElementById('sortDropdownMenu'),
        currentSortLabel: document.getElementById('currentSortLabel'),

        btnViewList: document.getElementById('btnViewList'),

        // Navigation Sidebar Links
        navLibraryLink: document.getElementById('navLibraryLink'),
        navTrashLink: document.getElementById('navTrashLink'),
        navTrashCountBadge: document.getElementById('navTrashCountBadge'),
        trashCountBadge: document.getElementById('trashCountBadge'),

        // Details Drawer
        detailsDrawer: document.getElementById('detailsDrawer'),
        btnCloseDrawer: document.getElementById('btnCloseDrawer'),
        drawerTypeBadge: document.getElementById('drawerTypeBadge'),
        drawerTitle: document.getElementById('drawerTitle'),
        drawerFilename: document.getElementById('drawerFilename'),
        drawerPosterWrap: document.getElementById('drawerPosterWrap'),
        drawerPosterImg: null,
        drawerPosterPlaceholder: document.getElementById('drawerPosterPlaceholder'),
        drawerBadgeStatus: document.getElementById('drawerBadgeStatus'),
        drawerBadgeQuality: document.getElementById('drawerBadgeQuality'),
        drawerBadgeDuration: document.getElementById('drawerBadgeDuration'),
        btnDrawerPlay: document.getElementById('btnDrawerPlay'),
        btnDrawerOpenNewTab: document.getElementById('btnDrawerOpenNewTab'),
        btnDrawerCopyLink: document.getElementById('btnDrawerCopyLink'),
        btnDrawerShare: document.getElementById('btnDrawerShare'),

        tabBtnDrawerInfo: document.getElementById('tabBtnDrawerInfo'),
        tabBtnDrawerPlayback: document.getElementById('tabBtnDrawerPlayback'),
        tabBtnDrawerSubs: document.getElementById('tabBtnDrawerSubs'),
        tabBtnDrawerShare: document.getElementById('tabBtnDrawerShare'),
        tabBtnDrawerDownload: document.getElementById('tabBtnDrawerDownload'),
        drawerSubsCountBadge: document.getElementById('drawerSubsCountBadge'),

        drawerPaneInfo: document.getElementById('drawerPaneInfo'),
        drawerPanePlayback: document.getElementById('drawerPanePlayback'),
        drawerPaneSubs: document.getElementById('drawerPaneSubs'),
        drawerPaneShare: document.getElementById('drawerPaneShare'),
        drawerPaneDownload: document.getElementById('drawerPaneDownload'),

        drawerWatchLinkBox: document.getElementById('drawerWatchLinkBox'),
        drawerWatchUrlInput: document.getElementById('drawerWatchUrlInput'),
        btnDrawerCopyWatchUrl: document.getElementById('btnDrawerCopyWatchUrl'),

        drawerInfoTitle: document.getElementById('drawerInfoTitle'),
        drawerQuality: document.getElementById('drawerQuality'),
        drawerDuration: document.getElementById('drawerDuration'),
        drawerSize: document.getElementById('drawerSize'),
        drawerStatusText: document.getElementById('drawerStatusText'),
        drawerCreatedAt: document.getElementById('drawerCreatedAt'),
        drawerDownloadPolicyText: document.getElementById('drawerDownloadPolicyText'),

        drawerAdvId: document.getElementById('drawerAdvId'),
        drawerAdvShareId: document.getElementById('drawerAdvShareId'),
        drawerAdvFilename: document.getElementById('drawerAdvFilename'),
        drawerSubtitles: document.getElementById('drawerSubtitles'),

        drawerSubsList: document.getElementById('drawerSubsList'),

        drawerNoShareBox: document.getElementById('drawerNoShareBox'),
        btnDrawerCreateShare: document.getElementById('btnDrawerCreateShare'),
        drawerActiveShareBox: document.getElementById('drawerActiveShareBox'),
        drawerShareStatus: document.getElementById('drawerShareStatus'),
        drawerShareExpiry: document.getElementById('drawerShareExpiry'),
        drawerShareAllowDl: document.getElementById('drawerShareAllowDl'),
        drawerShareUrlInput: document.getElementById('drawerShareUrlInput'),
        btnDrawerCopyShareUrl: document.getElementById('btnDrawerCopyShareUrl'),
        btnDrawerRevokeShare: document.getElementById('btnDrawerRevokeShare'),

        drawerPolicySelect: document.getElementById('drawerPolicySelect'),
        drawerEffectiveBadge: document.getElementById('drawerEffectiveBadge'),
        btnDrawerDirectDownload: document.getElementById('btnDrawerDirectDownload'),

        // Modals
        folderModal: document.getElementById('folderModal'),
        folderModalInput: document.getElementById('folderModalInput'),
        btnConfirmCreateFolder: document.getElementById('btnConfirmCreateFolder'),
        btnCancelCreateFolder: document.getElementById('btnCancelCreateFolder'),
        btnCloseFolderModalX: document.getElementById('btnCloseFolderModalX'),

        seriesModal: document.getElementById('seriesModal'),
        seriesModalTitle: document.getElementById('seriesModalTitle'),
        seriesModalYear: document.getElementById('seriesModalYear'),
        seriesModalDesc: document.getElementById('seriesModalDesc'),
        btnConfirmCreateSeries: document.getElementById('btnConfirmCreateSeries'),
        btnCancelCreateSeries: document.getElementById('btnCancelCreateSeries'),
        btnCloseSeriesModalX: document.getElementById('btnCloseSeriesModalX'),

        seasonModal: document.getElementById('seasonModal'),
        seasonModalNumber: document.getElementById('seasonModalNumber'),
        seasonModalName: document.getElementById('seasonModalName'),
        btnConfirmCreateSeason: document.getElementById('btnConfirmCreateSeason'),
        btnCancelCreateSeason: document.getElementById('btnCancelCreateSeason'),
        btnCloseSeasonModalX: document.getElementById('btnCloseSeasonModalX'),

        renameModal: document.getElementById('renameModal'),
        renameModalInput: document.getElementById('renameModalInput'),
        btnConfirmRename: document.getElementById('btnConfirmRename'),
        btnCancelRename: document.getElementById('btnCancelRename'),
        btnCloseRenameModalX: document.getElementById('btnCloseRenameModalX'),

        shareModal: document.getElementById('shareModal'),
        shareTargetTypeBadge: document.getElementById('shareTargetTypeBadge'),
        shareTargetTitle: document.getElementById('shareTargetTitle'),
        shareAllowDownloadToggle: document.getElementById('shareAllowDownloadToggle'),
        sharePasswordToggle: document.getElementById('sharePasswordToggle'),
        sharePasswordInput: document.getElementById('sharePasswordInput'),
        shareLinkResultBox: document.getElementById('shareLinkResultBox'),
        shareUrlDisplay: document.getElementById('shareUrlDisplay'),
        btnCopyShareUrl: document.getElementById('btnCopyShareUrl'),
        btnOpenSharePreview: document.getElementById('btnOpenSharePreview'),
        btnRevokeShare: document.getElementById('btnRevokeShare'),
        btnGenerateShare: document.getElementById('btnGenerateShare'),
        btnCancelShare: document.getElementById('btnCancelShare'),
        btnCloseShareModalX: document.getElementById('btnCloseShareModalX'),

        moveModal: document.getElementById('moveModal'),
        moveDestinationSelect: document.getElementById('moveDestinationSelect'),
        btnConfirmMove: document.getElementById('btnConfirmMove'),
        btnCancelMove: document.getElementById('btnCancelMove'),
        btnCloseMoveModalX: document.getElementById('btnCloseMoveModalX'),


        // Deletion Modals
        deleteModal: document.getElementById('deleteModal'),
        deleteModalTitle: document.getElementById('deleteModalTitle'),
        deleteModalDesc: document.getElementById('deleteModalDesc'),
        deleteModalWarningBox: document.getElementById('deleteModalWarningBox'),
        deleteModalWarningList: document.getElementById('deleteModalWarningList'),
        btnConfirmDelete: document.getElementById('btnConfirmDelete'),
        btnCancelDelete: document.getElementById('btnCancelDelete'),
        btnCloseDeleteModalX: document.getElementById('btnCloseDeleteModalX'),

        permanentDeleteModal: document.getElementById('permanentDeleteModal'),
        permanentDeleteModalDesc: document.getElementById('permanentDeleteModalDesc'),
        permanentDeleteConfirmation: document.getElementById('permanentDeleteConfirmation'),
        btnConfirmPermanentDelete: document.getElementById('btnConfirmPermanentDelete'),
        btnCancelPermanentDelete: document.getElementById('btnCancelPermanentDelete'),
        btnClosePermDeleteModalX: document.getElementById('btnClosePermDeleteModalX'),

        mobileToggle: document.getElementById('mobileToggle'),
        sidebar: document.getElementById('adminSidebar'),
        toastContainer: document.getElementById('toastContainer'),
    };

    // =========================================================================
    // API UTILITIES
    // =========================================================================

    async function apiRequest(url, options = {}) {
        const silentError = Boolean(options.silentError);
        delete options.silentError;
        options.headers = options.headers || {};
        options.headers['Accept'] = 'application/json';
        const method = String(options.method || 'GET').toUpperCase();
        if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
            const csrfCookie = document.cookie
                .split(';')
                .map(part => part.trim())
                .find(part => part.startsWith((document.body.dataset.csrfCookieName || 'aboodvideo_csrf') + '='));
            if (csrfCookie && !options.headers['X-CSRF-Token']) {
                options.headers['X-CSRF-Token'] = decodeURIComponent(csrfCookie.slice(((document.body.dataset.csrfCookieName || 'aboodvideo_csrf') + '=').length));
            }
        }
        if (options.body && typeof options.body === 'object') {
            options.headers['Content-Type'] = 'application/json';
            options.body = JSON.stringify(options.body);
        }

        try {
            const res = await fetch(url, options);
            if (!res.ok) {
                const errData = await res.json().catch(() => ({}));
                throw new Error(errData.detail || `Request failed (${res.status})`);
            }
            return await res.json();
        } catch (err) {
            console.error('API Error:', err);
            if (!silentError) showToast(err.message || 'حدث خطأ في الاتصال بالخادم', 'error');
            throw err;
        }
    }

    // =========================================================================
    // SVG ICONS (LOCAL, NO EMOJIS)
    // =========================================================================

    function getEntitySvg(type) {
        switch (type) {
            case 'folder':
                return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
            case 'series':
                return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>';
            case 'season':
                return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polygon points="10 8 16 12 10 16 10 8"/></svg>';
            case 'video':
            case 'حلقة':
            default:
                return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>';
        }
    }

    // =========================================================================
    // NAVIGATION & SCOPE LOADING
    // =========================================================================

    async function loadTreeData() {
        try {
            const data = await apiRequest('/api/v1/drive/tree');
            state.treeData = data;
            if (elements.trashCountBadge && typeof data.trash_count === 'number') {
                elements.trashCountBadge.textContent = String(data.trash_count);
                elements.trashCountBadge.classList.toggle('is-hidden', data.trash_count === 0);
            }
            if (elements.navTrashCountBadge && typeof data.trash_count === 'number') {
                elements.navTrashCountBadge.textContent = String(data.trash_count);
                elements.navTrashCountBadge.classList.toggle('hidden', data.trash_count === 0);
            }
        } catch (e) {
            console.error('Failed to load tree metadata', e);
        }
    }

    function selectionKey(item) {
        return `${item.type}:${item.id}`;
    }

    function scopeUrl() {
        const params = new URLSearchParams();
        if (state.currentScopeId !== 'root') params.set('id', state.currentScopeId);
        if (state.currentScopeType !== 'root') params.set('type', state.currentScopeType);
        params.set('filter', state.contentFilter);
        if (state.searchQuery) params.set('q', state.searchQuery);
        if (state.contentFilter === 'trash') { params.set('page', String(state.page)); params.set('limit', String(state.pageSize)); }
        return `/api/v1/drive/scope?${params.toString()}`;
    }

    async function loadScope(scopeId, scopeType, query, retainSelection = false) {
        const nextScopeId = scopeId || 'root';
        const nextScopeType = scopeType || 'root';
        const nextQuery = query === undefined ? state.searchQuery : query;
        if (nextScopeId !== state.currentScopeId || nextScopeType !== state.currentScopeType || nextQuery !== state.searchQuery) state.page = 1;
        state.currentScopeId = nextScopeId;
        state.currentScopeType = nextScopeType;
        state.searchQuery = nextQuery;

        if (!retainSelection) clearSelection();
        closeAllDropdowns();

        try {
            const data = await apiRequest(scopeUrl());
            if (state.contentFilter === 'trash' && state.page > Math.max(1, data.pages || 1)) {
                state.page = Math.max(1, data.pages || 1);
                return await loadScope(state.currentScopeId, state.currentScopeType);
            }
            state.currentData = data;
            renderExplorer(data);
            await loadTreeData();
            return data;
        } catch (err) {
            console.error('Failed to load scope:', err);
            return null;
        }
    }

    // =========================================================================
    // RENDERING
    // =========================================================================

    function renderBreadcrumbs(breadcrumbs) {
        elements.breadcrumbContainer.innerHTML = '';
        if (!breadcrumbs || breadcrumbs.length === 0) return;

        breadcrumbs.forEach((crumb, idx) => {
            const isLast = (idx === breadcrumbs.length - 1);
            const wrap = document.createElement('div');
            wrap.className = 'crumb-item-wrap';

            // Button for crumb title
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = `crumb-btn ${isLast ? 'is-current' : ''}`;
            btn.textContent = crumb.name || 'مكتبة المحتوى';
            if (!isLast) {
                btn.addEventListener('click', () => {
                    if (crumb.type === 'trash') {
                        setFilterScope('trash');
                    } else {
                        if (state.contentFilter === 'trash') state.contentFilter = 'content';
                        loadScope(crumb.id, crumb.type);
                    }
                });

                // Support Drag & Drop onto parent breadcrumbs
                btn.addEventListener('dragover', (e) => {
                    e.preventDefault();
                    e.dataTransfer.dropEffect = 'move';
                    btn.classList.add('drag-over');
                });
                btn.addEventListener('dragleave', () => {
                    btn.classList.remove('drag-over');
                });
                btn.addEventListener('drop', async (e) => {
                    e.preventDefault();
                    btn.classList.remove('drag-over');
                    try {
                        const rawData = e.dataTransfer.getData('application/json');
                        if (!rawData) return;
                        const items = JSON.parse(rawData);
                        await moveItems(items, crumb.id === 'root' ? 'root' : crumb.id, crumb.type || 'folder');
                    } catch (err) {
                        console.error('Breadcrumb drop error:', err);
                    }
                });
            }
            wrap.appendChild(btn);

            // Sibling jump dropdown (if more than 1 sibling exists)
            if (crumb.siblings && crumb.siblings.length > 1) {
                const jumpBtn = document.createElement('button');
                jumpBtn.type = 'button';
                jumpBtn.className = 'crumb-jump-btn';
                jumpBtn.title = 'تخطي سريع للعناصر المجاورة';
                jumpBtn.innerHTML = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>';

                const jumpMenu = document.createElement('div');
                jumpMenu.className = 'crumb-jump-menu';

                crumb.siblings.forEach(sib => {
                    const isSelectedSib = (sib.id === crumb.id);
                    const sibItem = document.createElement('button');
                    sibItem.type = 'button';
                    sibItem.className = `dropdown-item ${isSelectedSib ? 'is-active' : ''}`;
                    sibItem.innerHTML = `${getEntitySvg(sib.type)}<span>${escapeHtml(sib.name)}</span>`;
                    sibItem.addEventListener('click', (e) => {
                        e.stopPropagation();
                        wrap.classList.remove('is-open');
                        loadScope(sib.id, sib.type);
                    });
                    jumpMenu.appendChild(sibItem);
                });

                jumpBtn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const wasOpen = wrap.classList.contains('is-open');
                    closeAllDropdowns();
                    if (!wasOpen) wrap.classList.add('is-open');
                });

                wrap.appendChild(jumpBtn);
                wrap.appendChild(jumpMenu);
            }

            elements.breadcrumbContainer.appendChild(wrap);

            if (!isLast) {
                const sep = document.createElement('span');
                sep.className = 'crumb-separator';
                sep.textContent = '›';
                elements.breadcrumbContainer.appendChild(sep);
            }
        });
    }

    function normalizeItem(raw) {
        if (!raw) return null;
        let type = raw.type || 'video';
        let type_label = raw.type_label || '';

        // Canonical type normalization: 'folder', 'series', 'season', 'video'
        const lowerType = String(type).toLowerCase().trim();
        if (lowerType === 'folder' || lowerType === 'مجلد') {
            type = 'folder';
            type_label = type_label || 'مجلد';
        } else if (lowerType === 'series' || lowerType === 'مسلسل') {
            type = 'series';
            type_label = type_label || 'مسلسل';
        } else if (lowerType === 'season' || lowerType === 'موسم') {
            type = 'season';
            type_label = type_label || 'موسم';
        } else {
            type_label = type_label || (type === 'حلقة' || raw.episode_number != null ? 'حلقة' : 'فيديو');
            type = 'video';
        }

        const name = raw.name || raw.title || 'عنصر';
        const filename = raw.filename || raw.source_filename || name;
        const has_subtitles = Boolean(raw.has_subtitles || (raw.subtitles && raw.subtitles !== 'لا يوجد' && raw.subtitles !== '—' && raw.subtitles !== 'بدون'));

        return {
            ...raw,
            id: String(raw.id),
            name,
            title: name,
            type,
            type_label,
            filename,
            has_subtitles,
            status: (raw.status || 'READY').toUpperCase(),
            quality: raw.quality || '',
            duration_formatted: raw.duration_formatted || '',
            size_formatted: raw.size_formatted || '',
        };
    }

    function getAdminPosterUrl(videoId) {
        const id = typeof videoId === 'string' ? videoId.trim() : '';
        if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id)) {
            return '';
        }
        return `/api/v1/admin/videos/${encodeURIComponent(id)}/poster`;
    }

    function renderExplorer(data) {
        const isTrash = (state.contentFilter === 'trash'
            || data.current_scope?.type === 'trash'
            || data.current_scope?.id === 'trash');

        // 1. Navigation Active Link States
        if (elements.navLibraryLink) elements.navLibraryLink.classList.toggle('is-active', !isTrash);
        if (elements.navTrashLink) elements.navTrashLink.classList.toggle('is-active', isTrash);
        if (elements.trashBanner) elements.trashBanner.classList.toggle('is-hidden', !isTrash);
        if (elements.btnNewDropdown) elements.btnNewDropdown.disabled = isTrash;
        if (elements.btnQuickTrash) elements.btnQuickTrash.classList.toggle('is-active', isTrash);

        // 2. Breadcrumbs
        renderBreadcrumbs(data.breadcrumbs || []);

        // Series/Season creation belongs to metadata workflows, not file organization.
        if (elements.menuItemNewSeries) elements.menuItemNewSeries.classList.add('is-hidden');
        if (elements.menuItemNewSeason) elements.menuItemNewSeason.classList.add('is-hidden');

        // 4. Items List & Sorting
        let rawItems = [];
        if (isTrash) {
            const trashItems = Array.isArray(data.trash_items) ? data.trash_items : [];
            const scopedItems = Array.isArray(data.items) ? data.items : [];
            rawItems = (trashItems.length ? trashItems : scopedItems).slice();
        } else if (Array.isArray(data.items) && data.items.length > 0) {
            rawItems = data.items.slice();
        } else {
            rawItems = [
                ...(data.folders || []).map(f => ({ ...f, type: 'folder', type_label: 'مجلد' })),
                ...(data.series || []).map(s => ({ ...s, type: 'series', type_label: 'مسلسل' })),
                ...(data.seasons || []).map(sn => ({ ...sn, type: 'season', type_label: 'موسم' })),
                ...(data.videos || []).map(v => ({ ...v, type: 'video' })),
                ...(data.trash_items || []),
            ];
        }
        let items = rawItems.map(it => normalizeItem(it)).filter(Boolean);
        if (!isTrash) items = sortItems(items, state.sortKey, state.sortAsc);
        const totalItems = isTrash ? (data.total_items ?? items.length) : items.length;
        const pageCount = Math.max(1, isTrash ? (data.pages || 1) : Math.ceil(items.length / state.pageSize));
        state.page = Math.min(state.page, pageCount);
        const pageStart = (state.page - 1) * state.pageSize;
        state.visibleItems = isTrash ? items : items.slice(pageStart, pageStart + state.pageSize);
        if (elements.pagination) elements.pagination.hidden = totalItems <= state.pageSize;
        if (elements.pageInfo) {
            const first = items.length ? pageStart + 1 : 0;
            const last = Math.min(pageStart + state.pageSize, totalItems);
            elements.pageInfo.textContent = `عرض ${first}–${last} من ${totalItems.toLocaleString('ar')}`;
        }
        if (elements.prevPage) elements.prevPage.disabled = state.page <= 1;
        if (elements.nextPage) elements.nextPage.disabled = state.page >= pageCount;

        // 5. Update Badge
        elements.itemCountBadge.textContent = `${totalItems} عنصر`;

        // 6. Populate Table
        elements.tableBody.innerHTML = '';
        syncSelectAllCheckboxes();
        if (items.length === 0) {
            const filtered = !isTrash && (state.contentFilter !== 'all' || state.searchQuery);
            const tr = document.createElement('tr');
            tr.innerHTML = `
                <td colspan="10">
                    <div class="empty-state">
                        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
                        <h4>${isTrash ? 'سلة المحذوفات فارغة' : filtered ? 'لا توجد نتائج ضمن الفلتر الحالي' : 'هذا المجلد فارغ'}</h4>
                        <p>${isTrash ? 'لا توجد عناصر محذوفة حالياً.' : filtered ? 'قد توجد عناصر مستبعدة بسبب نوع المحتوى أو البحث.' : 'قم برفع ملف فيديو أو إنشاء مجلد جديد لبدء التنظيم.'}</p>
                        ${filtered ? '<button type="button" class="btn btn-secondary" id="showAllEmptyResults">إظهار الكل وإلغاء البحث</button>' : ''}
                    </div>
                </td>
            `;
            elements.tableBody.appendChild(tr);
            tr.querySelector('#showAllEmptyResults')?.addEventListener('click', () => {
                state.searchQuery = '';
                if (elements.searchInput) elements.searchInput.value = '';
                setFilterScope('all', true);
            });
            updateMultiActionBar();
            return;
        }

        state.visibleItems.forEach(item => {
            const tr = document.createElement('tr');
            tr.className = 'drive-row';
            tr.dataset.id = item.id;
            tr.dataset.type = item.type;
            if (state.selectedItems.has(selectionKey(item))) {
                tr.classList.add('is-selected');
            }

            // Drag and Drop (only when not in trash)
            if (!isTrash) {
                tr.draggable = true;
                tr.addEventListener('dragstart', (e) => handleDragStart(e, item));
                if (['folder', 'series', 'season'].includes(item.type)) {
                    tr.addEventListener('dragover', handleDragOver);
                    tr.addEventListener('dragleave', handleDragLeave);
                    tr.addEventListener('drop', (e) => handleDrop(e, item));
                }
            }

            // Click row to navigate (folders, series, seasons) or open details
            tr.addEventListener('click', (e) => {
                if (e.target.closest('.drive-checkbox') || e.target.closest('.action-dots-btn') || e.target.closest('.dropdown-menu')) {
                    return;
                }
                if (isTrash) {
                    toggleSelectItem(item, tr);
                    return;
                }

                const isTitleClick = !!e.target.closest('.name-primary');
                const isIconClick = !!e.target.closest('.name-icon-wrap');

                if (item.type === 'video' && item.location_path && (isTitleClick || isIconClick)) {
                    state.searchQuery = '';
                    if (elements.searchInput) elements.searchInput.value = '';
                    loadScope(item.folder_id || 'root', item.folder_id ? 'folder' : 'root');
                    return;
                }

                if (item.type === 'video' && (isTitleClick || isIconClick)) {
                    window.location.href = `/watch/${item.id}`;
                    return;
                }

                if (['folder', 'series', 'season'].includes(item.type)) {
                    loadScope(item.id, item.type);
                    return;
                }

                // Single click row: select item and open details drawer
                selectSingleRow(item, tr);
                openDetailsDrawer(item);
            });

            // Secondary subtitle calculation
            let subtitleText = '';
            if (isTrash) {
                subtitleText = `المكان السابق: ${item.previous_location || 'مكتبة المحتوى'}`;
            } else if (item.location_path) {
                subtitleText = item.location_path;
            } else if (item.type === 'folder') {
                subtitleText = item.items_count != null ? `مجلد · ${item.items_count} عنصر` : 'مجلد';
            } else if (item.type === 'series') {
                subtitleText = `${item.seasons_count || 0} مواسم · ${item.episodes_count || 0} حلقة`;
            } else if (item.type === 'season') {
                subtitleText = `${item.episodes_count || 0} حلقة · ${item.size_formatted || '—'}`;
            } else {
                if (item.series_name) {
                    const s = item.season_number ? `S${String(item.season_number).padStart(2, '0')}` : '';
                    const ep = item.episode_number ? `E${String(item.episode_number).padStart(2, '0')}` : '';
                    subtitleText = `${item.series_name} · ${s}${ep}`;
                } else {
                    subtitleText = item.filename || 'فيديو';
                }
            }

            // Type Label
            const typeLabel = item.type_label || item.type;

            // Policy badge
            let policyHtml = '—';
            if (!isTrash && item.allow_download_policy) {
                const pol = item.allow_download_policy.toLowerCase();
                const polText = pol === 'allow' ? 'مسموح' : pol === 'deny' ? 'معطل' : 'موروث';
                policyHtml = `<span class="policy-badge policy-${pol}">${polText}</span>`;
            }

            // Quality badge
            let qualityHtml = '—';
            if (item.quality) {
                qualityHtml = `<span class="quality-badge quality-${item.quality.toLowerCase()}">${escapeHtml(item.quality)}</span>`;
            }

            // Subtitle badge
            let subHtml = '—';
            if (item.has_subtitles) {
                subHtml = `<span class="quality-badge">${escapeHtml(item.subtitles_count || 'نعم')}</span>`;
            }

            // Checkbox
            const isChecked = state.selectedItems.has(selectionKey(item));
            const gridPosterUrl = item.type === 'video' ? getAdminPosterUrl(item.id) : '';
            const gridPosterMarkup = item.type === 'video'
                ? `<div class="drive-grid-poster" aria-hidden="true">${gridPosterUrl ? `<img src="${escapeHtml(gridPosterUrl)}" alt="" loading="lazy" decoding="async">` : ''}<span class="drive-grid-poster-fallback">${getEntitySvg('video')}</span></div>`
                : '';

            tr.innerHTML = `
                <td class="col-check">
                    <input type="checkbox" class="drive-checkbox" ${isChecked ? 'checked' : ''} aria-label="تحديد ${escapeHtml(item.name)}" />
                </td>
                <td class="col-name">
                    <div class="name-cell-wrap">
                        ${gridPosterMarkup}
                        <div class="name-icon-wrap icon-${item.type}">
                            ${getEntitySvg(item.type)}
                        </div>
                        <div class="name-text-group">
                            <bdi class="name-primary" dir="auto" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</bdi>
                            <span class="name-subtitle">${escapeHtml(subtitleText)}</span>
                        </div>
                    </div>
                </td>
                <td class="col-type"><span class="type-badge type-${item.type}">${escapeHtml(typeLabel)}</span></td>
                <td class="col-quality">${qualityHtml}</td>
                <td class="col-subtitles">${subHtml}</td>
                <td class="col-policy">${policyHtml}</td>
                <td class="col-size">${escapeHtml(item.size_formatted || '—')}</td>
                <td class="col-duration">${escapeHtml(item.duration_formatted || '—')}</td>
                <td class="col-date">${escapeHtml(isTrash ? (item.trashed_at || '—') : (item.created_at || '—'))}</td>
                <td class="col-actions">
                    <button type="button" class="action-dots-btn" aria-label="خيارات">⋯</button>
                </td>
            `;

            const gridPoster = tr.querySelector('.drive-grid-poster img');
            if (gridPoster) {
                gridPoster.addEventListener('error', () => gridPoster.closest('.drive-grid-poster')?.classList.add('has-fallback'), { once: true });
            }

            // Checkbox handler
            const checkbox = tr.querySelector('.drive-checkbox');
            checkbox.addEventListener('change', () => {
                toggleSelectItem(item, tr);
            });

            // Action Dots Handler
            const dotsBtn = tr.querySelector('.action-dots-btn');
            dotsBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                openContextMenu(e, item, dotsBtn);
            });

            // Row Right Click Context Menu Handler
            tr.addEventListener('contextmenu', (e) => {
                e.preventDefault();
                e.stopPropagation();
                const key = selectionKey(item);
                if (!state.selectedItems.has(key)) {
                    selectSingleRow(item, tr);
                }
                openUnifiedContextMenu(e, item);
            });

            elements.tableBody.appendChild(tr);
        });
        updateMultiActionBar();
    }

    function sortItems(items, key, asc) {
        return items.sort((a, b) => {
            // Folders and series always come first if not in trash
            if (state.contentFilter !== 'trash') {
                const isContainerA = ['folder', 'series', 'season'].includes(a.type);
                const isContainerB = ['folder', 'series', 'season'].includes(b.type);
                if (isContainerA && !isContainerB) return -1;
                if (!isContainerA && isContainerB) return 1;
            }

            let valA = a[key] ?? '';
            let valB = b[key] ?? '';

            if (key === 'size') {
                valA = a.size_bytes || 0;
                valB = b.size_bytes || 0;
            }

            if (typeof valA === 'string') {
                return asc ? valA.localeCompare(valB, 'ar') : valB.localeCompare(valA, 'ar');
            }
            return asc ? (valA > valB ? 1 : -1) : (valA < valB ? 1 : -1);
        });
    }

    // =========================================================================
    // SELECTION & MULTI-ACTION BAR
    // =========================================================================

    function toggleSelectItem(item, rowEl) {
        const key = selectionKey(item);
        if (state.selectedItems.has(key)) {
            state.selectedItems.delete(key);
            if (rowEl) {
                rowEl.classList.remove('is-selected');
                rowEl.querySelector('.drive-checkbox').checked = false;
            }
        } else {
            state.selectedItems.set(key, item);
            if (rowEl) {
                rowEl.classList.add('is-selected');
                rowEl.querySelector('.drive-checkbox').checked = true;
            }
        }
        updateMultiActionBar();
    }

    function selectSingleRow(item, rowEl) {
        state.selectedItems.clear();
        document.querySelectorAll('.drive-row.is-selected').forEach(r => {
            r.classList.remove('is-selected');
            const cb = r.querySelector('.drive-checkbox');
            if (cb) cb.checked = false;
        });
        const key = selectionKey(item);
        state.selectedItems.set(key, item);
        if (rowEl) {
            rowEl.classList.add('is-selected');
            const cb = rowEl.querySelector('.drive-checkbox');
            if (cb) cb.checked = true;
        }
        updateMultiActionBar();
    }

    function clearSelection() {
        state.selectedItems.clear();
        document.querySelectorAll('.drive-row.is-selected').forEach(r => r.classList.remove('is-selected'));
        document.querySelectorAll('.drive-row .drive-checkbox:checked').forEach(c => c.checked = false);
        updateMultiActionBar();
    }

    function syncSelectAllCheckboxes() {
        const total = state.visibleItems.length;
        const selected = state.visibleItems.filter(item => state.selectedItems.has(selectionKey(item))).length;
        for (const checkbox of [elements.selectAllCheckbox, elements.mobileSelectAllCheckbox]) {
            checkbox.checked = total > 0 && selected === total;
            checkbox.indeterminate = selected > 0 && selected < total;
            checkbox.disabled = total === 0;
        }
    }

    function selectAllVisible(checked) {
        for (const item of state.visibleItems) {
            if (checked) state.selectedItems.set(selectionKey(item), item);
            else state.selectedItems.delete(selectionKey(item));
        }
        elements.tableBody.querySelectorAll('.drive-row').forEach(row => {
            row.classList.toggle('is-selected', checked);
            row.querySelector('.drive-checkbox').checked = checked;
        });
        updateMultiActionBar();
    }

    function updateMultiActionBar() {
        const count = state.selectedItems.size;
        const isTrash = (state.contentFilter === 'trash');
        syncSelectAllCheckboxes();

        if (count > 0) {
            elements.selectedCountText.textContent = `${count} عنصر محدد`;
            elements.multiActionBar.classList.add('is-visible');

            if (isTrash) {
                elements.btnMultiMove.classList.add('is-hidden');
                elements.btnMultiShare.classList.add('is-hidden');
                elements.btnMultiTrash.classList.add('is-hidden');
                elements.btnMultiRestore.classList.remove('is-hidden');
                elements.btnMultiPermanentDelete.classList.remove('is-hidden');
            } else {
                elements.btnMultiMove.classList.remove('is-hidden');
                elements.btnMultiShare.classList.remove('is-hidden');
                elements.btnMultiTrash.classList.remove('is-hidden');
                elements.btnMultiRestore.classList.add('is-hidden');
                elements.btnMultiPermanentDelete.classList.add('is-hidden');
            }
        } else {
            elements.multiActionBar.classList.remove('is-visible');
        }
    }

    // =========================================================================
    // CONTEXT MENUS & CLIPBOARD (EMPTY-AREA, ITEM, MULTI-SELECT)
    // =========================================================================

    function closeAllContextMenus() {
        let wasOpen = false;
        if (elements.emptyContextMenu && elements.emptyContextMenu.classList.contains('is-open')) {
            elements.emptyContextMenu.classList.remove('is-open');
            wasOpen = true;
        }
        if (elements.unifiedContextMenu && elements.unifiedContextMenu.classList.contains('is-open')) {
            elements.unifiedContextMenu.classList.remove('is-open');
            wasOpen = true;
        }
        document.querySelectorAll('.menu-item-has-submenu.flip-submenu').forEach(el => el.classList.remove('flip-submenu'));
        return wasOpen;
    }

    function closeContextMenu() {
        return closeAllContextMenus();
    }

    function closeActiveModal() {
        const openModal = document.querySelector('.modal-overlay.is-open');
        if (openModal) {
            openModal.classList.remove('is-open');
            return true;
        }
        return false;
    }

    function positionMenuWithinContent(menuEl, clientX, clientY, alignRight = false) {
        const content = menuEl.closest('.drive-content');
        if (!content) return;
        const bounds = content.getBoundingClientRect();
        menuEl.classList.add('is-open');
        const menuWidth = menuEl.offsetWidth || 230;
        const menuHeight = menuEl.offsetHeight || 260;
        const inset = 8;
        const maxLeft = Math.max(inset, bounds.width - menuWidth - inset);
        const maxTop = Math.max(inset, bounds.height - menuHeight - inset);
        let posX = clientX - bounds.left - (alignRight ? menuWidth : 0);
        let posY = clientY - bounds.top;

        posX = Math.min(Math.max(inset, posX), maxLeft);
        posY = Math.min(Math.max(inset, posY), maxTop);

        menuEl.style.top = `${posY}px`;
        menuEl.style.left = `${posX}px`;
        menuEl.style.right = 'auto';
    }

    function positionMenuAtCoordinates(menuEl, clientX, clientY) {
        positionMenuWithinContent(menuEl, clientX, clientY);
    }

    function updateEmptyContextMenuRules() {
        const isTrash = (state.contentFilter === 'trash');
        const scopeType = state.currentScopeType || 'root';

        // 1. New Submenu Rules
        if (isTrash) {
            if (elements.emptyCtxNew) elements.emptyCtxNew.classList.add('is-disabled');
        } else {
            if (elements.emptyCtxNew) elements.emptyCtxNew.classList.remove('is-disabled');

            if (scopeType === 'root' || scopeType === 'folder') {
                if (elements.emptyCtxNewFolder) elements.emptyCtxNewFolder.classList.remove('is-hidden');
                if (elements.emptyCtxNewSeries) elements.emptyCtxNewSeries.classList.add('is-hidden');
                if (elements.emptyCtxNewSeason) elements.emptyCtxNewSeason.classList.add('is-hidden');
                if (elements.emptyCtxNewDisabled) elements.emptyCtxNewDisabled.classList.add('is-hidden');
            } else if (scopeType === 'series') {
                if (elements.emptyCtxNewFolder) elements.emptyCtxNewFolder.classList.add('is-hidden');
                if (elements.emptyCtxNewSeries) elements.emptyCtxNewSeries.classList.add('is-hidden');
                if (elements.emptyCtxNewSeason) elements.emptyCtxNewSeason.classList.remove('is-hidden');
                if (elements.emptyCtxNewDisabled) elements.emptyCtxNewDisabled.classList.add('is-hidden');
            } else if (scopeType === 'season') {
                if (elements.emptyCtxNewFolder) elements.emptyCtxNewFolder.classList.add('is-hidden');
                if (elements.emptyCtxNewSeries) elements.emptyCtxNewSeries.classList.add('is-hidden');
                if (elements.emptyCtxNewSeason) elements.emptyCtxNewSeason.classList.add('is-hidden');
                if (elements.emptyCtxNewDisabled) elements.emptyCtxNewDisabled.classList.remove('is-hidden');
            }
        }

        // 2. Paste Button Rule
        if (elements.emptyCtxPaste) {
            const hasClipboard = Boolean(state.clipboard && state.clipboard.items && state.clipboard.items.length > 0);
            if (hasClipboard && !isTrash) {
                elements.emptyCtxPaste.classList.remove('is-disabled');
                elements.emptyCtxPaste.removeAttribute('aria-disabled');
            } else {
                elements.emptyCtxPaste.classList.add('is-disabled');
                elements.emptyCtxPaste.setAttribute('aria-disabled', 'true');
            }
        }

        // 3. Sort Submenu Checks
        const sortKeys = ['name', 'date', 'size', 'type'];
        const sortElements = {
            name: elements.sortByName,
            date: elements.sortByDate,
            size: elements.sortBySize,
            type: elements.sortByType,
        };
        sortKeys.forEach(k => {
            const el = sortElements[k];
            if (!el) return;
            const isMatch = (state.sortKey === k);
            el.classList.toggle('is-checked', isMatch);
            el.setAttribute('aria-checked', isMatch ? 'true' : 'false');
            const check = el.querySelector('.menu-check');
            if (check) check.textContent = isMatch ? '✓' : '';
        });

        if (elements.sortDirAsc) {
            elements.sortDirAsc.classList.toggle('is-checked', state.sortAsc);
            elements.sortDirAsc.setAttribute('aria-checked', state.sortAsc ? 'true' : 'false');
            const check = elements.sortDirAsc.querySelector('.menu-check');
            if (check) check.textContent = state.sortAsc ? '✓' : '';
        }
        if (elements.sortDirDesc) {
            elements.sortDirDesc.classList.toggle('is-checked', !state.sortAsc);
            elements.sortDirDesc.setAttribute('aria-checked', !state.sortAsc ? 'true' : 'false');
            const check = elements.sortDirDesc.querySelector('.menu-check');
            if (check) check.textContent = !state.sortAsc ? '✓' : '';
        }

        // 4. View Submenu Checks
        const isGrid = (state.viewMode === 'grid');
        if (elements.viewModeList) {
            elements.viewModeList.classList.toggle('is-checked', !isGrid);
            elements.viewModeList.setAttribute('aria-checked', !isGrid ? 'true' : 'false');
            const check = elements.viewModeList.querySelector('.menu-check');
            if (check) check.textContent = !isGrid ? '✓' : '';
        }
        if (elements.viewModeGrid) {
            elements.viewModeGrid.classList.toggle('is-checked', isGrid);
            elements.viewModeGrid.setAttribute('aria-checked', isGrid ? 'true' : 'false');
            const check = elements.viewModeGrid.querySelector('.menu-check');
            if (check) check.textContent = isGrid ? '✓' : '';
        }
    }

    function openEmptyAreaContextMenu(e) {
        closeAllContextMenus();
        updateEmptyContextMenuRules();
        positionMenuAtCoordinates(elements.emptyContextMenu, e.clientX, e.clientY);
    }

    function openUnifiedContextMenu(e, rawItem, triggerEl) {
        closeAllContextMenus();
        const item = normalizeItem(rawItem);
        state.contextTarget = item;
        const isTrash = (state.contentFilter === 'trash');
        const isMulti = (state.selectedItems.size > 1);

        // Configure buttons
        if (isMulti) {
            // Hide single-item buttons
            if (elements.ctxMenuOpen) elements.ctxMenuOpen.classList.add('is-hidden');
            if (elements.ctxMenuPlay) elements.ctxMenuPlay.classList.add('is-hidden');
            if (elements.ctxMenuWatchPage) elements.ctxMenuWatchPage.classList.add('is-hidden');
            if (elements.ctxMenuCopyWatchLink) elements.ctxMenuCopyWatchLink.classList.add('is-hidden');
            if (elements.ctxMenuDividerMedia) elements.ctxMenuDividerMedia.classList.add('is-hidden');
            if (elements.ctxMenuRename) elements.ctxMenuRename.classList.add('is-hidden');
            if (elements.ctxMenuMove) elements.ctxMenuMove.classList.add('is-hidden');
            if (elements.ctxMenuCopy) elements.ctxMenuCopy.classList.add('is-hidden');
            if (elements.ctxMenuCut) elements.ctxMenuCut.classList.add('is-hidden');
            if (elements.ctxMenuShare) elements.ctxMenuShare.classList.add('is-hidden');
            if (elements.ctxMenuSubtitles) elements.ctxMenuSubtitles.classList.add('is-hidden');
            if (elements.ctxMenuDetails) elements.ctxMenuDetails.classList.add('is-hidden');
            if (elements.ctxMenuTrash) elements.ctxMenuTrash.classList.add('is-hidden');

            if (isTrash) {
                if (elements.ctxMenuMultiMove) elements.ctxMenuMultiMove.classList.add('is-hidden');
                if (elements.ctxMenuMultiCopy) elements.ctxMenuMultiCopy.classList.add('is-hidden');
                if (elements.ctxMenuMultiCut) elements.ctxMenuMultiCut.classList.add('is-hidden');
                if (elements.ctxMenuMultiShare) elements.ctxMenuMultiShare.classList.add('is-hidden');
                if (elements.ctxMenuMultiTrash) elements.ctxMenuMultiTrash.classList.add('is-hidden');
                if (elements.ctxMenuRestore) elements.ctxMenuRestore.classList.remove('is-hidden');
                if (elements.ctxMenuPermanentDelete) elements.ctxMenuPermanentDelete.classList.remove('is-hidden');
            } else {
                if (elements.ctxMenuMultiMove) elements.ctxMenuMultiMove.classList.remove('is-hidden');
                if (elements.ctxMenuMultiCopy) elements.ctxMenuMultiCopy.classList.add('is-hidden');
                if (elements.ctxMenuMultiCut) elements.ctxMenuMultiCut.classList.remove('is-hidden');
                if (elements.ctxMenuMultiShare) elements.ctxMenuMultiShare.classList.remove('is-hidden');
                if (elements.ctxMenuMultiTrash) elements.ctxMenuMultiTrash.classList.remove('is-hidden');
                if (elements.ctxMenuRestore) elements.ctxMenuRestore.classList.add('is-hidden');
                if (elements.ctxMenuPermanentDelete) elements.ctxMenuPermanentDelete.classList.add('is-hidden');
            }
            if (elements.ctxMenuMultiDeselect) elements.ctxMenuMultiDeselect.classList.remove('is-hidden');
            if (elements.ctxMenuDividerTrash) elements.ctxMenuDividerTrash.classList.remove('is-hidden');
        } else {
            // Hide multi-item buttons
            if (elements.ctxMenuMultiMove) elements.ctxMenuMultiMove.classList.add('is-hidden');
            if (elements.ctxMenuMultiCopy) elements.ctxMenuMultiCopy.classList.add('is-hidden');
            if (elements.ctxMenuMultiCut) elements.ctxMenuMultiCut.classList.add('is-hidden');
            if (elements.ctxMenuMultiShare) elements.ctxMenuMultiShare.classList.add('is-hidden');
            if (elements.ctxMenuMultiTrash) elements.ctxMenuMultiTrash.classList.add('is-hidden');
            if (elements.ctxMenuMultiDeselect) elements.ctxMenuMultiDeselect.classList.add('is-hidden');

            if (isTrash) {
                if (elements.ctxMenuOpen) elements.ctxMenuOpen.classList.add('is-hidden');
                if (elements.ctxMenuPlay) elements.ctxMenuPlay.classList.add('is-hidden');
                if (elements.ctxMenuWatchPage) elements.ctxMenuWatchPage.classList.add('is-hidden');
                if (elements.ctxMenuCopyWatchLink) elements.ctxMenuCopyWatchLink.classList.add('is-hidden');
                if (elements.ctxMenuDividerMedia) elements.ctxMenuDividerMedia.classList.add('is-hidden');
                if (elements.ctxMenuRename) elements.ctxMenuRename.classList.add('is-hidden');
                if (elements.ctxMenuMove) elements.ctxMenuMove.classList.add('is-hidden');
                if (elements.ctxMenuCopy) elements.ctxMenuCopy.classList.add('is-hidden');
                if (elements.ctxMenuCut) elements.ctxMenuCut.classList.add('is-hidden');
                if (elements.ctxMenuShare) elements.ctxMenuShare.classList.add('is-hidden');
                if (elements.ctxMenuSubtitles) elements.ctxMenuSubtitles.classList.add('is-hidden');
                if (elements.ctxMenuDetails) elements.ctxMenuDetails.classList.add('is-hidden');
                if (elements.ctxMenuTrash) elements.ctxMenuTrash.classList.add('is-hidden');
                if (elements.ctxMenuRestore) elements.ctxMenuRestore.classList.remove('is-hidden');
                if (elements.ctxMenuPermanentDelete) elements.ctxMenuPermanentDelete.classList.remove('is-hidden');
                if (elements.ctxMenuDividerTrash) elements.ctxMenuDividerTrash.classList.remove('is-hidden');
            } else {
                if (elements.ctxMenuRestore) elements.ctxMenuRestore.classList.add('is-hidden');
                if (elements.ctxMenuPermanentDelete) elements.ctxMenuPermanentDelete.classList.add('is-hidden');
                if (elements.ctxMenuDetails) elements.ctxMenuDetails.classList.remove('is-hidden');
                if (elements.ctxMenuRename) elements.ctxMenuRename.classList.remove('is-hidden');
                if (elements.ctxMenuTrash) elements.ctxMenuTrash.classList.remove('is-hidden');
                if (elements.ctxMenuDividerTrash) elements.ctxMenuDividerTrash.classList.remove('is-hidden');

                if (item.type === 'video') {
                    if (elements.ctxMenuOpen) elements.ctxMenuOpen.classList.add('is-hidden');
                    if (elements.ctxMenuPlay) elements.ctxMenuPlay.classList.remove('is-hidden');
                    if (elements.ctxMenuWatchPage) elements.ctxMenuWatchPage.classList.remove('is-hidden');
                    if (elements.ctxMenuCopyWatchLink) elements.ctxMenuCopyWatchLink.classList.remove('is-hidden');
                    if (elements.ctxMenuDividerMedia) elements.ctxMenuDividerMedia.classList.remove('is-hidden');
                    if (elements.ctxMenuMove) elements.ctxMenuMove.classList.remove('is-hidden');
                    if (elements.ctxMenuCopy) elements.ctxMenuCopy.classList.add('is-hidden');
                    if (elements.ctxMenuCut) elements.ctxMenuCut.classList.remove('is-hidden');
                    if (elements.ctxMenuShare) elements.ctxMenuShare.classList.remove('is-hidden');
                    if (elements.ctxMenuSubtitles) elements.ctxMenuSubtitles.classList.remove('is-hidden');
                } else if (item.type === 'folder') {
                    if (elements.ctxMenuOpen) elements.ctxMenuOpen.classList.remove('is-hidden');
                    if (elements.ctxMenuPlay) elements.ctxMenuPlay.classList.add('is-hidden');
                    if (elements.ctxMenuWatchPage) elements.ctxMenuWatchPage.classList.add('is-hidden');
                    if (elements.ctxMenuCopyWatchLink) elements.ctxMenuCopyWatchLink.classList.add('is-hidden');
                    if (elements.ctxMenuDividerMedia) elements.ctxMenuDividerMedia.classList.remove('is-hidden');
                    if (elements.ctxMenuMove) elements.ctxMenuMove.classList.remove('is-hidden');
                    if (elements.ctxMenuCopy) elements.ctxMenuCopy.classList.add('is-hidden');
                    if (elements.ctxMenuCut) elements.ctxMenuCut.classList.remove('is-hidden');
                    if (elements.ctxMenuShare) elements.ctxMenuShare.classList.remove('is-hidden');
                    if (elements.ctxMenuSubtitles) elements.ctxMenuSubtitles.classList.add('is-hidden');
                } else if (item.type === 'series') {
                    if (elements.ctxMenuOpen) elements.ctxMenuOpen.classList.remove('is-hidden');
                    if (elements.ctxMenuPlay) elements.ctxMenuPlay.classList.add('is-hidden');
                    if (elements.ctxMenuWatchPage) elements.ctxMenuWatchPage.classList.add('is-hidden');
                    if (elements.ctxMenuCopyWatchLink) elements.ctxMenuCopyWatchLink.classList.add('is-hidden');
                    if (elements.ctxMenuDividerMedia) elements.ctxMenuDividerMedia.classList.remove('is-hidden');
                    if (elements.ctxMenuMove) elements.ctxMenuMove.classList.remove('is-hidden');
                    if (elements.ctxMenuCopy) elements.ctxMenuCopy.classList.add('is-hidden');
                    if (elements.ctxMenuCut) elements.ctxMenuCut.classList.add('is-hidden');
                    if (elements.ctxMenuShare) elements.ctxMenuShare.classList.add('is-hidden');
                    if (elements.ctxMenuSubtitles) elements.ctxMenuSubtitles.classList.add('is-hidden');
                } else if (item.type === 'season') {
                    if (elements.ctxMenuOpen) elements.ctxMenuOpen.classList.remove('is-hidden');
                    if (elements.ctxMenuPlay) elements.ctxMenuPlay.classList.add('is-hidden');
                    if (elements.ctxMenuWatchPage) elements.ctxMenuWatchPage.classList.add('is-hidden');
                    if (elements.ctxMenuCopyWatchLink) elements.ctxMenuCopyWatchLink.classList.add('is-hidden');
                    if (elements.ctxMenuDividerMedia) elements.ctxMenuDividerMedia.classList.remove('is-hidden');
                    if (elements.ctxMenuMove) elements.ctxMenuMove.classList.add('is-hidden');
                    if (elements.ctxMenuCopy) elements.ctxMenuCopy.classList.add('is-hidden');
                    if (elements.ctxMenuCut) elements.ctxMenuCut.classList.add('is-hidden');
                    if (elements.ctxMenuShare) elements.ctxMenuShare.classList.add('is-hidden');
                    if (elements.ctxMenuSubtitles) elements.ctxMenuSubtitles.classList.add('is-hidden');
                }
            }
        }

        // Positioning
        if (triggerEl) {
            const rect = triggerEl.getBoundingClientRect();
            elements.unifiedContextMenu.classList.add('is-open');
            let top = rect.bottom + 4;
            const menuHeight = elements.unifiedContextMenu.offsetHeight || 220;
            const contentRect = elements.unifiedContextMenu.closest('.drive-content')?.getBoundingClientRect();
            if (contentRect && top + menuHeight > contentRect.bottom - 8) {
                top = rect.top - menuHeight - 4;
            }
            positionMenuWithinContent(elements.unifiedContextMenu, rect.right, top, true);
        } else if (e) {
            positionMenuAtCoordinates(elements.unifiedContextMenu, e.clientX, e.clientY);
        }
    }

    function openContextMenu(e, rawItem, triggerEl) {
        openUnifiedContextMenu(e, rawItem, triggerEl);
    }

    // CLIPBOARD ACTIONS
    function copyItemToClipboard(item, action) {
        if (action === 'copy') {
            showToast('نسخ العناصر غير مدعوم حالياً؛ استخدم قص ثم لصق لنقل العنصر دون تكرار ملفات الوسائط.', 'warning');
            return;
        }
        state.clipboard = {
            action: action,
            items: [{ id: item.id, type: item.type, name: item.name }]
        };
        const label = action === 'cut' ? 'قص' : 'نسخ';
        showToast(`تم ${label} «${item.name}» إلى الحافظة`, 'info');
        updateEmptyContextMenuRules();
    }

    function copySelectedToClipboard(action) {
        if (action === 'copy') {
            showToast('نسخ العناصر غير مدعوم حالياً؛ استخدم قص ثم لصق لنقل العناصر دون تكرار ملفات الوسائط.', 'warning');
            return;
        }
        const items = Array.from(state.selectedItems.values());
        if (!items.length) return;
        state.clipboard = {
            action: action,
            items: items.map(x => ({ id: x.id, type: x.type, name: x.name }))
        };
        const label = action === 'cut' ? 'قص' : 'نسخ';
        showToast(`تم ${label} ${items.length} عنصر إلى الحافظة`, 'info');
        updateEmptyContextMenuRules();
    }

    async function executePaste() {
        if (!state.clipboard || !state.clipboard.items || !state.clipboard.items.length) {
            showToast('الحافظة فارغة', 'warning');
            return;
        }
        if (state.clipboard.action !== 'cut') {
            showToast('عمليات اللصق المتاحة حالياً هي نقل العناصر المقصوصة فقط.', 'warning');
            return;
        }
        const destinationId = state.currentScopeId === 'root' ? null : state.currentScopeId;
        const destinationType = state.currentScopeType || 'folder';

        // Cycle check: destination cannot be in clipboard items
        if (destinationId && state.clipboard.items.some(x => x.id === destinationId)) {
            showToast('لا يمكن نقل العنصر إلى داخل نفسه', 'error');
            return;
        }

        try {
            await moveItems(state.clipboard.items, destinationId, destinationType);
            const count = state.clipboard.items.length;
            const action = state.clipboard.action;
            if (action === 'cut') {
                state.clipboard = null;
            }
            showToast(`تم لصق ${count} عنصر بنجاح`, 'success');
            updateEmptyContextMenuRules();
        } catch (err) {
            console.error('Paste error:', err);
        }
    }

    // VIEW MODE TOGGLING
    function setViewMode(mode) {
        state.viewMode = mode;
        if (elements.tableContainer) {
            elements.tableContainer.classList.toggle('is-grid-view', mode === 'grid');
        }
        if (elements.btnViewList) elements.btnViewList.classList.toggle('is-active', mode === 'list');
        if (elements.btnViewGrid) elements.btnViewGrid.classList.toggle('is-active', mode === 'grid');
        updateEmptyContextMenuRules();
    }

    // =========================================================================
    // SOFT-DELETE & TRASH RESTORE / PERMANENT DELETE (NO confirm())
    // =========================================================================

    async function promptTrashConfirmation(items) {
        if (!items.length || state.trashBusy) return;
        state.pendingDeleteItems = items;
        elements.deleteModalWarningBox.classList.add('is-hidden');
        elements.deleteModalWarningList.innerHTML = '';

        if (items.length === 1) {
            const item = items[0];
            elements.deleteModalTitle.textContent = 'نقل إلى سلة المحذوفات';
            elements.deleteModalDesc.textContent = `هل أنت متأكد من رغبتك في نقل ${item.name} إلى سلة المحذوفات؟`;

            // If folder or series, check for descendants
            if (['folder', 'series'].includes(item.type)) {
                try {
                    const res = await apiRequest(`/api/v1/drive/descendant-summary/${item.type}/${item.id}`);
                    const summary = res.summary;
                    if (summary && (summary.series_count > 0 || summary.seasons_count > 0 || summary.videos_count > 0)) {
                        elements.deleteModalWarningBox.classList.remove('is-hidden');
                        if (summary.series_count > 0) {
                            const li = document.createElement('li');
                            li.textContent = `• ${summary.series_count} مسلسلات`;
                            elements.deleteModalWarningList.appendChild(li);
                        }
                        if (summary.seasons_count > 0) {
                            const li = document.createElement('li');
                            li.textContent = `• ${summary.seasons_count} مواسم`;
                            elements.deleteModalWarningList.appendChild(li);
                        }
                        if (summary.videos_count > 0) {
                            const li = document.createElement('li');
                            li.textContent = `• ${summary.videos_count} فيديوهات وحلقات`;
                            elements.deleteModalWarningList.appendChild(li);
                        }
                        if (summary.folders_count > 0) {
                            const li = document.createElement('li');
                            li.textContent = `• ${summary.folders_count} مجلدات فرعية`;
                            elements.deleteModalWarningList.appendChild(li);
                        }
                    }
                } catch (e) {
                    console.error('Failed to get descendant summary', e);
                }
            }
        } else {
            elements.deleteModalTitle.textContent = 'نقل العناصر المحددة إلى سلة المحذوفات';
            elements.deleteModalDesc.textContent = `نقل ${items.length} عنصراً إلى سلة المحذوفات؟`;
        }

        elements.deleteModal.classList.add('is-open');
    }

    async function executeTrashPending() {
        if (!state.pendingDeleteItems.length || state.trashBusy) return;
        const requested = state.pendingDeleteItems.slice();
        const payloadItems = requested.map(it => ({ id: it.id, type: it.type }));
        state.trashBusy = true;
        elements.btnConfirmDelete.disabled = true;
        let posted = false;

        try {
            const res = await apiRequest('/api/v1/drive/trash', {
                method: 'POST',
                body: { items: payloadItems },
                silentError: true,
            });
            if (res.success !== true || !Number.isInteger(res.count) || res.count < 1) {
                throw new Error('Invalid Trash response');
            }
            posted = true;
            // The API count includes descendants. Re-read the authoritative visible roots
            // before claiming that the selected rows were actually removed.
            const data = await apiRequest(scopeUrl());
            if (!Array.isArray(data.items)) throw new Error('Invalid refreshed library response');
            const stillVisible = new Set(data.items.map(selectionKey));
            const failed = requested.filter(item => stillVisible.has(selectionKey(item)));
            state.selectedItems = new Map(failed.map(item => [selectionKey(item), item]));
            state.currentData = data;
            elements.deleteModal.classList.remove('is-open');
            state.pendingDeleteItems = [];
            renderExplorer(data);
            await loadTreeData();
            const moved = requested.length - failed.length;
            if (failed.length) {
                console.error('Trash did not remove all selected roots', { requested: payloadItems, failed: failed.map(item => ({ id: item.id, type: item.type })), response: res });
                showToast(`تم نقل ${moved} عنصراً إلى السلة، وتعذر نقل ${failed.length} عناصر.`, 'error');
            } else {
                showToast(`تم نقل ${moved} عنصراً إلى سلة المحذوفات`, 'success');
            }
        } catch (err) {
            console.error('Trash error:', err);
            if (posted) {
                elements.deleteModal.classList.remove('is-open');
                state.pendingDeleteItems = [];
                showToast('تم نقل العناصر إلى السلة، لكن تعذر تحديث عرض المكتبة. حدّث الصفحة.', 'warning');
            } else {
                showToast('تعذر نقل العناصر إلى السلة. حدّث المكتبة قبل إعادة المحاولة.', 'error');
            }
        } finally {
            state.trashBusy = false;
            elements.btnConfirmDelete.disabled = false;
        }
    }

    async function collectAllCurrentTrashItems() {
        // Read every server page before any mutation or delete confirmation.
        // Preserve current search/filter scope; refuse a changing page set.
        const url = new URL(scopeUrl(), window.location.origin);
        url.searchParams.set('page', '1');
        url.searchParams.set('limit', '100');
        const first = await apiRequest(url.pathname + url.search);
        const total = first.total_items ?? first.items.length;
        const items = new Map();
        const addPage = data => {
            if ((data.total_items ?? data.items.length) !== total) throw new Error('تغيّرت السلة أثناء القراءة؛ حدّث الصفحة وأعد المحاولة.');
            for (const item of data.items) items.set(`${item.type}:${item.id}`, item);
        };
        addPage(first);
        for (let page = 2; page <= (first.pages || 1); page += 1) {
            url.searchParams.set('page', String(page));
            addPage(await apiRequest(url.pathname + url.search));
        }
        if (items.size !== total) throw new Error('تغيّرت السلة أثناء القراءة؛ حدّث الصفحة وأعد المحاولة.');
        return Array.from(items.values());
    }

    async function restoreItems(items) {
        try {
            const res = await apiRequest('/api/v1/drive/restore', {
                method: 'POST',
                body: { items },
            });
            showToast(`تمت استعادة ${res.count} عنصر إلى مكانه الأصلي بنجاح`, 'success');
            clearSelection();
            loadScope(state.currentScopeId, state.currentScopeType);
        } catch (err) {
            console.error('Restore error:', err);
        }
    }

    function promptPermanentDelete(items) {
        if (!items.length) return;
        state.pendingPermanentDeleteItems = items;
        elements.permanentDeleteModalDesc.textContent = `حذف ${items.length} عنصراً نهائياً؟ لا يمكن التراجع عن هذا الإجراء. ستُحذف السجلات وفق سياسة الخادم، دون حذف ملفات الوسائط من القرص.`;
        elements.permanentDeleteConfirmation.value = '';
        elements.btnConfirmPermanentDelete.disabled = true;
        elements.permanentDeleteModal.classList.add('is-open');
    }

    async function executePermanentDeletePending() {
        if (!state.pendingPermanentDeleteItems.length || elements.permanentDeleteConfirmation.value.trim() !== 'حذف') return;
        const payloadItems = state.pendingPermanentDeleteItems.map(it => ({ id: it.id, type: it.type }));
        elements.btnConfirmPermanentDelete.disabled = true;

        try {
            const res = await apiRequest('/api/v1/drive/trash/delete-permanent', {
                method: 'POST',
                body: { items: payloadItems },
            });
            if (res.success !== true || !Number.isInteger(res.count) || res.count < 1) {
                throw new Error('لم يؤكد الخادم حذف أي عنصر');
            }
            elements.permanentDeleteModal.classList.remove('is-open');
            state.pendingPermanentDeleteItems = [];
            showToast(`تم الحذف النهائي لـ ${res.count} عنصر بنجاح`, 'success');
            clearSelection();
            await loadScope('trash', 'trash');
        } catch (err) {
            console.error('Permanent delete error:', err);
            showToast(err.message || 'تعذر الحذف النهائي؛ بقيت العناصر في السلة.', 'error');
        } finally {
            elements.btnConfirmPermanentDelete.disabled = elements.permanentDeleteConfirmation.value.trim() !== 'حذف';
        }
    }

    // =========================================================================
    // DRAG AND DROP / MOVE
    // =========================================================================

    function handleDragStart(e, item) {
        let dragList = [item];
        if (state.selectedItems.has(selectionKey(item))) {
            dragList = Array.from(state.selectedItems.values());
        }
        e.dataTransfer.setData('application/json', JSON.stringify(dragList));
        e.dataTransfer.effectAllowed = 'move';
    }

    function handleDragOver(e) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        const row = e.currentTarget;
        row.classList.add('drag-over');
    }

    function handleDragLeave(e) {
        const row = e.currentTarget;
        row.classList.remove('drag-over');
    }

    async function handleDrop(e, targetItem) {
        e.preventDefault();
        const row = e.currentTarget;
        row.classList.remove('drag-over');

        try {
            const rawData = e.dataTransfer.getData('application/json');
            if (!rawData) return;
            const items = JSON.parse(rawData);
            await moveItems(items, targetItem.id, targetItem.type);
        } catch (err) {
            console.error('Drop error:', err);
        }
    }

    async function moveItems(items, targetId, targetType) {
        const movePayload = {
            items: items.map(x => ({ id: x.id, type: x.type })),
            target_id: targetId === 'root' ? null : targetId,
            target_type: targetType || 'folder',
        };

        try {
            const res = await apiRequest('/api/v1/drive/move', {
                method: 'POST',
                body: movePayload,
            });

            state.undoAction = {
                previousStates: res.previous_states,
                targetId: targetId,
            };

            showToast(`تم نقل ${res.moved_count} عنصر بنجاح`, 'success', {
                actionText: 'تراجع',
                onAction: executeUndoMove,
            });

            clearSelection();
            loadScope(state.currentScopeId, state.currentScopeType);
        } catch (err) {
            console.error('Move error:', err);
        }
    }

    async function executeUndoMove() {
        if (!state.undoAction || !state.undoAction.previousStates) return;
        const prevStates = state.undoAction.previousStates;
        state.undoAction = null;

        for (const item of prevStates) {
            try {
                await apiRequest('/api/v1/drive/move', {
                    method: 'POST',
                    body: {
                        items: [{ id: item.id, type: item.type }],
                        target_id: item.previous_parent_id,
                        target_type: item.previous_target_type || 'folder',
                    },
                });
            } catch (e) {
                console.error('Undo item failure:', e);
            }
        }

        showToast('تم التراجع عن النقل بنجاح', 'success');
        loadScope(state.currentScopeId, state.currentScopeType);
    }

    // =========================================================================
    // DETAILS DRAWER
    // =========================================================================

    function switchDrawerTab(tabId) {
        const tabs = [
            { id: 'info', btn: elements.tabBtnDrawerInfo, pane: elements.drawerPaneInfo },
            { id: 'playback', btn: elements.tabBtnDrawerPlayback, pane: elements.drawerPanePlayback },
            { id: 'subs', btn: elements.tabBtnDrawerSubs, pane: elements.drawerPaneSubs },
            { id: 'share', btn: elements.tabBtnDrawerShare, pane: elements.drawerPaneShare },
            { id: 'download', btn: elements.tabBtnDrawerDownload, pane: elements.drawerPaneDownload },
        ];

        tabs.forEach(t => {
            if (!t.btn || !t.pane) return;
            if (t.id === tabId) {
                t.btn.classList.add('is-active');
                t.pane.classList.remove('is-hidden');
            } else {
                t.btn.classList.remove('is-active');
                t.pane.classList.add('is-hidden');
            }
        });
    }

    async function loadDrawerSubtitles(rawItem) {
        if (!elements.drawerSubsList) return;
        const item = normalizeItem(rawItem);
        if (item.type !== 'video') {
            elements.drawerSubsList.innerHTML = '<div class="drawer-empty-msg">الترجمات متاحة لمقاطع الفيديو فقط</div>';
            if (elements.drawerSubsCountBadge) elements.drawerSubsCountBadge.classList.add('is-hidden');
            return;
        }

        elements.drawerSubsList.innerHTML = '<div class="drawer-empty-msg">جاري تحميل الترجمات...</div>';

        try {
            const data = await apiRequest(`/api/v1/admin/videos/${item.id}/subtitles`);
            const tracks = Array.isArray(data) ? data : [];
            if (elements.drawerSubsCountBadge) {
                elements.drawerSubsCountBadge.textContent = tracks.length;
                if (tracks.length > 0) {
                    elements.drawerSubsCountBadge.classList.remove('is-hidden');
                } else {
                    elements.drawerSubsCountBadge.classList.add('is-hidden');
                }
            }

            if (tracks.length === 0) {
                elements.drawerSubsList.innerHTML = '<div class="drawer-empty-msg">لا توجد ترجمات</div>';
                return;
            }

            let html = '';
            tracks.forEach(track => {
                const lang = escapeHtml(track.language_name || track.label || track.language_code || 'ترجمة');
                const fmt = escapeHtml((track.format || 'ASS').toUpperCase());
                const isDef = track.is_default ? '<span class="sub-badge-default">افتراضية</span>' : '';
                const isForced = track.is_forced ? '<span class="sub-badge-default">إجبارية</span>' : '';
                const isEmbedded = track.is_playable ? '<span class="text-xs text-muted">مضمنة</span>' : '';

                html += `
                    <div class="sub-track-card">
                        <div>
                            <div class="sub-track-title">${lang} — ${fmt}</div>
                            <div class="sub-track-meta">${track.label ? escapeHtml(track.label) : ''} ${isEmbedded}</div>
                        </div>
                        <div class="flex items-center gap-6">
                            ${isDef}
                            ${isForced}
                        </div>
                    </div>
                `;
            });
            elements.drawerSubsList.innerHTML = html;
        } catch (err) {
            elements.drawerSubsList.innerHTML = '<div class="drawer-empty-msg">تعذر تحميل الترجمات</div>';
        }
    }

    async function loadDrawerShare(rawItem) {
        if (!elements.drawerActiveShareBox || !elements.drawerNoShareBox) return;
        const item = normalizeItem(rawItem);

        try {
            const res = await apiRequest(`/api/v1/drive/shares/target/${item.type}/${item.id}`);
            if (res && res.exists) {
                elements.drawerNoShareBox.classList.add('is-hidden');
                elements.drawerActiveShareBox.classList.remove('is-hidden');
                if (elements.drawerShareStatus) elements.drawerShareStatus.textContent = 'نشط';
                if (elements.drawerShareExpiry) {
                    elements.drawerShareExpiry.textContent = res.expires_at ? new Date(res.expires_at).toLocaleDateString('ar-EG') : 'دائم (بلا انتهاء)';
                }
                if (elements.drawerShareAllowDl) {
                    elements.drawerShareAllowDl.textContent = res.allow_download ? 'مسموح' : 'معطل';
                }
                const shareUrl = `${window.location.origin}${res.share_url}`;
                if (elements.drawerShareUrlInput) {
                    elements.drawerShareUrlInput.value = shareUrl;
                }

                if (elements.btnDrawerRevokeShare) {
                    elements.btnDrawerRevokeShare.onclick = async () => {
                        try {
                            await apiRequest(`/api/v1/drive/shares/${res.token}/revoke`, { method: 'PATCH' });
                            showToast('تم إلغاء رابط المشاركة بنجاح', 'success');
                            loadDrawerShare(item);
                        } catch (err) {
                            showToast('تعذر إلغاء رابط المشاركة', 'error');
                        }
                    };
                }
            } else {
                elements.drawerNoShareBox.classList.remove('is-hidden');
                elements.drawerActiveShareBox.classList.add('is-hidden');
            }
        } catch (err) {
            elements.drawerNoShareBox.classList.remove('is-hidden');
            elements.drawerActiveShareBox.classList.add('is-hidden');
        }
    }

    let embedRequestGeneration = 0;
    let embedData = null;

    async function loadEmbedPanel(item) {
        const generation = ++embedRequestGeneration;
        embedData = null;
        const box = document.getElementById('drawerEmbedBox');
        const fields = document.getElementById('drawerEmbedFields');
        const status = document.getElementById('drawerEmbedStatus');
        box.classList.toggle('is-hidden', item.type !== 'video');
        fields.classList.add('is-hidden');
        document.getElementById('drawerEmbedUrl').value = '';
        document.getElementById('drawerEmbedCode').value = '';
        if (item.type !== 'video') return;
        document.getElementById('drawerEmbedSettings').href = `/admin/videos/detail?v=${encodeURIComponent(item.id)}`;
        status.textContent = 'جارٍ التحقق من إتاحة الحلقة…';
        try {
            const response = await fetch(`/api/v1/admin/videos/${encodeURIComponent(item.id)}`, {
                credentials: 'same-origin', cache: 'no-store'
            });
            if (!response.ok) throw new Error('metadata');
            const video = await response.json();
            if (generation !== embedRequestGeneration) return;
            if (video.status !== 'READY' || !video.active_generation_id) {
                status.textContent = 'الحلقة غير جاهزة للتشغيل بعد.'; return;
            }
            if (video.share_revoked) {
                status.textContent = 'المشاركة موقوفة لهذه الحلقة. راجع إعدادات المشاركة.'; return;
            }
            if (!['PUBLIC', 'UNLISTED', 'PASSWORD_PROTECTED'].includes(video.visibility)) {
                status.textContent = 'هذه الحلقة خاصة؛ لن تعمل للزوار. غيّر الإتاحة من إعدادات الحلقة إذا أردت مشاركتها.'; return;
            }
            const url = new URL(`/embed/${encodeURIComponent(item.id)}`, window.location.origin).href;
            const code = `<iframe src="${url}" title="مشغّل ABOODVIDEO" width="100%" height="480" style="border:0;max-width:100%;aspect-ratio:16/9" allow="autoplay; fullscreen; picture-in-picture" allowfullscreen loading="lazy"></iframe>`;
            embedData = {id: item.id, url, code};
            document.getElementById('drawerEmbedUrl').value = url;
            document.getElementById('drawerEmbedCode').value = code;
            fields.classList.remove('is-hidden');
            status.textContent = video.visibility === 'PASSWORD_PROTECTED'
                ? 'الحلقة محمية بكلمة مرور؛ سيطلبها المشغّل من الزائر.'
                : 'جاهز للنسخ. يخضع التشغيل لقيود المواقع المسموح بها في إعدادات التضمين.';
        } catch (_) {
            if (generation === embedRequestGeneration) status.textContent = 'تعذّر التحقق من الحلقة. أعد فتح التفاصيل وحاول مرة أخرى.';
        }
    }

    async function copyEmbed(kind) {
        if (!embedData || String(state.contextTarget?.id) !== String(embedData.id)) return;
        const field = document.getElementById(kind === 'code' ? 'drawerEmbedCode' : 'drawerEmbedUrl');
        try {
            await navigator.clipboard.writeText(kind === 'code' ? embedData.code : embedData.url);
            showToast(kind === 'code' ? 'تم نسخ كود iframe' : 'تم نسخ رابط التضمين', 'success');
        } catch (_) {
            field.focus(); field.select();
            showToast('حدّدنا النص لك؛ انسخه يدويًا من الحقل.', 'info');
        }
    }

    function openDetailsDrawer(rawItem) {
        const item = normalizeItem(rawItem);
        if (elements.drawerPosterImg) {
            elements.drawerPosterImg.remove();
            elements.drawerPosterImg = null;
        }
        state.contextTarget = item;
        loadEmbedPanel(item);
        switchDrawerTab('info');

        // Header and hero info
        if (elements.drawerTitle) elements.drawerTitle.textContent = item.name || 'تفاصيل العنصر';
        if (elements.drawerInfoTitle) elements.drawerInfoTitle.textContent = item.name || '—';
        if (elements.drawerTypeBadge) elements.drawerTypeBadge.textContent = item.type_label || (item.type === 'video' ? 'فيديو' : item.type);
        if (elements.drawerFilename) elements.drawerFilename.textContent = item.filename || item.name || '—';
        if (elements.drawerQuality) elements.drawerQuality.textContent = item.quality || '—';
        if (elements.drawerDuration) elements.drawerDuration.textContent = item.duration_formatted || '—';
        if (elements.drawerSize) elements.drawerSize.textContent = item.size_formatted || '—';
        if (elements.drawerCreatedAt) elements.drawerCreatedAt.textContent = item.created_at || '—';

        // Download policy human text
        const pol = (item.allow_download_policy || 'INHERIT').toUpperCase();
        if (elements.drawerDownloadPolicyText) {
            elements.drawerDownloadPolicyText.textContent = pol === 'ALLOW' ? 'سماح بالتنزيل دائماً' : pol === 'DENY' ? 'تعطيل التنزيل' : 'موروثة من الأصل';
        }
        if (elements.drawerPolicySelect) {
            elements.drawerPolicySelect.value = pol;
        }
        if (elements.drawerEffectiveBadge) {
            elements.drawerEffectiveBadge.textContent = (pol === 'DENY') ? 'معطل' : 'مسموح';
        }

        // Advanced details
        if (elements.drawerAdvId) elements.drawerAdvId.textContent = item.id || '—';
        if (elements.drawerAdvShareId) elements.drawerAdvShareId.textContent = item.share_id || '—';
        if (elements.drawerAdvFilename) elements.drawerAdvFilename.textContent = item.filename || item.name || '—';
        if (elements.drawerSubtitles) elements.drawerSubtitles.textContent = item.has_subtitles ? 'متوفرة' : 'لا يوجد';

        const isVideo = (item.type === 'video');

        if (isVideo) {
            // Poster and Badges
            if (elements.drawerPosterWrap) elements.drawerPosterWrap.classList.remove('is-hidden');
            if (elements.drawerPrimaryActions) elements.drawerPrimaryActions.classList.remove('is-hidden');
            if (elements.tabBtnDrawerSubs) elements.tabBtnDrawerSubs.classList.remove('is-hidden');
            if (elements.tabBtnDrawerPlayback) elements.tabBtnDrawerPlayback.classList.remove('is-hidden');
            const watchPath = `/watch/${encodeURIComponent(item.share_id || item.id)}`;
            const previewLink = document.getElementById('btnDrawerPlaybackPreview');
            const inspectorLink = document.getElementById('btnDrawerPlaybackInspector');
            const managementLink = document.getElementById('btnDrawerVideoManagement');
            const subtitlesLink = document.getElementById('btnDrawerSubtitleManagement');
            if (subtitlesLink) subtitlesLink.href = `/admin/videos/detail?v=${encodeURIComponent(item.id)}#subtitles`;
            if (previewLink) previewLink.href = watchPath;
            if (inspectorLink) inspectorLink.href = `/admin/videos/detail?v=${encodeURIComponent(item.id)}#inspection`;
            if (managementLink) managementLink.href = `/admin/videos/detail?v=${encodeURIComponent(item.id)}`;

            const posterUrl = getAdminPosterUrl(item.id);
            if (elements.drawerPosterWrap && elements.drawerPosterPlaceholder) {
                elements.drawerPosterPlaceholder.classList.remove('is-hidden');
                if (posterUrl) {
                    const posterImg = document.createElement('img');
                    posterImg.id = 'drawerPosterImg';
                    posterImg.className = 'drawer-poster-img is-hidden';
                    posterImg.alt = 'بوستر الفيديو';
                    posterImg.loading = 'lazy';
                    posterImg.decoding = 'async';
                    posterImg.onload = () => {
                        posterImg.classList.remove('is-hidden');
                        elements.drawerPosterPlaceholder.classList.add('is-hidden');
                    };
                    posterImg.onerror = () => {
                        posterImg.classList.add('is-hidden');
                        elements.drawerPosterPlaceholder.classList.remove('is-hidden');
                    };
                    elements.drawerPosterImg = posterImg;
                    elements.drawerPosterWrap.insertBefore(posterImg, elements.drawerPosterPlaceholder);
                    posterImg.src = posterUrl;
                }
            }

            const isReady = (item.status === 'READY' || item.is_ready !== false);
            if (elements.drawerBadgeStatus) {
                elements.drawerBadgeStatus.textContent = isReady ? 'READY' : (item.status || 'معالجة');
                elements.drawerBadgeStatus.className = isReady ? 'poster-badge badge-status' : 'poster-badge badge-status is-pending';
            }
            if (elements.drawerStatusText) {
                elements.drawerStatusText.textContent = isReady ? 'جاهز للتشغيل' : (item.status || 'قيد المعالجة');
            }
            if (elements.drawerBadgeQuality) {
                elements.drawerBadgeQuality.textContent = item.quality || '';
                elements.drawerBadgeQuality.classList.toggle('is-hidden', !item.quality);
            }
            if (elements.drawerBadgeDuration) {
                elements.drawerBadgeDuration.textContent = item.duration_formatted || '';
                elements.drawerBadgeDuration.classList.toggle('is-hidden', !item.duration_formatted);
            }

            // Canonical Watch URL
            const canonicalWatchUrl = `${window.location.origin}/watch/${item.id}`;
            if (elements.drawerWatchUrlInput) {
                elements.drawerWatchUrlInput.value = isReady ? canonicalWatchUrl : '';
            }
            if (elements.drawerWatchLinkBox) {
                if (isReady) elements.drawerWatchLinkBox.classList.remove('is-hidden');
                else elements.drawerWatchLinkBox.classList.add('is-hidden');
            }

            // Primary Actions: Play, Open, Copy, Share
            if (elements.btnDrawerPlay) {
                elements.btnDrawerPlay.onclick = () => {
                    window.location.href = `/watch/${item.id}`;
                };
            }
            if (elements.btnDrawerOpenNewTab) {
                elements.btnDrawerOpenNewTab.onclick = () => {
                    window.open(`/watch/${item.id}`, '_blank');
                };
            }
            if (elements.btnDrawerCopyLink) {
                elements.btnDrawerCopyLink.onclick = async () => {
                    try {
                        await navigator.clipboard.writeText(canonicalWatchUrl);
                        showToast('تم نسخ رابط المشاهدة بنجاح', 'success');
                    } catch (e) {
                        showToast(canonicalWatchUrl, 'info');
                    }
                };
            }
            if (elements.btnDrawerShare) {
                elements.btnDrawerShare.onclick = () => {
                    switchDrawerTab('share');
                };
            }
            if (elements.btnDrawerCopyWatchUrl) {
                elements.btnDrawerCopyWatchUrl.onclick = async () => {
                    try {
                        await navigator.clipboard.writeText(canonicalWatchUrl);
                        showToast('تم نسخ رابط المشاهدة بنجاح', 'success');
                    } catch (e) {
                        showToast(canonicalWatchUrl, 'info');
                    }
                };
            }

            // Direct download button in Download tab
            if (elements.btnDrawerDirectDownload) {
                elements.btnDrawerDirectDownload.onclick = () => {
                    window.open(`/api/v1/playback/direct-download/${item.id}`, '_blank');
                };
            }

            // Load dynamic tab content
            loadDrawerSubtitles(item);
            loadDrawerShare(item);
        } else {
            // Not a video (folder or series)
            if (elements.drawerPosterWrap) elements.drawerPosterWrap.classList.add('is-hidden');
            if (elements.drawerPrimaryActions) elements.drawerPrimaryActions.classList.add('is-hidden');
            if (elements.drawerWatchLinkBox) elements.drawerWatchLinkBox.classList.add('is-hidden');
            if (elements.tabBtnDrawerSubs) elements.tabBtnDrawerSubs.classList.add('is-hidden');
            if (elements.tabBtnDrawerPlayback) elements.tabBtnDrawerPlayback.classList.add('is-hidden');
            loadDrawerShare(item);
        }

        elements.detailsDrawer.classList.add('is-open');
    }

    function closeDetailsDrawer() {
        ++embedRequestGeneration;
        embedData = null;
        elements.detailsDrawer.classList.remove('is-open');
    }

    // =========================================================================
    // MODALS: FOLDER / SERIES / SEASON / RENAME / SHARE / MOVE / DETECT
    // =========================================================================

    function getCurrentPathString() {
        if (state.currentData && state.currentData.breadcrumbs && state.currentData.breadcrumbs.length > 0) {
            return state.currentData.breadcrumbs.map(b => b.name).join(' / ');
        }
        return 'مكتبة المحتوى';
    }

    function openCreateFolderModal() {
        closeAllDropdowns();
        closeAllContextMenus();
        if (elements.folderModalLocation) elements.folderModalLocation.textContent = getCurrentPathString();
        elements.folderModalInput.value = '';
        elements.folderModal.classList.add('is-open');
        setTimeout(() => elements.folderModalInput.focus(), 50);
    }

    async function handleConfirmCreateFolder() {
        const name = elements.folderModalInput.value.trim();
        if (!name) return;
        elements.folderModal.classList.remove('is-open');

        let parentId = null;
        if (state.currentScopeType === 'folder' && state.currentScopeId !== 'root') {
            parentId = state.currentScopeId;
        }

        try {
            await apiRequest('/api/v1/drive/folders', {
                method: 'POST',
                body: { name, parent_id: parentId },
            });
            showToast('تم إنشاء المجلد بنجاح', 'success');
            loadScope(state.currentScopeId, state.currentScopeType);
        } catch (err) {
            console.error(err);
        }
    }

    function openCreateSeriesModal() {
        closeAllDropdowns();
        closeAllContextMenus();
        if (elements.seriesModalLocation) elements.seriesModalLocation.textContent = getCurrentPathString();
        elements.seriesModalTitle.value = '';
        elements.seriesModalYear.value = '';
        elements.seriesModalDesc.value = '';
        elements.seriesModal.classList.add('is-open');
        setTimeout(() => elements.seriesModalTitle.focus(), 50);
    }

    async function handleConfirmCreateSeries() {
        const title = elements.seriesModalTitle.value.trim();
        if (!title) return;
        const year = elements.seriesModalYear.value ? parseInt(elements.seriesModalYear.value, 10) : null;
        const description = elements.seriesModalDesc.value.trim() || null;
        elements.seriesModal.classList.remove('is-open');

        let parentFolderId = null;
        if (state.currentScopeType === 'folder' && state.currentScopeId !== 'root') {
            parentFolderId = state.currentScopeId;
        }

        try {
            await apiRequest('/api/v1/drive/series', {
                method: 'POST',
                body: { title, parent_folder_id: parentFolderId, year, description },
            });
            showToast('تم إنشاء المسلسل بنجاح', 'success');
            loadScope(state.currentScopeId, state.currentScopeType);
        } catch (err) {
            console.error(err);
        }
    }

    function openCreateSeasonModal() {
        closeAllDropdowns();
        closeAllContextMenus();
        if (elements.seasonModalLocation) elements.seasonModalLocation.textContent = getCurrentPathString();
        elements.seasonModalNumber.value = '1';
        elements.seasonModalName.value = '';
        elements.seasonModal.classList.add('is-open');
        setTimeout(() => elements.seasonModalNumber.focus(), 50);
    }

    async function handleConfirmCreateSeason() {
        if (state.currentScopeType !== 'series') return;
        const seasonNumber = parseInt(elements.seasonModalNumber.value, 10);
        const displayName = elements.seasonModalName.value.trim() || null;
        elements.seasonModal.classList.remove('is-open');

        try {
            await apiRequest('/api/v1/drive/seasons', {
                method: 'POST',
                body: {
                    series_id: state.currentScopeId,
                    season_number: seasonNumber,
                    display_name: displayName,
                },
            });
            showToast('تمت إضافة الموسم بنجاح', 'success');
            loadScope(state.currentScopeId, state.currentScopeType);
        } catch (err) {
            console.error(err);
        }
    }

    function openRenameModal(item) {
        closeAllContextMenus();
        state.contextTarget = item;
        if (elements.renameModalLocation) elements.renameModalLocation.textContent = getCurrentPathString();
        elements.renameModalInput.value = item.name;
        elements.renameModal.classList.add('is-open');
        setTimeout(() => {
            elements.renameModalInput.focus();
            elements.renameModalInput.select();
        }, 50);
    }

    async function handleConfirmRename() {
        if (!state.contextTarget) return;
        const newName = elements.renameModalInput.value.trim();
        if (!newName || newName === state.contextTarget.name) {
            elements.renameModal.classList.remove('is-open');
            return;
        }
        elements.renameModal.classList.remove('is-open');

        try {
            await apiRequest('/api/v1/drive/rename', {
                method: 'PATCH',
                body: {
                    id: state.contextTarget.id,
                    type: state.contextTarget.type,
                    name: newName,
                },
            });
            showToast('تمت إعادة التسمية بنجاح', 'success');
            loadScope(state.currentScopeId, state.currentScopeType);
        } catch (err) {
            console.error(err);
        }
    }

    let shareHasPassword = false;

    function updateSharePasswordInput() {
        if (elements.sharePasswordInput && elements.sharePasswordToggle) {
            elements.sharePasswordInput.disabled = !elements.sharePasswordToggle.checked;
            elements.sharePasswordInput.placeholder = shareHasPassword
                ? 'اتركها فارغة للإبقاء على كلمة المرور الحالية' : 'أدخل كلمة مرور للمشاركة';
        }
    }

    async function openShareModal(item) {
        closeContextMenu();
        state.contextTarget = item;
        elements.shareTargetTitle.textContent = item.name;
        elements.shareTargetTypeBadge.textContent = item.type_label || item.type;
        elements.shareAllowDownloadToggle.checked = false;
        shareHasPassword = false;
        if (elements.sharePasswordToggle) elements.sharePasswordToggle.checked = false;
        if (elements.sharePasswordInput) elements.sharePasswordInput.value = '';
        elements.shareLinkResultBox.classList.add('is-hidden');

        try {
            const existing = await apiRequest(`/api/v1/drive/shares/target/${item.type}/${item.id}`);
            if (existing && existing.exists) {
                elements.shareUrlDisplay.value = `${window.location.origin}${existing.share_url}`;
                elements.shareAllowDownloadToggle.checked = existing.allow_download;
                shareHasPassword = Boolean(existing.password_required);
                if (elements.sharePasswordToggle) elements.sharePasswordToggle.checked = shareHasPassword;
                elements.shareLinkResultBox.classList.remove('is-hidden');
                elements.btnOpenSharePreview.href = existing.share_url;
            }
        } catch (e) {
            console.error('Error fetching share info:', e);
        }

        updateSharePasswordInput();
        elements.shareModal.classList.add('is-open');
    }

    async function handleGenerateShare() {
        if (!state.contextTarget) return;
        const allowDownload = elements.shareAllowDownloadToggle.checked;
        const payload = {
            target_type: state.contextTarget.type,
            target_id: state.contextTarget.id,
            allow_download: allowDownload,
        };
        if (elements.sharePasswordToggle?.checked) {
            const password = elements.sharePasswordInput?.value || '';
            if (password) payload.password = password;
            else if (!shareHasPassword) {
                showToast('أدخل كلمة مرور للمشاركة', 'error');
                elements.sharePasswordInput?.focus();
                return;
            }
        } else {
            payload.password = null;
        }

        try {
            const res = await apiRequest('/api/v1/drive/shares', {
                method: 'POST',
                body: payload,
            });
            elements.shareUrlDisplay.value = `${window.location.origin}${res.share_url}`;
            elements.shareLinkResultBox.classList.remove('is-hidden');
            elements.btnOpenSharePreview.href = res.share_url;
            shareHasPassword = Boolean(res.password_required);
            if (elements.sharePasswordInput) elements.sharePasswordInput.value = '';
            updateSharePasswordInput();
            showToast('تم إنشاء الرابط بنجاح', 'success');
        } catch (err) {
            console.error(err);
        }
    }

    async function handleRevokeShare() {
        const url = elements.shareUrlDisplay.value;
        const token = url.split('/').pop();
        if (!token) return;

        try {
            await apiRequest(`/api/v1/drive/shares/${token}`, { method: 'DELETE' });
            elements.shareLinkResultBox.classList.add('is-hidden');
            elements.shareUrlDisplay.value = '';
            showToast('تم تعطيل الرابط بنجاح', 'success');
        } catch (err) {
            console.error(err);
        }
    }

    function openMoveDestinationPicker(items) {
        state.pendingDeleteItems = items;
        elements.moveDestinationSelect.innerHTML = '';

        const rootOpt = document.createElement('option');
        rootOpt.value = 'root';
        rootOpt.textContent = 'مكتبة المحتوى (المستوى الرئيسي)';
        elements.moveDestinationSelect.appendChild(rootOpt);

        if (state.treeData && state.treeData.tree) {
            const populateOptions = (nodes, indent = '') => {
                nodes.forEach(node => {
                    const opt = document.createElement('option');
                    opt.value = node.id;
                    opt.dataset.type = node.type;
                    const prefix = node.type === 'series' ? '' : node.type === 'season' ? '' : '';
                    opt.textContent = `${indent}${prefix}${node.name}`;
                    elements.moveDestinationSelect.appendChild(opt);
                    if (node.children && node.children.length > 0) {
                        populateOptions(node.children, indent + '    ');
                    }
                });
            };
            populateOptions(state.treeData.tree);
        }

        elements.moveModal.classList.add('is-open');
    }

    async function handleConfirmMovePicker() {
        const selectedOpt = elements.moveDestinationSelect.selectedOptions[0];
        if (!selectedOpt) return;
        const targetId = selectedOpt.value;
        const targetType = selectedOpt.dataset.type || 'folder';
        elements.moveModal.classList.remove('is-open');

        const items = state.pendingDeleteItems;
        await moveItems(items, targetId, targetType);
    }

    // =========================================================================
    // AUTHORITATIVE SCOPE / FILTER CHANGER
    // =========================================================================

    function setFilterScope(filterValue, preserveScope = false) {
        state.contentFilter = filterValue;
        state.page = 1;
        closeAllDropdowns();

        // Update active class in filter dropdown menu
        if (elements.filterDropdownMenu) elements.filterDropdownMenu.querySelectorAll('.dropdown-item').forEach(btn => {
            btn.classList.toggle('is-active', btn.dataset.filter === filterValue);
        });

        // Update filter label
        const labels = {
            content: 'المحتوى',
            uncategorized: 'غير مصنف',
            test: 'بيانات الاختبار',
            all: 'الكل',
            trash: 'سلة المحذوفات',
        };
        elements.currentFilterLabel.textContent = labels[filterValue] || 'المحتوى';

        // Load scope
        if (preserveScope) {
            loadScope(state.currentScopeId, state.currentScopeType);
        } else if (filterValue === 'trash') {
            loadScope('trash', 'trash');
        } else {
            loadScope('root', 'root');
        }
    }

    // =========================================================================
    // TOAST NOTIFICATIONS
    // =========================================================================

    function showToast(message, type = 'info', options = {}) {
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;

        const msgSpan = document.createElement('span');
        msgSpan.textContent = message;
        toast.appendChild(msgSpan);

        if (options.actionText && typeof options.onAction === 'function') {
            const actionBtn = document.createElement('button');
            actionBtn.type = 'button';
            actionBtn.className = 'toast-btn';
            actionBtn.textContent = options.actionText;
            actionBtn.addEventListener('click', () => {
                options.onAction();
                toast.remove();
            });
            toast.appendChild(actionBtn);
        }

        elements.toastContainer.appendChild(toast);
        setTimeout(() => {
            toast.remove();
        }, 5000);
    }

    function closeAllDropdowns() {
        document.querySelectorAll('.dropdown-wrapper.is-open').forEach(d => d.classList.remove('is-open'));
        document.querySelectorAll('.crumb-item-wrap.is-open').forEach(c => c.classList.remove('is-open'));
        closeContextMenu();
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

    // =========================================================================
    // EVENT LISTENERS & INITIALIZATION
    // =========================================================================

    function initEventListeners() {
        // Global click to close menus & dropdowns
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.dropdown-wrapper') && !e.target.closest('.crumb-item-wrap')) {
                closeAllDropdowns();
            }
        });

        // Topbar: Unified New Dropdown
        if (elements.btnNewDropdown) elements.btnNewDropdown.addEventListener('click', (e) => {
            e.stopPropagation();
            const isOpen = elements.newDropdownWrap.classList.contains('is-open');
            closeAllDropdowns();
            if (!isOpen) elements.newDropdownWrap.classList.add('is-open');
        });

        if (elements.menuItemNewFolder) elements.menuItemNewFolder.addEventListener('click', openCreateFolderModal);
        if (elements.menuItemNewSeries) elements.menuItemNewSeries.addEventListener('click', openCreateSeriesModal);
        if (elements.menuItemNewSeason) elements.menuItemNewSeason.addEventListener('click', openCreateSeasonModal);

        // Filter Dropdown
        if (elements.btnFilterDropdown) elements.btnFilterDropdown.addEventListener('click', (e) => {
            e.stopPropagation();
            const isOpen = elements.filterDropdownWrap.classList.contains('is-open');
            closeAllDropdowns();
            if (!isOpen) elements.filterDropdownWrap.classList.add('is-open');
        });

        if (elements.filterDropdownMenu) elements.filterDropdownMenu.querySelectorAll('.dropdown-item').forEach(itemBtn => {
            itemBtn.addEventListener('click', () => {
                setFilterScope(itemBtn.dataset.filter);
            });
        });

        // Sort Dropdown
        if (elements.btnSortDropdown) elements.btnSortDropdown.addEventListener('click', (e) => {
            e.stopPropagation();
            const isOpen = elements.sortDropdownWrap.classList.contains('is-open');
            closeAllDropdowns();
            if (!isOpen) elements.sortDropdownWrap.classList.add('is-open');
        });

        if (elements.sortDropdownMenu) elements.sortDropdownMenu.querySelectorAll('.dropdown-item').forEach(itemBtn => {
            itemBtn.addEventListener('click', () => {
                state.sortKey = itemBtn.dataset.sort;
                state.page = 1;
                elements.currentSortLabel.textContent = itemBtn.textContent;
                if (elements.sortDropdownMenu) elements.sortDropdownMenu.querySelectorAll('.dropdown-item').forEach(x => x.classList.remove('is-active'));
                itemBtn.classList.add('is-active');
                closeAllDropdowns();
                if (state.currentData) renderExplorer(state.currentData);
            });
        });

        // Search Input
        let searchTimer = null;
        if (elements.searchInput) elements.searchInput.addEventListener('input', (e) => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => {
                loadScope(state.currentScopeId, state.currentScopeType, e.target.value.trim());
            }, 300);
        });

        // Sidebar Navigation Links
        if (elements.navLibraryLink) elements.navLibraryLink.addEventListener('click', (e) => {
            e.preventDefault();
            document.body.classList.remove('sidebar-open');
            setFilterScope('content');
        });

        if (elements.navTrashLink) elements.navTrashLink.addEventListener('click', (e) => {
            e.preventDefault();
            document.body.classList.remove('sidebar-open');
            setFilterScope('trash');
        });

        if (elements.prevPage) elements.prevPage.addEventListener('click', () => {
            if (state.page <= 1 || !state.currentData) return;
            state.page -= 1;
            if (state.contentFilter === 'trash') loadScope(state.currentScopeId, state.currentScopeType);
            else renderExplorer(state.currentData);
        });
        if (elements.nextPage) elements.nextPage.addEventListener('click', () => {
            if (!state.currentData) return;
            state.page += 1;
            if (state.contentFilter === 'trash') loadScope(state.currentScopeId, state.currentScopeType);
            else renderExplorer(state.currentData);
        });

        // Trash Banner Actions
        if (elements.btnRestoreAllTrash) elements.btnRestoreAllTrash.addEventListener('click', async () => {
            elements.btnRestoreAllTrash.disabled = true;
            try {
                const items = await collectAllCurrentTrashItems();
                if (items.length) await restoreItems(items.map(x => ({id: x.id, type: x.type})));
            } catch (err) { showToast(err.message || 'تعذّر قراءة السلة.', 'error'); }
            finally { elements.btnRestoreAllTrash.disabled = false; }
        });

        if (elements.btnEmptyTrash) elements.btnEmptyTrash.addEventListener('click', async () => {
            elements.btnEmptyTrash.disabled = true;
            try { promptPermanentDelete(await collectAllCurrentTrashItems()); }
            catch (err) { showToast(err.message || 'تعذّر قراءة السلة.', 'error'); }
            finally { elements.btnEmptyTrash.disabled = false; }
        });

        // Select All Checkbox
        for (const checkbox of [elements.selectAllCheckbox, elements.mobileSelectAllCheckbox]) {
            checkbox.addEventListener('change', (e) => selectAllVisible(e.target.checked));
        }

        // Multi Action Bar Buttons
        if (elements.btnCancelSelection) elements.btnCancelSelection.addEventListener('click', clearSelection);
        if (elements.btnMultiMove) elements.btnMultiMove.addEventListener('click', () => {
            const items = Array.from(state.selectedItems.values());
            openMoveDestinationPicker(items);
        });
        if (elements.btnMultiTrash) elements.btnMultiTrash.addEventListener('click', () => {
            const items = Array.from(state.selectedItems.values());
            promptTrashConfirmation(items);
        });
        if (elements.btnMultiRestore) elements.btnMultiRestore.addEventListener('click', () => {
            const items = Array.from(state.selectedItems.values()).map(x => ({ id: x.id, type: x.type }));
            restoreItems(items);
        });
        if (elements.btnMultiPermanentDelete) elements.btnMultiPermanentDelete.addEventListener('click', () => {
            const items = Array.from(state.selectedItems.values());
            promptPermanentDelete(items);
        });
        if (elements.btnMultiShare) elements.btnMultiShare.addEventListener('click', () => {
            const items = Array.from(state.selectedItems.values());
            if (items.length > 0) openShareModal(items[0]);
        });

        // Context Menu Action Listeners
        if (elements.ctxMenuOpen) {
            elements.ctxMenuOpen.addEventListener('click', () => {
                closeContextMenu();
                if (state.contextTarget) loadScope(state.contextTarget.id, state.contextTarget.type);
            });
        }
        if (elements.ctxMenuPlay) {
            elements.ctxMenuPlay.addEventListener('click', () => {
                closeContextMenu();
                if (state.contextTarget) window.location.href = `/watch/${state.contextTarget.id}`;
            });
        }
        if (elements.ctxMenuWatchPage) {
            elements.ctxMenuWatchPage.addEventListener('click', () => {
                closeContextMenu();
                if (state.contextTarget) window.open(`/watch/${state.contextTarget.id}`, '_blank');
            });
        }
        if (elements.ctxMenuCopyWatchLink) {
            elements.ctxMenuCopyWatchLink.addEventListener('click', async () => {
                closeContextMenu();
                if (state.contextTarget) {
                    const url = `${window.location.origin}/watch/${state.contextTarget.id}`;
                    try {
                        await navigator.clipboard.writeText(url);
                        showToast('تم نسخ رابط المشاهدة بنجاح', 'success');
                    } catch (e) {
                        showToast(url, 'info');
                    }
                }
            });
        }
        if (elements.ctxMenuRename) {
            elements.ctxMenuRename.addEventListener('click', () => {
                closeContextMenu();
                if (state.contextTarget) openRenameModal(state.contextTarget);
            });
        }
        if (elements.ctxMenuMove) {
            elements.ctxMenuMove.addEventListener('click', () => {
                closeContextMenu();
                if (state.contextTarget) openMoveDestinationPicker([state.contextTarget]);
            });
        }
        if (elements.ctxMenuShare) {
            elements.ctxMenuShare.addEventListener('click', () => {
                closeContextMenu();
                if (state.contextTarget) openShareModal(state.contextTarget);
            });
        }
        if (elements.ctxMenuSubtitles) {
            elements.ctxMenuSubtitles.addEventListener('click', () => {
                closeContextMenu();
                if (state.contextTarget) {
                    openDetailsDrawer(state.contextTarget);
                    switchDrawerTab('subs');
                }
            });
        }
        if (elements.ctxMenuPolicy) {
            elements.ctxMenuPolicy.addEventListener('click', () => {
                closeContextMenu();
                if (state.contextTarget) {
                    openDetailsDrawer(state.contextTarget);
                    switchDrawerTab('download');
                }
            });
        }
        if (elements.ctxMenuDetails) {
            elements.ctxMenuDetails.addEventListener('click', () => {
                closeContextMenu();
                if (state.contextTarget) openDetailsDrawer(state.contextTarget);
            });
        }
        if (elements.ctxMenuTrash) {
            elements.ctxMenuTrash.addEventListener('click', () => {
                closeContextMenu();
                if (state.contextTarget) promptTrashConfirmation([state.contextTarget]);
            });
        }
        if (elements.ctxMenuRestore) {
            elements.ctxMenuRestore.addEventListener('click', () => {
                closeContextMenu();
                if (state.contextTarget) restoreItems([{ id: state.contextTarget.id, type: state.contextTarget.type }]);
            });
        }
        if (elements.ctxMenuPermanentDelete) {
            elements.ctxMenuPermanentDelete.addEventListener('click', () => {
                closeContextMenu();
                if (state.contextTarget) promptPermanentDelete([state.contextTarget]);
            });
        }

        if (elements.ctxMenuCopy) {
            elements.ctxMenuCopy.addEventListener('click', () => {
                closeAllContextMenus();
                if (state.contextTarget) copyItemToClipboard(state.contextTarget, 'copy');
            });
        }
        if (elements.ctxMenuCut) {
            elements.ctxMenuCut.addEventListener('click', () => {
                closeAllContextMenus();
                if (state.contextTarget) copyItemToClipboard(state.contextTarget, 'cut');
            });
        }

        // Multi-Selection Menu Listeners
        if (elements.ctxMenuMultiMove) {
            elements.ctxMenuMultiMove.addEventListener('click', () => {
                closeAllContextMenus();
                const items = Array.from(state.selectedItems.values());
                if (items.length) openMoveDestinationPicker(items);
            });
        }
        if (elements.ctxMenuMultiCopy) {
            elements.ctxMenuMultiCopy.addEventListener('click', () => {
                closeAllContextMenus();
                copySelectedToClipboard('copy');
            });
        }
        if (elements.ctxMenuMultiCut) {
            elements.ctxMenuMultiCut.addEventListener('click', () => {
                closeAllContextMenus();
                copySelectedToClipboard('cut');
            });
        }
        if (elements.ctxMenuMultiShare) {
            elements.ctxMenuMultiShare.addEventListener('click', () => {
                closeAllContextMenus();
                const items = Array.from(state.selectedItems.values());
                if (items.length) openShareModal(items[0]);
            });
        }
        if (elements.ctxMenuMultiTrash) {
            elements.ctxMenuMultiTrash.addEventListener('click', () => {
                closeAllContextMenus();
                const items = Array.from(state.selectedItems.values());
                if (items.length) promptTrashConfirmation(items);
            });
        }
        if (elements.ctxMenuMultiDeselect) {
            elements.ctxMenuMultiDeselect.addEventListener('click', () => {
                closeAllContextMenus();
                clearSelection();
            });
        }

        // Empty Area Context Menu Listeners
        if (elements.emptyCtxNewFolder) elements.emptyCtxNewFolder.addEventListener('click', openCreateFolderModal);
        if (elements.emptyCtxNewSeries) elements.emptyCtxNewSeries.addEventListener('click', openCreateSeriesModal);
        if (elements.emptyCtxNewSeason) elements.emptyCtxNewSeason.addEventListener('click', openCreateSeasonModal);


        if (elements.emptyCtxPaste) elements.emptyCtxPaste.addEventListener('click', executePaste);
        if (elements.emptyCtxRefresh) elements.emptyCtxRefresh.addEventListener('click', () => {
            closeAllContextMenus();
            loadScope(state.currentScopeId, state.currentScopeType, undefined, true);
            showToast('تم تحديث المحتوى بنجاح', 'info');
        });

        // Sort items in context menu
        function applySort(key, asc) {
            if (key !== undefined) state.sortKey = key;
            if (asc !== undefined) state.sortAsc = asc;
            state.page = 1;
            if (elements.sortDropdownMenu) {
                if (elements.sortDropdownMenu) elements.sortDropdownMenu.querySelectorAll('.dropdown-item').forEach(x => {
                    const isMatch = (x.dataset.sort === state.sortKey);
                    x.classList.toggle('is-active', isMatch);
                    if (isMatch && elements.currentSortLabel) {
                        elements.currentSortLabel.textContent = x.textContent;
                    }
                });
            }
            updateEmptyContextMenuRules();
            closeAllContextMenus();
            if (state.currentData) renderExplorer(state.currentData);
        }

        if (elements.sortByName) elements.sortByName.addEventListener('click', () => applySort('name', state.sortAsc));
        if (elements.sortByDate) elements.sortByDate.addEventListener('click', () => applySort('date', state.sortAsc));
        if (elements.sortBySize) elements.sortBySize.addEventListener('click', () => applySort('size', state.sortAsc));
        if (elements.sortByType) elements.sortByType.addEventListener('click', () => applySort('type', state.sortAsc));
        if (elements.sortDirAsc) elements.sortDirAsc.addEventListener('click', () => applySort(state.sortKey, true));
        if (elements.sortDirDesc) elements.sortDirDesc.addEventListener('click', () => applySort(state.sortKey, false));

        // View Mode buttons
        if (elements.btnViewList) elements.btnViewList.addEventListener('click', () => setViewMode('list'));
        if (elements.btnViewGrid) elements.btnViewGrid.addEventListener('click', () => setViewMode('grid'));
        if (elements.viewModeList) elements.viewModeList.addEventListener('click', () => { setViewMode('list'); closeAllContextMenus(); });
        if (elements.viewModeGrid) elements.viewModeGrid.addEventListener('click', () => { setViewMode('grid'); closeAllContextMenus(); });

        if (elements.btnQuickTrash) elements.btnQuickTrash.addEventListener('click', () => { if (state.contentFilter === 'trash') setFilterScope('content'); else setFilterScope('trash'); });

        // Keep both context menus inside the library content viewport.
        const driveContent = document.querySelector('.drive-content');
        if (driveContent) {
            [elements.emptyContextMenu, elements.unifiedContextMenu].forEach(menu => {
                if (menu && menu.parentElement !== driveContent) driveContent.appendChild(menu);
            });
        }

        // Right-Click Canvas Listener (Empty Area)
        document.addEventListener('contextmenu', (e) => {
            if (e.target.closest('.drive-row') || e.target.closest('.modal-card') || e.target.closest('.details-drawer') || e.target.closest('.sidebar') || e.target.closest('.drive-topbar')) {
                return;
            }
            if (e.target.closest('.drive-content')) {
                e.preventDefault();
                e.stopPropagation();
                openEmptyAreaContextMenu(e);
            }
        });

        // Close context menus on outside click or scroll
        document.addEventListener('click', (e) => {
            if (!e.target.closest('.context-menu') && !e.target.closest('.action-dots-btn')) {
                closeAllContextMenus();
            }
        });
        window.addEventListener('scroll', () => closeAllContextMenus(), { passive: true });
        if (elements.tableContainer) {
            elements.tableContainer.addEventListener('scroll', () => closeAllContextMenus(), { passive: true });
        }

        // Submenu hover positioning logic (check left boundary in RTL)
        document.querySelectorAll('.menu-item-has-submenu').forEach(parentItem => {
            parentItem.addEventListener('mouseenter', () => {
                const submenu = parentItem.querySelector('.context-submenu');
                if (!submenu) return;
                submenu.style.display = 'flex';
                const rect = submenu.getBoundingClientRect();
                if (rect.left < 10) {
                    parentItem.classList.add('flip-submenu');
                } else {
                    parentItem.classList.remove('flip-submenu');
                }
                if (rect.bottom > window.innerHeight - 10) {
                    submenu.style.top = 'auto';
                    submenu.style.bottom = '0';
                } else {
                    submenu.style.top = '-6px';
                    submenu.style.bottom = 'auto';
                }
                submenu.style.display = '';
            });
        });

        // Global Keyboard Shortcuts (F2, Del, Ctrl+A, Ctrl+C, Ctrl+X, Ctrl+V, Esc, F5, Menu Navigation)
        document.addEventListener('keydown', (e) => {
            const tag = (e.target.tagName || '').toLowerCase();
            const isInput = tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable;

            // Escape
            if (e.key === 'Escape') {
                if (closeAllContextMenus()) {
                    e.preventDefault();
                    return;
                }
                if (closeActiveModal()) {
                    e.preventDefault();
                    return;
                }
                if (state.selectedItems.size > 0) {
                    e.preventDefault();
                    clearSelection();
                    return;
                }
                return;
            }

            // Keyboard navigation inside open context menu
            const activeMenu = document.querySelector('.context-menu.is-open');
            if (activeMenu) {
                if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
                    e.preventDefault();
                    const items = Array.from(activeMenu.querySelectorAll('.menu-item:not(.is-disabled):not(.is-hidden)'));
                    if (!items.length) return;
                    const focused = activeMenu.querySelector('.menu-item:focus') || activeMenu.querySelector('.menu-item.is-focused');
                    let idx = items.indexOf(focused);
                    if (e.key === 'ArrowDown') {
                        idx = (idx + 1) % items.length;
                    } else {
                        idx = (idx - 1 + items.length) % items.length;
                    }
                    items.forEach(it => it.classList.remove('is-focused'));
                    items[idx].classList.add('is-focused');
                    items[idx].focus();
                    return;
                } else if (e.key === 'Enter') {
                    const focused = activeMenu.querySelector('.menu-item:focus') || activeMenu.querySelector('.menu-item.is-focused');
                    if (focused) {
                        e.preventDefault();
                        focused.click();
                        return;
                    }
                }
            }

            // If user is typing in form field / search, don't hijack editing keys
            if (isInput) return;

            // F5: Refresh
            if (e.key === 'F5') {
                e.preventDefault();
                loadScope(state.currentScopeId, state.currentScopeType, undefined, true);
                showToast('تم تحديث المحتوى بنجاح', 'info');
                return;
            }

            // F2: Rename
            if (e.key === 'F2') {
                e.preventDefault();
                if (state.selectedItems.size === 1) {
                    const item = Array.from(state.selectedItems.values())[0];
                    openRenameModal(item);
                } else if (state.contextTarget) {
                    openRenameModal(state.contextTarget);
                }
                return;
            }

            // Delete: Trash
            if (e.key === 'Delete') {
                e.preventDefault();
                if (state.selectedItems.size > 0) {
                    promptTrashConfirmation(Array.from(state.selectedItems.values()));
                } else if (state.contextTarget) {
                    promptTrashConfirmation([state.contextTarget]);
                }
                return;
            }

            // Ctrl+A: Select all visible items
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
                e.preventDefault();
                selectAllVisible(true);
                return;
            }

            // Ctrl+C: Copy
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'c') {
                if (state.selectedItems.size > 0) {
                    e.preventDefault();
                    copySelectedToClipboard('copy');
                } else if (state.contextTarget) {
                    e.preventDefault();
                    copyItemToClipboard(state.contextTarget, 'copy');
                }
                return;
            }

            // Ctrl+X: Cut
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'x') {
                if (state.selectedItems.size > 0) {
                    e.preventDefault();
                    copySelectedToClipboard('cut');
                } else if (state.contextTarget) {
                    e.preventDefault();
                    copyItemToClipboard(state.contextTarget, 'cut');
                }
                return;
            }

            // Ctrl+V: Paste
            if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'v') {
                if (state.clipboard && state.clipboard.items && state.clipboard.items.length > 0) {
                    e.preventDefault();
                    executePaste();
                }
                return;
            }
        });

        // Details Drawer Listeners
        if (elements.btnCloseDrawer) elements.btnCloseDrawer.addEventListener('click', closeDetailsDrawer);
        if (elements.tabBtnDrawerInfo) elements.tabBtnDrawerInfo.addEventListener('click', () => switchDrawerTab('info'));
        if (elements.tabBtnDrawerPlayback) elements.tabBtnDrawerPlayback.addEventListener('click', () => switchDrawerTab('playback'));
        if (elements.tabBtnDrawerSubs) elements.tabBtnDrawerSubs.addEventListener('click', () => switchDrawerTab('subs'));
        if (elements.tabBtnDrawerShare) elements.tabBtnDrawerShare.addEventListener('click', () => switchDrawerTab('share'));
        if (elements.tabBtnDrawerDownload) elements.tabBtnDrawerDownload.addEventListener('click', () => switchDrawerTab('download'));

        if (elements.btnDrawerCreateShare) {
            elements.btnDrawerCreateShare.addEventListener('click', () => {
                if (state.contextTarget) openShareModal(state.contextTarget);
            });
        }
        if (elements.btnDrawerCopyShareUrl) {
            elements.btnDrawerCopyShareUrl.addEventListener('click', async () => {
                if (elements.drawerShareUrlInput && elements.drawerShareUrlInput.value) {
                    try {
                        await navigator.clipboard.writeText(elements.drawerShareUrlInput.value);
                        showToast('تم نسخ رابط المشاركة بنجاح', 'success');
                    } catch (e) {
                        showToast(elements.drawerShareUrlInput.value, 'info');
                    }
                }
            });
        }

        if (elements.drawerPolicySelect) {
            elements.drawerPolicySelect.addEventListener('change', async (e) => {
                if (!state.contextTarget) return;
                const policy = e.target.value;
                try {
                    await apiRequest('/api/v1/drive/download-policy', {
                        method: 'PATCH',
                        body: {
                            id: state.contextTarget.id,
                            type: state.contextTarget.type,
                            policy: policy,
                        },
                    });
                    state.contextTarget.allow_download_policy = policy;
                    const eff = (policy === 'DENY') ? 'معطل' : 'مسموح';
                    if (elements.drawerEffectiveBadge) elements.drawerEffectiveBadge.textContent = eff;
                    if (elements.drawerDownloadPolicyText) {
                        elements.drawerDownloadPolicyText.textContent = policy === 'ALLOW' ? 'سماح بالتنزيل دائماً' : policy === 'DENY' ? 'تعطيل التنزيل' : 'موروثة من الأصل';
                    }
                    showToast('تم تحديث سياسة التنزيل بنجاح', 'success');
                    loadScope(state.currentScopeId, state.currentScopeType);
                } catch (err) {
                    console.error(err);
                }
            });
        }

        // Modal Cancel / Close Buttons
        if (elements.btnCancelCreateFolder) elements.btnCancelCreateFolder.addEventListener('click', () => elements.folderModal.classList.remove('is-open'));
        if (elements.btnCloseFolderModalX) elements.btnCloseFolderModalX.addEventListener('click', () => elements.folderModal.classList.remove('is-open'));
        if (elements.btnConfirmCreateFolder) elements.btnConfirmCreateFolder.addEventListener('click', handleConfirmCreateFolder);

        if (elements.btnCancelCreateSeries) elements.btnCancelCreateSeries.addEventListener('click', () => elements.seriesModal.classList.remove('is-open'));
        if (elements.btnCloseSeriesModalX) elements.btnCloseSeriesModalX.addEventListener('click', () => elements.seriesModal.classList.remove('is-open'));
        if (elements.btnConfirmCreateSeries) elements.btnConfirmCreateSeries.addEventListener('click', handleConfirmCreateSeries);

        if (elements.btnCancelCreateSeason) elements.btnCancelCreateSeason.addEventListener('click', () => elements.seasonModal.classList.remove('is-open'));
        if (elements.btnCloseSeasonModalX) elements.btnCloseSeasonModalX.addEventListener('click', () => elements.seasonModal.classList.remove('is-open'));
        if (elements.btnConfirmCreateSeason) elements.btnConfirmCreateSeason.addEventListener('click', handleConfirmCreateSeason);

        if (elements.btnCancelRename) elements.btnCancelRename.addEventListener('click', () => elements.renameModal.classList.remove('is-open'));
        if (elements.btnCloseRenameModalX) elements.btnCloseRenameModalX.addEventListener('click', () => elements.renameModal.classList.remove('is-open'));
        if (elements.btnConfirmRename) elements.btnConfirmRename.addEventListener('click', handleConfirmRename);

        if (elements.btnCancelShare) elements.btnCancelShare.addEventListener('click', () => elements.shareModal.classList.remove('is-open'));
        if (elements.btnCloseShareModalX) elements.btnCloseShareModalX.addEventListener('click', () => elements.shareModal.classList.remove('is-open'));
        if (elements.btnGenerateShare) elements.btnGenerateShare.addEventListener('click', handleGenerateShare);
        if (elements.sharePasswordToggle) elements.sharePasswordToggle.addEventListener('change', updateSharePasswordInput);
        if (elements.btnRevokeShare) elements.btnRevokeShare.addEventListener('click', handleRevokeShare);
        if (elements.btnCopyShareUrl) elements.btnCopyShareUrl.addEventListener('click', () => {
            navigator.clipboard.writeText(elements.shareUrlDisplay.value);
            showToast('تم نسخ رابط المشاركة إلى الحافظة', 'success');
        });

        if (elements.btnCancelMove) elements.btnCancelMove.addEventListener('click', () => elements.moveModal.classList.remove('is-open'));
        if (elements.btnCloseMoveModalX) elements.btnCloseMoveModalX.addEventListener('click', () => elements.moveModal.classList.remove('is-open'));
        if (elements.btnConfirmMove) elements.btnConfirmMove.addEventListener('click', handleConfirmMovePicker);

        if (elements.btnCancelDelete) elements.btnCancelDelete.addEventListener('click', () => elements.deleteModal.classList.remove('is-open'));
        if (elements.btnCloseDeleteModalX) elements.btnCloseDeleteModalX.addEventListener('click', () => elements.deleteModal.classList.remove('is-open'));
        if (elements.btnConfirmDelete) elements.btnConfirmDelete.addEventListener('click', executeTrashPending);

        if (elements.btnCancelPermanentDelete) elements.btnCancelPermanentDelete.addEventListener('click', () => elements.permanentDeleteModal.classList.remove('is-open'));
        if (elements.btnClosePermDeleteModalX) elements.btnClosePermDeleteModalX.addEventListener('click', () => elements.permanentDeleteModal.classList.remove('is-open'));
        if (elements.permanentDeleteConfirmation) elements.permanentDeleteConfirmation.addEventListener('input', () => {
            elements.btnConfirmPermanentDelete.disabled = elements.permanentDeleteConfirmation.value.trim() !== 'حذف';
        });
        if (elements.btnConfirmPermanentDelete) elements.btnConfirmPermanentDelete.addEventListener('click', executePermanentDeletePending);

        // Mobile Toggle
        if (elements.mobileToggle) elements.mobileToggle.addEventListener('click', () => {
            document.body.classList.toggle('sidebar-open');
        });
        const sidebarBackdrop = document.querySelector('.sidebar-backdrop');
        if (sidebarBackdrop) sidebarBackdrop.addEventListener('click', () => {
            document.body.classList.remove('sidebar-open');
        });
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') document.body.classList.remove('sidebar-open');
        });
    }

    // Initialize application
    function init() {
        if (new URLSearchParams(window.location.search).get('embed') === '1') {
            document.documentElement.classList.add('is-embedded');
            document.body.classList.add('is-embedded');
        }
        initEventListeners();
        document.getElementById('btnDrawerCopyEmbedUrl')?.addEventListener('click', () => copyEmbed('url'));
        document.getElementById('btnDrawerCopyEmbedCode')?.addEventListener('click', () => copyEmbed('code'));
        const query = new URLSearchParams(window.location.search);
        const requestedFilter = query.get('filter');
        state.searchQuery = (query.get('q') || '').trim();
        if (elements.searchInput) elements.searchInput.value = state.searchQuery;
        const supportedFilters = new Set(['content', 'uncategorized', 'test', 'all', 'trash']);
        setFilterScope(supportedFilters.has(requestedFilter) ? requestedFilter : 'content');
    }

    window.__abood_drive_state = state;

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
