/* ABOODVIDEO Admin UI V4 — API Keys Management Script */
document.addEventListener('DOMContentLoaded', () => {
    const tbody = document.getElementById('keys-tbody') || document.getElementById('apiKeysTableBody');
    const feedback = document.getElementById('keys-feedback');
    const totalCountEl = document.getElementById('keys-total-count') || document.getElementById('apiKeyCount');
    const pageInfoEl = document.getElementById('key-page-info');
    const btnPrev = document.getElementById('btn-key-prev');
    const btnNext = document.getElementById('btn-key-next');
    const searchInput = document.getElementById('filter-key-search');
    const statusSelect = document.getElementById('filter-key-status');
    const toggleTestKeys = document.getElementById('toggle-test-keys');

    let pendingRevoke = null;
    let pendingDelete = null;
    const selectedKeys = new Map();
    let currentPage = 1;
    const pageSize = 20;
    let totalPages = 1;
    let showTestKeys = false;
    let searchTerm = '';
    let statusFilter = '';
    let searchDebounceTimer = null;

    const el = (tag, text, cls = []) => {
        const e = document.createElement(tag);
        if (text !== '') e.textContent = String(text);
        if (cls.length) e.classList.add(...cls);
        return e;
    };

    const fmtDate = (iso) => {
        if (!iso) return '—';
        try {
            return new Date(iso).toLocaleDateString('ar', { year: 'numeric', month: 'short', day: 'numeric' });
        } catch (_) {
            return iso.slice(0, 10);
        }
    };

    const getCsrf = () => {
        for (const c of document.cookie.split(';')) {
            const [k, v] = c.trim().split('=');
            if (k === (document.body.dataset.csrfCookieName || 'aboodvideo_csrf')) return v;
        }
        return '';
    };

    const message = (text, isError = false) => {
        if (!feedback) return;
        feedback.textContent = text;
        feedback.className = `alert alert-${isError ? 'danger' : 'success'}`;
        feedback.classList.remove('hidden');
        window.clearTimeout(message._t);
        message._t = window.setTimeout(() => feedback.classList.add('hidden'), 5000);
    };

    const updateBulkToolbar = () => {
        const toolbar = document.getElementById('keys-bulk-toolbar');
        const count = document.getElementById('keys-selected-count');
        const button = document.getElementById('keys-bulk-delete');
        const selectedCount = selectedKeys.size;
        if (toolbar) toolbar.hidden = selectedCount === 0;
        if (count) count.textContent = `${selectedCount} مفتاح محدد`;
        if (button) button.disabled = selectedCount === 0;
    };

    const setModalOpen = (id, open) => {
        const modal = document.getElementById(id);
        if (!modal) return;
        modal.classList.toggle('is-open', open);
        modal.setAttribute('aria-hidden', String(!open));
    };

    async function fetchKeys() {
        try {
            const params = new URLSearchParams({
                page: String(currentPage),
                limit: String(pageSize),
                include_test: String(showTestKeys)
            });
            if (searchTerm) params.set('q', searchTerm);
            if (statusFilter) params.set('status', statusFilter);

            const res = await fetch(`/api/v1/admin/keys?${params.toString()}`, {
                credentials: 'include',
                cache: 'no-store'
            });

            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            const keys = data.keys || [];
            const total = data.total || 0;
            totalPages = Math.max(1, data.total_pages || Math.ceil(total / pageSize) || 1);

            if (totalCountEl) totalCountEl.textContent = `إجمالي المفاتيح: ${total}`;
            if (pageInfoEl) pageInfoEl.textContent = `صفحة ${currentPage} من ${totalPages}`;
            if (btnPrev) btnPrev.disabled = currentPage <= 1;
            if (btnNext) btnNext.disabled = currentPage >= totalPages;

            if (!keys.length) {
                tbody.innerHTML = '<tr><td colspan="8" class="table-empty-cell">لا توجد مفاتيح تطابق المعايير المحددة.</td></tr>';
                updateBulkToolbar();
                return;
            }

            tbody.replaceChildren(...keys.map(k => {
                const tr = document.createElement('tr');
                const isExpired = k.expires_at && new Date(k.expires_at) < new Date();
                const active = k.is_active && !isExpired;

                const statusBadge = active
                    ? el('span', 'نشط', ['badge', 'badge-quality'])
                    : el('span', isExpired ? 'منتهي الصلاحية' : 'ملغى', ['badge', 'badge-muted']);

                const actionsTd = el('td', '');
                if (k.is_active && !isExpired) {
                    const revokeBtn = el('button', 'إلغاء المفتاح', ['btn', 'btn-sm', 'btn-danger']);
                    revokeBtn.type = 'button';
                    revokeBtn.addEventListener('click', () => {
                        pendingRevoke = k.id;
                        const modal = document.getElementById('keys-confirm');
                        if (modal) modal.classList.add('is-open');
                    });
                    actionsTd.appendChild(revokeBtn);
                } else if (!k.is_active) {
                    const deleteBtn = el('button', 'حذف', ['btn', 'btn-sm', 'btn-danger']);
                    deleteBtn.type = 'button';
                    deleteBtn.addEventListener('click', () => {
                        pendingDelete = { id: k.id, name: k.name || 'بدون اسم', prefix: k.prefix || '' };
                        document.getElementById('deleteKeyName').textContent = pendingDelete.name;
                        document.getElementById('deleteKeyPrefix').textContent = `${pendingDelete.prefix}…`;
                        document.getElementById('deleteKeyStatus').textContent = 'ملغى';
                        document.getElementById('deleteKeyQuestion').textContent = 'هل تريد حذف هذا المفتاح من لوحة الإدارة؟';
                        setModalOpen('keys-delete-confirm', true);
                    });
                    actionsTd.appendChild(deleteBtn);
                } else {
                    actionsTd.textContent = '—';
                }

                const prefixTd = el('td', '');
                prefixTd.appendChild(el('span', (k.prefix || '') + '…', ['key-prefix']));

                const statusTd = el('td', '');
                statusTd.appendChild(statusBadge);

                const selectTd = el('td', '');
                const selectBox = document.createElement('input');
                selectBox.type = 'checkbox';
                selectBox.className = 'key-select-checkbox';
                selectBox.setAttribute('aria-label', `تحديد المفتاح ${k.name || ''}`);
                selectBox.disabled = Boolean(k.is_active);
                selectBox.checked = selectedKeys.has(k.id);
                selectBox.addEventListener('change', () => {
                    if (selectBox.checked && !k.is_active) selectedKeys.set(k.id, { id: k.id, name: k.name || 'بدون اسم', prefix: k.prefix || '' });
                    else selectedKeys.delete(k.id);
                    updateBulkToolbar();
                });
                selectTd.appendChild(selectBox);
                tr.append(
                    selectTd,
                    el('td', k.name || 'بدون اسم'),
                    prefixTd,
                    el('td', (k.scopes || []).join(', ') || '—'),
                    statusTd,
                    el('td', fmtDate(k.created_at)),
                    el('td', k.last_used_at ? fmtDate(k.last_used_at) : 'لم يُستخدم'),
                    actionsTd
                );
                return tr;
            }));
            updateBulkToolbar();
        } catch (err) {
            console.warn('Failed to load API keys', err?.message || 'request failed');
            tbody.innerHTML = '<tr><td colspan="8" class="table-empty-cell">تعذر تحميل قائمة المفاتيح.</td></tr>';
        }
    }

    // --- Toolbar Event Listeners ---
    if (searchInput) {
        searchInput.addEventListener('input', () => {
            clearTimeout(searchDebounceTimer);
            searchDebounceTimer = setTimeout(() => {
                searchTerm = searchInput.value.trim();
                currentPage = 1;
                fetchKeys();
            }, 300);
        });
    }

    if (statusSelect) {
        statusSelect.addEventListener('change', () => {
            statusFilter = statusSelect.value;
            currentPage = 1;
            fetchKeys();
        });
    }

    if (toggleTestKeys) {
        toggleTestKeys.addEventListener('change', () => {
            showTestKeys = toggleTestKeys.checked;
            currentPage = 1;
            fetchKeys();
        });
    }

    // --- Pagination Event Listeners ---
    if (btnPrev) {
        btnPrev.addEventListener('click', () => {
            if (currentPage > 1) {
                currentPage--;
                fetchKeys();
            }
        });
    }

    if (btnNext) {
        btnNext.addEventListener('click', () => {
            if (currentPage < totalPages) {
                currentPage++;
                fetchKeys();
            }
        });
    }

    const createModal = document.getElementById('createApiKeyModal');
    const createButton = document.getElementById('btnCreateApiKey');
    if (createButton && createModal) createButton.addEventListener('click', () => { createModal.classList.add('is-open'); createModal.setAttribute('aria-hidden','false'); document.getElementById('key-name')?.focus(); });
    document.querySelectorAll('[data-modal-close]').forEach(button => button.addEventListener('click', () => {
        const modal = document.getElementById(button.dataset.modalClose);
        if (!modal) return;
        modal.classList.remove('is-open'); modal.setAttribute('aria-hidden','true');
        if (modal.id === 'key-result') { const secret = document.getElementById('key-raw'); if (secret) secret.value = ''; modal.classList.add('hidden'); }
        if (modal.id === 'keys-delete-confirm') pendingDelete = null;
    }));
    document.querySelectorAll('.modal-overlay[role="dialog"]').forEach(modal => modal.addEventListener('click', e => { if (e.target === modal) { modal.classList.remove('is-open'); modal.setAttribute('aria-hidden','true'); if (modal.id === 'key-result') { const secret = document.getElementById('key-raw'); if (secret) secret.value = ''; modal.classList.add('hidden'); } } }));
    document.getElementById('key-result')?.classList.add('hidden');

    // --- Studio Preset Handler ---
    const btnPresetStudio = document.getElementById('btn-preset-studio');
    if (btnPresetStudio) {
        btnPresetStudio.addEventListener('click', () => {
            const studioScopes = new Set(['packages:create', 'packages:write', 'packages:publish']);
            document.querySelectorAll('.key-scope').forEach(cb => {
                cb.checked = studioScopes.has(cb.value);
            });
        });
        if (btnPresetStudio.checked) btnPresetStudio.click();
    }

    // --- Create Key Handler ---
    const keyForm = document.getElementById('key-form');
    if (keyForm) {
        keyForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const name = document.getElementById('key-name').value.trim();
            if (!name) {
                message('يرجى إدخال اسم للمفتاح.', true);
                return;
            }
            const scopes = [...document.querySelectorAll('.key-scope:checked')].map(cb => cb.value);
            if (!scopes.length) {
                message('اختر صلاحية واحدة على الأقل للمفتاح.', true);
                return;
            }
            const expiry = document.getElementById('key-expiry').value;
            const payload = { name, scopes };
            if (expiry) payload.expires_in_days = Number(expiry);

            const btn = document.getElementById('key-create-btn');
            btn.disabled = true;
            try {
                const res = await fetch('/api/v1/admin/keys', {
                    method: 'POST',
                    credentials: 'include',
                    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrf() },
                    body: JSON.stringify(payload),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(data.detail || 'خطأ غير معروف');

                document.getElementById('key-raw').value = data.raw_key;
                document.getElementById('key-result').classList.remove('hidden');
                document.getElementById('key-result').classList.add('is-open');
                document.getElementById('key-result').setAttribute('aria-hidden','false');
                document.getElementById('createApiKeyModal')?.classList.remove('is-open');
                document.getElementById('createApiKeyModal')?.setAttribute('aria-hidden','true');
                message('تم إنشاء المفتاح بنجاح — انسخ الرمز السري الآن.');
                currentPage = 1;
                fetchKeys();
            } catch (err) {
                message('تعذر إنشاء المفتاح: ' + err.message, true);
            } finally {
                btn.disabled = false;
            }
        });
    }

    // --- Copy Key Handler ---
    const btnCopy = document.getElementById('key-copy');
    if (btnCopy) {
        btnCopy.addEventListener('click', () => {
            const key = document.getElementById('key-raw').value;
            if (key) {
                navigator.clipboard.writeText(key).then(
                    () => message('تم نسخ المفتاح إلى الحافظة بنجاح.'),
                    () => message('تعذر النسخ إلى الحافظة.', true)
                );
            }
        });
    }

    // --- Revoke Confirmation Handlers ---
    const btnConfirmNo = document.getElementById('keys-confirm-no');
    if (btnConfirmNo) {
        btnConfirmNo.addEventListener('click', () => {
            pendingRevoke = null;
            document.getElementById('keys-confirm').classList.remove('is-open');
        });
    }

    const btnConfirmYes = document.getElementById('keys-confirm-yes');
    if (btnConfirmYes) {
        btnConfirmYes.addEventListener('click', async () => {
            if (!pendingRevoke) return;
            const id = pendingRevoke;
            pendingRevoke = null;
            document.getElementById('keys-confirm').classList.remove('is-open');
            try {
                const res = await fetch(`/api/v1/admin/keys/${id}/revoke`, {
                    method: 'POST',
                    credentials: 'include',
                    headers: { 'X-CSRF-Token': getCsrf() },
                });
                if (res.ok) {
                    message('تم إلغاء صلاحية المفتاح بنجاح.');
                    fetchKeys();
                } else {
                    message('تعذر إلغاء المفتاح.', true);
                }
            } catch (_) {
                message('تعذر الاتصال بالخادم أثناء إلغاء المفتاح.', true);
            }
        });
    }

    document.getElementById('keys-delete-no')?.addEventListener('click', () => { pendingDelete = null; setModalOpen('keys-delete-confirm', false); });
    document.getElementById('keys-delete-yes')?.addEventListener('click', async () => {
        if (!pendingDelete) return;
        const item = pendingDelete;
        const button = document.getElementById('keys-delete-yes');
        button.disabled = true;
        try {
            const res = await fetch(`/api/v1/admin/keys/${encodeURIComponent(item.id)}`, {
                method: 'DELETE', credentials: 'include', headers: { 'X-CSRF-Token': getCsrf() }
            });
            if (!res.ok) {
                if (res.status === 409) message('يجب إلغاء المفتاح أولاً قبل حذفه.', true);
                else message('تعذر حذف المفتاح', true);
                return;
            }
            selectedKeys.delete(item.id);
            pendingDelete = null;
            setModalOpen('keys-delete-confirm', false);
            message('تم حذف مفتاح API');
            await fetchKeys();
        } catch (_) { message('تعذر حذف المفتاح', true); }
        finally { button.disabled = false; }
    });

    document.getElementById('keys-bulk-delete')?.addEventListener('click', () => {
        if (!selectedKeys.size) return;
        const n = selectedKeys.size;
        document.getElementById('bulkDeleteQuestion').textContent = `حذف ${n} مفاتيح API الملغاة؟`;
        setModalOpen('keys-bulk-confirm', true);
    });
    document.getElementById('keys-bulk-no')?.addEventListener('click', () => setModalOpen('keys-bulk-confirm', false));
    document.getElementById('keys-bulk-yes')?.addEventListener('click', async () => {
        const ids = [...selectedKeys.keys()];
        if (!ids.length) return;
        const button = document.getElementById('keys-bulk-yes');
        button.disabled = true;
        try {
            const res = await fetch('/api/v1/admin/keys/bulk-delete', {
                method: 'POST', credentials: 'include',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': getCsrf() },
                body: JSON.stringify({ key_ids: ids })
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                if (res.status === 409) message('يجب إلغاء المفاتيح النشطة أولاً قبل حذفها.', true);
                else message('تعذر حذف المفتاح', true);
                return;
            }
            ids.forEach(id => selectedKeys.delete(id));
            setModalOpen('keys-bulk-confirm', false);
            message(`تم حذف ${data.deleted_count || ids.length} مفاتيح API`);
            await fetchKeys();
        } catch (_) { message('تعذر حذف المفتاح', true); }
        finally { button.disabled = false; }
    });

    // Initial Load
    fetchKeys();
});
