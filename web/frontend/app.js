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

// 工作图：上传原图作裁剪底图，裁剪结果作为后续 preview/replace 的工作图
// （对齐桌面版：new_image_raw 始终保留原图，new_image 是工作图）
let logoRawDataUrl = null;   // 上传原图（data URL），作裁剪底图
let logoRawImage = null;     // 上传原图（Image 对象），用于拿尺寸
let logoWorkingFile = null;  // 当前工作图（File），初始=原图，裁剪后=裁剪结果
let logoWorkingName = '';    // 工作图名称

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
const slotSelect = $('slot-select');
const fitModeSelect = $('fit-mode');
const zoomSlider = $('zoom-slider');
const zoomLabel = $('zoom-label');
const btnFitReset = $('btn-fit-reset');
const fitTip = $('fit-tip');
const btnCrop = $('btn-crop');
const cropOverlay = $('crop-overlay');
const cropCanvas = $('crop-canvas');
const cropLock = $('crop-lock');
const cropAspectEl = $('crop-aspect');
const cropSelAll = $('crop-sel-all');
const cropSelAspect = $('crop-sel-aspect');
const cropSelContent = $('crop-sel-content');
const cropRestore = $('crop-restore');
const cropSizeEl = $('crop-size');
const cropOk = $('crop-ok');
const cropCancel = $('crop-cancel');

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
  const hasLogo = !!logoFileInput.files.length;
  const canAdapt = !!sessionId && hasLogo;
  btnPreview.disabled = !canAdapt;
  btnReplace.disabled = !canAdapt;
  btnCrop.disabled = !hasLogo;
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
  const f = logoFileInput.files[0];
  if (f) {
    // 工作图初始=原图；同时读原图为 data URL（作裁剪底图）
    logoWorkingFile = f;
    logoWorkingName = f.name;
    logoRawDataUrl = null;
    logoRawImage = null;
    const reader = new FileReader();
    reader.onload = () => {
      logoRawDataUrl = reader.result;
      logoRawImage = new Image();
      logoRawImage.onload = () => { /* 尺寸就绪，供 source 模式 aspect 计算 */ };
      logoRawImage.src = reader.result;
    };
    reader.readAsDataURL(f);
    log(`已选择新 Logo：${f.name}`);
  } else {
    logoRawDataUrl = null;
    logoRawImage = null;
    logoWorkingFile = null;
    logoWorkingName = '';
    log('已选择新 Logo：（无）');
  }
  updateButtons();
});

// ---- 适配参数 -------------------------------------------------------------
// 适配方式提示（与桌面版 FIT_TIPS 对齐）
const FIT_TIPS = {
  contain: '完整显示：整张图按比例缩放放进目标框，不变形；两侧可能留黑边。',
  cover: '铺满裁剪：铺满目标框并裁掉超出部分，不变形；可能裁掉图片边缘。',
  stretch: '拉伸填满：直接拉伸到目标框大小，宽高比不一致时可能变形。',
};

function updateFitTip() {
  const mode = fitModeSelect.value;
  fitTip.textContent = FIT_TIPS[mode] || FIT_TIPS.contain;
}

function updateZoomLabel() {
  const v = parseInt(zoomSlider.value, 10) / 100;
  zoomLabel.textContent = v.toFixed(2) + '×';
}

outputSizeSelect.addEventListener('change', () => {
  const isCustom = outputSizeSelect.value === 'custom';
  customSizeLabel.classList.toggle('hidden', !isCustom);
  previewResultDiv.classList.add('hidden');
});

fitModeSelect.addEventListener('change', () => {
  updateFitTip();
  previewResultDiv.classList.add('hidden');
});

zoomSlider.addEventListener('input', () => {
  updateZoomLabel();
  previewResultDiv.classList.add('hidden');
});

btnFitReset.addEventListener('click', () => {
  fitModeSelect.value = 'contain';
  zoomSlider.value = '100';
  updateFitTip();
  updateZoomLabel();
  previewResultDiv.classList.add('hidden');
  log('已重置适配方式与缩放');
});

// 目标 Logo 段选择：切换时切换原图预览
slotSelect.addEventListener('change', () => {
  updateOriginalPreview(parseInt(slotSelect.value, 10));
  previewResultDiv.classList.add('hidden');
});

function collectAdaptParams() {
  const p = {
    output_size: outputSizeSelect.value,
    color_depth: colorDepthSelect.value,
    auto_trim: autoTrimCheckbox.checked,
    auto_shrink: autoShrinkCheckbox.checked,
    fit_mode: fitModeSelect.value,
    zoom: parseInt(zoomSlider.value, 10) / 100,
    slot_index: parseInt(slotSelect.value, 10) || 0,
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
  form.append('fit_mode', p.fit_mode);
  form.append('zoom', String(p.zoom));
  form.append('slot_index', String(p.slot_index));
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
  const file = logoWorkingFile || logoFileInput.files[0];
  if (!sessionId || !file) return;
  const verb = isReplace ? '替换并打包' : '预览适配';
  log(`开始${verb}…`);
  (isReplace ? btnReplace : btnPreview).disabled = true;
  try {
    const form = new FormData();
    form.append('file', file, file.name || 'logo.png');
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
  // 填充「目标 Logo 段」选择器
  if (slots.length) {
    slotSelect.innerHTML = '';
    for (const s of slots) {
      const opt = document.createElement('option');
      opt.value = String(s.index);
      opt.textContent = `${s.index + 1}. ${s.name || 'Logo 段'}（${s.original_width ?? '?'}×${s.original_height ?? '?'}）`;
      slotSelect.appendChild(opt);
    }
    slotSelect.value = slotSelect.options[0].value;
  } else {
    slotSelect.innerHTML = '<option value="0">— 无 Logo 段 —</option>';
  }
  // 渲染段列表 + 原图预览容器
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
    html += '<div class="preview-wrap" style="margin-top:14px"><div class="preview-box" id="original-preview"></div></div>';
  }
  scanResultDiv.innerHTML = html;
  scanResultDiv.classList.remove('hidden');
  // 初始显示选中段的原图
  updateOriginalPreview(parseInt(slotSelect.value, 10) || 0);
}

// 切换「目标 Logo 段」时，更新原图预览为对应段
function updateOriginalPreview(slotIndex) {
  const box = document.getElementById('original-preview');
  if (!box || !scanData) return;
  const slots = scanData.slots || [];
  const s = slots.find((x) => x.index === slotIndex) || slots[0];
  if (!s) return;
  if (s.original_b64) {
    box.innerHTML = `<div class="label">原 Logo（固件内当前内容，段 ${s.index + 1}）</div>
      <img src="data:image/png;base64,${s.original_b64}" alt="原 Logo">
      <div class="preview-meta">${s.original_width ?? '?'}×${s.original_height ?? '?'} · ${s.original_depth ?? '?'} 位</div>`;
  } else {
    box.innerHTML = '<div class="label">原 Logo（固件内当前内容）</div><div class="preview-meta">（该段原图暂不可用）</div>';
  }
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

// ---- 步骤 3：裁剪图片（手动框选，对齐桌面版 CropDialog / CropCanvas）-------
// 裁剪对话框状态
let cropBaseImage = null;    // 底图 Image 对象（上传原图）
let cropSel = null;          // 选区（原图像素坐标 [x0,y0,x1,y1]）
let cropMap = { s: 1, ox: 0, oy: 0 };  // 逻辑→显示像素映射
let cropDragging = false;
let cropAnchor = null;       // 拖拽起点（原图像素坐标）
let cropTargetSize = null;   // 目标输出尺寸 (w, h) 或 null
let cropScale = 1;           // 原图→逻辑显示 缩放系数

// 计算目标输出尺寸（对齐桌面版 _out_size）
function getTargetOutputSize() {
  const mode = outputSizeSelect.value;
  const slots = (scanData && scanData.slots) || [];
  const slot = slots.find((x) => x.index === (parseInt(slotSelect.value, 10) || 0));
  if (mode === 'orig') {
    if (slot && slot.original_width && slot.original_height) {
      return [slot.original_width, slot.original_height];
    }
    return [720, 480];
  }
  if (mode === 'source') {
    if (logoRawImage && logoRawImage.width) {
      return [logoRawImage.width, logoRawImage.height];
    }
    return [720, 480];
  }
  if (mode === 'fixed') {
    return [720, 480];
  }
  if (mode === 'custom') {
    const w = parseInt(customWidthInput.value, 10) || 0;
    const h = parseInt(customHeightInput.value, 10) || 0;
    if (w >= 8 && w <= 8192 && h >= 8 && h <= 8192) {
      return [w, h];
    }
    return [720, 480];
  }
  return [720, 480];
}

// 打开裁剪对话框
function openCropDialog() {
  if (!logoWorkingFile) {
    log('请先上传新的 Logo 图片，再裁剪。', 'warn');
    return;
  }
  // 底图始终用上传原图（对齐桌面版：每次裁剪都是对原图的独立框选）
  const baseSrc = logoRawDataUrl;
  if (!baseSrc) {
    log('裁剪底图尚未加载完成，请稍候再试。', 'warn');
    return;
  }
  cropBaseImage = new Image();
  cropBaseImage.onload = () => {
    setupCropCanvas();
    cropOverlay.classList.remove('hidden');
  };
  cropBaseImage.onerror = () => {
    log('裁剪底图加载失败。', 'err');
    cropBaseImage = null;
  };
  cropBaseImage.src = baseSrc;
}

// 初始化裁剪画布（对齐桌面版 CropCanvas._update_map）
function setupCropCanvas() {
  const img = cropBaseImage;
  const DW = 640, DH = 400;
  cropCanvas.width = DW;
  cropCanvas.height = DH;
  // 原图→逻辑显示 缩放系数（不放大，对齐桌面版 scale = min(MAX_W/w, MAX_H/H, 1.0)）
  cropScale = Math.min(DW / img.width, DH / img.height, 1.0);
  // 逻辑显示尺寸
  const dw = Math.max(1, Math.round(img.width * cropScale));
  const dh = Math.max(1, Math.round(img.height * cropScale));
  // 居中偏移
  cropMap = {
    s: cropScale,
    ox: (DW - dw) / 2,
    oy: (DH - dh) / 2,
  };
  // 初始选区 = 全图（原图像素坐标）
  cropSel = [0, 0, img.width, img.height];
  cropDragging = false;
  cropAnchor = null;
  // 目标输出尺寸（aspect）
  cropTargetSize = getTargetOutputSize();
  // 锁定比例：有 aspect 时默认勾选（对齐桌面版）
  cropLock.checked = !!cropTargetSize;
  cropAspectEl.textContent = cropTargetSize ? ` ${cropTargetSize[0]}:${cropTargetSize[1]}` : '';
  drawCropCanvas();
  updateCropSize();
}

// 绘制裁剪画布（对齐桌面版 CropCanvas.paintEvent）
function drawCropCanvas() {
  const ctx = cropCanvas.getContext('2d');
  const img = cropBaseImage;
  if (!img || !cropSel) return;
  const { s, ox, oy } = cropMap;
  const DW = cropCanvas.width, DH = cropCanvas.height;
  // 1) 黑底
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, DW, DH);
  // 2) 图（等比缩放 + 居中）
  const pw = img.width * s, ph = img.height * s;
  ctx.drawImage(img, ox, oy, pw, ph);
  // 3) 选区外压暗（选区外、图范围内的区域）
  const [x0, y0, x1, y1] = cropSel;
  const mx0 = ox + x0 * s, my0 = oy + y0 * s;
  const mx1 = ox + x1 * s, my1 = oy + y1 * s;
  ctx.fillStyle = 'rgba(0,0,0,0.55)';
  ctx.fillRect(ox, oy, pw, my0 - oy);          // 上
  ctx.fillRect(ox, my1, pw, oy + ph - my1);    // 下
  ctx.fillRect(ox, my0, mx0 - ox, my1 - my0);  // 左
  ctx.fillRect(mx1, my0, ox + pw - mx1, my1 - my0); // 右
  // 4) 选区边框
  ctx.strokeStyle = '#00c2ff';
  ctx.lineWidth = 2;
  ctx.strokeRect(mx0, my0, mx1 - mx0, my1 - my0);
}

// 更新裁剪尺寸标签（对齐桌面版 CropDialog._draw）
function updateCropSize() {
  if (!cropSel || !cropBaseImage) return;
  const [x0, y0, x1, y1] = cropSel;
  const sw = Math.max(1, Math.round((x1 - x0) / cropScale));
  const sh = Math.max(1, Math.round((y1 - y0) / cropScale));
  let target = '';
  if (cropTargetSize) {
    target = ` → 会等比缩放进 ${cropTargetSize[0]}×${cropTargetSize[1]} 的画布`;
  } else {
    target = ' → 会缩放进目标画布';
  }
  cropSizeEl.textContent = `裁剪区域：${sw} × ${sh} 像素（原图 ${cropBaseImage.width} × ${cropBaseImage.height}）${target}`;
}

// 显示像素 → 原图像素坐标（对齐桌面版 CropCanvas._to_logical）
function cropToLogical(px, py) {
  const { s, ox, oy } = cropMap;
  let x = (px - ox) / s, y = (py - oy) / s;
  x = Math.min(Math.max(x, 0), cropBaseImage.width);
  y = Math.min(Math.max(y, 0), cropBaseImage.height);
  return [x, y];
}

// 获取鼠标在画布内的坐标（内部分辨率坐标，考虑 CSS 缩放）
function canvasPos(ev) {
  const r = cropCanvas.getBoundingClientRect();
  const scaleX = r.width ? cropCanvas.width / r.width : 1;
  const scaleY = r.height ? cropCanvas.height / r.height : 1;
  return [(ev.clientX - r.left) * scaleX, (ev.clientY - r.top) * scaleY];
}

// 按目标比例约束选区（对齐桌面版 CropDialog._fit_aspect）
function fitAspect(r, bx, by) {
  const ar = cropTargetSize[0] / cropTargetSize[1];
  let w = Math.max(4.0, r[2] - r[0]);
  let h = Math.max(4.0, r[3] - r[1]);
  if (w / h > ar) {
    h = w / ar;
  } else {
    w = h * ar;
  }
  const ax = cropAnchor[0], ay = cropAnchor[1];
  let x0 = bx >= ax ? ax : ax - w;
  let y0 = by >= ay ? ay : ay - h;
  let x1 = x0 + w, y1 = y0 + h;
  if (x0 < 0) { x1 -= x0; x0 = 0; }
  if (y0 < 0) { y1 -= y0; y0 = 0; }
  if (x1 > cropBaseImage.width) { x0 -= (x1 - cropBaseImage.width); x1 = cropBaseImage.width; }
  if (y1 > cropBaseImage.height) { y0 -= (y1 - cropBaseImage.height); y1 = cropBaseImage.height; }
  return [Math.max(0, x0), Math.max(0, y0), x1, y1];
}

// 关闭裁剪对话框
function closeCropDialog() {
  cropOverlay.classList.add('hidden');
  cropBaseImage = null;
  cropSel = null;
  cropDragging = false;
  cropAnchor = null;
}

// ---- 裁剪对话框事件绑定 -----------------------------------------------------
btnCrop.addEventListener('click', () => openCropDialog());

cropCanvas.addEventListener('mousedown', (ev) => {
  if (ev.button !== 0) return;  // 左键
  const [px, py] = canvasPos(ev);
  const [lx, ly] = cropToLogical(px, py);
  cropDragging = true;
  cropAnchor = [lx, ly];
  cropSel = [lx, ly, lx, ly];
  drawCropCanvas();
  updateCropSize();
  ev.preventDefault();
});

cropCanvas.addEventListener('mousemove', (ev) => {
  if (!cropDragging) return;
  const [px, py] = canvasPos(ev);
  const [lx, ly] = cropToLogical(px, py);
  const [ax, ly0] = cropAnchor;
  let x0 = Math.min(ax, lx), y0 = Math.min(ly0, ly);
  let x1 = Math.max(ax, lx), y1 = Math.max(ly0, ly);
  // 锁定比例约束
  if (cropLock.checked && cropTargetSize) {
    [x0, y0, x1, y1] = fitAspect([x0, y0, x1, y1], lx, ly);
  }
  cropSel = [x0, y0, x1, y1];
  drawCropCanvas();
  updateCropSize();
  ev.preventDefault();
});

window.addEventListener('mouseup', () => {
  if (!cropDragging) return;
  cropDragging = false;
  // 选区太小则还原全图（对齐桌面版：释放时 <4px 则还原）
  if (cropSel && (cropSel[2] - cropSel[0] < 4 || cropSel[3] - cropSel[1] < 4)) {
    cropSel = [0, 0, cropBaseImage.width, cropBaseImage.height];
    drawCropCanvas();
    updateCropSize();
  }
});

// 全选
cropSelAll.addEventListener('click', () => {
  if (!cropBaseImage) return;
  cropSel = [0, 0, cropBaseImage.width, cropBaseImage.height];
  drawCropCanvas();
  updateCropSize();
});

// 按目标比例
cropSelAspect.addEventListener('click', () => {
  if (!cropBaseImage) return;
  if (!cropTargetSize) {
    cropSel = [0, 0, cropBaseImage.width, cropBaseImage.height];
    drawCropCanvas();
    updateCropSize();
    return;
  }
  const ar = cropTargetSize[0] / cropTargetSize[1];
  const W = cropBaseImage.width, H = cropBaseImage.height;
  let w = W, h = w / ar;
  if (h > H) {
    h = H;
    w = h * ar;
  }
  const x0 = (W - w) / 2, y0 = (H - h) / 2;
  cropSel = [x0, y0, x0 + w, y0 + h];
  drawCropCanvas();
  updateCropSize();
});

// 去黑边（对齐桌面版 CropDialog._sel_content + bioslogo.content_bbox）
cropSelContent.addEventListener('click', () => {
  if (!cropBaseImage) return;
  // 用已显示的缩放画布算非黑内容的外接矩形（快），再映射回原图坐标
  const c = document.createElement('canvas');
  const DW = cropCanvas.width, DH = cropCanvas.height;
  c.width = DW;
  c.height = DH;
  const ctx = c.getContext('2d');
  const { s, ox, oy } = cropMap;
  const pw = cropBaseImage.width * s, ph = cropBaseImage.height * s;
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, DW, DH);
  ctx.drawImage(cropBaseImage, ox, oy, pw, ph);
  const data = ctx.getImageData(0, 0, DW, DH).data;
  const THRESHOLD = 10;
  let l = DW, t = DH, r = -1, b = 0;
  for (let y = 0; y < DH; y++) {
    for (let x = 0; x < DW; x++) {
      const i = (y * DW + x) * 4;
      const lum = 0.299 * data[i] + 0.587 * data[i+1] + 0.114 * data[i+2];
      if (lum > THRESHOLD) {
        if (x < l) l = x;
        if (x > r) r = x;
        if (y < t) t = y;
        if (y > b) b = y;
      }
    }
  }
  if (r < l || b < t) {
    // 全黑，还原全图
    cropSel = [0, 0, cropBaseImage.width, cropBaseImage.height];
  } else {
    // 显示像素坐标 → 原图像素坐标
    let x0 = (l - ox) / s, y0 = (t - oy) / s;
    let x1 = (r + 1 - ox) / s, y1 = (b + 1 - oy) / s;
    x0 = Math.min(Math.max(x0, 0), cropBaseImage.width);
    y0 = Math.min(Math.max(y0, 0), cropBaseImage.height);
    x1 = Math.min(Math.max(x1, 0), cropBaseImage.width);
    y1 = Math.min(Math.max(y1, 0), cropBaseImage.height);
    // 锁定比例约束
    if (cropLock.checked && cropTargetSize) {
      cropAnchor = [x0, y0];
      [x0, y0, x1, y1] = fitAspect([x0, y0, x1, y1], x1, y1);
    }
    cropSel = [x0, y0, x1, y1];
  }
  drawCropCanvas();
  updateCropSize();
});

// 还原
cropRestore.addEventListener('click', () => {
  if (!cropBaseImage) return;
  cropSel = [0, 0, cropBaseImage.width, cropBaseImage.height];
  drawCropCanvas();
  updateCropSize();
});

// 确定（裁剪并导出为工作图，对齐桌面版 CropDialog._ok + _finish_crop）
cropOk.addEventListener('click', () => {
  if (!cropSel || !cropBaseImage) return;
  const [x0, y0, x1, y1] = cropSel;
  const w = Math.round(x1 - x0), h = Math.round(y1 - y0);
  if (w < 1 || h < 1) {
    log('选区太小，请重新框选。', 'warn');
    return;
  }
  const srcW = cropBaseImage.width, srcH = cropBaseImage.height;
  // 用 canvas 裁剪（原图像素坐标）
  const c = document.createElement('canvas');
  c.width = w;
  c.height = h;
  c.getContext('2d').drawImage(cropBaseImage, x0, y0, w, h, 0, 0, w, h);
  c.toBlob((blob) => {
    if (!blob) {
      log('裁剪导出失败。', 'err');
      return;
    }
    const baseName = logoWorkingName.replace(/（裁剪）+$/, '');
    logoWorkingName = baseName + '（裁剪）';
    logoWorkingFile = new File([blob], logoWorkingName, { type: 'image/png' });
    log(`已裁剪图片：${w}×${h}（原始上传图 ${srcW}×${srcH}）`);
    closeCropDialog();
    // 刷新预览（对齐桌面版 _finish_crop 的 _update_result_preview）
    if (!previewResultDiv.classList.contains('hidden')) {
      doAdapt(false);
    }
  }, 'image/png');
});

// 取消
cropCancel.addEventListener('click', () => closeCropDialog());

// ---- 启动 -----------------------------------------------------------------
updateButtons();
updateFitTip();
updateZoomLabel();
log('Web 版已加载。请载入 BIOS 文件开始。');
