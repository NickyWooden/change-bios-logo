"""change-bios-logo Web 版 — FastAPI 后端。

复用项目根的 ``bioslogo.py``（GUI-free 核心库），把桌面版的工作流
（载入 BIOS → 扫描 → 上传新 Logo → 预览适配 → 替换下载）暴露成 REST API。

会话模型：BIOS 文件只上传一次（``POST /api/scan``），之后用会话 ID 引用；
原始字节落在会话目录（Docker 里是 ``/app/sessions``），避免常驻内存。
"""
from __future__ import annotations

import base64
import io
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

# 核心库在项目根目录（Docker 里被 Dockerfile 拷到 /app/bioslogo.py）
import bioslogo
from bioslogo import (
    BiosLogoError,
    FIT_CONTAIN,
    FIT_MODES,
    LogoSlot,
    trim_black_border,
)

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------
def _default_session_dir() -> Path:
    """Docker 里是 /app/sessions；本地运行（/app 不存在）回退到系统临时目录。"""
    if Path("/app").exists():
        return Path("/app/sessions")
    return Path(tempfile.gettempdir()) / "bioslogo-sessions"

SESSION_DIR = Path(os.environ.get("BIOSSLOGO_SESSION_DIR") or _default_session_dir())
SESSION_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_MB = int(os.environ.get("BIOSSLOGO_MAX_UPLOAD_MB", "256"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

def _default_frontend_dir() -> Path:
    """Docker 里是 /app/frontend；本地运行时是 web/frontend（项目根下）。"""
    if Path("/app").exists():
        return Path("/app/frontend")
    return Path(__file__).resolve().parent.parent / "frontend"

FRONTEND_DIR = Path(os.environ.get("BIOSSLOGO_FRONTEND_DIR") or _default_frontend_dir())

# --------------------------------------------------------------------------
# 与桌面版对齐的适配参数常量
# --------------------------------------------------------------------------
TRIM_THRESHOLD = 10                        # 判定“黑边”的亮度阈值
ZOOM_MIN, ZOOM_MAX = 0.30, 3.00           # 缩放范围（0.30× ~ 3.00×）

# 输出 Logo 的尺寸策略（与桌面版 size_box 一一对应）
SIZE_ORIG = "orig"          # 跟原 Logo 一样：沿用原 Logo 的像素尺寸与 BMP 头（最保险）
SIZE_SOURCE = "source"      # 原图：用上传图片自己的像素尺寸，一个像素都不缩放
SIZE_FIXED = "fixed"        # 固定 720 × 480
SIZE_CUSTOM = "custom"      # 用户手填 W × H
SIZE_MODES = (SIZE_ORIG, SIZE_SOURCE, SIZE_FIXED, SIZE_CUSTOM)
SIZE_FIXED_WH = (720, 480)
SIZE_MIN, SIZE_MAX = 8, 8192

# --------------------------------------------------------------------------
# 会话
# --------------------------------------------------------------------------
@dataclass
class Session:
    id: str
    created_at: float
    raw_path: Path
    slots: list
    src_name: str = ""
    original_b64: str = ""
    original_dims: tuple = ()
    original_bpp: int = 0

SESSIONS: dict[str, Session] = {}


def _img_to_b64(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _new_session(raw: bytes, slots: list, src_name: str) -> Session:
    sid = uuid.uuid4().hex
    raw_path = SESSION_DIR / f"{sid}.bin"
    raw_path.write_bytes(raw)
    original_b64, original_dims, original_bpp = "", (), 0
    if slots:
        s0 = slots[0]
        original_b64 = _img_to_b64(s0.to_image())
        original_dims = (s0.bmp_info.width, s0.bmp_info.rows)
        original_bpp = s0.bmp_info.bpp
    s = Session(id=sid, created_at=time.time(), raw_path=raw_path,
                slots=slots, src_name=src_name,
                original_b64=original_b64,
                original_dims=original_dims, original_bpp=original_bpp)
    SESSIONS[sid] = s
    return s


def _get_session(sid: str) -> Session:
    s = SESSIONS.get(sid)
    if s is None:
        raise HTTPException(status_code=404,
                            detail=f"会话不存在或已过期：{sid}（请重新载入 BIOS）")
    return s


def _cleanup_session(sid: str) -> None:
    s = SESSIONS.pop(sid, None)
    if s is None:
        return
    try:
        s.raw_path.unlink(missing_ok=True)
    except Exception:
        pass


def _read_raw(s: Session) -> bytes:
    return s.raw_path.read_bytes()


def _load_image(raw: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        return img
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"无法读取图片：{e}")


def _resolve_out_size(size_mode: str, at: bool,
                      custom_width: Optional[str], custom_height: Optional[str],
                      new_image) -> Optional[tuple]:
    """复刻桌面版 ``_out_size()`` + ``_out_size_arg()`` 的逻辑，算出传给核心库的 ``out_size``。

    ``SIZE_ORIG``（含旧值 ``"same"``）返回 ``None``——核心库走「复用原 BMP 头」路径，
    明文逐字节一致，最保险；其余模式返回一个 ``(w, h)``。
    """
    if size_mode in (SIZE_ORIG, "same"):
        return None
    if size_mode == SIZE_SOURCE:
        src = new_image
        if at:
            try:
                src = trim_black_border(src, TRIM_THRESHOLD)
            except Exception:
                pass
        if src is None:
            return SIZE_FIXED_WH
        w, h = src.width, src.height
        if not (SIZE_MIN <= w <= SIZE_MAX and SIZE_MIN <= h <= SIZE_MAX):
            return SIZE_FIXED_WH
        return (w, h)
    if size_mode == SIZE_FIXED:
        return SIZE_FIXED_WH
    # SIZE_CUSTOM
    try:
        w, h = int(custom_width), int(custom_height)
    except (TypeError, ValueError):
        return SIZE_FIXED_WH
    if not (SIZE_MIN <= w <= SIZE_MAX and SIZE_MIN <= h <= SIZE_MAX):
        return SIZE_FIXED_WH
    return (w, h)


def _parse_adapt(output_size: str, color_depth: str, auto_trim: str,
                 auto_shrink: str, custom_width: Optional[str],
                 custom_height: Optional[str], fit_mode: str, zoom: str,
                 new_image):
    """把前端的适配参数映射成 bioslogo 的函数参数。

    返回 ``(out_size, out_bpp, at, ash, mode, zoom_f)``。
    """
    at = auto_trim in ("1", "true", "on")
    ash = auto_shrink in ("1", "true", "on")
    # 适配方式（contain / cover / stretch）
    mode = fit_mode if fit_mode in FIT_MODES else FIT_CONTAIN
    # 缩放（0.30 ~ 3.00）
    try:
        zoom_f = float(zoom)
    except (TypeError, ValueError):
        zoom_f = 1.0
    zoom_f = max(ZOOM_MIN, min(ZOOM_MAX, zoom_f))
    # 输出尺寸（4 种模式）
    out_size = _resolve_out_size(output_size, at, custom_width, custom_height,
                                 new_image)
    # 颜色位数
    out_bpp = int(color_depth) if color_depth in ("24", "8", "4", "1") else None
    return out_size, out_bpp, at, ash, mode, zoom_f


def _slot_info(slot: LogoSlot) -> dict:
    try:
        original_b64 = _img_to_b64(slot.to_image())
    except Exception:
        original_b64 = None
    return {
        "index": slot.index,
        "name": slot.name,
        "offset": slot.logo_guid_off,
        "section_off": slot.section_off,
        "size": slot.section_size,
        "stream_budget": slot.stream_budget,
        "original_width": slot.bmp_info.width,
        "original_height": slot.bmp_info.rows,
        "original_depth": slot.bmp_info.bpp,
        "original_b64": original_b64,
    }


# --------------------------------------------------------------------------
# FastAPI 应用
# --------------------------------------------------------------------------
app = FastAPI(title="change-bios-logo Web", version="1.0.0")


@app.get("/api/health")
def health():
    return {"status": "ok", "sessions": len(SESSIONS)}


@app.post("/api/scan")
async def scan(file: UploadFile = File(...)):
    """载入 BIOS 文件：扫描 Logo 段，建立会话，返回会话 ID + 原图预览。"""
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"文件太大（>{MAX_UPLOAD_MB} MB）")
    if len(raw) < (1 << 20):
        raise HTTPException(status_code=400,
                            detail=f"文件太小（{len(raw)} 字节），不像是完整 BIOS")
    try:
        slots = bioslogo.scan(raw)
    except BiosLogoError as e:
        raise HTTPException(status_code=400, detail=str(e))
    s = _new_session(raw, slots, file.filename or "bios.bin")
    return {
        "session_id": s.id,
        "src_name": s.src_name,
        "src_size": len(raw),
        "slots": [_slot_info(slot) for slot in slots],
        "original_logo_b64": s.original_b64,
        "original_dims": list(s.original_dims),
        "original_bpp": s.original_bpp,
    }


@app.post("/api/preview/{sid}")
async def preview(
    sid: str,
    file: UploadFile = File(...),
    output_size: str = Form("orig"),
    color_depth: str = Form("24"),
    auto_trim: str = Form("0"),
    auto_shrink: str = Form("1"),
    custom_width: Optional[str] = Form(None),
    custom_height: Optional[str] = Form(None),
    fit_mode: str = Form("contain"),
    zoom: str = Form("1.0"),
    slot_index: int = Form(0),
):
    """预览适配效果：返回适配后的图片（base64 PNG）+ 尺寸/位深/缩放比 + 警告。"""
    s = _get_session(sid)
    if not s.slots:
        raise HTTPException(status_code=400, detail="该固件没有可替换的 Logo 段")
    if slot_index < 0 or slot_index >= len(s.slots):
        raise HTTPException(status_code=400,
                            detail=f"槽位索引越界：{slot_index}")
    slot = s.slots[slot_index]
    new_image = _load_image(await file.read())
    out_size, out_bpp, at, ash, mode, zoom_f = _parse_adapt(
        output_size, color_depth, auto_trim, auto_shrink,
        custom_width, custom_height, fit_mode, zoom, new_image)
    try:
        if ash:
            new_bmp, dims, scale, _stream_len, _plain = bioslogo.fit_to_budget(
                slot, new_image, mode=mode, zoom=zoom_f, auto_trim=at,
                out_size=out_size, out_bpp=out_bpp)
        else:
            new_bmp = bioslogo.render_bmp(
                slot, new_image, mode=mode, zoom=zoom_f, auto_trim=at,
                out_size=out_size, out_bpp=out_bpp)
            scale = 1.0
        info = bioslogo.parse_bmp(new_bmp)
        preview_img = bioslogo.bmp_to_image(info)
        preview_b64 = _img_to_b64(preview_img)
        # 警告（与桌面版对齐）
        warnings = []
        if (info.width, info.rows) != (slot.bmp_info.width, slot.bmp_info.rows):
            warnings.append(
                f"输出尺寸 {info.width}×{info.rows} 与原 Logo "
                f"({slot.bmp_info.width}×{slot.bmp_info.rows}) 不同——Logo 在开机画面上的"
                "实际大小与位置由固件决定，请刷写后确认效果。")
        try:
            cov = bioslogo.coverage_ratio(preview_img, background=(0, 0, 0),
                                          threshold=TRIM_THRESHOLD)
            if cov < 0.35:
                warnings.append("内容只占画面的一小部分，Logo 在开机画面上会显得偏小。")
        except Exception:
            pass
        return {
            "preview_b64": preview_b64,
            "preview_width": info.width,
            "preview_height": info.rows,
            "preview_depth": info.bpp,
            "scale": round(scale, 4),
            "warnings": warnings,
        }
    except BiosLogoError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/replace/{sid}")
async def replace(
    sid: str,
    file: UploadFile = File(...),
    output_size: str = Form("orig"),
    color_depth: str = Form("24"),
    auto_trim: str = Form("0"),
    auto_shrink: str = Form("1"),
    custom_width: Optional[str] = Form(None),
    custom_height: Optional[str] = Form(None),
    fit_mode: str = Form("contain"),
    zoom: str = Form("1.0"),
    slot_index: int = Form(0),
):
    """替换并打包：等长替换 Logo 段，返回新的 BIOS 文件（文件下载）。"""
    s = _get_session(sid)
    raw = _read_raw(s)
    if not s.slots:
        raise HTTPException(status_code=400, detail="该固件没有可替换的 Logo 段")
    if slot_index < 0 or slot_index >= len(s.slots):
        raise HTTPException(status_code=400,
                            detail=f"槽位索引越界：{slot_index}")
    slot = s.slots[slot_index]
    new_image = _load_image(await file.read())
    out_size, out_bpp, at, ash, mode, zoom_f = _parse_adapt(
        output_size, color_depth, auto_trim, auto_shrink,
        custom_width, custom_height, fit_mode, zoom, new_image)
    out_path = SESSION_DIR / f"{s.id}-out.bin"
    try:
        bioslogo.replace(
            raw, slot, new_image, str(out_path),
            mode=mode, zoom=zoom_f, auto_trim=at,
            out_size=out_size, out_bpp=out_bpp, auto_fit=ash)
    except BiosLogoError as e:
        out_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(e))
    out_bytes = out_path.read_bytes()
    out_path.unlink(missing_ok=True)
    filename = f"new-{s.src_name}"
    return Response(
        content=out_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/session/{sid}")
def delete_session(sid: str):
    """清理会话（释放会话目录里的原始字节）。"""
    _cleanup_session(sid)
    return {"status": "ok"}


# 静态文件（前端）：挂在根路径，index.html 作为首页
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True),
              name="frontend")
