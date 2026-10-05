"""change-bios-logo Web 版 — FastAPI 后端。

复用项目根的 ``bioslogo.py``（GUI-free 核心库），把桌面版的工作流
（载入 BIOS → 扫描 → 上传新 Logo → 预览适配 → 替换下载）暴露成 REST API。

会话模型：BIOS 文件只上传一次（``POST /api/scan``），之后用会话 ID 引用；
原始字节落在会话目录（Docker 里是 ``/app/sessions``），避免常驻内存。
"""
from __future__ import annotations

import base64
import io
import io
import os
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

# 核心库在项目根目录（Docker 里被 Dockerfile 拷到 /app/bioslogo.py）
import bioslogo
from bioslogo import BiosLogoError, FIT_CONTAIN, LogoSlot

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


def _parse_adapt(output_size: str, color_depth: str, auto_trim: str,
                 auto_shrink: str, custom_width: Optional[str],
                 custom_height: Optional[str]):
    """把前端的适配参数映射成 bioslogo 的函数参数。"""
    out_size = None
    if output_size == "custom":
        if not custom_width or not custom_height:
            raise HTTPException(status_code=400,
                                detail="自定义尺寸需要同时提供宽和高")
        try:
            w, h = int(custom_width), int(custom_height)
        except ValueError:
            raise HTTPException(status_code=400, detail="自定义尺寸必须是整数")
        if w < 1 or h < 1:
            raise HTTPException(status_code=400, detail="自定义尺寸必须 ≥ 1")
        out_size = (w, h)
    out_bpp = int(color_depth) if color_depth in ("24", "8", "4", "1") else None
    at = auto_trim in ("1", "true", "true", "on")
    ash = auto_shrink in ("1", "true", "true", "on")
    return out_size, out_bpp, at, ash


def _slot_info(slot: LogoSlot) -> dict:
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
    output_size: str = Form("same"),
    color_depth: str = Form("24"),
    auto_trim: str = Form("1"),
    auto_shrink: str = Form("1"),
    custom_width: Optional[str] = Form(None),
    custom_height: Optional[str] = Form(None),
    slot_index: int = Form(0),
):
    """预览适配效果：返回适配后的图片（base64 PNG）+ 尺寸/位深/缩放比。"""
    s = _get_session(sid)
    if not s.slots:
        raise HTTPException(status_code=400, detail="该固件没有可替换的 Logo 段")
    if slot_index < 0 or slot_index >= len(s.slots):
        raise HTTPException(status_code=400,
                            detail=f"槽位索引越界：{slot_index}")
    slot = s.slots[slot_index]
    new_image = _load_image(await file.read())
    out_size, out_bpp, at, ash = _parse_adapt(
        output_size, color_depth, auto_trim, auto_shrink,
        custom_width, custom_height)
    try:
        if ash:
            new_bmp, dims, scale, _stream_len, _plain = bioslogo.fit_to_budget(
                slot, new_image, mode=FIT_CONTAIN, auto_trim=at,
                out_size=out_size, out_bpp=out_bpp)
        else:
            new_bmp = bioslogo.render_bmp(
                slot, new_image, mode=FIT_CONTAIN, auto_trim=at,
                out_size=out_size, out_bpp=out_bpp)
            scale = 1.0
        info = bioslogo.parse_bmp(new_bmp)
        preview_b64 = _img_to_b64(bioslogo.bmp_to_image(info))
        return {
            "preview_b64": preview_b64,
            "preview_width": info.width,
            "preview_height": info.rows,
            "preview_depth": info.bpp,
            "scale": round(scale, 4),
            "warnings": [],
        }
    except BiosLogoError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/replace/{sid}")
async def replace(
    sid: str,
    file: UploadFile = File(...),
    output_size: str = Form("same"),
    color_depth: str = Form("24"),
    auto_trim: str = Form("1"),
    auto_shrink: str = Form("1"),
    custom_width: Optional[str] = Form(None),
    custom_height: Optional[str] = Form(None),
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
    out_size, out_bpp, at, ash = _parse_adapt(
        output_size, color_depth, auto_trim, auto_shrink,
        custom_width, custom_height)
    out_path = SESSION_DIR / f"{s.id}-out.bin"
    try:
        bioslogo.replace(
            raw, slot, new_image, str(out_path),
            mode=FIT_CONTAIN, auto_trim=at,
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
