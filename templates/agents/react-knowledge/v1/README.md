# react-knowledge/v1

这是 v2.0 的标准只读知识 Agent 模板源。它不是可直接部署的 Agent 目录：阶段 3 的 Generator 会以受限
`AgentGenerationSpecV1` 渲染 `*.tmpl` 文件，并写入 `agent.yaml`、Prompt、测试和 provenance。

模板只使用 SDK 的 `create_scoped_data_retrieval_tool()`。模型唯一可控的工具参数是 query；资源、pipeline、
scope、返回字段和 `DataAccessContext` 均由可信生成/部署输入固定。部署必须设置
`MUYE_AGENT_SERVICE_ID`、`MUYE_AGENT_DEPLOYMENT_ID`、`MUYE_AGENT_DESCRIPTOR_CHECKSUM` 和
`MUYE_AGENT_SOURCE_TREE_CHECKSUM`，这些值不能来自用户请求或模型输出。

构建时必须提供公共、不可变的镜像引用：

```bash
docker build --build-arg MUYE_AGENT_BASE_IMAGE='python:3.12-slim@sha256:<digest>' .
```

`requirements.txt` 固定到 SDK `v2.0.0` tag 的 HTTPS 源码归档，因此镜像构建不依赖 Git 客户端。生产 base image
仍必须使用不可变 digest，并在构建阶段允许访问该已审阅的公开发布地址；容器启动时不会下载 Python 包。
