# Video Parser 部署指南

## 容器镜像信息

- **镜像仓库**: `registry.cn-hangzhou.aliyuncs.com/chuchengzhi/video-parser`
- **最新版本**: `latest`, `v2.0.1`
- **镜像大小**: ~2.25GB

## 快速部署

### 方式一：使用 docker-compose（推荐）

```bash
# 1. 克隆项目或下载 docker-compose.yml
git clone <repository-url>
cd video-parser

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 文件，配置必要的环境变量

# 3. 启动服务
docker-compose up -d

# 4. 查看日志
docker-compose logs -f

# 5. 停止服务
docker-compose down
```

### 方式二：使用 docker run

```bash
# 1. 拉取镜像
docker pull registry.cn-hangzhou.aliyuncs.com/chuchengzhi/video-parser:latest

# 2. 创建必要的目录
mkdir -p static/videos static/images downloads cache logs

# 3. 启动容器
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
  registry.cn-hangzhou.aliyuncs.com/chuchengzhi/video-parser:latest
```

## 环境变量配置

创建 `.env` 文件并配置以下变量：

```bash
# Qwen3-VL API 配置
QWEN_API_BASE_URL=https://api-inference.modelscope.cn/v1
QWEN_API_KEY=your-modelscope-api-key-here
QWEN_MODEL_ID=Qwen/Qwen3-VL-8B-Instruct
MAX_ANALYSIS_FRAMES=24
FRAME_INTERVAL_SECONDS=2.0
SCENE_CHANGE_THRESHOLD=0.32
CHANGE_DETECTION_FPS=2.0
TEXT_REGION_CHANGE_THRESHOLD=0.10
VISION_BATCH_SIZE=6

# 服务配置
API_SERVER_URL=http://127.0.0.1:7860
```

## 访问地址

- **Web 界面**: http://localhost:7860
- **API 文档**: http://localhost:7860/docs
- **ReDoc 文档**: http://localhost:7860/redoc
- **健康检查**: http://localhost:7860/health

## 功能特性

- ✅ 多平台视频解析（抖音、B站、小红书、快手、好看视频）
- ✅ 无水印视频下载
- ✅ 在线视频播放
- ✅ 时间轴级证据分析（Qwen3-VL + 独立字幕轨 + 可选 ASR）
- ✅ RESTful API 接口
- ✅ 健康检查和监控

## 数据持久化

容器会挂载以下目录到宿主机：

- `./static/videos` - 静态视频文件
- `./static/images` - 静态图片文件  
- `./downloads` - 下载的视频文件
- `./cache` - 缓存文件
- `./logs` - 应用日志

## 健康检查

容器内置健康检查，每30秒检查一次服务状态：

```bash
# 手动检查健康状态
curl http://localhost:7860/health
```

## 故障排除

### 查看容器日志
```bash
docker logs video-parser
# 或使用 docker-compose
docker-compose logs video-parser
```

### 进入容器调试
```bash
docker exec -it video-parser bash
```

### 重启服务
```bash
docker-compose restart video-parser
```

## 版本更新

```bash
# 拉取最新镜像
docker pull registry.cn-hangzhou.aliyuncs.com/chuchengzhi/video-parser:latest

# 重新启动服务
docker-compose down
docker-compose up -d
```

## 注意事项

1. **AI 功能**: 需要配置有效的 `QWEN_API_KEY`；如需纯语音转写，另行配置 `ASR_MODEL_ID`（及可选的 `ASR_API_BASE_URL`、`ASR_API_KEY`）
2. **B站视频**: 需要 ffmpeg 支持音视频合并（镜像已内置）
3. **网络访问**: 确保容器能够访问外部网络以下载视频
4. **存储空间**: 建议预留足够的磁盘空间用于视频下载和缓存
