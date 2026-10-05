# change-bios-logo Web 版
# ============
# 构建（在项目根目录执行）：
#   docker build -f web/Dockerfile -t change-bios-logo-web .
# 直接运行：
#   docker run -p 8000:8000 change-bios-logo-web
# 或用 docker compose：
#   docker compose -f web/docker-compose.yml up -d
#
# 说明：
#   - 后端是 FastAPI（uvicorn 跑），核心逻辑复用项目根的 bioslogo.py（GUI-free）。
#   - 前端是纯静态 HTML/CSS/JS，由 FastAPI 的 StaticFiles 直接伺服。
#   - 会话（载入的 BIOS 文件）存在容器内 /app/sessions，重启即丢；
#     需要持久化可在 docker-compose 里挂一个 volume 到 /app/sessions。

FROM python:3.12-slim

WORKDIR /app

# Pillow 需要的系统库（JPEG / 字体渲染）
RUN apt-get update \
    && apt-get install -y --no-install-recommends libjpeg62-turbo libfreetype6 \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY web/backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 后端代码 + 核心库（bioslogo.py 从项目根拷入）
COPY web/backend/ ./
COPY bioslogo.py ./

# 前端静态资源
COPY web/frontend/ ./frontend

# 会话存储目录
RUN mkdir -p /app/sessions

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
