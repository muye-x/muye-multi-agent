# Muye Multi-Agent Scaffold 生产部署指南

## 目录结构

```
deploy/
├── README.md                    # 本文档
├── .env.production              # 环境变量模板
├── compose.production.yaml      # 生产环境 Compose 配置
├── init-db.sql                  # 数据库初始化脚本
└── push-images.sh               # 镜像推送脚本
```

## 一、镜像推送

### 1.1 需要推送的镜像（8个）

| 镜像名 | 说明 |
|--------|------|
| `jimmydou/muye-muye-gateway:latest` | Nginx + 前端 |
| `jimmydou/muye-muye-control:latest` | 控制服务器 |
| `jimmydou/muye-muye-dashboard-api:latest` | 仪表盘 API |
| `jimmydou/muye-muye-agent-main:latest` | 主 Agent |
| `jimmydou/muye-muye-llm:latest` | LLM 代理 |
| `jimmydou/muye-muye-data:latest` | RAG 检索 |
| `jimmydou/muye-agent-hotel-employee:0.1.0` | 酒店员工手册子 Agent |
| `jimmydou/muye-muye-channels:latest` | 微信 iLink Channel |

### 1.2 公共镜像（自动拉取，无需推送）

- `postgres:16-alpine`
- `milvusdb/milvus:v2.5.10`
- `minio/minio:RELEASE.2024-10-13T13-34-11Z`
- `quay.io/coreos/etcd:v3.5.18`

### 1.3 执行推送

```bash
# 方式一：使用脚本
chmod +x deploy/push-images.sh
./deploy/push-images.sh

# 方式二：手动推送
docker login -u jimmydou
docker tag muye-gateway jimmydou/muye-muye-gateway:latest
docker push jimmydou/muye-muye-gateway:latest
# ... 对其他镜像重复
```

---

## 二、生产环境部署

### 2.1 前置条件

- Docker 24+ 和 Docker Compose V2
- 至少 4GB 内存（Milvus 需要较多内存）
- 开放端口：8080（HTTP）

### 2.2 部署步骤

```bash
# 1. 克隆代码
git clone <repo-url> muye-scaffold
cd muye-scaffold

# 2. 配置环境变量
cp deploy/.env.production .env
vim .env  # 填写实际值（密码、token 等）

# 3. 生成 token（在 .env 中填写）
openssl rand -hex 32  # 生成 64 位十六进制 token

# 还需要生成 base64 编码的 32 字节微信 Channel 加密密钥
.venv/bin/python -c "import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"

# 4. 启动 Milvus（独立 compose）
docker compose -f poc/phase1/milvus/compose.yaml up -d

# 5. 构建并启动主服务（包含微信 channels）
docker compose -f deploy/compose.production.yaml up -d --build

# 6. 初始化数据库（首次部署）
docker exec -i muye-postgres-1 psql -U muye -d muye < deploy/init-db.sql

# 7. 初始化 Admin 用户
# 访问 http://your-server:8080，系统会引导创建 admin 账号
# 或通过 API：
curl -X POST http://localhost:8080/api/v2/auth/bootstrap \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"your-strong-password"}'

# 8. 构建知识库（如果需要酒店员工手册）
python -m tools.cli knowledge build hotel-employee \
  --import-root agent-projects/hotel-employee \
  --milvus-uri http://localhost:19530

# 9. 验证部署
curl http://localhost:8080/gateway/health
# 预期返回：{"status":"healthy"}
```

### 2.3 微信 Channel 配置

生产 Compose 会启动 `channels`，并通过 Gateway 的已认证 `/api/v2/channels/` 路由提供控制台绑定功能。将以下值加入部署 `.env`：

```dotenv
MUYE_CHANNELS_CALLER_TOKEN=<独立的 gateway-to-channels token>
MUYE_CHANNELS_MAIN_TOKEN=<独立的 channels-to-main token>
MUYE_CHANNELS_ENCRYPTION_KEY=<base64 编码的 32 字节 AES-GCM 密钥>
WECHAT_ILINK_BASE_URL=https://ilinkai.weixin.qq.com/ilink/bot
WECHAT_ILINK_ALLOWED_HOSTS=ilinkai.weixin.qq.com
```

`MUYE_CHANNELS_CALLER_TOKEN` 必须同时供 Gateway 和 channels 使用，`MUYE_CHANNELS_MAIN_TOKEN` 必须同时供 channels 和 Agent Main 使用；两者及加密密钥都不得复用 Control、Gateway 或 SubAgent 的 token。登录控制台，在“微信”页完成二维码确认后再验证消息收发。详见[微信 Channel 接入](../docs/wechat-channel.md)。

### 2.4 数据持久化

| 数据 | Docker Volume | 说明 |
|------|---------------|------|
| PostgreSQL | `postgres-data` | 用户、授权、对话历史 |
| Control State | `control-state` | Catalog、Citation |
| Milvus | `phase1-milvus` | 知识库向量 |
| 微信 Channel | PostgreSQL `channel_*` 表 | 绑定、游标、消息和投递状态 |

---

## 三、数据管理

### 3.1 PostgreSQL

**Schema 初始化**：通过 `deploy/init-db.sql` 自动执行（挂载到 `/docker-entrypoint-initdb.d/`）

**种子数据**：
- hermes 用户（API 调用用）
- Agent 授权关系

**Admin 用户**：通过 bootstrap API 创建（首次访问时）

**备份**：
```bash
docker exec muye-postgres-1 pg_dump -U muye -d muye > backup.sql
```

**恢复**：
```bash
docker exec -i muye-postgres-1 psql -U muye -d muye < backup.sql
```

### 3.2 Milvus（知识库向量）

**初始化**：需要通过知识库构建脚本重新索引

```bash
# 构建酒店员工手册知识库
python -m tools.cli knowledge build hotel-employee \
  --import-root agent-projects/hotel-employee \
  --milvus-uri http://localhost:19530
```

**备份**：Milvus 数据存储在 Docker Volume 中，可通过 Volume 备份

**注意**：Milvus 数据不需要随镜像分发，每次部署后重新构建即可

### 3.3 Catalog（Agent 注册信息）

Catalog 存储在 `control-state` Volume 中的 `active-catalog.json`。

首次部署时，需要通过 `scripts/muye.sh agent sync` 生成并上传 Catalog。

---

## 四、常用运维命令

```bash
# 查看服务状态
docker compose -f deploy/compose.production.yaml ps

# 查看日志
docker compose -f deploy/compose.production.yaml logs -f agent-main

# 重启单个服务
docker compose -f deploy/compose.production.yaml restart agent-main

# 更新镜像并重启
docker compose -f deploy/compose.production.yaml pull
docker compose -f deploy/compose.production.yaml up -d

# 停止所有服务
docker compose -f deploy/compose.production.yaml down

# 停止并删除数据（危险！）
docker compose -f deploy/compose.production.yaml down -v
```

---

## 五、安全建议

1. **Token 管理**：所有 token 使用 `openssl rand -hex 32` 生成，不要使用示例中的值
2. **网络隔离**：`internal` 网络标记为 `internal: true`，外部无法直接访问
3. **只读文件系统**：Gateway 容器使用 `read_only: true`
4. **资源限制**：建议为各服务添加 `deploy.resources.limits`
5. **HTTPS**：生产环境建议配置 TLS，在 `.env` 中设置 `MUYE_GATEWAY_MODE=https`
