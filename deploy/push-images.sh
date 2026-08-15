#!/bin/bash
# Muye Docker 镜像推送脚本
# 用法：DOCKER_HUB_PASSWORD=<your-token> ./deploy/push-images.sh

set -e

DOCKER_HUB_USER="${DOCKER_HUB_USER:-jimmydou}"
IMAGE_PREFIX="${DOCKER_HUB_USER}/muye"

if [[ -z "${DOCKER_HUB_PASSWORD:-}" ]]; then
  echo "错误：请设置 DOCKER_HUB_PASSWORD 环境变量" >&2
  echo "用法：DOCKER_HUB_PASSWORD=<your-token> ./deploy/push-images.sh" >&2
  exit 1
fi

echo "=========================================="
echo "Muye Docker 镜像推送脚本"
echo "=========================================="

# 1. 登录 Docker Hub
echo ""
echo "[1/8] 登录 Docker Hub..."
echo "${DOCKER_HUB_PASSWORD}" | docker login -u "${DOCKER_HUB_USER}" --password-stdin

# 2. 构建所有镜像（如果尚未构建）
echo ""
echo "[2/8] 确保所有镜像已构建..."
docker compose build

# 3. 标记并推送镜像
IMAGES=(
    "muye-gateway:latest"
    "muye-control:latest"
    "muye-dashboard-api:latest"
    "muye-agent-main:latest"
    "muye-muye-llm:latest"
    "muye-muye-data:latest"
)

for img in "${IMAGES[@]}"; do
    LOCAL="${img}"
    REMOTE="${IMAGE_PREFIX}-${img}"
    echo ""
    echo "[推送] ${LOCAL} → ${REMOTE}"
    docker tag "${LOCAL}" "${REMOTE}"
    docker push "${REMOTE}"
done

# 4. 推送 hotel-employee 子 Agent
echo ""
echo "[推送] muye/agent-hotel-employee:0.1.0 → ${IMAGE_PREFIX}-agent-hotel-employee:0.1.0"
docker tag "muye/agent-hotel-employee:0.1.0" "${IMAGE_PREFIX}-agent-hotel-employee:0.1.0"
docker push "${IMAGE_PREFIX}-agent-hotel-employee:0.1.0"

echo ""
echo "=========================================="
echo "所有镜像推送完成！"
echo "=========================================="
echo ""
echo "镜像列表："
echo "  ${IMAGE_PREFIX}-muye-gateway:latest"
echo "  ${IMAGE_PREFIX}-muye-control:latest"
echo "  ${IMAGE_PREFIX}-muye-dashboard-api:latest"
echo "  ${IMAGE_PREFIX}-muye-agent-main:latest"
echo "  ${IMAGE_PREFIX}-muye-muye-llm:latest"
echo "  ${IMAGE_PREFIX}-muye-muye-data:latest"
echo "  ${IMAGE_PREFIX}-agent-hotel-employee:0.1.0"
