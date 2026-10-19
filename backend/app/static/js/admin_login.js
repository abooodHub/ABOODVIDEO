/**
 * admin_login.js — ABOODVIDEO Admin V2
 * Handles login form submission, API call, redirect and inline error display.
 * RTL-first. ES6+. No external dependencies.
 */

(() => {
  'use strict';

  /* ─────────────────────────────────────────────
   * HELPERS
   * ───────────────────────────────────────────── */
  const escapeHtml = (str) =>
    String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');

  /**
   * Show or clear an inline error message below the form.
   * @param {string|null} msg - Pass null/empty to clear the error.
   */
  const setError = (msg) => {
    let errorEl = document.getElementById('login-error');

    if (!msg) {
      if (errorEl) {
        errorEl.textContent = '';
        errorEl.hidden = true;
      }
      return;
    }

    if (!errorEl) {
      // Create the element if the template didn't include it
      errorEl = document.createElement('p');
      errorEl.id = 'login-error';
      errorEl.className = 'login-error-msg';
      errorEl.setAttribute('role', 'alert');
      const form = document.getElementById('login-form');
      if (form) form.appendChild(errorEl);
    }

    errorEl.textContent = msg;
    errorEl.hidden = false;
  };

  /**
   * Toggle the submit button loading state.
   * @param {HTMLButtonElement} btn
   * @param {boolean} loading
   */
  const setLoading = (btn, loading) => {
    btn.disabled = loading;
    btn.dataset.originalText = btn.dataset.originalText ?? btn.textContent;
    btn.textContent = loading ? 'جارٍ تسجيل الدخول…' : btn.dataset.originalText;
    btn.classList.toggle('btn--loading', loading);
  };

  /* ─────────────────────────────────────────────
   * FORM HANDLER
   * ───────────────────────────────────────────── */
  const initLoginForm = () => {
    const form = document.getElementById('login-form');
    if (!form) return;

    const submitBtn = form.querySelector('[type="submit"]') ?? form.querySelector('button');

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      setError(null);

      const usernameInput = form.querySelector('#username') ?? form.querySelector('[name="username"]');
      const passwordInput = form.querySelector('#password') ?? form.querySelector('[name="password"]');

      const username = usernameInput?.value.trim() ?? '';
      const password = passwordInput?.value ?? '';

      // Basic client-side validation
      if (!username) {
        setError('الرجاء إدخال اسم المستخدم.');
        usernameInput?.focus();
        return;
      }
      if (!password) {
        setError('الرجاء إدخال كلمة المرور.');
        passwordInput?.focus();
        return;
      }

      if (submitBtn) setLoading(submitBtn, true);

      try {
        const res = await fetch('/api/v1/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'include',
          body: JSON.stringify({ username, username_or_email: username, password }),
        });

        // Attempt to parse JSON body for error details regardless of status
        let data = null;
        try {
          data = await res.json();
        } catch (_) {
          // Non-JSON response — handled below
        }

        if (res.ok) {
          // Redirect to the admin dashboard (or a "next" param if present)
          const params = new URLSearchParams(window.location.search);
          const next = params.get('next');
          // Validate "next" to prevent open-redirect
          const safePath = next && next.startsWith('/') && !next.startsWith('//') ? next : '/admin';
          window.location.href = safePath;
          return;
        }

        // Failure — surface a helpful Arabic error
        const errorDetail = data?.error && typeof data.error === 'object' ? data.error : null;
        const serverMsg =
          data?.message ??
          data?.detail ??
          (typeof data?.error === 'string' ? data.error : errorDetail?.message) ??
          null;

        if (res.status === 401 || res.status === 403) {
          setError('اسم المستخدم أو كلمة المرور غير صحيحة.');
        } else if (res.status === 429) {
          setError('تم تجاوز عدد المحاولات المسموح بها. يرجى المحاولة لاحقاً.');
        } else if (errorDetail?.code === 'SYSTEM_UNAVAILABLE') {
          setError('خدمة تسجيل الدخول غير متاحة مؤقتاً.');
        } else {
          setError(serverMsg ?? `حدث خطأ غير متوقع (${res.status}). حاول مجدداً.`);
        }

        // Clear password field on failure for security
        if (passwordInput) {
          passwordInput.value = '';
          passwordInput.focus();
        }
      } catch (err) {
        // Network / server unreachable
        console.error('[admin_login] Fetch error:', err);
        setError('تعذّر الاتصال بالخادم. يرجى التحقق من الاتصال بالإنترنت والمحاولة مجدداً.');
      } finally {
        if (submitBtn) setLoading(submitBtn, false);
      }
    });

    // Clear error when user starts typing again
    form.addEventListener('input', () => setError(null));
  };

  /* ─────────────────────────────────────────────
   * BOOT
   * ───────────────────────────────────────────── */
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initLoginForm);
  } else {
    initLoginForm();
  }
})();
