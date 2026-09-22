#!/usr/bin/env bash
# ==============================================================================
#  Video Parser 一键部署 / 健康检查 / 验证脚本
#  适用环境: Docker（推荐，Windows Git Bash / Linux / macOS 均可运行）
#  目标应用: FastAPI + Gradio + Qwen3-VL 多平台视频解析系统
#  统一服务端口: 7860  (API + Web UI 共用)
#  健康检查接口: GET /health
# ==============================================================================
set -euo pipefail

# ----------------------------- 可配置参数 --------------------------------------
APP_NAME="video-parser"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${PORT:-7860}"
# 默认本地构建镜像；如需使用预构建镜像，取消下一行注释：
# IMAGE="registry.cn-hangzhou.aliyuncs.com/chuchengzhi/video-parser:latest"
IMAGE="${IMAGE:-video-parser:local}"
ENV_FILE="$APP_DIR/.env"
ENV_EXAMPLE="$APP_DIR/.env.example"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-90}"   # 健康检查最长等待秒数
CONTAINER_NAME="$APP_NAME"

# ----------------------------- 颜色与日志 -------------------------------------
log()  { echo -e "\033[32m[INFO]\033[0m  $*"; }
warn() { echo -e "\033[33m[WARN]\033[0m  $*"; }
err()  { echo -e "\033[31m[ERROR]\033[0m $*"; }
ok()   { echo -e "\033[36m[ OK ]\033[0m  $*"; }

# ----------------------------- 前置检查 ---------------------------------------
check_prerequisites() {
  log "检查运行环境依赖..."
  local missing=0
  for cmd in docker curl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      err "未找到命令: $cmd（请先安装 Docker Desktop 与 curl）"
      missing=1
    fi
  done
  # docker 守护进程是否可用
  if ! docker info >/dev/null 2>&1; then
    err "Docker 守护进程未运行，请先启动 Docker Desktop。"
    missing=1
  fi
  [ "$missing" -eq 0 ] && ok "前置依赖检查通过"
  return $missing
}

# ----------------------------- 环境文件 ---------------------------------------
setup_env() {
  if [ ! -f "$ENV_FILE" ]; then
    if [ ! -f "$ENV_EXAMPLE" ]; then
      err "缺少 .env.example，无法生成配置文件。"
      return 1
    fi
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    warn "已根据 .env.example 生成 .env，请编辑 $ENV_FILE 填入 QWEN_API_KEY。"
  fi
  # 校验关键变量
  if grep -q "QWEN_API_KEY=your-modelscope-api-key-here" "$ENV_FILE" 2>/dev/null; then
    warn "QWEN_API_KEY 仍为占位符，AI 视频内容提取功能将不可用（解析/下载功能不受影响）。"
    warn "请编辑 $ENV_FILE 填入真实的 ModelScope API Key: https://modelscope.cn/my/myaccesstoken"
  fi
}

# ----------------------------- 持久化目录 -------------------------------------
create_dirs() {
  log "创建持久化目录..."
  mkdir -p "$APP_DIR/static/videos" "$APP_DIR/static/images" \
           "$APP_DIR/downloads" "$APP_DIR/cache" "$APP_DIR/logs"
  ok "目录已就绪"
}

# ----------------------------- 构建镜像 ---------------------------------------
build_image() {
  if [ "${SKIP_BUILD:-0}" = "1" ]; then
    log "跳过构建（SKIP_BUILD=1）"
    return 0
  fi
  if [ -f "$APP_DIR/Dockerfile" ]; then
    log "使用本地 Dockerfile 构建镜像: $IMAGE"
    docker build -t "$IMAGE" "$APP_DIR"
    ok "镜像构建完成: $IMAGE"
  else
    warn "未找到 Dockerfile，尝试拉取预构建镜像..."
    docker pull "$IMAGE"
  fi
}

# ----------------------------- 启动容器 ---------------------------------------
start_container() {
  log "停止并移除旧容器（若存在）..."
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

  log "启动容器 $CONTAINER_NAME（端口 $PORT:$PORT）..."
  docker run -d \
    --name "$CONTAINER_NAME" \
    -p "${PORT}:${PORT}" \
    --env-file "$ENV_FILE" \
    -e TZ=Asia/Shanghai \
    -e PYTHONUNBUFFERED=1 \
    -v "$APP_DIR/static/videos:/app/static/videos" \
    -v "$APP_DIR/static/images:/app/static/images" \
    -v "$APP_DIR/downloads:/app/downloads" \
    -v "$APP_DIR/cache:/app/cache" \
    -v "$APP_DIR/logs:/app/logs" \
    --restart unless-stopped \
    "$IMAGE"
  ok "容器已启动"
}

# ----------------------------- 健康检查 ---------------------------------------
wait_health() {
  local url="http://localhost:${PORT}/health"
  log "等待服务健康检查通过（最长 ${HEALTH_TIMEOUT}s）..."
  local waited=0
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    if curl -fsS "$url" >/dev/null 2>&1; then
      ok "健康检查通过: $url"
      return 0
    fi
    sleep 3
    waited=$((waited + 3))
  done
  err "健康检查超时（${HEALTH_TIMEOUT}s）。服务可能启动失败，请查看日志: docker logs $CONTAINER_NAME"
  return 1
}

# ----------------------------- 部署验证 ---------------------------------------
verify() {
  local base="http://localhost:${PORT}"
  log "执行部署验证..."
  # 1. 健康检查接口
  if curl -fsS "$base/health" | grep -qi "ok\|healthy\|200\|succ"; then
    ok "/health 接口返回正常"
  else
    warn "/health 返回内容异常，请手动确认"
  fi
  # 2. 文档与首页可达性
  for path in "/" "/docs" "/redoc"; do
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" "$base$path" || echo "000")
    if [ "$code" = "200" ]; then
      ok "GET $path -> HTTP $code"
    else
      warn "GET $path -> HTTP $code（非 200，请关注）"
    fi
  done
  # 3. 解析接口冒烟测试（仅验证接口可响应，不依赖真实外网链接）
  local parse_code
  parse_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$base/api/parse" \
    -H "Content-Type: application/json" \
    -H "X-Timestamp: $(date +%s)000" \
    -H "X-GCLT-Text: smoke" \
    -H "X-EGCT-Text: smoke" \
    -d '{"text":"https://www.bilibili.com/video/BV1xx411c7mD"}' || echo "000")
  log "解析接口冒烟测试 -> HTTP $parse_code（4xx/5xx 多为外网链接失效，属正常现象）"
}

# ----------------------------- 辅助命令 ---------------------------------------
cmd_status() {
  docker ps --filter "name=$CONTAINER_NAME" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
  echo "--- 容器健康状态 ---"
  docker inspect -f '{{.State.Health.Status}}' "$CONTAINER_NAME" 2>/dev/null || echo "无健康探针(非本脚本启动)"
}
cmd_logs()   { docker logs -f --tail=100 "$CONTAINER_NAME"; }
cmd_stop()   { docker stop "$CONTAINER_NAME" && docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1; ok "已停止并移除容器"; }
cmd_restart(){ docker restart "$CONTAINER_NAME" && ok "已重启"; wait_health && verify; }
cmd_update() {
  SKIP_BUILD=0 build_image
  cmd_stop >/dev/null 2>&1 || true
  start_container
  wait_health && verify
}

# ----------------------------- 主流程 -----------------------------------------
usage() {
  cat <<EOF
用法: ./deploy.sh <command> [选项]

命令:
  deploy    完整部署: 检查依赖 -> 生成配置 -> 构建镜像 -> 启动 -> 健康检查 -> 验证
  status    查看容器状态与健康探针
  logs      实时查看容器日志 (Ctrl+C 退出)
  stop      停止并移除容器
  restart   重启容器并执行健康检查与验证
  update    重新构建镜像并重启（用于版本更新）

环境变量:
  PORT          服务端口 (默认 7860)
  IMAGE         镜像名 (默认 video-parser:local，可改为阿里云预构建镜像)
  SKIP_BUILD=1  跳过镜像构建
  HEALTH_TIMEOUT 健康检查等待秒数 (默认 90)

示例:
  ./deploy.sh deploy
  IMAGE=registry.cn-hangzhou.aliyuncs.com/chuchengzhi/video-parser:latest ./deploy.sh deploy
  ./deploy.sh status
EOF
}

main() {
  local cmd="${1:-deploy}"
  case "$cmd" in
    deploy)
      check_prerequisites || exit 1
      setup_env
      create_dirs
      build_image
      start_container
      if wait_health; then
        verify
        echo
        ok "部署成功！访问:  Web UI -> http://localhost:${PORT}   API 文档 -> http://localhost:${PORT}/docs"
      else
        err "部署未完成，请运行: ./deploy.sh logs 排查"
        exit 2
      fi
      ;;
    status|logs|stop|restart|update) "cmd_$cmd" ;;
    -h|--help|help) usage ;;
    *) err "未知命令: $cmd"; usage; exit 1 ;;
  esac
}

main "$@"
