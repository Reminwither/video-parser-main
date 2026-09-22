---
license: mit

datasets: #关联数据集
#- 

models: #关联模型
#- 

---

# Video Parser

**基于 FastAPI + Gradio + Qwen3-VL 的多平台视频解析、下载与证据优先视频分析系统**

[![Python](https://mypicture-1258720957.cos.ap-nanjing.myqcloud.com/Obsidian/python-3.10%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://mypicture-1258720957.cos.ap-nanjing.myqcloud.com/Obsidian/FastAPI-0.115%2B-green.svg)](https://fastapi.tiangolo.com/)
[![Gradio](https://mypicture-1258720957.cos.ap-nanjing.myqcloud.com/Obsidian/Gradio-5.0%2B-orange.svg)](https://gradio.app/)
[![Qwen3-VL](https://mypicture-1258720957.cos.ap-nanjing.myqcloud.com/Obsidian/Qwen3--VL-AI-purple.svg)](https://modelscope.cn/)
[![License](https://mypicture-1258720957.cos.ap-nanjing.myqcloud.com/Obsidian/license-MIT-green.svg)](LICENSE)

---

## 功能特性

- **多平台支持**：抖音、哔哩哔哩、小红书、快手、好看视频等
- **无水印下载**：智能解析视频直链，绕过水印限制
- **在线播放**：支持浏览器内直接播放视频
- **证据优先分析**：按时间轴对齐画面、字幕与可选 ASR，再区分原始证据、释义和推论
- **Web 界面**：基于 Gradio 的友好操作界面
- **RESTful API**：标准化接口，支持二次开发
- **自动文档**：FastAPI 自动生成 Swagger/ReDoc 文档

---

## 支持的平台

| 平台 | 状态 | 平台 | 状态 |
|------|------|------|------|
| 抖音 | ✅ | 小红书 | ✅ |
| 哔哩哔哩 | ✅ | 快手 | ✅ |
| 好看视频 | ✅ |  |  |

---

## 快速开始

### 方式一：Docker 部署（推荐）

#### 1. 环境要求

- Docker 20.10+
- Docker Compose 2.0+（可选）

#### 2. 使用 Docker Compose（推荐）

```bash
# 克隆项目
git clone https://github.com/initchu/video-parser.git
cd video-parser

# 配置环境变量
cp .env.example .env
# 编辑 .env 文件，填入 QWEN_API_KEY

# 构建并启动服务
docker-compose up -d

# 查看日志
docker-compose logs -f
```

docker-compose.yml

```
version: '3.8'

services:
  video-parser:
    image: registry.cn-hangzhou.aliyuncs.com/chuchengzhi/video-parser:latest
    container_name: video-parser
    restart: unless-stopped
    ports:
      - "7860:7860"   # 统一服务端口 (FastAPI + Gradio)
    volumes:
      # 持久化存储
      - ./static/videos:/app/static/videos
      - ./static/images:/app/static/images
      - ./downloads:/app/downloads
      - ./cache:/app/cache
      - ./logs:/app/logs
    # 从 .env 文件加载环境变量
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
      start_period: 10s
    networks:
      - video-parser-network

networks:
  video-parser-network:
    driver: bridge
```

#### 3. 使用 Docker 命令

```bash
# 配置环境变量
cp .env.example .env
# 编辑 .env 文件，填入 QWEN_API_KEY

# 打包镜像
docker build -t video-parser:latest .

# 运行容器（使用 --env-file 加载环境变量）
docker run -d \
  --name video-parser \
  -p 7860:7860 \
  --env-file .env \
  -v $(pwd)/static/videos:/app/static/videos \
  -v $(pwd)/downloads:/app/downloads \
  -v $(pwd)/cache:/app/cache \
  -v $(pwd)/logs:/app/logs \
  --restart unless-stopped \
  video-parser:latest

# 查看日志
docker logs -f video-parser
```

#### 4. 环境变量配置

在运行 Docker 前，需要创建 `.env` 文件：

```bash
cp .env.example .env
vim .env  # 编辑配置
```

**必填配置：**

| 变量名 | 说明 | 示例 |
|--------|------|------|
| `QWEN_API_KEY` | ModelScope API 密钥 | `ms-xxxxxxxx` |

**可选配置：**

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `QWEN_API_BASE_URL` | API 基础地址 | `https://api-inference.modelscope.cn/v1` |
| `QWEN_MODEL_ID` | 模型 ID | `Qwen/Qwen3-VL-8B-Instruct` |
| `MAX_ANALYSIS_FRAMES` | 时间轴画面采样上限 | `24` |
| `FRAME_INTERVAL_SECONDS` | 连续采样基础间隔（秒） | `2.0` |
| `SCENE_CHANGE_THRESHOLD` | 场景变化检测阈值 | `0.32` |
| `CHANGE_DETECTION_FPS` | 场景/文字变化检测频率 | `2.0` |
| `TEXT_REGION_CHANGE_THRESHOLD` | 字幕/屏幕文字区域变化候选阈值 | `0.10` |
| `VISION_BATCH_SIZE` | 每批视觉观察帧数 | `6` |
| `ASR_MODEL_ID` | 可选的 OpenAI 兼容语音转写模型 | 未配置（诚实降级） |
| `ASR_BACKEND` | `openai` 或本地 `faster-whisper` | `openai` |
| `ASR_LANGUAGE` | 可选语音语言提示，中文使用 `zh` | 无 |
| `ASR_CHUNK_SECONDS` | 无句级时间戳时的音频分块对齐长度 | `0`（不分块） |

> API 密钥获取地址：https://modelscope.cn/my/myaccesstoken

#### 5. 访问应用

- **Web 界面**：http://localhost:7860
- **API 文档**：http://localhost:7860/docs
- **ReDoc 文档**：http://localhost:7860/redoc

#### 6. 常用命令

```bash
# ===== Docker Compose 命令 =====
# 停止服务
docker-compose down

# 重新构建并启动
docker-compose up -d --build

# 查看服务状态
docker-compose ps

# 查看实时日志
docker-compose logs -f

# ===== Docker 命令 =====
# 停止容器
docker stop video-parser

# 启动容器
docker start video-parser

# 重启容器
docker restart video-parser

# 删除容器
docker rm -f video-parser

# 进入容器
docker exec -it video-parser bash

# 查看容器状态
docker ps -a | grep video-parser

# 查看镜像
docker images | grep video-parser

# 删除镜像
docker rmi video-parser:latest
```

---

### 方式二：本地部署

#### 1. 环境要求

- Python 3.10+
- ffmpeg（B站视频合并需要）

#### 2. 安装依赖

```bash
git clone https://github.com/initchu/video-parser.git
cd video-parser
pip install -r requirements.txt
```

#### 3. 配置环境变量

```bash
# 复制环境变量示例文件
cp .env.example .env

# 编辑 .env 文件，配置 API 密钥
vim .env
```

**.env 配置项说明：**

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `QWEN_API_BASE_URL` | Qwen API 基础地址 | `https://api-inference.modelscope.cn/v1` |
| `QWEN_API_KEY` | ModelScope API 密钥 | 无（必填） |
| `QWEN_MODEL_ID` | 模型 ID | `Qwen/Qwen3-VL-8B-Instruct` |
| `MAX_ANALYSIS_FRAMES` | 时间轴画面采样上限 | `24` |
| `FRAME_INTERVAL_SECONDS` | 连续采样基础间隔（秒） | `2.0` |
| `ASR_MODEL_ID` | 可选语音转写模型；未配置时不生成口播稿 | 无 |

> API 密钥获取：https://modelscope.cn/my/myaccesstoken

#### 4. 启动服务

**启动统一服务（包含 API 和 Web 界面）：**

```bash
python app.py
```

#### 5. 访问应用

- **Web 界面**：http://localhost:7860
- **API 文档**：http://localhost:7860/docs
- **ReDoc 文档**：http://localhost:7860/redoc

---

## API 接口

### 解析视频

**POST** `/api/parse`

请求体：
```json
{
  "text": "https://v.douyin.com/xxx"
}
```

请求头：
```
X-Timestamp: 毫秒时间戳
X-GCLT-Text: 随机明文
X-EGCT-Text: 加密后的文本
```

响应：
```json
{
  "retcode": 200,
  "retdesc": "成功",
  "data": {
    "video_id": "xxx",
    "platform": "抖音",
    "title": "视频标题",
    "video_url": "https://...",
    "cover_url": "https://...",
    "audio_url": "https://..."
  },
  "succ": true
}
```

### 获取下载链接

**POST** `/api/download`

请求体：
```json
{
  "video_url": "https://...",
  "video_id": "xxx"
}
```

响应：
```json
{
  "retcode": 200,
  "retdesc": "成功",
  "data": {
    "download_url": "https://..."
  },
  "succ": true
}
```

### 运行测试

```bash
# 测试后端 API
curl -X POST http://localhost:7860/api/parse \
  -H "Content-Type: application/json" \
  -H "X-Timestamp: $(date +%s)000" \
  -H "X-GCLT-Text: test" \
  -H "X-EGCT-Text: test" \
  -d '{"text": "https://www.bilibili.com/video/BV1TaqYBcEJc"}'
```

---

## 证据优先的视频分析

分析系统会先建立可回查的时间轴证据，再生成总结。页面标题只在正文分析完成后用于一致性核对，不参与正文推导。

### Web 界面使用

1. 解析视频链接

   ![image-20251227160812620](https://mypicture-1258720957.cos.ap-nanjing.myqcloud.com/Obsidian/image-20251227160812620.png)

2. 点击「在线播放」加载视频

   ![image-20251227160929670](https://mypicture-1258720957.cos.ap-nanjing.myqcloud.com/Obsidian/image-20251227160929670.png)

3. 点击「AI时间轴证据分析」按钮

   ![image-20251227161006218](https://mypicture-1258720957.cos.ap-nanjing.myqcloud.com/Obsidian/image-20251227161006218.png)

4. 等待证据提取和分析完成，查看覆盖说明、时间轴证据、E1/E2/E3 与论证结构

   ![image-20251227161017867](https://mypicture-1258720957.cos.ap-nanjing.myqcloud.com/Obsidian/image-20251227161017867.png)

### 命令行工具

```bash
# 列出所有视频文件
python qwen3vl.py --list

# 分析指定视频
python qwen3vl.py --video downloads/video.mp4

# 使用自定义提示词
python qwen3vl.py --video video.mp4 --prompt "这个视频讲的是什么故事？"

# 指定时间轴画面采样上限（默认24帧）
python qwen3vl.py --video video.mp4 --frames 24

# 交互式模式
python qwen3vl.py --interactive
```

### 工作原理

1. 使用 ffprobe 读取时长、分辨率、FPS、编码、音轨和字幕轨
2. 组合连续采样、场景变化采样、字幕轨变化与屏幕文字区域变化候选采样，并为每帧保留时间戳
3. 提取独立字幕轨；配置 ASR 时分离音轨并生成带时间戳转写
4. 分批调用视觉模型，只抄录逐帧可见事实和画面文字，不先做总结
5. 在同一时间轴对齐声音/字幕/画面，重建语义段和论证结构
6. 最终报告强制区分 E1 直接证据、E2 合理释义、E3 分析推论
7. 无字幕且未配置 ASR 时明确降级为视觉分析，禁止伪造口播或完整论证

---

## 注意事项

- B 站视频为音视频分离，需要安装 **ffmpeg** 进行合并
- 证据分析依赖 **ffmpeg/ffprobe** 提取媒体信息、字幕、音轨和时间轴画面
- 部分平台可能需要特殊的请求头（Referer）才能下载
- 视频链接有时效性，解析后请尽快下载
- Web 端分析需要先播放视频（加载到本地缓存）

### ffmpeg 安装指南

如果本地运行出现 `ffmpeg` 未找到的错误，请按照以下步骤安装：

#### Windows
1. 访问 [ffmpeg.org](https://ffmpeg.org/download.html#build-windows) 下载预编译二进制文件。
2. 解压并将 `bin` 目录路径添加到系统环境变量 `PATH` 中。
3. 或者在运行前设置环境变量：`set FFMPEG_PATH=C:\path\to\ffmpeg.exe`。

#### macOS
使用 Homebrew 安装：
```bash
brew install ffmpeg
```

#### Linux
使用包管理器安装：
```bash
sudo apt update && sudo apt install ffmpeg
```

#### Gradio 应用平台 (如 Hugging Face Spaces / OpenXLab)
大多数 Gradio 托管平台支持通过 `packages.txt` 文件安装系统级依赖。本项目已包含该文件，平台会自动识别并安装 `ffmpeg`。

如果平台不支持 `packages.txt`，程序在启动时也会尝试自动执行 `apt-get install` 进行安装。

---

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
