# 视频解析工作台 · 腾讯云轻量应用服务器（Lighthouse）部署指南

> 适用对象：本项目（`video-parser`，FastAPI + Gradio + Qwen3-VL，Docker 化，单端口 7860）。  
> 目标：把**你自己改过的代码（含深色 UI）**&#x90E8;署到腾讯云轻量应用服务器并对外访问。  
> 注意：**轻量应用服务器 ≠ CVM 云服务器**，本文档的"防火墙"指轻量控制台里的实例防火墙，不是 CVM 安全组。

---

## 0. 前置准备

| 项目             | 说明                                                             |
| -------------- | -------------------------------------------------------------- |
| 腾讯云账号          | 已实名，能购买轻量实例                                                    |
| `QWEN_API_KEY` | ModelScope 访问令牌，必填。申请：<https://modelscope.cn/my/myaccesstoken> |
| 项目代码           | 已包含你改好的 `app.py`（深色主题）。需能传到服务器（Git 仓库 或 打包上传）                  |
| 公网访问方式         | 直接 `IP:7860`，或 域名 + Nginx 反代（推荐 80/443）                        |

> 如果你希望我**直接通过 Lighthouse MCP 帮你建实例 / 开防火墙 / 跑容器**，需要先在 WorkBuddy 左侧"连接"卡片里接入「腾讯云轻量应用服务器」并完成授权，然后告诉我即可。



---

## 1. 创建轻量服务器实例

1. 腾讯云控制台 → **轻量应用服务器** → 新建。
2. **镜像选择**（二选一）：
   - 推荐：**应用镜像 → Docker CE**（自带 Docker，最省事）
   - 或：**系统镜像 → Ubuntu 22.04 LTS / 24.04 LTS**（稍后自己装 Docker，见第 2 步）
3. **实例规格**：
   - 入门体验：2 核 2G（能跑，但 AI 分析并发吃紧）
   - 稳定推荐：**2 核 4G** 或 **4 核 4G**（视频下载/合并 + Qwen 推理更稳）
4. **地域**：选离你近的（上海 / 南京 / 广州），国内访问快、无需备案即可用 IP 访问。
5. **防火墙**（创建时可先放通，也可第 6 步再配）：放通 TCP `7860`（若走 Nginx 则放通 `80`、`443`）。
6. 设置 root/管理员密码，完成创建，记下**公网 IP**。

---

## 2. 登录与基础环境（仅"系统镜像"需要）

> 若第 1 步选了 **Docker 应用镜像**，可跳过本节。

登录服务器（控制台 ORC 在线终端，或本地 `ssh root@<公网IP>`），装 Docker：

```bash
# 一键安装 Docker（官方脚本，国内可用）
curl -fsSL https://get.daocloud.io/docker | sh
# 或用官方：curl -fsSL https://get.docker.com | sh

# 启动并设置开机自启
systemctl enable --now docker

# 验证
docker version
docker compose version   # 轻量镜像自带 compose v2；系统镜像装完也有
```

> 拉取 `python:3.11-slim` 基础镜像时若很慢，可配置 Docker Hub 国内镜像加速：  
> 编辑 `/etc/docker/daemon.json`（没有就新建），加入 `"registry-mirrors": ["https://mirror.ccs.tencentyun.com"]`，  
> 然后 `systemctl restart docker`。

---

## 3. 获取项目代码

**方式 A：Git 拉取（推荐，便于后续更新）**  
先把改好的代码推到 GitHub / Gitee，然后在服务器上：

```bash
cd /opt
git clone <你的仓库地址> video-parser
cd video-parser
```

**方式 B：本地上传**  
在本机把项目打成包（排除 `.venv`、`__pycache__`、大文件），上传并解压：

```bash
# 本机（PowerShell / Git Bash）
cd E:\视频\video-parser-main
tar czf video-parser.tar.gz --exclude=.venv --exclude=__pycache__ --exclude=.git .
scp video-parser.tar.gz root@<公网IP>:/opt/

# 服务器
mkdir -p /opt/video-parser && cd /opt/video-parser
tar xzf /opt/video-parser.tar.gz -C /opt/video-parser
```

---

## 4. 配置环境变量 `.env`

```bash
cd /opt/video-parser
cp .env.example .env
vim .env        # 或用 nano .env
```

**必填项**：

```ini
QWEN_API_KEY=ms-xxxxxxxxxxxxxxxx   # 你的 ModelScope 令牌
```

其余项（`QWEN_MODEL_ID`、`MAX_ANALYSIS_FRAMES` 等）保持默认即可。  
Dockerfile 已内置 `TZ=Asia/Shanghai` 与 ffmpeg，无需额外处理。

---

## 5. 构建镜像并启动（本地构建 = 含你的 UI 改动）

> 关键：仓库里的 `docker-compose.yml` 默认 `image:` 拉的是阿里云上**别人旧版镜像**，  
> **不包含你刚改的深色 UI**。要部署自己的代码，必须**本地构建**。

### 方案 A：直接用 `docker build` + `docker run`（最直观）

```bash
cd /opt/video-parser

# 1) 构建镜像（基于你的代码）
docker build -t video-parser:latest .

# 2) 启动容器
docker run -d \
  --name video-parser \
  -p 7860:7860 \
  --env-file .env \
  -v $(pwd)/static/videos:/app/static/videos \
  -v $(pwd)/static/images:/app/static/images \
  -v $(pwd)/downloads:/app/downloads \
  -v $(pwd)/cache:/app/cache \
  -v $(pwd)/logs:/app/logs \
  -e TZ=Asia/Shanghai \
  --restart unless-stopped \
  video-parser:latest

# 3) 查看启动日志
docker logs -f video-parser
```

### 方案 B：改 compose 后 `docker compose up`（推荐长期维护）

把 `docker-compose.yml` 的 `image:` 改成 `build: .`（让它本地构建而非拉远程）：

```yaml
services:
  video-parser:
    build: .                       # ← 改这里：本地构建，含你的改动
    # image: registry.cn-hangzhou.aliyuncs.com/chuchengzhi/video-parser:latest  # 注释掉
    container_name: video-parser
    restart: unless-stopped
    ports:
      - "7860:7860"
    volumes:
      - ./static/videos:/app/static/videos
      - ./static/images:/app/static/images
      - ./downloads:/app/downloads
      - ./cache:/app/cache
      - ./logs:/app/logs
    env_file:
      - .env
    environment:
      - TZ=Asia/Shanghai
      - PYTHONUNBUFFERED=1
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:7860/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
```

然后：

```bash
docker compose up -d --build     # 构建并启动
docker compose ps                # 查看状态（STATUS 应为 healthy/running）
docker compose logs -f           # 看日志
```

---

## 6. 开放防火墙端口（轻量控制台）

1. 腾讯云控制台 → **轻量应用服务器** → 你的实例 → **防火墙**。
2. 添加规则：
   - 若直接 IP 访问：`协议 TCP` / `端口 7860` / `策略 允许` / `来源 0.0.0.0/0`
   - 若走 Nginx（第 8 步）：放通 `80` 和 `443`
3. 保存。**轻量防火墙是实例级，无需再去 CVM 安全组。**

> ⚠️ 即使容器在跑，防火墙没放通也会连不上。这是最常见的"部署成功但访问不了"原因。

---

## 7. 验证服务

```bash
# 服务器本地健康检查（Dockerfile 已配 /health）
curl -f http://localhost:7860/health && echo "  => 健康检查通过"

# 或看容器健康状态
docker ps   # STATUS 列出现 (healthy) 即可
```

浏览器访问：

- 直接 IP：`http://<公网IP>:7860`
- 若配了域名+HTTPS：`https://你的域名`

看到**深色沉浸风的"视频解析工作台"**&#x9996;页即部署成功。

---

## 8.（可选）域名 + HTTPS（Nginx 反向代理）

用 IP:端口 访问不够正式，且某些浏览器对混合内容有限制。建议用 Nginx 反代到 80/443。

1. 安装 Nginx：`apt update && apt install -y nginx`
2. 配置站点（以域名 `parser.example.com` 为例）：

```nginx
# /etc/nginx/sites-available/video-parser
server {
    listen 80;
    server_name parser.example.com;

    location / {
        proxy_pass http://127.0.0.1:7860;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Gradio 实时通信依赖 WebSocket，必须放行 Upgrade
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

```bash
ln -s /etc/nginx/sites-available/video-parser /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

1. 申请免费证书（二选一）：
   - 腾讯云 SSL 证书控制台申请免费 DV 证书，下载 Nginx 版，放到 `/etc/nginx/ssl/`
   - 或用 `certbot --nginx -d parser.example.com`（需先放行 80 端口）
2. 在上面的 `server` 前加 443 块并 `return 301 https://$host$request_uri;` 做 HTTP 跳转。

> 说明：本项目 Gradio 已 `mount_gradio_app(..., path="/")` 挂在根路径，Nginx 反代到根 `/` 即可，无需子路径改写。

---

## 9. 日常运维与更新

```bash
# 查看实时日志
docker compose logs -f
docker logs -f video-parser

# 重启
docker compose restart
docker restart video-parser

# 停止 / 启动
docker compose down        # 停止并移除容器（数据卷保留）
docker compose up -d       # 再次启动

# 更新代码后重新构建
cd /opt/video-parser
git pull                   # 或重新上传覆盖
docker compose up -d --build
```

- **监控**：轻量控制台自带 CPU / 内存 / 带宽 / 流量包监控，留意流量包余量（视频下载较费流量）。
- **持久化**：`downloads/`、`cache/`、`logs/` 已挂载到宿主机，容器重建不丢数据。

---

## 10. 常见故障排查

| 现象                   | 可能原因                         | 处理                                                              |
| -------------------- | ---------------------------- | --------------------------------------------------------------- |
| 浏览器连不上               | 轻量防火墙没放通端口                   | 第 6 步放通 7860 / 80 / 443                                         |
| 容器一直 `starting` / 重启 | `.env` 缺失或 `QWEN_API_KEY` 为空 | 确认 `.env` 已配且 `docker compose config` 能看到                       |
| `/health` 返回 502     | 容器还没起来 / 崩溃                  | `docker logs video-parser` 看报错                                  |
| UI 不是深色 / 没变化        | 拉的是旧远程镜像而非本地构建               | 确保用第 5 步"本地构建"方案                                                |
| 视频播放/下载失败            | 部分平台需 Referer 或链接失效          | 看应用日志；链接有时效，解析后尽快下载                                             |
| B 站合并失败              | 容器内 ffmpeg 缺失（一般不会）          | Dockerfile 已装 ffmpeg；`docker exec video-parser which ffmpeg` 验证 |
| AI 分析报错              | `QWEN_API_KEY` 无效或额度不足       | 检查 ModelScope 令牌与余额                                             |

---

## 附：一键命令清单（系统镜像 + Git 拉取 + 本地构建）

```bash
# 1. 装 Docker（系统镜像时）
curl -fsSL https://get.daocloud.io/docker | sh && systemctl enable --now docker

# 2. 拉代码 + 配置
cd /opt && git clone <仓库> video-parser && cd video-parser
cp .env.example .env && vim .env      # 填 QWEN_API_KEY

# 3. 构建并启动（用改过 build:. 的 compose）
docker compose up -d --build

# 4. 验证
curl -f http://localhost:7860/health
```

完成上面 4 步，再去轻量控制台防火墙放通端口，即可用 `http://<公网IP>:7860` 访问你的深色工作台。
