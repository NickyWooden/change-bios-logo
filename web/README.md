# change-bios-logo — Web 版

把桌面版（PySide6 GUI）改造成 **网页应用**：后端用 **FastAPI** 复用项目根的 `bioslogo.py`（GUI-free 核心库），前端是纯静态 **HTML/CSS/JS**（深色毛玻璃主题，呼应桌面版）。整体打包成 **Docker 镜像**，用 **docker compose** 一键部署。

## 目录结构

```
web/
├── backend/
│   ├── main.py            # FastAPI 应用（API 路由 + 会话管理）
│   └── requirements.txt   # 后端依赖（FastAPI / uvicorn / Pillow / numpy）
├── frontend/
│   ├── index.html         # 单页界面（4 步工作流）
│   ├── app.js             # 前端逻辑（fetch 调后端）
│   └── style.css          # 深色毛玻璃主题
├── Dockerfile             # 镜像构建（python:3.12-slim）
├── docker-compose.yml     # compose 部署
└── README.md              # 本文件
```

> 核心逻辑 `bioslogo.py` 在项目根目录，构建镜像时由 Dockerfile 拷入。

## 工作流（与桌面版一致）

1. **载入 BIOS 文件**：选一个 `.F44d` / `.bin` 固件镜像 → 后端扫描，定位 UEFI Logo 段，返回原图预览。
2. **上传新 Logo**：选一张新图片 → 后端按适配参数适配（缩放 / 位深 / 去黑边）。
3. **适配参数**：输出尺寸（与原图相同 / 指定）、颜色位深（24/8/4/1 位）、自动去黑边、放不下自动缩小。
4. **替换并下载**：后端等长替换 Logo 段、重新打包，返回新的 BIOS 文件供下载。

## 本地运行（不经过 Docker）

```bash
# 在项目根目录
cd web/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# bioslogo.py 在上一级，需要让 Python 能找到它：
PYTHONPATH=.. uvicorn main:app --host 0.0.0.0 --port 8000
```

然后浏览器打开 <http://localhost:8000>。

## Docker 构建 & 运行

```bash
# 在项目根目录执行
docker build -f web/Dockerfile -t change-bios-logo-web .
docker run -p 8000:8000 change-bios-logo-web
```

## docker compose 部署（推荐）

```bash
# 在项目根目录执行
docker compose -f web/docker-compose.yml up -d --build
# 查看日志
docker compose -f web/docker-compose.yml logs -f
# 停止
docker compose -f web/docker-compose.yml down
```

启动后访问 <http://localhost:8000>。

## 与桌面版的差异

| 维度 | 桌面版（exe / deb） | Web 版（Docker） |
|---|---|---|
| 运行环境 | 本机 Windows / Linux | 任意能跑 Docker 的机器（含服务器） |
| GUI | PySide6（Qt）原生窗口 | 浏览器（HTML/CSS/JS） |
| 核心逻辑 | `bioslogo.py` | 同一个 `bioslogo.py`（复用） |
| 文件选择 | 系统文件对话框 | 浏览器 `<input type=file>` |
| 会话 | 进程内内存 | 容器内 `/app/sessions`（重启即丢，可挂 volume 持久化） |
| 部署 | 双击 / `dpkg -i` | `docker compose up` |

## 说明

- 会话（载入的 BIOS 文件）默认存在容器内 `/app/sessions`，容器重启即丢。需要持久化，在 `docker-compose.yml` 里取消 `volumes` 的注释即可。
- 大文件（最大 256 MB）通过 `multipart/form-data` 上传；如需调大上限，改 `main.py` 里的 `MAX_UPLOAD_MB`。
