#!/bin/bash
set -e

echo "=========================================="
echo "  Video Parser - Starting Services"
echo "=========================================="

# 创建必要的目录
mkdir -p static/videos static/images downloads cache logs

# 启动合并后的服务 (包含 FastAPI 后端和 Gradio 前端)
echo "Starting Unified Service on port 7860..."
python app.py