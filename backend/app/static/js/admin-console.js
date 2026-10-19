window.AVi18n ||= {t: value => value, tpl: (parts, ...values) => parts.reduce((out, part, i) => out + part + (i < values.length ? values[i] : ''), '')};
(() => {
  'use strict';

  const search = document.getElementById('global-search');
  const status = document.getElementById('consoleConnectionStatus');
  const drawer = document.getElementById('uploadDrawer');
  const openUpload = document.getElementById('openUploadDrawer');
  const closeUpload = document.getElementById('closeUploadDrawer');
  const frame = document.getElementById('uploadDrawerFrame');
  const uploadIndicator = document.getElementById('consoleUploadIndicator');

  const focusSearch = () => {
    if (!search) return;
    search.focus();
    search.select();
  };

  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      focusSearch();
    }
    if (event.key === 'Escape' && drawer?.open) drawer.close();
  });

  if (search) {
    const params = new URLSearchParams(window.location.search);
    if (window.location.pathname === '/admin/videos' && params.has('q')) search.value = params.get('q') || '';
  }

  if (drawer && openUpload && frame) {
    openUpload.addEventListener('click', () => {
      const state = window.__abood_drive_state;
      const selected = state?.selectedItems?.size === 1 ? [...state.selectedItems.values()][0] : null;
      const scope = selected && ['folder', 'season'].includes(selected.type)
        ? selected : { id: state?.currentScopeId || 'root', type: state?.currentScopeType || 'root' };
      const destination = ['root', 'folder', 'series', 'season'].includes(scope.type)
        ? scope : { id: 'root', type: 'root' };
      const sendDestination = () => frame.contentWindow?.postMessage({ type: 'abv-upload-destination', id: destination.id, scopeType: destination.type }, window.location.origin);
      if (!frame.src || frame.src === window.location.href) {
        const url = new URL(frame.dataset.src, window.location.origin);
        url.searchParams.set('scope_id', destination.id);
        url.searchParams.set('scope_type', destination.type);
        frame.src = url.href;
      } else {
        sendDestination();
      }
      if (!drawer.open) drawer.showModal();
    });
    closeUpload?.addEventListener('click', () => drawer.close());
    drawer.addEventListener('click', (event) => {
      if (event.target === drawer) drawer.close();
    });
    window.addEventListener('message', (event) => {
      if (event.origin !== window.location.origin || event.source !== frame.contentWindow || event.data?.type !== 'abv-upload-activity') return;
      const count = Math.max(0, Number(event.data.active) || 0);
      const number = uploadIndicator?.querySelector('b');
      if (number) number.textContent = String(count);
      uploadIndicator?.classList.toggle('is-hidden', count === 0);
    });
  }

  const refreshConnection = async () => {
    if (!status) return;
    try {
      const response = await fetch('/api/v1/health', { credentials: 'include', cache: 'no-store' });
      status.dataset.state = response.ok ? 'online' : 'offline';
      const label = status.querySelector('span');
      if (label) label.textContent = response.ok ? window.AVi18n.t('الخادم متصل') : window.AVi18n.t('الخادم غير متاح');
    } catch (_) {
      status.dataset.state = 'offline';
      const label = status.querySelector('span');
      if (label) label.textContent = window.AVi18n.t('الخادم غير متاح');
    }
  };

  refreshConnection();
  window.setInterval(refreshConnection, 30_000);
})();
