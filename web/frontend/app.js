// change-bios-logo Web 版 — 前端逻辑
// 工作流：载入 BIOS → 扫描（拿 session + 原图）→ 上传新 Logo → 预览适配 → 替换下载
const API = {
  scan: '/api/scan',
  preview: (sid) => `/api/preview/${sid}`,
  replace: (sid) => `/api/replace/${sid}`,
  health: '/api/health',
};

let sessionId = null;   // 后端会话 ID（载入 BIOS 后由 /api/scan 返回）
let scanData = null;    // /api/scan 的返回（段信息 + 原图 base64）

// ---- DOM 引用 -------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const biosFileInput = $('bios-file');
const logoFileInput = $('logo-file');
const btnScan = $('btn-scan');
const btnPreview = $('btn-preview');
const btnReplace = $('btn-replace');
const scanResultDiv = $('scan-result');
const previewResultDiv = $('preview-result');
const downloadLink = $('download-link');
const logEl = $('log');
const outputSizeSelect = $('output-size');
const customSizeLabel = $('custom-size-label');
const customWidthInput = $('custom-width');
const customHeightInput = $('custom-height');
const colorDepthSelect = $('color-depth');
const autoTrimCheckbox = $('auto-trim');
const autoShrinkCheckbox = $('auto-shrink');

// ---- 日志 -----------------------------------------------------------------
function log(msg, cls = '') {
  const time = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  const line = `[${time}] ${msg}`;
  logEl.textContent += (logEl.textContent ? '\n' : '') + line;
  logEl.scrollTop = logEl.scrollHeight;
}

// ---- 按钮状态 -------------------------------------------------------------
function updateButtons() {
  btnScan.disabled = !biosFileInput.files.length;
  const canAdapt = !!sessionId && !!logoFileInput.files.length;
  btnPreview.disabled = !canAdapt;
  btnReplace.disabled = !canAdapt;
}

// ---- 文件选择 -------------------------------------------------------------
biosFileInput.addEventListener('change', () => {
  sessionId = null;
  scanData = null;
  scanResultDiv.classList.add('hidden');
  previewResultDiv.classList.add('hidden');
  downloadLink.classList.add('hidden');
  updateButtons();
  const f = biosFileInput.files[0];
  log(`已选择 BIOS 文件：${f ? f.name : '（无）'}`);
});

logoFileInput.addEventListener('change', () => {
  previewResultDiv.classList.add('hidden');
  downloadLink.classList.add('hidden');
  updateButtons();
  const f = logoFileInput.files[0];
  log(`已选择新 Logo：${f ? f.name : '（无）'}`);
});

// ---- 适配参数 -------------------------------------------------------------
outputSizeSelect.addEventListener('change', () => {
  const isCustom = outputSizeSelect.value === 'custom';
  customSizeLabel.classList.toggle('hidden', !isCustom);
});

function collectAdaptParams() {
  const p = {
    output_size: outputSizeSelect.value,
    color_depth: colorDepthSelect.value,
    auto_trim: autoTrimCheckbox.checked,
    auto_shrink: autoShrinkCheckbox.checked,
  };
  if (p.output_size === 'custom') {
    p.custom_width = parseInt(customWidthInput.value, 10) || 0;
    p.custom_height = parseInt(customHeightInput.value, 10) || 0;
  }
  return p;
}

function appendAdaptToForm(form) {
  const p = collectAdaptParams();
  form.append('output_size', p.output_size);
  form.append('color_depth', p.color_depth);
  form.append('auto_trim', p.auto_trim ? '1' : '0');
  form.append('auto_shrink', p.auto_shrink ? '1' : '0');
  if (p.output_size === 'custom') {
    form.append('custom_width', String(p.custom_width));
    form.append('custom_height', String(p.custom_height));
  }
}

// ---- 步骤 1：扫描 ---------------------------------------------------------
btnScan.addEventListener('click', async () => {
  const file = biosFileInput.files[0];
  if (!file) return;
  log(`开始扫描 ${file.name}（${(file.size / 1048576).toFixed(1)} MB）…`);
  btnScan.disabled = true;
  try {
    const form = new FormData();
    form.append('file', file);
    const resp = await fetch(API.scan, { method: 'POST', body: form });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}：${await resp.text()}`);
    const data = await resp.json();
    sessionId = data.session_id;
    scanData = data;
    const n = (data.slots || []).length;
    log(`扫描完成：找到 ${n} 个 Logo 段`);
    renderScanResult(data);
    updateButtons();
  } catch (e) {
    log(`扫描失败：${e.message}`, 'err');
  } finally {
    btnScan.disabled = !biosFileInput.files.length;
  }
});

// ---- 步骤 2/4：预览 & 替换（共用适配参数）---------------------------------
async function doAdapt(isReplace) {
  const file = logoFileInput.files[0];
  if (!sessionId || !file) return;
  const verb = isReplace ? '替换并打包' : '预览适配';
  log(`开始${verb}…`);
  (isReplace ? btnReplace : btnPreview).disabled = true;
  try {
    const form = new FormData();
    form.append('file', file);
    appendAdaptToForm(form);
    const url = isReplace ? API.replace(sessionId) : API.preview(sessionId);
    const resp = await fetch(url, { method: 'POST', body: form });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}：${await resp.text()}`);

    if (isReplace) {
      // 替换：响应是文件下载
      const blob = await resp.blob();
      const cd = resp.headers.get('Content-Disposition') || '';
      const m = cd.match(/filename="?([^"]*?)"/);
      const filename = m ? m[1] : 'new-bios.bin';
      const url2 = URL.createObjectURL(blob);
      downloadLink.href = url2;
      downloadLink.download = filename;
      downloadLink.textContent = `下载新 BIOS 文件（${filename}，${(blob.size / 1048576).toFixed(1)} MB）`;
      downloadLink.classList.remove('hidden');
      log(`替换完成：${filename}`);
    } else {
      // 预览：响应是 JSON（含适配后图片 base64）
      const data = await resp.json();
      log('预览完成');
      renderPreviewResult(data);
    }
    updateButtons();
  } catch (e) {
    log(`${verb}失败：${e.message}`, 'err');
  } finally {
    const canAdapt = !!sessionId && !!logoFileInput.files.length;
    (isReplace ? btnReplace : btnPreview).disabled = !canAdapt;
  }
}
btnPreview.addEventListener('click', () => doAdapt(false));
btnReplace.addEventListener('click', () => doAdapt(true));

// ---- 渲染 -----------------------------------------------------------------
function hex(n) {
  if (n == null) return '?';
  return '0x' + Number(n).toString(16).toUpperCase().padStart(8, '0');
}

function renderScanResult(data) {
  const slots = data.slots || [];
  let html = '';
  if (!slots.length) {
    html += '<p class="status warn">未找到 Logo 段（该固件可能不含标准 UEFI Logo，或结构不匹配）。</p>';
  } else {
    html += '<div class="slot-list">';
    for (const s of slots) {
      html += `<div class="slot-item">
        <div class="slot-title">${esc(s.name || 'Logo 段')}</div>
        <div class="slot-meta">
          偏移 <b>${hex(s.offset)}</b> · 段大小 <b>${s.size ?? '?'} 字节</b> ·
          原图 <b>${s.original_width ?? '?'}×${s.original_height ?? '?'}</b> ·
          位深 <b>${s.original_depth ?? '?'} 位</b>
        </div>
      </div>`;
    }
    html += '</div>';
  }
  if (data.original_logo_b64) {
    html += `<div class="preview-wrap" style="margin-top:14px">
      <div class="preview-box">
        <div class="label">原 Logo（固件内当前内容）</div>
        <img src="data:image/png;base64,${data.original_logo_b64}" alt="原 Logo">
      </div>
    </div>`;
  }
  scanResultDiv.innerHTML = html;
  scanResultDiv.classList.remove('hidden');
}

function renderPreviewResult(data) {
  let html = '<div class="preview-wrap">';
  if (data.preview_b64) {
    html += `<div class="preview-box">
      <div class="label">适配后（将写入固件）</div>
      <img src="data:image/png;base64,${data.preview_b64}" alt="适配后">
      <div class="preview-meta">${data.preview_width ?? '?'}×${data.preview_height ?? '?'} · ${data.preview_depth ?? '?'} 位</div>
    </div>`;
  }
  html += '</div>';
  if (data.warnings && data.warnings.length) {
    html += `<p class="status warn">${esc(data.warnings.join('；'))}</p>`;
  }
  previewResultDiv.innerHTML = html;
  previewResultDiv.classList.remove('hidden');
}

function esc(s) {
  return String(s).replace(/[&<>&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' }[c]));
}

// ---- 启动 -----------------------------------------------------------------
updateButtons();
log('Web 版已加载。请载入 BIOS 文件开始。');
