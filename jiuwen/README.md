# agentarts-instrumentation-jiuwen

OpenJiuwen 语义包：向华为云 AgentArts（agent-ops）上报 openjiuwen / jiuwenswarm 智能体的 Trace 数据。

- 注册 `agentarts` 投影派生：openjiuwen 数据契约（`openjiuwen.*` 属性、span 命名形态）→ AgentArts `gen_ai.*` 平台语义（span.kind 派生、token/输入跨 span 聚合、AGENT 根状态升级、技能识别、显示名压缩等，详见 [docs/design.md](../docs/design.md) §4.3）
- 提供 openjiuwen 宿主注入：包装 `init_observability(..., span_exporter_override=...)`，宿主原生导出已指向 AgentArts 网关时自动让位（防重复上报）
- 对 openjiuwen **零 import 依赖**：只读取其属性数据契约，可与任意宿主版本共存

## 安装

```bash
pip install "agentarts-instrumentation-jiuwen[grpc]"   # 自动安装 core；gRPC 为 AgentArts 接入网关必需
```

## 快速开始

方式一（standalone）：把 exporter 交给宿主的初始化钩子。

```python
import agentarts_instrumentation.jiuwen  # 注册 agentarts 语义派生
from agentarts_instrumentation import ExporterConfig, Projection, build_exporter

exporter = build_exporter(
    ExporterConfig(
        endpoint="https://apm-access.cn-southwest-2.myhuaweicloud.com",
        protocol="grpc",
        headers={"authentication": "<access_token>"},
        projection=Projection(
            preset="agentarts",
            service_name="AgentArts.<agent_id>.default",   # 平台要求的服务名
        ),
    )
)

# openjiuwen 宿主初始化时注入
init_observability(config, span_exporter_override=exporter)
```

方式二（一键注入）：安装后执行 `agentarts-inject`，向当前 venv 的 site-packages 写入 `.pth`，此后每个解释器进程自动完成注入；执行 `agentarts-uninject` 撤销。

```bash
agentarts-inject
```

## 访问凭证

通过环境变量提供（或用 `AGENTARTS_ENV_FILE` 指向 .env 文件，进程环境优先）：

| 变量 | 说明 |
|---|---|
| `agentarts_agentops_apm_token` | 接入 token（必需） |
| `agent_resource_id` | 智能体资源 ID（必需） |
| `agentarts_agentops_apm_exporter_endpoint` | 接入网关（可选，默认 cn-southwest-2） |
| `agentarts_agentops_otel_agent_apm_enable` | 显式 `false/0/off` 关闭上报 |
| `AGENTARTS_TRACE_TOKEN` / `AGENTARTS_AGENT_ID` / `AGENTARTS_TRACE_ENDPOINT` | 0.1.x 旧名（兼容回退，已废弃） |

## 测试

```bash
uv run pytest jiuwen/tests                     # 单元组（默认全跑）
uv run pytest jiuwen/tests/integration         # 需本地 agent-core checkout（AGENT_CORE_DIR）
uv run pytest jiuwen/tests/live                # 需真实平台凭证
```

## 文档

- [设计文档](../docs/design.md)（§4.3 agentarts 派生、§4.5 缓冲、§6 接入）
- [接入指南](../docs/接入指南.md)（逐步操作手册）
- [Tutorial](../docs/tutorial.md)
