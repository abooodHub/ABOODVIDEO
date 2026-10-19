/* Navigation accessibility only. The accepted shell owns toggle and logout actions. */
document.addEventListener('DOMContentLoaded',()=>{
  const sidebar=document.getElementById('adminSidebar');
  const toggle=document.getElementById('sidebarToggleBtn')||document.querySelector('.sidebar-toggle')||document.getElementById('mobileToggle');
  if(!sidebar||!toggle)return;
  toggle.setAttribute('aria-controls','adminSidebar');
  let wasOpen=false;
  const sync=()=>{const open=document.body.classList.contains('sidebar-open');toggle.setAttribute('aria-expanded',String(open));if(open&&!wasOpen&&matchMedia('(max-width:900px)').matches)sidebar.querySelector('a')?.focus();if(!open&&wasOpen)toggle.focus();wasOpen=open;};
  new MutationObserver(sync).observe(document.body,{attributes:true,attributeFilter:['class']});sync();
  document.addEventListener('keydown',e=>{if(e.key!=='Tab'||!wasOpen||!matchMedia('(max-width:900px)').matches)return;const links=[...sidebar.querySelectorAll('a[href],button:not([disabled])')].filter(n=>n.getClientRects().length);if(!links.length)return;const first=links[0],last=links[links.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}});
});
