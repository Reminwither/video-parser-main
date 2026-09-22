# Video Parser 部署与运行完整流程

> 应用：基于 **FastAPI + Gradio + Qwen3-VL** 的多平台视频解析、下载与 AI 内容提取系统
> 统一服务端口：**7860**（API 与 Web UI 共用同一端口）
> 启动命令：`python app.py`（内部通过 `gr.mount_gradio_app` 将 Gradio 挂载到 FastAPI，由 `uvicorn` 在 `0.0.0.0:7860` 对外提供服务）
> 健康检查接口：`GET /health`
> 访问地址：Web UI `http://<host>:7860` ｜ API 文档 `http://<host>:7860/docs` ｜ ReDoc `http://<host>:7860/redoc`

---

## 0. 部署目标环境（三选一）

| 目标环境 | 适用场景 | 推荐度 | 复杂度 |
|----------|----------|--------|--------|
| **A. 容器部署（Docker / Compose）** | 生产环境、可复现、隔离依赖 | ⭐⭐⭐ 推荐 | 低 |
| **B. 本地服务器（裸机 / 虚拟机）** | 内网、临时演示、无 Docker 环境 | ⭐⭐ | 中 |
| **C. 云平台（云服务器 / 托管平台）** | 公网访问、弹性扩容 | ⭐⭐⭐ | 中 |

下方给出三种环境的完整步骤，可直接执行。

---

## 1. 依赖与配置文件清单

### 1.1 运行依赖
- **Python 3.10+**（容器内置 3.11，本地部署需自行安装）
- **ffmpeg/ffprobe**（音视频合并、媒体检查与时间轴证据提取必须；容器已内置，本地需自行安装）
- **Docker 20.10+ / Docker Compose 2.0+**（仅容器部署需要）
- **MySQL 5.7+**（**可选**：仅 ranking / 解析记录 / 用户权限等附加功能用到；核心解析下载无需数据库，且 `mysql.connector` 未列入 `requirements.txt`，未启用 DB 时不影响启动）

### 1.2 关键配置文件
| 文件 | 作用 | 是否必需 |
|------|------|----------|
| `Dockerfile` | 容器镜像构建定义 | 容器部署必需 |
| `docker-compose.yml` | 多容器/单服务编排 | 容器部署推荐 |
| `.env` | 运行环境变量（由 `.env.example` 复制生成） | **必需**（至少含 `QWEN_API_KEY`） |
| `requirements.txt` | Python 依赖清单 | 本地部署必需 |
| `packages.txt` | 托管平台系统级依赖（ffmpeg） | 托管平台用 |
| `configs/business_config.json` | 业务配置 | 随代码打包 |
| `schema.sql` | 可选 MySQL 初始化脚本 | 仅启用 DB 功能时需要 |
| `deploy.sh` | 一键部署 + 健康检查 + 验证脚本 | 推荐 |

### 1.3 端口与挂载
- 端口：**7860**（容器映射 `-p 7860:7860`）
- 持久化挂载（容器）：`static/videos`、`static/images`、`downloads`、`cache`、`logs`

---

## 2. 环境变量设置方式

应用通过 `python-dotenv` 在启动时自动加载 **项目根目录下的 `.env` 文件**。

### 方式一：`.env` 文件（推荐，容器与本地通用）
```bash
cp .env.example .env
# 编辑 .env，至少填写 QWEN_API_KEY
```

### 方式二：Shell 导出（仅本地部署）
```bash
export QWEN_API_KEY="ms-xxxxxxxx"
export QWEN_API_BASE_URL="https://api-inference.modelscope.cn/v1"
export QWEN_MODEL_ID="Qwen/Qwen3-VL-8B-Instruct"
export MAX_ANALYSIS_FRAMES=24
export FRAME_INTERVAL_SECONDS=2.0
export SCENE_CHANGE_THRESHOLD=0.32
export CHANGE_DETECTION_FPS=2.0
export TEXT_REGION_CHANGE_THRESHOLD=0.10
export VISION_BATCH_SIZE=6
```

### 方式三：Docker 显式传入
```bash
docker run -d --name video-parser -p 7860:7860 \
  -e QWEN_API_KEY="ms-xxxxxxxx" \
  -e QWEN_MODEL_ID="Qwen/Qwen3-VL-8B-Instruct" \
  video-parser:latest
```

### 环境变量一览
| 变量名 | 说明 | 默认值 | 必填 |
|--------|------|--------|------|
| `QWEN_API_KEY` | ModelScope API 密钥（获取：https://modelscope.cn/my/myaccesstoken） | 无 | **是**（AI 功能） |
| `QWEN_API_BASE_URL` | Qwen API 基础地址 | `https://api-inference.modelscope.cn/v1` | 否 |
| `QWEN_MODEL_ID` | 模型 ID | `Qwen/Qwen3-VL-8B-Instruct` | 否 |
| `MAX_ANALYSIS_FRAMES` | 时间轴画面采样上限 | `24` | 否 |
| `FRAME_INTERVAL_SECONDS` | 连续采样基础间隔（秒） | `2.0` | 否 |
| `SCENE_CHANGE_THRESHOLD` | 场景变化阈值 | `0.32` | 否 |
| `CHANGE_DETECTION_FPS` | 场景/文字变化检测频率 | `2.0` | 否 |
| `TEXT_REGION_CHANGE_THRESHOLD` | 字幕/屏幕文字区域变化候选阈值 | `0.10` | 否 |
| `VISION_BATCH_SIZE` | 每批视觉观察帧数 | `6` | 否 |
| `ASR_MODEL_ID` | 可选 OpenAI 兼容语音转写模型 | 无 | 否 |
| `API_SERVER_URL` | 后端 API 地址（Gradio 调用） | `http://127.0.0.1:7860` | 否 |
| `DOMAIN` | 对外域名（生成下载链接用，可选） | 自动识别 | 否 |

> 注意：`.env` 含密钥，已写入 `.gitignore`，请勿提交到代码仓库。

---

## 3. 部署前构建步骤

### A. 容器部署（构建镜像）
```bash
# 进入项目目录
cd /path/to/video-parser-main

# 构建镜像（Dockerfile 已内置 ffmpeg + curl，并配置清华 PyPI 镜像加速）
docker build -t video-parser:latest .

# 或使用阿里云预构建镜像（跳过构建）
# docker pull registry.cn-hangzhou.aliyuncs.com/chuchengzhi/video-parser:latest
```

### B. 本地部署（安装依赖）
```bash
cd /path/to/video-parser-main
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 安装 ffmpeg（证据分析 / B站合并必需）
# Ubuntu/Debian:
sudo apt update && sudo apt install -y ffmpeg
# macOS:
brew install ffmpeg
# Windows: 下载 https://ffmpeg.org/download.html，将 bin 加入 PATH
ffmpeg -version   # 验证安装
```

### C. 可选：初始化 MySQL（仅启用附加功能）
```bash
mysql -u root -p < schema.sql
# 在代码中配置 DATABASE_CONFIG（host/user/password/database）后启用 DB 功能
```

---

## 4. 启动命令与运行参数

### A. 容器部署 — Docker Compose（推荐）
```bash
cp .env.example .env   # 先填好 QWEN_API_KEY
docker-compose up -d
docker-compose logs -f
```
> Compose 已内置 `healthcheck`：每 30s 探测 `http://localhost:7860/health`，超时 10s，重试 3 次，启动宽限 10s。

### A2. 容器部署 — 单 `docker run`
```bash
mkdir -p static/videos static/images downloads cache logs
docker run -d \
  --name video-parser \
  -p 7860:7860 \
  --env-file .env \
  -v $(pwd)/static/videos:/app/static/videos \
  -v $(pwd)/static/images:/app/static/images \
  -v $(pwd)/downloads:/app/downloads \
  -v $(pwd)/cache:/app/cache \
  -v $(pwd)/logs:/app/logs \
  --restart unless-stopped \
  video-parser:latest
```

### B. 本地部署 — 直接启动
```bash
cp .env.example .env   # 填好密钥
# 加载 .env 后启动（python-dotenv 会自动读取）
python app.py
# 服务监听 0.0.0.0:7860
```
运行参数说明：
- 绑定地址固定 `0.0.0.0`（容器内需对外暴露）；
- 端口固定 `7860`；
- 进程为「Gradio + FastAPI 合并服务」，单进程占用单端口，无需额外反向代理即可同时提供 API 与 Web UI。

### C. 云平台部署要点
- **云服务器（ECS/轻量应用服务器）**：按 A 部署后，在**安全组/防火墙**放行 `7860` 端口；建议前置 Nginx/Caddy 反代并配置 HTTPS。
- **托管平台（Hugging Face Spaces / OpenXLab）**：上传代码，平台读取 `packages.txt` 自动安装 `ffmpeg`；通过平台环境变量面板设置 `QWEN_API_KEY` 等。
- **公网访问**：如需自定义域名，在 `.env` 设置 `DOMAIN=https://your-domain.com`；反向代理需支持 WebSocket（Gradio 实时通信）。

---

## 5. 健康检查与验证部署成功

### 5.1 容器内健康探针（自动）
Dockerfile / Compose 已配置：
```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:7860/health || exit 1
```
查看状态：
```bash
docker inspect -f '{{.State.Health.Status}}' video-parser   # healthy / starting / unhealthy
```

### 5.2 手动健康检查
```bash
curl -f http://localhost:7860/health && echo "服务健康"
```

### 5.3 部署验证清单（建议全部通过）
```bash
# 1) 健康检查
curl -fsS http://localhost:7860/health

# 2) 首页与文档可达
curl -o /dev/null -w "首页: %{http_code}\n" http://localhost:7860/
curl -o /dev/null -w "Swagger: %{http_code}\n" http://localhost:7860/docs
curl -o /dev/null -w "ReDoc: %{http_code}\n" http://localhost:7860/redoc

# 3) 解析接口冒烟测试（外网链接失效会返回 4xx/5xx，属正常）
curl -X POST http://localhost:7860/api/parse \
  -H "Content-Type: application/json" \
  -H "X-Timestamp: $(date +%s)000" \
  -H "X-GCLT-Text: smoke" \
  -H "X-EGCT-Text: smoke" \
  -d '{"text":"https://www.bilibili.com/video/BV1xx411c7mD"}'
```

---

## 6. 启动后确认服务正常运行

```bash
# 容器是否在运行
docker ps --filter "name=video-parser"

# 端口是否监听
# Linux/macOS:
ss -ltnp | grep 7860
# Windows (PowerShell):
netstat -ano | findstr 7860

# 实时日志（关注 “正在启动服务” 与 uvicorn 监听行）
docker logs -f video-parser

# 进入容器调试
docker exec -it video-parser bash
```

浏览器访问 `http://<服务器IP>:7860` 能看到 Web 界面、能打开 `/docs` 即代表部署成功。

---

## 7. 常见错误排查要点

| 现象 | 可能原因 | 排查与解决 |
|------|----------|------------|
| `curl: (7) Failed to connect` / 健康检查超时 | 容器未启动或端口未映射 | `docker ps` 确认状态；确认 `-p 7860:7860`；`docker logs` 看启动报错 |
| `QWEN_API_KEY` 为占位符警告 | 未填写真实密钥 | 编辑 `.env` 填入 ModelScope Key，重启容器 |
| 证据分析报错 401/403 | API Key 无效或额度不足 | 到 ModelScope 控制台核对 Key 与余额 |
| B 站视频下载后无声 / 合并失败 | 宿主机/容器内缺少 ffmpeg | 容器内已内置；本地部署执行 `ffmpeg -version` 验证并安装 |
| `ModuleNotFoundError: mysql.connector` | 启用了 DB 附加功能但未装驱动 | 核心功能无需 DB；如需 DB：`pip install mysql-connector-python` 并初始化 `schema.sql` |
| `Address already in use` | 7860 被占用 | 释放端口（`lsof -i:7860` / `netstat`），或改 `PORT` 环境变量与映射 |
| 解析接口返回 4xx/5xx | 平台链接失效 / 需特殊请求头 | 链接有时效性，重新获取分享链接；部分平台需 Referer |
| 容器 `unhealthy` 但接口偶尔可用 | 探针间隔短、冷启动慢 | 调大 `start_period`；检查 `MAX_ANALYSIS_FRAMES` 或 `VISION_BATCH_SIZE` 是否过大 |
| 镜像拉取/构建慢 | 网络问题 | Dockerfile 已用清华源；或改用阿里云预构建镜像 |
| 上传/下载无写入 | 挂载目录权限不足 | 宿主机目录需可写：`chmod -R 755 static downloads cache logs` |
| 云平台 WebSocket 断连 | 反代未支持 WS | Nginx 需配置 `proxy_http_version 1.1` 与 `Upgrade/Connection` 头 |

---

## 8. 一键部署脚本（推荐）

仓库已提供 `deploy.sh`，覆盖「检查依赖 → 生成配置 → 构建镜像 → 启动 → 健康检查 → 验证」全流程：

```bash
# 完整部署（自动构建并使用本地镜像）
./deploy.sh deploy

# 使用阿里云预构建镜像部署（跳过本地构建）
IMAGE=registry.cn-hangzhou.aliyuncs.com/chuchengzhi/video-parser:latest ./deploy.sh deploy

# 其他命令
./deploy.sh status     # 查看容器状态与健康探针
./deploy.sh logs       # 实时日志
./deploy.sh stop       # 停止并移除容器
./deploy.sh restart    # 重启并执行健康检查
./deploy.sh update     # 重新构建镜像并重启（版本更新）
```

脚本在健康检查超时或接口异常时会给出明确的日志排查指引，适合生产环境直接使用。

---

## 9. 常用运维命令速查

```bash
# ===== Docker Compose =====
docker-compose up -d --build      # 构建并启动
docker-compose ps                 # 状态
docker-compose logs -f            # 日志
docker-compose down               # 停止并移除

# ===== Docker =====
docker start/stop/restart video-parser
docker rm -f video-parser
docker exec -it video-parser bash

# ===== 版本更新 =====
docker pull registry.cn-hangzhou.aliyuncs.com/chuchengzhi/video-parser:latest
docker-compose down && docker-compose up -d
```
