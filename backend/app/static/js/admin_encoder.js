document.addEventListener('DOMContentLoaded', () => {
  const form = document.getElementById('encoder-form');
  if (!form) return;
  const controls = document.getElementById('encoder-controls');
  const outputs = document.getElementById('encoder-output-options');
  const speed = document.getElementById('encoder-speed');
  const bitrate = document.getElementById('encoder-bitrate');
  const bitrateValue = document.getElementById('encoder-bitrate-value');
  const summary = document.getElementById('encoder-summary');
  const feedback = document.getElementById('encoder-feedback');
  const state = document.getElementById('encoder-save-state');
  const save = document.getElementById('encoder-save');
  const reset = document.getElementById('encoder-reset');
  const profiles = { fast: { preset: 'veryfast', video_bitrate_percent: 100 }, balanced: { preset: 'medium', video_bitrate_percent: 100 }, compact: { preset: 'slow', video_bitrate_percent: 75 } };
  let baseline = null;
  let saving = false;
  const current = () => ({ preset: speed.value, video_bitrate_percent: Number(bitrate.value), enabled_qualities: Array.from(outputs.querySelectorAll('input')).filter(input => input.checked && !input.disabled).map(input => input.value) });
  const csrf = () => document.cookie.split(';').map(part => part.trim().split('=')).find(([key]) => key === (document.body.dataset.csrfCookieName || 'aboodvideo_csrf'))?.[1] || '';
  function update() {
    if (!baseline) return;
    const value = current();
    const dirty = JSON.stringify(value) !== JSON.stringify(baseline);
    bitrateValue.textContent = `${value.video_bitrate_percent}%`;
    summary.textContent = `H.264 · ${value.preset} · ${value.video_bitrate_percent}% · ${value.enabled_qualities.join(' / ') || 'اختر دقة إخراج'}`;
    save.disabled = saving || !dirty || !value.enabled_qualities.length;
    reset.disabled = saving || !dirty;
    state.textContent = saving ? 'جارٍ الحفظ…' : dirty ? 'تغييرات غير محفوظة' : 'الإعداد محفوظ';
    form.querySelectorAll('[data-encoder-profile]').forEach(button => {
      const profile = profiles[button.dataset.encoderProfile];
      button.setAttribute('aria-pressed', String(value.preset === profile.preset && value.video_bitrate_percent === profile.video_bitrate_percent));
    });
  }
  function apply(value) {
    speed.value = value.preset;
    bitrate.value = String(value.video_bitrate_percent);
    outputs.querySelectorAll('input').forEach(input => { input.checked = !input.disabled && value.enabled_qualities.includes(input.value); });
    update();
  }
  form.querySelectorAll('[data-encoder-profile]').forEach(button => button.addEventListener('click', () => {
    const profile = profiles[button.dataset.encoderProfile];
    speed.value = profile.preset;
    bitrate.value = String(profile.video_bitrate_percent);
    feedback.textContent = '';
    update();
  }));
  controls.addEventListener('input', update);
  controls.addEventListener('change', update);
  reset.addEventListener('click', () => { apply(baseline); feedback.textContent = ''; });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (save.disabled || !baseline) return;
    const payload = current();
    saving = true; controls.disabled = true; update(); feedback.textContent = '';
    try {
      const response = await fetch('/api/v1/admin/quality-settings/encoder', { method: 'PUT', credentials: 'include', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() }, body: JSON.stringify(payload) });
      if (!response.ok) throw new Error(`تعذر حفظ إعداد الإنكودر (HTTP ${response.status}).`);
      const data = await response.json();
      baseline = { preset: data.preset, video_bitrate_percent: data.video_bitrate_percent, enabled_qualities: data.enabled_qualities };
      apply(baseline);
      feedback.textContent = 'تم الحفظ. ستستخدم مهام التحويل الجديدة هذا الإعداد تلقائيًا.';
    } catch (error) {
      feedback.textContent = error.message;
    } finally {
      saving = false; controls.disabled = false; update();
    }
  });
  window.addEventListener('beforeunload', event => {
    if (baseline && JSON.stringify(current()) !== JSON.stringify(baseline)) { event.preventDefault(); event.returnValue = ''; }
  });
  (async () => {
    try {
      const response = await fetch('/api/v1/admin/quality-settings/encoder', { credentials: 'include', cache: 'no-store' });
      if (!response.ok) throw new Error(`تعذر تحميل إعداد الإنكودر (HTTP ${response.status}).`);
      const data = await response.json();
      for (const preset of data.available_presets) {
        const option = document.createElement('option'); option.value = preset; option.textContent = preset; speed.appendChild(option);
      }
      for (const quality of data.available_qualities) {
        const label = document.createElement('label');
        const input = document.createElement('input'); input.type = 'checkbox'; input.value = quality; input.disabled = quality === '2160p' && !data.four_k_allowed;
        const text = document.createElement('span'); text.textContent = quality === '2160p' ? '2160p · 4K' : quality;
        label.append(input, text); outputs.appendChild(label);
      }
      baseline = { preset: data.preset, video_bitrate_percent: data.video_bitrate_percent, enabled_qualities: data.enabled_qualities };
      controls.disabled = false; apply(baseline);
    } catch (error) {
      feedback.textContent = error.message; state.textContent = 'الإعداد غير متاح';
    }
  })();
});
