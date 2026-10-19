/* Shared UI-only localization. Values interpolated by callers are never translated. */
(() => {
  'use strict';
  const catalog = window.AV_I18N_CATALOG || {};
  const supported = new Set(['ar', 'en']);
  const cookie = document.cookie.split(';').map(p => p.trim()).find(p => p.startsWith('av_language='));
  const query = new URLSearchParams(location.search).get('lang');
  let saved = '';
  try { saved = cookie ? decodeURIComponent(cookie.slice(12)) : ''; } catch (_) {}
  const locale = supported.has(query) ? query : supported.has(saved) ? saved : document.documentElement.lang === 'en' ? 'en' : 'ar';
  document.documentElement.lang = locale;
  document.documentElement.dir = locale === 'en' ? 'ltr' : 'rtl';
  const missing = new Set();
  const arabic = /[\u0621-\u064a\u066e-\u06d3]/;
  const entities = {amp:'&',lt:'<',gt:'>',quot:'"',apos:"'",nbsp:' '};
  const decode = value => value.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos|nbsp);/gi, (full, key) => {
    if (key[0] !== '#') return entities[key.toLowerCase()] || full;
    const code = key[1].toLowerCase() === 'x' ? parseInt(key.slice(2),16) : parseInt(key.slice(1),10);
    return code >= 0 && code <= 0x10ffff ? String.fromCodePoint(code) : full;
  });
  const normalize = value => decode(value).replace(/\s+/g, ' ').trim();
  const own = key => Object.prototype.hasOwnProperty.call(catalog, key);
  const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fill = (value, args) => value.replace(/\{(\d+)\}/g, (full, index) => index in args ? String(args[index]) : full);
  let patterns;
  function translatePlain(message) {
    const key = normalize(message);
    if (own(key)) return catalog[key];
    if (!patterns) patterns = Object.entries(catalog).filter(([id]) => /\{\d+\}/.test(id) && !/<[a-z]/i.test(id)).map(([id, value]) => {
      const slots = []; const pieces = id.split(/(\{\d+\})/);
      const source = pieces.map(piece => /^\{\d+\}$/.test(piece) ? (slots.push(piece.slice(1,-1)), '([\\s\\S]+?)') : piece.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('');
      return {pattern:new RegExp('^' + source + '$'), slots, value};
    });
    for (const entry of patterns) {
      const match = entry.pattern.exec(key);
      if (match) return fill(entry.value, Object.fromEntries(entry.slots.map((slot,index) => [slot,match[index+1]])));
    }
    if (arabic.test(key)) missing.add(key);
    return message;
  }
  function fragment(message) {
    const translate = text => {
      if (!arabic.test(text)) return text;
      const result = translatePlain(text);
      return result === text ? text : (text.match(/^\s*/)?.[0] || '') + escape(result) + (text.match(/\s*$/)?.[0] || '');
    };
    return message.replace(/\b(title|aria-label|aria-description|placeholder|alt|label)=(['"])([\s\S]*?)\2/gi,
      (full,name,quote,value) => `${name}=${quote}${translate(value)}${quote}`)
      .replace(/(^|>)([^<>]*)(?=<|$)/g, (full, boundary, text) => boundary + translate(text));
  }
  function t(message) {
    if (locale !== 'en' || typeof message !== 'string' || !arabic.test(message)) return message;
    if (/<\/?[a-z][^>]*>/i.test(message)) return fragment(message);
    const result = translatePlain(message);
    return result === message ? message : (message.match(/^\s*/)?.[0] || '') + result + (message.match(/\s*$/)?.[0] || '');
  }
  function tpl(parts, ...values) {
    if (locale !== 'en') return parts.reduce((out, part, i) => out + part + (i < values.length ? String(values[i]) : ''), '');
    const message = parts.reduce((out, part, i) => out + part + (i < values.length ? `{${i}}` : ''), '');
    return fill(t(message), values);
  }
  async function change(value) {
    if (!supported.has(value) || value === locale) return;
    document.cookie = `av_language=${value}; Path=/; Max-Age=31536000; SameSite=Lax${location.protocol === 'https:' ? '; Secure' : ''}`;
    try { localStorage.setItem('av_language', value); } catch (_) {}
    if (document.body.dataset.application === 'studio') {
      try {
        await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({language:value})});
      } catch (_) { /* The durable browser cookie still preserves this window's preference. */ }
    }
    const url = new URL(location.href);
    url.searchParams.set('lang', value);
    location.assign(url);
  }
  const deleteWord = locale === 'en' ? 'DELETE' : 'حذف';
  window.AVi18n = Object.freeze({locale, t, tpl, change, missing, deleteWord});
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-language-select]').forEach(select => {
      select.value = locale;
      select.addEventListener('change', () => change(select.value));
    });
  });
})();
