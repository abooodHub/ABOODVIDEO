/**
 * ABOODVIDEO Viewer Shell JS (V1)
 * Manages sticky header scrolling, mobile drawer toggling,
 * horizontal media row scrolling, and global keyboard shortcuts.
 */
function initViewerShell() {
    // 1. Sticky Header scroll styling
    const header = document.querySelector('.viewer-header');
    if (header) {
        const updateHeader = () => {
            if (window.scrollY > 20) {
                header.classList.add('is-scrolled');
            } else {
                header.classList.remove('is-scrolled');
            }
        };
        window.addEventListener('scroll', updateHeader, { passive: true });
        updateHeader();
    }

    // 2. Mobile Drawer Navigation
    const hamburgerBtn = document.getElementById('viewer-hamburger-btn');
    const drawer = document.getElementById('viewer-drawer');
    const backdrop = document.getElementById('viewer-drawer-backdrop');
    const drawerCloseBtn = document.getElementById('viewer-drawer-close');

    function openDrawer() {
        if (!drawer || !backdrop) return;
        drawer.classList.add('is-open');
        backdrop.classList.add('is-open');
        document.body.style.overflow = 'hidden';
    }

    function closeDrawer() {
        if (!drawer || !backdrop) return;
        drawer.classList.remove('is-open');
        backdrop.classList.remove('is-open');
        document.body.style.overflow = '';
    }

    if (hamburgerBtn) hamburgerBtn.addEventListener('click', openDrawer);
    if (backdrop) backdrop.addEventListener('click', closeDrawer);
    if (drawerCloseBtn) drawerCloseBtn.addEventListener('click', closeDrawer);

    // 3. Horizontal Scroll Rows navigation arrows
    document.querySelectorAll('.viewer-row-container').forEach(container => {
        const track = container.querySelector('.viewer-row-track');
        const prevBtn = container.querySelector('.viewer-row-arrow-prev');
        const nextBtn = container.querySelector('.viewer-row-arrow-next');

        if (!track) return;

        // In RTL: prev goes right (positive scroll), next goes left (negative scroll)
        if (prevBtn) {
            prevBtn.addEventListener('click', () => {
                const scrollAmount = track.clientWidth * 0.75;
                track.scrollBy({ left: scrollAmount, behavior: 'smooth' });
            });
        }

        if (nextBtn) {
            nextBtn.addEventListener('click', () => {
                const scrollAmount = track.clientWidth * 0.75;
                track.scrollBy({ left: -scrollAmount, behavior: 'smooth' });
            });
        }
    });

    // 4. Keyboard Shortcuts: '/' focuses search input, Escape closes drawer
    window.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeDrawer();
        }

        if (e.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) {
            const searchInput = document.querySelector('.viewer-header-search-input');
            if (searchInput) {
                e.preventDefault();
                searchInput.focus();
            }
        }
    });
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initViewerShell);
} else {
    initViewerShell();
}


  // P0.6: keep missing artwork intentional; use server-provided URLs only.
  document.querySelectorAll('.viewer-main img').forEach((image) => {
    image.addEventListener('error', () => {
      image.hidden = true;
      const frame = image.parentElement;
      if (frame) frame.classList.add('has-poster-fallback');
    }, { once: true });
  });
