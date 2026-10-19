/**
 * ABOODVIDEO Viewer Browse JS (V1)
 * Manages debounced search and filter selection on the browse/catalog page.
 */
function initViewerBrowse() {
    const browseSearchInput = document.getElementById('browse-search-input');
    const browseForm = document.getElementById('browse-filter-form');

    if (browseSearchInput && browseForm) {
        let debounceTimer;
        browseSearchInput.addEventListener('input', () => {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => {
                browseForm.submit();
            }, 450);
        });
    }

    if (browseForm) {
        browseForm.querySelectorAll('select[name="quality"], select[name="sub"]').forEach((select) => {
            select.addEventListener('change', () => browseForm.requestSubmit());
        });
    }

    const sortSelect = document.getElementById('browse-sort-select');
    if (sortSelect && browseForm) {
        sortSelect.addEventListener('change', () => {
            browseForm.submit();
        });
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initViewerBrowse);
} else {
    initViewerBrowse();
}
