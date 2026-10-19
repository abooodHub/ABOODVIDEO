(() => {
  const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  if (reduced || !('IntersectionObserver' in window)) return;
  const selector = [
    '.video-card', '.viewer-card', '.series-card', '.dash-video-row', '.dash-queue-item',
    '.job-card', '.processing-stat-card', '.api-key-card', '.share-stat-card',
    '.analytics-panel-card', '.settings-section', '.upload-queue > *', '.queue-row', '.dash-service-chip', '.dash-path-item'
  ].join(',');
  const seen = new WeakSet();
  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      entry.target.classList.add('abv-motion-enter');
      observer.unobserve(entry.target);
    }
  }, { threshold: 0.08, rootMargin: '0px 0px -24px 0px' });
  const observe = root => {
    if (!(root instanceof Element)) return;
    const nodes = [];
    if (root.matches(selector)) nodes.push(root);
    nodes.push(...root.querySelectorAll(selector));
    nodes.slice(0, 24).forEach((node, index) => {
      if (seen.has(node)) return;
      seen.add(node);
      node.style.setProperty('--atelier-delay', `${Math.min(index, 5) * 38}ms`);
      observer.observe(node);
    });
  };
  document.addEventListener('DOMContentLoaded', () => {
    observe(document.body);
    new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(observe)))
      .observe(document.body, { childList: true, subtree: true });
  }, { once: true });
})();

